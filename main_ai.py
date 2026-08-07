# =============================================================================
# DS102/DS112 步進馬達控制器 — 圖形化版本 v3.0
# 依據 main.py 指令格式完整整合
#
# 依賴套件：pyserial  (pip install pyserial)
# GUI 框架：tkinter（Python 3 內建，無需額外安裝）
#
# 改善項目（v3.0）：
#   1. 執行緒安全：positions / action_history 加入 threading.Lock 保護
#   2. 通訊可靠性：指令逾時保護、ACK 確認、重送機制（最多 3 次）
#   3. 行程錄製僅錄驅動指令（排除查詢類 SB?/POS?）
#   4. 到位確認：步進/原點後輪詢 SB1? 確認 Driving 旗標清除再繼續
#   5. 單位一律 pulse（不提供 um / mm 切換）
#   6. 軟體行程限制（Software Limit）：超限自動攔截並警告
#   7. Limit / 異常狀態自動彈窗警告並停止
#   8. 連線未建立時鎖定驅動按鈕
#   9. 速度 Profile 命名儲存與快速切換
#  10. Teaching Point 加入「移動至此點」功能
#  11. 實驗數據 CSV 匯出（時間戳 + 各軸位置）
#  12. 座標偏置（Offset）：定義工作原點與機械原點分離
#  13. EMS 解除後要求位置確認才能繼續操作
#  14. 行程重播中鎖定其他移動操作
# =============================================================================

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import serial
import serial.tools.list_ports
import sys
import threading
import time
import json
import logging
import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Tuple

# =============================================================================
# 執行期目錄與 LOG 系統
# =============================================================================
def _app_dir() -> Path:
    """
    程式所在目錄——**不是**目前工作目錄。

    以前三個資料目錄都是 `Path("logs")` 這種相對路徑，等於綁在 CWD 上。
    直接跑 .py 時 CWD 通常就是專案資料夾所以看不出問題，但打包成 exe 之後：
      - 從開始功能表／捷徑啟動，CWD 可能是 C:\\Windows\\System32
        → 教點與行程會被存到那裡，使用者以為資料不見了
      - 每次從不同位置啟動，看到的行程清單都不一樣
    改成以執行檔位置為基準，走到哪都指向同一份資料。
    """
    if getattr(sys, "frozen", False):  # PyInstaller 打包後為 True
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


_BASE_DIR = _app_dir()
LOG_DIR = _BASE_DIR / "logs"
RECORDING_DIR = _BASE_DIR / "recordings"
DATA_DIR = _BASE_DIR / "data"  # 實驗數據 CSV 輸出目錄

# 只取得 logger 物件（不做任何 I/O）。真正的檔案 handler 由
# init_runtime() 在 main() 裡建立——見該函式的說明。
logger = logging.getLogger("DS102")
log_filename: Optional[Path] = None


def init_runtime() -> Tuple[bool, str]:
    """
    建立執行期目錄並初始化 LOG，回傳 (成功?, 錯誤訊息)。

    **刻意不在 module import 時做這件事。** 以前 mkdir 與
    logging.FileHandler() 都寫在模組層級，也就是在 main() 與任何 GUI
    之前執行；一旦目錄不可寫（exe 放在 Program Files、磁碟唯讀），
    例外會在「還沒有視窗可以顯示錯誤」的階段拋出——打包成 windowed exe
    的話，使用者看到的就是雙擊之後什麼都沒發生，連錯誤訊息都沒有。
    改成由 main() 呼叫並回傳結果，失敗時還來得及用 messagebox 說明。
    """
    global log_filename
    try:
        for d in (LOG_DIR, RECORDING_DIR, DATA_DIR):
            d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, (
            f"無法建立資料目錄：\n{e}\n\n"
            f"程式需要在下列位置寫入 logs / recordings / data：\n{_BASE_DIR}\n\n"
            f"請把程式移到有寫入權限的位置（例如桌面或 D:\\），"
            f"不要放在 Program Files。"
        )

    handlers: List[logging.Handler] = []
    try:
        log_filename = LOG_DIR / (
            f"ds102_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        )
        fh = logging.FileHandler(log_filename, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        handlers.append(fh)
    except OSError as e:
        log_filename = None
        return False, f"無法建立 LOG 檔：\n{e}\n\n位置：{LOG_DIR}"

    # ⚠ PyInstaller 的 --windowed 會把 sys.stdout / sys.stderr 設成 None，
    # 而 StreamHandler() 預設綁 sys.stderr。少了這道檢查，每一筆 log 的
    # emit() 都會踩 AttributeError 再被 logging 內部吞掉——不會崩，
    # 但這支程式 log 量不小，等於每筆都白付一次例外成本。
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setLevel(logging.INFO)
        handlers.append(sh)

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )
    return True, ""


def _write_json_with_backup(path: Path, data: dict, log=None) -> None:
    """
    覆寫 JSON 設定檔前先留一份 .bak，並以「先寫暫存再置換」避免寫到一半壞檔。

    這些檔（teaching_points / speed_profiles）是把整個記憶體字典整份寫回，
    所以任何沒先 load 就儲存的程式碼路徑都會把既有內容清空——實際發生過兩次，
    都是測試腳本建了新的 DS102Controller 就呼叫 save/delete。
    .bak 讓這種意外可以直接復原。
    """
    if path.exists():
        try:
            prev = path.read_text(encoding="utf-8")
            # 別用「空的」蓋掉「有內容的」備份，否則備份本身就沒意義了
            if prev.strip() not in ("", "{}"):
                path.with_suffix(path.suffix + ".bak").write_text(
                    prev, encoding="utf-8"
                )
        except OSError as e:
            if log:
                log("WARN", f"備份 {path.name} 失敗: {e}")

    # 目錄可能不存在（測試直接指定路徑、或 init_runtime() 沒跑過）
    path.parent.mkdir(parents=True, exist_ok=True)

    # 例外要在這裡收掉並轉成 LOG + 例外往上拋給呼叫端判斷，不能讓
    # OSError 直接穿過 Tk callback 變成 traceback。磁碟滿、防毒鎖檔、
    # 或 exe 被放在唯讀位置時都會走到這裡——打包之後尤其容易遇上。
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)
    except OSError as e:
        if log:
            log("ERROR", f"寫入 {path.name} 失敗: {e}")
        # 別把半成品的 .tmp 留在資料夾裡
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# =============================================================================
# 常數定義
# =============================================================================
AXES = ["X", "Y", "Z", "U", "V", "W"]
AXIS_NO = {"X": "1", "Y": "2", "Z": "3", "U": "4", "V": "5", "W": "6"}
NO_AXIS = {v: k for k, v in AXIS_NO.items()}  # 反向對應："1"→"X"

# 驅動模式（對應 main.py mode 變數）
MODE_CONTINUE = 0
MODE_STEP = 1
MODE_ORIGIN = 2

# 單位固定為 pulse（DS102 UNIT 指令代碼 0）。
# 本程式不再提供 um / mm 切換：控制器裡的 SD（每 pulse 距離）並未配置實際尺度，
# 換算成 um/mm 只是拿未經驗證的假設去乘除，徒增出錯機會。
UNIT_PULSE = "0"

# 原點模式清單。
#
# 🔴 **刻意從 1 開始，不提供 ORG 0。** move_origin() 第一件事就是送
# `AXI{n}:MEMSW0 {type}`，所以選 ORG 0 等於把該軸的復歸樣式寫成
# Type0＝不執行。後果三重：GO ORG 什麼都不做而空等 180 秒逾時、該軸的
# 復歸樣式被永久覆蓋（實機值 X=2/Y=1/Z=2）、以及把 controller_config.json
# 剛還原回去的設定當場毀掉。這個下拉的預設值以前就是 ORG 0。
ORG_MODES = [f"ORG {i}" for i in range(1, 13)]

# 「有終點」的驅動指令——送出後可以等它自己停下來。
#
# 刻意排除兩類：
#   GO CWJ / CCWJ  連續點動，沒有終點，要等下一個 STOP 0 才停
#   GO ORG         原點復歸，可能橫跨整個行程且途中壓限位屬正常流程
#
# `\b` 是關鍵：CW 後面接的 J 也是文字字元，所以 `GO CW\b` 不會誤中 `GO CWJ`。
# 少了它，重播點動時會誤以為該等到位而阻塞到逾時。
_FINITE_MOVE_RE = re.compile(r":GO\s+(?:CW|CCW|ABS|HOME)\b|:GO(?:ABS|TCH)\b")


def _is_finite_move(tx: str) -> bool:
    """這條指令是否會自己停在某個終點（＝值得呼叫 _wait_axis_stop）。"""
    return bool(_FINITE_MOVE_RE.search(tx))

# 通訊重送次數上限
MAX_RETRY = 3
# 到位輪詢逾時（秒）
WAIT_TIMEOUT = 30.0
# 到位輪詢間隔（秒）
WAIT_INTERVAL = 0.5
# 背景位置刷新間隔（秒）。每輪對每個已啟用軸送一筆 POS?（實測約 56ms／筆），
# 四軸約 0.22s，設 0.5s 讓序列埠仍有餘裕給移動中的到位輪詢。
POSITION_POLL_INTERVAL = 0.5
# 連續點動時的軟體限位監看間隔（秒）。單軸只送一筆 POS?，實測約 56ms。
JOG_WATCH_INTERVAL = 0.06

# 例行輪詢用的查詢指令：這些每秒會送出十幾筆，逐筆記 DEBUG TX/RX 會把
# LOG 檔、GUI 的 Text widget 與 action_history 全部灌爆，連帶把 Tk 的
# after() 佇列塞滿（每筆 log 都要回主執行緒重繪五個 StatusBar）。
# 預設不記錄它們；真要追通訊細節時把 DS102Controller.verbose_poll_log 設 True。
_POLL_QUERIES = ("POS?", "SB1?", "SB2?", "SB3?")

# action_history 的上限。以前是無上限 list，長時間執行會一路吃記憶體，
# 也是「偶發當機」的來源之一。
HISTORY_MAX = 5000
# stop() 願意花多久等 _serial_lock。等不到就插隊直接寫入——
# 停止指令遲到的代價是滑台繼續前進，比打斷別人一次查詢嚴重得多。
STOP_LOCK_TIMEOUT = 0.15

# 控制器設定檔（放在 RECORDING_DIR，與 teaching_points / speed_profiles 同區）。
# 存的是 MEMSW0 復歸樣式與韌體軟體限位——這些都是 RAM-only，
# 控制器一斷電就整組回到出廠值。
CONFIG_FILE = "controller_config.json"

# RECORDING_DIR 底下「不是行程檔」的 json——載入錄製清單時要跳過它們。
# 新增任何設定檔都要記得加進來。
NON_RECORDING_JSON = frozenset(
    {"teaching_points.json", "speed_profiles.json", CONFIG_FILE}
)
# GUI LOG 文字框保留的最大行數，超過就從頭截掉。
LOG_TEXT_MAX_LINES = 2000
# UI 座標重繪間隔（毫秒）。純重繪、不碰序列埠，所以只需要跟得上
# POSITION_POLL_INTERVAL(0.5s) 的資料更新即可，設 10ms 純屬浪費。
UI_REDRAW_INTERVAL = 100
# 連續幾輪讀不到任何軸的位置，就判定「畫面上的座標已不可信」。
# 3 輪 × POSITION_POLL_INTERVAL(0.5s) ≈ 1.5 秒，足以濾掉偶發的單次逾時。
COMM_FAIL_THRESHOLD = 3
# 橫幅「合併視窗」：這段時間內連續來的訊息會排隊依序顯示（避免互相覆蓋），
# 超過就直接換掉目前這則——超過表示新訊息多半是使用者剛按下按鈕的回饋，
# 那不該排隊等好幾秒才出現。
BANNER_COALESCE_SEC = 1.0

# 顏色主題
CLR_BG = "#F4F3F0"
CLR_CARD = "#FFFFFF"
CLR_BORDER = "#DEDBD3"
CLR_ACCENT = "#1D9E75"
CLR_DANGER = "#D93025"
CLR_INFO = "#1A73E8"
CLR_WARN = "#F9AB00"
CLR_TEXT = "#1F1F1E"
CLR_MUTED = "#80807A"
CLR_LOG_BG = "#1B1B1B"


