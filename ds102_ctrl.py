# =============================================================================
# DS102/DS112 控制器核心模組
#
# 2026-08-17 從 main_ai.py 抽出（架構拆分第一階段 1a）。DS102Controller
# 本來就設計成完全不碰 tkinter（靠 set_log_callback/set_alarm_callback/
# set_power_reader 三個 callback 對外通知），這裡是機械式搬移，不涉及
# 重新設計狀態共享方式——全案仍然只有 DS102GUI.__init__ 那一處
# `self.ctrl = DS102Controller()` 實例化點，物件身分不受影響。
#
# main_ai.py 用 `from ds102_ctrl import DS102Controller` 取得這個類別；
# 本檔內其餘只被 main_ai.py（GUI 端）用到的模組層級常數/函式，也一併從
# main_ai.py 用 `from ds102_ctrl import ...` 重新引入，避免同一個名字
# 留兩份定義。哪些常數留在這裡、哪些留在 main_ai.py，判斷依據單純是
# 「DS102Controller 本身有沒有用到」——不是語意分類。
# =============================================================================

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

import serial


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
# main_ai.py 的 init_runtime() 在 main() 裡建立——見該函式的說明。
logger = logging.getLogger("DS102")


# =============================================================================
# JSON 設定檔讀寫（含備份與拒寫保護）
# =============================================================================
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
# UI 節奏／顯示上限與色票設定（app_settings.json）
#
# 邏輯上是「給維護人員手動編輯的 UI 靜態設定」（main_ai.py 的 CLR_* 色票、
# POSITION_POLL_INTERVAL 等大部分欄位都在那邊使用），但下方 HISTORY_MAX
# 需要透過 _app_setting_num() 覆寫，而 HISTORY_MAX 是 DS102Controller
# 用的常數。為了不讓 main_ai.py 反過來 import 這個檔案造成循環相依，
# 整組（_load_app_settings / _app_setting_num / _app_settings）留在這裡，
# main_ai.py 用到的部分改成 from ds102_ctrl import 回去。
# =============================================================================
def _load_app_settings() -> dict:
    """
    讀取 UI 節奏／顯示上限與色票設定（app_settings.json）。

    跟 meter_config.json／scanner_config.json 不同：這份是給維護人員
    手動編輯的靜態設定，程式只讀不寫，所以沒有對應的 `_save_app_settings()`
    ——沒有任何執行路徑會把這些值寫回檔案。

    這支函式在 **模組載入時**（任何 class 定義之前）就會被呼叫，此時
    `logger` 還沒有 `init_runtime()` 掛上的 FileHandler（那要等 main()
    呼叫 init_runtime() 才會建立），所以這裡的 log 不一定會落地到
    logs/*.log，但呼叫方式與既有的 `_load_meter_config` 一致，之後
    log 系統就緒時的行為不受影響。

    找不到檔案、壞檔、或個別欄位缺漏都不中止載入：找不到檔案就整份回傳
    空字典，個別欄位由呼叫端逐一 `.get(key, 預設值)` 退回目前寫死的
    預設值——不是整份退回、也不是報錯中止。這些值沒有安全含意，
    只記 INFO 不需要 WARN。
    """
    p = RECORDING_DIR / "app_settings.json"
    if not p.exists():
        logger.info("app_settings.json 不存在，UI 節奏／色票全部使用內建預設值")
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.info(f"app_settings.json 讀取失敗，改用內建預設值: {e}")
        return {}
    if not isinstance(data, dict):
        # 同 _load_meter_config／_load_scanner_config：合法 JSON 但頂層
        # 不是物件（例如被誤存成 [] / 字串 / 數字）時 json.loads() 不會
        # 拋例外，若照樣回傳出去，下面逐欄 `.get()` 的呼叫會直接
        # AttributeError，在任何視窗顯示出來之前就讓整個模組載入失敗。
        logger.info(
            f"app_settings.json 格式不符（預期物件，實際 {type(data).__name__}），改用內建預設值"
        )
        return {}
    logger.info(f"app_settings.json 已載入，{len(data)} 個欄位可能覆寫內建預設值")
    return data


def _app_setting_num(settings: dict, key: str, default, cast):
    """
    從 `_app_settings` 取一個數值欄位，型別不對就退回預設值。

    維護人員手動編輯 JSON 打錯型別（例如把數字打成字串）是可預期的失誤，
    這裡的值會直接餵給 `time.sleep()` / `root.after()` 這類函式，型別錯誤
    不處理的話會在程式啟動後才炸開、而且錯誤訊息離設定檔很遠。
    """
    if key not in settings:
        return default
    try:
        return cast(settings[key])
    except (TypeError, ValueError):
        logger.info(f"app_settings.json 欄位 {key!r} 型別不符，改用內建預設值 {default}")
        return default


