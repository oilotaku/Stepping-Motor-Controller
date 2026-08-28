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
import math
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    # DS102Controller 定義於 ds102_ctrl.py（2026-08-17 從 main_ai.py 抽出）。
    # 純型別提示、不影響執行期，方向仍是「不 import main_ai.py」——
    # ds102_ctrl.py 本身也不 import main_ai.py，不構成循環相依。
    from ds102_ctrl import DS102Controller

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
DEFAULT_NO_SIGNAL_RANGE_MULT = 2.0
# 全域無訊號偵測的安全倍數。⚠ 2026-08-12 語意變更：舊版直接乘在固定的
# 3σ 雜訊底限上（等於假設 n 落在某個固定範圍，已被合成資料實測推翻，
# 見 fiber_scanner 設計文件）。新版乘在「n 相關的期望純雜訊全距
# d2(n)×σ」上，數值本身沿用 2.0（對實測 n≈150~350 區間仍有數個標準差
# 的餘裕，見驗算），但若曾假設這是「6σ」等固定倍數，該假設已不成立。
REOPEN_STEP_MULT = 8           # 階段一第 2 輪起，每輪從 step_min×這個倍數重新收斂
# 階段一單軸單一步長的「沒收斂就再來一輪」次數上限（見 run_stage1 的用法）。
# 🔴 這是防無窮迴圈的保險絲，不是調校參數。`while s >= step_min` 只在
# `_search_axis_once()` 回報收斂時才縮步，而該函式只要「有移動」就回報
# 未收斂——純雜訊環境下方向探測可以無止境地左右擺盪，兩邊輪流看起來
# 都比對方好，於是步長永遠不縮、迴圈永遠不結束。2026-08-26 用假物件重現：
# 起點壓在限位上、讀值只有雜訊時，舊版跑到 180 萬次移動仍未收斂（其中
# 60 萬次是真的撞在限位開關上）。實機的症狀就是「尋光跑不完、限位警報
# 一直跳」。200 對真的在跟訊號的搜尋非常寬鬆——爬坡是在單次呼叫內部
# 連續走完的，這裡數的是「換方向的次數」。
STAGE1_MAX_PASSES_PER_STEP = 200

# ---- 階段零（盲搜粗掃）的預設值，同樣是起跳值不是校準值 ----
# 2026-08-26 新增。動機：座標下降／爬坡需要梯度才能決定方向，而尋光的
# 起點**本來就常常是完全無光的**——那正是要尋光的原因。無光時所有方向
# 的讀值都貼在同一個底噪水準，演算法在原地無從選擇方向，只能中止。
# 盲搜的職責就是在這種情況下用固定樣式掃過一塊區域，把滑台帶到「量得到
# 高於底噪的訊號」的位置，再交棒給既有的三階段。
DEFAULT_BLIND_STEP = 200            # 盲搜格點間距（pulse）
DEFAULT_BLIND_MAX_RADIUS = 1000     # 盲搜最大半徑（pulse，Chebyshev 距離）
# ⚠ 上面兩個值是「起跳用的保守預設」，**不是依光學條件校準過的值**，而且
# 這一組的物理正確性比本檔其他預設更依賴使用者的實際架設：
#   - 格點數是 (2×半徑÷格距+1)²，平方成長。半徑 1000／格距 200 是 121 點
#     （粗估半分鐘），刻意讓「預設按下去」是一次規模溫和、掃得完的動作；
#     早期版本預設半徑 4000 等於 1681 點、七分鐘以上的無人看管運動，那不
#     適合當預設值。
#   - 🔴 **格距必須小於耦合光斑的尺度，否則螺旋會直接跨過訊號區而漏掉。**
#     單模光纖纖芯約 9μm，以 2μm/pulse 換算約 5 pulse；多模 50/62.5μm 也
#     不過 25~30 pulse。200 pulse（約 400μm）對這兩種都太粗——它只適合
#     「已知光斑很大／只是要先確認大方向」的情境。真正要靠盲搜找到單模
#     耦合，格距得往個位數 pulse 設，而那會讓同樣半徑的點數暴增，必須
#     同時把半徑縮小。這個取捨沒有通用解，只能由使用者依自己的光學架設
#     決定，GUI 的即時格點數估算就是為了讓這個取捨看得見。
DEFAULT_BLIND_SIGNAL_SIGMA_MULT = 5.0   # 判定「找到訊號」的 σ 倍數
DEFAULT_BLIND_SIGNAL_MIN_DELTA_DB = 0.5  # 判定「找到訊號」的絕對下限（dB）
# 盲搜模式：
#   "off"    不做盲搜，維持 2026-08-26 之前的行為
#   "auto"   先跑階段一；判定無訊號才回頭盲搜，找到訊號後重跑階段一
#   "always" 一律先盲搜再進階段一
BLIND_MODES = ("off", "auto", "always")
# 🔴 函式庫層的預設刻意是 "off"，不是 "auto"。盲搜會驅動滑台在一個平面上
# 走過上千個格點，是本專案目前**單次自動運動量最大**的操作；沒有明確
# 要求就自動跑起來，違反 CLAUDE.md 一貫的「寧可少搜不多動」fail-safe
# 原則，也會讓任何忘記設定的呼叫端得到一次非預期的長時間機械運動。
# GUI 端（main_ai.py）預設是 "auto"，但那是使用者看得到、可以取消、
# 而且會在確認對話框裡明列掃描規模的情境，兩者不衝突。
DEFAULT_BLIND_MODE = "off"
GUI_DEFAULT_BLIND_MODE = "auto"


def _norm_ppf(p: float) -> float:
    """
    標準常態反累積分布函數，僅在本模組內部供 _expected_noise_range_factor
    使用（呼叫端保證 p 落在 (0, 1) 區間內）。

    A&S 26.2.23 有理近似起跳 + 一次 Newton 修正（用 math.erf，標準庫內建，
    非 scipy），把近似誤差從 ~4.5e-4 壓到 ~1e-9——安全門檻的計算基礎值得
    這幾行額外成本。
    """
    p = min(max(p, 1e-12), 1 - 1e-12)
    t = math.sqrt(-2.0 * math.log(1.0 - p))
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2_, d3 = 1.432788, 0.189269, 0.001308
    z = t - (c0 + c1 * t + c2 * t * t) / (1.0 + d1 * t + d2_ * t * t + d3 * t * t * t)
    for _ in range(2):
        cdf = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        pdf = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
        if pdf < 1e-300:
            break
        z -= (cdf - p) / pdf
    return z


def _expected_noise_range_factor(n: int) -> float:
    """
    純雜訊（iid 常態）下，n 個樣本的期望全距（range）／σ，即統計製程管制
    文獻中的 d2(n) 係數。用 Blom (1958) 近似：

        E[R_n] = 2 * E[X_(n)] ≈ 2 * Φ^{-1}((n - 0.375) / (n + 0.25))

    與 n 相關，對任意 n≥2 都成立（不需要查表／不需要事先假設 n 的上界）
    ——這是這次改版要修的病根：舊版「固定 6σ 門檻」隱含假設了一個 n 的
    合理範圍，一旦實際 n 超出假設就會失守（2026-08-12 合成資料實測踩到，
    n=206 時舊門檻與實際 range 只差 1.5%）。

    已與已發表 d2 表核對，n=5/10/25/50/100 誤差皆 <2%，見設計文件。
    """
    n = max(2, n)
    p = (n - 0.375) / (n + 0.25)
    return 2.0 * _norm_ppf(p)


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
    # 純附加的估算快照（見 CLAUDE.md〈軸機械校正參數〉）：只有 coords 裡
    # 有校正參數的軸才會出現在這兩個字典中。軸沒有校正參數就整個是
    # None（不是空字典），呼叫端／報表據此分辨「沒有校正資料」跟「量到
    # 但剛好沒有軸」。演算法邏輯只讀 coords（pulse），不讀這兩個欄位。
    coords_um: Optional[Dict[str, float]] = None
    calib_snapshot: Optional[Dict[str, dict]] = None

    def to_dict(self) -> dict:
        return asdict(self)


class ScanAbort(Exception):
    """
    搜尋被中止：使用者主動停止、EMS 觸發、或起點量測失敗等不可續行的狀況。

    這是正常的中止路徑，不代表程式錯誤——`run()` 會捕捉它、寫出已收集的
    樣本，然後正常返回，不會讓例外一路炸到呼叫端。
    """


class NoSignalAbort(ScanAbort):
    """
    「整個探測範圍內沒有偵測到高於雜訊的訊號」這一種中止。

    刻意做成 ScanAbort 的**子類別**而不是加一個布林屬性：既有所有
    `except ScanAbort` 的呼叫端（`run()` 自己、main_ai.py 的結果分級）
    完全不必改就照樣接得住，而 `run()` 內部需要分辨「這種中止可以靠
    盲搜救回來」時再用 `except NoSignalAbort` 精確攔截，不會誤攔使用者
    主動停止或 EMS 觸發——那兩種絕對不可以自動接著跑盲搜。
    """


