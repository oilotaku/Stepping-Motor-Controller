# =============================================================================
# 光纖對準尋光演算法 — FiberAlignmentScanner
#
# 設計依據（完整討論見 FIBER_ALIGNMENT_SCAN_DESIGN.md）：
#   - mathematician 代理兩輪設計：座標下降式模式搜尋（階段一）
#     + K 近鄰局部加權迴歸精修（階段二，按需啟用）+ 收尾微擾（階段三）
#   - architect 代理落地評估：獨立類別、scanning_active 安全整合、
#     多軸批次收尾邏輯（DS102 沒有單軸停止指令）、樣本一次性持久化
#
# 尚未接上真實 HP 8153A。power_query 透過依賴注入解耦，測試時可傳入
# 合成功率函式（例如高斯峰值曲線），不需要真實硬體即可驗證收斂性、
# 安全邏輯與樣本持久化——見 scratchpad 的 verify_fiber_scanner.py。
#
# 本檔刻意不在執行期 import main_ai.py：main_ai.py 未來若要接上 GUI，
# 會 import 這個檔案，若這裡也 import main_ai 會構成循環 import。
# 軸名/軸號對應因此在本檔自成一份（AXES/AXIS_NO/NO_AXIS），
# 必須與 main_ai.py 的定義保持一致——若那邊改了六軸命名，這裡要同步改。
# =============================================================================

import json
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from main_ai import DS102Controller

# 與 main_ai.py 一致的軸命名（見上方模組說明：刻意不 import，避免循環相依）
AXES = ["X", "Y", "Z", "U", "V", "W"]
AXIS_NO = {"X": "1", "Y": "2", "Z": "3", "U": "4", "V": "5", "W": "6"}
NO_AXIS = {v: k for k, v in AXIS_NO.items()}

# power_query()：呼叫一次即量測一次，回傳 (成功?, 功率dBm)。
# 契約與 meter_GPIB.HP8153APowerMeter.get_power() 一致（見該檔的修正說明）。
PowerQuery = Callable[[], Tuple[bool, float]]
ProgressCallback = Callable[[str], None]
# 每次「移動＋量測」完成（不論成功與否）都會呼叫一次，用於外部即時視覺化
# （例如即時軌跡圖）。跟 ProgressCallback 不同：那個只在階段/輪次等巨觀
# 里程碑觸發，這個是每一筆樣本都觸發，頻率高很多。
SampleCallback = Callable[["Sample"], None]

# ---- 以下數值皆為「起跳用」的保守預設，不是校準值 ----
# 全部待接上 HP 8153A、量出真實響應曲線與雜訊水準後才能校準，
# 詳細清單見設計文件〈待實測參數〉一節，不要當成已驗證的常數看待。
DEFAULT_STEP_MIN = 2           # 最小步長（pulse）。需 ≥ 機械重現性下限 ±1~2 pulse
DEFAULT_SETTLE_SEC = 0.03      # 到位後等機構震動衰減的時間
DEFAULT_MAX_CYCLES = 5         # 階段一外層座標下降的最多輪數
DEFAULT_NOISE_SIGMA_MULT = 3.0  # 功率雜訊底限＝重複量測標準差的幾倍
DEFAULT_NO_SIGNAL_RANGE_MULT = 2.0  # 全域無訊號偵測的雜訊底限倍數，見 _check_signal_detectable
REOPEN_STEP_MULT = 8           # 階段一第 2 輪起，每輪從 step_min×這個倍數重新收斂


@dataclass
class Sample:
    """
    一次量測樣本：機械座標（pulse）+ 功率讀值。

    `ok` 的語意是「這個讀值可信賴、可以拿去比較」，不是單純轉述
    meter_GPIB.get_power() 的通訊結果——它額外涵蓋了「數值本身是否
    低於絕對訊號下限」（見 FiberAlignmentScanner.min_valid_power_dbm）。

    `ok=False` 時 `power` 有兩種可能：
      - 通訊失敗（meter_GPIB 回 ok=False）→ power=None，沒有真實數值。
      - 讀值低於絕對下限（meter_GPIB 回 ok=True，但演算法判定太小不可信）
        → power=實際讀值，只是不參與比較邏輯，保留供除錯／事後繪圖使用。
    兩種情況都會在 `note` 記錄原因。既有的 `if s.ok and s.power is not None`
    比較寫法對兩種情況都正確排除，不需要另外分支。
    """

    coords: Dict[str, float]
    ok: bool
    power: Optional[float] = None
    note: str = ""
    ts: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return asdict(self)


class ScanAbort(Exception):
    """
    搜尋被中止：使用者主動停止、EMS 觸發、或起點量測失敗等不可續行的狀況。

    這是正常的中止路徑，不代表程式錯誤——`run()` 會捕捉它、寫出已收集的
    樣本，然後正常返回，不會讓例外一路炸到呼叫端。
    """


