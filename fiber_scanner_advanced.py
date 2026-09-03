# =============================================================================
# 光纖對準尋光演算法 — Powell 共軛方向法（替代路徑）
#
# 設計依據：FIBER_ALIGNMENT_SCAN_DESIGN.md〈第三輪：多軸尋光演算法候選評估
# （2026-08-27）〉「本輪結論」第 5 點的落地實作。
#
# 🔴 範圍界定：
#   - 只取代 fiber_scanner.py 三階段設計的階段一（座標下降）＋階段二（K
#     近鄰局部加權迴歸精修）。階段零（盲搜找起點）與階段三（收尾微擾）
#     仍呼叫 fiber_scanner.py 原方法，銜接是呼叫端（GUI／腳本）的責任：
#     scanner.run_stage0(...) → run_stage_powell(scanner) → scanner.run_stage3()。
#   - 不重新實作安全邏輯。移動一律經 scanner._move_multi_axis()（內含
#     _targets_reachable() 行程檢查與 _note_limit_hit() 記錄），量測一律
#     經 scanner._measure_here()，中止語意沿用 ScanAbort / NoSignalAbort。
#     這些函式承載一系列事故修正後的安全不變量，本檔不複製也不改寫
#     ——維持單一實作來源。
#
# 待實測校準（目前全是起跳值，見設計文件〈本輪結論〉第 6 點）：
#   - xtol_pulse / ftol_sigma_mult：要等峰附近實測 dB/pulse 斜率與
#     calibrate_noise() 實機 σ 才能收斂到合理值。
#   - penalty_lambda：軟牆懲罰斜率，目前 0.01 是保守猜測，需用實測峰
#     附近斜率校準（太小 Powell 會覺得越界沒差；太大會連邊界附近的合法
#     方向也一併打死）。
# =============================================================================

from typing import Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

# scipy 是本模組唯一新增的相依（requirements.txt 已含 scipy==1.16.2）。
# 比照 fiber_scanner.py 對 xlsxwriter 的優雅降級：裝不到不讓整個模組
# import 失敗，只在真正呼叫 run_stage_powell() 時才報錯，其他呼叫端
# 仍能安全 import 本模組。
try:
    from scipy.optimize import minimize  # type: ignore
    import numpy as np  # type: ignore

    _SCIPY_AVAILABLE = True
    _SCIPY_IMPORT_ERROR = ""
except ImportError as _scipy_err:  # pragma: no cover - 取決於執行環境
    minimize = None  # type: ignore
    np = None  # type: ignore
    _SCIPY_AVAILABLE = False
    _SCIPY_IMPORT_ERROR = str(_scipy_err)

from fiber_scanner import REOPEN_STEP_MULT, ScanAbort, NoSignalAbort

if TYPE_CHECKING:
    from fiber_scanner import FiberAlignmentScanner

# 軟牆懲罰的預設斜率（dB/pulse）。⚠ 起跳值，非校準值。撞限位或量測失敗時
# Powell 仍需要一個「比目前最佳點差、且差距與越界距離成比例」的數字，才能
# 讓三點拋物線內插算出正確方向；用 inf 或大常數會讓內插退化成隨機亂走。
DEFAULT_PENALTY_LAMBDA = 0.01

# xtol／ftol_sigma_mult 拉成具名常數：fiber_scanner.py 的
# _powell_param_snapshot() 直接讀這三個常數，不用 inspect.signature 反射
# 函式簽章預設值，避免改簽章忘了改這裡、或改名時 KeyError 炸穿
# persist_samples() 的 finally 保證。
DEFAULT_XTOL_PULSE = 5.0
DEFAULT_FTOL_SIGMA_MULT = 3.0

# 量測失敗（非撞限位、非中止）時的固定懲罰（dB）。起跳值：比雜訊底限
# 明顯大、又不足以把整個搜尋方向帶偏即可。
_MEASURE_FAIL_PENALTY_DB = 1.0


