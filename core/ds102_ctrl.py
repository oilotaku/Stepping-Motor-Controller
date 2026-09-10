# =============================================================================
# DS102/DS112 控制器核心模組
#
# 2026-08-17 從 main_ai.py 抽出。DS102Controller 本來就設計成完全不碰
# tkinter（靠 set_log_callback/set_alarm_callback/set_power_reader 三個
# callback 對外通知），這裡是機械式搬移——全案仍然只有 DS102GUI.__init__
# 那一處 `self.ctrl = DS102Controller()` 實例化點，物件身分不受影響。
#
# main_ai.py 用 `from ds102_ctrl import DS102Controller` 取得這個類別；
# 只被 main_ai.py（GUI 端）用到的模組層級常數/函式也一併重新引入，避免
# 同一個名字留兩份定義。哪些常數留在這裡、哪些留在 main_ai.py，判斷依據
# 單純是「DS102Controller 本身有沒有用到」，不是語意分類。
# =============================================================================

import sys
import threading
import time
import json
import logging
import csv
import re
import math
import statistics
import contextlib
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Callable

import serial


# =============================================================================
# 執行期目錄與 LOG 系統
# =============================================================================
def _app_dir() -> Path:
    """
    專案根目錄（放 logs/recordings/data 的位置）——**不是**目前工作目錄，
    也**不是**本檔案所在目錄。

    以前三個資料目錄用 `Path("logs")` 這種相對路徑，綁在 CWD 上：直接跑
    .py 時看不出問題，但打包成 exe 後從開始功能表啟動，CWD 可能變成
    C:\\Windows\\System32，教點與行程會被存到那裡且每次啟動位置不同看到的
    清單都不一樣。改成以執行檔位置為基準，走到哪都指向同一份資料。

    2026-09-03：分資料夾遷移第一步。本檔未來會搬進 core/ 子資料夾，
    若沿用「本檔所在目錄」（`Path(__file__).resolve().parent`）就會多繞一層，
    算出 core/ 而不是專案根，recordings/logs/data 會悄悄跑去錯的位置且完全
    不報錯。改成往上尋找「含 main_ai.py（主程式入口，架構上固定留在根目
    錄不搬）的目錄」，這樣不管本檔被搬到第幾層子資料夾，算出來的都還是
    同一個專案根——搬檔案這件事本身不會影響任何人的 recordings/logs/data
    位置。
    """
    if getattr(sys, "frozen", False):  # PyInstaller 打包後為 True
        return Path(sys.executable).resolve().parent
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / "main_ai.py").exists():
            return candidate
    # 找不到 main_ai.py（理論上不會發生，除非本檔被複製到專案外執行）：
    # 退回本檔所在目錄，至少行為可預期，不會靜默指向奇怪的位置。
    return here


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
    所以任何沒先 load 就儲存的程式碼路徑都會把既有內容清空——實際發生過
    兩次，都是測試腳本建了新的 DS102Controller 就呼叫 save/delete。.bak
    讓這種意外可以直接復原。
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

    # 例外要在這裡收掉並轉成 LOG + 往上拋給呼叫端判斷，不能讓 OSError
    # 直接穿過 Tk callback 變成 traceback。磁碟滿、防毒鎖檔、或 exe 被放
    # 在唯讀位置時都會走到這裡——打包之後尤其容易遇上。
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
# JSON 設定檔讀取骨架（共用）
#
# app_settings / safety_settings（本檔）與 meter_config / scanner_config
# （main_ai.py）四份讀取函式原本逐字重複同一套骨架：exists 檢查 →
# read_text → json.loads → except (OSError, JSONDecodeError) →
# isinstance(dict) 檢查 → 回傳 data。2026-08-18 抽成這支共用函式。
#
# 四者的差異——訊息文字、log 等級、「不存在」與「成功」要不要記錄、用
# log 回呼還是模組 logger——全部靠參數重現，刻意不「平均化」：純粹的
# 行為保留重構，不能改變任何呼叫點的可觀察行為。
# =============================================================================
def _log_level_adapter(level: str, msg: str) -> None:
    """
    `_load_json_settings()` 在呼叫端沒提供 `log` 回呼時使用的預設轉接。

    與 main_ai.py 的同名函式邏輯一致（那邊給 `_load_meter_config`／
    `_load_scanner_config` 用），這裡單獨放一份是因為 ds102_ctrl.py
    不能反過來 import main_ai.py（會造成循環相依）。
    `_load_app_settings`／`_load_safety_settings` 一律是 INFO 等級，
    實務上只會走到 `logger.info` 這一支，但保留完整對應表是為了讓
    `_load_json_settings` 作為通用骨架時行為一致、不必依賴呼叫端。
    """
    getattr(
        logger,
        {"ERROR": "error", "WARN": "warning", "DEBUG": "debug"}.get(level, "info"),
    )(msg)


def _load_json_settings(
    path: Path,
    label: str,
    log=None,
    error_level: str = "INFO",
    not_found_msg: Optional[str] = None,
    fail_msg: str = "{label} 讀取失敗: {error}",
    invalid_type_msg: str = "{label} 格式不符（預期物件，實際 {type}），已忽略",
    success_msg: Optional[str] = None,
) -> dict:
    """
    共用的 JSON 設定檔讀取骨架。找不到檔案、壞檔、頂層非 dict 一律回傳
    空字典，不中止呼叫端載入流程（呼叫端自行決定退回哪個預設值）。

    參數（預設值對齊 meter_config／scanner_config 現有「安靜」行為，
    app_settings／safety_settings 呼叫時逐一覆寫成各自的「詳細」行為）：
      - path / label：設定檔路徑，以及訊息裡要嵌入的檔名標籤
        （樣板字串用 `{label}` 佔位）。
      - log：`(level, msg) -> None` 回呼。未提供時用上面的
        `_log_level_adapter` 轉呼模組層級 `logger`。
      - error_level：讀取失敗／型別不符時的 log 等級。
        app_settings／safety_settings 固定 INFO（沿用此預設值不覆寫），
        meter_config／scanner_config 呼叫時覆寫成 ERROR。
      - not_found_msg：檔案不存在時要記錄的訊息樣板（可用 `{label}`
        佔位）；預設 `None` 表示不記錄——這是 meter_config／
        scanner_config 的既有行為。app_settings／safety_settings
        呼叫時傳入各自的樣板字串。
      - fail_msg：讀取失敗（OSError/JSONDecodeError）時的訊息樣板
        （可用 `{label}`／`{error}` 佔位），預設值對齊 meter_config／
        scanner_config 現有文字。
      - invalid_type_msg：頂層非 dict 時的訊息樣板（可用 `{label}`／
        `{type}` 佔位），預設值同上對齊 meter_config／scanner_config。
      - success_msg：成功讀到內容時要記錄的訊息樣板（可用 `{label}`／
        `{n}` 佔位）；預設 `None` 表示不記錄——同樣是 meter_config／
        scanner_config 的既有行為。
    """
    emit = log or _log_level_adapter

    if not path.exists():
        if not_found_msg is not None:
            emit("INFO", not_found_msg.format(label=label))
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        emit(error_level, fail_msg.format(label=label, error=e))
        return {}
    if not isinstance(data, dict):
        # 合法 JSON 但頂層不是物件（例如被誤存成 [] / 字串 / 數字）不會讓
        # json.loads() 拋例外，若照樣回傳出去，呼叫端當成 dict 呼叫 .get()
        # 會直接 AttributeError，在還沒有任何視窗顯示出來之前就讓程式崩潰
        # （architect 審查抓到的問題，四份原始函式都各自有這段防護）。
        emit(
            error_level,
            invalid_type_msg.format(label=label, type=type(data).__name__),
        )
        return {}
    if success_msg is not None:
        emit("INFO", success_msg.format(label=label, n=len(data)))
    return data


# =============================================================================
# UI 節奏／顯示上限與色票設定（app_settings.json）
#
# 邏輯上是給維護人員手動編輯的 UI 靜態設定（main_ai.py 的 CLR_* 色票、
# POSITION_POLL_INTERVAL 等大部分欄位在那邊使用），但下方 HISTORY_MAX
# 是 DS102Controller 用的常數、需要 _app_setting_num() 覆寫。為了不讓
# main_ai.py 反過來 import 這個檔案造成循環相依，整組
# （_load_app_settings / _app_setting_num / _app_settings）留在這裡，
# main_ai.py 用到的部分改成 from ds102_ctrl import 回去。
# =============================================================================
def _load_app_settings() -> dict:
    """
    讀取 UI 節奏／顯示上限與色票設定（app_settings.json）。

    跟 meter_config.json／scanner_config.json 不同：這份是給維護人員
    手動編輯的靜態設定，程式只讀不寫，沒有對應的 `_save_app_settings()`。

    這支函式在模組載入時（任何 class 定義之前）就會被呼叫，此時 `logger`
    還沒有 `init_runtime()` 掛上的 FileHandler，所以這裡的 log 不一定會
    落地到 logs/*.log，但呼叫方式與既有的 `_load_meter_config` 一致。

    找不到檔案、壞檔、或個別欄位缺漏都不中止載入：找不到檔案就整份回傳
    空字典，個別欄位由呼叫端逐一 `.get(key, 預設值)` 退回內建預設值。
    這些值沒有安全含意，只記 INFO 不需要 WARN。
    """
    return _load_json_settings(
        RECORDING_DIR / "app_settings.json",
        "app_settings.json",
        not_found_msg="{label} 不存在，UI 節奏／色票全部使用內建預設值",
        fail_msg="{label} 讀取失敗，改用內建預設值: {error}",
        invalid_type_msg="{label} 格式不符（預期物件，實際 {type}），改用內建預設值",
        success_msg="{label} 已載入，{n} 個欄位可能覆寫內建預設值",
    )


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
# 色票，改壞了最多介面卡頓；這裡六顆常數牽涉通訊重送次數、到位逾時、
# 限位監看頻率、STOP 插隊搶鎖時限、通訊失聯判定門檻，改壞了會直接影響
# 「撞限位要多久才停下來」這類安全行為。recordings/ 不進版控、沒有 PR
# review，跟色票放同一份檔案會讓人誤以為風險等級相同，故不沿用
# _load_app_settings／_app_setting_num。
#
# 驗證也更嚴格：A 類只檢查型別，這裡型別對了還要落在合理範圍內——例如
# stop_lock_timeout 打成 500 秒不會讓程式崩潰，但等同拿掉「STOP 插隊
# 直接寫入」這層保護。範圍之外一律拒絕、退回內建預設值（不 clamp 到邊
# 界），並記 WARNING（比 A 類高一級）＋寫進 _safety_setting_rejections
# （目前只在模組層級準備好，GUI 端橫幅顯示是後續任務）。
# =============================================================================
def _load_safety_settings() -> dict:
    """
    讀取 B 類安全常數設定（safety_settings.json）。

    讀取行為刻意與 `_load_app_settings()` 對齊（檔案不存在／壞檔／頂層
    非 dict 都回傳空字典、不中止載入），但這是獨立的檔案與獨立的函式，
    理由見上方區塊註解：安全常數不與 UI 節奏／色票共用同一份檔案。
    """
    return _load_json_settings(
        RECORDING_DIR / "safety_settings.json",
        "safety_settings.json",
        not_found_msg="{label} 不存在，安全相關常數全部使用內建預設值",
        fail_msg="{label} 讀取失敗，改用內建預設值: {error}",
        invalid_type_msg="{label} 格式不符（預期物件，實際 {type}），改用內建預設值",
        success_msg="{label} 已載入，{n} 個欄位可能覆寫內建預設值",
    )


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

# 原點復歸「有沒有真的執行」的判定參數（2026-08-21 COM2 實機驗證：Z 軸
# 送出 GO ORG 後第一次 SB1? 往返約 56ms，但 Driving 位元要 +0.08s 才
# assert，舊語意「非 Driving 即完成」會在復歸還沒開始時就回報成功，
# 下游 set_position(axis_no, "0") 因此在滑台飛行途中寫錯座標原點）。
#
# 🔴 刻意不外部化成 safety_settings.json——CLAUDE.md 定義的 B 類安全
# 常數清單是固定六顆，新增這兩顆並可被覆寫，等於讓維護人員能把「復歸
# 有沒有真的執行」這道驗證整組關掉或調鬆到失去意義。
#
# 2026-08-21 COM2 實測的 Driving assert 延遲（決定 GRACE 要多長）：
#   GO ORG（壓在限位上出發）96ms／GO CW（一般步進）96ms／
#   GO ORG（離開限位後出發）80ms——三者幾乎一致，延遲是韌體處理 GO
#   指令的固定成本，跟出發位置無關。GRACE 取 2.0s，對 96ms 約 20 倍餘裕。
ORIGIN_START_GRACE = 2.0   # 送出 GO ORG 後容許 Driving 尚未 assert 的寬限期（秒）
ORIGIN_MOTION_EPS = 2.0    # 判定「POS 確實變化過」的門檻（pulse）
ORIGIN_START_POLL = 0.1    # 寬限期內的快輪詢間隔（秒）
# 寬限期內不用 WAIT_INTERVAL(0.5s)：2 秒只取樣 4 次，對 96ms 的 assert
# 延遲解析度太差；0.1s 約 20 次機會，且只有「還沒看到 Driving」這條路
# 徑用到——一 assert 就切回 WAIT_INTERVAL 節奏，正常復歸最多多送 1~3 筆。