_app_settings = _load_app_settings()


# =============================================================================
# B 類：安全相關常數設定（safety_settings.json）
#
# 與上面的 app_settings.json 刻意分成獨立檔案、獨立機制：A 類是 UI 節奏／
# 色票，改壞了最多介面卡頓；這裡的六顆常數牽涉通訊重送次數、到位逾時、
# 限位監看頻率、STOP 插隊搶鎖時限、通訊失聯判定門檻——改壞了會直接影響
# 「撞限位要多久才停下來」這類安全行為。recordings/ 整個目錄不進版控、
# 沒有 PR review 這道關卡，把安全常數跟色票放同一份檔案會讓人誤以為
# 兩者風險等級相同，所以刻意不沿用 _load_app_settings／_app_setting_num。
#
# 驗證也因此比 A 類嚴格：A 類只檢查型別，這裡型別對了還要落在合理範圍內
# ——例如 stop_lock_timeout 打成 500 秒不會讓程式崩潰，但等同拿掉
# 「STOP 插隊直接寫入」這層保護。範圍之外一律拒絕、退回內建預設值
# （不 clamp 到邊界：貼著邊界的值本身也未必是維護人員的本意），並記
# WARNING（比 A 類的 INFO 高一級）＋寫進 _safety_setting_rejections。
# 這份清單目前只在模組層級準備好，GUI 端的橫幅顯示是後續任務。
# =============================================================================
def _load_safety_settings() -> dict:
    """
    讀取 B 類安全常數設定（safety_settings.json）。

    讀取行為刻意與 `_load_app_settings()` 對齊（檔案不存在／壞檔／頂層
    非 dict 都回傳空字典、不中止載入），但這是獨立的檔案與獨立的函式，
    理由見上方區塊註解：安全常數不與 UI 節奏／色票共用同一份檔案。
    """
    p = RECORDING_DIR / "safety_settings.json"
    if not p.exists():
        logger.info("safety_settings.json 不存在，安全相關常數全部使用內建預設值")
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.info(f"safety_settings.json 讀取失敗，改用內建預設值: {e}")
        return {}
    if not isinstance(data, dict):
        # 合法 JSON 但頂層不是物件（例如被誤存成 [] / 字串 / 數字）時
        # json.loads() 不會拋例外，若照樣回傳出去，下面逐欄檢查會直接
        # 出錯，在任何視窗顯示出來之前就讓整個模組載入失敗。
        logger.info(
            f"safety_settings.json 格式不符（預期物件，實際 {type(data).__name__}），改用內建預設值"
        )
        return {}
    logger.info(f"safety_settings.json 已載入，{len(data)} 個欄位可能覆寫內建預設值")
    return data


def _safety_setting_num(settings: dict, key: str, default, cast, min_val, max_val):
    """
    從 `_safety_settings` 取一個數值欄位，型別不符或超出合法範圍一律拒絕。

    比 `_app_setting_num` 多一層範圍檢查（理由見上方區塊註解）。兩種
    拒絕情況（型別錯誤／範圍超出）都退回 `default`、記一筆
    `logger.warning(...)`，並把可讀訊息 append 進
    `_safety_setting_rejections`，供之後接上 GUI 橫幅時直接顯示；
    兩種情況的訊息文字刻意不同，方便分辨拒絕原因。欄位缺漏是正常情況
    （維護人員沒特別覆寫），靜默回傳 default，不記錄也不進清單。
    """
    if key not in settings:
        return default
    raw = settings[key]
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        msg = (
            f"{key}: 設定值 {raw!r} 型別不符（需為 {cast.__name__}），"
            f"已改用內建預設值 {default}"
        )
        logger.warning(f"safety_settings.json 欄位 {msg}")
        _safety_setting_rejections.append(msg)
        return default
    if not (min_val <= value <= max_val):
        msg = (
            f"{key}: 設定值 {value} 超出合法範圍 [{min_val}, {max_val}]，"
            f"已改用內建預設值 {default}"
        )
        logger.warning(f"safety_settings.json 欄位 {msg}")
        _safety_setting_rejections.append(msg)
        return default
    return value