# =============================================================================
# 後端控制器
# =============================================================================
class DS102Controller:
    """
    DS102/DS112 控制器核心類別。
    所有串列通訊均在此集中管理，GUI 只呼叫公開方法。
    指令格式完全依照 main.py 範本。
    """

    def __init__(self) -> None:
        self.ser: Optional[serial.Serial] = None
        self.port = ""
        self.baudrate = 38400
        self.connected = False
        self.ems_active = False
        # 設 True 才會把例行輪詢（POS?/SB?）的 TX/RX 寫進 LOG。
        # 預設關閉：那是每秒十幾筆的量，開著會把 LOG 與 UI 一起拖垮。
        self.verbose_poll_log = False

        # 當前選取軸號（字串"1"~"6"）
        self.axis_no = "1"
        self.drive_mode = MODE_CONTINUE

        # ── 執行緒鎖（保護共享資料，避免競爭條件）──
        self._lock = threading.Lock()
        # 序列埠交易鎖：一次 TX→RX 必須是不可分割的整體。
        # 移動執行緒的 _wait_axis_stop 與 UI 的狀態輪詢會同時查詢，
        # 沒有這把鎖時兩邊的問答會交錯，回應被對方讀走（位置錯亂、假的 limit 警報）。
        self._serial_lock = threading.RLock()

        # 各軸位置（內部永遠以 pulse 為單位儲存）
        self._positions_pulse: Dict[str, float] = {ax: 0.0 for ax in AXES}
        # 各軸座標偏置（工作原點 offset，以 pulse 為單位）
        self._offsets: Dict[str, float] = {ax: 0.0 for ax in AXES}
        # 各軸軟體行程限制（pulse，None 表示不限制）
        self.sw_limits: Dict[str, Tuple[Optional[float], Optional[float]]] = {
            ax: (None, None) for ax in AXES  # (CCW_limit, CW_limit)
        }

        self.firmware = ""
        self.axis_count = 0
        # 通訊健康度：連續讀不到位置的次數，與上次成功的時間戳。
        # 用來讓畫面能區分「這是即時值」與「這是停住的舊值」。
        self.comm_failures = 0
        self.last_position_ok = 0.0

        # 連線時偵測到「復歸樣式未設定」的軸（MEMSW0=0）。
        # MEMSW 是 RAM-only，控制器斷電後會全部歸零。
        self.homing_unconfigured: List[str] = []
        # 控制器設定（MEMSW0 / 韌體軟體限位）的存檔內容
        self.controller_config: dict = {}
        # 同 _points_loaded：沒載入就存檔會把既有設定整份蓋掉
        self._config_loaded = False
        # 連線時實際還原了哪些項目（供 GUI 顯示）
        self.config_restored: List[str] = []
        # 座標一律為 pulse——不提供單位切換，內部與顯示同一個數值

        # Teaching Points
        self.saved_points: Dict[str, dict] = {}
        # 是否已從磁碟載入過——沒載入就儲存會把既有點位整份蓋掉
        self._points_loaded = False

        # 動作歷史（含執行緒鎖保護）
        self._history_lock = threading.Lock()
        self.action_history: List[dict] = []

        # 行程錄製（僅錄驅動指令，排除查詢類指令）
        self.recording = False
        self.recorded_steps: List[dict] = []
        self._recording_name = ""
        self.recordings: List[dict] = []

        # 實驗數據記錄（CSV）
        self._data_log: List[dict] = []
        self._data_logging = False

        # 速度 Profile
        self.speed_profiles: Dict[str, dict] = {}
        # 同 _points_loaded：沒載入就儲存會把既有 Profile 整份蓋掉
        self._profiles_loaded = False

        # GUI LOG 回調
        self._log_cb = None

        # 重播鎖定旗標（重播中禁止其他移動操作）
        self.playback_running = False

        # 點動結束訊號：放開按鈕（stop）時設起，讓限位監看執行緒收工
        self._jog_stop = threading.Event()
        self._jog_stop.set()

        # 狀態異常回調（用於 GUI 彈窗）
        self._alarm_cb = None

    # =========================================================================
    # 屬性：positions（對外公開，自動扣除 offset）
    # =========================================================================
    @property
    def positions(self) -> Dict[str, float]:
        """
        回傳各軸工作座標（= 機械位置 − offset）。
        此為對外公開的顯示用座標，內部儲存以 _positions_pulse 為準。
        """
        with self._lock:
            return {ax: self._positions_pulse[ax] - self._offsets[ax] for ax in AXES}

    def set_offset_here(self, axis_no: str) -> None:
        """將當前位置設為工作原點（offset = 目前機械位置）"""
        ax = NO_AXIS.get(axis_no)
        if ax:
            with self._lock:
                self._offsets[ax] = self._positions_pulse[ax]
            self._log("INFO", f"軸 {ax} 工作原點已設為當前位置")

    def clear_offset(self, axis_no: str) -> None:
        """清除工作原點偏置，回復機械座標"""
        ax = NO_AXIS.get(axis_no)
        if ax:
            with self._lock:
                self._offsets[ax] = 0.0
            self._log("INFO", f"軸 {ax} 偏置已清除")

    # =========================================================================
    # LOG 系統
    # =========================================================================
    def set_log_callback(self, cb) -> None:
        self._log_cb = cb

    def set_alarm_callback(self, cb):
        """設定狀態異常回調（供 GUI 顯示彈窗警告）"""
        self._alarm_cb = cb

    def _is_poll_cmd(self, cmd: str) -> bool:
        """這條指令是否為每秒重複數次的例行輪詢（預設不寫進 LOG）。"""
        if self.verbose_poll_log:
            return False
        return any(q in cmd for q in _POLL_QUERIES)

    def _log(self, level: str, msg: str, tx: str = "", rx: str = ""):
        """
        統一 LOG 記錄入口。
        level: INFO / DEBUG / WARN / ERROR
        tx:    發送的原始串列指令（人讀描述寫在 msg）
        rx:    控制器回應字串
        """
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        entry = {"ts": ts, "level": level, "msg": msg, "tx": tx, "rx": rx}

        with self._history_lock:
            self.action_history.append(entry)
            # 上限保護：無上限的 list 在長時間執行下會一路吃記憶體。
            # 一次砍一批而不是每筆都 pop(0)，避免 O(n) 搬移成為新的負擔。
            if len(self.action_history) > HISTORY_MAX:
                del self.action_history[: len(self.action_history) - HISTORY_MAX]

        # ── 行程錄製：只記錄驅動指令（GO / STOP / MEMSW），排除查詢 ──
        # 查詢指令特徵：以 ? 結尾，或包含 SB1/SB2/SB3/POS?/CONTA/IDN/VER
        _is_query = tx.endswith("?") or any(
            k in tx for k in ["SB1?", "SB2?", "SB3?", "POS?", "CONTA?", "IDN?", "VER?"]
        )
        if self.recording and tx and not _is_query:
            self.recorded_steps.append({**entry, "delay_ms": 800})

        if self._log_cb:
            self._log_cb(entry)

        log_line = f"TX=[{tx}] RX=[{rx}] {msg}" if tx else msg
        getattr(
            logger,
            {"ERROR": "error", "WARN": "warning", "DEBUG": "debug"}.get(level, "info"),
        )(log_line)

    # =========================================================================
    # 串列通訊底層（含重送機制與逾時保護）
    # =========================================================================
    def _serial_write(self, cmd: str) -> None:
        """
        發送指令，不等待回應。
        對應 main.py serial_write()。
        重送：最多 MAX_RETRY 次，發送前先 flush 輸入緩衝。
        """
        raw = (cmd + "\r").encode("utf-8")
        for attempt in range(1, MAX_RETRY + 1):
            if not (self.ser and self.ser.is_open):
                break
            try:
                with self._serial_lock:
                    self.ser.reset_input_buffer()  # 清除殘留回應
                    self.ser.write(raw)
                # self._log("INFO", f"發送指令 (嘗試{attempt})", tx=cmd)
                return
            except serial.SerialException as e:
                self._log("WARN", f"寫入失敗 (嘗試{attempt}/{MAX_RETRY}): {e}", tx=cmd)
                time.sleep(0.05 * attempt)

        self._log("ERROR", f"指令發送失敗（{MAX_RETRY} 次均失敗）", tx=cmd)

    def _serial_write_read(self, cmd: str, timeout: float = 2.0) -> str:
        """
        發送指令並等待回應，回傳解碼字串。
        對應 main.py serial_write_read()。
        使用本地 timeout 保護，避免永久阻塞。
        重送機制：最多 MAX_RETRY 次。
        """
        raw = (cmd + "\r").encode("utf-8")
        # 例行輪詢（POS?/SB?）每秒十幾筆，逐筆記 log 會灌爆 LOG 與 UI。
        # 只有成功路徑安靜；WARN / ERROR 一律照記，異常不能被吃掉。
        quiet = self._is_poll_cmd(cmd)
        for attempt in range(1, MAX_RETRY + 1):
            if not (self.ser and self.ser.is_open):
                break
            try:
                # 整段 TX→RX 在鎖內完成，避免與其他執行緒的查詢交錯
                with self._serial_lock:
                    self.ser.reset_input_buffer()
                    self.ser.write(raw)
                    if not quiet:
                        self._log("DEBUG", f"TX: {cmd} (嘗試{attempt})", tx=cmd)
                    # 使用獨立 timeout 讀取回應
                    self.ser.timeout = timeout
                    data = self.ser.read_until(b"\r")
                    self.ser.timeout = 2.0  # 還原預設
                resp = data.decode("utf-8", errors="ignore").strip()
                if resp:
                    if not quiet:
                        self._log("DEBUG", f"RX: {resp}", rx=resp)
                    return resp
                self._log("WARN", f"空回應 (嘗試{attempt}/{MAX_RETRY})", tx=cmd)
            except serial.SerialException as e:
                self._log("WARN", f"讀寫失敗 (嘗試{attempt}/{MAX_RETRY}): {e}", tx=cmd)
            time.sleep(0.05 * attempt)

        self._log("ERROR", f"查詢失敗（{MAX_RETRY} 次均無回應）", tx=cmd)
        return ""

    # =========================================================================
    # 連線管理
    # =========================================================================
    def connect(self, port: str, baudrate: int = 38400) -> Tuple[bool, str]:
        """
        開啟 COM port 並驗證為 DS102/DS112 控制器。
        流程完全依照 main.py comm_port_open()。
        """
        if self.ser and self.ser.is_open:
            self.ser.close()
        try:
            self.ser = serial.Serial(port, baudrate, timeout=2)
        except serial.SerialException as e:
            return False, f"COM port 開啟失敗: {e}"

        # 驗證 IDN
        r = self._serial_write_read("*IDN?")
        if "SURUGA,DS1" not in str(r):
            self.ser.close()
            return False, (
                f"{port} 回應非預期：{r!r}\n" f"請確認連接的是 DS102/DS112 控制器。"
            )

        # 韌體版本
        self.firmware = self._serial_write_read("DS102VER?")
        self._log("INFO", f"韌體版本: {self.firmware}")

        # 軸數
        conta = self._serial_write_read("CONTA?")
        try:
            self.axis_count = int(conta)
        except ValueError:
            self.axis_count = 2
            self._log("WARN", f"CONTA? 異常({conta!r})，預設 2 軸")

        # 初始化各軸 UNIT=pulse, SELSP=0（依照 main.py：UNIT 0 = pulse）
        for i in range(self.axis_count):
            self._serial_write(f"AXI{i+1}:UNIT {UNIT_PULSE}:SELSP 0")
            time.sleep(0.1)

        self.port = port
        self.baudrate = baudrate
        self.connected = True

        # 連線後立刻讀一次各軸位置，否則畫面會停在 0 直到第一次移動
        self.refresh_positions()

        # 控制器設定是 RAM-only，斷電後會被清空。先把存檔的值補回去，
        # 再檢查還有沒有補不齊的（例如設定檔裡本來就沒有那一軸）。
        self.load_controller_config()
        self.config_restored = self.restore_controller_config()
        self.check_homing_config()

        msg = f"已連線至 {port}（{self.axis_count} 軸，韌體 {self.firmware}）"
        self._log("INFO", msg)
        return True, msg

    def refresh_positions(self) -> None:
        """
        查詢所有已啟用軸的目前位置，更新 _positions_pulse。

        只送 POS?（每軸一筆交易），比 query_status() 的三段式查詢輕得多，
        適合當成背景定時刷新。query_status() 一次只更新它被傳入的那一軸，
        不足以讓畫面上其餘各軸保持同步。

        同時維護通訊健康度（`comm_failures` / `last_position_ok`）：
        以前讀不到就 `continue`，畫面上的座標會**停在最後一次成功的數值不動**，
        而連線點仍是綠的——USB 被拔掉、控制器斷電都看不出來，操作者看到的是
        一組長得完全正常但其實已經跟硬體脫節的座標。
        """
        if not self.connected:
            return
        got_any = False
        for i in range(self.axis_count):
            axis_no = str(i + 1)
            ax = NO_AXIS.get(axis_no)
            if not ax:
                continue
            pos = self._serial_write_read(f"AXI{axis_no}:POS?")
            if not pos:
                continue
            try:
                with self._lock:
                    self._positions_pulse[ax] = float(pos)
                got_any = True
            except ValueError:
                continue

        if got_any:
            if self.comm_failures >= COMM_FAIL_THRESHOLD:
                self._log("INFO", "位置刷新已恢復正常")
            self.comm_failures = 0
            self.last_position_ok = time.time()
        else:
            self.comm_failures += 1
            if self.comm_failures == COMM_FAIL_THRESHOLD:
                self._log(
                    "ERROR",
                    f"連續 {self.comm_failures} 次讀不到任何軸的位置——"
                    f"畫面上的座標已不可信，請檢查連線",
                )

    @property
    def comm_stale(self) -> bool:
        """畫面上的座標是否已不可信（連續讀取失敗達門檻）。"""
        return self.connected and self.comm_failures >= COMM_FAIL_THRESHOLD

    def position_age(self) -> float:
        """距離上次成功讀到位置過了幾秒。未曾成功過回傳 inf。"""
        if not self.last_position_ok:
            return float("inf")
        return time.time() - self.last_position_ok

    # =========================================================================
    # 控制器設定持久化（MEMSW0 復歸樣式 + 韌體軟體限位）
    #
    # 為什麼需要：這些設定**全部是 RAM-only**，控制器斷電後整組回到出廠值
    # （2026-08-05 實機踩到：MEMSW0～7 全軸歸零、軟限位回到停用 ±99999999）。
    # MEMSW0=0 的語意是「復歸樣式 Type0＝不執行」，所以斷電後按「全軸原點
    # 復歸」會把每一軸都合法略過。把設定存檔並在連線時補回去，就不必每次
    # 手動重設。
    # =========================================================================
    def capture_controller_config(self) -> dict:
        """讀出控制器目前的設定並存檔，作為日後還原的基準。"""
        if not self.connected:
            self._log("WARN", "未連線，無法擷取控制器設定")
            return {}

        axes = {}
        for i in range(self.axis_count):
            n = str(i + 1)
            ax = NO_AXIS.get(n, n)
            axes[ax] = {
                "memsw0": self._serial_write_read(f"AXI{n}:MEMSW0?").strip(),
                "cwslp": self._serial_write_read(f"AXI{n}:CWSLP?").strip(),
                "ccwslp": self._serial_write_read(f"AXI{n}:CCWSLP?").strip(),
                "cwsle": self._serial_write_read(f"AXI{n}:CWSLE?").strip(),
                "ccwsle": self._serial_write_read(f"AXI{n}:CCWSLE?").strip(),
            }

        # 與磁碟上既有的設定合併，而不是整份覆蓋。
        # 若這次連線只認到部分軸（通訊異常、或接了不同台控制器），
        # 直接覆蓋會把其餘軸的設定清掉——與 teaching points 踩過的坑同一類。
        if not self._config_loaded:
            self.load_controller_config()
        merged = dict((self.controller_config or {}).get("axes", {}))
        dropped = set(merged) - set(axes)
        merged.update(axes)
        if dropped:
            self._log(
                "WARN",
                f"本次只讀到 {'、'.join(axes)} 的設定，"
                f"保留設定檔中既有的 {'、'.join(sorted(dropped))} 不動",
            )

        cfg = {
            "saved": datetime.now().isoformat(timespec="seconds"),
            "port": self.port,
            "firmware": self.firmware,
            "axes": merged,
        }
        self.controller_config = cfg
        self._config_loaded = True
        _write_json_with_backup(RECORDING_DIR / CONFIG_FILE, cfg, self._log)
        self._log(
            "INFO",
            f"控制器設定已存檔（{len(axes)} 軸）："
            + "、".join(f"{a}:MEMSW0={v['memsw0']}" for a, v in axes.items()),
        )
        return cfg

    def load_controller_config(self) -> dict:
        """載入設定檔。找不到就回空 dict（首次使用的正常情況）。"""
        p = RECORDING_DIR / CONFIG_FILE
        if not p.exists():
            self._config_loaded = True  # 沒有檔案也算「已知狀態」
            return {}
        try:
            self.controller_config = json.loads(p.read_text(encoding="utf-8"))
            self._config_loaded = True
            n = len(self.controller_config.get("axes", {}))
            self._log(
                "INFO",
                f"載入控制器設定檔（{n} 軸，存於 "
                f"{self.controller_config.get('saved', '?')}）",
            )
        except (OSError, json.JSONDecodeError) as e:
            self._log("ERROR", f"控制器設定檔讀取失敗: {e}")
        return self.controller_config

    def restore_controller_config(self) -> List[str]:
        """
        把設定檔裡的值補回控制器，回傳實際還原了哪些項目的說明。

        只在「控制器看起來被清空」時才寫入，不會蓋掉使用者刻意改過的值：
          - MEMSW0：設定檔有非 0 值、而控制器現在是 0 → 寫回
          - 軟體限位：設定檔記錄為啟用(1)、而控制器現在是停用(0) → 寫回座標再啟用

        未接滑台的軸一律跳過。
        """
        if not self.connected:
            return []
        axes_cfg = (self.controller_config or {}).get("axes", {})
        if not axes_cfg:
            return []

        restored: List[str] = []
        for i in range(self.axis_count):
            n = str(i + 1)
            ax = NO_AXIS.get(n, n)
            saved = axes_cfg.get(ax)
            if not saved:
                continue
            st, _ = self.query_status(n)
            if st == "Stage not connected":
                continue

            # ── 復歸樣式 ──
            want = str(saved.get("memsw0", "0")).strip()
            if want and want != "0":
                cur = self._serial_write_read(f"AXI{n}:MEMSW0?").strip()
                if cur == "0":
                    self._serial_write(f"AXI{n}:MEMSW0 {want}")
                    time.sleep(0.1)
                    back = self._serial_write_read(f"AXI{n}:MEMSW0?").strip()
                    if back == want:
                        restored.append(f"{ax} 復歸樣式={want}")
                    else:
                        self._log(
                            "ERROR",
                            f"軸 {ax} MEMSW0 寫回失敗（想寫 {want}，讀回 {back!r}）",
                        )

            # ── 韌體軟體限位：只在設定檔記錄為啟用時才補 ──
            for side, lp_key, le_key in (
                ("CW", "cwslp", "cwsle"),
                ("CCW", "ccwslp", "ccwsle"),
            ):
                if str(saved.get(le_key, "0")).strip() != "1":
                    continue
                if self._serial_write_read(f"AXI{n}:{side}SLE?").strip() == "1":
                    continue  # 已經啟用，不動它
                lp = str(saved.get(lp_key, "")).strip()
                if not lp:
                    continue
                self._serial_write(f"AXI{n}:{side}SLP {lp}")
                self._serial_write(f"AXI{n}:{side}SLE 1")
                time.sleep(0.1)
                restored.append(f"{ax} {side} 軟限位={lp}")

        if restored:
            self._log(
                "INFO",
                "控制器設定已從設定檔還原（斷電後會被清空）：" + "、".join(restored),
            )
        return restored

    def check_homing_config(self) -> List[str]:
        """
        檢查各軸的復歸樣式（MEMSW0）是否還在，回傳「未設定」的軸名清單。

        為什麼需要這個：**MEMSW 與韌體軟體限位一樣是 RAM-only**，控制器
        斷電後整組回到 0（2026-08-05 實測踩到）。而 MEMSW0=0 的語意是
        「復歸樣式 Type0＝不執行」，於是「全軸原點復歸」會把每一軸都合法
        略過、幾秒就跑完、看起來像成功——實際上滑台完全沒動。

        刻意**不自動寫回**任何值：復歸樣式決定滑台往哪個方向、用哪顆感測器
        找原點，猜錯會讓它往非預期方向跑完整個行程。這裡只負責讓使用者知道。
        """
        unset: List[str] = []
        if not self.connected:
            return unset
        for i in range(self.axis_count):
            axis_no = str(i + 1)
            ax = NO_AXIS.get(axis_no, axis_no)
            # 未接滑台的軸本來就不該復歸，不算「未設定」
            st, _ = self.query_status(axis_no)
            if st == "Stage not connected":
                continue
            if self._serial_write_read(f"AXI{axis_no}:MEMSW0?").strip() == "0":
                unset.append(ax)

        self.homing_unconfigured = unset
        if unset:
            self._log(
                "WARN",
                f"軸 {'、'.join(unset)} 的復歸樣式 MEMSW0=0（Type0＝不執行）"
                f"——原點復歸會直接略過這些軸。控制器斷電後 MEMSW 會全部歸零，"
                f"需要復歸的話請先設定各軸樣式。",
            )
        return unset

    def disconnect(self) -> None:
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.connected = False
        self._log("INFO", "已中斷連線")

    # =========================================================================
    # 軟體行程限制
    # =========================================================================
    def _check_sw_limit(self, axis_no: str, target_pulse: float) -> Tuple[bool, str]:
        """
        檢查目標位置是否超出軟體行程限制。
        回傳 (允許移動?, 原因訊息)。
        """
        ax = NO_AXIS.get(axis_no)
        if not ax:
            return True, ""
        ccw_lim, cw_lim = self.sw_limits.get(ax, (None, None))
        if ccw_lim is not None and target_pulse < ccw_lim:
            return False, (
                f"軸 {ax} 目標 {target_pulse:.1f} pulse "
                f"超出 CCW 限制 {ccw_lim:.1f} pulse"
            )
        if cw_lim is not None and target_pulse > cw_lim:
            return False, (
                f"軸 {ax} 目標 {target_pulse:.1f} pulse "
                f"超出 CW 限制 {cw_lim:.1f} pulse"
            )
        return True, ""

    # =========================================================================
    # 驅動指令（依照 main.py move_stage() 格式）
    # =========================================================================
    def move_continue(
        self,
        axis_no: str,
        direction: str,
        l_speed: str,
        f_speed: str,
        rate: str,
        s_rate: str,
    ):
        """
        連續點動（長按不放，放開後送 STOP 0）。
        格式：AXI{n}:L0 {l}:R0 {r}:S0 {s}:F0 {f}:GO CWJ / CCWJ

        點動沒有目標座標，無法像 move_step 那樣事先攔截，所以分兩層防護：
          1. 出發前：已經在該方向的軟體限位上就拒絕啟動
          2. 移動中：背景執行緒監看座標，越界立刻送 STOP
        """
        if self.ems_active or self.playback_running:
            return

        ax = NO_AXIS.get(axis_no)
        lim = None
        if ax:
            ccw_lim, cw_lim = self.sw_limits.get(ax, (None, None))
            lim = cw_lim if direction == "CW" else ccw_lim
            if lim is not None:
                with self._lock:
                    cur = self._positions_pulse.get(ax, 0.0)
                over = cur >= lim if direction == "CW" else cur <= lim
                if over:
                    reason = (
                        f"軸 {ax} 目前 {cur:.0f} pulse 已達 {direction} "
                        f"軟體限位 {lim:.0f} pulse"
                    )
                    self._log("WARN", f"點動被軟體限位攔截: {reason}")
                    if self._alarm_cb:
                        self._alarm_cb("軟體行程限制", reason)
                    return

        dir_str = "CWJ" if direction == "CW" else "CCWJ"
        cmd = (
            f"AXI{axis_no}:L0 {l_speed}:R0 {rate}"
            f":S0 {s_rate}:F0 {f_speed}:GO {dir_str}"
        )
        self._jog_stop.clear()
        self._serial_write(cmd)
        self._log("INFO", f"連續點動 軸{axis_no} {direction}", tx=cmd)

        if lim is not None:
            threading.Thread(
                target=self._watch_jog_limit,
                args=(axis_no, direction, lim, f_speed, rate),
                daemon=True,
            ).start()

    def _watch_jog_limit(
        self, axis_no: str, direction: str, lim: float, f_speed: str, rate: str
    ) -> None:
        """
        點動期間監看軟體限位，越界即送 STOP。

        這是「事後偵測」，從發現越界到真正停穩還會再前進一段，提前量要把
        兩件事都算進去：

          1. **偵測延遲** —— 一輪的實際耗時（送 POS? 約 56ms ＋ 等待間隔），
             最壞情況是剛量完就往前跑滿一輪。這個週期會隨序列埠負載變動，
             所以用實測值而非常數：每輪記下真正花掉的時間。
          2. **減速距離** —— 送出 STOP 後由 f_speed 減速到 0，減速時間為
             rate(ms)，期間平均速度約 f_speed/2，故距離 ≈ f_speed × rate / 2000

        兩次實測都證明少算會滑出限位外：只算等待間隔（60ms）時超出 24 pulse，
        補上減速項但週期仍用常數時超出 36 pulse——因為真正的週期是 116ms
        而非 60ms。寧可提早停，也不要越過限位，軟體限位的用途就是不該被越過。
        """
        ax = NO_AXIS.get(axis_no)
        if not ax:
            return
        try:
            f = abs(float(f_speed))
            r = abs(float(rate))
        except (ValueError, TypeError):
            f = r = 0.0
        brake = f * r / 2000.0
        # 首輪還沒有實測值，先用「查詢往返 ＋ 等待」的保守估計
        period = JOG_WATCH_INTERVAL + 0.06

        while not self._jog_stop.is_set():
            if self.ems_active:
                return
            t_start = time.time()
            pos = self._serial_write_read(f"AXI{axis_no}:POS?")
            try:
                cur = float(pos)
            except (ValueError, TypeError):
                # 回應空的或不是數字時要照樣睡一輪再重試。
                # 直接 continue 會變成不睡眠的忙迴圈，在序列埠本來就已經
                # 出狀況的當下再灌一堆查詢進去，只會讓情況更糟。
                self._jog_stop.wait(JOG_WATCH_INTERVAL)
                continue
            with self._lock:
                self._positions_pulse[ax] = cur

            lookahead = f * period + brake
            hit = (
                cur + lookahead >= lim
                if direction == "CW"
                else cur - lookahead <= lim
            )
            if hit:
                self.stop()
                reason = (
                    f"軸 {ax} 於 {cur:.0f} pulse 觸及 {direction} "
                    f"軟體限位 {lim:.0f} pulse，已停止"
                )
                self._log("WARN", reason)
                if self._alarm_cb:
                    self._alarm_cb("軟體行程限制", reason)
                return
            self._jog_stop.wait(JOG_WATCH_INTERVAL)
            # 實測這一輪的完整耗時，供下一輪推算提前量
            period = max(time.time() - t_start, JOG_WATCH_INTERVAL)

    def move_step(
        self,
        axis_no: str,
        direction: str,
        amount: str,
        l_speed: str,
        f_speed: str,
        rate: str,
        s_rate: str,
        wait_done: bool = True,
    ):
        """
        步進移動（一次性）。
        格式：AXI{n}:L0 {l}:R0 {r}:S0 {s}:F0 {f}:PULS {p}:GO CW / CCW
        wait_done=True 時，發送後阻塞直到到位（輪詢 SB1? Driving 位元清除）。
        amount 單位為 pulse。

        回傳 True=順利到位（或未要求等待），False=被攔截／逾時／撞限位。
        呼叫端請務必看這個回傳值：連續移動多軸時，第一軸撞了限位還往下跑
        就會演變成一路撞端點。
        """
        if self.ems_active or self.playback_running:
            return False
        try:
            pulse_amt = amount_f = float(amount)
        except ValueError:
            self._log("ERROR", f"步進距離格式錯誤: {amount}")
            return False

        ax = NO_AXIS.get(axis_no)
        if ax:
            with self._lock:
                cur = self._positions_pulse[ax]
            target = cur + (pulse_amt if direction == "CW" else -pulse_amt)
            ok, reason = self._check_sw_limit(axis_no, target)
            if not ok:
                self._log("WARN", f"軟體限位攔截: {reason}")
                if self._alarm_cb:
                    self._alarm_cb("軟體行程限制", reason)
                return False

        # DS102 韌體會「靜默忽略」帶小數點的 PULS 值：實測 PULS 500.0000
        # 完全不動且不回報錯誤，PULS 500 才會動。一律送整數。
        amount = f"{amount_f:.0f}"

        cmd = (
            f"AXI{axis_no}:L0 {l_speed}:R0 {rate}"
            f":S0 {s_rate}:F0 {f_speed}:PULS {amount}:GO {direction}"
        )
        self._serial_write(cmd)
        self._log("INFO", f"步進 軸{axis_no} {direction} {amount} pulse", tx=cmd)

        if wait_done:
            return self._wait_axis_stop(axis_no)
        return True

    def move_origin(
        self,
        axis_no: str,
        org_type: int,
        l_speed: str,
        f_speed: str,
        rate: str,
        s_rate: str,
        wait_done: bool = True,
    ):
        """
        單軸原點返回。
        格式：AXI{n}:MEMSW0 {type} → AXI{n}:L0...:GO ORG

        回傳 True=完成並已歸零，False=逾時或歸零失敗。

        等待用 `_wait_origin_done()` 而**不是** `_wait_axis_stop()`：
        復歸樣式本來就靠偵測限位感測器的邊緣來定位，途中壓到限位是正常
        流程，`_wait_axis_stop` 會把它當異常而提早回報失敗，逾時長度
        （30s）對橫跨整個行程的復歸也不夠。這點與 origin_all 一致。

        另外實測（2026-08-05，COM2）：**即使 MEMSW7 讀回是 0，GO ORG
        完成後 POS 也不會自動歸零**——所以復歸後一律確認並強制寫入 0，
        這正是「歸位後 0 點不固定」的成因。
        """
        if self.ems_active or self.playback_running:
            return False
        self._serial_write(f"AXI{axis_no}:MEMSW0 {org_type}")
        time.sleep(0.1)
        cmd = f"AXI{axis_no}:L0 {l_speed}:R0 {rate}" f":S0 {s_rate}:F0 {f_speed}:GO ORG"
        self._serial_write(cmd)
        self._log("INFO", f"原點返回 軸{axis_no} ORG{org_type}", tx=cmd)
        if not wait_done:
            return True

        ax = NO_AXIS.get(axis_no, axis_no)
        if not self._wait_origin_done(axis_no):
            self._log("ERROR", f"軸 {ax} 原點復歸逾時")
            if self._alarm_cb:
                self._alarm_cb(f"軸 {ax} 復歸逾時", "原點復歸未在時限內完成")
            return False

        _, pos = self.query_status(axis_no)
        try:
            if abs(float(pos)) < 0.5:
                return True
        except (ValueError, TypeError):
            pass
        self._log("WARN", f"軸 {ax} 復歸後 POS={pos} 未自動歸零，強制設為 0")
        self.set_position(axis_no, "0")
        return True

    @staticmethod
    def limit_direction(status: str) -> Optional[str]:
        """
        從狀態字串判斷「目前壓在哪一側的限位」，回傳 "CW" / "CCW" / None。

        涵蓋四種寫法：Detect CW limit / Detect CCW limit /
        Detect CW SW limit / Detect CCW SW limit。
        必須先判斷 CCW——"CCW" 字串本身就含有 "CW"，順序反了會全部誤判成 CW。
        """
        if "limit" not in status.lower():
            return None
        if "CCW" in status:
            return "CCW"
        if "CW" in status:
            return "CW"
        return None

    def _set_soft_limits_enabled(self, axis_no: str, on: bool) -> None:
        """開關單軸的控制器軟體限位（CWSLE / CCWSLE）。"""
        v = "1" if on else "0"
        self._serial_write(f"AXI{axis_no}:CWSLE {v}")
        self._serial_write(f"AXI{axis_no}:CCWSLE {v}")

    def _soft_limits_enabled(self, axis_no: str) -> Tuple[str, str]:
        """讀回 (CWSLE, CCWSLE) 目前值，供事後還原。"""
        return (
            self._serial_write_read(f"AXI{axis_no}:CWSLE?"),
            self._serial_write_read(f"AXI{axis_no}:CCWSLE?"),
        )

    def _wait_origin_done(self, axis_no: str, timeout: float = 180.0) -> bool:
        """
        等待原點復歸結束——只看 Driving 旗標清除，不把限位當失敗。

        不能沿用 _wait_axis_stop()：復歸樣式 5/6 本來就是靠偵測限位感測器
        的邊緣來定位，途中壓到限位是正常流程而非異常。
        復歸可能橫跨整個行程，所以逾時比一般移動寬鬆得多。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ems_active:
                return False
            sb1 = self._serial_write_read(f"AXI{axis_no}:SB1?")
            try:
                if not (int(sb1) & 0x40):  # bit6 Driving 清除
                    return True
            except (ValueError, TypeError):
                pass
            time.sleep(WAIT_INTERVAL)
        self._log("WARN", f"軸{axis_no} 原點復歸逾時（{timeout}s）")
        return False

    def origin_all(
        self,
        l_speed: str,
        f_speed: str,
        rate: str,
        s_rate: str,
        progress_cb=None,
    ) -> Tuple[bool, str]:
        """
        全軸依序原點復歸（GO ORG），使用各軸自己已設定的 MEMSW0 樣式。

        MEMSW7=0 時控制器會在復歸完成後自動把 POS 歸零，這是 DS102 設計上
        的「歸零」做法——比起把座標 0 當成目標點去追，這才是正解。

        復歸期間暫時停用控制器軟體限位：原點通常就落在行程末端（POS≈0），
        會在軟限位之外，不關掉會被自己設的保護擋住。結束後還原原本的啟用狀態。

        某一軸失敗不中止整批，繼續跑其餘各軸。
        回傳 (全部成功?, 摘要訊息)。
        """
        if self.ems_active or self.playback_running:
            return False, "EMS 作用中或重播進行中，已略過"

        done, skipped, failed = [], [], []
        saved: Dict[str, Tuple[str, str]] = {}

        try:
            for i in range(self.axis_count):
                axis_no = str(i + 1)
                ax = NO_AXIS.get(axis_no, axis_no)
                if progress_cb:
                    progress_cb(ax, "檢查中")

                st, _ = self.query_status(axis_no)
                if st == "Stage not connected":
                    skipped.append(f"{ax}(未接滑台)")
                    continue

                org_type = self._serial_write_read(f"AXI{axis_no}:MEMSW0?")
                if org_type.strip() == "0":
                    skipped.append(f"{ax}(復歸樣式 Type0＝不執行)")
                    continue

                # 暫時解除軟體限位，記下原值以便還原
                saved[axis_no] = self._soft_limits_enabled(axis_no)
                self._set_soft_limits_enabled(axis_no, False)

                # MEMSW7=0 才會讓控制器在復歸完成後自動把 POS 歸零。
                # 程式以前從來沒讀過也沒設過它，只是「假設」已經是 0——
                # 這正是「歸位後 0 點不固定」的成因：控制器若不是 0，
                # 復歸後座標會停在任意值。這裡明確設定，不再靠假設。
                msw7 = self._serial_write_read(f"AXI{axis_no}:MEMSW7?").strip()
                if msw7 != "0":
                    self._log(
                        "WARN",
                        f"軸 {ax} MEMSW7={msw7 or '讀取失敗'}（非 0），"
                        f"復歸不會自動歸零——已改設為 0",
                    )
                    self._serial_write(f"AXI{axis_no}:MEMSW7 0")
                    time.sleep(0.1)

                if progress_cb:
                    progress_cb(ax, f"復歸中 (Type{org_type})")
                cmd = (
                    f"AXI{axis_no}:L0 {l_speed}:R0 {rate}"
                    f":S0 {s_rate}:F0 {f_speed}:GO ORG"
                )
                self._serial_write(cmd)
                self._log("INFO", f"原點復歸 軸{ax} Type{org_type}", tx=cmd)
                time.sleep(0.1)  # 給控制器一點時間啟動復歸

                if not self._wait_origin_done(axis_no):
                    failed.append(f"{ax}(復歸逾時)")
                    continue

                _, pos2 = self.query_status(axis_no)
                try:
                    zeroed = abs(float(pos2)) < 0.5
                except (ValueError, TypeError):
                    zeroed = False
                if zeroed:
                    done.append(f"{ax}(POS=0)")
                else:
                    # 已經先設過 MEMSW7=0 還是沒歸零 → 直接強制寫入 POS 0。
                    # 「原點復歸後座標必為 0」是後續所有教點與限位的共同前提，
                    # 讓它停在任意值等於整組座標系失準。
                    self._log(
                        "WARN",
                        f"軸 {ax} 復歸後 POS={pos2} 未自動歸零，強制設為 0",
                    )
                    self.set_position(axis_no, "0")
                    _, pos3 = self.query_status(axis_no)
                    try:
                        forced_ok = abs(float(pos3)) < 0.5
                    except (ValueError, TypeError):
                        forced_ok = False
                    if forced_ok:
                        done.append(f"{ax}(POS=0 強制)")
                    else:
                        failed.append(f"{ax}(歸零失敗 POS={pos3})")
        finally:
            # 無論成功與否都要把軟體限位還原回去。
            #
            # 還原不到就一律開啟（"1"），絕不 fallback 到停用：
            # _serial_write_read 三次失敗會回傳空字串，舊寫法的 `cw or '0'`
            # 會把它變成 '0'＝停用，於是「序列埠壅塞一下」就等於把韌體端
            # 唯一可靠的那層保護永久關掉，而且不留任何痕跡。
            # 保護該有的失效方向是「寧可多擋」，不是「寧可放行」。
            for axis_no, (cw, ccw) in saved.items():
                ax = NO_AXIS.get(axis_no, axis_no)
                for cmd, val in (("CWSLE", cw), ("CCWSLE", ccw)):
                    v = (val or "").strip()
                    if v not in ("0", "1"):
                        self._log(
                            "ERROR",
                            f"軸 {ax} {cmd} 原始值讀不到（收到 {val!r}），"
                            f"改以啟用(1)還原——請確認限位設定是否符合預期",
                        )
                        v = "1"
                    self._serial_write(f"AXI{axis_no}:{cmd} {v}")

        parts = []
        if done:
            parts.append("已復歸: " + "、".join(done))
        if skipped:
            parts.append("略過: " + "、".join(skipped))
        if failed:
            parts.append("失敗: " + "、".join(failed))
        msg = "；".join(parts) if parts else "沒有可動作的軸"

        # 一軸都沒真的復歸 → 不可回報成功。
        #
        # 實機踩過：控制器斷電後 MEMSW 全組回到 0，而 MEMSW0=0 的語意是
        # 「復歸樣式 Type0＝不執行」，於是每一軸都被合法略過、耗時 0.9 秒、
        # 舊寫法回傳 True。使用者按下「全軸原點復歸」看到成功訊息，
        # 但滑台根本沒動、座標也沒歸零——比直接報錯更危險。
        if not done:
            hint = (
                "；沒有任何軸完成復歸。若略過原因是「復歸樣式 Type0」，"
                "表示控制器的 MEMSW0 未設定（斷電後會回到 0），"
                "需先對各軸設定復歸樣式再試"
            )
            self._log("ERROR", f"全軸原點復歸 — {msg}{hint}")
            return False, msg + hint

        self._log("INFO" if not failed else "ERROR", f"全軸原點復歸 — {msg}")
        return (not failed), msg

    def stop(self) -> None:
        """
        停止所有軸（STOP 0）。同時收掉點動的限位監看執行緒。

        **不會無限等 _serial_lock。** 這是「長按點動放開後卡死」的成因：
        position worker 若正卡在 read_until 的逾時裡（最壞 2s × 3 retry ≈ 6s），
        舊寫法走 _serial_write 會傻等整段時間，期間 UI 凍結而滑台持續前進。

        改成只短暫嘗試取鎖（STOP_LOCK_TIMEOUT），取不到就直接寫入。
        STOP 是唯寫、不讀回應，插隊最壞只會打斷別人的一次查詢，
        對方本來就有 MAX_RETRY 重送機制。用一次重送換即時停止很划算。
        """
        self._jog_stop.set()  # 先讓監看執行緒收工——這一步不需要序列埠
        cmd = "STOP 0"

        if not (self.ser and self.ser.is_open):
            return

        raw = (cmd + "\r").encode("utf-8")
        got = self._serial_lock.acquire(timeout=STOP_LOCK_TIMEOUT)
        try:
            self.ser.write(raw)
        except Exception as e:  # 停止路徑不可讓例外逃逸
            self._log("ERROR", f"停止指令送出失敗: {e}", tx=cmd)
            return
        finally:
            if got:
                self._serial_lock.release()

        if got:
            self._log("INFO", "停止所有軸", tx=cmd)
        else:
            self._log(
                "WARN", "停止所有軸（序列埠忙碌，已插隊送出）", tx=cmd
            )

    def emergency_stop(self) -> None:
        """
        緊急停止：繞過所有佇列直接寫入串列埠。
        刻意「不」取 _serial_lock——若此刻有查詢正卡在讀取逾時，
        等鎖會延遲停止指令數秒。安全性上寧可讓這個位元組插隊。
        """
        self.ems_active = True
        self.playback_running = False
        raw = b"STOP 0\r"
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(raw)
            except Exception:
                pass
        self._log("ERROR", "🚨 緊急停止！", tx="STOP 0")

    def release_ems(self) -> None:
        """解除緊急停止（GUI 層需額外要求位置確認）"""
        self.ems_active = False
        self._log("INFO", "緊急停止已解除，請確認各軸位置後再操作")

    # =========================================================================
    # 到位等待（核心改善：確保步進完成後再繼續）
    # =========================================================================
    def _wait_axis_stop(self, axis_no: str, timeout: float = WAIT_TIMEOUT) -> bool:
        """
        阻塞等待指定軸停止（SB1 bit6 Driving 旗標清除）。
        同時偵測異常狀態（Limit）並觸發警報回調。
        回傳：True=正常停止，False=逾時或異常。
        此方法應在背景執行緒呼叫，避免凍結 UI。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ems_active:
                return False
            # query_status 內部已完成位置換算與寫入，此處不重複處理
            status, _pos = self.query_status(axis_no)
            # 記錄數據
            if self._data_logging:
                self._record_data_point()

            if status == "Stop":
                return True
            if status == "Driving":
                time.sleep(WAIT_INTERVAL)
                continue
            # 其他狀態（Limit / 異常）→ 記錄並觸發警報。
            # 這段一度被註解掉，加上呼叫端忽略回傳值，結果是撞限位全程靜默：
            # teaching point 超出行程時三軸會接連撞端點而畫面與 LOG 都沒有警告。
            ax = NO_AXIS.get(axis_no, axis_no)
            self._log("WARN", f"軸 {ax} 等待到位時進入異常狀態: {status}")
            if self._alarm_cb:
                self._alarm_cb(f"軸 {ax} 異常", status)
            return False

        self._log("WARN", f"軸{axis_no} 等待到位逾時（{timeout}s）")
        return False

    # =========================================================================
    # 狀態查詢（對應 main.py update_status()）
    # =========================================================================
    def query_status(self, axis_no: str) -> Tuple[str, str]:
        """
        查詢指定軸狀態與位置。
        回傳 (狀態字串, 位置字串_pulse)。
        """
        sb3 = self._serial_write_read(f"AXI{axis_no}:SB3?")
        try:
            if not (int(sb3) & 0x01):
                return "軸無法選取", ""
        except (ValueError, TypeError):
            return "通訊錯誤", ""

        sb1 = self._serial_write_read(f"AXI{axis_no}:SB1?")
        try:
            sb1_val = int(sb1)
        except (ValueError, TypeError):
            return "通訊錯誤", ""

        if sb1_val & 0x40:
            status = "Driving"
        elif sb1_val & 0x10:
            status = "Detect origin"
        elif sb1_val & 0x06:
            sb2 = self._serial_write_read(f"AXI{axis_no}:SB2?")
            try:
                sb2_val = int(sb2)
            except (ValueError, TypeError):
                sb2_val = 0
            if sb2_val & 0x03 == 0x03:
                status = "Stage not connected"
            elif sb2_val & 0x01:
                status = "Detect CW limit"
            elif sb2_val & 0x02:
                status = "Detect CCW limit"
            elif sb2_val & 0x04:
                status = "Detect CW SW limit"
            elif sb2_val & 0x08:
                status = "Detect CCW SW limit"
            else:
                status = "Limit"
        else:
            status = "Stop"

        pos = self._serial_write_read(f"AXI{axis_no}:POS?")
        ax = NO_AXIS.get(axis_no)
        if ax and pos:
            try:
                # 控制器 UNIT 固定為 pulse（連線時設定），POS? 回傳值即 pulse。
                with self._lock:
                    self._positions_pulse[ax] = float(pos)
            except ValueError:
                pass

        return status, pos

    def set_position(self, axis_no: str, value: str) -> None:
        """設定當前位置（AXI{n}:POS {val}）"""
        cmd = f"AXI{axis_no}:POS {value}"
        self._serial_write(cmd)
        # 同步更新內部值
        ax = NO_AXIS.get(axis_no)
        if ax:
            try:
                with self._lock:
                    self._positions_pulse[ax] = float(value)
            except ValueError:
                pass
        self._log("INFO", f"軸{axis_no} 位置設為 {value} pulse", tx=cmd)

    # =========================================================================
    # 速度 Profile 管理
    # =========================================================================
    def save_speed_profile(
        self, name: str, l_speed: str, f_speed: str, rate: str, s_rate: str
    ):
        """儲存速度 Profile"""
        self.speed_profiles[name] = {
            "l_speed": l_speed,
            "f_speed": f_speed,
            "rate": rate,
            "s_rate": s_rate,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        self._persist_profiles()
        self._log("INFO", f"速度 Profile [{name}] 已儲存")

    def delete_speed_profile(self, name: str) -> None:
        self.speed_profiles.pop(name, None)
        self._persist_profiles()
        self._log("INFO", f"速度 Profile [{name}] 已刪除")

    def _persist_profiles(self) -> None:
        p = RECORDING_DIR / "speed_profiles.json"
        # 與 _persist_points() 同一套防護：沒 load 過就寫回，等於拿一份不完整的
        # 記憶體狀態覆蓋磁碟。teaching points 早就有這層保護，profiles 一直沒有。
        if not self._profiles_loaded and p.exists():
            try:
                existing = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            missing = set(existing) - set(self.speed_profiles)
            if missing:
                self._log(
                    "ERROR",
                    f"拒絕寫入 speed_profiles.json：未先 load_speed_profiles() 就儲存，"
                    f"會遺失 {len(missing)} 個既有 Profile（{'、'.join(sorted(missing))}）",
                )
                return
        _write_json_with_backup(p, self.speed_profiles, self._log)

    def load_speed_profiles(self) -> None:
        p = RECORDING_DIR / "speed_profiles.json"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                self.speed_profiles = json.load(f)
            self._profiles_loaded = True
            self._log("INFO", f"載入 {len(self.speed_profiles)} 個速度 Profile")

    # =========================================================================
    # Teaching Points
    # =========================================================================
    def save_point(self, name: str, positions: Dict[str, float]) -> None:
        """
        儲存 Teaching Point（工作座標，單位 pulse）。
        只有有填值的軸會被納入；未納入的軸在 goto_point 時不動。
        """
        self.saved_points[name] = {
            "positions_pulse": dict(positions),
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        self._log("INFO", f"Teaching Point [{name}] 已儲存: {positions}")
        self._persist_points()

    def delete_point(self, name: str) -> None:
        self.saved_points.pop(name, None)
        self._log("INFO", f"Teaching Point [{name}] 已刪除")
        self._persist_points()

    def goto_point(
        self,
        name: str,
        l_speed: str,
        f_speed: str,
        rate: str,
        s_rate: str,
        wait_done: bool = True,
    ):
        """
        移動至 Teaching Point。
        對各軸依序發送步進指令（先確認 X→Y→Z 順序或可設定）。
        """
        if name not in self.saved_points:
            self._log("ERROR", f"Teaching Point [{name}] 不存在")
            return False
        if self.ems_active or self.playback_running:
            return False

        pt = self.saved_points[name]
        pos_pulse = pt.get("positions_pulse", {})
        self._log("INFO", f"移動至 Teaching Point [{name}]")

        # Teaching Point 存的是「工作座標」（已扣除 offset），
        # 而 _positions_pulse 與 sw_limits 都是機械座標，比較前必須換到同一個座標系。
        for ax, target_work in pos_pulse.items():
            axis_no = AXIS_NO.get(ax)
            if not axis_no:
                continue
            # 僅移動有啟用的軸
            if int(axis_no) > self.axis_count:
                continue

            with self._lock:
                cur_pulse = self._positions_pulse.get(ax, 0.0)
                target_pulse = target_work + self._offsets.get(ax, 0.0)

            ok, reason = self._check_sw_limit(axis_no, target_pulse)
            if not ok:
                self._log("WARN", f"Teaching goto 軟體限位: {reason}")
                if self._alarm_cb:
                    self._alarm_cb("軟體行程限制", reason)
                return False

            delta_pulse = target_pulse - cur_pulse
            if abs(delta_pulse) < 0.5:  # 已在目標位置（<0.5 pulse）
                continue
            direction = "CW" if delta_pulse > 0 else "CCW"

            # 送出前先確認這一軸真的能往那個方向走。
            # 原點復歸後座標 0 就落在限位開關上，(0,0,0) 這種點會把每一軸
            # 都往端點推；再加上未接滑台的軸，結果就是一連串限位警報。
            st, _ = self.query_status(axis_no)
            if st == "Stage not connected":
                self._log("WARN", f"軸 {ax} 未接滑台，跳過")
                continue
            if self.limit_direction(st) == direction:
                self._log(
                    "WARN",
                    f"軸 {ax} 已在 {direction} 限位上（{st}），"
                    f"無法再往 {direction} 走，跳過",
                )
                continue
            moved = self.move_step(
                axis_no,
                direction,
                f"{abs(delta_pulse):.0f}",
                l_speed,
                f_speed,
                rate,
                s_rate,
                wait_done=wait_done,
            )

            # 這一軸若不是正常停下來（撞限位／逾時），後面的軸不要再跑。
            # 目標點超出行程時，逐軸執行會演變成連續撞端點——而且因為
            # 以前這裡沒有檢查、move_step 也沒回傳值，全程不會有任何警告。
            if wait_done and not moved:
                st, _ = self.query_status(axis_no)
                self._log(
                    "ERROR", f"軸 {ax} 未能正常到位（{st}），中止 Teaching 移動"
                )
                if self._alarm_cb:
                    self._alarm_cb(f"軸 {ax} 未到位", f"{st}（已中止後續軸）")
                return False
        return True

    def _persist_points(self) -> None:
        p = RECORDING_DIR / "teaching_points.json"
        # 沒 load 過就寫回，等於拿一份不完整的記憶體狀態覆蓋磁碟。
        # 正常流程 GUI 啟動一定會 load_points()，會走到這裡的多半是
        # 直接 new 一個 controller 的測試腳本。
        if not self._points_loaded and p.exists():
            try:
                existing = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            missing = set(existing) - set(self.saved_points)
            if missing:
                self._log(
                    "ERROR",
                    f"拒絕寫入 teaching_points.json：未先 load_points() 就儲存，"
                    f"會遺失 {len(missing)} 個既有點位（{'、'.join(sorted(missing))}）",
                )
                return
        _write_json_with_backup(p, self.saved_points, self._log)

    def load_points(self) -> None:
        p = RECORDING_DIR / "teaching_points.json"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                self.saved_points = json.load(f)
            self._points_loaded = True
            self._log("INFO", f"載入 {len(self.saved_points)} 個 Teaching Points")

    # =========================================================================
    # 行程錄製與重播
    # =========================================================================
    def start_recording(self, name: str = "") -> None:
        self.recording = True
        self.recorded_steps = []
        self._recording_name = name or f"rec_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self._log("INFO", f"開始錄製行程: {self._recording_name}")

    def stop_recording(self) -> dict:
        self.recording = False
        rec = {
            "name": self._recording_name,
            "created": datetime.now().isoformat(timespec="seconds"),
            "steps": list(self.recorded_steps),
            "count": len(self.recorded_steps),
        }
        self.recordings.append(rec)
        self.save_recording(rec)
        self._log(
            "INFO", f"行程 [{rec['name']}] 已儲存，{rec['count']} 步（純驅動指令）"
        )
        return rec

    def save_recording(self, rec: dict) -> bool:
        """
        把單一行程寫回它自己的 json。

        改動步驟延遲（單步或整批）以前只改了記憶體裡的 dict，重開程式就
        變回原值。任何修改 rec 內容的地方都要呼叫這個，否則使用者會以為
        設定有存到。走 _write_json_with_backup 以取得 .bak 與原子置換。
        """
        name = rec.get("name")
        if not name:
            self._log("ERROR", "行程沒有名稱，無法儲存")
            return False
        try:
            _write_json_with_backup(
                RECORDING_DIR / f"{name}.json", rec, self._log
            )
            return True
        except OSError as e:
            self._log("ERROR", f"行程 [{name}] 寫入失敗: {e}")
            return False

    def play_recording(
        self,
        rec: dict,
        repeat: int = 1,
        stop_event: Optional[threading.Event] = None,
        progress_cb=None,
        cycle_delay_ms: int = 3000,
    ):
        """
        重播行程。
        - 重播前設定 playback_running=True，鎖定其他移動操作。
        - 每步先等待前一步到位，再發送下一步，確保精度。
        - 發送完畢才等待該步自己的 delay_ms（不含到位等待時間）。

        `cycle_delay_ms` 是**跑完一輪完整行程之後、下一輪開始之前**的間隔，
        不是步與步之間的延遲（那是各步驟自己的 `delay_ms`）。
        只在輪與輪之間等待，第一輪不等——以前這裡是寫死的 `time.sleep(3)`，
        而且連第一輪之前都會等，畫面上完全沒有提示，看起來像沒反應。
        """
        self.playback_running = True
        steps = rec.get("steps", [])
        total = len(steps) * repeat
        done = 0
        completed = False  # 只有正常跑完才會被設 True，見 finally
        self._log(
            "INFO",
            f"開始重播 [{rec['name']}] × {repeat}，共 {total} 步"
            + (f"，每輪間隔 {cycle_delay_ms}ms" if repeat > 1 else ""),
        )

        try:
            for cycle in range(repeat):
                # 輪與輪之間的間隔（第一輪不等）。分段睡以便中途可中止。
                if cycle > 0 and cycle_delay_ms > 0:
                    if progress_cb:
                        progress_cb(done, total, f"等待 {cycle_delay_ms}ms")
                    waited = 0.0
                    while waited < cycle_delay_ms / 1000.0:
                        if (stop_event and stop_event.is_set()) or self.ems_active:
                            self._log("WARN", "重播已中止（輪間等待中）")
                            return
                        time.sleep(min(0.1, cycle_delay_ms / 1000.0 - waited))
                        waited += 0.1
                if progress_cb and repeat > 1:
                    progress_cb(done, total, f"第 {cycle + 1}/{repeat} 輪")
                for step in steps:
                    if stop_event and stop_event.is_set():
                        self._log("WARN", "重播已中止")
                        return
                    if self.ems_active:
                        self._log("WARN", "EMS 中止重播")
                        return
                    tx = step.get("tx", "")
                    if tx:
                        self._serial_write(tx)
                        # 只有「有終點」的指令才等到位。
                        #
                        # GO CWJ / CCWJ 是連續點動，沒有終點——它會一直跑到
                        # 下一個 step 的 STOP 0 才停。舊寫法用 `"GO" in tx`
                        # 把點動也納入等待，於是重播時 _wait_axis_stop 會阻塞
                        # 到 30 秒逾時（或撞上硬體限位），STOP 0 遲遲送不出去。
                        # ORG 同樣排除：它可能橫跨整個行程且途中壓限位屬正常。
                        if _is_finite_move(tx):
                            ax_m = re.search(r"AXI(\d)", tx)
                            if ax_m:
                                if not self._wait_axis_stop(ax_m.group(1)):
                                    self._log(
                                        "ERROR", "重播中某軸未能正常到位，已中止"
                                    )
                                    return
                    # 步與步之間用該步自己的 delay_ms（可在 GUI 雙擊修改並存檔）
                    delay_ms = step.get("delay_ms", 200)
                    time.sleep(max(0, delay_ms) / 1000.0)
                    done += 1
                    if progress_cb:
                        progress_cb(done, total)
            completed = True
        finally:
            self.playback_running = False
            # 中途離開（使用者中止、EMS、某軸未到位、例外）時務必送出停止。
            # 被中斷的那一步若是 GO CWJ（連續點動、沒有終點），錄製檔裡負責
            # 收尾的 STOP 0 就永遠送不出去了，該軸會一路跑到硬體限位。
            # 正常跑完不送——最後一步本來就有自己的收尾。
            if not completed:
                self.stop()
                self._log("WARN", "重播未完整結束，已送出停止指令")

        self._log("INFO", f"行程 [{rec['name']}] 重播完成")

    def delete_recording(self, name: str) -> Tuple[bool, str]:
        """
        刪除已儲存的行程：從記憶體清單移除，並把它的 json 改名成 .bak。

        刻意用「改名成 .bak」而不是真的 unlink——這個專案已經因為
        「整份覆蓋」的寫法弄丟過兩次 teaching points，行程檔同樣是使用者
        花時間錄出來的東西，留一份可救回的副本成本很低。
        `.bak` 不符合 `*.json` 的 glob，所以不會再被載入清單。

        回傳 (成功?, 給人看的訊息)。
        """
        if self.recording:
            return False, "錄製進行中，請先停止錄製"
        if self.playback_running:
            return False, "重播進行中，無法刪除行程"

        idx = next(
            (i for i, r in enumerate(self.recordings) if r.get("name") == name), None
        )
        if idx is None:
            return False, f"找不到行程 [{name}]"

        p = RECORDING_DIR / f"{name}.json"
        backup = ""
        if p.exists():
            bak = p.with_suffix(p.suffix + ".bak")
            try:
                p.replace(bak)  # 原子改名，比「讀出→寫入→刪除」安全
                backup = str(bak)
            except OSError as e:
                self._log("ERROR", f"行程 [{name}] 檔案刪除失敗: {e}")
                return False, f"檔案刪除失敗: {e}"

        self.recordings.pop(idx)
        msg = f"行程 [{name}] 已刪除"
        if backup:
            msg += f"（備份保留於 {Path(backup).name}）"
        self._log("INFO", msg)
        return True, msg

    def load_recordings_from_disk(self) -> None:
        # RECORDING_DIR 同時放行程檔與設定檔，載入行程時必須把設定檔排除，
        # 否則它們會以「沒有 steps 的行程」身分出現在清單裡。
        # 新增設定檔時記得同步加進這個集合。
        for p in sorted(RECORDING_DIR.glob("*.json")):
            if p.name in NON_RECORDING_JSON:
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    rec = json.load(f)
                if not any(r.get("name") == rec.get("name") for r in self.recordings):
                    self.recordings.append(rec)
            except Exception as e:
                self._log("WARN", f"無法載入行程 {p.name}: {e}")

    # =========================================================================
    # 實驗數據記錄（CSV）
    # =========================================================================
    def start_data_log(self) -> None:
        self._data_log = []
        self._data_logging = True
        self._log("INFO", "實驗數據記錄已啟動")

    def stop_data_log(self) -> str:
        """停止記錄並儲存 CSV，回傳檔案路徑"""
        self._data_logging = False
        path = DATA_DIR / f"data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["ts"] + AXES)
            writer.writeheader()
            writer.writerows(self._data_log)
        self._log("INFO", f"實驗數據已匯出: {path}（{len(self._data_log)} 筆）")
        return str(path)

    def _record_data_point(self) -> None:
        """記錄當前時間戳與各軸位置（在 _wait_axis_stop 中定期呼叫）"""
        with self._lock:
            row = {"ts": datetime.now().isoformat(timespec="milliseconds")}
            row.update({ax: self._positions_pulse[ax] for ax in AXES}) # type: ignore
        with self._history_lock:
            self._data_log.append(row)

    # =========================================================================
    # LOG 匯出
    # =========================================================================
    def export_log(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            with self._history_lock:
                history = list(self.action_history)
            for h in history:
                tx = f" TX=[{h['tx']}]" if h.get("tx") else ""
                rx = f" RX=[{h['rx']}]" if h.get("rx") else ""
                f.write(f"[{h['ts']}] [{h['level']}]{tx}{rx} {h['msg']}\n")
        self._log("INFO", f"LOG 已匯出: {path}")


# =============================================================================
# 常駐狀態列
# =============================================================================
class StatusBar(tk.Frame):
    """每個分頁底部的常駐狀態列：顯示所有軸工作座標（pulse）與最新 LOG。"""

    def __init__(self, parent, ctrl: DS102Controller, **kwargs):
        super().__init__(parent, bg=CLR_BORDER, **kwargs)
        self.ctrl = ctrl

        coord_frame = tk.Frame(self, bg=CLR_CARD)
        coord_frame.pack(fill="x", padx=1, pady=(1, 0))

        self._coord_labels: Dict[str, tk.Label] = {}
        self._axis_cells: Dict[str, tk.Frame] = {}
        self._axis_names: Dict[str, tk.Label] = {}
        for ax in AXES:
            cell = tk.Frame(coord_frame, bg=CLR_CARD)
            cell.pack(side="left", padx=6, pady=2)
            name = tk.Label(
                cell,
                text=f"{ax}:",
                bg=CLR_CARD,
                fg=CLR_MUTED,
                font=("Segoe UI", 8, "bold"),
            )
            name.pack(side="left")
            lbl = tk.Label(
                cell,
                # 初值不可是 0——三軸的 0 幾乎就落在限位開關上，顯示一組
                # 「所有軸都壓在端點」的假座標，而且完全看不出它是假的。
                text="—",
                bg=CLR_CARD,
                fg=CLR_MUTED,
                font=("Consolas", 10, "bold"),
                width=10,
                anchor="e",
            )
            lbl.pack(side="left")
            self._coord_labels[ax] = lbl
            self._axis_cells[ax] = cell
            self._axis_names[ax] = name

        # 資料新鮮度：讓「停住的舊值」看得出來，不要偽裝成即時值
        self._age_var = tk.StringVar(value="未連線")
        self._age_lbl = tk.Label(
            coord_frame,
            textvariable=self._age_var,
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 8),
        )
        self._age_lbl.pack(side="right", padx=6)

        log_frame = tk.Frame(self, bg="#E8E7E2")
        log_frame.pack(fill="x", padx=1, pady=(0, 1))
        self._log_var = tk.StringVar(value="就緒")
        tk.Label(
            log_frame,
            textvariable=self._log_var,
            bg="#E8E7E2",
            fg=CLR_MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", padx=6, pady=1)

    def update_coords(self):
        """
        刷新各軸工作座標（機械位置扣除 offset，單位 pulse）。

        三種狀態要能一眼分辨，這是控制台的基本要求：
          未連線   → 全部「—」，絕不顯示 0（0 幾乎就在限位開關上）
          失聯     → 座標轉為警告色並標示「已停止更新」
          正常     → 顯示數值，右側標註資料年齡
        另外把**當前選取軸**highlight 出來——選錯軸就是驅動錯的滑台，
        而軸選擇器只存在於兩個分頁，其餘分頁完全看不出選的是哪一軸。
        """
        connected = self.ctrl.connected
        stale = self.ctrl.comm_stale
        cur_ax = NO_AXIS.get(self.ctrl.axis_no)
        n_axes = self.ctrl.axis_count if connected else 0
        pos_work = self.ctrl.positions

        for ax, lbl in self._coord_labels.items():
            enabled = connected and int(AXIS_NO[ax]) <= n_axes
            is_cur = enabled and ax == cur_ax

            if not enabled:
                text, fg = "—", CLR_MUTED
            elif stale:
                text, fg = f"{pos_work.get(ax, 0.0):,.0f}", CLR_DANGER
            else:
                text, fg = f"{pos_work.get(ax, 0.0):,.0f}", CLR_TEXT
            lbl.config(text=text, fg=fg)

            # 當前軸用底色標示（比改字色顯眼，且不與失聯的警告色打架）
            bg = CLR_ACCENT if is_cur else CLR_CARD
            self._axis_cells[ax].config(bg=bg)
            self._axis_names[ax].config(
                bg=bg, fg="white" if is_cur else CLR_MUTED
            )
            lbl.config(bg=bg, fg="white" if (is_cur and not stale) else fg)

        if not connected:
            self._age_var.set("未連線")
            self._age_lbl.config(fg=CLR_MUTED)
        elif stale:
            self._age_var.set("⚠ 已停止更新")
            self._age_lbl.config(fg=CLR_DANGER)
        else:
            age = self.ctrl.position_age()
            self._age_var.set(
                "pulse · 剛更新" if age < 1.5 else f"pulse · {age:.0f}s 前"
            )
            self._age_lbl.config(fg=CLR_MUTED if age < 2 else CLR_WARN)

    def update_log(self, msg: str):
        self._log_var.set(msg[:100])


# =============================================================================
# 主 GUI
# =============================================================================
class DS102GUI:

    def __init__(self, root: tk.Tk):
        self.root = root
        self.ctrl = DS102Controller()
        self.ctrl.set_log_callback(self._on_log_entry)
        self.ctrl.set_alarm_callback(self._on_alarm)

        # 全域 StringVar
        self._axis_no_var = tk.StringVar(value="1")
        # 軸名（X/Y/Z…）——畫面上一律用軸名，軸號只在組指令時用
        self._axis_name_var = tk.StringVar(value="X")

        # 事件
        self._stop_playback = threading.Event()
        # 狀態輪詢中旗標，避免連按驅動鍵時疊出多條輪詢執行緒搶序列埠
        self._poll_busy = threading.Event()
        # 關閉旗標，讓背景位置刷新執行緒能收工
        self._shutting_down = threading.Event()
        # 全軸原點復歸進行中。_update_stat_ui 每輪都會重設按鈕狀態，
        # 沒有這個旗標的話它會在 100ms 後把復歸期間的鎖定解掉。
        self._homing = threading.Event()

        # 按鈕組（多分頁同步更新）
        self._all_axis_btn_groups: List[Dict[str, tk.Button]] = []

        # StatusBar 清單
        self._status_bars: List[StatusBar] = []

        # LOG 文字框
        self._log_text: Optional[tk.Text] = None
        self._log_auto_scroll = tk.BooleanVar(value=True)
        self._log_filter_var = tk.StringVar(value="全部")
        self._log_count_var = tk.StringVar(value="")

        # 移動控制 UI 參考
        self._ctrl_status_var = tk.StringVar(value="Stop")
        self._ctrl_pos_var = tk.StringVar(value="0")
        self._drive_mode_var = tk.IntVar(value=MODE_CONTINUE)

        # 驅動按鈕參考（連線前 disable）
        self._drive_buttons: List[tk.Button] = []

        # 非強制的提醒橫幅（取代會卡住 UI 的 messagebox）
        self._banner_after_id: Optional[str] = None
        # 失聯橫幅只提醒一次，恢復後才重置——否則每 100ms 就會重跳一次
        self._comm_warned = False
        # 目前這則橫幅是何時開始顯示的（決定新訊息要排隊還是直接換掉）
        self._banner_shown_at = 0.0

        self._build_window()
        self._build_top_bar()
        self._build_banner()
        self._build_notebook()

        self.ctrl.load_points()
        self.ctrl.load_recordings_from_disk()
        self.ctrl.load_speed_profiles()
        # 先載入，連線時才知道要補回哪些被斷電清掉的設定
        self.ctrl.load_controller_config()
        self._cfg_status_var.set(self._cfg_summary())
        self._refresh_points()
        self._refresh_recordings()
        self._refresh_profiles()
        self._start_poller()          # UI 重繪（Tk 主執行緒）
        self._start_position_worker()  # 硬體位置刷新（背景執行緒）

        self.ctrl._log("INFO", "DS102  圖形化控制器啟動")

    # =========================================================================
    # 提醒橫幅（非強制，取代 modal messagebox）
    # =========================================================================
    def _build_banner(self):
        """
        建立一條可自動消失的提醒橫幅。

        限位是很常見的正常狀況（點動到底、教點超界），以前每次都跳 modal
        messagebox，使用者必須按確認、期間整個 UI 停擺。改用這條橫幅：
        看得到、不擋操作、幾秒後自己收起來。
        """
        # ⚠ 橫幅**常駐 pack**、高度固定，閒置時只是變成背景色。
        # 以前是有訊息才 pack、逾時 pack_forget，結果整個 notebook 會上下
        # 跳動約 30px——而觸發橫幅最頻繁的情境正是「長按點動撞到限位」，
        # 按鈕就在手指下方位移。Tk 的隱式指標抓取保證這次的 release 仍送到
        # 原 widget（放開即停不受影響），但**下一次點擊**很可能落在錯位的
        # 目標上，而點動按鈕旁邊就是停止鍵。
        self._banner = tk.Frame(self.root, bg=CLR_BG, height=30)
        self._banner.pack(fill="x", side="top")
        self._banner.pack_propagate(False)
        self._banner_var = tk.StringVar(value="")
        self._banner_lbl = tk.Label(
            self._banner,
            textvariable=self._banner_var,
            bg=CLR_BG,
            fg=CLR_BG,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
            justify="left",
        )
        self._banner_lbl.pack(side="left", padx=(12, 6), fill="both", expand=True)
        self._banner_close = tk.Button(
            self._banner,
            text="✕",
            bg=CLR_BG,
            fg=CLR_BG,
            relief="flat",
            cursor="hand2",
            font=("Segoe UI", 10, "bold"),
            command=self._hide_banner,
        )
        self._banner_close.pack(side="right", padx=(0, 10))
        # 待顯示的訊息佇列（見 _flash_banner）
        self._banner_queue: List[Tuple[str, int]] = []

    def _flash_banner(self, msg: str, ms: int = 8000):
        """
        顯示提醒，ms 毫秒後自動收起。

        短時間內來第二則訊息時**排隊依序顯示**，不會互相覆蓋。
        以前是直接覆寫同一個變數並重設倒數——連線成功時若同時有
        「已從設定檔還原」與「復歸樣式未設定」兩則，第一則會在顯示 0ms
        後被蓋掉，實質上永遠看不到。
        """
        try:
            if self._banner_after_id:
                # 只有「幾乎同時」湧入的訊息才排隊。這正是佇列要解決的情境：
                # 連線成功時「已從設定檔還原」與「復歸樣式未設定」在同一個
                # 事件裡連續觸發，舊寫法會讓第一則顯示 0ms 就被蓋掉。
                #
                # 但若目前這則已經顯示一段時間，新訊息多半是使用者剛按下
                # 某個按鈕的回饋——那不該排隊等好幾秒才出現，直接換掉。
                if time.time() - self._banner_shown_at < BANNER_COALESCE_SEC:
                    if (msg, ms) not in self._banner_queue:
                        self._banner_queue.append((msg, ms))
                    return
                self.root.after_cancel(self._banner_after_id)
                self._banner_after_id = None
            self._show_banner_now(msg, ms)
        except tk.TclError:
            pass  # 關閉流程中 widget 可能已銷毀

    def _show_banner_now(self, msg: str, ms: int):
        self._banner_shown_at = time.time()
        self._banner_var.set(msg)
        self._banner.config(bg=CLR_WARN)
        self._banner_lbl.config(bg=CLR_WARN, fg="white")
        self._banner_close.config(bg=CLR_WARN, fg="white")
        self._banner_after_id = self.root.after(ms, self._hide_banner)

    def _hide_banner(self):
        """收起目前訊息；佇列裡還有就接著顯示下一則。"""
        try:
            if self._banner_after_id:
                self.root.after_cancel(self._banner_after_id)
                self._banner_after_id = None
            if self._banner_queue:
                nxt_msg, nxt_ms = self._banner_queue.pop(0)
                self._show_banner_now(nxt_msg, nxt_ms)
                return
            # 不 pack_forget，只是變回背景色——版面高度永遠不變
            self._banner_var.set("")
            self._banner.config(bg=CLR_BG)
            self._banner_lbl.config(bg=CLR_BG, fg=CLR_BG)
            self._banner_close.config(bg=CLR_BG, fg=CLR_BG)
        except tk.TclError:
            pass

    # =========================================================================
    # 視窗骨架
    # =========================================================================
    def _build_window(self):
        self.root.title("DS102 / DS112 步進馬達控制器 ")
        self.root.configure(bg=CLR_BG)
        self.root.minsize(1020, 720)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Escape = 停止所有軸。機台旁操作時滑鼠不一定在手上，而停止鍵
        # 只存在於「移動控制」分頁。bind_all 讓它在任何分頁、任何焦點下都有效。
        # ⚠ 緊急停止**刻意不綁鍵盤**：誤觸後還要走解除流程並確認各軸位置，
        #   代價比多按一次滑鼠高。
        self.root.bind_all("<Escape>", self._on_escape)

        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=CLR_BG, foreground=CLR_TEXT, font=("Segoe UI", 10))
        s.configure("TFrame", background=CLR_BG)
        s.configure("TLabel", background=CLR_BG, foreground=CLR_TEXT)
        s.configure("TEntry", fieldbackground=CLR_CARD, foreground=CLR_TEXT)
        s.configure("TCombobox", fieldbackground=CLR_CARD, foreground=CLR_TEXT)
        s.configure("TNotebook", background=CLR_BG, borderwidth=0)
        s.configure("TNotebook.Tab", padding=[14, 7], font=("Segoe UI", 10))
        s.map(
            "TNotebook.Tab", background=[("selected", CLR_CARD), ("!selected", CLR_BG)]
        )
        for name, bg, fg, abg in [
            ("Accent", CLR_ACCENT, "white", "#138A5F"),
            ("Danger", CLR_DANGER, "white", "#B52C22"),
            ("Info", CLR_INFO, "white", "#1557B0"),
            ("Warn", CLR_WARN, "white", "#C88000"),
            ("Flat", CLR_BORDER, CLR_TEXT, "#CCCAC3"),
        ]:
            s.configure(
                f"{name}.TButton",
                background=bg,
                foreground=fg,
                font=("Segoe UI", 10, "bold"),
                padding=[10, 5],
            )
            s.map(f"{name}.TButton", background=[("active", abg)])
        s.configure(
            "Treeview",
            background=CLR_CARD,
            fieldbackground=CLR_CARD,
            foreground=CLR_TEXT,
            rowheight=26,
        )
        s.configure(
            "Treeview.Heading",
            background=CLR_BG,
            foreground=CLR_MUTED,
            font=("Segoe UI", 9),
        )

    def _build_top_bar(self):
        top = tk.Frame(
            self.root, bg=CLR_CARD, highlightbackground=CLR_BORDER, highlightthickness=1
        )
        top.pack(fill="x")

        left = tk.Frame(top, bg=CLR_CARD)
        left.pack(side="left", padx=16, pady=8)
        tk.Label(
            left,
            text="DS102 / DS112  馬達控制器",
            bg=CLR_CARD,
            fg=CLR_TEXT,
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor="w")
        self._fw_var = tk.StringVar(value="（未連線）")
        tk.Label(
            left,
            textvariable=self._fw_var,
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
        ).pack(anchor="w")

        right = tk.Frame(top, bg=CLR_CARD)
        right.pack(side="right", padx=16, pady=8)
        self._ems_btn = tk.Button(
            right,
            text="⛔  緊急停止",
            bg=CLR_DANGER,
            fg="white",
            font=("Segoe UI", 11, "bold"),
            relief="flat",
            padx=14,
            pady=6,
            cursor="hand2",
            command=self._toggle_ems,
        )
        self._ems_btn.pack(side="right", padx=(10, 0))

        self._home_btn = tk.Button(
            right,
            text="🏠  全軸原點復歸",
            bg=CLR_INFO,
            fg="white",
            font=("Segoe UI", 11, "bold"),
            relief="flat",
            padx=14,
            pady=6,
            cursor="hand2",
            state="disabled",
            command=self._do_home_all,
        )
        self._home_btn.pack(side="right", padx=(10, 0))
        self._drive_buttons.append(self._home_btn)

        conn_f = tk.Frame(right, bg=CLR_CARD)
        conn_f.pack(side="right")
        self._conn_dot = tk.Canvas(
            conn_f, width=10, height=10, bg=CLR_CARD, highlightthickness=0
        )
        self._conn_dot.pack(side="left", padx=(0, 4))
        self._conn_dot_id = self._conn_dot.create_oval(
            1, 1, 9, 9, fill=CLR_DANGER, outline=""
        )
        self._conn_lbl = tk.Label(
            conn_f, text="未連線", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 10)
        )
        self._conn_lbl.pack(side="left")

        cr = tk.Frame(top, bg=CLR_CARD)
        cr.pack(side="left", padx=20, pady=8)

        tk.Label(
            cr, text="Port:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).grid(row=0, column=0, sticky="e", padx=(0, 4))
        self._port_var = tk.StringVar()
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self._port_cb = ttk.Combobox(
            cr, textvariable=self._port_var, values=ports, width=12, state="readonly"
        )
        if ports:
            self._port_var.set(ports[0])
        self._port_cb.grid(row=0, column=1, padx=(0, 6))

        tk.Label(
            cr, text="Baud:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).grid(row=0, column=2, sticky="e", padx=(0, 4))
        self._baud_var = tk.StringVar(value="38400")
        ttk.Combobox(
            cr,
            textvariable=self._baud_var,
            values=["38400", "19200", "9600", "4800"],
            width=8,
            state="readonly",
        ).grid(row=0, column=3, padx=(0, 8))

        tk.Button(
            cr,
            text="↻",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            relief="flat",
            font=("Segoe UI", 11),
            cursor="hand2",
            command=self._scan_ports,
        ).grid(row=0, column=4, padx=(0, 4))
        self._conn_btn = tk.Button(
            cr,
            text="連線",
            bg=CLR_ACCENT,
            fg="white",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
            command=self._toggle_connect,
        )
        self._conn_btn.grid(row=0, column=5, padx=(0, 4))

    # =========================================================================
    # Notebook
    # =========================================================================
    def _build_notebook(self):
        self._nb = ttk.Notebook(self.root)
        self._nb.pack(fill="both", expand=True)
        for name, builder in [
            ("儀表板", self._build_tab_dashboard),
            ("移動控制", self._build_tab_control),
            ("Teaching", self._build_tab_points),
            ("行程錄製", self._build_tab_recording),
            ("LOG", self._build_tab_log),
        ]:
            f = ttk.Frame(self._nb)
            self._nb.add(f, text=f"  {name}  ")
            builder(f)

    # ── 通用 UI 元件 ──────────────────────────────────────────
    def _scrollable(self, parent) -> tk.Frame:
        canvas = tk.Canvas(parent, bg=CLR_BG, highlightthickness=0)
        scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        frame = tk.Frame(canvas, bg=CLR_BG)
        frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=frame, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        def _mw(ev):
            canvas.yview_scroll(int(-1 * (ev.delta / 120)), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _mw))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return frame

    def _card(self, parent, title="", pady=(6, 6)) -> tk.Frame:
        outer = tk.Frame(parent, bg=CLR_BG)
        outer.pack(fill="x", padx=12, pady=pady)
        inner = tk.Frame(
            outer, bg=CLR_CARD, highlightbackground=CLR_BORDER, highlightthickness=1
        )
        inner.pack(fill="x")
        if title:
            tk.Label(
                inner,
                text=title,
                bg=CLR_CARD,
                fg=CLR_MUTED,
                font=("Segoe UI", 8, "bold"),
            ).pack(anchor="w", padx=12, pady=(6, 0))
        return inner

    def _add_status_bar(self, parent) -> StatusBar:
        sb = StatusBar(parent, self.ctrl)
        sb.pack(side="bottom", fill="x")
        self._status_bars.append(sb)
        return sb

    # ── 軸選取 + 單位工具列 ───────────────────────────────────
    def _build_axis_selector(self, parent):
        row = tk.Frame(
            parent, bg=CLR_CARD, highlightbackground=CLR_BORDER, highlightthickness=1
        )
        row.pack(fill="x")
        tk.Label(
            row, text="軸選取:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left", padx=(12, 6), pady=6)

        cur_ax = next((a for a, n in AXIS_NO.items() if n == self.ctrl.axis_no), "X")
        axis_btns: Dict[str, tk.Button] = {}
        for ax in AXES:
            b = tk.Button(
                row,
                text=ax,
                width=3,
                relief="flat",
                font=("Segoe UI", 10, "bold"),
                cursor="hand2",
                bg=CLR_ACCENT if ax == cur_ax else CLR_BORDER,
                fg="white" if ax == cur_ax else CLR_TEXT,
                command=lambda a=ax, n=AXIS_NO[ax]: self._select_axis(a, n),
            )
            b.pack(side="left", padx=2, pady=4)
            axis_btns[ax] = b
        self._all_axis_btn_groups.append(axis_btns)

        ttk.Separator(row, orient="vertical").pack(
            side="left", fill="y", padx=10, pady=4
        )
        tk.Label(
            row, text="單位: pulse", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left", padx=(0, 6))

    def _select_axis(self, ax_name: str, ax_no: str):
        self.ctrl.axis_no = ax_no
        self._axis_no_var.set(ax_no)
        self._axis_name_var.set(ax_name)
        for grp in self._all_axis_btn_groups:
            for a, b in grp.items():
                b.config(
                    bg=CLR_ACCENT if a == ax_name else CLR_BORDER,
                    fg="white" if a == ax_name else CLR_TEXT,
                )
        self.ctrl._log("INFO", f"選取軸 {ax_name} ({ax_no})")
        self._async_query()
        # 復歸樣式是各軸自己的設定，換軸就要重讀，否則下拉會停在上一軸的值——
        # 按下原點返回時會把這一軸的樣式改成上一軸的。
        self._sync_org_mode()

    # =========================================================================
    # TAB：儀表板
    # =========================================================================
    def _build_tab_dashboard(self, parent):
        self._add_status_bar(parent)
        scr = self._scrollable(parent)

        # ── 系統狀態 ──
        stat_card = self._card(scr, "系統狀態")
        stat_row = tk.Frame(stat_card, bg=CLR_CARD)
        stat_row.pack(fill="x", padx=12, pady=8)
        self._stat_vars: Dict[str, tk.StringVar] = {}
        for i, (key, lbl, val) in enumerate(
            [
                ("conn", "連線狀態", "未連線"),
                ("axes", "可用軸數", "—"),
                ("ems", "EMS", "正常"),
                ("play", "重播狀態", "閒置"),
                ("dlog", "數據記錄", "停止"),
            ]
        ):
            cell = tk.Frame(stat_row, bg="#F0EEE8")
            cell.grid(row=0, column=i, padx=4, sticky="ew")
            stat_row.columnconfigure(i, weight=1)
            tk.Label(
                cell, text=lbl, bg="#F0EEE8", fg=CLR_MUTED, font=("Segoe UI", 8)
            ).pack(pady=(6, 0))
            v = tk.StringVar(value=val)
            self._stat_vars[key] = v
            tk.Label(
                cell,
                textvariable=v,
                bg="#F0EEE8",
                fg=CLR_TEXT,
                font=("Segoe UI", 12, "bold"),
            ).pack(pady=(0, 6))

        # ── 各軸位置（顯示工作座標） ──
        pos_card = self._card(scr, "各軸工作座標（已套用偏置）")
        pos_grid = tk.Frame(pos_card, bg=CLR_CARD)
        pos_grid.pack(fill="x", padx=12, pady=8)
        self._dash_pos_vars: Dict[str, tk.StringVar] = {}
        self._dash_status_vars: Dict[str, tk.StringVar] = {}
        self._dash_pos_labels: Dict[str, tk.Label] = {}
        for i, ax in enumerate(AXES):
            col = i % 3
            row = i // 3
            cell = tk.Frame(pos_grid, bg="#F0EEE8")
            cell.grid(row=row, column=col, padx=6, pady=4, sticky="ew")
            pos_grid.columnconfigure(col, weight=1)
            tk.Label(
                cell,
                text=f"{ax} 軸",
                bg="#F0EEE8",
                fg=CLR_MUTED,
                font=("Segoe UI", 9, "bold"),
            ).pack(anchor="w", padx=8, pady=(6, 0))
            # 初值「—」而非 0：0 幾乎就落在限位開關上，未連線就顯示 0
            # 等於畫一組「所有軸都壓在端點」的假座標
            pv = tk.StringVar(value="—")
            self._dash_pos_vars[ax] = pv
            pl = tk.Label(
                cell,
                textvariable=pv,
                bg="#F0EEE8",
                fg=CLR_MUTED,
                font=("Consolas", 18, "bold"),
            )
            pl.pack(anchor="w", padx=8)
            self._dash_pos_labels[ax] = pl
            sv = tk.StringVar(value="未連線")
            self._dash_status_vars[ax] = sv
            tk.Label(
                cell, textvariable=sv, bg="#F0EEE8", fg=CLR_MUTED, font=("Segoe UI", 8)
            ).pack(anchor="w", padx=8, pady=(0, 2))
            # 工作原點設定
            btn_f = tk.Frame(cell, bg="#F0EEE8")
            btn_f.pack(fill="x", padx=8, pady=(0, 6))
            tk.Button(
                btn_f,
                text="此處設工作原點",
                bg="#E8F5E9",
                fg="#2E7D32",
                font=("Segoe UI", 8),
                relief="flat",
                cursor="hand2",
                command=lambda a=AXIS_NO[ax]: self.ctrl.set_offset_here(a),
            ).pack(side="left", padx=(0, 4))
            tk.Button(
                btn_f,
                text="清除偏置",
                bg=CLR_BORDER,
                fg=CLR_TEXT,
                font=("Segoe UI", 8),
                relief="flat",
                cursor="hand2",
                command=lambda a=AXIS_NO[ax]: self.ctrl.clear_offset(a),
            ).pack(side="left")

        # ── 實驗數據記錄（屬於「觀測」，留在儀表板）──
        self._build_card_datalog(scr)

    # =========================================================================
    # 可搬移的卡片（抽成獨立方法，讓分頁配置能單獨調整）
    # =========================================================================
    def _build_card_speed(self, scr):
        """
        速度設定 + Profile。

        放在**移動控制**分頁、緊接驅動按鈕下方：調速是靠感覺反覆試出來的，
        以前這張卡在儀表板，等於「改速度 → 切分頁 → 點動 → 再切回去改」，
        每次調整都要來回切兩次分頁。
        """
        spd_card = self._card(scr, "速度設定")
        spd_f = tk.Frame(spd_card, bg=CLR_CARD)
        spd_f.pack(fill="x", padx=12, pady=8)
        self._spd_vars: Dict[str, tk.StringVar] = {}
        for r, (key, lbl, default, unit) in enumerate(
            [
                ("l_speed", "Start-up Speed (L)", "100", "pps"),
                ("rate", "Accel/Decel Rate (R)", "100", "ms"),
                ("s_rate", "S-curve Rate (S)", "100", "%"),
                ("f_speed", "Driving Speed (F)", "1000", "pps"),
            ]
        ):
            tk.Label(
                spd_f,
                text=lbl,
                bg=CLR_CARD,
                fg=CLR_MUTED,
                font=("Segoe UI", 9),
                width=22,
                anchor="w",
            ).grid(row=r, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=default)
            self._spd_vars[key] = v
            ttk.Entry(spd_f, textvariable=v, width=12).grid(row=r, column=1, padx=8)
            tk.Label(
                spd_f, text=unit, bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
            ).grid(row=r, column=2, sticky="w")

        # Profile 快速切換
        prof_row = tk.Frame(spd_card, bg=CLR_CARD)
        prof_row.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(
            prof_row, text="Profile:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left")
        self._profile_var = tk.StringVar()
        self._profile_cb = ttk.Combobox(
            prof_row, textvariable=self._profile_var, width=16, state="readonly"
        )
        self._profile_cb.pack(side="left", padx=6)
        self._profile_cb.bind("<<ComboboxSelected>>", self._load_profile)
        ttk.Button(
            prof_row, text="載入", style="Flat.TButton", command=self._load_profile
        ).pack(side="left", padx=2)
        ttk.Button(
            prof_row, text="儲存目前設定", command=self._save_profile_dialog
        ).pack(side="left", padx=6)
        ttk.Button(
            prof_row, text="刪除", style="Danger.TButton", command=self._delete_profile
        ).pack(side="left", padx=2)

    def _build_card_sw_limits(self, scr):
        """程式端軟體行程限制。屬於「連線後設定一次」，放分頁底部。"""
        lim_card = self._card(scr, "軟體行程限制（程式端，非控制器韌體）")
        lim_note = tk.Label(
            lim_card,
            text="單位：pulse。留空＝不限制。這是**程式端**的攔截，"
                 "與下方「控制器設定」裡的韌體軟體限位（CWSLE）是兩套、互不同步。\n"
                 "程式端只在送出指令前用快取座標算一次；韌體端才是實時的。",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 8),
            justify="left",
        )
        lim_note.pack(anchor="w", padx=12, pady=(2, 4))
        lim_grid = tk.Frame(lim_card, bg=CLR_CARD)
        lim_grid.pack(fill="x", padx=12, pady=(0, 8))
        self._lim_vars: Dict[str, Tuple[tk.StringVar, tk.StringVar]] = {}
        tk.Label(
            lim_grid,
            text="軸",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
            width=4,
        ).grid(row=0, column=0)
        tk.Label(
            lim_grid,
            text="CCW 限制",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
            width=14,
        ).grid(row=0, column=1)
        tk.Label(
            lim_grid,
            text="CW 限制",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
            width=14,
        ).grid(row=0, column=2)
        # 「目前生效」欄：輸入框留白有兩種無法分辨的含意——「這一軸沒設限制」
        # 與「有設，只是沒顯示」。把 ctrl.sw_limits 的實際內容攤出來。
        tk.Label(
            lim_grid,
            text="目前生效",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
            width=22,
        ).grid(row=0, column=3)
        self._lim_cur_vars: Dict[str, tk.StringVar] = {}
        for r, ax in enumerate(AXES, start=1):
            tk.Label(
                lim_grid,
                text=ax,
                bg=CLR_CARD,
                fg=CLR_TEXT,
                font=("Segoe UI", 9, "bold"),
                width=4,
            ).grid(row=r, column=0, pady=2)
            ccw_v = tk.StringVar(value="")
            cw_v = tk.StringVar(value="")
            self._lim_vars[ax] = (ccw_v, cw_v)
            ttk.Entry(lim_grid, textvariable=ccw_v, width=14).grid(
                row=r, column=1, padx=4, pady=2
            )
            ttk.Entry(lim_grid, textvariable=cw_v, width=14).grid(
                row=r, column=2, padx=4, pady=2
            )
            cur_v = tk.StringVar(value="無限制")
            self._lim_cur_vars[ax] = cur_v
            tk.Label(
                lim_grid,
                textvariable=cur_v,
                bg=CLR_CARD,
                fg=CLR_MUTED,
                font=("Consolas", 8),
                width=22,
                anchor="w",
            ).grid(row=r, column=3, padx=4, pady=2)
        ttk.Button(
            lim_card,
            text="套用限制設定",
            style="Accent.TButton",
            command=self._apply_sw_limits,
        ).pack(padx=12, pady=(0, 8))

    def _build_card_controller_cfg(self, scr):
        """控制器設定的存檔／還原。同樣是「連線後設定一次」的東西。"""
        cfg_card = self._card(scr, "控制器設定（MEMSW0 復歸樣式 + 韌體軟體限位）")
        tk.Label(
            cfg_card,
            text="這些設定存在控制器的 RAM，斷電後會全部回到出廠值。\n"
                 "存檔後，往後每次連線都會自動補回被清空的項目。",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
            justify="left",
        ).pack(anchor="w", padx=12, pady=(4, 2))
        self._cfg_status_var = tk.StringVar(value="—")
        tk.Label(
            cfg_card,
            textvariable=self._cfg_status_var,
            bg=CLR_CARD,
            fg=CLR_TEXT,
            font=("Segoe UI", 9),
            justify="left",
            wraplength=520,
        ).pack(anchor="w", padx=12, pady=(0, 4))
        cfg_btns = tk.Frame(cfg_card, bg=CLR_CARD)
        cfg_btns.pack(anchor="w", padx=12, pady=(0, 8))
        ttk.Button(
            cfg_btns,
            text="儲存控制器設定",
            style="Accent.TButton",
            command=self._save_controller_config,
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            cfg_btns,
            text="立即還原",
            style="Flat.TButton",
            command=self._restore_controller_config,
        ).pack(side="left")

    def _build_card_datalog(self, scr):
        """實驗數據記錄（CSV）。屬於「觀測」，留在儀表板。"""
        data_card = self._card(scr, "實驗數據記錄（CSV）")
        data_row = tk.Frame(data_card, bg=CLR_CARD)
        data_row.pack(fill="x", padx=12, pady=8)
        self._dlog_status_var = tk.StringVar(value="停止")
        tk.Label(
            data_row,
            textvariable=self._dlog_status_var,
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 12))
        self._dlog_start_btn = ttk.Button(
            data_row,
            text="▶ 開始記錄",
            style="Accent.TButton",
            command=self._start_data_log,
        )
        self._dlog_start_btn.pack(side="left", padx=4)
        self._dlog_stop_btn = ttk.Button(
            data_row, text="■ 停止並匯出", command=self._stop_data_log, state="disabled"
        )
        self._dlog_stop_btn.pack(side="left", padx=4)

    # =========================================================================
    # TAB：移動控制
    # =========================================================================
    def _build_tab_control(self, parent):
        self._add_status_bar(parent)
        self._build_axis_selector(parent)
        scr = self._scrollable(parent)

        # ── 驅動模式 ──
        mode_card = self._card(scr, "驅動模式")
        mode_f = tk.Frame(mode_card, bg=CLR_CARD)
        mode_f.pack(fill="x", padx=12, pady=8)
        self._step_dist_var = tk.StringVar(value="1000")
        # 留空，等連線後由 _sync_org_mode() 從控制器讀當前軸的實際樣式填入。
        # 不給預設值是刻意的：任何寫死的預設都可能覆蓋掉該軸真正的復歸樣式。
        self._org_mode_var = tk.StringVar(value="")
        for mode_val, mode_lbl in [
            (MODE_CONTINUE, "連續點動 (Continue)"),
            (MODE_STEP, "步進 (Step)"),
            (MODE_ORIGIN, "原點返回 (Origin)"),
        ]:
            rf = tk.Frame(mode_f, bg=CLR_CARD)
            rf.pack(fill="x", pady=2)
            tk.Radiobutton(
                rf,
                text=mode_lbl,
                variable=self._drive_mode_var,
                value=mode_val,
                bg=CLR_CARD,
                fg=CLR_TEXT,
                font=("Segoe UI", 10),
                activebackground=CLR_CARD,
                command=self._on_mode_change,
                width=22,
                anchor="w",
            ).pack(side="left")
            if mode_val == MODE_STEP:
                ttk.Entry(rf, textvariable=self._step_dist_var, width=12).pack(
                    side="left", padx=4
                )
                tk.Label(
                    rf,
                    text="pulse",
                    bg=CLR_CARD,
                    fg=CLR_MUTED,
                    font=("Segoe UI", 8),
                ).pack(side="left")
            elif mode_val == MODE_ORIGIN:
                ttk.Combobox(
                    rf,
                    textvariable=self._org_mode_var,
                    values=ORG_MODES,
                    width=10,
                    state="readonly",
                ).pack(side="left", padx=4)

        # ── 驅動按鈕 ──
        drv_card = self._card(scr, "驅動（長按=連續點動，點擊=步進/原點）")
        drv_f = tk.Frame(drv_card, bg=CLR_CARD)
        drv_f.pack(pady=12)

        # 當前軸 + 狀態顯示
        info_row = tk.Frame(drv_f, bg=CLR_CARD)
        info_row.pack(pady=(0, 6))
        tk.Label(
            info_row, text="軸:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left")
        # 顯示軸名（X/Y/Z…）而非軸號（1/2/3）——軸選擇器上按的是 X，
        # 這裡卻寫 1，同一個畫面兩套命名，而「選錯軸就是驅動錯的滑台」
        tk.Label(
            info_row,
            textvariable=self._axis_name_var,
            bg=CLR_CARD,
            fg=CLR_ACCENT,
            font=("Segoe UI", 14, "bold"),
        ).pack(side="left", padx=(4, 16))
        tk.Label(
            info_row, text="狀態:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left")
        tk.Label(
            info_row,
            textvariable=self._ctrl_status_var,
            bg=CLR_CARD,
            fg=CLR_TEXT,
            font=("Segoe UI", 11, "bold"),
        ).pack(side="left", padx=4)

        # 位置設定
        pos_row = tk.Frame(drv_f, bg=CLR_CARD)
        pos_row.pack(pady=(0, 10))
        tk.Label(
            pos_row, text="Position:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left")
        # ⚠ 唯讀顯示，與下方的輸入框分開。
        # 以前兩者共用 _ctrl_pos_var：輪詢每 100ms 覆寫它，使用者打到一半的
        # 數字會被清掉；反過來看，欄位裡的數字像是「即將設定的值」，實際是
        # 「剛讀回來的值」，兩種語意疊在同一個 widget 上。
        tk.Label(
            pos_row,
            textvariable=self._ctrl_pos_var,
            bg=CLR_CARD,
            fg=CLR_TEXT,
            font=("Consolas", 11, "bold"),
            width=12,
            anchor="e",
        ).pack(side="left", padx=6)
        tk.Label(
            pos_row, text="pulse", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8)
        ).pack(side="left", padx=(0, 16))

        # 改寫座標暫存器（不產生任何移動）——預設留空，不可預填 0
        tk.Label(
            pos_row, text="改寫為:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left")
        self._setpos_var = tk.StringVar(value="")
        ttk.Entry(pos_row, textvariable=self._setpos_var, width=10).pack(
            side="left", padx=6
        )
        ttk.Button(
            pos_row, text="設為此值", style="Flat.TButton",
            command=self._do_set_position,
        ).pack(side="left", padx=4)

        # CCW / Stop / CW
        btn_row = tk.Frame(drv_f, bg=CLR_CARD)
        btn_row.pack()
        self._ccw_btn = tk.Button(
            btn_row,
            text="−  CCW",
            width=12,
            height=3,
            bg=CLR_INFO,
            fg="white",
            font=("Segoe UI", 12, "bold"),
            relief="flat",
            cursor="hand2",
            state="disabled",
        )
        self._ccw_btn.bind("<ButtonPress-1>", self._on_ccw_press)
        self._ccw_btn.bind("<ButtonRelease-1>", self._on_ccw_release)
        self._ccw_btn.pack(side="left", padx=12)

        stop_btn = tk.Button(
            btn_row,
            text="■  Stop",
            width=10,
            height=3,
            bg=CLR_DANGER,
            fg="white",
            font=("Segoe UI", 11, "bold"),
            relief="flat",
            cursor="hand2",
            state="disabled",
            command=self._do_stop,
        )
        stop_btn.pack(side="left", padx=4)
        # 停止鍵**不可**放進 _drive_buttons——那串會被
        # _set_drive_buttons_state("disabled") 整批關掉，時機包含重播中與
        # 全軸復歸中，也就是滑台正在動、最需要停止的時候。
        # 它只受「有沒有連線」影響，其餘一律保持可按。
        self._stop_btn = stop_btn

        self._cw_btn = tk.Button(
            btn_row,
            text="CW  ＋",
            width=12,
            height=3,
            bg=CLR_ACCENT,
            fg="white",
            font=("Segoe UI", 12, "bold"),
            relief="flat",
            cursor="hand2",
            state="disabled",
        )
        self._cw_btn.bind("<ButtonPress-1>", self._on_cw_press)
        self._cw_btn.bind("<ButtonRelease-1>", self._on_cw_release)
        self._cw_btn.pack(side="left", padx=12)

        # 只有「會發起移動」的按鈕才進這串（停止鍵見上方註解）
        self._drive_buttons.extend([self._ccw_btn, self._cw_btn])
        tk.Label(
            drv_card,
            text="連線後方可使用；EMS 或重播中驅動按鈕自動鎖定（停止鍵不受此限）"
                 "　·　Esc = 停止所有軸",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 8),
        ).pack(pady=(0, 8))

        # ── 速度設定緊接在驅動按鈕下方 ──
        # 調速是靠感覺反覆試出來的，兩者必須在同一個視野內。
        # 以前速度設定在儀表板，每次調整都要來回切兩次分頁。
        self._build_card_speed(scr)

        # ── 以下是「連線後設定一次」的東西，用分隔線與日常操作區隔 ──
        ttk.Separator(scr, orient="horizontal").pack(fill="x", padx=12, pady=(14, 6))
        tk.Label(
            scr,
            text="以下為連線後設定一次即可的項目",
            bg=CLR_BG,
            fg=CLR_MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=14)
        self._build_card_sw_limits(scr)
        self._build_card_controller_cfg(scr)

    # ── 模式與驅動事件 ────────────────────────────────────────
    def _on_mode_change(self):
        mode = self._drive_mode_var.get()
        if mode == MODE_ORIGIN:
            self._ccw_btn.config(text="Origin")
            self._cw_btn.config(text="Origin")
        else:
            self._ccw_btn.config(text="−  CCW")
            self._cw_btn.config(text="CW  ＋")

    def _get_spd(self):
        return (
            self._spd_vars["l_speed"].get(),
            self._spd_vars["f_speed"].get(),
            self._spd_vars["rate"].get(),
            self._spd_vars["s_rate"].get(),
        )

    def _on_ccw_press(self, event):
        if (
            not self.ctrl.connected
            or self.ctrl.ems_active
            or self.ctrl.playback_running
        ):
            return
        mode = self._drive_mode_var.get()
        ax = self.ctrl.axis_no
        l, f, r, s = self._get_spd()
        if mode == MODE_CONTINUE:
            self.ctrl.move_continue(ax, "CCW", l, f, r, s)
            self._poll_status()
        elif mode == MODE_STEP:
            threading.Thread(
                target=self.ctrl.move_step,
                args=(ax, "CCW", self._step_dist_var.get(), l, f, r, s, True),
                daemon=True,
            ).start()
            self._poll_status()
        elif mode == MODE_ORIGIN:
            self._do_origin_move(ax, (l, f, r, s))

    def _on_ccw_release(self, event):
        if self._drive_mode_var.get() == MODE_CONTINUE:
            self.ctrl.stop()

    def _on_cw_press(self, event):
        if (
            not self.ctrl.connected
            or self.ctrl.ems_active
            or self.ctrl.playback_running
        ):
            return
        mode = self._drive_mode_var.get()
        ax = self.ctrl.axis_no
        l, f, r, s = self._get_spd()
        if mode == MODE_CONTINUE:
            self.ctrl.move_continue(ax, "CW", l, f, r, s)
            self._poll_status()
        elif mode == MODE_STEP:
            threading.Thread(
                target=self.ctrl.move_step,
                args=(ax, "CW", self._step_dist_var.get(), l, f, r, s, True),
                daemon=True,
            ).start()
            self._poll_status()
        elif mode == MODE_ORIGIN:
            self._do_origin_move(ax, (l, f, r, s))

    def _on_cw_release(self, event):
        if self._drive_mode_var.get() == MODE_CONTINUE:
            self.ctrl.stop()

    def _do_stop(self):
        self.ctrl.stop()

    def _on_escape(self, event=None):
        """Escape：停止所有軸，並中止進行中的重播。"""
        if not self.ctrl.connected:
            return
        self.ctrl.stop()
        if self.ctrl.playback_running:
            self._stop_playback.set()
        self._flash_banner("■ 已送出停止指令（Esc）", 4000)

    def _do_set_position(self):
        """
        改寫座標暫存器（`AXI{n}:POS {val}`）——不會產生任何移動。

        值得一次確認：這會讓後續所有軟體限位比對與 teaching point 的基準
        全部跟著偏移，而且沒有任何實體動作可以提示使用者「剛剛發生了什麼」。
        """
        if not self.ctrl.connected:
            self._flash_banner("尚未連線，無法設定座標", 4000)
            return
        raw = self._setpos_var.get().strip()
        if not raw:
            self._flash_banner("請先在「改寫為」欄位輸入數值", 4000)
            return
        try:
            val = float(raw)
        except ValueError:
            messagebox.showerror("錯誤", f"「{raw}」不是有效數值（只填數字，不要單位）")
            return

        ax = NO_AXIS.get(self.ctrl.axis_no, self.ctrl.axis_no)
        cur = self._ctrl_pos_var.get()
        if not messagebox.askyesno(
            "確認改寫座標",
            f"將軸 {ax} 的座標暫存器\n\n"
            f"　　由 {cur} 改寫為 {val:.0f}\n\n"
            f"這**不會產生任何移動**，但之後的軟體限位比對與\n"
            f"Teaching Point 都會以新座標為基準。確定？",
            icon="warning",
            default="no",
        ):
            return
        self.ctrl.set_position(self.ctrl.axis_no, f"{val:.0f}")
        self._setpos_var.set("")
        self._flash_banner(f"軸 {ax} 座標已改寫為 {val:.0f}", 5000)

    def _poll_status(self):
        """
        非同步輪詢狀態直到停止。
        整個迴圈都必須留在背景執行緒：改用 time.sleep 而非 root.after 排下一輪，
        否則第 2 輪起會被排回 Tk 主執行緒執行阻塞式序列查詢，UI 直接凍結。
        只有 UI 更新才 marshal 回主執行緒。
        """
        if self._poll_busy.is_set():
            return  # 已有輪詢在跑，不要疊第二條上去搶序列埠
        self._poll_busy.set()

        def _check():
            try:
                while True:
                    status, pos = self.ctrl.query_status(self.ctrl.axis_no)
                    self.root.after(0, lambda s=status: self._ctrl_status_var.set(s))
                    if pos:
                        self.root.after(0, lambda p=pos: self._ctrl_pos_var.set(p))
                    if status != "Driving":
                        return
                    time.sleep(0.1)
            finally:
                self._poll_busy.clear()

        threading.Thread(target=_check, daemon=True).start()

    def _async_query(self):
        def _q():
            status, pos = self.ctrl.query_status(self.ctrl.axis_no)
            self.root.after(0, lambda: self._ctrl_status_var.set(status))
            if pos:
                self.root.after(0, lambda: self._ctrl_pos_var.set(pos))

        threading.Thread(target=_q, daemon=True).start()

    # =========================================================================
    # TAB：Teaching Points
    # =========================================================================
    def _build_tab_points(self, parent):
        self._add_status_bar(parent)
        self._build_axis_selector(parent)
        scr = self._scrollable(parent)

        add_card = self._card(scr, "新增 / 編輯 Teaching Point")
        af = tk.Frame(add_card, bg=CLR_CARD)
        af.pack(fill="x", padx=12, pady=8)

        tk.Label(
            af, text="點名稱:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).grid(row=0, column=0, sticky="w", pady=3)
        self._pt_name_var = tk.StringVar()
        ttk.Entry(af, textvariable=self._pt_name_var, width=20).grid(
            row=0, column=1, sticky="w", padx=8
        )

        self._pt_pos_vars: Dict[str, tk.StringVar] = {}
        tk.Label(
            af,
            text="座標（pulse，−99999999~99999999）。留空 = 該軸不動。",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 8),
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 2))
        vcmd = (self.root.register(self._validate_coord), "%P")
        for i, ax in enumerate(AXES):
            r_base, col_base = 2 + i // 3, (i % 3) * 2
            tk.Label(
                af,
                text=f"{ax}:",
                bg=CLR_CARD,
                fg=CLR_MUTED,
                font=("Segoe UI", 9),
                width=3,
                anchor="e",
            ).grid(row=r_base, column=col_base, sticky="e", padx=(8, 2), pady=2)
            # 預設留空 = 「這一軸不動」。若預設 "0"，使用者只填 X 也會連帶把
            # Y/Z 命令到座標 0——原點復歸後 0 就在限位開關上，等於撞端點。
            v = tk.StringVar(value="")
            self._pt_pos_vars[ax] = v
            ttk.Entry(
                af, textvariable=v, width=14, validate="key", validatecommand=vcmd
            ).grid(row=r_base, column=col_base + 1, sticky="w", padx=4, pady=2)

        pbtn = tk.Frame(add_card, bg=CLR_CARD)
        pbtn.pack(padx=12, pady=(0, 8))
        ttk.Button(
            pbtn,
            text="儲存 Teaching Point",
            style="Accent.TButton",
            command=self._do_save_point,
        ).pack(side="left", padx=4)
        ttk.Button(
            pbtn,
            text="填入當前位置",
            style="Info.TButton",
            command=self._do_fill_current,
        ).pack(side="left", padx=4)
        ttk.Button(
            pbtn,
            text="清除",
            style="Flat.TButton",
            command=lambda: [v.set("") for v in self._pt_pos_vars.values()],
        ).pack(side="left", padx=4)

        list_card = self._card(scr, "已儲存 Teaching Points")
        self._pts_tree = ttk.Treeview(
            list_card,
            columns=("name", "X", "Y", "Z", "ts"),
            show="headings",
            height=8,
        )
        for col, w, lbl in [
            ("name", 120, "名稱"),
            ("X", 90, "X (pulse)"),
            ("Y", 90, "Y (pulse)"),
            ("Z", 90, "Z (pulse)"),
            ("ts", 160, "時間"),
        ]:
            self._pts_tree.heading(col, text=lbl)
            self._pts_tree.column(col, width=w)
        self._pts_tree.bind("<Double-1>", lambda e: self._do_load_point())
        sy = ttk.Scrollbar(list_card, orient="vertical", command=self._pts_tree.yview)
        self._pts_tree.configure(yscrollcommand=sy.set)
        self._pts_tree.pack(side="left", fill="x", expand=True, padx=12, pady=8)
        sy.pack(side="right", fill="y", pady=8)

        ptbtn = tk.Frame(list_card, bg=CLR_CARD)
        ptbtn.pack(padx=12, pady=(0, 8))
        ttk.Button(
            ptbtn, text="✏ 載入編輯", style="Flat.TButton", command=self._do_load_point
        ).pack(side="left", padx=4)
        ttk.Button(
            ptbtn,
            text="▶ 移動至此點",
            style="Accent.TButton",
            command=self._do_goto_point,
        ).pack(side="left", padx=4)
        ttk.Button(
            ptbtn, text="🗑 刪除", style="Danger.TButton", command=self._do_delete_point
        ).pack(side="left", padx=4)

    def _validate_coord(self, value: str) -> bool:
        """座標輸入檢核：pulse 為整數，範圍依手冊 ±99999999。"""
        if value in ("", "-", "+"):
            return True
        try:
            return -99999999 <= int(value) <= 99999999
        except ValueError:
            return False

    def _do_save_point(self):
        name = self._pt_name_var.get().strip()
        if not name:
            messagebox.showerror("錯誤", "請輸入點名稱")
            return
        # 只收有填值的軸；留空代表「goto 時不動這一軸」。
        positions = {}
        for ax, var in self._pt_pos_vars.items():
            raw = var.get().strip()
            if raw in ("", "-", ".", "-.", "+"):
                continue
            try:
                positions[ax] = float(raw)
            except ValueError:
                continue
        if not positions:
            messagebox.showerror("錯誤", "請至少填入一個軸的座標")
            return
        self.ctrl.save_point(name, positions)
        self._refresh_points()

    def _do_fill_current(self):
        # 用公開的 positions（已扣除 offset）而非機械座標 _positions_pulse，
        # 否則設過 offset 後填入的數字會與畫面上顯示的座標對不起來
        pos_work = self.ctrl.positions
        for ax, var in self._pt_pos_vars.items():
            var.set(f"{pos_work.get(ax, 0.0):.0f}")

    def _do_load_point(self):
        sel = self._pts_tree.selection()
        if not sel:
            return
        name = sel[0]  # iid 即點名稱，不經 Treeview 的型別轉換
        pt = self.ctrl.saved_points.get(name, {})
        self._pt_name_var.set(name)
        pos_pulse = pt.get("positions_pulse", {})
        for ax, var in self._pt_pos_vars.items():
            if ax not in pos_pulse:
                var.set("")  # 該軸未納入此點 → 保持留空
                continue
            var.set(f"{pos_pulse[ax]:.0f}")

    def _do_goto_point(self):
        sel = self._pts_tree.selection()
        if not sel:
            messagebox.showwarning("警告", "請先選取 Teaching Point")
            return
        name = sel[0]
        l, f, r, s = self._get_spd()
        threading.Thread(
            target=self.ctrl.goto_point, args=(name, l, f, r, s, True), daemon=True
        ).start()
        self._poll_status()

    def _do_delete_point(self):
        """
        刪除 Teaching Point。

        確認視窗比照「刪除行程」的規格：列出內容、`icon="warning"`、
        `default="no"`。以前只有一句「確定刪除 [name]？」且沒有 default，
        Tk 的預設焦點在「是」——而教點是操作者一個一個教出來的座標，
        誤刪只有整檔 .bak 可救。
        """
        sel = self._pts_tree.selection()
        if not sel:
            self._flash_banner("請先選取要刪除的 Teaching Point", 4000)
            return
        name = sel[0]  # iid 即點名稱
        pt = self.ctrl.saved_points.get(name, {})
        pos = pt.get("positions_pulse", {})
        detail = "、".join(f"{a}={v:,.0f}" for a, v in pos.items()) or "（無座標）"
        if not messagebox.askyesno(
            "確認刪除",
            f"確定刪除 Teaching Point [{name}]？\n\n"
            f"座標：{detail}\n"
            f"建立：{pt.get('ts', '?')}\n\n"
            f"刪除後只能從 teaching_points.json.bak 手動救回。",
            icon="warning",
            default="no",
        ):
            return
        self.ctrl.delete_point(name)
        self._refresh_points()
        self._flash_banner(f"🗑 Teaching Point [{name}] 已刪除", 5000)

    def _refresh_points(self):
        self._pts_tree.delete(*self._pts_tree.get_children())
        for name, data in self.ctrl.saved_points.items():
            pos_p = data.get("positions_pulse", {})

            def _fmt(ax, pp=pos_p):
                if ax not in pp:
                    return "—"  # 此點未含該軸 → goto 時不動
                return f"{pp[ax]:.0f}"

            # iid 直接用點名稱：Treeview 會把看起來像數字的儲存格值轉成 int
            # （"123"→123、"007"→7），從 values 讀回來的名稱對不上 saved_points 的
            # 字串 key。iid 不會被轉型，是唯一可靠的取名方式。
            self._pts_tree.insert(
                "",
                "end",
                iid=name,
                values=(
                    name,
                    _fmt("X"),
                    _fmt("Y"),
                    _fmt("Z"),
                    data.get("ts", "")[:19],
                ),
            )

    # =========================================================================
    # TAB：行程錄製
    # =========================================================================
    def _build_tab_recording(self, parent):
        """
        行程錄製分頁。

        卡片順序刻意依「實際操作動線」排，而不是依功能分類：

            行程錄製      ← 錄一段新的
            已儲存行程    ← 選一個既有的（這是下面兩張卡的資料來源）
            步驟明細      ← 顯示上面選取那一筆的內容，可改延遲
            重播設定      ← 跑它

        以前「步驟明細」排在「已儲存行程」**上面**，但它的內容是由下方
        清單的選取事件填進去的——資料來源在消費者下方。要編輯既有行程的
        延遲得先往下捲選行程、再往上捲看步驟、再往下捲按重播。
        """
        self._add_status_bar(parent)
        scr = self._scrollable(parent)

        self._build_card_record(scr)
        self._build_card_rec_list(scr)
        self._build_card_steps(scr)
        self._build_card_playback(scr)

    def _build_card_record(self, scr):
        rec_card = self._card(scr, "行程錄製")
        rf = tk.Frame(rec_card, bg=CLR_CARD)
        rf.pack(fill="x", padx=12, pady=8)
        tk.Label(
            rf, text="行程名稱:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).grid(row=0, column=0, sticky="w")
        self._rec_name_var = tk.StringVar()
        self._rec_status_var = tk.StringVar(value="● 閒置")
        self._rec_count_var = tk.StringVar(value="0 步")
        ttk.Entry(rf, textvariable=self._rec_name_var, width=24).grid(
            row=0, column=1, sticky="w", padx=8
        )
        tk.Label(
            rf,
            textvariable=self._rec_status_var,
            bg=CLR_CARD,
            fg=CLR_DANGER,
            font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=2, padx=6)
        tk.Label(
            rf,
            textvariable=self._rec_count_var,
            bg=CLR_CARD,
            fg=CLR_TEXT,
            font=("Segoe UI", 9),
        ).grid(row=0, column=3)

        rbtn = tk.Frame(rec_card, bg=CLR_CARD)
        rbtn.pack(padx=12, pady=(0, 8))
        self._rec_start_btn = ttk.Button(
            rbtn, text="⏺ 開始錄製", style="Danger.TButton", command=self._do_start_rec
        )
        self._rec_start_btn.pack(side="left", padx=4)
        self._rec_stop_btn = ttk.Button(
            rbtn, text="⏹ 停止錄製", command=self._do_stop_rec, state="disabled"
        )
        self._rec_stop_btn.pack(side="left", padx=4)

    def _build_card_steps(self, scr):
        steps_card = self._card(
            scr, "步驟明細（顯示上方選取的行程；雙擊延遲欄可修改並存檔）"
        )
        tf = tk.Frame(steps_card, bg=CLR_CARD)
        tf.pack(fill="x", padx=12, pady=6)
        self._steps_tree = ttk.Treeview(
            tf, columns=("idx", "ts", "cmd", "delay_ms"), show="headings", height=8
        )
        for col, w, lbl in [
            ("idx", 40, "#"),
            ("ts", 100, "時間"),
            ("cmd", 300, "指令"),
            ("delay_ms", 90, "延遲(ms)"),
        ]:
            self._steps_tree.heading(col, text=lbl)
            self._steps_tree.column(col, width=w)
        self._steps_tree.bind("<Double-1>", self._on_step_dclick)
        sy2 = ttk.Scrollbar(tf, orient="vertical", command=self._steps_tree.yview)
        self._steps_tree.configure(yscrollcommand=sy2.set)
        self._steps_tree.pack(side="left", fill="x", expand=True)
        sy2.pack(side="right", fill="y")

        gd = tk.Frame(steps_card, bg=CLR_CARD)
        gd.pack(padx=12, pady=(0, 8))
        tk.Label(
            gd,
            text="批次設定所有步驟延遲:",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left")
        self._global_delay_var = tk.StringVar(value="200")
        ttk.Entry(gd, textvariable=self._global_delay_var, width=8).pack(
            side="left", padx=6
        )
        tk.Label(gd, text="ms", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)).pack(
            side="left"
        )
        ttk.Button(
            gd, text="套用", style="Flat.TButton", command=self._apply_global_delay
        ).pack(side="left", padx=6)
        # 與下方「每輪之間間隔」外觀太像、預設值也相近，必須靠文案與顏色
        # 區分「會寫檔」與「只影響這次」
        tk.Label(
            gd,
            text="⚠ 會覆蓋並存檔",
            bg=CLR_CARD,
            fg=CLR_WARN,
            font=("Segoe UI", 8, "bold"),
        ).pack(side="left", padx=4)

    def _build_card_rec_list(self, scr):
        list_card = self._card(scr, "已儲存行程（選一個，下方會顯示它的步驟）")
        self._rec_tree = ttk.Treeview(
            list_card,
            columns=("name", "count", "created"),
            show="headings",
            height=5,
        )
        for col, w, lbl in [
            ("name", 160, "名稱"),
            ("count", 60, "步數"),
            ("created", 180, "建立時間"),
        ]:
            self._rec_tree.heading(col, text=lbl)
            self._rec_tree.column(col, width=w)
        self._rec_tree.bind("<<TreeviewSelect>>", self._on_rec_select)
        sy3 = ttk.Scrollbar(list_card, orient="vertical", command=self._rec_tree.yview)
        self._rec_tree.configure(yscrollcommand=sy3.set)
        self._rec_tree.pack(side="left", fill="x", expand=True, padx=12, pady=8)
        sy3.pack(side="right", fill="y", pady=8)

        recbtn = tk.Frame(list_card, bg=CLR_CARD)
        recbtn.pack(side="bottom", anchor="w", padx=12, pady=(0, 8))
        ttk.Button(
            recbtn,
            text="🗑 刪除行程",
            style="Danger.TButton",
            command=self._do_delete_rec,
        ).pack(side="left")
        tk.Label(
            recbtn,
            text="（檔案會改名成 .bak 保留，可手動救回）",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 8),
        ).pack(side="left", padx=8)

    def _build_card_playback(self, scr):
        play_card = self._card(scr, "重播設定")
        pf = tk.Frame(play_card, bg=CLR_CARD)
        pf.pack(fill="x", padx=12, pady=8)
        tk.Label(
            pf, text="重複次數:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).grid(row=0, column=0, sticky="w", pady=3)
        self._repeat_var = tk.IntVar(value=1)
        ttk.Spinbox(pf, from_=1, to=9999, textvariable=self._repeat_var, width=8).grid(
            row=0, column=1, sticky="w", padx=8
        )
        self._play_progress_var = tk.StringVar(value="—")
        tk.Label(
            pf,
            textvariable=self._play_progress_var,
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
        ).grid(row=0, column=2, padx=16)

        # ── 每輪之間的間隔（不是步與步之間；那是各步驟自己的 delay）──
        tk.Label(
            pf,
            text="每輪之間間隔:",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
        ).grid(row=1, column=0, sticky="w", pady=3)
        self._cycle_delay_var = tk.StringVar(value="3000")
        ttk.Entry(pf, textvariable=self._cycle_delay_var, width=8).grid(
            row=1, column=1, sticky="w", padx=8
        )
        tk.Label(
            pf,
            text="ms（跑完一次完整行程後、下一輪開始前的等待；"
                 "步與步之間的延遲請在上方「已錄製步驟」設定）",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 8),
            justify="left",
            wraplength=420,
        ).grid(row=1, column=2, sticky="w", padx=8)

        tk.Label(
            play_card,
            text="⚠ 重播會直接重送錄下來的原始指令，"
                 "不經過軟體限位檢查、也不做 PULS 取整正規化。"
                 "執行前請先確認各軸位置。",
            bg=CLR_CARD,
            fg=CLR_WARN,
            font=("Segoe UI", 8),
            justify="left",
            wraplength=560,
        ).pack(anchor="w", padx=12, pady=(0, 4))

        plbtn = tk.Frame(play_card, bg=CLR_CARD)
        plbtn.pack(padx=12, pady=(0, 8))
        # Warn 而非 Accent：它會用未經限位檢查的指令驅動真實滑台，
        # 不該跟「儲存 Teaching Point」長得一樣安全
        self._play_btn = ttk.Button(
            plbtn, text="▶ 重播行程", style="Warn.TButton", command=self._do_play_rec
        )
        self._play_btn.pack(side="left", padx=4)
        self._stop_play_btn = ttk.Button(
            plbtn,
            text="■ 停止重播",
            style="Danger.TButton",
            command=self._do_stop_playback,
            state="disabled",
        )
        self._stop_play_btn.pack(side="left", padx=4)

    def _do_start_rec(self):
        self.ctrl.start_recording(self._rec_name_var.get().strip())
        self._rec_status_var.set("🔴 錄製中")
        self._rec_start_btn.state(["disabled"])
        self._rec_stop_btn.state(["!disabled"])
        self._rec_step_timer()

    def _rec_step_timer(self):
        if self.ctrl.recording:
            self._rec_count_var.set(f"{len(self.ctrl.recorded_steps)} 步")
            self.root.after(500, self._rec_step_timer)

    def _do_stop_rec(self):
        rec = self.ctrl.stop_recording()
        self._rec_status_var.set("● 閒置")
        self._rec_count_var.set("0 步")
        self._rec_start_btn.state(["!disabled"])
        self._rec_stop_btn.state(["disabled"])
        self._refresh_recordings()
        self._load_steps_to_tree(rec)

    def _on_rec_select(self, event):
        rec = self._selected_recording()
        if rec is not None:
            self._load_steps_to_tree(rec)

    def _load_steps_to_tree(self, rec: dict):
        self._steps_tree.delete(*self._steps_tree.get_children())
        for i, step in enumerate(rec.get("steps", [])):
            self._steps_tree.insert(
                "",
                "end",
                values=(
                    i + 1,
                    step.get("ts", ""),
                    step.get("tx", step.get("msg", "")),
                    step.get("delay_ms", 200),
                ),
            )

    def _on_step_dclick(self, event):
        sel = self._steps_tree.selection()
        if not sel:
            return
        item = sel[0]
        vals = self._steps_tree.item(item)["values"]
        step_idx = int(vals[0]) - 1
        win = tk.Toplevel(self.root)
        win.title("修改步驟延遲")
        win.geometry("300x110")
        win.resizable(False, False)
        win.grab_set()
        tk.Label(
            win,
            text=f"步驟 #{vals[0]}  指令: {str(vals[2])[:35]}",
            font=("Segoe UI", 9),
            fg=CLR_MUTED,
        ).pack(pady=(10, 0))
        dvar = tk.StringVar(value=str(vals[3]))
        rf = tk.Frame(win)
        rf.pack(pady=8)
        tk.Label(rf, text="延遲 (ms):").pack(side="left")
        ttk.Entry(rf, textvariable=dvar, width=10).pack(side="left", padx=6)

        def _apply():
            try:
                ms = int(dvar.get())
                assert ms >= 0
            except Exception:
                messagebox.showerror("錯誤", "請輸入有效正整數", parent=win)
                return
            # 同樣是「先確認能寫成功，再動畫面」
            rec = self._selected_recording()
            if rec is None:
                messagebox.showerror("錯誤", "找不到對應的行程", parent=win)
                return
            steps = rec.get("steps", [])
            if not (0 <= step_idx < len(steps)):
                messagebox.showerror("錯誤", "步驟索引超出範圍", parent=win)
                return
            steps[step_idx]["delay_ms"] = ms
            if not self.ctrl.save_recording(rec):
                messagebox.showerror(
                    "錯誤", "寫入行程檔失敗（詳見 LOG），畫面未更新", parent=win
                )
                return
            self._steps_tree.item(item, values=(vals[0], vals[1], vals[2], ms))
            self._flash_banner(f"步驟 #{vals[0]} 延遲已改為 {ms}ms 並存檔", 4000)
            win.destroy()

        ttk.Button(win, text="確認", style="Accent.TButton", command=_apply).pack()

    def _apply_global_delay(self):
        """
        批次把所有步驟的延遲設成同一個值，並寫回行程檔。

        ⚠ **所有檢查都要在動畫面之前完成。** 舊寫法先把 tree 上每一列的
        延遲欄改掉，才發現沒選行程 → 跳警告 return，結果是「檔案沒寫、
        記憶體沒改，但螢幕上滿屏都是新值」。使用者按完確認會合理認為
        已生效——這正是「其實沒生效卻顯示成功」。
        """
        rec = self._selected_recording()
        if rec is None:
            self._flash_banner("請先選擇要套用的行程", 4000)
            return
        try:
            ms = int(self._global_delay_var.get())
            assert ms >= 0
        except (ValueError, AssertionError):
            messagebox.showerror("錯誤", "請輸入 0 或正整數毫秒")
            return

        # 檢查全過了才動資料與畫面
        for step in rec.get("steps", []):
            step["delay_ms"] = ms
        if not self.ctrl.save_recording(rec):
            messagebox.showerror("錯誤", "寫入行程檔失敗（詳見 LOG），畫面未更新")
            return
        for item in self._steps_tree.get_children():
            vals = self._steps_tree.item(item)["values"]
            self._steps_tree.item(item, values=(vals[0], vals[1], vals[2], ms))
        self._flash_banner(
            f"行程 [{rec.get('name')}] 全部步驟延遲已設為 {ms}ms 並存檔", 4000
        )

    def _do_play_rec(self):
        if self.ctrl.playback_running:
            self._flash_banner("已有重播進行中", 4000)
            return
        rec = self._selected_recording()
        if rec is None:
            self._flash_banner("請先選擇要重播的行程", 4000)
            return
        # 每輪之間的間隔（跑完一次完整行程後、下一輪開始前）
        try:
            cycle_delay = int(self._cycle_delay_var.get())
            assert cycle_delay >= 0
        except (ValueError, AssertionError):
            messagebox.showerror("錯誤", "每輪之間間隔請輸入 0 或正整數毫秒")
            return

        repeat = self._repeat_var.get()
        n_steps = len(rec.get("steps", []))
        # 粗估：各步驟延遲 + 輪間間隔（不含到位等待，所以是下限）
        est_s = (
            sum(s.get("delay_ms", 200) for s in rec.get("steps", [])) * repeat
            + cycle_delay * max(0, repeat - 1)
        ) / 1000.0

        # 這是「必須做決定才能繼續」的情境——它會用未經限位檢查的原始指令
        # 驅動真實滑台，值得一次確認。預設焦點放在「否」。
        if not messagebox.askyesno(
            "確認重播",
            f"即將重播行程 [{rec.get('name')}]\n\n"
            f"步數：{n_steps}　重複：{repeat} 輪\n"
            f"每輪間隔：{cycle_delay} ms\n"
            f"預估最短時間：{est_s:.1f} 秒（不含到位等待）\n\n"
            f"⚠ 重播不檢查軟體限位，也不做 PULS 取整正規化。\n"
            f"請先確認各軸目前位置足以完整跑完這段行程。",
            icon="warning",
            default="no",
        ):
            return

        self._stop_playback.clear()

        def _progress(done, total, note=""):
            # note 是輪次資訊或「等待中」，讓輪與輪之間的空檔不再看起來像沒反應
            txt = f"{done}/{total}" + (f"  {note}" if note else "")
            self.root.after(0, lambda: self._play_progress_var.set(txt))
            self.root.after(0, self._update_stat_ui)

        # 重播中不可再按（以前不在 _drive_buttons 裡，連按會疊出第二條
        # 執行緒，兩條同時寫序列埠）；停止鍵反過來要啟用
        self._play_btn.config(state="disabled")
        self._stop_play_btn.config(state="normal")

        def _run():
            try:
                self.ctrl.play_recording(
                    rec=rec,
                    repeat=repeat,
                    stop_event=self._stop_playback,
                    progress_cb=_progress,
                    cycle_delay_ms=cycle_delay,
                )
            finally:
                self.root.after(0, self._on_playback_end)

        threading.Thread(target=_run, daemon=True).start()

    def _on_playback_end(self):
        try:
            self._play_btn.config(state="normal")
            self._stop_play_btn.config(state="disabled")
        except tk.TclError:
            pass


    def _do_stop_playback(self):
        """
        停止重播——**同時要真的把馬達停下來**。

        舊寫法只有 `self._stop_playback.set()`，而 play_recording 收到旗標後
        直接 return，不送任何停止指令。若中斷的那一步是 `GO CWJ`（連續點動，
        沒有終點），錄製檔裡負責收尾的 `STOP 0` 就永遠送不出去了，該軸會
        一路跑到硬體限位——按鈕寫「停止」，失效方向卻是「繼續移動」。

        先送 STOP 再 set 旗標：stop() 有 STOP_LOCK_TIMEOUT(0.15s) 上限，
        在主執行緒呼叫可以接受，而且越早送出滑台滑行越短。
        """
        if self.ctrl.connected:
            self.ctrl.stop()
        self._stop_playback.set()
        self._flash_banner("■ 已中止重播並送出停止指令", 5000)

    def _do_delete_rec(self):
        rec = self._selected_recording()
        if rec is None:
            self._flash_banner("請先選擇要刪除的行程", 4000)
            return
        name = rec.get("name", "")
        n_steps = len(rec.get("steps", []))

        if not messagebox.askyesno(
            "確認刪除",
            f"確定刪除行程 [{name}]？\n\n"
            f"步數：{n_steps}\n"
            f"建立：{rec.get('created', '?')[:19]}\n\n"
            f"檔案會改名成 {name}.json.bak 保留，可手動救回。",
            icon="warning",
            default="no",
        ):
            return

        ok, msg = self.ctrl.delete_recording(name)
        if not ok:
            messagebox.showerror("刪除失敗", msg)
            return

        # 被刪的若正好是目前顯示步驟的那一筆，把步驟表一併清掉
        self._steps_tree.delete(*self._steps_tree.get_children())
        self._refresh_recordings()
        self._flash_banner(f"🗑 {msg}", 6000)

    def _refresh_recordings(self):
        """
        重繪行程清單。**iid 用行程名稱**，不要靠位置索引。

        以前四處都用 `_rec_tree.index(sel[0])` 去索引 `ctrl.recordings`，
        等於假設「顯示順序」與「記憶體順序」永遠一致——排序、篩選、
        或刪除後重繪都可能讓這個假設破功，而破功的後果是對錯誤的行程
        套用延遲、甚至刪錯檔案。Teaching Point 已經因為同類問題改用 iid。
        """
        self._rec_tree.delete(*self._rec_tree.get_children())
        for rec in self.ctrl.recordings:
            name = rec.get("name", "")
            self._rec_tree.insert(
                "",
                "end",
                iid=name,
                values=(name, rec.get("count", 0), rec.get("created", "")[:19]),
            )

    def _selected_recording(self) -> Optional[dict]:
        """回傳目前選取的行程 dict；沒選或找不到時回 None。"""
        sel = self._rec_tree.selection()
        if not sel:
            return None
        name = sel[0]  # iid 即行程名稱，不經 Treeview 的型別轉換
        return next(
            (r for r in self.ctrl.recordings if r.get("name") == name), None
        )

    # =========================================================================
    # TAB：LOG
    # =========================================================================
    def _build_tab_log(self, parent):
        self._add_status_bar(parent)
        toolbar = tk.Frame(
            parent, bg=CLR_CARD, highlightbackground=CLR_BORDER, highlightthickness=1
        )
        toolbar.pack(fill="x")
        tk.Label(
            toolbar,
            text="動作 LOG",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=12, pady=6)
        ttk.Button(
            toolbar, text="清除", style="Flat.TButton", command=self._clear_log
        ).pack(side="left", padx=4, pady=4)
        ttk.Button(
            toolbar, text="匯出 LOG", style="Info.TButton", command=self._export_log
        ).pack(side="left", padx=4, pady=4)
        ttk.Checkbutton(toolbar, text="自動捲動", variable=self._log_auto_scroll).pack(
            side="left", padx=8
        )
        # 層級篩選：出事後要找警告，不該被大量 INFO 淹掉
        ttk.Separator(toolbar, orient="vertical").pack(
            side="left", fill="y", padx=8, pady=4
        )
        tk.Label(
            toolbar, text="只顯示:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left")
        ttk.Combobox(
            toolbar,
            textvariable=self._log_filter_var,
            values=["全部", "WARN 以上", "只看 ERROR"],
            width=10,
            state="readonly",
        ).pack(side="left", padx=6)
        self._log_filter_var.trace_add("write", lambda *a: self._rerender_log())
        tk.Label(
            toolbar,
            textvariable=self._log_count_var,
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 8),
        ).pack(side="right", padx=12)

        log_frame = tk.Frame(parent, bg=CLR_LOG_BG)
        log_frame.pack(fill="both", expand=True)
        self._log_text = tk.Text(
            log_frame,
            bg=CLR_LOG_BG,
            fg="#D4D4D4",
            font=("Consolas", 9),
            relief="flat",
            bd=0,
            state="disabled",
            wrap="none",
        )
        lsy = ttk.Scrollbar(log_frame, orient="vertical", command=self._log_text.yview)
        lsx = ttk.Scrollbar(
            log_frame, orient="horizontal", command=self._log_text.xview
        )
        self._log_text.configure(yscrollcommand=lsy.set, xscrollcommand=lsx.set)
        lsy.pack(side="right", fill="y")
        lsx.pack(side="bottom", fill="x")
        self._log_text.pack(fill="both", expand=True)

        def _mw(e):
            self._log_text.yview_scroll(int(-1 * (e.delta / 120)), "units")
            return "break"

        self._log_text.bind("<MouseWheel>", _mw)
        for tag, clr in [
            ("INFO", "#4EC9B0"),
            ("WARN", "#CE9178"),
            ("ERROR", "#F44747"),
            ("DEBUG", "#6A6A6A"),
        ]:
            self._log_text.tag_config(tag, foreground=clr)

    def _clear_log(self):
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")

    def _export_log(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All", "*.*")],
            initialfile=f"ds102_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
        )
        if path:
            self.ctrl.export_log(path)
            messagebox.showinfo("完成", f"LOG 已匯出:\n{path}")

    # =========================================================================
    # LOG 回調
    # =========================================================================
    def _on_log_entry(self, entry: dict):
        self.root.after(0, self._append_log_ui, entry)

    def _log_visible(self, level: str) -> bool:
        """依目前的層級篩選判斷這筆要不要顯示。"""
        mode = self._log_filter_var.get()
        if mode == "只看 ERROR":
            return level == "ERROR"
        if mode == "WARN 以上":
            return level in ("WARN", "ERROR")
        return True

    def _rerender_log(self):
        """切換篩選後，用 action_history 重繪整個 LOG 視窗。"""
        if not self._log_text:
            return
        with self.ctrl._history_lock:
            history = list(self.ctrl.action_history)
        shown = [h for h in history if self._log_visible(h.get("level", "INFO"))]
        # 只保留尾端 LOG_TEXT_MAX_LINES 筆，避免一次塞爆 Text widget
        shown = shown[-LOG_TEXT_MAX_LINES:]
        try:
            self._log_text.config(state="normal")
            self._log_text.delete("1.0", "end")
            for h in shown:
                self._log_text.insert("end", self._fmt_log_line(h),
                                      h.get("level", "INFO"))
            if self._log_auto_scroll.get():
                self._log_text.see("end")
            self._log_text.config(state="disabled")
        except tk.TclError:
            return
        self._log_count_var.set(
            f"顯示 {len(shown)} / 共 {len(history)} 筆"
            if self._log_filter_var.get() != "全部"
            else f"共 {len(history)} 筆"
        )

    @staticmethod
    def _fmt_log_line(entry: dict) -> str:
        level = entry.get("level", "INFO")
        ts = entry.get("ts", "")
        tx = f"[{entry['tx']}] " if entry.get("tx") else ""
        rx = f"→[{entry['rx']}] " if entry.get("rx") else ""
        return f"{ts} [{level:<5}] {tx}{rx}{entry.get('msg','')}\n"

    def _append_log_ui(self, entry: dict):
        level = entry.get("level", "INFO")
        if not self._log_visible(level):
            return  # 被篩選掉，但仍留在 action_history，切回「全部」就看得到
        line = self._fmt_log_line(entry)
        if self._log_text:
            self._log_text.config(state="normal")
            self._log_text.insert("end", line, level)
            # 無上限成長的 Text widget 會讓 Tk 越跑越慢終至無回應。
            # 超過上限就從頭砍掉一批（不是每行砍一行，避免頻繁重排）。
            n_lines = int(self._log_text.index("end-1c").split(".")[0])
            if n_lines > LOG_TEXT_MAX_LINES:
                self._log_text.delete("1.0", f"{n_lines - LOG_TEXT_MAX_LINES + 1}.0")
            if self._log_auto_scroll.get():
                self._log_text.see("end")
            self._log_text.config(state="disabled")
        for sb in self._status_bars:
            sb.update_log(f"[{level}] {entry.get('msg','')}")
        # 這裡刻意不呼叫 _update_stat_ui()——_start_poller 每
        # UI_REDRAW_INTERVAL 已經會跑一次。每筆 log 都重跑一遍是純粹的
        # 重複工，而 log 的頻率遠高於 UI 需要更新的頻率。

    # =========================================================================
    # 警報回調
    # =========================================================================
    def _on_alarm(self, title: str, msg: str):
        self.root.after(0, lambda: self._show_alarm(title, msg))

    def _show_alarm(self, title: str, msg: str):
        """
        顯示限位／異常提醒。

        刻意「不」做兩件以前會做的事：
          1. 不再呼叫 ctrl.stop()。`STOP 0` 是停**全部**軸，但觸發限位的
             只有一軸；其他軸正在進行的動作沒有理由被連坐。該軸自己早已
             被控制器擋停，不需要軟體再補一刀。
          2. 不再用 messagebox 強制確認。它是 modal 的，會卡住整個 UI 直到
             使用者按下確認——限位是很常見的正常狀況（點動到底、教點超界），
             每次都要按一下非常干擾，而且期間畫面完全不更新。

        改用畫面上的橫幅提示：會自己淡出，不阻塞任何操作。
        """
        self._flash_banner(f"⚠ {title}：{msg}")

    # =========================================================================
    # 連線 / EMS
    # =========================================================================
    def _scan_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self._port_cb["values"] = ports
        if ports and not self._port_var.get():
            self._port_var.set(ports[0])

    def _toggle_connect(self):
        if self.ctrl.connected:
            # 先停再斷。少了這行，移動中按「中斷」會關掉 port 卻讓馬達繼續跑，
            # 程式從此失去對它的控制（_on_close 有做，這裡以前漏了）。
            self.ctrl.stop()
            self.ctrl.disconnect()
            self._conn_dot.itemconfig(self._conn_dot_id, fill=CLR_DANGER)
            self._conn_lbl.config(text="未連線")
            self._conn_btn.config(text="連線", bg=CLR_ACCENT)
            self._fw_var.set("（未連線）")
            self._set_drive_buttons_state("disabled")
            self._set_axis_btns_state("disabled")
        else:
            port = self._port_var.get()
            baud = int(self._baud_var.get())
            if not port:
                messagebox.showerror("錯誤", "請選擇 COM Port")
                return
            self._conn_btn.config(text="連線中...", state="disabled", bg=CLR_WARN)
            self.root.update()

            def _do():
                ok, msg = self.ctrl.connect(port, baud)
                self.root.after(0, lambda: self._on_connect_result(ok, msg))

            threading.Thread(target=_do, daemon=True).start()

    def _on_connect_result(self, ok: bool, msg: str):
        self._conn_btn.config(state="normal")
        if ok:
            self._conn_dot.itemconfig(self._conn_dot_id, fill=CLR_ACCENT)
            self._conn_lbl.config(text=f"{self.ctrl.port} @ {self.ctrl.baudrate}")
            self._conn_btn.config(text="中斷", bg=CLR_DANGER)
            self._fw_var.set(f"韌體: {self.ctrl.firmware} | {self.ctrl.axis_count} 軸")
            self._set_drive_buttons_state("normal")
            for grp in self._all_axis_btn_groups:
                for ax, b in grp.items():
                    b.config(
                        state=(
                            "normal"
                            if int(AXIS_NO[ax]) <= self.ctrl.axis_count
                            else "disabled"
                        )
                    )
            # 控制器設定是 RAM-only，斷電就沒了。連線時已嘗試從設定檔補回，
            # 這裡把結果告訴使用者：補了什麼、還缺什麼。
            # 復歸樣式下拉要填當前軸的實際值（設定檔還原之後才讀才準）
            self._sync_org_mode()
            restored = self.ctrl.config_restored
            unset = self.ctrl.homing_unconfigured
            if restored:
                self._flash_banner(
                    "✔ 已從設定檔還原控制器設定（斷電後會被清空）："
                    + "、".join(restored),
                    10000,
                )
            if unset:
                self._flash_banner(
                    f"⚠ 軸 {'、'.join(unset)} 的復歸樣式未設定（MEMSW0=0），"
                    f"原點復歸會略過這些軸。設好之後按「儲存控制器設定」，"
                    f"下次連線就會自動補回。",
                    14000,
                )
        else:
            self._conn_btn.config(text="連線", bg=CLR_ACCENT)
            messagebox.showerror("連線失敗", msg)

    def _set_drive_buttons_state(self, state: str):
        """
        切換「會發起移動」的按鈕狀態。

        停止鍵刻意不在 self._drive_buttons 裡，所以不受這裡影響——
        它由 _sync_stop_button() 單獨管理，只看有沒有連線。
        """
        for b in self._drive_buttons:
            try:
                b.config(state=state)
            except tk.TclError:
                pass

    def _selected_org_type(self) -> Optional[int]:
        """
        取得下拉目前選的復歸樣式編號；未選或不合法時回 None。

        ⚠ 不可用 `ORG_MODES.index(...)`：ORG_MODES 從 "ORG 1" 開始，
        index 會差一位（"ORG 1" 的 index 是 0）。而且舊寫法的
        `else 0` fallback 正好會送出 MEMSW0 0＝把復歸樣式毀掉。
        一律直接解析字串裡的數字，解析不出來就回 None、不動作。
        """
        raw = self._org_mode_var.get().strip()
        m = re.match(r"ORG\s+(\d+)$", raw)
        if not m:
            return None
        n = int(m.group(1))
        return n if 1 <= n <= 12 else None

    def _do_origin_move(self, ax: str, spd):
        """按下原點返回：樣式未知就明確拒絕，不要猜一個值送出去。"""
        org_type = self._selected_org_type()
        if org_type is None:
            self._flash_banner(
                "⚠ 尚未取得此軸的復歸樣式，無法執行原點返回。"
                "請先連線；若下拉是空的，表示控制器的 MEMSW0 未設定"
                "（可到儀表板的「控制器設定」還原）。",
                10000,
            )
            return
        l, f, r, s = spd
        threading.Thread(
            target=self.ctrl.move_origin,
            args=(ax, org_type, l, f, r, s, True),
            daemon=True,
        ).start()
        self._poll_status()

    def _sync_org_mode(self):
        """
        把當前軸在控制器裡的實際復歸樣式填進下拉。

        為什麼不給寫死的預設值：`move_origin()` 送出的第一道指令就是
        `MEMSW0 {type}`，所以下拉顯示什麼，按下去就會把該軸的復歸樣式
        改成什麼。以前預設是 `ORG 0`（Type0＝不執行），等於一按就把樣式
        毀掉。讀不到就留空，並由 `_on_*_press` 拒絕執行原點動作。
        """
        if not self.ctrl.connected:
            self._org_mode_var.set("")
            return

        def _work():
            raw = self.ctrl._serial_write_read(
                f"AXI{self.ctrl.axis_no}:MEMSW0?"
            ).strip()
            val = f"ORG {raw}" if raw in [str(i) for i in range(1, 13)] else ""
            self.root.after(0, lambda: self._org_mode_var.set(val))

        threading.Thread(target=_work, daemon=True).start()

    def _sync_stop_button(self):
        """停止鍵只在未連線時 disable，其餘一律可按（含重播中、復歸中）。"""
        btn = getattr(self, "_stop_btn", None)
        if btn is None:
            return
        try:
            btn.config(state="normal" if self.ctrl.connected else "disabled")
        except tk.TclError:
            pass

    def _set_axis_btns_state(self, state: str):
        for grp in self._all_axis_btn_groups:
            for b in grp.values():
                b.config(state=state)

    def _toggle_ems(self):
        if not self.ctrl.ems_active:
            self.ctrl.emergency_stop()
            self._ems_btn.config(text="✅ 解除緊急停止", bg="#2E7D32")
            self._set_drive_buttons_state("disabled")
        else:
            if messagebox.askyesno(
                "解除緊急停止",
                "請確認各軸已移離危險位置，\n且操作環境安全後再解除。\n\n確認解除？",
            ):
                self.ctrl.release_ems()
                self._ems_btn.config(text="⛔  緊急停止", bg=CLR_DANGER)
                if self.ctrl.connected:
                    self._set_drive_buttons_state("normal")

    # =========================================================================
    # 全軸回 HOME
    # =========================================================================
    def _do_home_all(self):
        """
        全軸原點復歸，按下即執行、不跳確認視窗。
        某軸失敗不中止整批，繼續跑其餘各軸。
        結果寫入 LOG 與狀態列；只有出現失敗項目時才彈窗。
        """
        if not self.ctrl.connected or self.ctrl.ems_active:
            return
        if self._homing.is_set():
            return  # 已經在復歸，別疊第二條執行緒上去

        # 這個旗標讓 _update_stat_ui 知道「作業進行中」。少了它，那邊每
        # UI_REDRAW_INTERVAL 就會把下面這行鎖定解掉，於是一顆寫著
        # 「復歸中…」的按鈕變成可按的。
        self._homing.set()
        self._home_btn.config(state="disabled", text="🏠  復歸中…")
        self._set_drive_buttons_state("disabled")
        l, f, r, s = self._get_spd()

        def _run():
            try:
                ok, msg = self.ctrl.origin_all(l, f, r, s, progress_cb=_progress)
            except Exception as e:  # 背景執行緒的例外不可讓旗標卡在 set
                self.root.after(0, lambda: _done(False, f"復歸過程發生例外: {e}"))
                return
            self.root.after(0, lambda: _done(ok, msg))

        def _progress(ax, state):
            self.root.after(
                0, lambda: self._home_btn.config(text=f"🏠  {ax} {state}")
            )

        def _done(ok, msg):
            # 先清旗標再還原狀態，否則 _update_stat_ui 會再把它鎖回去
            self._homing.clear()
            self._home_btn.config(state="normal", text="🏠  全軸原點復歸")
            if self.ctrl.connected and not self.ctrl.ems_active:
                self._set_drive_buttons_state("normal")
            for sb in self._status_bars:
                sb.update_log(f"全軸原點復歸 — {msg}")
            if not ok:
                messagebox.showwarning("全軸原點復歸", msg)

        threading.Thread(target=_run, daemon=True).start()

    # =========================================================================
    # 速度 Profile
    # =========================================================================
    def _save_profile_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("儲存速度 Profile")
        win.geometry("280x90")
        win.resizable(False, False)
        win.grab_set()
        tk.Label(win, text="Profile 名稱:").pack(pady=(10, 4))
        nv = tk.StringVar()
        ttk.Entry(win, textvariable=nv, width=24).pack()

        def _save():
            name = nv.get().strip()
            if not name:
                messagebox.showerror("錯誤", "請輸入名稱", parent=win)
                return
            l, f, r, s = self._get_spd()
            self.ctrl.save_speed_profile(name, l, f, r, s)
            self._refresh_profiles()
            win.destroy()

        ttk.Button(win, text="儲存", style="Accent.TButton", command=_save).pack(pady=8)

    def _load_profile(self, event=None):
        name = self._profile_var.get()
        p = self.ctrl.speed_profiles.get(name)
        if not p:
            return
        self._spd_vars["l_speed"].set(p["l_speed"])
        self._spd_vars["f_speed"].set(p["f_speed"])
        self._spd_vars["rate"].set(p["rate"])
        self._spd_vars["s_rate"].set(p["s_rate"])
        self.ctrl._log("INFO", f"載入速度 Profile [{name}]")

    def _delete_profile(self):
        """刪除速度 Profile。確認視窗比照「刪除行程」的規格。"""
        name = self._profile_var.get()
        if not name:
            self._flash_banner("請先選取要刪除的 Profile", 4000)
            return
        p = self.ctrl.speed_profiles.get(name, {})
        detail = "　".join(
            f"{k}={p.get(k, '?')}" for k in ("l_speed", "f_speed", "rate", "s_rate")
        )
        if not messagebox.askyesno(
            "確認刪除",
            f"確定刪除速度 Profile [{name}]？\n\n{detail}\n\n"
            f"刪除後只能從 speed_profiles.json.bak 手動救回。",
            icon="warning",
            default="no",
        ):
            return
        self.ctrl.delete_speed_profile(name)
        self._profile_var.set("")
        self._refresh_profiles()
        self._flash_banner(f"🗑 Profile [{name}] 已刪除", 5000)

    def _refresh_profiles(self):
        names = list(self.ctrl.speed_profiles.keys())
        self._profile_cb["values"] = names
        if names and not self._profile_var.get():
            self._profile_var.set(names[0])

    # =========================================================================
    # 控制器設定存檔 / 還原
    # =========================================================================
    def _cfg_summary(self) -> str:
        cfg = self.ctrl.controller_config or {}
        axes = cfg.get("axes", {})
        if not axes:
            return "尚未儲存過控制器設定"
        parts = []
        for ax, v in axes.items():
            bits = [f"樣式{v.get('memsw0', '?')}"]
            if v.get("cwsle") == "1" or v.get("ccwsle") == "1":
                bits.append("含軟限位")
            parts.append(f"{ax}({'/'.join(bits)})")
        return f"存於 {cfg.get('saved', '?')}｜" + "、".join(parts)

    def _save_controller_config(self):
        if not self.ctrl.connected:
            messagebox.showwarning("未連線", "請先連線到實體控制器再儲存設定")
            return

        def _work():
            cfg = self.ctrl.capture_controller_config()
            self.root.after(0, lambda: self._on_cfg_saved(cfg))

        threading.Thread(target=_work, daemon=True).start()

    def _on_cfg_saved(self, cfg: dict):
        self._cfg_status_var.set(self._cfg_summary())
        if cfg:
            self._flash_banner("✔ 控制器設定已存檔，往後連線會自動補回", 6000)

    def _restore_controller_config(self):
        if not self.ctrl.connected:
            messagebox.showwarning("未連線", "請先連線到實體控制器")
            return

        def _work():
            self.ctrl.load_controller_config()
            done = self.ctrl.restore_controller_config()
            self.root.after(0, lambda: self._on_cfg_restored(done))

        threading.Thread(target=_work, daemon=True).start()

    def _on_cfg_restored(self, done: List[str]):
        self._cfg_status_var.set(self._cfg_summary())
        if done:
            self._flash_banner("✔ 已還原：" + "、".join(done), 8000)
        else:
            self._flash_banner("控制器設定與存檔一致，無須還原", 5000)

    # =========================================================================
    # 軟體行程限制
    # =========================================================================
    def _apply_sw_limits(self):
        """
        把輸入框的值套進 ctrl.sw_limits。

        兩件事以前是靜默發生的，現在都會講出來：
          1. 格式錯（例如打成 10,613 帶逗號）以前直接吃掉變 None＝無限制，
             卻照樣跳「已套用」的成功視窗。
          2. 輸入框開機是空的，只填 X 就按套用會把 Y/Z 既有的限制一併歸零。
        兩者都是「以為有保護、其實沒有」，比沒設還危險。
        """
        bad: List[str] = []
        pending: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
        cleared: List[str] = []

        def _parse(raw: str, ax: str, side: str) -> Optional[float]:
            raw = raw.strip()
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                bad.append(f"軸 {ax} {side}「{raw}」")
                return None

        # ── 先算出「將要變成什麼」，一個字都還沒寫進 ctrl.sw_limits ──
        for ax, (ccw_v, cw_v) in self._lim_vars.items():
            prev = self.ctrl.sw_limits.get(ax, (None, None))
            ccw = _parse(ccw_v.get(), ax, "CCW")
            cw = _parse(cw_v.get(), ax, "CW")
            pending[ax] = (ccw, cw)
            if any(p is not None for p in prev) and ccw is None and cw is None:
                cleared.append(f"{ax}（原 CCW={prev[0]}, CW={prev[1]}）")

        if bad:
            self.ctrl._log("ERROR", f"軟體限制格式錯誤，未套用: {'、'.join(bad)}")
            messagebox.showerror(
                "格式錯誤",
                "以下欄位無法解析：\n\n"
                + "\n".join(bad)
                + "\n\n**這次完全沒有套用**，既有限制維持不變。\n"
                  "請只填數字（不要有逗號或單位）後重新套用。",
            )
            return

        # ── 會清空既有保護時，事前徵求確認（以前是寫完才警告）──
        if cleared and not messagebox.askyesno(
            "確認清除限制",
            "以下軸的欄位是空的，套用後其限制會被清除（＝無限制）：\n\n"
            + "\n".join(f"　{c}" for c in cleared)
            + "\n\n這些軸將失去程式端的行程保護。確定要套用嗎？",
            icon="warning",
            default="no",
        ):
            return

        for ax, (ccw, cw) in pending.items():
            self.ctrl.sw_limits[ax] = (ccw, cw)
            self.ctrl._log("INFO", f"軸 {ax} 軟體限制: CCW={ccw}, CW={cw}")
        self._refresh_sw_limit_display()
        if cleared:
            self._flash_banner(
                f"⚠ 已清除 {len(cleared)} 個軸的程式端行程限制", 8000
            )
        else:
            self._flash_banner("✔ 軟體行程限制已套用", 5000)

    def _refresh_sw_limit_display(self):
        """把 ctrl.sw_limits 的實際內容顯示在「目前生效」欄。"""
        for ax, var in self._lim_cur_vars.items():
            ccw, cw = self.ctrl.sw_limits.get(ax, (None, None))
            if ccw is None and cw is None:
                var.set("無限制")
            else:
                lo = f"{ccw:,.0f}" if ccw is not None else "−∞"
                hi = f"{cw:,.0f}" if cw is not None else "+∞"
                var.set(f"{lo} … {hi}")

    # =========================================================================
    # 實驗數據記錄
    # =========================================================================
    def _start_data_log(self):
        self.ctrl.start_data_log()
        self._dlog_status_var.set("🔴 記錄中")
        self._dlog_start_btn.state(["disabled"])
        self._dlog_stop_btn.state(["!disabled"])
        if "dlog" in self._stat_vars:
            self._stat_vars["dlog"].set("記錄中")

    def _stop_data_log(self):
        path = self.ctrl.stop_data_log()
        self._dlog_status_var.set("停止")
        self._dlog_start_btn.state(["!disabled"])
        self._dlog_stop_btn.state(["disabled"])
        if "dlog" in self._stat_vars:
            self._stat_vars["dlog"].set("停止")
        messagebox.showinfo("完成", f"數據已匯出:\n{path}")

    # =========================================================================
    # 儀表板狀態更新
    # =========================================================================
    def _update_stat_ui(self):
        # 失聯要看得出來：以前 USB 被拔掉、控制器斷電時 connected 仍是 True，
        # 指示燈維持綠色、座標停在最後一次成功的值，畫面看起來完全正常。
        stale = self.ctrl.comm_stale
        if stale and not self._comm_warned:
            self._comm_warned = True
            self._flash_banner(
                "⚠ 讀不到控制器回應，畫面上的座標已停止更新、不可信。"
                "請檢查 USB／電源，或重新連線。",
                15000,
            )
        elif not stale and self._comm_warned:
            self._comm_warned = False
            self._flash_banner("✔ 與控制器的通訊已恢復", 5000)

        if self.ctrl.connected:
            self._conn_dot.itemconfig(
                self._conn_dot_id,
                fill=CLR_DANGER if stale else CLR_ACCENT,
            )

        if "conn" in self._stat_vars:
            self._stat_vars["conn"].set(
                "⚠ 失聯" if stale
                else ("已連線" if self.ctrl.connected else "未連線")
            )
        if "axes" in self._stat_vars:
            self._stat_vars["axes"].set(
                str(self.ctrl.axis_count) if self.ctrl.connected else "—"
            )
        if "ems" in self._stat_vars:
            self._stat_vars["ems"].set("⚠️ EMS" if self.ctrl.ems_active else "正常")
        if "play" in self._stat_vars:
            self._stat_vars["play"].set(
                "🔴 重播中" if self.ctrl.playback_running else "閒置"
            )
        # ⚠ 這段每 UI_REDRAW_INTERVAL 就跑一次，會覆蓋掉別處設定的按鈕狀態。
        # 以前只認得 playback_running 與 ems_active，不認得「復歸中」，
        # 所以 _do_home_all 剛鎖上的按鈕會在 100ms 後全部復活——包含那顆
        # 文字還停在「🏠 復歸中…」的按鈕，再按一次就疊出第二條復歸執行緒。
        # 任何新增的「作業進行中」狀態都必須同步加進這個判斷。
        busy = self.ctrl.playback_running or self._homing.is_set()
        if busy:
            self._set_drive_buttons_state("disabled")
        elif self.ctrl.connected and not self.ctrl.ems_active:
            self._set_drive_buttons_state("normal")
        self._sync_stop_button()

    # =========================================================================
    # 座標定時輪詢
    # =========================================================================
    def _redraw_positions(self):
        """
        重繪儀表板的各軸座標與狀態。純顯示，不做任何 I/O。

        三種狀態必須看得出差別：未連線→「—」（不可顯示 0，0 幾乎就在
        限位開關上）、失聯→數值轉警告色並註明已停止更新、正常→顯示數值。
        每軸下方的狀態小字以前是死的（`_dash_status_vars` 建立後全檔沒有
        任何地方更新它，永遠顯示「—」），這裡一併接上。
        """
        connected = self.ctrl.connected
        stale = self.ctrl.comm_stale
        n_axes = self.ctrl.axis_count if connected else 0
        cur_ax = NO_AXIS.get(self.ctrl.axis_no)
        pos_work = self.ctrl.positions

        for ax, var in self._dash_pos_vars.items():
            enabled = connected and int(AXIS_NO[ax]) <= n_axes
            lbl = self._dash_pos_labels.get(ax)
            sv = self._dash_status_vars.get(ax)

            if not enabled:
                var.set("—")
                if lbl:
                    lbl.config(fg=CLR_MUTED)
                if sv:
                    sv.set("未連線" if not connected else "未啟用")
                continue

            var.set(f"{pos_work.get(ax, 0.0):,.0f}")
            if lbl:
                lbl.config(fg=CLR_DANGER if stale else CLR_TEXT)
            if sv:
                bits = []
                if ax == cur_ax:
                    bits.append("● 選取中")
                if stale:
                    bits.append("⚠ 已停止更新")
                else:
                    age = self.ctrl.position_age()
                    bits.append("即時" if age < 1.5 else f"{age:.0f}s 前")
                if self.ctrl.playback_running:
                    bits.append("重播中")
                sv.set("　".join(bits))

    def _start_poller(self):
        """
        把快取的座標重繪到畫面上。不做任何 I/O。

        間隔從 10ms 放寬到 UI_REDRAW_INTERVAL：資料來源（position worker）
        只有 2 Hz，10ms 等於 49/50 次在重繪同一個數字，代價是每秒約 600 次
        _lock 取放與數千次 Label.config()。那些工全部堆在 Tk 主執行緒上，
        跟 LOG 洪水疊加就是「介面越跑越鈍、偶發沒回應」。
        """

        def _poll():
            if self._shutting_down.is_set():
                return  # 關閉後不要再排下一輪，否則會對已銷毀的 widget 動作
            try:
                self._redraw_positions()
                for sb in self._status_bars:
                    sb.update_coords()
                self._update_stat_ui()
            except tk.TclError:
                return  # widget 已被銷毀（關閉流程中），安靜收工
            self.root.after(UI_REDRAW_INTERVAL, _poll)

        self.root.after(UI_REDRAW_INTERVAL, _poll)

    def _start_position_worker(self):
        """
        背景定時把硬體的實際位置讀回 _positions_pulse。

        _start_poller() 只是把快取的座標重繪到畫面上，不會去問硬體；
        而 query_status() 一次只更新它被傳入的那一軸。沒有這條執行緒的話，
        未選取的軸永遠停在舊值、選取的軸也只有移動時才會變。

        必須留在 worker 用 time.sleep——用 root.after 會把阻塞式序列查詢
        搬回 Tk 主執行緒而凍結 UI。
        """

        def _worker():
            while not self._shutting_down.is_set():
                if self.ctrl.connected and not self.ctrl.playback_running:
                    try:
                        self.ctrl.refresh_positions()
                    except Exception as e:  # 背景執行緒不可讓例外逃逸
                        logger.debug(f"背景位置刷新失敗: {e}")
                self._shutting_down.wait(POSITION_POLL_INTERVAL)

        threading.Thread(target=_worker, daemon=True).start()

    # =========================================================================
    # 關閉
    # =========================================================================
    def _on_close(self):
        self._shutting_down.set()
        self._stop_playback.set()
        if self.ctrl.connected:
            self.ctrl.stop()
            self.ctrl.disconnect()
        # log_filename 在 init_runtime() 失敗或未呼叫時會是 None（例如
        # 測試直接建 DS102GUI 而沒走 main()），那就跳過歷程匯出
        if log_filename is not None:
            auto = str(log_filename).replace(".log", "_history.txt")
            try:
                self.ctrl.export_log(auto)
            except Exception:
                logger.exception("Failed to export log on close")
        self.root.destroy()


# =============================================================================
# 程式入口
# =============================================================================
def main() -> None:
    # 先把視窗建起來，才有東西可以顯示錯誤訊息。
    # 初始化失敗時 messagebox 需要一個 root，而且打包成 windowed exe 後
    # 沒有 console，這是使用者唯一看得到原因的管道。
    root = tk.Tk()
    root.withdraw()

    ok, err = init_runtime()
    if not ok:
        messagebox.showerror("啟動失敗", err)
        root.destroy()
        return

    root.deiconify()
    DS102GUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