# =============================================================================
# 純數學工具：無母數局部迴歸用的小型線性代數（不依賴 numpy）
# =============================================================================
def _solve_linear_system(a: List[List[float]], b: List[float]) -> List[float]:
    """
    高斯消去法（含部分選主元）求解 Ax=b。

    階段二的迴歸自由度最多是「截距 + 6 軸」= 7 維，矩陣小到不需要
    numpy——這也讓 fiber_scanner.py 可以維持零額外相依套件。
    """
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot_row][col]) < 1e-12:
            raise ValueError("矩陣奇異，無法求解（樣本可能共線或數量不足）")
        m[col], m[pivot_row] = m[pivot_row], m[col]
        pivot = m[col][col]
        m[col] = [v / pivot for v in m[col]]
        for r in range(n):
            if r != col:
                factor = m[r][col]
                m[r] = [m[r][k] - factor * m[col][k] for k in range(n + 1)]
    return [m[i][n] for i in range(n)]


def _weighted_least_squares(
    rows: List[List[float]], targets: List[float], weights: List[float]
) -> List[float]:
    """
    解加權最小平方：對每筆 (設計向量含截距, 功率) 找係數 beta，
    使 Σ w_i (row_i·beta − target_i)² 最小。用正規方程 (XᵀWX)β=XᵀWy。
    """
    n_features = len(rows[0])
    xtwx = [[0.0] * n_features for _ in range(n_features)]
    xtwy = [0.0] * n_features
    for row, y, w in zip(rows, targets, weights):
        for i in range(n_features):
            xtwy[i] += w * row[i] * y
            for j in range(n_features):
                xtwx[i][j] += w * row[i] * row[j]
    return _solve_linear_system(xtwx, xtwy)