# 拒絕紀錄：型別不符或超出範圍的欄位會 append 一筆可讀訊息到這裡。
# GUI 端會 `from ds102_ctrl import _safety_setting_rejections` 讀取並顯示
# 橫幅（見上方區塊註解）。宣告刻意放在 _load_safety_settings() 呼叫**之前**
# ——雖然 _load_safety_settings() 本身不觸碰這份清單（真正會 append 的是
# 下面呼叫 _safety_setting_num() 的六顆常數），但若之後有人在
# _load_safety_settings() 裡加邏輯用到它，先定義能避免 NameError。
_safety_setting_rejections: list[str] = []
_safety_settings = _load_safety_settings()


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

# 通訊重送次數上限。可由 safety_settings.json 的 max_retry 覆寫（範圍 1-5）。
MAX_RETRY = _safety_setting_num(_safety_settings, "max_retry", 3, int, 1, 5)
# 到位輪詢逾時（秒）。可由 safety_settings.json 的 wait_timeout 覆寫（範圍 10.0-120.0）。
WAIT_TIMEOUT = _safety_setting_num(_safety_settings, "wait_timeout", 30.0, float, 10.0, 120.0)
# 到位輪詢間隔（秒）。可由 safety_settings.json 的 wait_interval 覆寫（範圍 0.1-2.0）。
WAIT_INTERVAL = _safety_setting_num(_safety_settings, "wait_interval", 0.5, float, 0.1, 2.0)
# 連續點動時的軟體限位監看間隔（秒）。單軸只送一筆 POS?，實測約 56ms。
# 可由 safety_settings.json 的 jog_watch_interval 覆寫（範圍 0.03-1.0）。
JOG_WATCH_INTERVAL = _safety_setting_num(
    _safety_settings, "jog_watch_interval", 0.06, float, 0.03, 1.0
)

# 例行輪詢用的查詢指令：這些每秒會送出十幾筆，逐筆記 DEBUG TX/RX 會把
# LOG 檔、GUI 的 Text widget 與 action_history 全部灌爆，連帶把 Tk 的
# after() 佇列塞滿（每筆 log 都要回主執行緒重繪五個 StatusBar）。
# 預設不記錄它們；真要追通訊細節時把 DS102Controller.verbose_poll_log 設 True。
_POLL_QUERIES = ("POS?", "SB1?", "SB2?", "SB3?")

# action_history 的上限。以前是無上限 list，長時間執行會一路吃記憶體，
# 也是「偶發當機」的來源之一。
# 可由 app_settings.json 的 history_max 覆寫。
HISTORY_MAX = _app_setting_num(_app_settings, "history_max", 5000, int)
# stop() 願意花多久等 _serial_lock。等不到就插隊直接寫入——
# 停止指令遲到的代價是滑台繼續前進，比打斷別人一次查詢嚴重得多。
# 可由 safety_settings.json 的 stop_lock_timeout 覆寫（範圍 0.0-0.5；
# 負值會自然落在範圍外被拒絕，不需要另外特判）。
STOP_LOCK_TIMEOUT = _safety_setting_num(
    _safety_settings, "stop_lock_timeout", 0.15, float, 0.0, 0.5
)

# 控制器設定檔（放在 RECORDING_DIR，與 teaching_points / speed_profiles 同區）。
# 存的是 MEMSW0 復歸樣式與韌體軟體限位——這些都是 RAM-only，
# 控制器一斷電就整組回到出廠值。
CONFIG_FILE = "controller_config.json"

