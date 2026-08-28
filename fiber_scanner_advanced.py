# =============================================================================
# 光纖對準尋光演算法 — Powell 共軛方向法（替代路徑）
#
# 設計依據：FIBER_ALIGNMENT_SCAN_DESIGN.md〈第三輪：多軸尋光演算法候選評估
# （2026-08-27）〉。該節記錄了 mathematician 兩輪落地設計意見與 architect
# 的相容性／檔案切分評估，本檔是「本輪結論」第 5 點的落地實作。
#
# 🔴 範圍界定（務必先讀）：
#   - 本檔**只取代**既有三階段設計（見 fiber_scanner.py）的階段一（座標
#     下降）＋ 階段二（K 近鄰局部加權迴歸精修）。階段零（盲搜粗掃找起點）
#     與階段三（收尾微擾）維持呼叫 fiber_scanner.py 原本的方法——本模組
#     不負責銜接、也不負責跑完整三階段流程，這是呼叫端（GUI／腳本）的
#     責任。典型用法：scanner.run_stage0(...) → run_stage_powell(scanner)
#     → scanner.run_stage3()。
#   - 不重新實作任何安全邏輯。移動一律經由 scanner._move_multi_axis()
#     （內部已含 _targets_reachable() 行程檢查與 _note_limit_hit() 記錄），
#     量測一律經由 scanner._measure_here()，中止語意一律沿用
#     scanner._check_abort() / ScanAbort / NoSignalAbort。這些函式所在的
#     fiber_scanner.py 承載一系列事故修正後的安全不變量，本檔不得複製、
#     也不會被改寫——architect 已明確要求維持單一實作來源。
#
# 待實測校準（目前全部是起跳值，見設計文件〈本輪結論〉第 6 點）：
#   - xtol_pulse / ftol_sigma_mult：容差量級對不對，要等峰附近實測
#     dB/pulse 斜率與 calibrate_noise() 實機 σ 才能收斂到合理值。
#   - penalty_lambda：軟牆懲罰的 dB/pulse 斜率，目前 0.01 是保守猜測，
#     需要用實測的峰附近斜率校準（斜率太小，Powell 會覺得「多繞一點
#     行程外沒關係」；太大則會把邊界附近的合法搜尋方向也一併打死）。
# =============================================================================

from typing import Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

# scipy 是這個模組唯一新增的相依（main requirements.txt 已含 scipy==1.16.2，
# 見 FIBER_ALIGNMENT_SCAN_DESIGN.md〈architect 意見〉的依賴分層建議）。
# 比照 fiber_scanner.py 對 xlsxwriter 的優雅降級模式：裝不到就不讓整個
# 模組 import 失敗，只在真正呼叫 run_stage_powell() 時才報錯——這樣即使
# 環境沒裝 scipy，其他呼叫端（例如未來的單元測試檔）仍能安全 import 本
# 模組去檢查其他內容而不會整支炸掉。
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

# 軟牆懲罰的預設斜率（dB/pulse）。⚠ 這是起跳值，不是校準值——見上方模組
# 說明。用途：撞限位或量測失敗時，Powell 仍需要一個「看起來比目前最佳點
# 差、但差距與越界距離成比例」的數字，才能讓三點拋物線內插算出正確方向；
# 用 inf 或大常數會讓內插退化成隨機亂走（見設計文件 mathematician 第二輪
# 意見）。
DEFAULT_PENALTY_LAMBDA = 0.01

# xtol／ftol_sigma_mult 的預設值。跟 DEFAULT_PENALTY_LAMBDA 同一個理由拉成
# 具名常數：fiber_scanner.py 的 _powell_param_snapshot() 直接讀這三個模組
# 常數組 metadata，不再用 inspect.signature 反射函式簽章預設值——這樣未來
# 校準這兩個參數只需要改這裡，不會有「改了簽章預設值、忘記還有地方在讀
# 簽章」這種對不上的風險（也不會在參數改名時讓 _powell_param_snapshot()
# 因為 KeyError 炸穿 persist_samples() 的 finally 保證）。
DEFAULT_XTOL_PULSE = 5.0
DEFAULT_FTOL_SIGMA_MULT = 3.0