def _rect_spiral_offsets(step: int, max_radius: int):
    """
    產生由中心往外、逐環擴大的**方形螺旋**格點偏移 `(a, b)`（單位 pulse）。

    為什麼是方形螺旋而不是阿基米德螺旋：DS102 只有「單軸相對步進」這
    一種移動原語（見 CLAUDE.md〈DS102 通訊協定重點〉，本程式實際送出的
    指令只有四種，沒有任何插補功能）。阿基米德螺旋的每一步都是斜線，
    要靠兩軸同時等比例移動才畫得出來，這台控制器做不到；方形螺旋的每
    一步只動一個軸、位移固定等於 `step`，直接對應 `_move_relative()`。

    為什麼由內而外：盲搜最可能成功的地方是起點附近（使用者通常已經
    大致對好位置，只是還沒耦到光）。由內而外掃描讓「早點找到」成為
    常態，而且中途被使用者停止時，已經覆蓋的是一塊完整的方形區域，
    不是掃了一半的長條。

    邊長序列是標準的 1,1,2,2,3,3,... —— 第 k 環的 Chebyshev 半徑是
    `k × step`。⚠ 走到邊長 `2 × n_rings` **還差最後 4 個角落格點**：
    最外環是在長度 `2 × n_rings + 1` 的那一段才補完的（實測 step=1、
    max_radius=2 時，邊長上限取 4 只吐得出 21 點而非 5×5=25），所以
    迴圈上限是 `2 × n_rings + 1`。多走的那一段幾乎全部落在半徑外，
    會被下面的範圍判斷濾掉，不會多量測。

    超出 `max_radius` 的格點會被跳過但不中斷走訪（螺旋路徑本身必須
    走完才能繞到下一環，這裡只是不 yield 出去而已）。
    """
    yield (0, 0)
    step = max(1, int(step))
    n_rings = int(max_radius) // step
    if n_rings < 1:
        return
    dirs = [(1, 0), (0, 1), (-1, 0), (0, -1)]
    a = b = 0
    d = 0
    leg_len = 1
    while leg_len <= 2 * n_rings + 1:
        for _ in range(2):
            da, db = dirs[d % 4]
            for _ in range(leg_len):
                a += da * step
                b += db * step
                if max(abs(a), abs(b)) <= max_radius:
                    yield (a, b)
            d += 1
        leg_len += 1


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
# 樣本 → Excel（.xlsx）匯出
# =============================================================================
# xlsxwriter 只在「產報表」時用得到，跟量測／移動主流程毫無關係。比照
# main_ai.py 對 matplotlib 的處理方式優雅降級：裝不到就不寫 Excel，既有的
# JSON 樣本檔照常產出，尋光本身完全不受影響——樣本持久化是「跑到哪裡出
# 問題」的唯一資料來源，絕不能因為一個報表格式的相依套件缺席而失效。
try:
    import xlsxwriter  # type: ignore

    _XLSXWRITER_AVAILABLE = True
    _XLSXWRITER_IMPORT_ERROR = ""
except ImportError as _xlsx_err:  # pragma: no cover - 取決於執行環境
    xlsxwriter = None  # type: ignore
    _XLSXWRITER_AVAILABLE = False
    _XLSXWRITER_IMPORT_ERROR = str(_xlsx_err)

# Excel 儲存格上限是 32767 個字元，超過會被 xlsxwriter 拒收（整份寫檔失敗）。
# note 欄理論上不會這麼長，但截斷比讓整份報表寫不出來好。
_XLSX_CELL_LIMIT = 32000


def _sample_axes(samples: List["Sample"]) -> List[str]:
    """這批樣本實際量到的軸，依 AXES 的固定順序排列。"""
    seen = set()
    for s in samples:
        seen.update(s.coords.keys())
    ordered = [ax for ax in AXES if ax in seen]
    # 不在 AXES 清單裡的鍵理論上不會出現，但假物件測試可能塞進來。附在
    # 後面而不是靜默丟掉——報表少一整欄比多一欄難察覺得多。
    ordered += sorted(str(k) for k in seen if k not in AXES)
    return ordered