# RECORDING_DIR 底下「不是行程檔」的 json——載入錄製清單時要跳過它們。
# 新增任何設定檔都要記得加進來。
NON_RECORDING_JSON = frozenset(
    {
        "teaching_points.json",
        "speed_profiles.json",
        CONFIG_FILE,
        "meter_config.json",
        "scanner_config.json",
        "app_settings.json",
        "safety_settings.json",
    }
)
# 連續幾輪讀不到任何軸的位置，就判定「畫面上的座標已不可信」。
# 3 輪 × POSITION_POLL_INTERVAL(0.5s) ≈ 1.5 秒，足以濾掉偶發的單次逾時。
# 可由 safety_settings.json 的 comm_fail_threshold 覆寫（範圍 1-10）。
COMM_FAIL_THRESHOLD = _safety_setting_num(
    _safety_settings, "comm_fail_threshold", 3, int, 1, 10
)


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
        # 各軸驅動器分割設定（AXI{n}:DRDIV? 原始回應字串，0=full step…15=1/250）。
        # 🔴 對這台滑台裝的 AMS（微步進）型驅動器沒有意義——手冊(ds102 (2).pdf
        # p.73-75)明講 MS 型驅動器的細分是打開外殼調實體旋轉開關，:DRDIV 指令
        # 對它不生效，控制器也沒有電路能讀回實體開關位置。2026-08-18 實測：
        # 使用者把實體開關轉到 6，這裡查回來依然是 0——查到的只是控制器內部
        # 一個沒人寫過的軟體暫存器，跟實體開關無關，不是查詢邏輯錯誤。
        # 連線時查一次、之後不會變，純資訊性顯示，不做任何 pulse→um 換算
        # ——RESOLUT? 在這台機器上實測回傳 1，代表控制器裡沒有配置真實尺度，
        # 貿然拿 DRDIV 去乘除會是未經驗證的假設（同一個理由，2026-08-05 拿掉
        # 了 um/mm 單位切換，見 CLAUDE.md）。
        self.axis_drdiv: Dict[str, str] = {}
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
        # 光功率讀值來源（GUI 端注入，見 set_power_reader）。controller 不碰
        # GPIB，只在記錄每個資料點時問一下「目前快取的 dBm 是多少」——
        # 不是觸發新查詢，純讀快取值，避免拖慢 _wait_axis_stop 的等待迴圈。
        self._power_reader_cb = None

        # 速度 Profile
        self.speed_profiles: Dict[str, dict] = {}
        # 同 _points_loaded：沒載入就儲存會把既有 Profile 整份蓋掉
        self._profiles_loaded = False

        # GUI LOG 回調
        self._log_cb = None

        # 重播鎖定旗標（重播中禁止其他移動操作）
        self.playback_running = False
        # 尋光掃描鎖定旗標（見 FiberAlignmentScanner）。掃描期間演算法會
        # 自行決定何時移動哪一軸，其他來源（GUI 手動操作、_start_position_worker
        # 的背景輪詢）都必須讓路，否則會跟演算法的移動指令交錯、
        # 或在演算法等待到位時搶走 _serial_lock 拖慢量測預算。
        self.scanning_active = False

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

    @property
    def positions_machine(self) -> Dict[str, float]:
        """
        回傳各軸機械座標（未扣除 offset）。

        `positions` 回傳的是工作座標，給人看／給 Teaching Point 用；
        軟體限位（`sw_limits`）與 `check_sw_limits_batch` 比對的都是
        機械座標——CLAUDE.md〈單位與座標〉已載明這條分界，跨界線前
        先確認在同一個座標系。FiberAlignmentScanner 的搜尋全程在
        機械座標下進行（跟限位比對用同一個座標系），不透過 offset。
        """
        with self._lock:
            return dict(self._positions_pulse)

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

    def set_power_reader(self, cb) -> None:
        """
        設定光功率讀值來源：cb() -> Optional[float]。

        回傳「最近一次成功讀值的快取」，不得在這裡觸發新的 GPIB 查詢——
        本方法會被 _record_data_point() 在 _wait_axis_stop 的等待迴圈裡
        呼叫，若 cb 內部又去等一次 GPIB I/O（單次約 110~130ms，見
        diagnose_timing.py 的實測），會把這段延遲疊加進軸的到位判斷。
        沒有讀值可用（未連線、或連續失敗超過門檻）回傳 None，CSV 該欄留空。
        """
        self._power_reader_cb = cb

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

        # 各軸驅動器分割設定，純資訊性查詢，不影響任何既有邏輯。查一次、
        # 存起來，不放進背景輪詢——這是驅動器的靜態設定，不會像 POS? 那樣
        # 隨時間變化。個別軸查詢失敗不影響連線本身（保留舊值或空字串）。
        self.axis_drdiv = {}
        for i in range(self.axis_count):
            axis_no = str(i + 1)
            ax = NO_AXIS.get(axis_no)
            if not ax:
                continue
            drdiv = self._serial_write_read(f"AXI{axis_no}:DRDIV?")
            if drdiv:
                self.axis_drdiv[ax] = drdiv
        if self.axis_drdiv:
            summary = "、".join(f"{ax}={v}" for ax, v in self.axis_drdiv.items())
            self._log("INFO", f"驅動器分割 DRDIV: {summary}")

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

    def check_sw_limits_batch(self, targets: Dict[str, float]) -> Tuple[bool, str]:
        """
        一次性檢查多個軸的目標座標（機械座標，pulse）是否都在軟體限位內。

        給 FiberAlignmentScanner 的多軸同時出發流程使用：那個情境要求
        「全部通過才送任何一軸的 GO」，若只逐軸檢查再逐軸送出，會出現
        「已經送了兩軸的 GO，第三軸才發現超限」的半出發狀態——此時已出發
        的軸該怎麼收尾又是另一個問題（DS102 沒有單軸停止指令）。批次
        檢查把這個問題挪到「送出前」解決，一根軸都不送就是最乾淨的失敗。

        targets: {軸名: 目標機械座標}。回傳 (全部通過?, 若失敗的原因)。
        """
        for ax, target in targets.items():
            axis_no = AXIS_NO.get(ax)
            if not axis_no:
                continue
            ok, reason = self._check_sw_limit(axis_no, target)
            if not ok:
                return False, reason
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
        if self.ems_active or self.playback_running or self.scanning_active:
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
        步進移動（一次性），供 GUI／使用者操作使用。

        格式：AXI{n}:L0 {l}:R0 {r}:S0 {s}:F0 {f}:PULS {p}:GO CW / CCW
        wait_done=True 時，發送後阻塞直到到位（輪詢 SB1? Driving 位元清除）。
        amount 單位為 pulse。

        回傳 True=順利到位（或未要求等待），False=被攔截／逾時／撞限位。
        呼叫端請務必看這個回傳值：連續移動多軸時，第一軸撞了限位還往下跑
        就會演變成一路撞端點。

        搜尋演算法（FiberAlignmentScanner）期間請呼叫 `scan_move_step()`
        而非這個方法——這裡的守衛包含 `scanning_active`，會把演算法
        自己的移動也一併擋下（`scanning_active` 存在的目的是擋「其他
        來源」，不是擋演算法本身）。
        """
        if self.ems_active or self.playback_running or self.scanning_active:
            return False
        return self._do_move_step(axis_no, direction, amount, l_speed, f_speed, rate, s_rate, wait_done)

    def scan_move_step(
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
        `move_step()` 的搜尋演算法專用版本：只受 `ems_active` 攔截，
        不檢查 `scanning_active`（那個旗標本來就是搜尋演算法自己設的，
        用來擋 GUI 手動操作與背景輪詢，不該連自己也一併擋下）。

        `playback_running` 也不檢查——搜尋與重播本來就是互斥的兩種
        「作業進行中」狀態，`run()` 一開始就已經確認沒有其他搜尋在跑，
        重播互斥交給 GUI 層的按鈕鎖定處理（比照既有的 `_homing` 模式）。

        只給 `FiberAlignmentScanner` 呼叫，不對 GUI 開放這個入口。
        """
        if self.ems_active:
            return False
        return self._do_move_step(axis_no, direction, amount, l_speed, f_speed, rate, s_rate, wait_done)

    def _do_move_step(
        self,
        axis_no: str,
        direction: str,
        amount: str,
        l_speed: str,
        f_speed: str,
        rate: str,
        s_rate: str,
        wait_done: bool,
    ) -> bool:
        """`move_step` / `scan_move_step` 共用的實作，守衛檢查交給呼叫端。"""
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
        if self.ems_active or self.playback_running or self.scanning_active:
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
        if self.ems_active or self.playback_running or self.scanning_active:
            return False, "EMS 作用中／重播進行中／尋光進行中，已略過"

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

    def wait_axis_stop(self, axis_no: str, timeout: float = WAIT_TIMEOUT) -> bool:
        """
        `_wait_axis_stop()` 的公開版本。

        `move_step(wait_done=False)` 只負責送出 GO 指令、立刻回傳，不等到位。
        FiberAlignmentScanner 的多軸同時出發流程需要「先送完所有軸的 GO，
        再依序等每一軸到位」，這個等待步驟因此要獨立於 move_step 之外被
        呼叫——供給 controller 以外的模組（fiber_scanner.py）使用，不必
        讓它碰底線用底線開頭的內部方法。
        """
        return self._wait_axis_stop(axis_no, timeout)

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
        if self.ems_active or self.playback_running or self.scanning_active:
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
            writer = csv.DictWriter(f, fieldnames=["ts"] + AXES + ["dbm"])
            writer.writeheader()
            writer.writerows(self._data_log)
        self._log("INFO", f"實驗數據已匯出: {path}（{len(self._data_log)} 筆）")
        return str(path)

    def _record_data_point(self) -> None:
        """記錄當前時間戳、各軸位置與光功率快取值（在 _wait_axis_stop 中定期呼叫）"""
        with self._lock:
            row = {"ts": datetime.now().isoformat(timespec="milliseconds")}
            row.update({ax: self._positions_pulse[ax] for ax in AXES}) # type: ignore
        dbm = None
        if self._power_reader_cb is not None:
            try:
                dbm = self._power_reader_cb()
            except Exception:
                dbm = None
        row["dbm"] = dbm if dbm is not None else ""
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