def _default_scan_dir() -> Path:
    """
    掃描樣本的預設輸出目錄，比照 main_ai.py 的 _app_dir() 邏輯：
    以程式所在位置為準，不是目前工作目錄（見 CLAUDE.md〈執行期目錄
    與啟動流程〉——這裡刻意不 import main_ai，所以邏輯獨立複製一份）。
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    return base / "recordings" / "scans"


# =============================================================================
# FiberAlignmentScanner
# =============================================================================
class FiberAlignmentScanner:
    """
    三階段光纖對準尋光：座標下降粗定位 → K 近鄰局部精修（可選）→ 收尾微擾。

    只透過 `ctrl` 的公開方法操作滑台（`scan_move_step` / `query_status` /
    `positions_machine` / `check_sw_limits_batch` / `wait_axis_stop` /
    `stop`），絕不直接碰 `ctrl.ser` 或 `ctrl._serial_lock`——維持
    DS102Controller 對序列通訊的獨占（architect 落地評估的既定要求）。

    `scan_move_step` 是 `move_step` 的搜尋演算法專用版本，只受
    `ems_active` 攔截、不檢查 `scanning_active`——那個旗標是演算法
    自己設的，用來擋 GUI 手動操作與背景輪詢，不該連自己也一併擋下。

    量測與移動全程單一執行緒依序執行（step-motor.txt 的硬體限制：
    不要多執行緒同時對 GPIB 與序列埠通訊）。`run()` 應該在背景執行緒
    呼叫，避免凍結呼叫端的事件迴圈。
    """

    def __init__(
        self,
        ctrl: "DS102Controller",
        power_query: PowerQuery,
        l_speed: str = "5",
        f_speed: str = "1000",
        rate: str = "100",
        s_rate: str = "5",
        step_min: int = DEFAULT_STEP_MIN,
        settle_sec: float = DEFAULT_SETTLE_SEC,
        max_cycles: int = DEFAULT_MAX_CYCLES,
        noise_sigma_mult: float = DEFAULT_NOISE_SIGMA_MULT,
        min_valid_power_dbm: Optional[float] = None,
        no_signal_range_mult: float = DEFAULT_NO_SIGNAL_RANGE_MULT,
        abort_if_no_signal: bool = True,
        f_speed_min: Optional[str] = None,
        speed_scale_pulses: Optional[Tuple[int, int]] = None,
        progress_cb: Optional[ProgressCallback] = None,
        sample_cb: Optional[SampleCallback] = None,
    ):
        self.ctrl = ctrl
        self._power_query = power_query
        self._l_speed = l_speed
        self._rate = rate
        self._s_rate = s_rate
        self.step_min = max(1, int(step_min))
        self.settle_sec = settle_sec
        self.max_cycles = max(1, int(max_cycles))
        self.noise_sigma_mult = noise_sigma_mult
        # ── 訊號有效性判準（與 _noise_floor 的相對差異門檻是兩件事）──
        # min_valid_power_dbm：絕對下限，None＝停用（尚未校準時的安全預設，
        # 向下相容既有行為）。這個值必須來自真機「刻意不耦光」的暗電流
        # 基準量測，程式無法自己假設，數值待接上 HP 8153A 後才能定案。
        self.min_valid_power_dbm = min_valid_power_dbm
        # no_signal_range_mult：階段一第 1 輪若「改善量」與「離散度」都低於
        # 雜訊底限，判定整個探測範圍沒有偵測到訊號，見 _check_signal_detectable。
        self.no_signal_range_mult = no_signal_range_mult
        self.abort_if_no_signal = abort_if_no_signal
        self._progress_cb = progress_cb
        self._sample_cb = sample_cb

        # ── 動態調速：位移量越小、驅動速度 F0 越低（見 _dynamic_speed）──
        # `f_speed` 是「大位移用的上限速度」，`f_speed_min` 是「最小步長
        # 附近用的下限速度」。兩者間用位移量線性內插。
        self._f_speed_max = float(f_speed)
        self._f_speed_min = (
            float(f_speed_min) if f_speed_min is not None else max(50.0, self._f_speed_max / 5)
        )
        lo, hi = speed_scale_pulses or (self.step_min, self.step_min * 32)
        self._speed_scale_lo = max(1, int(lo))
        self._speed_scale_hi = max(self._speed_scale_lo + 1, int(hi))

        self.samples: List[Sample] = []
        self._stop_event = threading.Event()
        self._noise_sigma: Optional[float] = None  # 校準後才有值，見 calibrate_noise()
        # 階段二開始時記下 samples 的長度，_estimate_gradient 只從這個
        # 索引之後取鄰居——見該方法 docstring 說明為什麼不能用階段一的
        # 歷史樣本。
        self._stage2_sample_start = 0

    # ------------------------------------------------------------------
    # 對外控制
    # ------------------------------------------------------------------
    def request_stop(self) -> None:
        """外部（GUI 停止鍵或 Esc）要求中止搜尋。"""
        self._stop_event.set()

    def run(
        self,
        initial_step: Dict[str, int],
        enable_stage2: bool = False,
        stage2_local_radius: Optional[Dict[str, int]] = None,
        axis_scale: Optional[Dict[str, float]] = None,
        scan_dir: Optional[Path] = None,
    ) -> Dict[str, float]:
        """
        完整跑三階段（階段二可選，按需啟用——見設計文件〈折衷方案〉）。

        完成、中止、或任何未預期例外都會在 finally 清掉 scanning_active
        並寫出已收集的樣本（persist_samples）——中止時的資料是唯一能
        回答「跑到哪裡出問題」的來源，不能只在正常結束時才寫。
        """
        if not self.ctrl.connected:
            raise ScanAbort("控制器未連線")
        if self.ctrl.scanning_active:
            raise ScanAbort("已有搜尋在進行中")

        self.ctrl.scanning_active = True
        completed = False
        try:
            self._check_abort()
            self.calibrate_noise()
            self.run_stage1(initial_step)
            if enable_stage2:
                axes = self._active_axes()
                radius = stage2_local_radius or {
                    ax: self.step_min * REOPEN_STEP_MULT for ax in axes
                }
                self.run_stage2(radius, axis_scale=axis_scale)
            self.run_stage3()
            completed = True
        except ScanAbort as e:
            self._log(f"搜尋中止：{e}")
        finally:
            self.ctrl.scanning_active = False
            self.persist_samples(completed=completed, scan_dir=scan_dir)
        return dict(self.ctrl.positions_machine)

    # ------------------------------------------------------------------
    # 階段一：座標下降式全域粗定位
    # ------------------------------------------------------------------
    def run_stage1(self, initial_step: Dict[str, int]) -> Dict[str, float]:
        """
        對每一個實際可動軸，依 `initial_step[axis]` 起跳，反覆「尋峰→
        步長減半」，直到步長縮到 `step_min`。多輪 cycle 處理軸間耦合：
        第 1 輪用完整起始步長做真正的全域粗掃，第 2 輪起從
        `step_min × REOPEN_STEP_MULT` 重新收斂——只是局部重掃，
        不是每輪都重複整個全域搜尋。

        ⚠ 這個「重開步長」機制是實作時對 mathematician 原始虛擬碼的
        修正：原虛擬碼裡 `step[axis]` 只在迴圈外初始化一次，一旦某軸
        在第 1 輪就收斂到 `step_min` 以下，後續所有輪次對該軸的
        while 迴圈都不會再執行——這樣「多輪 cycle 修正耦合」的設計
        目的實際上達不到（該軸不會被重新檢視）。這裡改成每輪開頭
        重新給一個較小的起始步長，讓每個軸在每一輪都有機會被重新
        優化，同時不必付出「每輪都從最大步長開始」的全額成本。
        """
        self._check_abort()
        axes = self._active_axes()
        if not axes:
            raise ScanAbort("沒有可動的軸")
        self._log(f"階段一開始，可動軸：{axes}")
        stage1_start_idx = len(self.samples)  # 供 cycle==1 的無訊號偵測取樣本範圍

        for cycle in range(1, self.max_cycles + 1):
            self._check_abort()
            if cycle == 1:
                step = {
                    ax: max(self.step_min, int(initial_step.get(ax, self.step_min * 8)))
                    for ax in axes
                }
            else:
                step = {ax: self.step_min * REOPEN_STEP_MULT for ax in axes}
            self._log(f"階段一 第 {cycle}/{self.max_cycles} 輪，起始步長 {step}")

            total_improvement = 0.0
            for ax in axes:
                self._check_abort()
                p_before = self._current_power_estimate()
                s = step[ax]
                while s >= self.step_min:
                    self._check_abort()
                    converged, _ = self._search_axis_once(ax, s)
                    if converged:
                        s //= 2
                p_after = self._current_power_estimate()
                if p_before is not None and p_after is not None:
                    total_improvement += max(0.0, p_after - p_before)

            self._log(f"階段一 第 {cycle} 輪結束，本輪改善 {total_improvement:.4f}")
            if total_improvement < self._noise_floor():
                if cycle == 1 and self.abort_if_no_signal:
                    self._check_signal_detectable(self.samples[stage1_start_idx:])
                self._log("階段一收斂（本輪改善低於雜訊底限）")
                break

        return dict(self.ctrl.positions_machine)

    def _search_axis_once(self, axis: str, step: int) -> Tuple[bool, Optional[float]]:
        """
        單軸尋峰子程序跑一輪：方向探測 → 定向爬坡 → 三點拋物線內插。

        回傳 (此步長是否已收斂, 目前功率)。收斂＝出發前後兩側都沒有
        改善；未收斂＝位置有移動，這個步長值得在新位置再跑一輪
        （外層的 while 迴圈負責重複呼叫，直到收斂才縮步）。
        """
        self._check_abort()
        s0 = self._measure_here()
        p0 = s0.power if s0.ok else None
        if p0 is None:
            return True, None  # 起點都量不到，無從比較，外層會縮步或最終中止

        # ── 1. 方向探測（最多兩次移動）──
        moved_plus, p_plus = self._probe(axis, step)
        if moved_plus and p_plus is not None and p_plus > p0:
            direction, p_prev, p_curr = 1, p0, p_plus
        else:
            if moved_plus:
                self._move_relative(axis, -step)  # 撤回原點
            moved_minus, p_minus = self._probe(axis, -step)
            if moved_minus and p_minus is not None and p_minus > p0:
                direction, p_prev, p_curr = -1, p0, p_minus
            else:
                if moved_minus:
                    self._move_relative(axis, step)  # 撤回原點
                return True, p0  # 兩側都沒有可用的改善 → 此步長已收斂

        # ── 2. 定向爬坡：固定步長沿方向走，直到不再上升或撞限位 ──
        p_next: Optional[float] = None
        while True:
            self._check_abort()
            moved = self._move_relative(axis, direction * step)
            if not moved:
                p_next = None  # 撞限位/逾時，視為「下一點不可行」
                break
            s = self._measure_here()
            cur_power = s.power if s.ok else None
            if cur_power is None or cur_power <= p_curr + self._noise_floor():
                p_next = cur_power
                self._move_relative(axis, -direction * step)  # 沒有更好，退回峰值點
                break
            p_prev, p_curr = p_curr, cur_power

        # ── 3. 三點拋物線內插（只有三點都有效才做）──
        if p_next is not None:
            denom = p_prev - 2 * p_curr + p_next
            if abs(denom) > self._noise_floor():
                offset = 0.5 * (p_prev - p_next) / denom  # 介於約 -0.5~0.5 個 step
                x_peak = round(offset * step)
                if x_peak != 0:
                    moved = self._move_relative(axis, direction * x_peak)
                    if moved:
                        s = self._measure_here()
                        better = (
                            s.ok
                            and s.power is not None
                            and s.power > p_curr + self._noise_floor()
                        )
                        if better:
                            p_curr = s.power
                        else:
                            # 內插沒有實際更好，退回格點最大值（防止雜訊誤導外插）
                            self._move_relative(axis, -direction * x_peak)

        return False, p_curr

    def _probe(self, axis: str, delta: int) -> Tuple[bool, Optional[float]]:
        """移動 `delta`（相對，可正可負），量測。回傳 (是否真的移動成功, 功率)。"""
        moved = self._move_relative(axis, delta)
        if not moved:
            return False, None
        s = self._measure_here()
        return True, (s.power if s.ok else None)

    # ------------------------------------------------------------------
    # 階段二：局部 K 近鄰 / 加權線性迴歸精修（按需啟用）
    # ------------------------------------------------------------------
    def run_stage2(
        self,
        local_radius: Dict[str, int],
        axis_scale: Optional[Dict[str, float]] = None,
        k: Optional[int] = None,
        max_iterations: int = 20,
    ) -> Dict[str, float]:
        """
        只在階段一收斂到的小盒子內執行——這是「局部性」假設能成立的
        前提（設計文件對維度詛咒的分析：稀疏樣本下的 K 近鄰在大範圍
        搜尋時不可靠，只有盒子夠小才有意義）。**按需啟用**：只有觀察
        到階段一收斂變慢時才需要呼叫這個方法，是否啟用由呼叫端決定，
        這裡不自動判斷。

        local_radius: {軸名: 初始取樣半徑(pulse)}，決定星形設計的臂長。
        axis_scale:   {軸名: 距離正規化尺度}，未提供則不正規化（各軸
                      等權重）——只能做到「大致公平」，真正的物理尺度
                      需要階段一的副產品才能反推（見設計文件 2.3 節）。
        """
        self._check_abort()
        axes = self._active_axes()
        if not axes:
            raise ScanAbort("沒有可動的軸")
        d = len(axes)
        scale = axis_scale or {ax: 1.0 for ax in axes}
        k = k or max(2 * (d + 1), 4)

        # 從這裡開始收集的樣本才進入 _estimate_gradient 的鄰居池——階段一
        # 座標下降的歷史樣本沿軸向堆積（同一輪只動一個軸），距離上常常
        # 比星形設計的臂點更近，會把星形點擠出 K 近鄰、且本身缺乏多軸
        # 變化（例如最近 6 點全部同一個 X），迴歸矩陣因此奇異。
        self._stage2_sample_start = len(self.samples)

        origin_sample = self._measure_here()
        if not origin_sample.ok or origin_sample.power is None:
            raise ScanAbort("階段二起點量測失敗，無法開始局部精修")
        best_power = origin_sample.power
        self._log(f"階段二開始，起點功率 {best_power:.4f}")

        # ── 初始星形設計：每軸 ±1 個臂點，逐臂取樣完畢即撤回中心 ──
        # ⚠ 撤回中心只是「量測時」暫時回去，不是放棄找到的結果：
        # 星形設計結束後若某個臂比中心好，要真的走到那個臂點，見下方
        # best_arm 的處理。之前的寫法量完就撤回、只把數值記進
        # best_power，位置卻沒有跟著移動——後面梯度下降迴圈重新算出
        # 同一個方向、踩到同一個點時，量到的功率跟 best_power 打平
        # （不是更好），嚴格 `>` 判斷會判定「沒有改善」而撤回，
        # step_length 跟著減半，星形設計已經找到的方向就這樣白費，
        # 最終回報「階段二沒有幫助」——實測用旋轉橢圓高斯耦合合成
        # 函式踩到，星形設計量到 X-24 功率 8.9236（優於中心 8.6545），
        # 梯度下降卻只敢踩 X-4、X-24 都因為打平 best_power 被拒絕，
        # 20 輪後回到原點，跟純座標下降結果完全相同。
        best_arm: Optional[Tuple[str, int]] = None
        for ax in axes:
            r = max(self.step_min, int(local_radius.get(ax, self.step_min * 4)))
            for sign in (1, -1):
                self._check_abort()
                target = self.ctrl.positions_machine[ax] + sign * r
                ok, reason = self.ctrl.check_sw_limits_batch({ax: target})
                if not ok:
                    self._log(f"階段二星形取樣：{reason}，略過此點")
                    continue
                moved = self._move_relative(ax, sign * r)
                if moved:
                    s = self._measure_here()
                    if s.ok and s.power is not None and s.power > best_power:
                        best_power = s.power
                        best_arm = (ax, sign * r)
                    self._move_relative(ax, -sign * r)  # 撤回中心

        if best_arm is not None:
            ax, delta = best_arm
            if self._move_relative(ax, delta):
                self._log(f"階段二星形取樣找到更好的起點：{ax}{delta:+d} pulse，功率 {best_power:.4f}")
            else:
                # 走不過去（撞限位/逾時）：位置仍在中心，best_power 要
                # 跟著改回中心的功率，否則後面梯度下降迴圈永遠贏不了
                # 一個實際上沒有站上去的數字，重演這裡要修的同一種 bug。
                best_power = origin_sample.power

        step_length = max(local_radius.values()) if local_radius else self.step_min * 4
        stall_count = 0

        for _ in range(max_iterations):
            self._check_abort()
            if step_length < self.step_min:
                break

            grad = self._estimate_gradient(axes, scale, k)
            if grad is None:
                self._log("階段二：局部梯度不顯著，判定已收斂")
                break

            norm = sum(g * g for g in grad.values()) ** 0.5
            if norm < 1e-9:
                break
            direction = {ax: grad[ax] / norm for ax in axes}
            deltas = {ax: round(direction[ax] * step_length) for ax in axes}
            if all(v == 0 for v in deltas.values()):
                # 方向量化後全部歸零（步長太小、維度太高）——縮步重試
                step_length = max(self.step_min, step_length // 2)
                continue

            targets = {
                ax: self.ctrl.positions_machine[ax] + dv
                for ax, dv in deltas.items()
                if dv != 0
            }
            ok, reason = self.ctrl.check_sw_limits_batch(targets)
            if not ok:
                self._log(f"階段二候選點超限：{reason}，縮小步長重試")
                step_length = max(self.step_min, step_length // 2)
                stall_count += 1
                if stall_count > max_iterations:
                    break
                continue

            moved_ok = self._move_multi_axis(deltas)
            if not moved_ok:
                # 任一軸撞限位/逾時：這批多軸移動已經整批 STOP，候選點
                # 視為不可行——不是緊急事件，是候選點失敗（見 _move_multi_axis）。
                self._log("階段二候選點移動中失敗（已整批停止），縮小步長重試")
                step_length = max(self.step_min, step_length // 2)
                stall_count += 1
                continue

            s = self._measure_here()
            if s.ok and s.power is not None and s.power > best_power + self._noise_floor():
                best_power = s.power
                stall_count = 0
                step_length = min(step_length * 1.5, max(local_radius.values()))
            else:
                # 預測落空：退回移動前的座標，縮小步長
                self._move_multi_axis({ax: -dv for ax, dv in deltas.items()})
                step_length = max(self.step_min, step_length // 2)
                stall_count += 1

        return dict(self.ctrl.positions_machine)

    def _estimate_gradient(
        self, axes: List[str], scale: Dict[str, float], k: int
    ) -> Optional[Dict[str, float]]:
        """
        用目前位置附近的 K 個最近樣本做加權線性迴歸，估計局部梯度方向。

        距離依 `scale` 正規化後計算（避免行程小的軸被系統性低估變化量）。
        權重用高斯核，核寬度取鄰居距離的中位數。樣本不足（少於軸數+2，
        即含截距項的自由度）時無法擬合，回傳 None。

        ⚠ 鄰居池只取 `self.samples[self._stage2_sample_start:]`（階段二
        開始之後收集的樣本），不用階段一的歷史樣本。原因是實測踩到的
        兩層 bug：

        1. 同座標重複點：`_search_axis_once` 的方向探測在「兩側都沒有
           改善」時會在同一個座標連續量測 2~3 次，`run_stage1` 每輪前
           後也各測一次目前位置。這些點距離全是 0，若混進鄰居池會排
           在 K 近鄰最前面，把星形取樣點擠出去——加了去重仍不夠，見下一點。
        2. 軸向退化：座標下降每輪只沿單一軸移動，即使去重後，階段一
           收斂末期在目前位置附近留下的樣本仍集中在「同一座標、只有
           最後一個處理的軸在變化」（例如全部 6 個最近鄰居的 X 座標
           完全相同，只有 Y 不同）。這種鄰居集合對那個沒有變化的軸
           而言，設計矩陣的截距欄與該軸欄位線性相依，迴歸矩陣依然
           奇異——不是重複點造成的，是鄰居集合本身缺乏該軸方向的
           變異。星形設計每軸都刻意留了 ±r 的取樣點，具備多軸變化，
           排除階段一的歷史樣本後鄰居池自然只剩這些有效點。
        """
        center = self.ctrl.positions_machine
        pool = self.samples[self._stage2_sample_start:]
        valid = [s for s in pool if s.ok and s.power is not None]

        dedup: Dict[Tuple[float, ...], Sample] = {}
        for s in valid:
            key = tuple(round(s.coords.get(ax, center[ax]), 3) for ax in axes)
            dedup[key] = s  # 同座標保留最後一次量測
        unique_samples = list(dedup.values())

        if len(unique_samples) < len(axes) + 2:
            return None

        def dist2(s: Sample) -> float:
            return sum(
                (
                    (s.coords.get(ax, center[ax]) - center[ax])
                    / max(scale.get(ax, 1.0), 1e-9)
                )
                ** 2
                for ax in axes
            )

        neighbors = sorted(unique_samples, key=dist2)[: max(k, len(axes) + 2)]
        dists = sorted(dist2(s) ** 0.5 for s in neighbors)
        ell = dists[len(dists) // 2] or 1.0

        rows, targets, weights = [], [], []
        for s in neighbors:
            d2 = dist2(s)
            w = pow(2.718281828459045, -d2 / (2 * ell * ell))
            row = [1.0] + [
                (s.coords.get(ax, center[ax]) - center[ax]) / max(scale.get(ax, 1.0), 1e-9)
                for ax in axes
            ]
            rows.append(row)
            targets.append(s.power)
            weights.append(w)

        try:
            beta = _weighted_least_squares(rows, targets, weights)
        except ValueError:
            return None  # 樣本共線或矩陣奇異，無法擬合

        grad_normalized = dict(zip(axes, beta[1:]))
        noise = self._noise_floor()
        if noise and all(abs(g) < noise for g in grad_normalized.values()):
            return None  # 梯度分量都淹沒在雜訊裡，判定局部收斂

        return {ax: grad_normalized[ax] / max(scale.get(ax, 1.0), 1e-9) for ax in axes}

    def _move_multi_axis(self, deltas: Dict[str, int]) -> bool:
        """
        多軸同時出發：全部送出 GO（不等待）→ 依序等每一軸到位。

        任一軸失敗（撞限位/逾時）就整批 STOP——DS102 沒有單軸停止指令，
        而且這批候選點本身已經不成立，繼續等其餘軸到位沒有意義。這跟
        「單軸點動撞限位不該連坐停全部軸」是不同情境：那是使用者操作，
        這裡是演算法內部協同移動的一個候選點失敗。

        回傳是否全部軸都成功移動並到位。
        """
        self._check_abort()
        active = {ax: dv for ax, dv in deltas.items() if dv != 0}
        if not active:
            return True

        sent: List[str] = []
        for ax, delta in active.items():
            axis_no = AXIS_NO[ax]
            direction = "CW" if delta > 0 else "CCW"
            ok = self.ctrl.scan_move_step(
                axis_no, direction, str(abs(delta)), *self._dynamic_speed(delta),
                wait_done=False,
            )
            if not ok:
                # 這一軸的 GO 沒送出去（多半是 move_step 內建的單軸限位
                # 檢查擋下）——已出發的軸也要一併停止，不留半出發狀態。
                self.ctrl.stop()
                return False
            sent.append(axis_no)

        all_arrived = True
        for axis_no in sent:
            if not self.ctrl.wait_axis_stop(axis_no):
                all_arrived = False

        if not all_arrived:
            self.ctrl.stop()  # 保險：確保其餘可能還在動的軸也停下來
            return False
        return True

    # ------------------------------------------------------------------
    # 階段三：收尾微擾
    # ------------------------------------------------------------------
    def run_stage3(self) -> Dict[str, float]:
        """
        對每軸做 ±step_min 的微擾，確認沒有更好的鄰近格點——抓階段二
        因座標取整可能錯過的、就在旁邊的格點最大值。
        """
        self._check_abort()
        axes = self._active_axes()
        p_curr = self._current_power_estimate()
        self._log("階段三開始（收尾微擾）")
        for ax in axes:
            self._check_abort()
            if p_curr is None:
                p_curr = self._current_power_estimate()
            baseline = p_curr if p_curr is not None else float("-inf")
            for delta in (self.step_min, -self.step_min):
                moved = self._move_relative(ax, delta)
                if not moved:
                    continue
                s = self._measure_here()
                if s.ok and s.power is not None and s.power > baseline + self._noise_floor():
                    p_curr = s.power
                    baseline = s.power
                else:
                    self._move_relative(ax, -delta)  # 沒有更好，退回
        return dict(self.ctrl.positions_machine)

    # ------------------------------------------------------------------
    # 動態調速
    # ------------------------------------------------------------------
    def _dynamic_speed(self, delta_pulse: int) -> Tuple[str, str, str, str]:
        """
        依這一步的位移量決定驅動速度：位移越小，F0 越低。

        DS102 的 R0（加減速時間）是固定的**時間**，不是固定的距離。
        小位移若沿用大位移的高速 F0，相對加速度會更劇烈——超過馬達
        可用扭矩就會失步，而 DS102 是開迴路控制，沒有編碼器回授，
        失步不會有任何訊號，`_positions_pulse` 會悄悄跟真實座標脫節，
        之後所有收斂判斷都建立在一個錯誤的座標基準上。降速是用時間
        換可靠度，屬於保守但廉價的緩解手段。

        ⚠ 這是線性內插的起跳值，不是實測校準過的失步安全邊界——
        DS102 實際的扭矩-轉速曲線需要真實硬體才量得出來，見設計文件
        〈待實測參數〉。`speed_scale_pulses` 之外（[lo, hi] 兩端）直接
        夾在 `f_speed_min` / `f_speed_max`，中間線性內插。
        """
        amt = abs(delta_pulse)
        lo, hi = self._speed_scale_lo, self._speed_scale_hi
        if amt <= lo:
            f = self._f_speed_min
        elif amt >= hi:
            f = self._f_speed_max
        else:
            frac = (amt - lo) / (hi - lo)
            f = self._f_speed_min + frac * (self._f_speed_max - self._f_speed_min)
        return (self._l_speed, f"{f:.0f}", self._rate, self._s_rate)

    # ------------------------------------------------------------------
    # 量測原語（一次「移動＋量測」是不可分割的最小操作）
    # ------------------------------------------------------------------
    def _move_relative(self, axis: str, delta_pulse: int) -> bool:
        """相對移動指定軸 delta_pulse（可正可負），到位後回傳是否成功。"""
        self._check_abort()
        if delta_pulse == 0:
            return True
        axis_no = AXIS_NO[axis]
        direction = "CW" if delta_pulse > 0 else "CCW"
        ok = self.ctrl.scan_move_step(
            axis_no, direction, str(abs(delta_pulse)), *self._dynamic_speed(delta_pulse),
            wait_done=True,
        )
        if not ok:
            self._log(f"軸 {axis} 移動 {delta_pulse:+d} pulse 失敗（撞限位/逾時）")
        return ok

    def _measure_here(self) -> Sample:
        """在目前座標讀一次功率（不移動）。到位後先等機構震動衰減再量測。"""
        self._check_abort()
        time.sleep(self.settle_sec)
        coords = dict(self.ctrl.positions_machine)
        meter_ok, raw_power = self._safe_power_query()

        ok = meter_ok
        power: Optional[float] = raw_power if meter_ok else None
        note = ""
        if meter_ok and self.min_valid_power_dbm is not None and raw_power < self.min_valid_power_dbm:
            # 通訊成功、數值可解析，但低於絕對訊號下限——判定不可信，收斂
            # 成 ok=False（見 Sample dataclass 的 docstring），但保留實際
            # 讀值於 power／note，供事後除錯或繪圖使用。
            ok = False
            note = f"low_signal: {raw_power:.3f}dBm < floor {self.min_valid_power_dbm:.3f}dBm"
            self._log(f"讀值 {raw_power:.3f} dBm 低於絕對訊號下限 "
                       f"{self.min_valid_power_dbm:.3f} dBm，判定無效")

        s = Sample(coords=coords, ok=ok, power=power, note=note)
        self.samples.append(s)
        if self._sample_cb:
            try:
                self._sample_cb(s)
            except Exception:
                pass  # 樣本回呼本身失敗不可拖垮搜尋（比照 _log 的既有寫法）
        return s

    def _current_power_estimate(self) -> Optional[float]:
        s = self._measure_here()
        return s.power if s.ok else None

    def _safe_power_query(self) -> Tuple[bool, float]:
        """
        功率查詢的例外邊界。背景執行緒不可讓例外逃逸（main_ai.py 既有
        守則的延伸）——GPIB 逾時/裝置忙碌若沒接住，會讓整條搜尋執行緒
        靜默死掉，畫面卡住但沒有任何錯誤訊息。
        """
        try:
            return self._power_query()
        except Exception as e:
            self._log(f"功率查詢例外: {e}")
            return False, 0.0

    # ------------------------------------------------------------------
    # 雜訊校準與收斂判準
    # ------------------------------------------------------------------
    def calibrate_noise(self, n_samples: int = 5) -> float:
        """
        在目前位置重複量測 n_samples 次，估計功率讀值的標準差並快取。

        這是「功率雜訊底限」的具體實作，但實際標準差要接上真實
        HP 8153A 才有意義——對合成測試資料跑，量出來的就是合成雜訊
        的標準差，一樣能驗證判準邏輯本身是否正確。
        """
        self._check_abort()
        powers = []
        for _ in range(max(2, n_samples)):
            self._check_abort()
            time.sleep(self.settle_sec)
            ok, p = self._safe_power_query()
            if ok:
                powers.append(p)
        if len(powers) < 2:
            self._noise_sigma = 0.0
            return 0.0
        mean = sum(powers) / len(powers)
        var = sum((p - mean) ** 2 for p in powers) / (len(powers) - 1)
        self._noise_sigma = var ** 0.5
        self._log(f"雜訊校準完成：σ={self._noise_sigma:.5f}（{len(powers)} 次量測）")
        return self._noise_sigma

    def _noise_floor(self) -> float:
        """功率改善需要超過這個值才算「真的更好」（雜訊底限概念）。"""
        if self._noise_sigma is None:
            return 0.0  # 尚未校準：不設底限，保守起見一律相信讀值差異
        return self.noise_sigma_mult * self._noise_sigma

    def _check_signal_detectable(self, cycle_samples: List[Sample]) -> None:
        """
        階段一第 1 輪結束、且本輪淨改善已低於雜訊底限時呼叫，用來區分兩種
        外觀相同（total_improvement 都很小）但意義完全不同的情況：

          (a) 起點運氣好，本來就已經站在峰值附近——各方向探測仍會量到
              明顯偏低的谷值，樣本間離散度（range）大。
          (b) 整個探測範圍內根本沒有偵測到高於雜訊的訊號（沒耦光、光源
              沒開、光纖沒插好）——各方向讀值都貼在同一雜訊水準，range 小。

        只有 (b) 中止搜尋；(a) 是正常收斂，讓呼叫端繼續往下跑（不誤殺）。

        ⚠ range 的判斷方向假設 HP 8153A 在固定量程、無光耦合時的讀值是
        「穩定貼底」而非「因對數壓縮而劇烈跳動」——這是待真機驗證的假設。
        如果真機量出來的行為相反（無光時讀值反而在 dBm 尺度上劇烈跳動，
        因為線性功率趨近零時對數會放大雜訊），這個判準的方向需要重新
        設計，不能沿用「range 小＝無訊號」。這件事必須用真機在「刻意
        不對準」的位置實測才能確認，不能靠猜測定案。
        """
        powers = [s.power for s in cycle_samples if s.ok and s.power is not None]
        if len(powers) < 4:
            return  # 樣本太少，無法可靠判斷，留給後續輪次或呼叫端自行判斷
        rng = max(powers) - min(powers)
        threshold = self.no_signal_range_mult * self._noise_floor()
        if threshold <= 0:
            return  # 尚未校準雜訊（_noise_floor()==0），無法判斷，不誤殺
        if rng <= threshold:
            raise ScanAbort(
                f"第一輪座標下降共 {len(powers)} 個有效讀值，功率變化範圍僅 "
                f"{rng:.4f}（門檻 {threshold:.4f} = {self.no_signal_range_mult}x 雜訊底限），"
                "研判整個探測範圍內沒有偵測到高於雜訊的訊號——請確認光纖已耦合、"
                "光源已開啟，或起始點/initial_step 是否涵蓋了正確的行程範圍"
            )

    # ------------------------------------------------------------------
    # 輔助
    # ------------------------------------------------------------------
    def _active_axes(self) -> List[str]:
        """
        偵測「目前實際可動」的軸清單——不是寫死 AXES 常數。比照
        `origin_all()` 判斷 `Stage not connected` 的既有寫法，U 軸
        目前未接滑台就會被排除。
        """
        axes = []
        for i in range(self.ctrl.axis_count):
            axis_no = str(i + 1)
            ax = NO_AXIS.get(axis_no)
            if not ax:
                continue
            st, _ = self.ctrl.query_status(axis_no)
            if st == "Stage not connected":
                continue
            axes.append(ax)
        return axes

    def _check_abort(self) -> None:
        """使用者中止或 EMS 觸發時拋出 ScanAbort，讓呼叫鏈自然收工。"""
        if self._stop_event.is_set():
            raise ScanAbort("使用者中止搜尋")
        if self.ctrl.ems_active:
            raise ScanAbort("EMS 觸發，搜尋中止")

    def _log(self, msg: str) -> None:
        if self._progress_cb:
            try:
                self._progress_cb(msg)
            except Exception:
                pass  # 進度回呼本身失敗不可拖垮搜尋

    # ------------------------------------------------------------------
    # 樣本持久化
    # ------------------------------------------------------------------
    def persist_samples(
        self, completed: bool, scan_dir: Optional[Path] = None
    ) -> Optional[Path]:
        """
        把已收集的樣本一次性寫出。不逐筆即時寫檔——hot loop 裡做磁碟
        I/O 會拖慢緊繃的量測預算（0.5~1s 量級）。中止／例外時也要寫，
        這是唯一能回答「跑到哪裡出問題」的資料來源。

        寫入方式仿照 main_ai.py 的 `_write_json_with_backup()`：先寫
        `.tmp` 再 `replace`，避免寫到一半壞檔。
        """
        if not self.samples:
            return None
        out_dir = scan_dir or _default_scan_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"scan_{ts}.json"
        data = {
            "completed": completed,
            "saved": datetime.now().isoformat(timespec="seconds"),
            "sample_count": len(self.samples),
            "samples": [s.to_dict() for s in self.samples],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            self._log(f"搜尋樣本寫入失敗: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        self._log(f"搜尋樣本已存檔：{path.name}（{len(self.samples)} 筆）")
        return path