def export_samples_xlsx(
    samples: List["Sample"],
    path: Path,
    *,
    completed: Optional[bool] = None,
    abort_reason: Optional[str] = None,
    extra_meta: Optional[Dict[str, object]] = None,
) -> Path:
    """
    把樣本寫成一份 .xlsx（兩張工作表：〈摘要〉與〈樣本〉）。

    與 `persist_samples()` 寫的 JSON 是**互補**而非取代：JSON 是完整、
    無損、給程式讀的原始紀錄（事後重繪軌跡圖、回溯除錯都靠它）；xlsx 是
    給人看的報表，欄位攤平成表格、加了 μm 估算欄與統計摘要。兩者同時
    產出，不要為了「省一個檔」把 JSON 換掉。

    寫法比照 `_write_json_with_backup()`：先寫 `.tmp` 再 `replace`，
    避免中途失敗留下一份半殘的 xlsx（Excel 開起來會直接報毀損）。

    失敗一律拋例外（`RuntimeError` = 沒有 xlsxwriter；`ValueError` = 沒有
    樣本；`OSError` = 寫檔失敗），由呼叫端決定要靜默略過（persist_samples）
    還是跳訊息框（GUI 的手動匯出按鈕）。
    """
    if not _XLSXWRITER_AVAILABLE:
        raise RuntimeError(
            f"未安裝 xlsxwriter，無法輸出 Excel（{_XLSXWRITER_IMPORT_ERROR}）"
        )
    if not samples:
        raise ValueError("沒有樣本可匯出")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    axes = _sample_axes(samples)
    um_axes = [
        ax for ax in axes if any(s.coords_um and ax in s.coords_um for s in samples)
    ]

    valid = [s for s in samples if s.ok and s.power is not None]
    best = max(valid, key=lambda s: s.power if s.power is not None else float("-inf")) if valid else None
    best_idx = samples.index(best) + 1 if best is not None else None

    # 校正參數快照取「最後一筆有帶的」——一輪搜尋期間使用者理論上不會去
    # 改校正參數，取最後一筆與取第一筆結果相同；真的中途被改過時，取最後
    # 一筆反映的才是這輪結束時實際生效的那組。
    calib: Dict[str, dict] = {}
    for s in samples:
        if s.calib_snapshot:
            calib = s.calib_snapshot

    tmp = path.with_suffix(path.suffix + ".tmp")
    wb = xlsxwriter.Workbook(str(tmp))
    try:
        f_title = wb.add_format({"bold": True, "font_size": 12})
        f_warn = wb.add_format({"font_color": "#B36B00"})
        f_head = wb.add_format(
            {"bold": True, "bg_color": "#DDE6F0", "border": 1, "align": "center"}
        )
        f_key = wb.add_format({"bold": True, "bg_color": "#F2F2F2", "border": 1})
        f_val = wb.add_format({"border": 1})
        f_pulse = wb.add_format({"num_format": "#,##0", "border": 1})
        f_um = wb.add_format({"num_format": "0.000", "border": 1})
        f_pw = wb.add_format({"num_format": "0.00", "border": 1})

        # ── 工作表：摘要 ──────────────────────────────────────────────
        ws = wb.add_worksheet("摘要")
        ws.set_column(0, 0, 22)
        ws.set_column(1, 1, 46)
        ws.write(0, 0, "尋光搜尋結果摘要", f_title)

        rows: List[Tuple[str, object]] = [
            ("匯出時間", datetime.now().isoformat(timespec="seconds")),
            (
                "結束狀態",
                "完成" if completed else ("中止／未完成" if completed is not None else "未提供"),
            ),
        ]
        if abort_reason:
            rows.append(("中止原因", abort_reason))
        rows += [
            ("樣本總數", len(samples)),
            ("有效樣本數", len(valid)),
            ("搜尋軸", "、".join(axes) if axes else "—"),
            ("起始時間", samples[0].ts),
            ("結束時間", samples[-1].ts),
        ]
        if best is not None:
            rows.append(("最佳功率 (dBm)", round(best.power, 3) if best.power is not None else "—"))
            rows.append(("最佳樣本序號", best_idx))
            rows.append(
                (
                    "最佳座標 (pulse)",
                    "  ".join(f"{ax}={best.coords.get(ax, 0.0):,.0f}" for ax in axes),
                )
            )
            if best.coords_um:
                rows.append(
                    (
                        "最佳座標 (µm 估算)",
                        "  ".join(
                            f"{ax}={best.coords_um[ax]:,.3f}"
                            for ax in um_axes
                            if ax in best.coords_um
                        ),
                    )
                )
        else:
            rows.append(("最佳功率 (dBm)", "—（整輪沒有任何有效讀值）"))
        rows.append(
            (
                "最終座標 (pulse)",
                "  ".join(f"{ax}={samples[-1].coords.get(ax, 0.0):,.0f}" for ax in axes),
            )
        )
        for ax in axes:
            p = calib.get(ax)
            if p:
                rows.append(
                    (
                        f"{ax} 軸校正參數",
                        f"導程 {p.get('lead_pitch_mm')} mm／步進角 "
                        f"{p.get('step_angle_deg')}°／分度 {p.get('division')}",
                    )
                )
        for k, v in (extra_meta or {}).items():
            rows.append((str(k), v))

        r = 3
        for key, val in rows:
            ws.write(r, 0, key, f_key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                ws.write_number(r, 1, val, f_val)
            else:
                ws.write_string(r, 1, str(val)[:_XLSX_CELL_LIMIT], f_val)
            r += 1
        # µm 欄是估算顯示值，不是量測值——報表被單獨傳出去時，這句話是唯一
        # 能阻止讀者把它當實測位移引用的東西（見 docs/axis-calibration.md）。
        if um_axes:
            ws.write(
                r + 1,
                0,
                "⚠ µm 欄為依機械校正參數換算的估算值，非實測位移；pulse 才是控制器的原始單位。",
                f_warn,
            )

        # ── 工作表：樣本 ──────────────────────────────────────────────
        ws2 = wb.add_worksheet("樣本")
        headers = ["#", "時間"]
        headers += [f"{ax} (pulse)" for ax in axes]
        headers += [f"{ax} (µm 估算)" for ax in um_axes]
        headers += ["功率 (dBm)", "有效", "備註"]
        for c, h in enumerate(headers):
            ws2.write(0, c, h, f_head)
        ws2.set_column(0, 0, 6)
        ws2.set_column(1, 1, 20)
        ws2.set_column(2, max(2, len(headers) - 2), 13)
        ws2.set_column(len(headers) - 1, len(headers) - 1, 40)
        ws2.freeze_panes(1, 2)
        ws2.autofilter(0, 0, len(samples), len(headers) - 1)

        for i, s in enumerate(samples, start=1):
            c = 0
            ws2.write_number(i, c, i, f_val)
            c += 1
            ws2.write_string(i, c, str(s.ts), f_val)
            c += 1
            for ax in axes:
                v = s.coords.get(ax)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    ws2.write_number(i, c, float(v), f_pulse)
                else:
                    ws2.write_blank(i, c, None, f_pulse)
                c += 1
            for ax in um_axes:
                v = (s.coords_um or {}).get(ax)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    ws2.write_number(i, c, float(v), f_um)
                else:
                    ws2.write_blank(i, c, None, f_um)
                c += 1
            # 🔴 power 有值但 ok=False 時照樣寫進去（讀值低於絕對下限的情況，
            # 見 Sample 的 docstring）——那個數值是事後判斷「門檻是不是設太高」
            # 的唯一依據，「有效」欄已經分辨得出來，不需要再把數值抹掉。
            if isinstance(s.power, (int, float)) and not isinstance(s.power, bool):
                ws2.write_number(i, c, float(s.power), f_pw)
            else:
                ws2.write_blank(i, c, None, f_pw)
            c += 1
            ws2.write_string(i, c, "是" if s.ok else "否", f_val)
            c += 1
            ws2.write_string(i, c, str(s.note or "")[:_XLSX_CELL_LIMIT], f_val)

        wb.close()
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    try:
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


# =============================================================================
# FiberAlignmentScanner
# =============================================================================
class FiberAlignmentScanner:
    """
    三階段光纖對準尋光：座標下降粗定位 → K 近鄰局部精修（可選）→ 收尾微擾。

    只透過 `ctrl` 的公開方法操作滑台（`scan_move_step` / `query_status` /
    `positions_machine` / `check_sw_limits_batch` / `limit_direction` /
    `wait_axis_stop` / `stop`），絕不直接碰 `ctrl.ser` 或
    `ctrl._serial_lock`——維持
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
        selected_axes: Optional[List[str]] = None,
        blind_mode: str = DEFAULT_BLIND_MODE,
        blind_axes: Optional[Tuple[str, str]] = None,
        blind_step: int = DEFAULT_BLIND_STEP,
        blind_max_radius: int = DEFAULT_BLIND_MAX_RADIUS,
        blind_signal_sigma_mult: float = DEFAULT_BLIND_SIGNAL_SIGMA_MULT,
        blind_signal_min_delta_db: float = DEFAULT_BLIND_SIGNAL_MIN_DELTA_DB,
        on_signal_found: Optional[Callable[[float], None]] = None,
    ):
        self.ctrl = ctrl
        self._power_query = power_query
        # 使用者從 GUI 勾選的搜尋範圍（None＝不限制，維持改動前的行為：
        # 全部交給硬體可用性偵測決定）。_active_axes() 在偵測結果之後
        # 再與這份清單取交集，兩層判斷互不取代——見該方法 docstring。
        self._selected_axes = list(selected_axes) if selected_axes is not None else None
        # run() 開始後才會有值，供呼叫端（GUI 畫即時軌跡圖）讀「這次
        # 實際搜尋範圍」，不需要自己重算一次交集。
        self.active_axes: List[str] = []
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

        # ── 階段零：盲搜粗掃（2026-08-26 新增，見 run_stage0_blind）──
        self.blind_mode = blind_mode if blind_mode in BLIND_MODES else DEFAULT_BLIND_MODE
        # None＝開跑時自動取實際搜尋軸的前兩軸。盲搜刻意固定成 2 軸：
        # 光纖對準的搜尋空間本質是橫向平面，第三軸（間距）用同樣的格點
        # 密度掃會讓點數變成 N³——以單模光纖需要的格距估算是天文數字，
        # 不是「比較慢」而是根本跑不完。要掃第三軸請分次做。
        self._blind_axes = tuple(blind_axes) if blind_axes else None
        self.blind_step = max(1, int(blind_step))
        self.blind_max_radius = max(0, int(blind_max_radius))
        self.blind_signal_sigma_mult = float(blind_signal_sigma_mult)
        # 絕對下限：無光時底噪可能非常穩定（σ→0），純靠 σ 倍數會讓判準
        # 退化成「比 baseline 大一點點就算找到訊號」，任何一個雜訊尖峰
        # 都會誤觸發、把滑台停在沒有光的地方並回報成功。兩者取大。
        self.blind_signal_min_delta_db = float(blind_signal_min_delta_db)
        self._on_signal_found = on_signal_found
        self._signal_confirmed = False  # on_signal_found 只在第一次確認訊號時觸發

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
        # 校準當下的平均功率。盲搜用它當「這裡有沒有比起點更亮」的基準，
        # None＝校準時一個有效讀值都沒拿到（見 calibrate_noise）。
        self._noise_baseline: Optional[float] = None
        # run() 內部把所有中止事件（使用者停止／EMS／無訊號判定）都用
        # ScanAbort 自己接住、正常 return——呼叫端如果只看 run() 的回傳值
        # 或例外，完全無法分辨「真的收斂完成」跟「中途被中止」。這個屬性
        # 在 run() 正常返回後仍然可以讀到中止原因（None＝真的完成），供
        # main_ai.py 這類需要分級呈現結果的呼叫端使用，不需要重新設計
        # run() 既有的例外吞併行為（那是刻意的：中止是正常結束路徑，不該
        # 讓呼叫端還要自己包 try/except 分辨語意）。
        self.last_abort_reason: Optional[str] = None
        # 與 last_abort_reason 配套的型別化分類："no_signal"／"other"，
        # None＝沒有中止。見 run() 的 except 區塊說明為什麼不讓呼叫端
        # 繼續對訊息字串做子字串比對。
        self.last_abort_kind: Optional[str] = None
        # 這一輪自動寫出的 Excel 報表路徑（沒裝 xlsxwriter 或寫檔失敗時是
        # None）。GUI 端拿它顯示「報表在哪」，不必自己重組檔名。
        self.last_xlsx_path: Optional[Path] = None
        # 階段二開始時記下 samples 的長度，_estimate_gradient 只從這個
        # 索引之後取鄰居——見該方法 docstring 說明為什麼不能用階段一的
        # 歷史樣本。
        self._stage2_sample_start = 0
        # 這一輪實測到的行程邊界（機械座標 pulse）：{軸: [CCW 端, CW 端]}，
        # None＝該側這一輪還沒撞到過。由 _note_limit_hit() 寫入、
        # _targets_reachable() 讀取，每次 run() 重置。見 _note_limit_hit()
        # docstring 說明為什麼光靠 ctrl.sw_limits 擋不住。
        self._travel_bounds: Dict[str, List[Optional[float]]] = {}

        # ── 主搜尋演算法選擇（2026-08-28 新增，見 fiber_scanner_advanced.py）──
        # run() 才會依呼叫端傳入的值覆寫；這裡先給預設值，讓
        # persist_samples() 在 run() 都還沒跑過（例如測試直接塞 samples
        # 呼叫 persist_samples）時也不會因為屬性不存在而炸掉。
        self.algorithm: str = "coordinate_descent"
        self._powell_max_iterations: int = 200
        self.enable_stage2: bool = False

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
        algorithm: str = "coordinate_descent",
        powell_max_iterations: int = 200,
    ) -> Dict[str, float]:
        """
        完整跑三階段（階段二可選，按需啟用——見設計文件〈折衷方案〉）。

        `algorithm` 選擇主搜尋方式：
          - "coordinate_descent"（預設）：階段一座標下降＋可選階段二 K 近鄰精修，
            即改動前的行為，完全不變。
          - "powell"：改跑 fiber_scanner_advanced.run_stage_powell()，涵蓋原本
            階段一＋階段二的範圍（見該模組 docstring）。此時 `enable_stage2`
            會被忽略（只記一筆 log，不當成錯誤）。實驗性功能，未經真機驗證，
            見 FIBER_ALIGNMENT_SCAN_DESIGN.md〈第三輪〉。

        完成、中止、或任何未預期例外都會在 finally 清掉 scanning_active
        並寫出已收集的樣本（persist_samples）——中止時的資料是唯一能
        回答「跑到哪裡出問題」的來源，不能只在正常結束時才寫。
        """
        if not self.ctrl.connected:
            raise ScanAbort("控制器未連線")
        if self.ctrl.scanning_active:
            raise ScanAbort("已有搜尋在進行中")

        # 先算一次交集結果並存起來，供 GUI 端（即時軌跡圖）讀取「這次
        # 實際搜尋範圍」；同時在這裡就擋下「選了軸但交集後是空的」情況，
        # 不必等 run_stage1 內部再拋一次語意不夠精確的「沒有可動的軸」。
        self.active_axes = self._active_axes()
        if not self.active_axes:
            if self._selected_axes is not None:
                raise ScanAbort("選定的軸目前皆不可動（未接滑台或未啟用）")
            raise ScanAbort("沒有可動的軸")
        if self._selected_axes is not None:
            missing = [ax for ax in self._selected_axes if ax not in self.active_axes]
            if missing:
                self._log(
                    "⚠ 已排除目前偵測不到滑台的軸："
                    f"{'、'.join(missing)}（原已勾選）"
                )

        self.ctrl.scanning_active = True
        # 白名單驗證，不合法值一律退回預設——比照 blind_mode 既有寫法
        # （BLIND_MODES 那行），不可用 .index() 之類的位置換算。
        self.algorithm = algorithm if algorithm in ("coordinate_descent", "powell") else "coordinate_descent"
        self._powell_max_iterations = max(1, int(powell_max_iterations))
        self.enable_stage2 = enable_stage2
        self.last_abort_reason = None  # 重置：這個實例若被重複呼叫 run()，不能沿用上一輪的中止原因
        self.last_xlsx_path = None  # 同上，不能讓 GUI 指到上一輪的報表
        self.last_abort_kind = None
        self._signal_confirmed = False  # 同理：量程鎖定的一次性旗標也要重置
        # 行程邊界是「這一輪實測到的」，不跨輪沿用：兩輪之間可能做過原點
        # 復歸，POS 是相對暫存器（見 docs/hardware.md），復歸後同一個機械
        # 位置的座標值會整組改變，沿用舊邊界等於用錯誤的座標把一整片區域
        # 靜默排除掉。代價只是每輪各方向要重新實撞一次。
        self._travel_bounds = {}
        completed = False
        try:
            self._check_abort()
            # 使用者按下開始時的位置。階段一在純雜訊上會亂爬（每個方向
            # 探測都可能因雜訊而「看起來有改善」），漂走之後那裡不該當成
            # 盲搜的圓心——半徑是使用者相對這個起點設定的。
            start_pos = dict(self.ctrl.positions_machine)

            self.calibrate_noise()

            # ── 決定要不要先跑階段零 ──
            # 「起點連底噪都讀不到」曾經被當成 fatal 直接中止（2026-08-26
            # 第一版），那是錯的：sentinel 的語意是「低於可量測下限」，是
            # 明確資訊而非未知，盲搜在這種情況下反而最該跑。實機 log 證實
            # 了這個判斷失誤——13:57／13:59 兩次全 sentinel，使用者選了
            # auto 模式卻直接看到「未偵測訊號」，盲搜一次都沒跑到。
            no_baseline = self._noise_baseline is None
            if no_baseline and self.blind_mode == "off":
                # 只有明確關掉盲搜時才維持「立刻中止並直指儀器」的行為。
                raise NoSignalAbort(
                    "光功率計在起始位置完全讀不到有效值，無法開始尋光。"
                    "最常見的原因是量程設定不涵蓋目前功率——無光時固定量程"
                    "會 underrange，儀器回傳 +9.9E+37 而非數值；其次是光學頭"
                    "未接或 GPIB 通訊異常。請改用自動量程後重試，或啟用階段零盲搜。"
                )

            if no_baseline:
                self._log("起點讀不到任何有效功率值 → 直接進入階段零盲搜")
            if no_baseline or self.blind_mode == "always":
                self.run_stage0_blind(center=start_pos)
                self.calibrate_noise()  # 新位置的底噪與起點不同，重新建立基準

            # ── 階段一，必要時回頭補盲搜 ──
            # 🔴 判準是「跑完階段一有沒有確認到訊號」（self._signal_confirmed），
            # 不是「階段一有沒有拋 NoSignalAbort」。第一版只攔例外，漏掉了
            # 階段一**正常收斂結束**卻從未偵測到訊號的情況：
            # `_check_signal_detectable()` 在有效樣本 <4 時走「樣本太少，
            # 不誤殺」的放行分支，接著 `total_improvement < noise_floor`
            # 讓外層迴圈 break，run_stage1 就這樣正常 return。實機 log
            # 13:08 正是如此——X 軸撞限位使該輪只收到 11 筆樣本、有效的
            # 更少，於是「階段一收斂」→ 沒有例外 → 沒有盲搜 → 階段二丟出
            # 「起點量測失敗」。用旗標判斷同時涵蓋這兩條路徑。
            stage1_aborted_no_signal = self._run_primary_algorithm(initial_step)

            if (
                self.blind_mode == "auto"
                and not self._signal_confirmed
                and not stage1_aborted_no_signal
            ):
                here = self._power_clearly_above_baseline()
                if here is not None:
                    # 已經站在訊號上，只是樣本數不足以讓判準表態——不必盲搜。
                    self._log(
                        f"階段一結束時當下功率 {here:.4f} dBm 已明顯高於基準，"
                        "判定有訊號，略過階段零盲搜"
                    )
                    self._confirm_signal(here)

            if self.blind_mode == "auto" and not self._signal_confirmed:
                # 🔴 這裡刻意不寫成 `except ScanAbort`：使用者主動停止與
                # EMS 觸發也是 ScanAbort，那兩種絕對不可以自動接著跑一輪
                # 會驅動滑台上千個格點的盲搜。它們會直接往外傳，根本走不
                # 到這一行——NoSignalAbort 子類別存在的理由就是這個分辨。
                reason = (
                    "階段一未偵測到高於雜訊的訊號"
                    if stage1_aborted_no_signal
                    else "階段一已收斂但全程未確認到訊號"
                )
                self._log(f"{reason} → 回到起點改跑階段零盲搜")
                self.run_stage0_blind(center=start_pos)
                self.calibrate_noise()
                # swallow_no_signal=False：這是盲搜後的重跑，已經沒有下一次
                # 補救機會了，第二次還是沒訊號就要讓 NoSignalAbort 照原本
                # 行為（改動前 run_stage1(initial_step) 沒被 try/except 包住）
                # 直接往外傳、走到 run() 的 except ScanAbort 中止收尾，不能
                # 靜默吞掉繼續跑下去。
                self._run_primary_algorithm(initial_step, swallow_no_signal=False)

            if enable_stage2 and self.algorithm != "powell":
                axes = self._active_axes()
                radius = stage2_local_radius or {
                    ax: self.step_min * REOPEN_STEP_MULT for ax in axes
                }
                self.run_stage2(radius, axis_scale=axis_scale)
            self.run_stage3()
            completed = True
        except ScanAbort as e:
            self.last_abort_reason = str(e)
            # 型別化的中止分類。呼叫端（main_ai._classify_scan_abort）原本
            # 只能對訊息字串做子字串比對——每新增一種無訊號中止訊息就得
            # 記得同步改那邊的字面量，否則新訊息會靜默掉進「其他錯誤」
            # 分支、失去專屬的 no_signal 呈現。2026-08-26 一口氣新增四種
            # 無訊號中止訊息時這個耦合就繃不住了，改成由這裡直接給型別。
            self.last_abort_kind = "no_signal" if isinstance(e, NoSignalAbort) else "other"
            self._log(f"搜尋中止：{e}")
        except Exception as e:
            # 非預期例外（非 ScanAbort）也要讓 last_abort_reason／kind 有值，
            # 否則 finally 照樣呼叫 persist_samples()，報表會輸出但完全看
            # 不出中止原因——這是自動輸出的報表唯一能還原「跑到哪裡出錯」
            # 的欄位，不能因為例外種類是未預期的就留白。
            self.last_abort_reason = f"未預期例外：{e}"
            self.last_abort_kind = "exception"
            self._log(f"搜尋因未預期例外中止：{e}")
            raise
        finally:
            self.ctrl.scanning_active = False
            self.persist_samples(completed=completed, scan_dir=scan_dir)
        return dict(self.ctrl.positions_machine)

    def _run_primary_algorithm(
        self, initial_step: Dict[str, int], swallow_no_signal: bool = True
    ) -> bool:
        """
        依 self.algorithm 分流主搜尋（座標下降的階段一，或 Powell）。

        run() 內部有兩處要跑主演算法：第一次正常嘗試，以及 auto 盲搜模式
        下訊號未確認時補一次盲搜之後的重跑。兩處都呼叫這個方法，不可以
        各寫一份分流邏輯——那會讓其中一處在改動時忘記同步跟著 algorithm
        走（見 CLAUDE.md 落地規格的踩雷紀錄）。

        回傳 True 表示「這一輪判定為訊號未確認，可能需要（或已經）補盲搜」，
        對應改動前內聯的 stage1_aborted_no_signal 旗標語意。

        `swallow_no_signal` 控制 NoSignalAbort 在 blind_mode=="auto" 時是否
        吞掉、回傳 True（讓呼叫端接著跑盲搜補救），還是照樣往外傳：
          - 第一次呼叫（swallow_no_signal=True，預設）：允許吞掉，觸發後續
            的盲搜補救。
          - 補盲搜之後的第二次呼叫（swallow_no_signal=False）：改動前的
            寫法在這裡完全沒有 try/except，第二次還偵測不到訊號就必須讓
            NoSignalAbort 真的往外傳、把整輪搜尋中止掉，不能再吞第二次
            靜默跑去 run_stage3——這裡用參數保留同一段語意，而不是讓
            這個方法在兩個呼叫點表現不一致。
        """
        if self.algorithm == "powell":
            if self.enable_stage2:
                self._log("⚠ Powell 已涵蓋階段一＋二範圍，忽略「啟用階段二局部精修」設定")
            # 模組層級 import 會與 fiber_scanner_advanced.py 對 fiber_scanner
            # 的 import 形成循環相依，所以刻意延遲到這裡才 import；用模組
            # 別名（而非 from ... import run_stage_powell）方便測試對這個
            # 模組屬性做 monkeypatch。
            import fiber_scanner_advanced as _fsa
            idx = len(self.samples)

            def _early_check() -> None:
                # Powell 完全繞過 run_stage1() 內建的訊號確認檢查（那個
                # 檢查只在 run_stage1() 的 cycle==1 觸發）。這個 hook 讓
                # run_stage_powell() 每完成一次真正的測量就檢查一次
                # （見該函式 early_signal_check 參數的文件字串）——等
                # minimize() 整個跑完（最多 max_iterations 次移動＋量測，
                # 真機是數分鐘量級）才檢查，會讓 abort_if_no_signal 對
                # Powell 路徑形同虛設，_signal_confirmed／量程鎖定也全程
                # 不會觸發。
                self._check_signal_detectable(
                    self.samples[idx:], raise_on_no_signal=self.abort_if_no_signal
                )

            # run_stage_powell()（含 _early_check hook）只會自然往外傳
            # ScanAbort／NoSignalAbort（使用者中止／EMS／_check_abort()／
            # _check_signal_detectable() 判定無訊號），這裡的 try/except
            # 就是原本等著接 NoSignalAbort 的同一個位置——不需要另外在
            # run_stage_powell() 呼叫前後分兩段接。
            try:
                _fsa.run_stage_powell(
                    self,
                    max_iterations=self._powell_max_iterations,
                    early_signal_check=_early_check,
                )
            except NoSignalAbort:
                if not swallow_no_signal or self.blind_mode != "auto":
                    raise
                return True
            return False
        else:
            try:
                self.run_stage1(initial_step)
            except NoSignalAbort:
                if not swallow_no_signal or self.blind_mode != "auto":
                    raise
                return True
            return False

    def _powell_param_snapshot(self) -> Dict[str, object]:
        """
        Powell 本輪實際生效的容差／懲罰參數快照，供 persist_samples() 的
        JSON metadata 與 Excel〈摘要〉共用。這三個值目前沒有開放 GUI 輸入
        （architect 結論：錯了會靜默失效，操作員也沒有回饋依據能校準），
        一律讀 fiber_scanner_advanced.py 的模組層級常數（DEFAULT_XTOL_PULSE
        / DEFAULT_FTOL_SIGMA_MULT / DEFAULT_PENALTY_LAMBDA）——這些常數
        同時也是 run_stage_powell() 函式簽章的預設值來源，兩處保證同步。

        🔴 2026-08-28 之前這裡是用 `inspect.signature()` 反射函式簽章預設
        值，理由是「怕這裡另外硬編一份數字、跟簽章預設值不同步」。但這個
        呼叫點在 persist_samples()／_persist_samples_xlsx() 的 try 保護
        範圍**外**——若日後 run_stage_powell() 的參數改名（例如
        xtol_pulse → tol_pulse，這正是待實測校準參數的常見下場），
        `sig.parameters["xtol_pulse"]` 會直接 KeyError，炸穿 run() 的
        finally，導致整輪 JSON 樣本檔一個字都不會寫出。改讀模組常數本身
        就不會因為參數改名而 KeyError（常數名稱與函式參數名稱各自獨立、
        改動時是兩個顯式的賦值語句，不是反射查找）；呼叫端另外包了一層
        try/except 當第二道防線，見 persist_samples() 與
        _persist_samples_xlsx()。
        """
        import fiber_scanner_advanced as _fsa

        return {
            "xtol_pulse": _fsa.DEFAULT_XTOL_PULSE,
            "ftol_sigma_mult": _fsa.DEFAULT_FTOL_SIGMA_MULT,
            "penalty_lambda": _fsa.DEFAULT_PENALTY_LAMBDA,
            "max_iterations": self._powell_max_iterations,
        }

    # ------------------------------------------------------------------
    # 階段零：盲搜粗掃（無訊號時才用得上）
    # ------------------------------------------------------------------
    def _power_clearly_above_baseline(self) -> Optional[float]:
        """
        當下位置的功率是否明顯高於校準時的基準？量一次、不移動。

        用途：階段一跑完但 `_signal_confirmed` 仍是 False 時，區分兩種
        情況——(a) 真的沒訊號，該去盲搜；(b) 其實已經站在訊號上，只是
        `_check_signal_detectable()` 因為有效樣本不足 4 個而無從判定
        （實機 log 13:08：X 軸撞限位使該輪只收到 11 筆樣本）。沒有這道
        檢查，(b) 會白跑一輪上百格點的盲搜。

        用的門檻與盲搜完全相同，避免兩處判準不一致造成「盲搜認為找到了、
        這裡認為沒有」的來回擺盪。

        回傳判定為有訊號時的當下讀值，否則 None（讀不到或未達門檻）。
        回傳數值而非 bool 是為了讓呼叫端能把真實功率傳進 `_confirm_signal()`
        ——那個值會出現在 log 與量程鎖定的訊息裡，塞一個假的 0.0 會誤導。
        """
        s = self._measure_here()
        if not s.ok or s.power is None:
            return None
        if self._noise_baseline is None:
            # 校準時連底噪都讀不到，現在讀得到＝確定有東西（同盲搜的退化判準）
            return s.power
        sigma = self._noise_sigma or 0.0
        delta = max(self.blind_signal_sigma_mult * sigma, self.blind_signal_min_delta_db)
        return s.power if s.power >= self._noise_baseline + delta else None

    def _blind_axis_pair(self, axes: List[str]) -> Tuple[str, str]:
        """
        決定盲搜要掃哪兩個軸。

        使用者指定的 `blind_axes` 優先，但必須兩個軸都在本次實際搜尋
        範圍內——指定了一個沒在搜尋的軸卻照掃，會把滑台移到使用者
        以為不會動的方向去。不合格就退回「實際搜尋軸的前兩軸」並記
        一則警告，不靜默改用別的軸。
        """
        if self._blind_axes:
            pair = tuple(self._blind_axes)
            if len(pair) == 2 and all(ax in axes for ax in pair):
                return pair  # type: ignore[return-value]
            self._log(
                f"⚠ 盲搜指定的軸 {self._blind_axes} 不在本次搜尋範圍 {axes} 內，"
                "改用搜尋範圍的前兩軸"
            )
        if len(axes) < 2:
            raise ScanAbort(
                f"盲搜需要兩個軸才能掃出一個平面，本次可動軸只有 {axes}。"
                "請多勾選一個軸，或把盲搜模式設為「off」。"
            )
        return (axes[0], axes[1])

    def _confirm_signal(self, power: float) -> None:
        """
        第一次確認「真的量到訊號」時觸發 on_signal_found（只觸發一次）。

        用途是讓呼叫端把光功率計從自動量程切成鎖定量程（自動量程每次
        讀值都要讓儀器自己找檔位，比較慢；確認訊號量級之後鎖定可以省
        掉這段）。**這是樂觀最佳化，不是必要步驟**——鎖定之後功率從
        底噪爬到耦合峰值可能跨數十 dB 而 overrange，那時由
        meter_GPIB.get_power() 自己偵測 sentinel 並切回自動量程，見該
        檔的自動退回邏輯。沒有那道保護就不該在這裡鎖。
        """
        if self._signal_confirmed:
            return
        self._signal_confirmed = True
        if self._on_signal_found is None:
            return
        try:
            self._on_signal_found(power)
        except Exception as e:
            # 比照 _log／_sample_cb 的既有寫法：回呼失敗不可拖垮搜尋。
            # 這個回呼只做效能最佳化，失敗的後果僅僅是量程沒鎖成。
            self._log(f"訊號確認回呼失敗（不影響搜尋）: {e}")

    def _return_to_machine(self, coords: Dict[str, float], axes: List[str]) -> bool:
        """
        把指定的軸移回一組機械座標（盡力而為，失敗只記錄不中止）。

        給「階段一在純雜訊上亂爬之後要回到使用者的起點再盲搜」用：
        盲搜半徑是使用者相對**起點**設定的，若以階段一漂走後的位置當
        圓心，掃出來的區域跟使用者以為的完全不是同一塊。
        """
        cur = dict(self.ctrl.positions_machine)
        deltas = {
            ax: int(round(coords[ax] - cur[ax]))
            for ax in axes
            if ax in coords and ax in cur and int(round(coords[ax] - cur[ax])) != 0
        }
        if not deltas:
            return True
        ok = self._move_multi_axis(deltas)
        if not ok:
            self._log("⚠ 回到起點的移動未完全成功，盲搜將以目前位置為圓心")
        return ok

    def run_stage0_blind(self, center: Optional[Dict[str, float]] = None) -> bool:
        """
        盲搜粗掃：在兩軸平面上用方形螺旋由內而外掃描，找到第一個「功率
        顯著高於起點底噪」的格點就停下，把滑台留在該點並回傳 True。

        為什麼需要這個階段：階段一的座標下降與爬坡都需要**梯度**才能決定
        往哪邊走。完全無光時所有方向的讀值都貼在同一個底噪水準，
        `_search_axis_once()` 的方向探測兩側都沒有改善，直接判定「此步長
        已收斂」——演算法在原地一步都不動。而尋光的起點本來就常常是無光
        的，那正是要尋光的原因。盲搜不需要梯度，只需要「掃到就算數」。

        判定門檻 = 基準底噪 + max(σ倍數×σ, 絕對下限dB)。兩者取大的理由見
        `blind_signal_min_delta_db` 的註解：底噪很穩定時 σ→0，純靠 σ 倍數
        會退化成「比基準大一點點就算找到」，一個雜訊尖峰就能把滑台停在
        沒有光的地方並回報成功。

        🔴 每個格點都用「絕對目標 − 目前座標」重算 delta，不是沿著螺旋
        累加相對位移。撞限位而跳過的格點會讓實際位置偏離螺旋路徑，若用
        累加式相對位移，後續每一點都會跟著整體平移，掃出來的區域跟使用
        者設定的半徑不再對應。

        掃完整個半徑仍未找到訊號，丟 `NoSignalAbort`（不是回傳 False）：
        呼叫端沒有「盲搜失敗但還能繼續」的合理後續動作，而中止訊息要帶
        著掃描規模，使用者才知道該擴大半徑還是該回頭檢查光路。
        """
        self._check_abort()
        axes = self._active_axes()
        if not axes:
            raise ScanAbort("沒有可動的軸")
        ax_a, ax_b = self._blind_axis_pair(axes)

        # 🔴 底噪讀不到值**不是**不能盲搜的理由，這點 2026-08-26 第一版判斷
        # 錯了。sentinel（+9.9E+37）的語意是「功率低於目前可量測下限」，那是
        # 明確的資訊，不是「不知道」——在連底噪都測不到的環境裡，任何一個
        # 讀得到的有效值本身就已經高於底噪，直接當成找到訊號即可。第一版讓
        # run() 在這種情況下直接中止，等於把盲搜最該派上用場的情境擋掉了
        # （實機 log：13:57／13:59 兩次全 sentinel，連盲搜都沒跑到）。
        if self._noise_baseline is None:
            target_power = None
            self._log(
                "階段零（盲搜）：起點底噪低於儀器可量測下限（每次讀值都是 sentinel），"
                "改用退化判準——掃到任何一個讀得到的有效值就視為找到訊號"
            )
        else:
            sigma = self._noise_sigma or 0.0
            delta_thresh = max(
                self.blind_signal_sigma_mult * sigma, self.blind_signal_min_delta_db
            )
            target_power = self._noise_baseline + delta_thresh

        if center is not None:
            self._return_to_machine(center, axes)

        offsets = list(_rect_spiral_offsets(self.blind_step, self.blind_max_radius))
        center = dict(self.ctrl.positions_machine)
        if target_power is None:
            thresh_desc = "訊號門檻：任何有效讀值（底噪不可量測）"
        else:
            thresh_desc = (
                f"訊號門檻 {target_power:.4f} dBm"
                f"（基準 {self._noise_baseline:.4f} + {delta_thresh:.4f}）"
            )
        self._log(
            f"階段零（盲搜）開始：{ax_a}-{ax_b} 平面，格距 {self.blind_step} pulse，"
            f"最大半徑 {self.blind_max_radius} pulse，共 {len(offsets)} 個格點；{thresh_desc}"
        )

        measured = 0
        blocked = 0
        for idx, (da, db) in enumerate(offsets):
            self._check_abort()
            cur = dict(self.ctrl.positions_machine)
            targets = {ax_a: center[ax_a] + da, ax_b: center[ax_b] + db}

            # 先做批次行程檢查再送指令：超出行程的格點不必真的送一次 GO
            # 才發現走不了。盲搜的半徑常常會蓋到行程邊界外，逐點試錯的
            # 代價（每點一次來回通訊＋一次失敗等待）在上千個格點下很可觀。
            # 🔴 檢查走 `_targets_reachable()` 而不是直接 `check_sw_limits_batch()`：
            # 後者比對的 `ctrl.sw_limits` 預設是空的（GUI 不填就全部放行），
            # 光靠它，這段最該發揮作用的檢查會整個退化成 no-op——實機
            # 2026-08-26 一輪盲搜就實撞了 50 次限位。見 `_note_limit_hit()`。
            ok, _reason = self._targets_reachable(targets)
            if not ok:
                blocked += 1
                continue

            deltas = {
                ax: int(round(targets[ax] - cur[ax]))
                for ax in (ax_a, ax_b)
                if int(round(targets[ax] - cur[ax])) != 0
            }
            if deltas and not self._move_multi_axis(deltas):
                blocked += 1
                continue

            s = self._measure_here()
            measured += 1
            if s.ok and s.power is not None and (target_power is None or s.power >= target_power):
                gate = "任何有效讀值" if target_power is None else f"{target_power:.4f}"
                self._log(
                    f"階段零找到訊號：第 {idx + 1}/{len(offsets)} 個格點，"
                    f"偏移 ({ax_a}{da:+d}, {ax_b}{db:+d})，"
                    f"功率 {s.power:.4f} dBm（門檻 {gate}）。"
                    "改由階段一接手精細收斂。"
                )
                self._confirm_signal(s.power)
                return True

            if measured % 100 == 0:
                self._log(
                    f"階段零掃描中：已量測 {measured} 點／共 {len(offsets)}，"
                    f"目前偏移 ({ax_a}{da:+d}, {ax_b}{db:+d})"
                )

        raise NoSignalAbort(
            f"階段零盲搜已掃完 {ax_a}-{ax_b} 平面上 {measured} 個格點"
            f"（格距 {self.blind_step} pulse、半徑 {self.blind_max_radius} pulse，"
            f"另有 {blocked} 點因超出行程或撞限位而略過），"
            + (
                "沒有任何一點讀到有效功率值（全程都低於儀器可量測下限）。"
                if target_power is None
                else f"沒有任何一點的功率達到門檻 {target_power:.4f} dBm。"
            )
            + 
            "請確認光源已開啟、光纖已插好，或擴大盲搜半徑／改掃另一組軸"
            "（例如耦合距離不對時要先調整 Z 軸再重掃）。"
        )

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
                passes = 0  # 同一步長「未收斂而重跑」的次數，見下方保險絲
                while s >= self.step_min:
                    self._check_abort()
                    converged, _ = self._search_axis_once(ax, s)
                    if converged:
                        s //= 2
                        passes = 0
                        continue
                    passes += 1
                    if passes >= STAGE1_MAX_PASSES_PER_STEP:
                        # 保險絲熔斷。這裡刻意縮步繼續而不是整個中止：走到
                        # 這一步代表這個步長在這個位置附近沒有可靠的梯度，
                        # 換更小的步長還有機會，而中止整輪搜尋的代價太大。
                        # 一定要記 log——這條路徑正常情況下不該被走到，
                        # 靜默熔斷會讓「為什麼結果怪怪的」永遠查不出來。
                        self._log(
                            f"⚠ 階段一 軸 {ax} 步長 {s} pulse 連續 {passes} 次未收斂，"
                            "判定此步長附近沒有可靠梯度（多半是純雜訊），強制縮步"
                        )
                        s //= 2
                        passes = 0
                p_after = self._current_power_estimate()
                if p_before is not None and p_after is not None:
                    total_improvement += max(0.0, p_after - p_before)

            self._log(f"階段一 第 {cycle} 輪結束，本輪改善 {total_improvement:.4f}")

            # 無訊號偵測：只在第 1 輪跑一次，且刻意不看 total_improvement 是否已
            # 低於雜訊底限——total_improvement 是「各軸 max(0, 改善) 相加」，
            # 純雜訊情境下方向探測的 p_plus>p0 比較沒有雜訊門檻（等同挑雜訊讀值
            # 中較大者的選擇偏誤），多軸加總後有結構性正偏誤，可能意外跳過
            # 「本該檢查」的時機（2026-08-12 合成資料測試踩到：3 軸純雜訊情境下
            # total_improvement 意外大於雜訊底限，導致這裡完全沒被觸發）。
            # range 判準（_check_signal_detectable 內部）不受此偏誤影響——其
            # 統計期望值只隨樣本數對數成長，目前門檻在合理樣本數下有安全餘裕。
            # 第 1 輪跑完就直接檢查，不必等 total_improvement 這個有偏誤的
            # 中介指標開線燈。
            # 🔴 一律呼叫（不再被 abort_if_no_signal 整個 gate 掉），只有
            # 「判定沒訊號時要不要拋例外」受那個開關控制。原因：這個函式
            # 同時負責在**有**訊號時設起 `_signal_confirmed`（供量程鎖定與
            # run() 判斷要不要補盲搜），關掉開關等於連「有訊號」這個判定
            # 也一併跳過，run() 會誤以為從未偵測到訊號而多跑一輪盲搜。
            if cycle == 1:
                self._check_signal_detectable(
                    self.samples[stage1_start_idx:],
                    raise_on_no_signal=self.abort_if_no_signal,
                )

            if total_improvement < self._noise_floor():
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
        # 🔴 門檻是 `p0 + 雜訊底限`，不是 `p0`。下方爬坡迴圈一直都用這個
        # 門檻，方向探測卻用赤裸的 `>`——這個不對稱本身就是既有註解說的
        # 「等同挑雜訊讀值中較大者的選擇偏誤」。後果不只是統計上的偏誤：
        # 純雜訊環境下兩側輪流「看起來比較好」，每次呼叫都會移動、於是
        # 每次都回報未收斂，外層 while 的步長永遠不縮——搜尋不會結束。
        # 2026-08-26 假物件重現：起點壓在限位上、讀值只有雜訊時，舊版
        # 180 萬次移動仍在原地兩點之間擺盪。低於雜訊底限的「改善」本來
        # 就不是可靠資訊，拿它決定方向等於讓雜訊駕駛滑台。
        floor = self._noise_floor()
        moved_plus, p_plus = self._probe(axis, step)
        if moved_plus and p_plus is not None and p_plus > p0 + floor:
            direction, p_prev, p_curr = 1, p0, p_plus
        else:
            if moved_plus:
                self._move_relative(axis, -step)  # 撤回原點
            moved_minus, p_minus = self._probe(axis, -step)
            if moved_minus and p_minus is not None and p_minus > p0 + floor:
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
                ok, reason = self._targets_reachable({ax: target})
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
            ok, reason = self._targets_reachable(targets)
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

    # ------------------------------------------------------------------
    # 行程邊界（撞過一次就記起來，不再往同一個方向反覆撞）
    # ------------------------------------------------------------------
    def _note_limit_hit(self, axis: str) -> bool:
        """
        移動失敗之後查一次該軸狀態；確實壓在限位上就把當下座標記成
        這一輪該側的行程邊界。回傳是否真的判定為撞限位。

        **為什麼需要這個**：搜尋過程中所有「送出前的限位檢查」比對的都是
        `ctrl.sw_limits`，而那份軟體限位預設六軸都是 `(None, None)`、GUI
        不填就是全部放行，控制器韌體的 `CWSLE`/`CCWSLE` 出廠也是停用
        （見 [docs/hardware.md]，2026-08-05 複測 `cwsle=0`，且它是 RAM-only、
        斷電就沒了）。也就是說候選點超出行程時**沒有任何一層擋得住**，
        只能靠真的撞上限位開關才知道走不了。

        撞一次是必要的代價，撞五十次不是。2026-08-26 實機 log
        （`ds102_20260826_150901.log` 15:10 起）一輪盲搜就撞了 50 次、
        全部是 `Detect CCW limit`：盲搜每個格點都用「絕對目標 − 目前座標」
        重算 delta（這是刻意的，見 `run_stage0_blind`），而卡在限位上的軸
        座標不會變，於是下一個格點算出來的 delta 幾乎一模一樣，原地反覆
        撞同一顆開關，每撞一次就送一次 `STOP 0`（連坐停掉正在正常移動的
        另一軸）並彈一則限位警報橫幅。記下邊界之後，同側超界的候選點在
        送指令前就被 `_targets_reachable()` 擋掉，一輪最多各撞一次。

        🔴 **判不出限位方向時一律不動邊界**（回傳 False）。移動失敗還有
        逾時、通訊失聯等成因，把那些誤記成「行程末端」會讓該方向一整片
        區域在這一輪被永久排除，而且是靜默排除——比多撞幾次嚴重得多。

        同側重複撞到時取「比較靠內側」的那個值（CCW 取大、CW 取小）：
        限位開關有實體作用寬度，同一顆開關每次停下的座標會差幾個 pulse，
        取靠內側的才是保守方向。
        """
        axis_no = AXIS_NO.get(axis)
        if not axis_no:
            return False
        try:
            status, _pos = self.ctrl.query_status(axis_no)
            side = self.ctrl.limit_direction(status)
        except Exception as e:
            # 查狀態本身失敗不該讓搜尋中止——這只是「要不要記邊界」的
            # 額外資訊，拿不到就退回改動前的行為（下次還是會實撞）。
            self._log(f"軸 {axis} 移動失敗後查狀態失敗（不影響搜尋）: {e}")
            return False
        if side is None:
            return False
        here = self.ctrl.positions_machine.get(axis)
        if here is None:
            return False

        bounds = self._travel_bounds.setdefault(axis, [None, None])
        idx = 0 if side == "CCW" else 1
        prev = bounds[idx]
        if prev is None:
            bounds[idx] = here
            self._log(
                f"軸 {axis} 已到 {side} 側行程末端（{here:.0f} pulse）——"
                "本輪後續超過這個座標的候選點會在送指令前直接略過，不再實撞"
            )
        else:
            bounds[idx] = max(prev, here) if side == "CCW" else min(prev, here)
        return True

    def _targets_reachable(self, targets: Dict[str, float]) -> Tuple[bool, str]:
        """
        候選點送指令前的行程檢查，兩層都要過：

          1. `ctrl.check_sw_limits_batch()`——使用者在 GUI 設定的軟體限位。
             涵蓋「還沒撞過但已知不該去」的方向，預設沒設就是全部放行。
          2. 本輪實測到的行程邊界（`_note_limit_hit()` 記的）。涵蓋「沒設
             軟體限位」這個實際上的常態。

        兩層互相取代不了：第一層事先知道、但預設是空的；第二層一定準、
        但要先撞過一次才有。`targets` 是**機械座標**（與 `sw_limits`、
        `positions_machine` 同一個座標系）。
        """
        ok, reason = self.ctrl.check_sw_limits_batch(targets)
        if not ok:
            return False, reason
        for ax, target in targets.items():
            lo, hi = self._travel_bounds.get(ax, (None, None))
            if lo is not None and target < lo:
                return False, (
                    f"軸 {ax} 目標 {target:.0f} pulse 超出本輪實測的 "
                    f"CCW 行程末端 {lo:.0f} pulse"
                )
            if hi is not None and target > hi:
                return False, (
                    f"軸 {ax} 目標 {target:.0f} pulse 超出本輪實測的 "
                    f"CW 行程末端 {hi:.0f} pulse"
                )
        return True, ""

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

        # 出發前先抓一份機械座標，供 wait_axis_stop() 的位移證據當基準
        # （見 ds102_ctrl._wait_axis_stop 的「孿生競態」段）。搜尋的單步
        # 移動量常常小到在第一次狀態取樣之前就跑完，少了這兩個值會被誤
        # 判成「GO 未生效」——這裡有現成的 deltas，沒有理由不傳。
        before = dict(self.ctrl.positions_machine)

        # 已知走不到的目標一根軸都不送。呼叫端（階段零/二）多半已經檢查
        # 過同一批座標，這裡是最後一道：`_move_relative()` 以外的所有多軸
        # 移動都經過本函式，把檢查放在這裡才涵蓋得完（例如 _return_to_machine）。
        targets = {
            ax: before[ax] + dv for ax, dv in active.items() if ax in before
        }
        if targets:
            ok, reason = self._targets_reachable(targets)
            if not ok:
                self._log(f"多軸候選點略過：{reason}")
                return False

        sent: List[Tuple[str, str]] = []
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
            sent.append((axis_no, ax))

        all_arrived = True
        failed: List[str] = []
        for axis_no, ax in sent:
            if not self.ctrl.wait_axis_stop(
                axis_no,
                start_pos=before.get(ax),
                expected_travel=abs(active[ax]),
            ):
                all_arrived = False
                failed.append(ax)

        if not all_arrived:
            self.ctrl.stop()  # 保險：確保其餘可能還在動的軸也停下來
            # 🔴 記邊界要排在 stop() **之後**：_note_limit_hit() 會多送一次
            # SB3?/SB1? 查詢（約 112ms），插在 stop() 前面等於讓其餘還在動的
            # 軸多跑那段時間。限位狀態是準位不是邊緣，停下來之後照樣讀得到。
            for ax in failed:
                self._note_limit_hit(ax)
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
        here = self.ctrl.positions_machine.get(axis)
        if here is not None:
            ok, reason = self._targets_reachable({axis: here + delta_pulse})
            if not ok:
                # 刻意**不**記 log：這條路徑在階段一每一輪的方向探測都會
                # 走到（撞到底的那一側每次都會被擋），逐筆記等於把 log 灌爆，
                # 而真正需要大聲講的那一次（實際撞上限位）已經由
                # _note_limit_hit() 記過了。回傳 False 的語意與「真的撞了
                # 走不動」一致，呼叫端不需要分辨這兩者。
                return False
        axis_no = AXIS_NO[axis]
        direction = "CW" if delta_pulse > 0 else "CCW"
        ok = self.ctrl.scan_move_step(
            axis_no, direction, str(abs(delta_pulse)), *self._dynamic_speed(delta_pulse),
            wait_done=True,
        )
        if not ok:
            self._log(f"軸 {axis} 移動 {delta_pulse:+d} pulse 失敗（撞限位/逾時）")
            self._note_limit_hit(axis)
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

        coords_um = {}
        calib_snapshot = {}
        for ax, pulse in coords.items():
            um = self.ctrl.estimate_um(ax, pulse)
            if um is not None:
                coords_um[ax] = um
                calib_snapshot[ax] = dict(self.ctrl.axis_calib[ax])

        s = Sample(
            coords=coords, ok=ok, power=power, note=note,
            coords_um=coords_um or None,
            calib_snapshot=calib_snapshot or None,
        )
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
        if not powers:
            # 一個有效讀值都沒拿到。舊版只是靜默把 σ 設成 0 就繼續往下跑，
            # 結果是整個階段一空轉（起點量不到 → _search_axis_once 立刻
            # 判定「此步長已收斂」→ 滑台一步都不動），使用者看到的症狀是
            # 「按了尋光完全沒反應」，而唯一的錯誤訊息要等到階段二才丟、
            # 還指向錯誤的位置（2026-08-26 實機事故，見 CLAUDE.md）。
            # 這裡把 baseline 留成 None，由 run() 立刻中止並直指光功率計。
            self._noise_sigma = 0.0
            self._noise_baseline = None
            self._log("⚠ 雜訊校準：所有讀值皆無效，無法建立基準")
            return 0.0

        self._noise_baseline = sum(powers) / len(powers)
        if len(powers) < 2:
            self._noise_sigma = 0.0
            self._log(
                f"雜訊校準：只取得 {len(powers)} 個有效讀值，"
                f"σ 無法估計（設為 0），基準功率 {self._noise_baseline:.4f} dBm"
            )
            return 0.0
        mean = self._noise_baseline
        var = sum((p - mean) ** 2 for p in powers) / (len(powers) - 1)
        sigma = var ** 0.5
        self._noise_sigma = sigma
        self._log(
            f"雜訊校準完成：σ={sigma:.5f}，"
            f"基準功率 {self._noise_baseline:.4f} dBm（{len(powers)} 次量測）"
        )
        return sigma

    def _noise_floor(self) -> float:
        """功率改善需要超過這個值才算「真的更好」（雜訊底限概念）。"""
        if self._noise_sigma is None:
            return 0.0  # 尚未校準：不設底限，保守起見一律相信讀值差異
        return self.noise_sigma_mult * self._noise_sigma

    def _check_signal_detectable(
        self, cycle_samples: List[Sample], raise_on_no_signal: bool = True
    ) -> None:
        """
        階段一第 1 輪座標下降跑完就無條件呼叫一次（與本輪 total_improvement
        是否低於雜訊底限無關——原因見 run_stage1 呼叫處的註解），用來區分
        兩種外觀相同（樣本 range 都很小）但意義完全不同的情況：

          (a) 起點運氣好，本來就已經站在峰值附近——各方向探測仍會量到
              明顯偏低的谷值，樣本間離散度（range）大。
          (b) 整個探測範圍內根本沒有偵測到高於雜訊的訊號（沒耦光、光源
              沒開、光纖沒插好）——各方向讀值都貼在同一雜訊水準，range 小。

        只有 (b) 中止搜尋；(a) 是正常收斂，讓呼叫端繼續往下跑（不誤殺）。

        ⚠ 門檻不是固定的「no_signal_range_mult × 3σ 雜訊底限」——純雜訊下
        range 的期望值本身隨樣本數 n 增加（d2(n) 效應），固定門檻在 n 偏離
        設計時假設的範圍時會失守，2026-08-12 合成資料實測（n=206）已證實。
        改為 no_signal_range_mult × d2(n) × σ，見 _expected_noise_range_factor。

        ⚠ range 的判斷方向仍假設 HP 8153A 在固定量程、無光耦合時的讀值是
        「穩定貼底」而非「因對數壓縮而劇烈跳動」——這是待真機驗證的假設，
        這次改動沒有動這個方向本身，只重新校準了門檻的計算方式。
        """
        powers = [s.power for s in cycle_samples if s.ok and s.power is not None]

        # 🔴 「有效讀值為 0，但量測次數不少」跟「樣本太少」是完全不同的
        # 兩件事，不可以共用同一條 `len(powers) < 4: return` 放行。前者
        # 代表光功率計整輪一次都沒讀到可用的值（量程 underrange、光學頭
        # 沒接、GPIB 異常），是明確的失敗；舊版把它併進「樣本太少，不
        # 誤殺」而靜默放行，正是 2026-08-26「尋光無動作」事故裡讓 82 個
        # 全無效樣本一路溜過去、什麼警告都沒有的那個缺口。
        if not powers and len(cycle_samples) >= 4:
            if not raise_on_no_signal:
                self._log(
                    f"⚠ 第一輪 {len(cycle_samples)} 次量測全部無效"
                    "（已關閉「無訊號時中止」，繼續執行）"
                )
                return
            raise NoSignalAbort(
                f"第一輪座標下降共量測 {len(cycle_samples)} 次，"
                "但**沒有任何一次讀到有效功率值**——這不是「沒有光」，"
                "而是光功率計根本讀不到數字。最常見的原因是量程設定不涵蓋"
                "目前的功率（無光時固定量程會 underrange，回傳 +9.9E+37），"
                "其次是光學頭未接或 GPIB 通訊異常。請改用自動量程後重試。"
            )

        if len(powers) < 4:
            return  # 樣本太少，無法可靠判斷，留給後續輪次或呼叫端自行判斷

        if self._noise_sigma is None or self._noise_sigma <= 0:
            return  # 尚未校準雜訊，無法判斷，不誤殺

        n = len(powers)
        rng = max(powers) - min(powers)
        expected_noise_range = _expected_noise_range_factor(n) * self._noise_sigma
        threshold = self.no_signal_range_mult * expected_noise_range

        if rng > threshold:
            # 判定「這一輪確實看到高於雜訊的訊號」——這也是量程可以從
            # 自動切成鎖定的時機（見 _confirm_signal）。盲搜路徑有自己的
            # 呼叫點，兩邊都靠 _signal_confirmed 保證只觸發一次。
            self._confirm_signal(max(powers))
            return

        if rng <= threshold:
            if not raise_on_no_signal:
                self._log(
                    f"⚠ 第一輪 {n} 個有效讀值的功率變化範圍僅 {rng:.4f}"
                    f"（門檻 {threshold:.4f}），研判沒有訊號"
                    "（已關閉「無訊號時中止」，繼續執行）"
                )
                return
            raise NoSignalAbort(
                f"第一輪座標下降共 {n} 個有效讀值，功率變化範圍僅 "
                f"{rng:.4f}（門檻 {threshold:.4f} = {self.no_signal_range_mult}x "
                f"純雜訊下 n={n} 的期望全距 {expected_noise_range:.4f}），"
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

        再與 `self._selected_axes`（使用者從 GUI 勾選的搜尋範圍）取
        交集——`None` 代表沒有限制，行為與改動前完全一致；`run_stage1`
        / `run_stage2` / `run_stage3` 都透過呼叫這個方法間接受益，
        它們本身不需要知道有這道篩選存在。
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
        if self._selected_axes is not None:
            axes = [ax for ax in axes if ax in self._selected_axes]
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
        # 檔名用微秒精度：同一秒內連續兩輪 persist_samples()（例如階段零
        # 盲搜後緊接失敗中止、run() 又立刻重跑一輪）曾經共用同一個檔名，
        # tmp.replace() 不報錯，第一輪的原始樣本會被靜默覆蓋消失。
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = out_dir / f"scan_{ts}.json"
        data = {
            "completed": completed,
            "saved": datetime.now().isoformat(timespec="seconds"),
            "sample_count": len(self.samples),
            "abort_reason": self.last_abort_reason,
            "abort_kind": self.last_abort_kind,
            "algorithm": self.algorithm,
            "samples": [s.to_dict() for s in self.samples],
        }
        # 供之後真機校準時回推「這輪用了什麼參數」——見 _powell_param_snapshot
        # docstring。座標下降沒有對應的可調參數，不寫這個鍵，維持 JSON 精簡。
        if self.algorithm == "powell":
            # 第二層防禦：_powell_param_snapshot() 內部已經改成讀模組常數、
            # 理論上不會再 KeyError，但這裡仍包一層 try/except——任何未
            # 預期的失敗都只能記 log、略過這個欄位，不可讓例外炸穿本函式，
            # 否則呼叫端 run() 的 finally 會連 JSON 樣本檔都寫不出來
            # （CLAUDE.md 明文紅線：尋光樣本落地失敗只能記 log，不可外拋）。
            try:
                data["powell_params"] = self._powell_param_snapshot()
            except Exception as e:
                self._log(f"Powell 參數快照讀取失敗（不影響樣本存檔）: {e}")
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
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
        self.last_xlsx_path = self._persist_samples_xlsx(path, completed)
        return path

    def _persist_samples_xlsx(self, json_path: Path, completed: bool) -> Optional[Path]:
        """
        在 JSON 旁邊再寫一份同檔名的 .xlsx 報表。

        🔴 **任何失敗都只記 log、不往外拋。** 這個函式跑在 `run()` 的
        `finally` 裡，緊接在 JSON 寫完之後——JSON 才是「跑到哪裡出問題」
        的權威紀錄，絕不能因為報表格式的相依套件缺席、或檔案被 Excel 開著
        鎖住（Windows 上會 PermissionError，實務上很容易發生：使用者上一輪
        的報表還開著），就讓例外炸穿 finally、蓋掉原本要回傳的結果。
        """
        xlsx_path = json_path.with_suffix(".xlsx")
        extra_meta: Dict[str, object] = {"演算法": self.algorithm}
        if self.algorithm == "powell":
            # 同 persist_samples() 的第二層防禦：_powell_param_snapshot()
            # 失敗只記 log、extra_meta 就略過 Powell 那幾個欄位（保留
            # 「演算法」那一項），不可讓例外往外傳（見上方 JSON 端同一
            # 段防禦的理由）。
            try:
                p = self._powell_param_snapshot()
                extra_meta["Powell xtol (pulse)"] = p["xtol_pulse"]
                extra_meta["Powell ftol_sigma_mult"] = p["ftol_sigma_mult"]
                extra_meta["Powell penalty_lambda"] = p["penalty_lambda"]
                extra_meta["Powell max_iterations"] = p["max_iterations"]
            except Exception as e:
                self._log(f"Powell 參數快照讀取失敗（不影響 Excel 報表其餘內容）: {e}")
        try:
            export_samples_xlsx(
                self.samples,
                xlsx_path,
                completed=completed,
                abort_reason=self.last_abort_reason,
                extra_meta=extra_meta,
            )
        except RuntimeError as e:
            # 沒裝 xlsxwriter：講一次就好，不必每輪都當成錯誤
            self._log(f"未輸出 Excel 報表：{e}")
            return None
        except Exception as e:
            self._log(f"Excel 報表寫入失敗（JSON 樣本檔不受影響）: {e}")
            return None
        self._log(f"Excel 報表已存檔：{xlsx_path.name}")
        return xlsx_path