# 量測本身失敗（非撞限位、非中止）時的固定懲罰（dB）。同樣是保守起跳值：
# 只要比雜訊底限明顯大、又不會大到把整個搜尋方向帶偏即可。
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
      - scanner.run() 已跑過（或呼叫端已手動設好 scanner.active_axes），
        本函式不會自己決定要搜哪些軸。
      - scanner._noise_floor() 已校準（scanner.calibrate_noise() 跑過），
        否則 ftol 會退化成 0（見 fiber_scanner._noise_floor 的文件字串），
        Powell 會一路跑到 maxfev 才停，失去容差的意義。

    行為特性（對應設計文件〈mathematician 第二輪意見〉逐條落地）：
      - objective 內部用整數 pulse 座標當快取 key，避免線搜末期在 <1
        pulse 範圍內重複移動＋量測。
      - 撞限位／_move_multi_axis 失敗、或量測失敗，一律回軟牆懲罰值，
        絕不拋例外中斷 minimize()。
      - 使用者中止／EMS（ScanAbort、NoSignalAbort）則相反：一律自然
        往外傳，但外層會先把滑台移回目前看過的最佳座標，才重新拋出。

    `early_signal_check`：每次 objective() 真正完成一次測量（不是快取
    命中、也不是撞限位/量測失敗的懲罰分支）之後呼叫一次，通常是呼叫端
    包好的 `scanner._check_signal_detectable(...)`。用途：座標下降在
    `run_stage1()` 的 cycle==1 就會做這個檢查，十幾筆樣本內就能判定
    無訊號並中止；Powell 若沒有這個 hook，要等 `minimize()` 整個跑完
    （最多 `max_iterations` 次移動＋量測，真機是數分鐘量級）才有機會
    觸發，`abort_if_no_signal` 對 Powell 路徑形同虛設。它若拋例外
    （NoSignalAbort／ScanAbort），刻意讓例外沿 objective() → minimize()
    → 本函式的呼叫鏈自然往外傳，不透過 scipy 的 `callback=` 參數——
    那條路徑依賴 scipy 內部怎麼處理 callback 拋出的例外，不保證跨版本
    行為一致，直接在我們自己控制的 objective() 內呼叫才可靠。是否要
    「至少跑 N 次才檢查」這類門檻邏輯不在這裡做——`_check_signal_detectable()`
    本身已經有「有效樣本 <4 時不誤殺」的保護，不重複造第二套判斷。
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
    # best-so-far：中止時要把滑台移回這裡，而不是留在中止當下（可能不是
    # 目前看過的最佳點——例如正處在軟牆懲罰的越界候選點上）。用一個有限的
    # 保守初值（不是 -inf）：若還沒有任何一次成功量測，軟牆懲罰公式
    # `-best_power + lam*l1` 用 -inf 會直接算出 inf，讓三點拋物線內插拿到
    # 垃圾值；-1000 dBm 已遠低於 HP 8153A 任何可能讀值，效果等同「目前
    # 還沒有已知最佳點」但不會產生 inf。
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
            # 撞限位／行程檢查否決：軟牆懲罰，不拋例外（見模組說明）。
            # L1 距離用「原本想去的目標座標」相對「目前實際座標」計算——
            # _move_multi_axis 失敗時滑台可能完全沒動（_targets_reachable
            # 擋下）或部分軸已移動後才被 stop()，兩種情況都用
            # positions_machine 的最新值當基準，距離量的是「還差多遠」。
            actual = {ax: scanner.ctrl.positions_machine[ax] for ax in axes}
            l1 = sum(abs(target[ax] - actual[ax]) for ax in axes)
            penalty = -best_state["power"] + lam * l1
            cache[int_coords] = penalty
            return penalty

        sample = scanner._measure_here()
        if not sample.ok or sample.power is None:
            # 量測失敗（非撞限位）：同樣不拋例外，用固定小懲罰疊在目前
            # 最佳值之上，讓 Powell 認為這個方向「比目前已知最佳點差」，
            # 但不足以像撞限位那樣被判定成完全不可行的方向。
            penalty = -best_state["power"] + _MEASURE_FAIL_PENALTY_DB
            cache[int_coords] = penalty
            return penalty

        _record_best(target, sample.power)

        # 真正完成一次測量之後才觸發無訊號偵測（撞限位/量測失敗的懲罰
        # 分支、快取命中都不算）——早偵測的意義就是在這裡，讓判定跑在
        # 真實樣本累積的節奏上，不是等 minimize() 整個跑完。刻意不接
        # try/except：拋出的例外（NoSignalAbort/ScanAbort）要原樣往外
        # 傳，讓外層的 try/except (ScanAbort, NoSignalAbort) 接手善後。
        if early_signal_check is not None:
            early_signal_check()

        value = -sample.power
        cache[int_coords] = value
        return value

    x0 = np.array(
        [float(scanner.ctrl.positions_machine[ax]) for ax in axes], dtype=float
    )
    # 把目前起點也記進 best-so-far——即使 Powell 一次都沒有找到更好的
    # 點（例如第一步就中止），善後時仍有座標可以「移回去」（即原地不動）。
    # 🔴 這裡刻意**不**呼叫 _record_best()：它的 `or best_state["coords"] is
    # None` 短路條件會讓傳入的 power 值直接覆寫 best_state["power"]，若傳
    # -inf 會把上面費心設計避開的 -1000.0 哨兵值蓋回 -inf，之後第一次撞
    # 軟牆算 `penalty = -best_state["power"] + ...` 又會炸出 +inf——原地
    # 直接寫 dict，繞開這個短路條件。
    best_state["coords"] = {ax: int(round(v)) for ax, v in zip(axes, x0)}

    # 初始方向集合：不用 scipy 預設的單位矩陣（等同 1 pulse 步長，第一輪
    # 線搜會泡在雜訊裡），改用 step_min × REOPEN_STEP_MULT 量級的座標軸
    # 方向（見設計文件 mathematician 第二輪意見）。
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
        # 中止：把滑台移回目前看過的最佳座標再重新拋出，不能留在中止
        # 當下（那個座標可能是軟牆懲罰候選點，不是最佳點）。
        best_coords = best_state["coords"]
        if best_coords is not None:
            current = {ax: scanner.ctrl.positions_machine[ax] for ax in axes}
            back_deltas = {ax: best_coords[ax] - current[ax] for ax in axes}
            try:
                scanner._move_multi_axis(back_deltas)
            except (ScanAbort, NoSignalAbort):
                # 回退移動本身也可能因為再次中止/EMS 而被擋下——不能讓
                # 這裡的例外蓋掉原本要往外傳的中止原因，直接放棄回退。
                pass
        raise

    scanner._log(
        f"Powell 收斂完成：目前最佳 {best_state['power']:.3f} dBm，"
        f"座標 {best_state['coords']}"
    )

    # 收斂後保險對齊：用 scipy 回報的 result.x（不是 best_state，那個只是
    # 中止善後用的備援）round() 成整數。理論上不該跟目前實際座標有落差
    # ——每次 objective 呼叫已經真的移動過去、量測過——這裡只是防禦 Powell
    # 內部三點拋物線內插對 result.x 做最後一次浮點微調、但沒有真的再送一次
    # 移動指令的邊角情況。
    final_target = {ax: int(round(v)) for ax, v in zip(axes, result.x)}
    current = {ax: scanner.ctrl.positions_machine[ax] for ax in axes}
    residual = {ax: final_target[ax] - current[ax] for ax in axes}
    if any(dv != 0 for dv in residual.values()):
        scanner._move_multi_axis(residual)

    return dict(scanner.ctrl.positions_machine)