def run_stage_powell(
    scanner: "FiberAlignmentScanner",
    xtol_pulse: float = DEFAULT_XTOL_PULSE,
    ftol_sigma_mult: float = DEFAULT_FTOL_SIGMA_MULT,
    max_iterations: int = 200,
    penalty_lambda: Optional[float] = None,
    early_signal_check: Optional[Callable[[], None]] = None,
) -> Dict[str, float]:
    """
    用 scipy.optimize.minimize(method='Powell') 取代階段一＋階段二，對
    scanner.active_axes 做多軸聯合最佳化，回傳最終機械座標（pulse）。

    前置條件：
      - scanner.run() 已跑過（或已手動設好 scanner.active_axes）——本函式
        不會自己決定要搜哪些軸。
      - scanner._noise_floor() 已校準（跑過 calibrate_noise()），否則
        ftol 會退化成 0，Powell 會一路跑到 maxfev 才停，失去容差意義。

    行為特性：
      - objective 內部用整數 pulse 座標當快取 key，避免線搜末期在 <1
        pulse 範圍內重複移動＋量測。
      - 撞限位／_move_multi_axis 失敗、或量測失敗，一律回軟牆懲罰值，
        絕不拋例外中斷 minimize()。
      - 使用者中止／EMS（ScanAbort、NoSignalAbort）則相反：自然往外傳，
        但外層會先把滑台移回目前看過的最佳座標，才重新拋出。

    `early_signal_check`：每次 objective() 真正完成一次測量（非快取命中、
    非懲罰分支）後呼叫一次，通常是 `scanner._check_signal_detectable(...)`。
    座標下降在 `run_stage1()` cycle==1 就會做這個檢查，十幾筆樣本內即可
    判定無訊號並中止；Powell 若無此 hook，要等 `minimize()` 整個跑完
    （真機數分鐘量級）才有機會觸發。刻意不透過 scipy 的 `callback=`——
    那條路徑對 callback 拋出例外的處理不保證跨版本一致，直接在我們自己
    控制的 objective() 內呼叫才可靠。「至少跑 N 次才檢查」這類門檻邏輯
    不在這裡做——`_check_signal_detectable()` 本身已有「樣本 <4 不誤殺」
    的保護。
    """
    if not _SCIPY_AVAILABLE:
        raise RuntimeError(
            f"scipy 未安裝，無法使用 Powell 尋光——見 requirements.txt（{_SCIPY_IMPORT_ERROR}）"
        )

    axes: List[str] = list(scanner.active_axes)
    if not axes:
        raise RuntimeError("scanner.active_axes 是空的——run_stage_powell() 必須在 "
                            "scanner 已經決定搜尋軸範圍之後才能呼叫（例如先跑過階段零）")

    lam = DEFAULT_PENALTY_LAMBDA if penalty_lambda is None else float(penalty_lambda)

    # cache: 整數 pulse 座標 tuple（依 axes 順序）→ objective 值（-dBm 或懲罰值）
    cache: Dict[Tuple[int, ...], float] = {}
    # best-so-far：中止時滑台要移回這裡，而非留在中止當下（可能是軟牆懲罰
    # 的越界候選點）。初值用有限的 -1000 dBm（遠低於 HP 8153A 任何可能讀
    # 值）而非 -inf：否則第一次撞軟牆時 `-best_power + lam*l1` 會算出 inf。
    best_state = {"coords": None, "power": -1000.0}  # type: Dict[str, object]

    def _record_best(coords: Dict[str, int], power: float) -> None:
        if power > best_state["power"] or best_state["coords"] is None:
            best_state["power"] = power
            best_state["coords"] = dict(coords)

    def objective(x: "np.ndarray") -> float:
        # 1) round 成整數 pulse，查快取。
        int_coords = tuple(int(round(v)) for v in x)
        cached = cache.get(int_coords)
        if cached is not None:
            return cached

        target = dict(zip(axes, int_coords))
        current = {ax: scanner.ctrl.positions_machine[ax] for ax in axes}
        deltas = {ax: target[ax] - current[ax] for ax in axes}

        # ScanAbort/NoSignalAbort 刻意不在這裡攔截——讓它自然往外傳，
        # 外層 run_stage_powell() 的 try/except 才是負責善後的地方。
        moved = scanner._move_multi_axis(deltas)
        if not moved:
            # 撞限位／行程檢查否決：軟牆懲罰，不拋例外。L1 距離用目標座標
            # 相對目前實際座標算——失敗時滑台可能完全沒動（_targets_reachable
            # 擋下）或部分軸已移動才被 stop()，都以 positions_machine 最新值
            # 為準，量的是「還差多遠」。
            actual = {ax: scanner.ctrl.positions_machine[ax] for ax in axes}
            l1 = sum(abs(target[ax] - actual[ax]) for ax in axes)
            penalty = -best_state["power"] + lam * l1
            cache[int_coords] = penalty
            return penalty

        sample = scanner._measure_here()
        if not sample.ok or sample.power is None:
            # 量測失敗（非撞限位）：同樣不拋例外，固定小懲罰疊在目前最佳值
            # 之上——讓 Powell 認為這方向較差，但不到完全不可行的程度。
            penalty = -best_state["power"] + _MEASURE_FAIL_PENALTY_DB
            cache[int_coords] = penalty
            return penalty

        _record_best(target, sample.power)

        # 只在真正完成一次測量後才觸發無訊號偵測（懲罰分支、快取命中都不
        # 算），讓判定跟著真實樣本累積的節奏走。刻意不接 try/except：拋出
        # 的例外原樣往外傳，交給外層 try/except 善後。
        if early_signal_check is not None:
            early_signal_check()

        value = -sample.power
        cache[int_coords] = value
        return value

    x0 = np.array(
        [float(scanner.ctrl.positions_machine[ax]) for ax in axes], dtype=float
    )
    # 把目前起點記進 best-so-far，即使 Powell 一次都沒找到更好的點，善後
    # 時仍有座標可以「移回去」。🔴 刻意不呼叫 _record_best()：它的短路
    # 條件會讓 power 直接覆寫 best_state["power"]，把上面的 -1000.0 哨兵
    # 值蓋回 -inf；直接寫 dict 繞開這個短路。
    best_state["coords"] = {ax: int(round(v)) for ax, v in zip(axes, x0)}

    # 初始方向集合：不用 scipy 預設的單位矩陣（等同 1 pulse 步長，第一輪
    # 線搜會泡在雜訊裡），改用 step_min × REOPEN_STEP_MULT 量級的座標軸方向。
    initial_scale = scanner.step_min * REOPEN_STEP_MULT
    direc = np.eye(len(axes)) * initial_scale

    ftol = ftol_sigma_mult * scanner._noise_floor()

    try:
        result = minimize(
            objective,
            x0,
            method="Powell",
            options={
                "xtol": xtol_pulse,
                "ftol": ftol,
                "maxfev": max_iterations,
                "direc": direc,
            },
        )
    except (ScanAbort, NoSignalAbort):
        # 中止：把滑台移回目前看過的最佳座標再重新拋出，不留在可能是軟牆
        # 懲罰候選點的中止當下。
        best_coords = best_state["coords"]
        if best_coords is not None:
            current = {ax: scanner.ctrl.positions_machine[ax] for ax in axes}
            back_deltas = {ax: best_coords[ax] - current[ax] for ax in axes}
            try:
                scanner._move_multi_axis(back_deltas)
            except (ScanAbort, NoSignalAbort):
                # 回退移動也可能再次被中止/EMS 擋下——不能蓋掉原本要往外
                # 傳的中止原因，直接放棄回退。
                pass
        raise

    scanner._log(
        f"Powell 收斂完成：目前最佳 {best_state['power']:.3f} dBm，"
        f"座標 {best_state['coords']}"
    )

    # 收斂後保險對齊：result.x（非 best_state，那只是中止善後備援）round
    # 成整數。理論上不該與目前實際座標有落差，這裡只防禦 Powell 內部最後
    # 一次浮點微調沒有真的再送移動指令的邊角情況。
    final_target = {ax: int(round(v)) for ax, v in zip(axes, result.x)}
    current = {ax: scanner.ctrl.positions_machine[ax] for ax in axes}
    residual = {ax: final_target[ax] - current[ax] for ax in axes}
    if any(dv != 0 for dv in residual.values()):
        scanner._move_multi_axis(residual)

    return dict(scanner.ctrl.positions_machine)