# 一般步進移動「有沒有真的起步」的判定參數（2026-08-26；CLAUDE.md 自
# 2026-08-21 記載的「孿生競態」技術債，修的是上面那組的孿生體
# `_wait_axis_stop()`）。舊語意 `status == "Stop"` 直接 return True，
# 而 `GO CW` 的 Driving assert 延遲同樣是 96ms、`_wait_axis_stop()` 第
# 一次查詢要 SB3?+SB1? 兩次往返約 112ms——餘裕只有約 16ms，落在窗口裡
# 就把「還沒起步」讀成「已經停好」：`move_step(wait_done=True)` 在軸
# 飛行中回報成功，下游 `goto_point()` 提前送下一軸、
# `fiber_scanner._measure_here()` 在移動中量光功率。
#
# 🔴 同 ORIGIN_* 那組，刻意不外部化成 safety_settings.json。
#
# GRACE 取 1.0s（對 96ms 約 10 倍餘裕）而非復歸那邊的 2.0s：步進移動本身
# 可能只有幾毫秒，寬限期的代價是「出發前就壓在限位上」這種必敗情境要
# 多等才報錯，取短一點較合理。
MOVE_START_GRACE = 1.0     # 送出 GO CW/CCW 後容許 Driving 尚未 assert 的寬限期（秒）
MOVE_START_POLL = 0.05     # 寬限期內的快輪詢間隔（秒）
MOVE_POS_EPS = 1.0         # 「已走完預期行程」的容差（pulse），與量測路徑的 offset-1 同一慣例
MOVE_MOTION_EPS = 1.0      # 「POS 確實變化過」的門檻（pulse）

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
        "axis_calibration.json",
    }
)
# 連續幾輪讀不到任何軸的位置，就判定「畫面上的座標已不可信」。
# 3 輪 × POSITION_POLL_INTERVAL(0.5s) ≈ 1.5 秒，足以濾掉偶發的單次逾時。
# 可由 safety_settings.json 的 comm_fail_threshold 覆寫（範圍 1-10）。
COMM_FAIL_THRESHOLD = _safety_setting_num(
    _safety_settings, "comm_fail_threshold", 3, int, 1, 10
)

# 韌體軟體限位出廠／停用時，CWSLP?/CCWSLP? 查回來的哨兵值——不是真邊界。
# docs/hardware.md 實測記錄；sync_sw_limits_from_controller() 用它排除
# 「查得到數字但那個數字沒有意義」的情況，避免把 ±99999999 當成真的行程極限。
SW_LIMIT_SENTINEL = 99_999_999.0


# =============================================================================
# 原點復歸重現性量測——結果存檔（模組層級函式）
#
# 獨立於 DS102Controller.measure_homing_repeatability() 之外，由 GUI 端
# 量測結束後另外呼叫。這是 data/ 底下的實驗數據，跟 teaching_points.json
# 那類「整份覆蓋的累積型集合」不是同一種風險——每次都是全新的時間戳
# 檔名，不需要 _write_json_with_backup()／_points_loaded 那套拒寫保護。
# =============================================================================
def save_homing_repeat_result(result: dict) -> Tuple[str, str]:
    """
    把 measure_homing_repeatability() 的回傳結果寫入 data/ 目錄，
    回傳 (csv_path, json_path)（字串）。

    CSV 是長格式（每輪每筆一列），JSON 是完整 metadata + 統計摘要。
    檔名精確到秒，理論上不會撞名；萬一真的撞上（同一秒內呼叫兩次），
    用遞增後綴避免靜默覆蓋前一份資料，而不是直接蓋掉——這裡沒有
    _write_json_with_backup() 那層備份機制，覆蓋就是真的丟資料。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"homing_repeat_{ts}"
    csv_path = DATA_DIR / f"{base}.csv"
    json_path = DATA_DIR / f"{base}.json"
    n = 1
    while csv_path.exists() or json_path.exists():
        csv_path = DATA_DIR / f"{base}_{n}.csv"
        json_path = DATA_DIR / f"{base}_{n}.json"
        n += 1

    fieldnames = [
        "ts", "axis", "offset", "trial", "residual_pulse", "residual_um",
        "status", "left_switch", "on_sensor", "direction", "org_type",
        "origin_lost", "offset_below_switch", "homed_off_sensor", "memsw7",
        "note",
    ]
    with open(csv_path, "x", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for combo in result.get("combos", []):
            samples = combo.get("samples", [])
            last_idx = len(samples) - 1
            if not samples:
                # n_samples=0 的組合（多半正是 origin_lost 那些最重要的
                # 失敗記錄，例如基準復歸成功但第一輪一開始就撞限位）
                # 以前一列都不會輸出到 CSV，只存在於 JSON——這裡補一列
                # 只有 axis/offset/note 的紀錄，不讓失敗案例在 CSV 裡
                # 完全消失。
                writer.writerow(
                    {
                        "ts": "",
                        "axis": combo.get("axis", ""),
                        "offset": combo.get("offset", ""),
                        "trial": "",
                        "residual_pulse": "",
                        "residual_um": "",
                        "status": "",
                        # 零樣本代表連一輪都沒收集到 left_switch（連離開
                        # 移動的等待都沒成功過），留空而不是猜一個值——
                        # DictWriter 只要求欄位存在，不要求非空。
                        "left_switch": "",
                        "on_sensor": "",
                        "direction": combo.get("direction", ""),
                        "org_type": combo.get("org_type", ""),
                        "origin_lost": combo.get("origin_lost", ""),
                        "offset_below_switch": combo.get("offset_below_switch", ""),
                        "homed_off_sensor": combo.get("homed_off_sensor", ""),
                        "memsw7": combo.get("memsw7", ""),
                        "note": combo.get("note", ""),
                    }
                )
                continue
            for i, s in enumerate(samples):
                note_parts = [s["note"]] if s.get("note") else []
                if i == last_idx and combo.get("note"):
                    note_parts.append(combo["note"])
                p = s.get("residual_pulse")
                um = s.get("residual_um")
                writer.writerow(
                    {
                        "ts": s.get("ts", ""),
                        "axis": combo.get("axis", ""),
                        "offset": combo.get("offset", ""),
                        "trial": s.get("trial", ""),
                        "residual_pulse": p if p is not None else "",
                        "residual_um": f"{um:.5f}" if um is not None else "",
                        "status": s.get("status", ""),
                        # 每一輪自己的到位結果（M1/M2 的免費副產品）：這一
                        # 輪離開移動結束時是否已經脫離出發側限位開關。
                        "left_switch": s.get("left_switch", ""),
                        # 這一輪復歸後是否停在原點/限位感測器上。False 代表
                        # 復歸結束時軸不在任何感測器上，殘差意義存疑。
                        "on_sensor": s.get("on_sensor", ""),
                        "direction": combo.get("direction", ""),
                        "org_type": combo.get("org_type", ""),
                        # 跟 axis/offset/direction/org_type 一樣是組合層級
                        # 的中繼資料，每一列都重複填、不是只填最後一列——
                        # 這樣用 pandas 之類工具依 axis/offset group-by
                        # 時每一列都拿得到完整資訊，不需要另外找最後一列。
                        "origin_lost": combo.get("origin_lost", ""),
                        "offset_below_switch": combo.get("offset_below_switch", ""),
                        "homed_off_sensor": combo.get("homed_off_sensor", ""),
                        "memsw7": combo.get("memsw7", ""),
                        "note": "；".join(note_parts),
                    }
                )

    with open(json_path, "x", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(f"復歸重現性量測結果已存檔: {csv_path.name} / {json_path.name}")
    return str(csv_path), str(json_path)


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
        # 🔴 對這台滑台裝的 AMS（微步進）型驅動器沒有意義——手冊明講 MS 型
        # 驅動器的細分是打開外殼調實體旋轉開關，:DRDIV 指令對它不生效，控制
        # 器也沒有電路能讀回實體開關位置（2026-08-18 實測：開關轉到 6，這裡
        # 查回來依然是 0，查到的只是沒人寫過的軟體暫存器）。連線時查一次、
        # 純資訊性顯示，不做任何 pulse→um 換算——RESOLUT? 實測回傳 1，代表
        # 控制器沒有配置真實尺度，貿然乘除是未經驗證的假設（同一理由，
        # 2026-08-05 拿掉了 um/mm 單位切換，見 CLAUDE.md）。
        self.axis_drdiv: Dict[str, str] = {}
        # 軸機械校正參數（螺桿導程 / 馬達步進角 / 使用者手動設定的分割倍數）。
        # 純粹用來把 pulse「額外」估算成 μm 顯示——不影響任何移動、限位、
        # 教點比對邏輯，那些永遠只認 pulse。只有參數完整的軸才會是這個
        # 字典的 key（不像 sw_limits 六軸都預先擺好 (None, None)）。
        self.axis_calib: Dict[str, dict] = {}
        # 同 _points_loaded：沒載入就存檔會把既有校正參數整份蓋掉
        self._axis_calib_loaded = False
        # 通訊健康度：連續讀不到位置的次數，與上次成功的時間戳。
        # 用來讓畫面能區分「這是即時值」與「這是停住的舊值」。
        self.comm_failures = 0
        self.last_position_ok = 0.0

        # 連線時偵測到「復歸樣式未設定」的軸（MEMSW0=0）。
        # MEMSW 是 RAM-only，控制器斷電後會全部歸零。
        self.homing_unconfigured: List[str] = []
        # 本次連線實際從韌體同步了哪些軸的程式端行程限制說明（供 GUI 顯示）。
        # 見 sync_sw_limits_from_controller()。
        self.sw_limits_synced: List[str] = []
        # 兩層限位（韌體軟限位／程式端 sw_limits）合併後仍完全無保護的軸名。
        self.sw_limits_unprotected: List[str] = []
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

        # 原點復歸重現性量測鎖定旗標（見 measure_homing_repeatability）。
        # 比照 scanning_active 的既有模式，擋 GUI 手動操作與背景輪詢，
        # 但絕對不可以放進 _do_move_step()／_do_origin()（那兩個是無守衛
        # 層，量測方法自己要呼叫它們）——scanning_active 進 move_step
        # 守衛曾經導致所有收斂測試卡死（scanner 呼叫自己的移動被自己設的
        # 旗標擋住），這是同一個陷阱的第三次翻版，不能再犯。
        self.measuring_active = False

        # 點動結束訊號：放開按鈕（stop）時設起，讓限位監看執行緒收工
        self._jog_stop = threading.Event()
        self._jog_stop.set()

        # 序列埠移動動作巢狀計數器（供 motion_active property 使用）。
        # 用計數器而非 bool 是因為 origin_all 會巢狀呼叫 move_origin，
        # 必須等最外層也離開才算真正結束。刻意不共用 self._lock
        # （那把鎖保護 _positions_pulse/_offsets，是熱路徑，不擴大其責任）。
        self._motion_depth = 0
        self._motion_lock = threading.Lock()

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

    @contextlib.contextmanager
    def _motion_scope(self):
        """
        標記「目前有一段序列埠移動動作正在進行」的內部 context manager。

        用巢狀計數器（_motion_depth）而非單純 bool，是因為 origin_all()
        會巢狀呼叫 move_origin()——用 bool 的話內層 move_origin 結束時會
        把旗標提前清成 False，此時 origin_all 其實還沒收尾。只給
        motion_active property 讀，不對外公開、不做任何移動守衛判斷
        （守衛邏輯各自沿用既有的 playback_running/scanning_active 檢查）。
        """
        with self._motion_lock:
            self._motion_depth += 1
        try:
            yield
        finally:
            with self._motion_lock:
                self._motion_depth -= 1

    @property
    def motion_active(self) -> bool:
        """
        是否有任何序列埠移動動作正在進行（涵蓋點動/步進/原點復歸/重播/尋光）。

        🔴 只給「要不要占用其他硬體資源（如 GPIB）」的判斷用，絕對不可拿來
        當移動守衛（不可放進 move_step/move_continue/goto_point 的守衛條件）。
        scanning_active 進 move_step 的守衛曾經導致所有收斂測試卡死
        （scanner 呼叫自己的移動被自己設的旗標擋住），這裡是同一個陷阱的翻版。
        """
        return (
            self.playback_running
            or self.scanning_active
            or not self._jog_stop.is_set()
            or self._motion_depth > 0
        )

    def _manual_ops_blocked(self) -> bool:
        """
        使用者／GUI 發起的移動入口是否應被擋下（EMS／重播／尋光／量測任一
        進行中）。2026-09 起收斂 move_continue/move_step/move_origin/
        origin_all/goto_point 五處原本逐字相同的守衛條件，機械性去重、
        零行為變更。

        🔴 只給上述五個「使用者發起」的入口用。
        🔴 絕對不可用於 scan_move_step()／_do_move_step()／_do_origin()——
           那三個是無守衛層，把 scanning_active 放進去曾讓所有收斂測試卡死
           （scanner 呼叫自己的移動被自己設的旗標擋住），見 motion_active
           docstring 同一個陷阱。
        🔴 不要把 motion_active／_motion_depth 併進來（那是「要不要占用其他
           硬體資源」的判斷，語意不同），也不要為這四個旗標加鎖——維持既有
           的非原子讀取語義，加鎖只會引入新的取鎖順序問題。
        """
        return (
            self.ems_active
            or self.playback_running
            or self.scanning_active
            or self.measuring_active
        )

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
        # 韌體軟體限位是否停用（出廠／斷電即如此）決定了 Python 端
        # sw_limits 是否有東西可用——上面 restore_controller_config()
        # 只在「設定檔記錄為啟用」時才補寫回控制器，這裡要在那之後、
        # 讀取還原後的最終狀態同步進 sw_limits，順序不可調換。
        self.sw_limits_synced = self.sync_sw_limits_from_controller()

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
        # 中斷連線也要確保點動監看執行緒收工，否則 _jog_stop 會卡在
        # clear() 狀態，motion_active 永遠回報 True（見 emergency_stop 同一類前置缺陷）。
        self._jog_stop.set()
        # connected 必須先設 False 再關 port：兩者順序顛倒會製造一段「其他
        # 執行緒看得到 connected=True，但 ser 已關」的窗口（architect
        # 2026-09-03 審查抓到）。「已宣告不再使用」應先於「實際釋放資源」。
        self.connected = False
        if self.ser and self.ser.is_open:
            self.ser.close()
        self._log("INFO", "已中斷連線")

    # =========================================================================
    # 軟體行程限制
    # =========================================================================
    def sync_sw_limits_from_controller(self) -> List[str]:
        """
        連線時把韌體軟體限位（CWSLP?/CCWSLP?）同步進程式端 `self.sw_limits`。

        背景：`_check_sw_limit()` 比對的 `self.sw_limits` 預設六軸皆為
        `(None, None)`（無保護），只有使用者在〈軟體行程限制〉卡片手動輸入
        並套用才會有值——這治不好「預設沒保護」的病根，因為韌體軟體限位
        本身出廠／斷電後就是停用的，此時查回來的 CWSLP?/CCWSLP? 是哨兵值
        `SW_LIMIT_SENTINEL`（±99999999），不是真邊界。這支方法做的是把
        「兩層限位彼此同步」＋「兩層都沒保護時主動告知」，讓不可見的
        無保護狀態變成可見的（architect 評估結論，見呼叫端 CLAUDE.md 條目）。

        合併規則只收緊、永不放寬、永不清空——這是安全不變量，不是效能考量：
        使用者手動設定的值代表明確意圖，不能被「韌體這次查不到／查到停用」
        這種訊號覆寫掉；韌體有更嚴格的值時才收緊，兩者都有值但不同時取
        較保守的一側（CCW 取較大值、CW 取較小值，兩者都是讓行程範圍變窄
        的方向）。

        回傳：本次實際同步（採用了韌體值、或韌體值與既有值不同而取嚴格側）
        的軸／側說明清單，同時寫入 `self.sw_limits_synced`；完全沒有保護的
        軸名寫入 `self.sw_limits_unprotected`。未連線時直接回傳空清單。
        """
        if not self.connected:
            return []

        synced: List[str] = []
        unprotected: List[str] = []

        for i in range(self.axis_count):
            n = str(i + 1)
            ax = NO_AXIS.get(n)
            if not ax:
                continue

            ccw_lim, cw_lim = self.sw_limits.get(ax, (None, None))
            current_by_side = {"CCW": ccw_lim, "CW": cw_lim}
            final_by_side: Dict[str, Optional[float]] = {}

            for side, le_suffix, lp_suffix in (
                ("CCW", "CCWSLE?", "CCWSLP?"),
                ("CW", "CWSLE?", "CWSLP?"),
            ):
                current = current_by_side[side]
                fw_val: Optional[float] = None

                le_resp = self._serial_write_read(f"AXI{n}:{le_suffix}").strip()
                if le_resp not in ("0", "1"):
                    self._log(
                        "ERROR",
                        f"軸 {ax} {le_suffix} 回應非預期（{le_resp!r}），"
                        f"視為該側無有效邊界",
                    )
                elif le_resp == "1":
                    lp_resp = self._serial_write_read(f"AXI{n}:{lp_suffix}").strip()
                    try:
                        v = float(lp_resp)
                    except ValueError:
                        self._log(
                            "ERROR",
                            f"軸 {ax} {lp_suffix} 回應非數字（{lp_resp!r}），"
                            f"視為該側無有效邊界",
                        )
                    else:
                        if abs(v) >= SW_LIMIT_SENTINEL:
                            self._log(
                                "INFO",
                                f"軸 {ax} {side} 韌體限位為哨兵值 {v:.0f}"
                                f"（停用狀態），視為無有效邊界",
                            )
                        else:
                            fw_val = v
                # le_resp == "0"：該側韌體限位本來就停用，fw_val 維持 None，
                # 這是正常情況、不需要記 log。

                if current is None and fw_val is not None:
                    # 程式端原本沒有保護，韌體有 → 採用韌體值
                    final = fw_val
                    synced.append(f"{ax} {side}={final:.0f}")
                elif current is None and fw_val is None:
                    # 兩邊都沒有 → 維持無保護
                    final = None
                elif current is not None and fw_val is None:
                    # 使用者已手動設定，韌體這次查不到有效邊界
                    # → 維持使用者的值不動，絕不清空
                    final = current
                else:
                    # 兩邊都有值 → 取較嚴格的一側（CCW 取較大值、CW 取
                    # 較小值，兩者皆為讓行程範圍變窄的方向）
                    final = max(current, fw_val) if side == "CCW" else min(current, fw_val)
                    if fw_val != current:
                        self._log(
                            "INFO",
                            f"軸 {ax} {side} 韌體限位（{fw_val:.0f}）與程式端既有值"
                            f"（{current:.0f}）不同，取較嚴格者 {final:.0f}",
                        )
                        # 只有真的採用了韌體值（韌體較嚴格）才算「同步」；
                        # 韌體較寬鬆時 final 維持 current 不變，若仍列進
                        # synced 會讓橫幅謊報「已同步」成一個實際上被
                        # 拒絕採用的數字（architect 審查抓到）。
                        if final != current:
                            synced.append(f"{ax} {side}={final:.0f}")

                final_by_side[side] = final

            # 整個 tuple 一次指派——這是跨執行緒共用的屬性，寫入要保持
            # 原子觀感一致，不要逐欄位改讓其他執行緒讀到「寫一半」的狀態。
            self.sw_limits[ax] = (final_by_side["CCW"], final_by_side["CW"])

            if final_by_side["CCW"] is None and final_by_side["CW"] is None:
                # 未接滑台的軸本來就不會被驅動，不需要行程保護，列進
                # 「沒有保護」警示只會是假警報、稀釋真正的警示可信度
                # （比照 restore_controller_config()／check_homing_config()
                # 既有的「未接滑台一律跳過」慣例，architect 審查抓到）。
                # 只在真的會加進清單時才查，其餘軸不多花這筆往返。
                st, _ = self.query_status(n)
                if st != "Stage not connected":
                    unprotected.append(ax)

        self.sw_limits_synced = synced
        self.sw_limits_unprotected = unprotected

        if unprotected:
            self._log(
                "WARN",
                f"軸 {'、'.join(unprotected)} 沒有任何行程保護"
                f"（韌體限位停用、程式端未設定），長按點動只靠機械限位擋",
            )
        if synced:
            self._log(
                "INFO",
                f"已從韌體限位同步程式端行程限制：{'、'.join(synced)}",
            )

        return self.sw_limits_synced

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
        if self._manual_ops_blocked():
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
        if self._manual_ops_blocked():
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

        # 用 _motion_scope 標記「這段期間有序列埠移動動作」，供
        # motion_active property 讀取（目前用途：暫停光功率背景輪詢，
        # 避免馬達震動期間讀到不可信的雜訊值）。
        with self._motion_scope():
            ax = NO_AXIS.get(axis_no)
            # 送出前的機械座標，供 _wait_axis_stop() 的位移證據當基準
            # （見該函式 docstring 的「孿生競態」段）。取的是快取值而非
            # 另打一筆 POS?：熱路徑上多一次往返約 56ms，而快取在每次
            # 移動結束時都被 query_status() 寫成當下實測值，起點是準的。
            cur: Optional[float] = None
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
                return self._wait_axis_stop(
                    axis_no, start_pos=cur, expected_travel=pulse_amt
                )
            return True

    def _do_origin_ex(
        self,
        axis_no: str,
        org_type: int,
        l_speed: str,
        f_speed: str,
        rate: str,
        s_rate: str,
        wait_done: bool = True,
        abort_event: Optional[threading.Event] = None,
    ) -> Tuple[bool, str]:
        """
        `_do_origin()` 的完整版，回傳 (是否成功, 原因代碼)。

        原因代碼直接來自 `_wait_origin_done_ex()`（`"ok"`／
        `"ok_no_driving_seen"`／`"not_executed"`／`"timeout"`／`"ems"`／
        `"aborted"`），呼叫端據此寫出能分辨成因的訊息——把「復歸沒有實際
        執行」（該去查 MEMSW0 樣式）誤報成「逾時」（會讓人去查通訊或速度）
        會把事後判讀導向完全錯誤的方向。

        🔴 送出 `GO ORG` 之後補了 `time.sleep(0.1)`，比照 `origin_all()`
        既有的做法。改動前 `_do_origin()` 少了這一步，是它比 `origin_all()`
        更容易踩到「Driving 尚未 assert」競態的直接原因（實測 assert 延遲
        ~96ms，而少了這個 sleep 時第一次 `SB1?` 只要 ~56ms 就回來了）。

        🔴 `wait_done=False` 這條路徑**無從驗證**復歸有沒有真的發生——
        沒有等待就沒有任何證據可收集。呼叫端自負，量測路徑一律用
        `wait_done=True`。
        """
        with self._motion_scope():
            raw = self._serial_write_read(f"AXI{axis_no}:POS?")
            try:
                pos_before: Optional[float] = float(raw)
            except (ValueError, TypeError):
                pos_before = None

            self._serial_write(f"AXI{axis_no}:MEMSW0 {org_type}")
            time.sleep(0.1)
            cmd = (
                f"AXI{axis_no}:L0 {l_speed}:R0 {rate}"
                f":S0 {s_rate}:F0 {f_speed}:GO ORG"
            )
            self._serial_write(cmd)
            self._log("INFO", f"原點返回 軸{axis_no} ORG{org_type}", tx=cmd)
            time.sleep(0.1)  # 比照 origin_all，給控制器啟動時間
            if not wait_done:
                return True, "not_waited"
            return self._wait_origin_done_ex(
                axis_no, abort_event=abort_event, pos_before=pos_before
            )

    def _do_origin(
        self,
        axis_no: str,
        org_type: int,
        l_speed: str,
        f_speed: str,
        rate: str,
        s_rate: str,
        wait_done: bool = True,
        abort_event: Optional[threading.Event] = None,
    ) -> bool:
        """
        `_do_origin_ex()` 的薄 bool wrapper（既有呼叫點不必改簽章）。

        原點復歸核心動作的無守衛實作：送出 MEMSW0 + GO ORG，
        wait_done=True 時等待復歸完成。

        不讀 POS、不做「復歸後未歸零就強制寫 0」的判斷——那是
        `move_origin()` 自己的收尾邏輯。守衛（ems_active/playback_running/
        scanning_active/measuring_active）一律交給呼叫端：這是
        CLAUDE.md 記載過兩次的陷阱翻版（`scanning_active` 進 `move_step`
        守衛曾讓演算法自己的移動被自己設的旗標擋住），
        `measure_homing_repeatability()` 需要直接呼叫這個無守衛版本，
        不能被自己設的 `measuring_active` 擋住。

        `abort_event`：供量測方法用，讓使用者中止量測時能讓這裡的等待
        提前結束，不必等滿 180s 逾時；為 None 時完全不檢查，
        行為與改動前的 `move_origin()` 一致。
        """
        return self._do_origin_ex(
            axis_no, org_type, l_speed, f_speed, rate, s_rate,
            wait_done=wait_done, abort_event=abort_event,
        )[0]

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

        核心的「送出 MEMSW0+GO ORG 並等待」抽到 `_do_origin()`（無守衛層，
        `measure_homing_repeatability()` 直接呼叫它），這裡維持原本
        「復歸後檢查 POS、視情況強制歸零」的收尾邏輯逐字不變。
        """
        if self._manual_ops_blocked():
            return False
        # 用 _motion_scope 標記移動期間（見 _do_move_step 同一段註解）。
        # origin_all 會巢狀呼叫本方法，_motion_scope 用計數器正確處理巢狀。
        with self._motion_scope():
            ax = NO_AXIS.get(axis_no, axis_no)
            ok, reason = self._do_origin_ex(
                axis_no, org_type, l_speed, f_speed, rate, s_rate, wait_done=wait_done
            )
            if not ok:
                # 分辨「逾時」與「復歸未實際執行」：前者會讓人去查通訊或
                # 速度，後者才會讓人去查 MEMSW0 樣式（2026-08-21 實機驗證
                # 抓到 Z 軸 MEMSW0=1 時 GO ORG 完全無作用）。
                if reason == "not_executed":
                    detail = "復歸未實際執行，請確認 MEMSW0 樣式是否適用該軸"
                    self._log("ERROR", f"軸 {ax} {detail}")
                else:
                    detail = "原點復歸未在時限內完成"
                    self._log("ERROR", f"軸 {ax} 原點復歸未完成（{reason}）")
                if self._alarm_cb:
                    self._alarm_cb(f"軸 {ax} 復歸未完成", detail)
                return False
            if not wait_done:
                return True

            _, pos = self.query_status(axis_no)
            try:
                if abs(float(pos)) < 0.5:
                    return True
            except (ValueError, TypeError):
                pass
            # 🔴 寫 POS 0 之前先確認軸真的停穩了，不能只信上面的等待函式
            # 回報完成（見 _confirm_stopped 的 docstring）。
            stopped, _ = self._confirm_stopped(axis_no)
            if not stopped:
                self._log(
                    "ERROR",
                    f"軸 {ax} 復歸回報完成但軸仍在移動，未強制歸零——"
                    f"座標系可能不準確，請重新執行原點復歸",
                )
                return False
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

    def _restore_soft_limits(self, axis_no: str, saved: Tuple[str, str]) -> None:
        """
        還原單軸韌體軟體限位（origin_all / measure_homing_repeatability 的
        finally 共用）。讀不到原值一律還原成啟用(1)，絕不 fallback 到停用：
        `cw or '0'` 這種寫法在 _serial_write_read 三次失敗回傳空字串時會變成
        停用，等於序列埠壅塞一下就把韌體端唯一可靠的保護永久關掉，且不留
        痕跡。保護該有的失效方向是「寧可多擋」，不是「寧可放行」。
        """
        cw, ccw = saved
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

    def _wait_origin_done_ex(
        self,
        axis_no: str,
        timeout: float = 180.0,
        abort_event: Optional[threading.Event] = None,
        pos_before: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        等待原點復歸結束，回傳 (是否視為完成, 原因代碼)。

        不能沿用 _wait_axis_stop()：復歸樣式 5/6 本來就是靠偵測限位感測器
        的邊緣來定位，途中壓到限位是正常流程而非異常。復歸可能橫跨整個
        行程，所以逾時比一般移動寬鬆得多。

        2026-08-21 COM2 實機驗證抓到：Z 軸送出 `GO ORG` 後第一次 `SB1?`
        往返約 56ms，但 Driving 位元要 +96ms 才真正 assert。舊版「非
        Driving 即完成」的語意會在復歸根本還沒開始時就回報成功——實測
        序列：`GO ORG` 送出 → 第一次 `SB1?` 回 `10`（bit6 未 set）→ 立刻
        `return True` → 下游 `set_position(axis_no, "0")` 把座標系原點
        寫在滑台正要開始飛的那一刻 → 下一次 `SB1?` 才回 `66`（0x42，
        Driving+limit，復歸這時才真的開始）。

        🔴 決定會不會踩到這個競態的是**呼叫端第一次查詢有多快**，不是
        指令種類（實測三種起始條件的 assert 延遲都是 80～96ms，見模組
        頂端 ORIGIN_START_GRACE 附近的數據）：
          - `_do_origin()` 送出後只打一次 `SB1?`（~56ms）→ 穩定落在
            96ms 之前 → **必然**踩中。
          - `_wait_axis_stop()` 第一次走 `query_status()`，要 `SB3?`+
            `SB1?` 兩次往返（~112ms）→ 剛好越過 96ms → 大多數時候僥倖
            避開，餘裕只有約 16ms。那是同一個競態的孿生體，尚未修，
            見 CLAUDE.md 的技術債紀錄。

        判定改成**三重證據**：
          1. `saw_driving`——輪詢期間看過 Driving 位元 assert 過，是唯一
             正常路徑（`return True, "ok"`）。
          2. 若從未看過 Driving，先給 `ORIGIN_START_GRACE` 秒的寬限期，
             容許「還沒來得及 assert」這個已知的實測延遲（避免用單一
             0.5s 輪詢節奏就誤判成沒動）。
          3. 寬限期過後仍沒看過 Driving，才退而求其次比對 POS：位移超過
             `ORIGIN_MOTION_EPS` 視為「復歸極短、輪詢真的漏接」
             （`return True, "ok_no_driving_seen"`，記 WARN）；位移也沒有
             就是「復歸根本沒有實際執行」（`return False, "not_executed"`，
             記 ERROR——這才是本次實機問題的真正根因，常見原因是 MEMSW0
             樣式對這顆感測器配置不適用）。

        兩種證據用 OR 而非各自獨立判斷：只看 Driving 會被 0.5s 輪詢節奏
        漏掉極短的復歸；只看 POS 位移會把「本來就在原點、復歸原地不動」
        誤判成失敗。兩者 OR 之後，只有「Driving 從沒 assert 且 POS 完全
        沒動」才判失敗——物理上就是什麼都沒發生。寬限期只在「從未看過
        Driving」這條路徑上才會多付出一次 POS? 查詢的成本，正常復歸的
        輪詢節奏與通訊量完全不變。

        `pos_before`：呼叫端已經讀過一次起始位置時可以直接傳入，省一次
        查詢；為 None 時在這裡自己讀一次，讓 `origin_all()` 這類不方便
        先讀 POS 的呼叫端也能自動受益。讀不到（通訊失敗）時位移證據這
        條路徑會直接失效，只能靠 Driving 證據——不會因此誤報成功。

        `abort_event`：供 measure_homing_repeatability() 用，讓使用者按
        「停止量測」時能提前結束等待，不必空等到 180s 逾時。為 None 時
        完全不檢查，行為與改動前一致。
        """
        if pos_before is None:
            raw = self._serial_write_read(f"AXI{axis_no}:POS?")
            try:
                pos_before = float(raw)
            except (ValueError, TypeError):
                pos_before = None  # 讀不到起始位置，位移證據那條路徑會直接失效

        start_time = time.time()
        deadline = start_time + timeout
        saw_driving = False
        while time.time() < deadline:
            if self.ems_active:
                return False, "ems"
            if abort_event is not None and abort_event.is_set():
                return False, "aborted"

            sb1 = self._serial_write_read(f"AXI{axis_no}:SB1?")
            try:
                driving = bool(int(sb1) & 0x40)  # bit6
            except (ValueError, TypeError):
                driving = False

            if driving:
                saw_driving = True
                time.sleep(WAIT_INTERVAL)
                continue

            if saw_driving:
                return True, "ok"

            if time.time() - start_time < ORIGIN_START_GRACE:
                # 還在寬限期內，Driving 可能只是尚未 assert——繼續等，
                # 不要在這裡就下任何結論。這裡用 ORIGIN_START_POLL 而非
                # WAIT_INTERVAL：實測 assert 延遲只有 ~96ms，0.5s 的節奏
                # 在 2 秒寬限期內只取樣 4 次，解析度不足。
                time.sleep(ORIGIN_START_POLL)
                continue

            # 寬限期已過仍沒看過 Driving，退而求其次比對 POS 是否變化過。
            raw = self._serial_write_read(f"AXI{axis_no}:POS?")
            try:
                pos_now = float(raw)
            except (ValueError, TypeError):
                pos_now = None

            if (
                pos_before is not None
                and pos_now is not None
                and abs(pos_now - pos_before) > ORIGIN_MOTION_EPS
            ):
                self._log(
                    "WARN",
                    f"軸{axis_no} 復歸期間未偵測到 Driving 旗標，但 POS 有"
                    f"變化（{pos_before}→{pos_now}），視為已完成",
                )
                return True, "ok_no_driving_seen"

            self._log(
                "ERROR",
                f"軸{axis_no} 復歸未實際執行——Driving 未 assert 且 POS 未"
                f"變化，請確認 MEMSW0 樣式是否適用該軸",
            )
            return False, "not_executed"

        self._log("WARN", f"軸{axis_no} 原點復歸逾時（{timeout}s）")
        return False, "timeout"

    def _wait_origin_done(
        self,
        axis_no: str,
        timeout: float = 180.0,
        abort_event: Optional[threading.Event] = None,
    ) -> bool:
        """`_wait_origin_done_ex()` 的薄 bool wrapper，既有呼叫點不必改簽章。"""
        return self._wait_origin_done_ex(axis_no, timeout, abort_event)[0]

    def _confirm_stopped(
        self,
        axis_no: str,
        checks: int = 3,
        interval: float = 0.1,
    ) -> Tuple[bool, Optional[float]]:
        """
        連續 `checks` 次確認：狀態非 Driving，且 POS 在這段期間完全沒變。
        回傳 (是否確認靜止, 最後讀到的 POS)。

        🔴 專門守在任何 `set_position(axis_no, "0")` 之前。2026-08-21 COM2
        指令追蹤證實 `_wait_origin_done()` 會在軸尚未起步時回報完成，導致
        `POS 0` 被寫在飛行途中——把座標系原點悄悄搬到滑台當下的位置，之後
        goto 教點、`sw_limits` 比對、`estimate_um()` 全部跟著偏移且零警告。

        `_wait_origin_done_ex()` 的三重證據已經把根因堵住了，但那仍然是
        「相信上游」的架構：任何一條新的呼叫路徑、或未來對等待函式的改動，
        都可能繞過它。把確認放在**危險動作本身**才是機制性保證，這是本專案
        第三次在同一個模式上出事後（`_wait_axis_stop` 誤判、`_wait_origin_done`
        誤判、`POS 0` 寫在飛行中）該有的防線。

        用「POS 連續不變」而非只看 Driving 位元，理由同 `_wait_origin_done_ex`：
        Driving 有 ~96ms 的 assert 延遲，單看它會把「還沒起步」讀成「已停好」。
        POS 是實際位移的直接證據，沒有這個延遲。
        """
        last: Optional[float] = None
        stable = 0
        for _ in range(max(1, checks) * 4):   # 上限：避免軸持續移動時無限等待
            status, pos_s = self.query_status(axis_no)
            try:
                pos = float(pos_s)
            except (ValueError, TypeError):
                # 讀不到位置就無從確認靜止——寧可判失敗也不要放行寫入
                return False, last
            if status == "Driving":
                stable = 0
                last = pos
                time.sleep(interval)
                continue
            if last is not None and pos == last:
                stable += 1
                if stable >= checks:
                    return True, pos
            else:
                stable = 0
            last = pos
            time.sleep(interval)
        return False, last

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
        if self._manual_ops_blocked():
            return False, "EMS 作用中／重播進行中／尋光進行中／量測進行中，已略過"

        done, skipped, failed = [], [], []
        saved: Dict[str, Tuple[str, str]] = {}

        # 用 _motion_scope 標記整批復歸期間（見 _do_move_step 同一段註解）。
        # 計數器設計讓這裡即使巢狀呼叫到其他也會進入 _motion_scope 的
        # 移動方法也不會提早清空旗標。
        with self._motion_scope():
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

                    ok_org, reason_org = self._wait_origin_done_ex(axis_no)
                    if not ok_org:
                        # 分辨兩種成因：「逾時」讓人去查通訊/速度，
                        # 「未實際執行」才會讓人去查 MEMSW0 樣式。
                        if reason_org == "not_executed":
                            failed.append(f"{ax}(復歸未實際執行)")
                        else:
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
                        # 🔴 寫入前先確認軸真的停穩（見 _confirm_stopped）。
                        stopped, _ = self._confirm_stopped(axis_no)
                        if not stopped:
                            self._log(
                                "ERROR",
                                f"軸 {ax} 復歸回報完成但軸仍在移動，未強制歸零",
                            )
                            failed.append(f"{ax}(復歸後仍在移動，未歸零)")
                            continue
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
                # 無論成功與否都要把軟體限位還原回去，見 _restore_soft_limits
                # docstring（保護該有的失效方向是「寧可多擋」，不是「寧可放行」）。
                for axis_no, saved_limits in saved.items():
                    self._restore_soft_limits(axis_no, saved_limits)

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

    # =========================================================================
    # 原點復歸重現性量測（2026-08-21）
    #
    # 自動化原本要人工用碼表做的量測：讓軸離開原點固定 pulse 數、送
    # GO ORG 復歸、讀取復歸後 POS 殘差，重複多輪並掃描多個離開距離，
    # 統計殘差離散程度——用來評估「軟體座標原點」能否當作光纖對準的
    # 可信基準。純量測，不寫檔；落地存檔交給 save_homing_repeat_result()
    # （模組層級函式，GUI 端量測結束後另外呼叫）。
    # =========================================================================
    def measure_homing_repeatability(
        self,
        axes: List[str],
        offsets: List[int],
        trials: int,
        l_speed: str,
        f_speed: str,
        rate: str,
        s_rate: str,
        directions: Optional[Dict[str, str]] = None,
        progress_cb: Optional[Callable] = None,
        combo_done_cb: Optional[Callable] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> dict:
        """
        對每個軸、每個離開距離（offset）重複 trials 輪「離開→復歸→讀殘差」，
        回傳整體結果 dict（GUI 端據此存檔、畫摘要）。

        directions 缺該軸 key 表示要自動判定：查一次 query_status()，若
        當下正壓在某側限位（limit_direction() 判得出來），departure 方向
        取相反方向——原點復歸後座標幾乎必然停在某一側限位附近（見
        CLAUDE.md〈座標 0 幾乎就落在限位開關上〉），這是唯一站得住腳的
        自動判定依據；判不出來（此刻沒有壓在任何限位上）就整批跳過該軸，
        改由 GUI 提示使用者手動指定。

        🔴 stop_event 被 set 時，不論卡在哪個階段，都保證會執行到
        _measure_one_combo 的 try/finally 收尾判斷，但收尾**不是**無條件
        歸零：只有滑台確定在原點（`at_origin`）才寫 `POS 0`，否則不歸零、
        記 ERROR 並回傳 `origin_lost=True`。中止時把 `POS 0` 寫在滑台當下
        的任意位置，比不歸零更危險——見 _measure_one_combo 的 docstring。
        """
        if directions is None:
            directions = {}

        result: dict = {
            "axes_requested": list(axes),
            "offsets": list(offsets),
            "trials": trials,
            "speed": {"l_speed": l_speed, "f_speed": f_speed, "rate": rate, "s_rate": s_rate},
            "started_ts": datetime.now().isoformat(timespec="seconds"),
            "finished_ts": None,
            "aborted": False,
            "skipped_axes": [],
            "combos": [],
        }

        self.measuring_active = True
        try:
            for axis in axes:
                if self._homing_repeat_abort(stop_event):
                    break

                axis_no = AXIS_NO.get(axis)
                if not axis_no:
                    result["skipped_axes"].append({"axis": axis, "reason": "未知軸名"})
                    continue

                st, _ = self.query_status(axis_no)
                if st == "Stage not connected":
                    result["skipped_axes"].append({"axis": axis, "reason": "未接滑台"})
                    continue

                org_type_raw = self._serial_write_read(f"AXI{axis_no}:MEMSW0?").strip()
                if not org_type_raw or org_type_raw == "0":
                    result["skipped_axes"].append(
                        {"axis": axis, "reason": "復歸樣式未設定（MEMSW0=0 或讀取失敗）"}
                    )
                    continue
                try:
                    org_type = int(org_type_raw)
                except ValueError:
                    result["skipped_axes"].append(
                        {"axis": axis, "reason": f"MEMSW0 回應格式錯誤: {org_type_raw!r}"}
                    )
                    continue

                ax_name = NO_AXIS.get(axis_no, axis)
                # 只記錄、不改寫：MEMSW7=0 才會讓控制器在復歸完成後自動把
                # POS 歸零（見 origin_all 同段說明），附進每組合 metadata
                # 供事後判讀，不影響量測邏輯。
                axis_memsw7 = self._serial_write_read(f"AXI{axis_no}:MEMSW7?").strip()

                # 暫停該軸的韌體軟體限位——offset 可能把軸推到韌體限位以外
                # （比照 origin_all 既有規則：讀不到一律還原成 1／啟用，
                # 不可 fail-unsafe，見下方 finally）。
                saved_limits = self._soft_limits_enabled(axis_no)
                self._set_soft_limits_enabled(axis_no, False)
                try:
                    # 基準復歸：不能假設進場時 POS≈0（使用者可能剛手動點動
                    # 過，或方向是手動指定）。沒有這一步，第一組 offset 的
                    # 殘差會混進「進場時離原點多遠」的誤差：offset=100 但
                    # 起點離原點 3000 pulse，復歸後殘差 ≈3000 遠超漂移門檻
                    # （0.25×100=25），會被誤判成累積漂移（architect
                    # 2026-08-21 審查抓到）。
                    #
                    # 🔴 這次歸零定義了整組量測的座標框架，是全流程最重要的
                    # 一次寫入，故走四步驗證：驗證復歸真的執行過 → 確認軸
                    # 已停穩 → 寫 0 → 回讀確認。少了這幾步時基準復歸會踩到
                    # 「Driving 尚未 assert」競態、把 POS 0 寫在飛行途中，
                    # 整軸資料靜默作廢（實測 Y 軸因此多出 83 pulse 假性偏移）。
                    base_ok, base_reason = self._do_origin_ex(
                        axis_no, org_type, l_speed, f_speed, rate, s_rate,
                        abort_event=stop_event,
                    )
                    if not base_ok:
                        txt = (
                            f"基準復歸未實際執行（MEMSW0={org_type} 對本軸可能不適用）"
                            if base_reason == "not_executed"
                            else "基準復歸失敗"
                        )
                        result["skipped_axes"].append(
                            {"axis": axis, "reason": txt + "，未進行量測"}
                        )
                        continue
                    base_stopped, _ = self._confirm_stopped(axis_no)
                    if not base_stopped:
                        result["skipped_axes"].append(
                            {
                                "axis": axis,
                                "reason": "基準復歸後軸仍在移動，未寫入 POS 0，未進行量測",
                            }
                        )
                        continue
                    self.set_position(axis_no, "0")
                    p_back, _ = self._read_pos_consistent(axis_no)
                    if p_back is None or abs(p_back) > 1:
                        result["skipped_axes"].append(
                            {
                                "axis": axis,
                                "reason": f"基準歸零回讀失敗（POS={p_back}），未進行量測",
                            }
                        )
                        continue

                    # 方向自動判定搬到基準復歸之後（architect 2026-08-21
                    # 建議 N1）：判定依賴「當下正壓在某側限位」，剛復歸完的
                    # 軸必然壓在某側限位上，判定幾乎必定成功。放在復歸之前
                    # 的舊寫法，使用者若剛手動點動停在行程中間又沒指定方向，
                    # 會被誤判成無法自動判定而整軸跳過。`_do_origin()` 不需要
                    # `direction` 參數，搬動不影響基準復歸本身。
                    direction = directions.get(axis)
                    if direction not in ("CW", "CCW"):
                        st2, _ = self.query_status(axis_no)
                        limit_side = self.limit_direction(st2)
                        if limit_side not in ("CW", "CCW"):
                            result["skipped_axes"].append(
                                {"axis": axis, "reason": "無法自動判定方向且未手動指定"}
                            )
                            continue
                        # 離開方向＝目前所壓限位的反方向
                        direction = "CCW" if limit_side == "CW" else "CW"

                    self._log(
                        "INFO", f"[復歸重現性量測] 軸 {ax_name} 開始，方向 {direction}"
                    )

                    for offset in offsets:
                        if self._homing_repeat_abort(stop_event):
                            break
                        combo_result = self._measure_one_combo(
                            axis, axis_no, direction, org_type, offset, trials,
                            l_speed, f_speed, rate, s_rate, progress_cb, stop_event,
                        )
                        combo_result["memsw7"] = axis_memsw7
                        result["combos"].append(combo_result)
                        if combo_done_cb:
                            combo_done_cb(axis, offset, combo_result)
                        if combo_result.get("origin_lost"):
                            # 座標系已失準：這一軸剩下的 offset 既不可信
                            # （殘差在未歸零的參考框裡量，會誤報成累積漂移），
                            # 也不安全（下一組合若在第一輪之前就被中止，
                            # finally 會把 POS 0 寫在滑台當下任意位置）。整軸
                            # 收手讓使用者先手動復歸，只中止這一軸，其他軸
                            # 各自有自己的基準復歸不受影響。
                            result["skipped_axes"].append(
                                {
                                    "axis": axis,
                                    "reason": (
                                        f"offset={offset} 組合在非原點狀態中止，"
                                        f"座標系已失準，該軸剩餘 offset 已略過"
                                    ),
                                }
                            )
                            break
                        if self._homing_repeat_abort(stop_event):
                            break
                finally:
                    self._restore_soft_limits(axis_no, saved_limits)

                if self._homing_repeat_abort(stop_event):
                    break
        finally:
            self.measuring_active = False

        # 中止跟正常跑完不是同一回事：中止時已存的資料通常只涵蓋部分
        # 軸／offset，GUI 端要能分辨「完成」與「已中止，部分資料已存檔」，
        # 不能一律顯示「完成」（architect 2026-08-21 審查建議）。這裡直接
        # 讀 stop_event/ems_active 的目前狀態——兩者在中止路徑上都不會被
        # 這個函式自己清掉，能正確反映「是不是因為這兩個原因而提前結束」。
        result["aborted"] = self._homing_repeat_abort(stop_event)
        result["finished_ts"] = datetime.now().isoformat(timespec="seconds")
        self._log(
            "INFO" if not result["aborted"] else "WARN",
            "[復歸重現性量測] 已中止（部分資料）" if result["aborted"] else "[復歸重現性量測] 全部完成",
        )
        return result

    def _homing_repeat_abort(self, stop_event: Optional[threading.Event]) -> bool:
        """
        量測是否該立刻收手：使用者主動停止（stop_event）或 EMS 觸發，
        兩者都要讓迴圈立刻停手，缺一不可。

        🔴 EMS 觸發時 `_wait_axis_stop()`/`_wait_origin_done()` 會因
        `self.ems_active` 回 False，讓當下那個 (軸, offset) 組合中止，
        但這只影響 `_measure_one_combo()` 內部的 trial 迴圈——外層的
        axes／offsets 迴圈以前只認 `stop_event`，於是會繼續處理下一個
        組合／下一軸，重新呼叫 `_do_move_step()`/`_do_origin()` 送出新的
        移動指令，等於「使用者按下緊急停止後，滑台又動了起來」
        （architect 2026-08-21 審查抓到的安全問題）。

        ⚠ 這個方法只給**外層** `measure_homing_repeatability()` 的
        axes／offsets 迴圈入口用（回傳單純 bool 就夠判斷要不要 break）。
        `_measure_one_combo()` 內部**刻意不呼叫這個方法**、手動分開檢查
        `stop_event`／`self.ems_active`——因為它還要分辨兩者才能寫出
        正確的 `note`（「使用者中止」vs「EMS 觸發，中止量測」），合併成
        一個回 bool 的判斷式會弄丟這個區分能力。這是刻意的設計差異，
        不是遺漏，不要「順手統一」成都呼叫這個方法。
        """
        return bool(stop_event and stop_event.is_set()) or self.ems_active

    def _measure_one_combo(
        self,
        axis: str,
        axis_no: str,
        direction: str,
        org_type: int,
        offset: int,
        trials: int,
        l_speed: str,
        f_speed: str,
        rate: str,
        s_rate: str,
        progress_cb: Optional[Callable],
        stop_event: Optional[threading.Event],
    ) -> dict:
        """
        單一 (軸, offset) 組合：重複 trials 輪「離開→復歸→讀殘差」。

        呼叫端前提（由 measure_homing_repeatability() 保證，不在這裡重新
        驗證）：進入這個組合時滑台已確定在原點——來自呼叫端的基準復歸，
        或上一個 offset 組合自己歸零留下的結果。呼叫端同時保證：一旦
        某個組合回傳 `origin_lost=True`，該軸後續的 offset 就不會再呼叫
        這個方法（見 measure_homing_repeatability() 的 offsets 迴圈，
        `origin_lost` 為真時整軸收手），所以這裡不需要、也無法自行驗證
        這個前提是否成立。

        🔴 用 try/finally 包整段（而非提前 return）確保無論正常跑完、
        偵測到漂移提前收工、移動/復歸失敗、EMS 觸發、還是被 stop_event
        中止，都會執行到收尾判斷——但收尾**不是**無條件歸零。只有滑台
        「確定在原點」時才把座標寫回 0；離開移動失敗、`_do_origin`
        失敗或逾時、或被 EMS 中止時，滑台可能停在行程中的任意點，此時
        寫 `POS 0` 等於把座標系原點偷偷改到滑台當下位置——這比不歸零
        更危險（之後 goto 教點、限位比對全部跟著偏移，且沒有任何警告），
        是 architect 2026-08-21 審查抓到的問題。`at_origin` 這個旗標就是
        用來追蹤這件事：初始值 True（沿用呼叫端前提），只要移動/復歸/EMS
        任一步驟出狀況就設回 False，且一路維持到下一次 `_do_origin`
        再次成功為止。

        🔴 ems_active 檢查獨立於 stop_event 之外，且比 `_wait_axis_stop_
        leaving_limit()`/`_do_origin()` 內部既有的檢查更早、更完整——這
        兩個是無守衛層，`measuring_active` 只擋得住「其他來源啟動量測」，
        擋不住「量測進行中途 EMS 被觸發」，那必須由這個迴圈自己攔。EMS
        觸發後絕對不能再送出新的移動指令（哪怕是回原點的復歸），這是與
        外層 `_homing_repeat_abort()` 同一類問題的組合內版本。

        🔴 離開移動改用 `_do_move_step(..., wait_done=False)` +
        `_wait_axis_stop_leaving_limit()`，不能沿用 `_do_move_step(...,
        wait_done=True)`（等於內部呼叫 `_wait_axis_stop()`）——2026-08-21
        COM2 實機驗證：原點就落在限位開關上，限位開關有實體作用寬度
        （實測 X 軸 POS=100 時仍壓著 CCW 硬體限位，POS=150 才解除），
        offset 不夠大時離開移動走完仍會壓在出發側限位上，`_wait_axis_
        stop()` 依既有語意（非 Driving 就視為異常）會把這個正常起始
        條件誤判成撞限位失敗，導致量測一輪都跑不完——這跟 CLAUDE.md
        記載「原點復歸必須用 `_wait_origin_done()` 不能沿用 `_wait_axis_
        stop()`」是同一類問題。`_wait_axis_stop_leaving_limit()` 只給
        這條量測路徑用，`_do_move_step()`/`_wait_axis_stop()` 本身完全
        沒有改動——一般步進移動撞到限位永遠要是失敗，不能因為量測需要
        容忍就連帶放寬。
        """
        samples: List[dict] = []
        note = ""
        at_origin = True
        try:
            for trial in range(trials):
                if stop_event and stop_event.is_set():
                    note = "使用者中止"
                    break
                if self.ems_active:
                    note = "EMS 觸發，中止量測"
                    break

                # 出發位置不能假設是 0：第 2 輪以後的起點是上一輪的殘差
                # （只有第一輪才緊接在基準復歸之後）。
                p_start, _ = self._read_pos_consistent(axis_no)
                if p_start is None:
                    note = "讀不到出發位置（通訊異常），中止量測"
                    break  # 尚未送出移動 → at_origin 維持 True，finally 歸零正確

                leaving_side = "CCW" if direction == "CW" else "CW"
                # wait_done=False 會讓 _do_move_step 內部的 _motion_scope
                # 立刻退出，等待期間 motion_active 會變 False、光功率背景
                # 輪詢會在馬達還在動時恢復（2026-08-20 那項修正的回歸）。
                # 這裡自己在外層補一層 _motion_scope，計數器設計本來就
                # 支援巢狀，涵蓋整段「送出＋等待」。
                with self._motion_scope():
                    started = self._do_move_step(
                        axis_no, direction, str(offset), l_speed, f_speed, rate, s_rate, False
                    )
                    if not started:
                        # wait_done=False 時 _do_move_step 只會因為「格式
                        # 錯誤」或「Python 端軟體限位攔截」回 False，兩者
                        # 都發生在送出指令之前，滑台沒動過——at_origin
                        # 不動，不能跟「送出後失敗」混為一談去觸發整軸
                        # 收手、宣告座標系已失準。
                        note = "移動指令未送出（格式錯誤或軟體限位攔截）"
                        break
                    moved, still_on_switch = self._wait_axis_stop_leaving_limit(
                        axis_no, leaving_side, p_start, offset - 1, abort_event=stop_event
                    )
                if not moved:
                    at_origin = False
                    if self.ems_active:
                        note = "EMS 觸發，中止量測"
                    elif stop_event and stop_event.is_set():
                        note = "使用者中止（離開移動中）"
                    else:
                        note = "離開移動失敗（撞對向限位或逾時）"
                    break

                # M2：軸從未真正脫離出發側限位開關作用區，GO ORG 沒有從
                # 外側重新掃過感測器邊緣，量到的不是其他 offset 在量的
                # 同一個量——留給組合層級彙整成 note，不在這裡處理。
                left_switch = not still_on_switch

                if self.ems_active:
                    # 離開移動完成後、送出 GO ORG 之前再檢查一次：EMS 有
                    # 可能恰好在這段空檔被觸發，此時滑台已經不在原點，
                    # 不能再送一個「回原點」的移動指令——使用者剛按緊急
                    # 停止，程式不該又讓滑台動起來。
                    at_origin = False
                    note = "EMS 觸發，中止量測"
                    break

                homed, home_reason = self._do_origin_ex(
                    axis_no, org_type, l_speed, f_speed, rate, s_rate,
                    wait_done=True, abort_event=stop_event,
                )
                if not homed:
                    at_origin = False
                    # 同上：等待函式對 EMS／使用者中止／真正逾時／復歸未
                    # 實際執行都回 False，這裡分開標記，不要讓「有人按了
                    # 緊急停止」或「MEMSW0 樣式不適用」被誤讀成「復歸真的
                    # 逾時了」——三者要查的方向完全不同。
                    if self.ems_active:
                        note = "EMS 觸發，中止量測"
                    elif stop_event and stop_event.is_set():
                        note = "使用者中止（復歸中）"
                    elif home_reason == "not_executed":
                        note = (
                            f"GO ORG 未實際執行（Driving 未 assert 且 POS 未變化）"
                            f"——MEMSW0={org_type} 對本軸可能不適用，非累積漂移"
                        )
                    else:
                        note = "原點復歸逾時"
                    break
                at_origin = True

                time.sleep(0.05)
                p_i, inconsistent = self._read_pos_consistent(axis_no)
                status_str, _ = self.query_status(axis_no)

                # 🔴 讀值不一致在量測情境下，最可能的成因是「軸還在動」而
                # 不是通訊雜訊（兩次 POS? 相隔約 56ms，軸以 F0 飛行時會差
                # 數十 pulse）。這是對「復歸回報完成但實際還在跑」那個
                # bug 的直接回歸鎖：即使將來等待函式又出現新的漏網路徑，
                # 這裡會攔下來，而且**不歸零**。
                if inconsistent and status_str == "Driving":
                    at_origin = False
                    note = "復歸回報完成但軸仍在移動，殘差不可信，中止量測"
                    break

                # 復歸後是否停在原點/限位感測器上。只在下方失控門檻那個
                # 「已知異常」的分支拿來判斷該不該歸零——不可當成一般路徑
                # 的歸零閘門：有些 ORG 樣式會在找到感測器後退出作用區停下，
                # 那時 on_sensor 是 False 但復歸完全正常。
                on_sensor = (
                    status_str == "Detect origin"
                    or self.limit_direction(status_str) is not None
                )
                residual_um = self.estimate_um(axis, p_i) if p_i is not None else None
                samples.append(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "trial": trial + 1,
                        "residual_pulse": p_i,
                        "residual_um": residual_um,
                        "status": status_str,
                        "left_switch": left_switch,
                        "on_sensor": on_sensor,
                        "note": "讀值不一致，已取多數/最後值" if inconsistent else "",
                    }
                )
                if progress_cb:
                    progress_cb(axis, offset, trial + 1, trials, p_i, status_str)

                # 這是失控保護（防止量測在明顯異常的情況下無止盡跑下去），
                # **不是漂移判定**——真正的漂移判定在事後統計（drift_rate／
                # σ(diff)，見 _compute_homing_stats）。變數名刻意叫
                # runaway 而非 drift，避免下一個人從變數名推回錯誤結論。
                # 門檻加絕對下限 30 pulse：offset 一小（例如 100）時純比例
                # 門檻只有 25 pulse，會把正常的系統性偏移誤判成失控。
                runaway_threshold = max(0.25 * offset, 30.0)
                if p_i is not None and abs(p_i) > runaway_threshold:
                    if abs(abs(p_i) - offset) <= max(0.1 * offset, 5.0):
                        # 殘差 ≈ 離開距離，是「軸根本沒回來」的簽名——真正的
                        # 累積漂移是小量逐輪累加，不會一次就落在 offset 附近。
                        note = (
                            f"殘差({p_i:.0f}) ≈ 離開距離({offset})，軸幾乎沒有回到"
                            f"原點——復歸未生效或樣式不適用，非累積漂移"
                        )
                    else:
                        note = (
                            f"殘差({p_i:.0f}) 超出失控保護門檻"
                            f"({runaway_threshold:.0f})，提前收工——可能是復歸未"
                            f"生效、樣式不適用或座標系已偏移，是否為累積漂移"
                            f"須看事後統計的 drift_rate"
                        )
                    # M3 的第二種復發路徑：復歸確實執行過、但滑台沒回到
                    # 原點附近（樣式不適用／機械卡住／感測器接觸不良）。
                    # 既沒回到原點、也沒壓在任何感測器上時不可歸零。
                    if not on_sensor:
                        at_origin = False
                    break
        finally:
            if at_origin:
                # 🔴 寫入前最後一道閘門：確認軸真的停穩了。等待函式的判定
                # 再嚴格都只是「相信上游」，把確認放在危險動作本身才是機制
                # 性保證（見 _confirm_stopped 的 docstring）。
                try:
                    at_origin, _ = self._confirm_stopped(axis_no)
                except Exception as e:
                    at_origin = False
                    self._log(
                        "ERROR", f"[復歸重現性量測] 軸 {axis} 停止確認失敗: {e}"
                    )
                if not at_origin:
                    self._log(
                        "ERROR",
                        f"軸 {axis} 收尾時仍在移動或無法確認靜止，未強制歸零",
                    )

            if at_origin:
                try:
                    self.set_position(axis_no, "0")
                except Exception as e:  # 歸零本身不可讓例外逃逸、蓋掉已收集的樣本資料
                    at_origin = False
                    self._log("ERROR", f"[復歸重現性量測] 軸 {axis} 強制歸零失敗: {e}")

            if not at_origin:
                msg = (
                    f"軸 {axis} 在非原點狀態下中止，未強制歸零——"
                    f"座標系已失準，請重新執行原點復歸後再操作"
                )
                self._log("ERROR", msg)
                note = (note + "；" if note else "") + "座標系已失準，需重新復歸"

        # M2：offset 太小、軸從未脫離出發側限位開關作用區的組合，數據
        # 跟其他 offset 不可直接比較——GUI 三個預設 offset 全部預勾，
        # 使用者拿到這種資料混在正常資料裡外觀完全看不出來，note 必須
        # 明講。`still_on_switch`／`left_switch` 是 M1 的免費副產品，
        # 不需要額外移動或查詢。
        offset_below_switch = any(s.get("left_switch") is False for s in samples)
        if offset_below_switch:
            note = (
                (note + "；" if note else "")
                + "offset 未脫離出發側限位開關作用區，本組數據與其他 offset 不可直接比較"
            )

        # 復歸後沒停在原點/限位感測器上的輪次：殘差的物理意義存疑。
        # 跟 left_switch 一樣是免費副產品（query_status 本來就要呼叫）。
        # 有了這一欄，CSV 本身就看得出「復歸後 status=Stop」這種異常，
        # 事後判讀不必再回頭下原始指令重現。
        homed_off_sensor = any(s.get("on_sensor") is False for s in samples)
        if homed_off_sensor:
            note = (
                (note + "；" if note else "")
                + "部分輪次復歸後未偵測到原點/限位感測器，殘差意義存疑"
            )

        stats = self._compute_homing_stats(axis, samples)
        return {
            "axis": axis,
            "offset": offset,
            "direction": direction,
            "org_type": org_type,
            "n_samples": len(samples),
            "note": note,
            "origin_lost": not at_origin,
            "offset_below_switch": offset_below_switch,
            "homed_off_sensor": homed_off_sensor,
            "samples": samples,
            "stats": stats,
        }

    def _read_pos_consistent(self, axis_no: str) -> Tuple[Optional[float], bool]:
        """
        連讀兩次 POS? 要求一致；不一致就再讀第三次，三筆裡有兩筆相同就用
        多數值，否則保守地退回最後一次讀到的值（寧可留一個可能有雜訊的
        值並標記，也不要讓這筆量測資料整筆開天窗）。

        回傳 (數值或 None, 是否曾經讀值不一致)。POS? 讀取失敗（空字串／
        非數字）視為不一致。
        """

        def _read() -> Optional[float]:
            raw = self._serial_write_read(f"AXI{axis_no}:POS?")
            try:
                return float(raw)
            except (ValueError, TypeError):
                return None

        p1 = _read()
        p2 = _read()
        if p1 is not None and p2 is not None and p1 == p2:
            return p1, False

        p3 = _read()
        candidates = [p1, p2, p3]
        for v in candidates:
            if v is not None and candidates.count(v) >= 2:
                return v, True
        return p3, True

    def _compute_homing_stats(self, axis: str, samples: List[dict]) -> dict:
        """
        對單一 (軸, offset) 組合的殘差序列算統計量。

        先用線性回歸判斷有沒有系統性漂移；有漂移時 range/sigma(p) 這種
        數字會隨 N 成長沒有意義，改報漂移率／輪間差的標準差。沒有漂移
        才報 range/median/sigma/MAD。N<2（組合失敗提早中止）一律回 None，
        不嘗試除以零。

        有校正參數的軸額外換算一份 μm 版本，並附上當下的校正參數快照
        （比照〈存檔時的 μm 快照〉：存完整參數而非只存算出來的 μm，
        參數本身之後可能被使用者改掉或用 clear_axis_calib() 清除）。
        """
        p_series = [s["residual_pulse"] for s in samples if s["residual_pulse"] is not None]
        n = len(p_series)

        stats: dict = {
            "n": n,
            "drift_detected": None,
            "range": None,
            "median": None,
            "sigma": None,
            "mad_scaled": None,
            "drift_rate": None,
            "sigma_diff": None,
        }
        if n < 2:
            stats["um"] = None
            stats["axis_calib_snapshot"] = None
            return stats

        b, se_b = self._linear_regress_slope(p_series)
        drift_detected = abs(b) > max(3 * se_b, 0.3)
        stats["drift_detected"] = drift_detected

        if drift_detected:
            diffs = [p_series[i + 1] - p_series[i] for i in range(n - 1)]
            stats["drift_rate"] = statistics.mean(diffs) if diffs else None
            stats["sigma_diff"] = statistics.stdev(diffs) if len(diffs) >= 2 else None
        else:
            med = statistics.median(p_series)
            stats["range"] = max(p_series) - min(p_series)
            stats["median"] = med
            stats["sigma"] = statistics.stdev(p_series) if n >= 2 else None
            stats["mad_scaled"] = 1.4826 * statistics.median([abs(v - med) for v in p_series])

        params = self.axis_calib.get(axis)
        if params:
            stats["axis_calib_snapshot"] = dict(params)
            um_fields = {}
            for key in ("range", "median", "sigma", "mad_scaled", "drift_rate", "sigma_diff"):
                v = stats.get(key)
                if v is not None:
                    um = self.estimate_um(axis, v)
                    if um is not None:
                        um_fields[key] = um
            stats["um"] = um_fields
        else:
            stats["axis_calib_snapshot"] = None
            stats["um"] = None

        return stats

    @staticmethod
    def _linear_regress_slope(y: List[float]) -> Tuple[float, float]:
        """
        簡單最小平方法算 y 對「輪數索引」(0..n-1) 的斜率與標準誤。
        輪與輪之間本來就是等間隔的重複量測，不需要真實時間戳當 x 軸。

        n<=2 時自由度不足以估殘差標準差（n=2 時 dof=0，直接除會是
        ZeroDivisionError），回傳極大的標準誤，讓呼叫端的漂移顯著性
        判斷式保守地偏向「非顯著」，而不是讓程式崩潰。
        """
        n = len(y)
        if n < 2:
            return 0.0, float("inf")
        xs = list(range(n))
        x_mean = statistics.mean(xs)
        y_mean = statistics.mean(y)
        sxx = sum((x - x_mean) ** 2 for x in xs)
        if sxx == 0:
            return 0.0, float("inf")
        sxy = sum((x - x_mean) * (yy - y_mean) for x, yy in zip(xs, y))
        b = sxy / sxx
        if n <= 2:
            return b, float("inf")
        residuals = [yy - (y_mean + b * (x - x_mean)) for x, yy in zip(xs, y)]
        sse = sum(r ** 2 for r in residuals)
        mse = sse / (n - 2)
        se_b = math.sqrt(mse / sxx)
        return b, se_b

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
        # 點動中觸發緊急停止也要讓監看執行緒收工，否則 _jog_stop 會卡在
        # clear() 狀態，motion_active 永遠回報 True（見 disconnect 同一類前置缺陷）。
        self._jog_stop.set()
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
    def _wait_axis_stop(
        self,
        axis_no: str,
        timeout: float = WAIT_TIMEOUT,
        start_pos: Optional[float] = None,
        expected_travel: Optional[float] = None,
    ) -> bool:
        """
        阻塞等待指定軸停止（SB1 bit6 Driving 旗標清除）。
        同時偵測異常狀態（Limit）並觸發警報回調。
        回傳：True=正常停止，False=逾時／異常／GO 未生效。
        此方法應在背景執行緒呼叫，避免凍結 UI。

        🔴 **「非 Driving」不等於「移動已完成」**（2026-08-26 修，CLAUDE.md
        自 2026-08-21 起記載的「孿生競態」）。舊寫法 `status == "Stop"` 直接
        `return True`，而實測 `GO CW` 的 Driving assert 延遲是 96ms、本函式
        第一次 `query_status()` 要 SB3?+SB1? 兩次往返約 112ms——餘裕只有約
        16ms。落在那個窗口裡就會把「還沒起步」讀成「已經停好」，於是
        `move_step(wait_done=True)` 在軸飛行中回報成功，`goto_point()` 提前
        送出下一軸、`fiber_scanner._measure_here()` 在移動中量光功率。這與
        `_wait_origin_done_ex()` 修掉的是同一個韌體特性，只是症狀不同。

        判定沿用 `_wait_origin_done_ex()` 已經實機驗證過的三重證據配方，
        但**只在寬限期內**要求證據——寬限期一過就回到舊語意：

          1. `saw_driving`：看過 Driving assert（正常移動的主要路徑）。
          2. 走完預期行程：`|POS - start_pos| >= expected_travel - MOVE_POS_EPS`。
             這條是為「移動短到在第一次取樣之前就跑完」準備的——單看
             Driving 會把它誤判成從未起步。容差 1 pulse，與量測路徑
             `_wait_axis_stop_leaving_limit()` 的 `offset - 1` 同一慣例。
          3. POS 相對**第一次取樣值**變化過：呼叫端沒傳 `start_pos` /
             `expected_travel` 時的保底證據，涵蓋「短移動整段落在兩次取樣
             之間」。

        三者 OR。只有「Driving 從沒 assert、POS 完全沒動、且寬限期已過」
        才判 GO 未生效並回傳 False——物理上就是什麼都沒發生。

        🔴 寬限期外**刻意不**要求位移證據，這是與 CLAUDE.md 原本記載的
        修法（無條件要求 `travelled >= expected - 1`）唯一的差異，理由是
        後者會在使用者中途按「停止」時退化成空等到 `WAIT_TIMEOUT`(30s)：
        `STOP 0` 讓軸提前停下，`travelled` 永遠達不到 expected。競態本身
        純粹是「起步窗口」現象，把證據要求限縮在寬限期內就足以堵住它，
        且寬限期之後的行為與改動前逐字相同，不影響任何既有路徑。

        `start_pos` / `expected_travel` 皆為機械座標系 pulse（`query_status()`
        回傳的 POS 就是機械座標），為 None 時只是少一條證據，不會誤報成功。
        """
        start_time = time.time()
        deadline = start_time + timeout
        saw_driving = False
        first_pos: Optional[float] = None  # 第一次成功取樣到的 POS，證據 3 的基準
        while time.time() < deadline:
            if self.ems_active:
                return False
            # query_status 內部已完成位置換算與寫入，此處不重複處理
            status, pos = self.query_status(axis_no)
            # 記錄數據
            if self._data_logging:
                self._record_data_point()

            try:
                pos_val: Optional[float] = float(pos)
            except (ValueError, TypeError):
                pos_val = None
            if pos_val is not None and first_pos is None:
                first_pos = pos_val

            if status == "Driving":
                saw_driving = True
                time.sleep(WAIT_INTERVAL)
                continue

            moved = self._move_evidence(pos_val, first_pos, start_pos, expected_travel)
            within_grace = time.time() - start_time < MOVE_START_GRACE

            if status == "Stop":
                if saw_driving or moved:
                    return True
                if within_grace:
                    # 從未看過 Driving、POS 也沒動，且還在寬限期內——無法
                    # 分辨「GO 尚未生效」與「真的停好了」，續輪，不在這裡
                    # 下任何結論。這裡用 MOVE_START_POLL 而非 WAIT_INTERVAL，
                    # 理由同 _wait_origin_done_ex()：0.5s 的節奏對 96ms 的
                    # assert 延遲解析度太差。
                    time.sleep(MOVE_START_POLL)
                    continue
                ax = NO_AXIS.get(axis_no, axis_no)
                self._log(
                    "ERROR",
                    f"軸 {ax} 移動未生效——Driving 未 assert 且 POS 未變化"
                    f"（{start_pos if start_pos is not None else first_pos}"
                    f"→{pos_val}），指令可能被韌體忽略",
                )
                if self._alarm_cb:
                    self._alarm_cb(
                        f"軸 {ax} 移動未生效", "GO 指令送出後未偵測到任何動作"
                    )
                return False

            if not saw_driving and not moved and within_grace:
                # 🔴 限位／異常狀態同樣可能只是 GO 還沒生效時讀到出發前就
                # 壓著的那顆限位（例如從 CCW 限位上往 CW 走）。續輪等
                # Driving assert 即可分辨：真的走得掉就轉成 Driving，走
                # 不掉則寬限期一過照樣報錯，代價只是晚 1 秒才報。
                #
                # 🔴 刻意排除 moved 成立的情況：那代表軸確實走完並停在
                # 限位上，是貨真價實的撞限位，必須照 2026-08-05「撞限位
                # 不再靜默」的結論報出來，不可因為有位移證據就當成功回傳。
                time.sleep(MOVE_START_POLL)
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

    @staticmethod
    def _move_evidence(
        pos_val: Optional[float],
        first_pos: Optional[float],
        start_pos: Optional[float],
        expected_travel: Optional[float],
    ) -> bool:
        """
        `_wait_axis_stop()` 用的位移證據：軸是不是真的動過／已經走完。

        兩條證據（見 `_wait_axis_stop()` docstring 的 2. 與 3.）任一成立
        即可。純計算、無 I/O，讀不到 POS（None）時一律回傳 False——
        缺證據時只會讓呼叫端繼續等，不會誤報成功。

        `expected_travel <= 0`（例如 PULS 0）時第一條天然成立：
        `0 >= 0 - MOVE_POS_EPS`，不需要另外特判。
        """
        if pos_val is None:
            return False
        if (
            start_pos is not None
            and expected_travel is not None
            and abs(pos_val - start_pos) >= expected_travel - MOVE_POS_EPS
        ):
            return True
        return first_pos is not None and abs(pos_val - first_pos) > MOVE_MOTION_EPS

    def _replay_move_hint(
        self, axis_no: str, tx: str
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        從重播的原始指令字串推出 `_wait_axis_stop()` 要的
        (start_pos, expected_travel)，湊不出來就回 (None, None)。

        只處理 `PULS n` + `GO CW/CCW` 這一種——本程式錄下來的有終點移動
        只會是這種格式（見 CLAUDE.md〈DS102 通訊協定重點〉的四種指令）。
        `GO ABS`/`GO HOME`/`GOTCH` 的行程與 PULS 無關，硬套會算出錯的
        expected_travel，寧可不傳。

        start_pos 取快取的機械座標：重播期間 position worker 被
        `playback_running` 排除，快取由上一步的 `_wait_axis_stop()` 內
        `query_status()` 更新；點動步驟不等到位時可能偏舊，但那只會讓
        這條證據失效而已——證據不成立只是繼續等，不會誤報成功。
        """
        if not re.search(r":GO\s+(?:CW|CCW)\b", tx):
            return None, None
        m = re.search(r":PULS\s+(-?\d+(?:\.\d+)?)", tx)
        if not m:
            return None, None
        ax = NO_AXIS.get(axis_no)
        if not ax:
            return None, None
        try:
            travel = abs(float(m.group(1)))
        except ValueError:
            return None, None
        with self._lock:
            return self._positions_pulse[ax], travel

    def _wait_axis_stop_leaving_limit(
        self,
        axis_no: str,
        leaving_side: str,
        start_pos: float,
        min_travel: float,
        timeout: float = WAIT_TIMEOUT,
        abort_event: Optional[threading.Event] = None,
    ) -> Tuple[bool, bool]:
        """
        量測專用的到位等待：容忍「出發時就壓著的那一側」限位。

        🔴 只給 measure_homing_repeatability()/_measure_one_combo() 這條
        量測路徑使用，絕對不可放進 _do_move_step()——一般步進移動撞到限位
        永遠是失敗，這是 2026-08-05 那批安全修正的核心結論之一。這裡容忍
        的只有「出發那一側」限位，且只在這個特定情境下成立，不是放寬一般
        撞限位的判定。

        背景（2026-08-21 COM2 實機驗證）：原點就落在限位開關上，而限位
        開關有實體作用寬度——實測 X 軸 POS=100 時 SB2=2（CCW 硬體限位
        仍壓著），POS=150 時 SB2=0（解除）。離開移動走完一個較小的
        offset（例如 100）之後，軸有可能還壓在「出發時那一顆」限位上，
        `_wait_axis_stop()` 依既有語意（非 Driving 就視為異常）會把這個
        正常起始條件誤判成撞限位失敗——這跟 CLAUDE.md 記載「原點復歸
        必須用 `_wait_origin_done()`、不能沿用 `_wait_axis_stop()`」是
        同一類問題：量測起點必然在限位上，壓著它不是異常。

        🔴 只看「非 Driving + 出發側限位/Stop」還不夠：`GO` 指令剛送出、
        Driving 位元根本還沒 assert 時，軸一步都還沒動也會符合這個條件，
        若因此判成「到位」，會接著送出 `GO ORG`、量到一組殘差≈0 的假
        資料存進 CSV——比大聲失敗危險得多。所以再疊一層位移判準：只有
        實際位移達到 `min_travel`（呼叫端傳 `offset - 1`，容 1 pulse）
        才真的算到位，否則視為「GO 尚未生效」，繼續等，最終交給逾時
        判失敗。這道判準同時讓行為與 offset 大小、與輪詢時機都無關，
        不必再擔心「offset 夠不夠大」這類競態問題。

        回傳 (是否正常到位, 停止時是否仍壓在出發側限位)。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ems_active:
                return False, False
            if abort_event is not None and abort_event.is_set():
                return False, False

            status, pos = self.query_status(axis_no)
            if status == "Driving":
                time.sleep(WAIT_INTERVAL)
                continue

            try:
                travelled = abs(float(pos) - start_pos)
            except (ValueError, TypeError):
                # 讀不到位置，無法確認是否已到位——續輪，最終由逾時判失敗，
                # 不要在這裡就直接放行或直接失敗。
                travelled = -1.0

            side = self.limit_direction(status)
            if status == "Stop" or side == leaving_side:
                if travelled >= min_travel:
                    return True, side == leaving_side
                # GO 尚未生效（Driving 位元還沒 assert）或真的卡住不動，
                # 兩者都繼續等，交給上面的 deadline 逾時判失敗。
                time.sleep(WAIT_INTERVAL)
                continue

            # 其他狀態（行進方向那一側限位、或其他異常）才是真正的失敗——
            # 這才是應該攔下來的情境，跟出發側限位不是同一回事。
            ax = NO_AXIS.get(axis_no, axis_no)
            self._log("WARN", f"軸 {ax} 離開移動時進入異常狀態: {status}")
            if self._alarm_cb:
                self._alarm_cb(f"軸 {ax} 異常", status)
            return False, False

        self._log("WARN", f"軸{axis_no} 離開移動等待逾時（{timeout}s）")
        return False, False

    def wait_axis_stop(
        self,
        axis_no: str,
        timeout: float = WAIT_TIMEOUT,
        start_pos: Optional[float] = None,
        expected_travel: Optional[float] = None,
    ) -> bool:
        """
        `_wait_axis_stop()` 的公開版本。

        `move_step(wait_done=False)` 只負責送出 GO 指令、立刻回傳，不等到位。
        FiberAlignmentScanner 的多軸同時出發流程需要「先送完所有軸的 GO，
        再依序等每一軸到位」，這個等待步驟因此要獨立於 move_step 之外被
        呼叫——供給 controller 以外的模組（fiber_scanner.py）使用，不必
        讓它碰底線用底線開頭的內部方法。

        `start_pos` / `expected_travel` 直接轉交 `_wait_axis_stop()` 當位移
        證據（機械座標 pulse）。搜尋演算法每次的移動量都很小、可能在第一次
        取樣之前就跑完，這兩個參數是它避免被誤判成「GO 未生效」的關鍵，
        呼叫端有值就該傳。
        """
        return self._wait_axis_stop(axis_no, timeout, start_pos, expected_travel)

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

    def _persist_guarded(
        self,
        filename: str,
        data: dict,
        loaded: bool,
        loader_name: str,
        item_label: str,
    ) -> None:
        """
        持久化拒寫守護（_persist_profiles / _persist_points / _persist_axis_calib 共用）。

        沒先 load 過就寫回，等於拿一份不完整的記憶體狀態覆蓋磁碟：比對磁碟既有
        內容缺哪些 key，缺就拒寫並記 ERROR（只有 .bak 可救）。`filename` 只傳
        檔名字串、不可傳 Path 或在別處先組好路徑——RECORDING_DIR 必須在這裡、
        呼叫當下才組出來，否則 tests/conftest.py 對 ds102_ctrl.RECORDING_DIR 的
        monkeypatch 會失效，測試會直接寫進真正的 recordings/。
        """
        p = RECORDING_DIR / filename
        if not loaded and p.exists():
            try:
                existing = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            missing = set(existing) - set(data)
            if missing:
                self._log(
                    "ERROR",
                    f"拒絕寫入 {filename}：未先 {loader_name}() 就儲存，"
                    f"會遺失 {len(missing)} {item_label}（{'、'.join(sorted(missing))}）",
                )
                return
        _write_json_with_backup(p, data, self._log)

    def _persist_profiles(self) -> None:
        # 與 _persist_points() 同一套防護：沒 load 過就寫回，等於拿一份不完整的
        # 記憶體狀態覆蓋磁碟。teaching points 早就有這層保護，profiles 一直沒有。
        self._persist_guarded(
            "speed_profiles.json",
            self.speed_profiles,
            self._profiles_loaded,
            "load_speed_profiles",
            "個既有 Profile",
        )

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

        若某軸當下有設定機械校正參數（axis_calib），額外存一份 μm 估算值
        與當下的校正參數快照——校正參數之後可能被使用者改掉或清除
        （clear_axis_calib），沒有存快照的話，這筆歷史紀錄的 μm 之後就沒
        辦法驗證是怎麼算出來的。這兩個新欄位純粹是附加的歷史快照，
        goto_point() 完全不讀它們，只讀 positions_pulse。
        """
        entry = {
            "positions_pulse": dict(positions),
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        positions_um = {}
        axis_calib_snapshot = {}
        for ax, pulse in positions.items():
            um = self.estimate_um(ax, pulse)
            if um is not None:
                positions_um[ax] = um
                axis_calib_snapshot[ax] = dict(self.axis_calib[ax])
        if positions_um:
            entry["positions_um"] = positions_um
            entry["axis_calib_snapshot"] = axis_calib_snapshot

        self.saved_points[name] = entry
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
        if self._manual_ops_blocked():
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
        # 沒 load 過就寫回，等於拿一份不完整的記憶體狀態覆蓋磁碟。
        # 正常流程 GUI 啟動一定會 load_points()，會走到這裡的多半是
        # 直接 new 一個 controller 的測試腳本。
        self._persist_guarded(
            "teaching_points.json",
            self.saved_points,
            self._points_loaded,
            "load_points",
            "個既有點位",
        )

    def load_points(self) -> None:
        p = RECORDING_DIR / "teaching_points.json"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                self.saved_points = json.load(f)
            self._points_loaded = True
            self._log("INFO", f"載入 {len(self.saved_points)} 個 Teaching Points")

    # =========================================================================
    # 軸機械校正參數（純顯示用的 pulse→μm 估算，不影響任何控制邏輯）
    # =========================================================================
    def load_axis_calib(self) -> None:
        """開機載入 recordings/axis_calibration.json。找不到檔案也算已知狀態。"""
        p = RECORDING_DIR / "axis_calibration.json"
        if p.exists():
            try:
                self.axis_calib = json.loads(p.read_text(encoding="utf-8"))
                self._log(
                    "INFO", f"載入 {len(self.axis_calib)} 軸的機械校正參數"
                )
            except (OSError, json.JSONDecodeError) as e:
                self._log("ERROR", f"軸機械校正參數檔讀取失敗: {e}")
        self._axis_calib_loaded = True

    def _persist_axis_calib(self) -> None:
        """
        寫回 axis_calibration.json。

        沒先 load 過就寫回，等於拿一份不完整的記憶體狀態覆蓋磁碟——比照
        _persist_points 的既有防護：比對磁碟既有內容缺哪些軸，缺就拒寫並記
        ERROR，只有 .bak 可救。
        """
        self._persist_guarded(
            "axis_calibration.json",
            self.axis_calib,
            self._axis_calib_loaded,
            "load_axis_calib",
            "軸既有校正參數",
        )

    def set_axis_calib(self, calib: Dict[str, dict]) -> List[str]:
        """
        合併更新軸機械校正參數（只更新傳入的軸，其餘軸既有資料不受影響，
        仿照 capture_controller_config() 的合併邏輯，不是整份覆蓋）。

        逐軸驗證 lead_pitch_mm / step_angle_deg 皆為正數，division 為正整數；
        任一軸不合法就在回傳的 list 附加一則錯誤字串，該軸不寫入。
        回傳空 list 代表全部合法並已存檔；非空 list 代表有錯誤，呼叫端
        （GUI）要整批不當作套用成功——這批裡合法的軸也不會被寫入，維持
        「全部成功或全部不動」，避免使用者以為六軸都套用了但其實只套用一半。
        """
        errors: List[str] = []
        to_write: Dict[str, dict] = {}
        for ax, params in calib.items():
            lead = params.get("lead_pitch_mm")
            angle = params.get("step_angle_deg")
            division = params.get("division")

            # math.isfinite() 排除 inf/nan——float("inf") 與 float("nan") 都不會
            # 拋 ValueError，且兩者都滿足 x <= 0 為 False，沒有這道檢查會被前面的
            # 正數判斷放行，算出 "≈ nan μm" 這種顯示（architect 審查抓到的邊界案例）。
            if (
                not isinstance(lead, (int, float))
                or isinstance(lead, bool)
                or not math.isfinite(lead)
                or lead <= 0
            ):
                errors.append(f"{ax}: 導程需為正數")
                continue
            if (
                not isinstance(angle, (int, float))
                or isinstance(angle, bool)
                or not math.isfinite(angle)
                or angle <= 0
            ):
                errors.append(f"{ax}: 步進角需為正數")
                continue
            if not isinstance(division, int) or isinstance(division, bool) or division <= 0:
                errors.append(f"{ax}: 分度值需為正整數")
                continue

            to_write[ax] = {
                "lead_pitch_mm": float(lead),
                "step_angle_deg": float(angle),
                "division": int(division),
                "ts": datetime.now().isoformat(timespec="seconds"),
            }

        if errors:
            return errors

        if to_write:
            self.axis_calib.update(to_write)
            self._log(
                "INFO",
                f"軸機械校正參數已更新：{'、'.join(sorted(to_write))}",
            )
            self._persist_axis_calib()
        return []

    def clear_axis_calib(self, ax: str) -> bool:
        """
        清除單一軸的機械校正參數。

        `_apply_axis_calib()`（GUI）把「三欄全空」當成「本次不動這軸」，
        因此使用者把已存的值手動清空再按套用，並不會真的刪除資料——這是
        故意的（避免使用者不小心清掉某一欄就整軸消失），但也代表沒有任何
        路徑能移除已存的校正參數。這個方法是唯一的清除入口，語意明確：
        呼叫了就是真的要刪，不是「留空跳過」。
        """
        if ax not in self.axis_calib:
            return False
        self.axis_calib.pop(ax)
        self._log("INFO", f"{ax} 軸的機械校正參數已清除")
        self._persist_axis_calib()
        return True

    def estimate_um(self, ax: str, pulse: float) -> Optional[float]:
        """
        把 pulse 估算成 μm，純顯示用途。

        um_per_pulse = (導程mm * 1000) / ((360 / 步進角) * 分度值)

        軸沒有校正參數、或參數不合法（第二道防線，防的是 json 檔被手動編輯
        繞過 GUI 輸入驗證，不是防使用者手滑），一律回傳 None——呼叫端據此
        決定「不顯示」而不是顯示 0 或猜測值，沿用「未連線一律顯示 —
        不顯示 0」的既有原則。
        """
        params = self.axis_calib.get(ax)
        if not params:
            return None
        lead = params.get("lead_pitch_mm")
        angle = params.get("step_angle_deg")
        division = params.get("division")
        if (
            not isinstance(lead, (int, float))
            or isinstance(lead, bool)
            or not math.isfinite(lead)
            or lead <= 0
        ):
            return None
        if (
            not isinstance(angle, (int, float))
            or isinstance(angle, bool)
            or not math.isfinite(angle)
            or angle <= 0
        ):
            return None
        if not isinstance(division, int) or isinstance(division, bool) or division <= 0:
            return None

        um_per_pulse = (lead * 1000.0) / ((360.0 / angle) * division)
        return pulse * um_per_pulse

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
                        # 只有「有終點」的指令才等到位。GO CWJ / CCWJ 是連續
                        # 點動，沒有終點，會一直跑到下一個 step 的 STOP 0
                        # 才停——用 `"GO" in tx` 把它也納入等待會讓重播的
                        # _wait_axis_stop 阻塞到 30 秒逾時（或撞上硬體限位），
                        # STOP 0 遲遲送不出去。ORG 同樣排除：可能橫跨整個
                        # 行程且途中壓限位屬正常。
                        if _is_finite_move(tx):
                            ax_m = re.search(r"AXI(\d)", tx)
                            if ax_m:
                                # 從原始指令字串還原位移證據所需的兩個值，
                                # 讓重播也受「孿生競態」那道保護（見
                                # _wait_axis_stop docstring）。只認 CW/CCW
                                # ——GO ABS/HOME/GOTCH 的行程跟 PULS 無關，
                                # 湊不出 expected_travel 就傳 None，退回
                                # 保底證據那條路徑。
                                rec_start, rec_travel = self._replay_move_hint(
                                    ax_m.group(1), tx
                                )
                                if not self._wait_axis_stop(
                                    ax_m.group(1),
                                    start_pos=rec_start,
                                    expected_travel=rec_travel,
                                ):
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
