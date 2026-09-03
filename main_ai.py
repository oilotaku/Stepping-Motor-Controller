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
import logging
import math
import re
import itertools
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Callable
from dataclasses import dataclass

from meter_GPIB import HP8153APowerMeter
from fiber_scanner import (
    FiberAlignmentScanner,
    ScanAbort,
    DEFAULT_STEP_MIN,
    DEFAULT_SETTLE_SEC,
    DEFAULT_MAX_CYCLES,
    DEFAULT_NOISE_SIGMA_MULT,
    DEFAULT_NO_SIGNAL_RANGE_MULT,
    BLIND_MODES,
    GUI_DEFAULT_BLIND_MODE,
    DEFAULT_BLIND_STEP,
    DEFAULT_BLIND_MAX_RADIUS,
    DEFAULT_BLIND_SIGNAL_SIGMA_MULT,
    DEFAULT_BLIND_SIGNAL_MIN_DELTA_DB,
    export_samples_xlsx,
    _XLSXWRITER_AVAILABLE,
)
# 尋光的 Powell 共軛方向法（實驗性替代演算法）獨立在這個模組，見該檔開頭
# 說明。單向 import：fiber_scanner_advanced.py 本身不 import main_ai.py，
# 不會循環相依。這裡只讀 `_SCIPY_AVAILABLE` / `_SCIPY_IMPORT_ERROR` 兩個
# 模組屬性決定 GUI 要不要讓使用者選到這個選項，不直接呼叫模組內的函式
# （那是 fiber_scanner.FiberAlignmentScanner.run() 內部的事）。
import fiber_scanner_advanced
# DS102Controller 與其專屬的模組層級常數／函式已抽到 ds102_ctrl.py
# （2026-08-17，架構拆分第一階段 1a，機械式搬移，行為不變）。
# 這裡把 GUI 端仍需要的名字重新引入自己的命名空間，其餘只有
# DS102Controller 內部用到的常數（MAX_RETRY、WAIT_TIMEOUT 等）留在
# ds102_ctrl.py，不在此重複定義。
from ds102_ctrl import (
    DS102Controller,
    AXES,
    AXIS_NO,
    NO_AXIS,
    MODE_CONTINUE,
    MODE_STEP,
    MODE_ORIGIN,
    COMM_FAIL_THRESHOLD,
    _BASE_DIR,
    LOG_DIR,
    RECORDING_DIR,
    DATA_DIR,
    NON_RECORDING_JSON,
    logger,
    _write_json_with_backup,
    _load_json_settings,
    _app_settings,
    _app_setting_num,
    _safety_setting_rejections,
    save_homing_repeat_result,
)
# UI 色票（2026-08-31，模組化前置工作 3，見 docs/modularization.md〈八〉）。
# 單向 import：ui_theme.py 只 import ds102_ctrl.py，不 import 本檔，
# 所以依賴鏈是 ds102_ctrl → ui_theme → main_ai，沒有環。
# 這裡刻意逐一列名而非 `import *`：main_ai.py 底下有 445 處引用，
# 用 `import *` 會讓靜態分析完全查不到這些名字從哪來。
from ui_theme import (
    CLR_BG,
    CLR_CARD,
    CLR_BORDER,
    CLR_ACCENT,
    CLR_DANGER,
    CLR_INFO,
    CLR_WARN,
    CLR_TEXT,
    CLR_MUTED,
    CLR_LOG_BG,
)

# matplotlib 是尋光分頁的即時軌跡圖用的，非本程式核心相依（序列通訊與其餘
# 分頁完全不需要它）。優雅降級：裝不到就停用尋光分頁，不影響其他功能。
try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    import numpy as np  # matplotlib 本身就強制依賴 numpy（PathCollection.set_offsets
    # 內部用 np.asanyarray 處理），不是額外引入的相依，這裡直接借用來組空陣列。
    # 座標軸標籤／標題有中文，matplotlib 預設字型沒有對應字形會顯示缺字方框。
    # 只需設定一次，放在任何 Figure 建立之前即可。
    matplotlib.rcParams["font.sans-serif"] = ["Microsoft JhengHei", "Segoe UI", "SimHei", "Arial"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    _MATPLOTLIB_AVAILABLE = True
    _MATPLOTLIB_IMPORT_ERROR = None
except ImportError as e:
    _MATPLOTLIB_AVAILABLE = False
    _MATPLOTLIB_IMPORT_ERROR = str(e)

# =============================================================================
# 執行期目錄與 LOG 系統
#
# _app_dir()／_BASE_DIR／LOG_DIR／RECORDING_DIR／DATA_DIR／logger 已搬到
# ds102_ctrl.py（DS102Controller 需要 RECORDING_DIR／DATA_DIR／logger），
# 上方 import 區塊已重新引入這幾個名字，此處不重複定義。
# =============================================================================
log_filename: Optional[Path] = None

if not _MATPLOTLIB_AVAILABLE:
    logger.warning(f"matplotlib 不可用，尋光分頁停用: {_MATPLOTLIB_IMPORT_ERROR}")


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


# _write_json_with_backup() 已搬到 ds102_ctrl.py（DS102Controller 的
# teaching_points / speed_profiles / controller_config 持久化都要用它），
# 上方 import 區塊已重新引入，這裡用到的地方（_save_meter_config /
# _save_scanner_config）不必改動呼叫方式。
def _log_level_adapter(level: str, msg: str) -> None:
    """
    把 `_write_json_with_backup` 期待的 `log(level, msg)` 呼叫轉給模組 logger。

    既有呼叫端（DS102Controller）傳的都是 `self._log`（(level, msg) 簽章的
    bound method），但 meter 設定檔是模組層級函式，沒有 controller 實例
    可用。直接傳 `logger`（`logging.Logger` 物件）不可行——`log(...)`
    內部會呼叫 `log("WARN", msg)`，而 `Logger` 物件本身不可呼叫，會在第一次
    寫入失敗時丟出 `TypeError`。這個轉接函式沿用 `DS102Controller._log`
    同一套 level 對應表。
    """
    getattr(
        logger,
        {"ERROR": "error", "WARN": "warning", "DEBUG": "debug"}.get(level, "info"),
    )(msg)


def _load_meter_config(log=None) -> dict:
    """
    讀取光功率計連線設定（GPIB 位址／channel／波長）。找不到或壞檔都回空字典。

    log：選用的 `(level, msg)` 簽章回呼。呼叫端若已有 DS102Controller 實例
    （目前只在 DS102GUI.__init__，此時 self.ctrl 已建立），應傳 `self.ctrl._log`，
    讀取失敗才會同時進 GUI 的 LOG 分頁與匯出的歷程檔，而不只是寫進 log 檔。
    不傳則退回只寫模組 logger（例如測試腳本直接呼叫，沒有 GUI 可用）。
    """
    return _load_json_settings(
        RECORDING_DIR / "meter_config.json",
        "meter_config.json",
        log=log or _log_level_adapter,
        error_level="ERROR",
    )


def _save_meter_config(data: dict, log=None) -> None:
    """
    整份覆寫光功率計連線設定。

    不需要比照 teaching_points 的 `_points_loaded` 拒寫保護——那道保護
    是為了防止「累積型集合」被空的記憶體狀態覆寫掉既有內容；這裡的欄位
    只有位址／channel／波長三個純量值，整份覆寫本來就是正確行為。

    log：見 `_load_meter_config` 的說明，同一套規則。
    """
    _write_json_with_backup(
        RECORDING_DIR / "meter_config.json", data, log=log or _log_level_adapter
    )


def _load_scanner_config(log=None) -> dict:
    """讀取尋光演算法設定（速度/安全判準等跨次搜尋穩定的參數）。找不到或壞檔都回空字典。"""
    return _load_json_settings(
        RECORDING_DIR / "scanner_config.json",
        "scanner_config.json",
        log=log or _log_level_adapter,
        error_level="ERROR",
    )


def _save_scanner_config(data: dict, log=None) -> None:
    """
    整份覆寫尋光演算法設定。欄位大多是純量值，不需要 teaching_points 那種
    拒寫保護；例外是 `selected_axes`（使用者勾選的搜尋軸清單，字串
    list），但它同樣沒有「累積型集合被空狀態覆寫」的風險——每次存檔都是
    當下六個勾選框的完整快照，整份覆寫本來就是正確行為，不需要另外處理。
    """
    _write_json_with_backup(
        RECORDING_DIR / "scanner_config.json", data, log=log or _log_level_adapter
    )


# _load_app_settings() / _app_setting_num() / _app_settings 已搬到
# ds102_ctrl.py（HISTORY_MAX 需要靠 _app_setting_num() 覆寫，避免
# ds102_ctrl.py 反過來 import main_ai.py），這裡改用上方 import 區塊
# 重新引入的 _app_settings / _app_setting_num。
#
# AXES / AXIS_NO / NO_AXIS / MODE_CONTINUE / MODE_STEP / MODE_ORIGIN /
# UNIT_PULSE 同樣搬到 ds102_ctrl.py（UNIT_PULSE 只有該檔內部用到，不重新
# 引入）。ORG_MODES 不搬——DS102Controller 完全沒用到它，純粹是 GUI 下拉選單。

# =============================================================================
# 常數定義
# =============================================================================
# 原點模式清單。
#
# 🔴 刻意從 1 開始，不提供 ORG 0。move_origin() 第一件事就是送
# `AXI{n}:MEMSW0 {type}`，選 ORG 0 等於把該軸復歸樣式寫成 Type0＝不執行。
# 後果三重：GO ORG 空等 180 秒逾時、復歸樣式被永久覆蓋（實機值
# X=2/Y=1/Z=2）、把 controller_config.json 剛還原的設定當場毀掉。
ORG_MODES = [f"ORG {i}" for i in range(1, 13)]

# 尋光「演算法」下拉選單：代碼（存進 scanner_config.json、傳給
# FiberAlignmentScanner.run(algorithm=...)）→ 顯示標籤。與 ORG_MODES 不同的
# 既有慣例（見 _scan_blind_mode_labels）：Combobox 存的是**顯示標籤字串**，
# 換算回代碼一律查反向 map，不可用 .index()（ORG_MODES 差一位就是前車之鑑）。
ALGO_LABELS = {
    "coordinate_descent": "座標下降（三階段，含可選階段二精修）",
    "powell": "Powell 共軛方向法（實驗性，未真機驗證）",
}

# _FINITE_MOVE_RE / _is_finite_move / MAX_RETRY / WAIT_TIMEOUT /
# WAIT_INTERVAL / JOG_WATCH_INTERVAL / _POLL_QUERIES / HISTORY_MAX /
# STOP_LOCK_TIMEOUT / CONFIG_FILE 只有 DS102Controller 內部用到，已搬到
# ds102_ctrl.py，不在此重新引入。COMM_FAIL_THRESHOLD 兩邊都用到，已在
# 上方 import 區塊重新引入。
#
# ⚠ NON_RECORDING_JSON 在 main_ai.py 邏輯裡同樣沒被直接用到（IDE 會標成
# unused import），但不能移除——verify_meter_panel.py 直接讀
# `main_ai.NON_RECORDING_JSON` 做斷言，拿掉會讓那支回歸測試整支炸掉
# （2026-08-17 實際踩過：清死 import 時被重跑測試抓到）。改動這裡前務必
# 先跑 verify_meter_panel.py，不要只憑 grep 或 IDE 診斷判斷。

# 背景位置刷新間隔（秒）。每輪對每個已啟用軸送一筆 POS?（實測約 56ms／筆），
# 四軸約 0.22s，設 0.5s 讓序列埠仍有餘裕給移動中的到位輪詢。
# 可由 recordings/app_settings.json 的 position_poll_interval 覆寫（純 UI 節奏，無安全含意）。
POSITION_POLL_INTERVAL = _app_setting_num(_app_settings, "position_poll_interval", 0.5, float)

# 光功率浮動視窗的固定尺寸（win.resizable(False, False)，不讓使用者調整）。
# 定位邏輯見 DS102GUI._pm_float_position()。
PM_FLOAT_W = 260
PM_FLOAT_H = 200
# GUI LOG 文字框保留的最大行數，超過就從頭截掉。
# 可由 app_settings.json 的 log_text_max_lines 覆寫。
LOG_TEXT_MAX_LINES = _app_setting_num(_app_settings, "log_text_max_lines", 2000, int)
# UI 座標重繪間隔（毫秒）。純重繪、不碰序列埠，所以只需要跟得上
# POSITION_POLL_INTERVAL(0.5s) 的資料更新即可，設 10ms 純屬浪費。
# 可由 app_settings.json 的 ui_redraw_interval 覆寫。
UI_REDRAW_INTERVAL = _app_setting_num(_app_settings, "ui_redraw_interval", 100, int)
# 橫幅「合併視窗」：這段時間內連續來的訊息會排隊依序顯示（避免互相覆蓋），
# 超過就直接換掉目前這則——超過表示新訊息多半是使用者剛按下按鈕的回饋，
# 那不該排隊等好幾秒才出現。
# 可由 app_settings.json 的 banner_coalesce_sec 覆寫。
BANNER_COALESCE_SEC = _app_setting_num(_app_settings, "banner_coalesce_sec", 1.0, float)
# 光功率面板自動輪詢的預設間隔（秒）。
# 可由 app_settings.json 的 meter_poll_interval 覆寫。
METER_POLL_INTERVAL = _app_setting_num(_app_settings, "meter_poll_interval", 0.5, float)
# 尋光分頁即時軌跡圖的重繪間隔（毫秒）。matplotlib 的 draw_idle() 比
# Label.config() 貴得多，資料源（sample_cb）本身只有約 1Hz，不必追到
# UI_REDRAW_INTERVAL 那麼快。
# 可由 app_settings.json 的 scan_plot_redraw_interval 覆寫。
SCAN_PLOT_REDRAW_INTERVAL = _app_setting_num(
    _app_settings, "scan_plot_redraw_interval", 250, int
)

# 顏色主題已搬到 ui_theme.py（2026-08-31），上方的 `from ui_theme import
# ...` 把十個名字重新引入本模組命名空間，底下 445 處 `CLR_*` 引用與
# monkeypatch `main_ai.CLR_*` 都不受影響。搬出去的理由：CLR_* 是唯一橫跨
# 全部七個分頁的 UI 常數，留在 main_ai.py 會讓任何想搬出去的 GUI 程式碼
# 反過來 import main_ai.py，形成循環相依。完整說明見 ui_theme.py docstring。
#
# ⚠ `_app_settings` / `_app_setting_num` 沒有跟著搬去 ui_theme.py：它們
# 定義在 ds102_ctrl.py（HISTORY_MAX 需要），搬走會讓 ds102_ctrl.py 反過來
# import ui_theme.py，變成新的環。上方各項 UI 節奏常數因此維持原樣。



# =============================================================================
# 後端控制器
#
# DS102Controller 已搬到 ds102_ctrl.py（2026-08-17，機械式搬移，行為不變）。
# 上方 import 區塊已用 `from ds102_ctrl import DS102Controller` 重新引入
# 這個名字，DS102GUI.__init__ 的 `self.ctrl = DS102Controller()` 不必修改。
# =============================================================================

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

        ⚠ 刻意不在這裡附加 μm 估算：這顆 Label 是 width=10 的固定寬度
        （六軸要並排塞進同一條 StatusBar），Consolas 10pt bold 底下連
        原本的 pulse 數字都常常頂到邊界，再接一段「≈ 1234.5 μm」只會
        被截斷或把整條 bar 撐爆。μm 估算的必顯示位置是儀表板
        （_redraw_positions），這裡不做。
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
# 長時間背景作業註冊表
# =============================================================================
@dataclass(frozen=True)
class LongOperation:
    """
    一項「長時間背景作業」的註冊項目（2026-08-31 前置1，見
    docs/modularization.md）。

    在這之前，每新增一種長時間作業（重播／全軸原點復歸／原點復歸重現性
    量測／尋光），作者都必須自己記得手動接進 6 個散落的位置：
    `_update_stat_ui` 的 busy 判斷、`_start_poller` 的經過時間、`_do_stop`、
    `_on_escape`、`_toggle_connect` 的中斷分支、`_on_close`。實際上**已經
    漏過**——尋光在前四個位置裡漏了三個（architect 2026-08-31 複審抓到，
    症狀是尋光中按 Esc／■ Stop 滑台停一下又自己繼續走）。改成註冊表之後，
    新增作業只要在 `_register_long_ops()` 多寫一筆，漏接在結構上不可能發生。

    🔴 **這張表只服務兩件事：「UI 忙碌顯示」與「停止請求分派」。**
    絕不可以拿它當移動守衛，也不可以把 `ctrl.motion_active` /
    `ctrl.scanning_active` 併進來——那是 docs/fiber-scan.md 明列的紅線
    （`motion_active` 只判斷「要不要占用其他硬體資源」；把 `scanning_active`
    放進移動守衛曾讓所有收斂測試卡死）。

    🔴 **除了 `key` / `label`，每個欄位都是 callable，這是刻意的。**
    註冊發生在 `__init__` 早期，而部分依賴物件（例如
    `_org_repeat_elapsed_var` 要等 `_build_card_origin_repeatability()`、
    `_active_scanner` 要等使用者按下開始）當下根本還不存在。全部走 callable
    表示每個欄位都在「被呼叫的當下」才解析 `self` 的屬性，註冊表因此完全
    不受 `__init__` 內的建構順序影響——這正是 docs/modularization.md 第 21
    行點名的那類初始化順序相依，這裡從一開始就避開，而不是靠註解提醒。

    欄位：
      key          內部識別字串（log 與測試用，不顯示給使用者）
      label        給人看的作業名稱，`_busy_reasons()` 用它組訊息
      is_running   () -> bool，作業是否進行中（唯一的 busy 判準）
      request_stop () -> None，請求該作業收工；**None 代表這項作業沒有
                   軟體停止機制**（全軸原點復歸就是這種：`origin_all()`
                   不收 stop_event，只能靠 `ctrl.stop()` 讓當下那一軸的
                   `_wait_origin_done()` 結束）。呼叫端一律要判斷 None。
      started_at   () -> float，本輪起算的 time.time() 基準；與
                   `show_elapsed` 必須成對出現，其一為 None 就不計時。
      show_elapsed (str) -> None，把格式化好的 "MM:SS" 寫進對應的 StringVar。
    """

    key: str
    label: str
    is_running: Callable[[], bool]
    request_stop: Optional[Callable[[], None]] = None
    started_at: Optional[Callable[[], float]] = None
    show_elapsed: Optional[Callable[[str], None]] = None


@dataclass(frozen=True)
class PowerReading:
    """
    「最近一次成功的光功率讀值」快取（2026-08-31 前置2，見
    docs/modularization.md〈九〉）。

    在這之前，這份快取是 `_pm_last_value` / `_pm_last_ok_time` 兩個裸欄位，
    由「光功率」與「尋光」**兩個分頁各自直接指派**。2026-08-31 的模組化複審
    把它列為專案裡唯一一組「跨分頁共用的未命名可變狀態」——比「互相呼叫對方
    的私有方法」更難查，因為連呼叫點都 grep 不到，只能靠記得去搜欄位名。
    收斂成一個具名物件之後，寫入只走 `_pm_note_reading()` /
    `_pm_clear_reading()`，讀取只走 `self._pm_reading`。

    🔴 **frozen（不可變）是刻意的，而且是這個設計唯一真正的技術理由。**
    寫入端全部在 Tk 主執行緒（背景輪詢與單次查詢都走 `root.after` 回主
    執行緒；`_scan_plot_extend` 掛在 `_redraw_scan_plot` 這條 `root.after`
    鏈上），但**讀取端不是**：`_get_last_pm_value()` 由
    `DS102Controller._record_data_point()` 在 `_wait_axis_stop()` 的等待
    迴圈裡呼叫，跑在移動執行緒上。原本 value 與 ok_time 是兩行各自指派，
    移動執行緒有機會讀到「新的 value 配舊的 ok_time」這種撕裂組合；換成
    frozen dataclass 後，更新是**單一次屬性重新指派**（GIL 下不可分割），
    讀取端拿到的必定是同一次讀值的完整快照。

    ⚠ 這只是把原本就存在的跨執行緒讀取變得一致，**沒有**、也不宣稱新增
    任何鎖保護。不要因為它現在有名字就假設它是執行緒安全的容器——真正的
    保證只有「單一寫入執行緒 ＋ 不可變快照」這一條。

    欄位：
      value    最近一次成功讀值（dBm）。None＝從未讀到過可信讀值
      ok_time  該次讀值當下的 time.time()。0.0＝從未成功讀到（`_pm_update_age_label`
               用 `<= 0` 判斷要不要顯示「—」，沿用原本 `_pm_last_ok_time` 的語意）
      source   "meter"＝光功率分頁自己的查詢／背景輪詢；"scan"＝尋光分頁的
               sample callback 轉貼。**純診斷用**，沒有任何邏輯依它分流——
               加這個欄位是為了讓將來看到一筆可疑讀值時，能直接知道它從哪
               條路徑進來，不必回頭推理當下 scanning_active 是什麼狀態。
    """

    value: Optional[float] = None
    ok_time: float = 0.0
    source: str = ""


# =============================================================================
# 主 GUI
# =============================================================================
class DS102GUI:

    def __init__(self, root: tk.Tk):
        self.root = root
        self.ctrl = DS102Controller()
        self.ctrl.set_log_callback(self._on_log_entry)
        self.ctrl.set_alarm_callback(self._on_alarm)
        self.ctrl.set_power_reader(self._get_last_pm_value)

        # ── B 類安全設定檔（safety_settings.json）驗證結果 ──
        # _safety_setting_rejections 是 ds102_ctrl 模組層級清單，在模組載入
        # 當下（比任何 DS102Controller 實例、比 init_runtime() 的 FileHandler
        # 都早）就已經定案，程式生命週期內不會再變動。裡面的訊息只進了
        # Python 內建 logging，不會進到 GUI 的 LOG 分頁（那邊是 ctrl._log()
        # 寫入 action_history 的另一條獨立路徑）。這裡逐則補寫一次，讓「橫幅
        # 說有 N 項 → 使用者去 LOG 分頁篩 WARN → 看到 N 條對應明細」這條路徑
        # 成立。橫幅本身的顯示（會用到 _flash_banner，需要 _build_banner()
        # 先建好 widget）放在 __init__ 尾端、_build_notebook() 之後。
        for _msg in _safety_setting_rejections:
            self.ctrl._log("WARN", f"[安全設定] {_msg}")

        # ── 尋光（FiberAlignmentScanner，今天稍早完成的訊號有效性判準已驗證過）──
        self._scanning = threading.Event()  # GUI 層忙碌旗標，比照 self._homing 的既有模式
        self._active_scanner: Optional["FiberAlignmentScanner"] = None
        self._scan_axis_step_vars = {}  # {軸名: tk.StringVar}，_build_tab_scan 建立分頁時才會實際填入
        # ⚠ _scan_stage2_var 不在這裡建立——它跟其餘會被 scanner_config.json
        # 覆寫預設值的欄位一樣，要等 _build_tab_scan 讀到
        # self._scanner_cfg_pending 後才建立（見該處 `cfg.get("enable_stage2",
        # False)`）。若先建立成寫死的 BooleanVar(value=False)，存檔的
        # enable_stage2 欄位就永遠讀不回來（測試案例 22e 抓到：使用者勾選
        # 過就存檔，下次開程式勾選框卻永遠回到未勾選）。
        self._scan_status_var = tk.StringVar(value="尚未開始")
        self._scan_elapsed_var = tk.StringVar(value="00:00")
        # 上一輪搜尋的結束狀態，供「匯出 Excel」把「完成／中止」與中止原因
        # 寫進報表〈摘要〉。刻意不從 self._active_scanner 讀——_on_scan_done
        # 已經把它清成 None（那是必要的，scanner 實例不該被 GUI 續抱），
        # 使用者按匯出時早就沒有 scanner 可問了。
        self._scan_last_completed: Optional[bool] = None
        self._scan_last_abort_reason: Optional[str] = None
        self._scan_start_time = 0.0
        # 設定檔載入延後套用：這裡的 tk.StringVar/BooleanVar 要等
        # _build_tab_scan 建立分頁時才會存在，先把設定檔內容存成普通 dict，
        # 由 _build_tab_scan 決定哪些欄位用它覆寫函式庫預設值。
        self._scanner_cfg_pending: dict = _load_scanner_config(log=self.ctrl._log)

        # ── 尋光即時軌跡圖（第三階段新增）──
        # sample_cb 跑在 scanner 的背景執行緒，matplotlib／tkinter API 都不能
        # 在那裡呼叫（Python 3.14 tkinter 會丟 RuntimeError）。做法是資料寫入
        # 與重繪分離：背景執行緒只把 Sample 塞進這個 list，_redraw_scan_plot
        # 固定節奏在主執行緒把它清空、套進圖表。
        self._scan_plot_lock = threading.Lock()
        self._scan_plot_pending: List = []  # List[Sample]，背景執行緒寫入、主執行緒讀取清空
        self._scan_samples: List = []  # List[Sample]，完整歷史（主執行緒專用，供重繪整張圖）
        self._scan_best_power: Optional[float] = None  # 目前為止最佳功率，供收斂圖最佳線與數值摘要
        self._scan_sample_count = 0
        self._scan_cur_power_var = tk.StringVar(value="—")
        self._scan_best_power_var = tk.StringVar(value="—")
        self._scan_n_var = tk.StringVar(value="0")
        self._scan_coord_var = tk.StringVar(value="—")

        # ── 光功率計（HP 8153A，獨立於 DS102 連線）──
        self.meter: Optional[HP8153APowerMeter] = None
        self._pm_auto_poll = tk.BooleanVar(value=False)
        self._pm_poll_interval = tk.StringVar(value=str(METER_POLL_INTERVAL))
        self._pm_comm_failures = 0
        # 最近一次成功讀值（供 CSV 記錄與「幾秒前」標籤取用）。
        # 🔴 唯一寫入者是 _pm_note_reading() / _pm_clear_reading()，不要在
        # 任何地方直接指派這個欄位——「尋光」分頁曾經直接寫舊的兩個裸欄位，
        # 那正是前置2 要消滅的東西（見 PowerReading 的 docstring）。
        self._pm_reading = PowerReading()
        self._pm_power_var = tk.StringVar(value="—")
        self._pm_unit_var = tk.StringVar(value="")
        self._pm_status_var = tk.StringVar(value="未連線")
        self._pm_age_var = tk.StringVar(value="—")
        self._pm_gpib_addr_var = tk.StringVar(value="21")
        self._pm_channel_var = tk.StringVar(value="2")
        self._pm_wavelength_var = tk.StringVar(value="1550")
        self._pm_range_mode_var = tk.StringVar(value="auto")  # "auto" / "manual"
        self._pm_range_manual_var = tk.StringVar(value="-20")
        # 獨立浮動視窗：調滑台軸時不必切到「光功率」分頁就能看到讀值。
        # 不記憶上次開關狀態與視窗位置——每次啟動預設關閉。
        self._pm_float_win: Optional[tk.Toplevel] = None
        self._pm_float_open = tk.BooleanVar(value=False)
        # 開機時只把設定檔的值填進輸入框，不觸發連線——與 DS102 一致。
        _meter_cfg = _load_meter_config(log=self.ctrl._log)
        if "gpib_address" in _meter_cfg:
            self._pm_gpib_addr_var.set(str(_meter_cfg["gpib_address"]))
        if "channel" in _meter_cfg:
            self._pm_channel_var.set(str(_meter_cfg["channel"]))
        if "wavelength_nm" in _meter_cfg:
            self._pm_wavelength_var.set(str(_meter_cfg["wavelength_nm"]))

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

        # 原點復歸重現性量測進行中（見 _build_card_origin_repeatability）。
        # 比照 _homing/_scanning 的既有模式：_update_stat_ui 靠這個旗標
        # 判斷要不要鎖定驅動按鈕。獨立的 stop_event 是因為量測迴圈跑在
        # 自己的背景執行緒、沒有現成旗標可用（跟 _stop_playback 是同一類，
        # 不是跟 _homing 共用）。
        self._org_repeat_running = threading.Event()
        self._org_repeat_stop_event = threading.Event()
        self._org_repeat_start_time = 0.0

        # 長時間背景作業註冊表（見 LongOperation 的說明）。
        # 放在這裡純粹是為了可讀性——上面四種作業的旗標剛好都在眼前；
        # 正確性上它可以放在 __init__ 的任何位置，因為每個欄位都是
        # callable、一律延後到被呼叫的當下才解析 self 的屬性。
        self._register_long_ops()

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
        # 初始值刻意是「—」不是「0」：未連線時顯示 0 等於畫一個「軸壓在
        # 限位開關上」的假座標（0 幾乎就落在限位上），這是既有的顯示原則。
        self._ctrl_pos_var = tk.StringVar(value="—")
        # 移動控制分頁「Position:」旁的 um 估算附加顯示，見 _update_ctrl_pos_um()。
        # 用 trace 掛在 _ctrl_pos_var 上——它現在的唯一寫入點是
        # _redraw_positions()（Tk 主執行緒、UI_REDRAW_INTERVAL 節奏），
        # 切軸時下一輪重繪就會帶到新軸的座標，不需要額外監聽軸切換事件。
        self._ctrl_pos_um_var = tk.StringVar(value="")
        self._ctrl_pos_var.trace_add("write", self._update_ctrl_pos_um)
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
        # 軸機械校正參數卡片會在 _build_notebook() 內用 ctrl.axis_calib
        # 回填 Entry（跟其餘設定檔不同，那些卡片建構時先留空、事後才靠
        # _refresh_points() 這類方法補上；這裡選擇提早載入，讓卡片一次
        # 建對，不必額外補一個「建構後回填 Entry」的路徑）。
        self.ctrl.load_axis_calib()
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
        self._start_meter_poll_worker()  # 光功率背景輪詢（獨立於上述兩條迴圈）

        # 安全設定檔驗證結果的橫幅提醒——本地設定檔驗證，跟有沒有連硬體
        # 無關，開機、視窗建好後立刻顯示一次（不等連線）。LOG 分頁的對應
        # 明細已在上面（ctrl 建立後）補寫完畢，此處只負責橫幅文案。
        _n_rejected = len(_safety_setting_rejections)
        if _n_rejected == 1:
            self._flash_banner(f"⚠ 安全設定檔：{_safety_setting_rejections[0]}")
        elif _n_rejected >= 2:
            self._flash_banner(
                f"⚠ 安全設定檔有 {_n_rejected} 項欄位超出合法範圍，"
                f"已改用內建預設值（詳見 LOG／儀表板「系統狀態」）"
            )

        self.ctrl._log("INFO", "DS102  圖形化控制器啟動")

    # =========================================================================
    # 長時間背景作業註冊表
    #
    # 🔴 新增任何「跑在自己的背景執行緒、會持續驅動硬體」的作業時，
    #    **唯一該做的事就是在 _register_long_ops() 多寫一筆**。不要再自己
    #    去 _update_stat_ui / _start_poller / _do_stop / _on_escape /
    #    _toggle_connect / _on_close 手動接線——那六個位置現在全部改成
    #    走這張表，手動接線只會製造第二份會走鐘的真相來源。
    # =========================================================================
    def _register_long_ops(self):
        """
        建立長時間背景作業註冊表。欄位語意見 LongOperation 的 docstring。

        目前四項，與改用註冊表之前 `_update_stat_ui` 的 busy 判斷逐項對應
        （playback_running / _homing / _scanning / _org_repeat_running），
        沒有新增也沒有移除任何一項。
        """
        # ⚠ 每個欄位一律包成 lambda，不要寫成 `self._stop_playback.set` 這種
        # 綁定方法。綁定方法會在註冊當下就把 Event/StringVar 實例抓進閉包，
        # 於是這張表反過來要求「註冊必須排在那些物件建立之後」——正是本次
        # 重構要消滅的那種初始化順序相依；更糟的是若日後有人重新指派
        # `self._scanning = threading.Event()`，註冊表會靜默地繼續盯著舊物件。
        self._long_ops: Tuple[LongOperation, ...] = (
            LongOperation(
                key="playback",
                label="行程重播",
                is_running=lambda: self.ctrl.playback_running,
                request_stop=lambda: self._stop_playback.set(),
                # 重播沒有經過時間顯示（它有自己的 x/y 步數進度），
                # started_at / show_elapsed 留 None。
            ),
            LongOperation(
                key="homing",
                label="全軸原點復歸",
                is_running=lambda: self._homing.is_set(),
                # 🔴 request_stop 是 None，不是漏寫：ctrl.origin_all() 不收
                # stop_event，沒有任何軟體旗標能讓它提前收工。唯一能縮短它的
                # 是呼叫端本來就會送的 ctrl.stop()——那會讓當下那一軸的
                # _wait_origin_done() 提早結束，但整批仍會依序跑完其餘軸。
                # 要改成可中止必須動 ds102_ctrl.origin_all() 的簽章，屬於
                # 另一件事，不在前置1 的範圍內。
            ),
            LongOperation(
                key="scan",
                label="尋光",
                is_running=lambda: self._scanning.is_set(),
                request_stop=lambda: self._request_stop_scanner(),
                started_at=lambda: self._scan_start_time,
                show_elapsed=lambda txt: self._scan_elapsed_var.set(txt),
            ),
            LongOperation(
                key="org_repeat",
                label="原點復歸重現性量測",
                is_running=lambda: self._org_repeat_running.is_set(),
                request_stop=lambda: self._org_repeat_stop_event.set(),
                started_at=lambda: self._org_repeat_start_time,
                # ⚠ 這一項是延後求值最有感的地方：_org_repeat_elapsed_var
                # 要等 _build_card_origin_repeatability() 才建立，而
                # _register_long_ops() 跑在 _build_notebook() 之前。
                show_elapsed=lambda txt: self._org_repeat_elapsed_var.set(txt),
            ),
        )

    def _request_stop_scanner(self):
        """
        請求尋光背景執行緒收工。

        `_active_scanner` 是 Optional 且會動態變成 None（_on_scan_done 收尾
        時放掉 scanner 實例），所以 None 判斷必須留在這裡、而不是留給
        `_request_stop_long_ops()` 的迴圈——註冊表對外的約定是
        「request_stop 不為 None 時，任何時候呼叫都必須是安全且冪等的」。
        """
        scanner = self._active_scanner
        if scanner is not None:
            scanner.request_stop()

    def _busy_reasons(self) -> List[str]:
        """目前進行中的長時間作業名稱（給人看的），沒有則回空 list。"""
        return [op.label for op in self._long_ops if op.is_running()]

    def _any_long_op_running(self) -> bool:
        """是否有任何長時間作業進行中。`_update_stat_ui` 的 busy 判準。"""
        return any(op.is_running() for op in self._long_ops)

    def _request_stop_long_ops(self):
        """
        把停止請求分派給所有長時間背景作業。

        **刻意不先判斷 `is_running()`**，理由有二：
        1. 三個 request_stop 都是冪等的旗標設定，而且各自的啟動流程
           （`_do_play_rec` 的 `_stop_playback.clear()`、
           `_do_start_org_repeat` 的 `_org_repeat_stop_event.clear()`）
           都會在下一輪開始前把旗標清掉，殘留的 set 不會誤殺下一輪。
        2. 「進行中旗標」與「停止目標」不是同一個物件，兩者的 set/clear
           時序有極短的交錯窗口（例如 `_scanning.set()` 早於
           `_active_scanner = scanner`）。用 is_running() 當閘門等於把這個
           窗口變成漏接窗口，而漏接正是這張表要消滅的東西。

        每一項各自 try/except：這是「使用者按下停止」的路徑，任何一項
        request_stop 拋例外都不可以吃掉其餘作業的停止請求。
        """
        for op in self._long_ops:
            if op.request_stop is None:
                continue
            try:
                op.request_stop()
            except Exception:  # 停止路徑不可因單一作業失敗而中斷
                logger.exception(f"請求停止長時間作業 [{op.key}] 失敗")

    def _update_long_op_elapsed(self):
        """
        更新各長時間作業的經過時間顯示。由 `_start_poller` 每輪呼叫。

        只在作業進行中更新，收工後 StringVar 保留最後一次的值——
        `_on_scan_done()` 的完成橫幅會去讀 `_scan_elapsed_var.get()`
        把總耗時寫進訊息，歸零會讓那則訊息永遠顯示 00:00。
        """
        now = time.time()
        for op in self._long_ops:
            if op.started_at is None or op.show_elapsed is None:
                continue
            if not op.is_running():
                continue
            elapsed = int(now - op.started_at())
            op.show_elapsed(f"{elapsed // 60:02d}:{elapsed % 60:02d}")

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
        self._banner_queue: List[Tuple[str, int, str]] = []

    def _flash_banner(self, msg: str, ms: int = 8000, color: str = CLR_WARN):
        """
        顯示提醒，ms 毫秒後自動收起。

        短時間內來第二則訊息時**排隊依序顯示**，不會互相覆蓋。
        以前是直接覆寫同一個變數並重設倒數——連線成功時若同時有
        「已從設定檔還原」與「復歸樣式未設定」兩則，第一則會在顯示 0ms
        後被蓋掉，實質上永遠看不到。

        `color` 預設 `CLR_WARN`（與改動前所有既有呼叫點行為一致）。
        2026-08-21 為「原點復歸重現性量測中止在非原點狀態」這種比一般
        警告更嚴重的情境新增——呼叫端可傳 `CLR_DANGER` 拉高視覺急迫性，
        不需要另外新增一套獨立的橫幅機制。
        """
        try:
            if self._banner_after_id:
                # 只有「幾乎同時」湧入的訊息才排隊。這正是佇列要解決的情境：
                # 連線成功時「已從設定檔還原」與「復歸樣式未設定」兩則在同一個
                # 事件裡連續觸發，舊寫法會讓第一則顯示 0ms 就被蓋掉。
                #
                # 但若目前這則已經顯示一段時間，新訊息多半是使用者剛按下
                # 某個按鈕的回饋——那不該排隊等好幾秒才出現，直接換掉。
                if time.time() - self._banner_shown_at < BANNER_COALESCE_SEC:
                    if (msg, ms, color) not in self._banner_queue:
                        self._banner_queue.append((msg, ms, color))
                    return
                self.root.after_cancel(self._banner_after_id)
                self._banner_after_id = None
            self._show_banner_now(msg, ms, color)
        except tk.TclError:
            pass  # 關閉流程中 widget 可能已銷毀

    def _show_banner_now(self, msg: str, ms: int, color: str = CLR_WARN):
        self._banner_shown_at = time.time()
        self._banner_var.set(msg)
        self._banner.config(bg=color)
        self._banner_lbl.config(bg=color, fg="white")
        self._banner_close.config(bg=color, fg="white")
        self._banner_after_id = self.root.after(ms, self._hide_banner)

    def _hide_banner(self):
        """收起目前訊息；佇列裡還有就接著顯示下一則。"""
        try:
            if self._banner_after_id:
                self.root.after_cancel(self._banner_after_id)
                self._banner_after_id = None
            if self._banner_queue:
                nxt_msg, nxt_ms, nxt_color = self._banner_queue.pop(0)
                self._show_banner_now(nxt_msg, nxt_ms, nxt_color)
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
        # 開機預設最大化，不猜固定像素尺寸——新卡片陸續增多後，沒有明確
        # geometry() 時 Tk 只照 widget 最小需求開窗，內容容易被擠壓成需要
        # 捲動。用 state("zoomed")（Windows 專用，保留標題列/工作列）自動
        # 吃滿螢幕可用空間，使用者仍可自行拖曳還原。
        # 🔴 "zoomed" 是 Windows 專用 state，Linux（含 Xvfb 測試環境）會丟
        # TclError，失敗時退回 attributes("-zoomed", True)，兩者都不支援
        # 時保持預設視窗大小（不影響功能，只是開窗不是最大化）。
        try:
            self.root.state("zoomed")
        except tk.TclError:
            try:
                self.root.attributes("-zoomed", True)
            except tk.TclError:
                pass
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
            ("光功率", self._build_tab_power),
            ("尋光", self._build_tab_scan),
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
        # 安全設定檔驗證結果是靜態值（模組載入時就定案，程式生命週期內不會
        # 再變動），在此直接讀一次決定文字即可，不掛進 _update_stat_ui 的
        # 100ms 週期性重繪——那是給連線狀態／可用軸數這類會變動的值用的。
        _n_rejected = len(_safety_setting_rejections)
        _safety_val = "內建預設" if _n_rejected == 0 else f"⚠ {_n_rejected} 項已回退"
        for i, (key, lbl, val) in enumerate(
            [
                ("conn", "連線狀態", "未連線"),
                ("axes", "可用軸數", "—"),
                ("ems", "EMS", "正常"),
                ("play", "重播狀態", "閒置"),
                ("dlog", "數據記錄", "停止"),
                ("safety", "安全設定", _safety_val),
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

    def _build_card_axis_calib(self, scr):
        """
        軸機械校正參數：純顯示用的 pulse→μm 估算換算表。

        跟 DRDIV 是兩個不同語意的數字，刻意不互相關聯或自動代入
        （division 是使用者自己讀實體開關填的，DRDIV 是控制器內部一個
        跟實體開關無關的軟體暫存器，見 ds102_ctrl.py 的 axis_drdiv 註解）。
        這張卡片完全不影響任何移動/限位/教點邏輯，錯了也不會有警報。
        """
        calib_card = self._card(
            scr,
            "軸機械校正參數（僅估算顯示，不影響任何移動/限位/教點判斷）",
        )
        tk.Label(
            calib_card,
            text="輸入螺桿導程、馬達步進角、目前手動轉到的分割倍數，"
                 "換算出 pulse↔μm 的估算比例，附加顯示在座標旁邊。",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 8),
            justify="left",
        ).pack(anchor="w", padx=12, pady=(2, 0))
        tk.Label(
            calib_card,
            text="⚠ 估算值——導程/步進角/分度值任一填錯，顯示的 μm 就會是錯的，"
                 "不會有任何警報或攔截。",
            bg=CLR_CARD,
            fg=CLR_WARN,
            font=("Segoe UI", 8),
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 4))

        calib_grid = tk.Frame(calib_card, bg=CLR_CARD)
        calib_grid.pack(fill="x", padx=12, pady=(0, 8))
        headers = ["軸", "導程 (mm)", "步進角 (度)", "分度值", "韌體 DRDIV 參考", "目前生效", ""]
        widths = [4, 12, 12, 10, 16, 20, 6]
        for c, (h, w) in enumerate(zip(headers, widths)):
            tk.Label(
                calib_grid,
                text=h,
                bg=CLR_CARD,
                fg=CLR_MUTED,
                font=("Segoe UI", 9),
                width=w,
            ).grid(row=0, column=c)

        self._calib_vars: Dict[str, Dict[str, tk.StringVar]] = {}
        self._calib_cur_vars: Dict[str, tk.StringVar] = {}
        self._calib_drdiv_vars: Dict[str, tk.StringVar] = {}
        for r, ax in enumerate(AXES, start=1):
            tk.Label(
                calib_grid,
                text=ax,
                bg=CLR_CARD,
                fg=CLR_TEXT,
                font=("Segoe UI", 9, "bold"),
                width=4,
            ).grid(row=r, column=0, pady=2)

            existing = self.ctrl.axis_calib.get(ax) or {}
            lead_v = tk.StringVar(
                value="" if "lead_pitch_mm" not in existing
                else str(existing["lead_pitch_mm"])
            )
            angle_v = tk.StringVar(
                value="" if "step_angle_deg" not in existing
                else str(existing["step_angle_deg"])
            )
            div_v = tk.StringVar(
                value="" if "division" not in existing
                else str(existing["division"])
            )
            self._calib_vars[ax] = {"lead": lead_v, "angle": angle_v, "div": div_v}
            ttk.Entry(calib_grid, textvariable=lead_v, width=12).grid(
                row=r, column=1, padx=4, pady=2
            )
            ttk.Entry(calib_grid, textvariable=angle_v, width=12).grid(
                row=r, column=2, padx=4, pady=2
            )
            ttk.Entry(calib_grid, textvariable=div_v, width=10).grid(
                row=r, column=3, padx=4, pady=2
            )

            drdiv_v = tk.StringVar(value="—")
            self._calib_drdiv_vars[ax] = drdiv_v
            tk.Label(
                calib_grid,
                textvariable=drdiv_v,
                bg=CLR_CARD,
                fg=CLR_MUTED,
                font=("Consolas", 8),
                width=16,
                anchor="w",
            ).grid(row=r, column=4, padx=4, pady=2)

            cur_v = tk.StringVar(value="未設定")
            self._calib_cur_vars[ax] = cur_v
            tk.Label(
                calib_grid,
                textvariable=cur_v,
                bg=CLR_CARD,
                fg=CLR_MUTED,
                font=("Consolas", 8),
                width=20,
                anchor="w",
            ).grid(row=r, column=5, padx=4, pady=2)

            ttk.Button(
                calib_grid,
                text="清除",
                style="Flat.TButton",
                command=lambda a=ax: self._do_clear_axis_calib(a),
            ).grid(row=r, column=6, padx=4, pady=2)

        ttk.Button(
            calib_card,
            text="套用機械校正參數",
            style="Accent.TButton",
            command=self._apply_axis_calib,
        ).pack(padx=12, pady=(0, 8))

        self._refresh_calib_display()

    def _refresh_calib_display(self):
        """
        重新計算六軸「目前生效」欄的文字（estimate_um(ax, 1) 的結果）。
        不掛進任何輪詢迴圈，只在卡片建構完成、與 _apply_axis_calib 成功後呼叫。
        """
        for ax in AXES:
            v = self.ctrl.estimate_um(ax, 1)
            if v is None:
                self._calib_cur_vars[ax].set("未設定")
            else:
                self._calib_cur_vars[ax].set(f"≈ {v:.5f} μm/pulse")

    def _apply_axis_calib(self):
        """
        套用軸機械校正參數：GUI 端先做型別/留空/部分填寫檢查，
        全過才呼叫 controller 的 set_axis_calib()（那邊仍會再驗證一次，
        但那道防線是防手動編輯 json，不是防這裡漏檢查，所以 GUI 端的
        檢查不能省略）。
        """
        local_errors: List[str] = []
        calib: Dict[str, dict] = {}
        for ax in AXES:
            vars_ = self._calib_vars[ax]
            lead_s = vars_["lead"].get().strip()
            angle_s = vars_["angle"].get().strip()
            div_s = vars_["div"].get().strip()
            filled = [bool(lead_s), bool(angle_s), bool(div_s)]

            if not any(filled):
                continue  # 三欄全空，本次不動這軸
            if not all(filled):
                local_errors.append(f"{ax}: 三個欄位需一起填寫或一起留空")
                continue

            try:
                lead = float(lead_s)
                angle = float(angle_s)
                division = int(div_s)
            except ValueError:
                local_errors.append(f"{ax}: 導程/步進角須為數字、分度值須為整數")
                continue

            # math.isfinite() 排除 inf/nan——float("inf")/float("nan") 不會拋
            # ValueError，且兩者都滿足 x <= 0 為 False，沒有這道檢查會被下面
            # 的正數判斷放行，算出 "≈ nan μm" 這種顯示（GUI 端跟 ds102_ctrl.py
            # 的 set_axis_calib()/estimate_um() 要用同一套檢查，不能只擋一邊）。
            if not math.isfinite(lead) or lead <= 0:
                local_errors.append(f"{ax}: 導程需為正數")
                continue
            if not math.isfinite(angle) or angle <= 0:
                local_errors.append(f"{ax}: 步進角需為正數")
                continue
            if division <= 0:
                local_errors.append(f"{ax}: 分度值必須是正整數")
                continue

            calib[ax] = {
                "lead_pitch_mm": lead,
                "step_angle_deg": angle,
                "division": division,
            }

        if local_errors:
            messagebox.showerror(
                "機械校正參數格式錯誤", "\n".join(local_errors)
            )
            return

        if not calib:
            return  # 全部留空，沒有要更新的軸

        ctrl_errors = self.ctrl.set_axis_calib(calib)
        if ctrl_errors:
            messagebox.showerror(
                "機械校正參數套用失敗", "\n".join(ctrl_errors)
            )
            return

        self._flash_banner("✔ 機械校正參數已存檔（僅影響 μm 估算顯示）", 5000)
        self._refresh_calib_display()

    def _do_clear_axis_calib(self, ax: str):
        """
        清除單一軸的機械校正參數。

        `_apply_axis_calib()` 把「三欄全空」當成「本次不動這軸」（避免使用者
        不小心清掉一欄就整軸消失），所以清除需要一個獨立、明確的入口——
        這顆按鈕就是。這軸本來就沒設定時不彈任何視窗，按下去沒反應是合理的。
        """
        existing = self.ctrl.axis_calib.get(ax)
        if not existing:
            return
        lead = existing.get("lead_pitch_mm")
        angle = existing.get("step_angle_deg")
        division = existing.get("division")
        if not messagebox.askyesno(
            "清除機械校正參數",
            f"確定要清除 {ax} 軸的機械校正參數？\n\n"
            f"目前設定：導程 {lead}mm、步進角 {angle}°、分度值 {division}\n\n"
            f"清除後座標旁邊將不再顯示 {ax} 軸的估算 μm 值，"
            f"直到重新輸入並套用。",
            icon="warning",
            default="no",
        ):
            return
        if self.ctrl.clear_axis_calib(ax):
            self._calib_vars[ax]["lead"].set("")
            self._calib_vars[ax]["angle"].set("")
            self._calib_vars[ax]["div"].set("")
            self._refresh_calib_display()
            self._flash_banner(f"✔ {ax} 軸機械校正參數已清除", 5000)

    # =========================================================================
    # 原點復歸重現性量測（2026-08-21）
    #
    # 自動化「離開原點固定 pulse 數 → GO ORG 復歸 → 讀 POS 殘差」，多輪
    # 多 offset 掃描，統計殘差離散度，評估「軟體座標原點」能否當作光纖
    # 對準的可信基準。核心邏輯在 ds102_ctrl.DS102Controller.
    # measure_homing_repeatability()，這裡只負責蒐集輸入、跑背景執行緒、
    # 把回呼結果畫出來。
    # =========================================================================
    def _build_card_origin_repeatability(self, scr):
        card = self._card(scr, "原點復歸重現性量測")
        tk.Label(
            card,
            text="讓軸離開原點固定距離後送出原點復歸，重複多輪並掃描多個離開\n"
                 "距離，統計復歸後 POS 殘差的離散程度——用來評估軟體座標原點\n"
                 "能不能當作光纖對準的可信基準。每個軸開始量測前會先執行一次\n"
                 "原點復歸建立基準（不假設目前位置就是原點）。",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 8),
            justify="left",
        ).pack(anchor="w", padx=12, pady=(2, 4))
        tk.Label(
            card,
            text="⚠ 量測期間會暫停該軸的韌體軟體限位（結束後自動還原）。過程中\n"
                 "請勿手動點動同一軸。只有成功完成原點復歸的組合才會把座標\n"
                 "強制寫回 0；中途失敗、逾時或被緊急停止中斷則不會歸零，畫面\n"
                 "會明確警示，需重新執行原點復歸後才能繼續其他操作。",
            bg=CLR_CARD,
            fg=CLR_WARN,
            font=("Segoe UI", 8),
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 8))

        # ── 量測軸 ──
        axis_head = tk.Frame(card, bg=CLR_CARD)
        axis_head.pack(fill="x", padx=12, pady=(0, 2))
        tk.Label(
            axis_head, text="量測軸", bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 9)
        ).pack(side="left")
        ttk.Button(
            axis_head, text="全不選", style="Flat.TButton",
            command=lambda: self._set_org_repeat_axis_selection(False),
        ).pack(side="right")
        ttk.Button(
            axis_head, text="全選", style="Flat.TButton",
            command=lambda: self._set_org_repeat_axis_selection(True),
        ).pack(side="right", padx=(0, 4))

        # 固定六軸版面、連線後依 axis_count 動態 enable/disable，停用軸
        # 強制 BooleanVar 設回 False——完全比照尋光分頁 _scan_axis_
        # checkbuttons 的既有邏輯（見 _on_connect_result／_set_axis_btns_state）。
        self._org_repeat_axis_vars: Dict[str, tk.BooleanVar] = {}
        self._org_repeat_axis_checkbuttons: Dict[str, ttk.Checkbutton] = {}
        for row_axes in (("X", "Y", "Z"), ("U", "V", "W")):
            row_f = tk.Frame(card, bg=CLR_CARD)
            row_f.pack(fill="x", padx=12, pady=(2, 0))
            for ax in row_axes:
                var = tk.BooleanVar(value=False)
                self._org_repeat_axis_vars[ax] = var
                cb = ttk.Checkbutton(row_f, text=ax, variable=var)
                cb.pack(side="left", padx=(0, 10))
                self._org_repeat_axis_checkbuttons[ax] = cb

        ttk.Separator(card, orient="horizontal").pack(fill="x", padx=12, pady=(8, 6))

        # ── 測試 offset ──
        off_f = tk.Frame(card, bg=CLR_CARD)
        off_f.pack(fill="x", padx=12, pady=(0, 2))
        tk.Label(
            off_f, text="測試 Offset（pulse）", bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 9)
        ).pack(anchor="w")
        preset_row = tk.Frame(off_f, bg=CLR_CARD)
        preset_row.pack(fill="x", pady=(4, 0))
        # 三顆常用值列在清單上，刻意避開 0——0 幾乎等於原地不動，對重現性
        # 量測沒有意義。🔴 100 不預設勾選（2026-08-21 COM2 實機驗證後改）：
        # 原點落在限位開關上，開關本身有實體作用寬度（實測 X 軸約
        # 100~150 pulse），offset=100 量到的是軸從未真正脫離開關作用區的
        # 資料，不該預設勾選一組先天不具代表性的資料；想量開關寬度的人
        # 仍可自己勾選。
        self._org_repeat_offset_preset_vars: Dict[int, tk.BooleanVar] = {}
        for val, default_checked in ((100, False), (1000, True), (5000, True)):
            var = tk.BooleanVar(value=default_checked)
            self._org_repeat_offset_preset_vars[val] = var
            ttk.Checkbutton(preset_row, text=str(val), variable=var).pack(
                side="left", padx=(0, 14)
            )
        custom_row = tk.Frame(off_f, bg=CLR_CARD)
        custom_row.pack(fill="x", pady=(4, 0))
        tk.Label(
            custom_row, text="自訂（逗號分隔）:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8)
        ).pack(side="left")
        self._org_repeat_offset_custom_var = tk.StringVar(value="")
        ttk.Entry(custom_row, textvariable=self._org_repeat_offset_custom_var, width=22).pack(
            side="left", padx=(4, 0)
        )
        tk.Label(
            off_f,
            text="offset 需大於限位開關作用區（本機 X 軸實測約 100～150 pulse）"
                 "才具代表性，太小的 offset 會被標記為不可比較。",
            bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8), justify="left", wraplength=340,
        ).pack(anchor="w", pady=(4, 0))

        # ── 每組輪數 ──
        trials_row = tk.Frame(card, bg=CLR_CARD)
        trials_row.pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(
            trials_row, text="每組輪數 N:", bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 9)
        ).pack(side="left")
        self._org_repeat_trials_var = tk.StringVar(value="10")
        ttk.Entry(trials_row, textvariable=self._org_repeat_trials_var, width=8).pack(
            side="left", padx=(4, 0)
        )

        # ── 開始/停止 + 狀態 + 已耗時 ──
        ctrl_row = tk.Frame(card, bg=CLR_CARD)
        ctrl_row.pack(fill="x", padx=12, pady=(10, 2))
        self._org_repeat_start_btn = ttk.Button(
            ctrl_row, text="▶ 開始量測", style="Accent.TButton",
            command=self._do_start_org_repeat,
        )
        self._org_repeat_start_btn.pack(side="left")
        # 停止鍵獨立管理，不放進 _drive_buttons（比照 _scan_stop_btn 的
        # 既有先例）——CLAUDE.md 明載這是曾經真實發生的 bug：作業進行中
        # 最需要停止時，停止鍵被整批 disabled 按鈕鎖住。
        self._org_repeat_stop_btn = ttk.Button(
            ctrl_row, text="■ 停止量測", style="Danger.TButton",
            command=self._do_stop_org_repeat, state="disabled",
        )
        self._org_repeat_stop_btn.pack(side="left", padx=(6, 0))
        self._org_repeat_status_var = tk.StringVar(value="尚未開始")
        tk.Label(
            ctrl_row, textvariable=self._org_repeat_status_var, bg=CLR_CARD, fg=CLR_TEXT,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left", padx=(14, 4))
        tk.Label(
            ctrl_row, text="已耗時", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8)
        ).pack(side="left", padx=(14, 2))
        self._org_repeat_elapsed_var = tk.StringVar(value="00:00")
        tk.Label(
            ctrl_row, textvariable=self._org_repeat_elapsed_var, bg=CLR_CARD, fg=CLR_TEXT,
            font=("Consolas", 9, "bold"),
        ).pack(side="left")

        # ── 進度列：目前軸/offset/第幾輪 + 最新殘差 ──
        self._org_repeat_progress_var = tk.StringVar(value="—")
        tk.Label(
            card, textvariable=self._org_repeat_progress_var, bg=CLR_CARD, fg=CLR_MUTED,
            font=("Consolas", 8), anchor="w", justify="left",
        ).pack(fill="x", padx=12, pady=(6, 6))

        # ── 結果摘要：逐組合附加一列，不等全部跑完才顯示 ──
        result_cols = ("axis", "offset", "n", "range", "stdev", "median", "drift", "note")
        result_headers = {
            "axis": "軸", "offset": "Offset", "n": "N", "range": "Range",
            "stdev": "StdDev", "median": "中位數", "drift": "漂移判定", "note": "備註",
        }
        result_widths = {
            "axis": 40, "offset": 60, "n": 40, "range": 70,
            "stdev": 70, "median": 70, "drift": 90, "note": 180,
        }
        self._org_repeat_tree = ttk.Treeview(
            card, columns=result_cols, show="headings", height=6,
        )
        for c in result_cols:
            self._org_repeat_tree.heading(c, text=result_headers[c])
            self._org_repeat_tree.column(c, width=result_widths[c], anchor="center")
        # 標記「offset 未脫離出發側限位開關作用區」的組合——資料本身有效
        # （量得到、統計算得出來），只是跟其他 offset 不可直接比較，用
        # CLR_WARN（不是 CLR_DANGER，那是保留給 origin_lost 這種真正的
        # 失敗／座標系失準情境）。
        self._org_repeat_tree.tag_configure("below_switch", foreground=CLR_WARN)
        self._org_repeat_tree.pack(fill="x", padx=12, pady=(0, 8))

        # ── 存檔路徑：灰字、可選取文字，不彈檔案總管 ──
        path_f = tk.Frame(card, bg=CLR_CARD)
        path_f.pack(fill="x", padx=12, pady=(0, 10))
        tk.Label(
            path_f, text="CSV:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8)
        ).pack(anchor="w")
        self._org_repeat_csv_path_var = tk.StringVar(value="—")
        ttk.Entry(
            path_f, textvariable=self._org_repeat_csv_path_var, state="readonly",
            font=("Consolas", 8),
        ).pack(fill="x", pady=(0, 4))
        tk.Label(
            path_f, text="JSON:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8)
        ).pack(anchor="w")
        self._org_repeat_json_path_var = tk.StringVar(value="—")
        ttk.Entry(
            path_f, textvariable=self._org_repeat_json_path_var, state="readonly",
            font=("Consolas", 8),
        ).pack(fill="x")

    def _set_org_repeat_axis_selection(self, value: bool):
        """全選／全不選——只動未被停用（有實際偵測到）的軸勾選框。"""
        for ax in AXES:
            cb = self._org_repeat_axis_checkbuttons.get(ax)
            if cb is None or cb.instate(["disabled"]):
                continue
            self._org_repeat_axis_vars[ax].set(value)

    def _collect_org_repeat_offsets(self) -> List[int]:
        """
        收集勾選的常用 offset + 自訂欄位（逗號分隔），去重、只留正整數、
        格式錯的片段直接忽略（不因為使用者手滑打錯一個逗號就整批擋下，
        跟尋光分頁「格式錯就讓函式庫走預設」同一種寬容原則）。

        🔴 排除的是「非正數」，不是只排除 0——負的 offset 一樣沒有物理
        意義（方向已經由 direction 決定），而且會讓漂移門檻
        `abs(p_i) > 0.25 * offset` 因為 offset<0 恆成立，每一輪都被
        誤判成「偵測到累積漂移」（architect 2026-08-21 審查建議）。
        """
        offsets: List[int] = []
        for val, var in self._org_repeat_offset_preset_vars.items():
            if var.get():
                offsets.append(val)
        extra_raw = self._org_repeat_offset_custom_var.get().strip()
        if extra_raw:
            for part in extra_raw.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    v = int(part)
                except ValueError:
                    continue
                if v > 0 and v not in offsets:
                    offsets.append(v)
        return offsets

    def _ask_org_repeat_directions(self, axes: List[str]) -> Optional[Dict[str, str]]:
        """
        方向預檢判不出來的軸，彈一個小 modal 要求使用者手動指定
        CW/CCW（radiobutton）。取消回傳 None，呼叫端據此整個放棄量測。

        純 UI 互動、不牽涉序列通訊，直接跑在主執行緒、用 wait_window()
        阻塞等待使用者操作即可，不需要另開背景執行緒。
        """
        win = tk.Toplevel(self.root)
        win.title("指定量測方向")
        win.configure(bg=CLR_BG)
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)

        tk.Label(
            win,
            text="下列軸目前未壓在任何限位上，無法自動判定「離開原點」的方向，\n"
                 "請手動指定（該軸離開原點時應該往哪個方向走）：",
            bg=CLR_BG, fg=CLR_TEXT, font=("Segoe UI", 9), justify="left",
        ).pack(padx=16, pady=(14, 8), anchor="w")

        dir_vars: Dict[str, tk.StringVar] = {}
        for ax in axes:
            row = tk.Frame(win, bg=CLR_BG)
            row.pack(fill="x", padx=16, pady=2)
            tk.Label(
                row, text=f"{ax} 軸:", bg=CLR_BG, fg=CLR_TEXT, width=6, anchor="w",
                font=("Segoe UI", 9),
            ).pack(side="left")
            var = tk.StringVar(value="CW")
            dir_vars[ax] = var
            ttk.Radiobutton(row, text="CW", variable=var, value="CW").pack(
                side="left", padx=(0, 8)
            )
            ttk.Radiobutton(row, text="CCW", variable=var, value="CCW").pack(side="left")

        outcome: Dict[str, Optional[Dict[str, str]]] = {"value": None}

        def _ok():
            outcome["value"] = {ax: v.get() for ax, v in dir_vars.items()}
            win.destroy()

        def _cancel():
            outcome["value"] = None
            win.destroy()

        btn_row = tk.Frame(win, bg=CLR_BG)
        btn_row.pack(fill="x", padx=16, pady=(10, 14))
        ttk.Button(btn_row, text="取消", style="Flat.TButton", command=_cancel).pack(
            side="right"
        )
        ttk.Button(btn_row, text="確定", style="Accent.TButton", command=_ok).pack(
            side="right", padx=(0, 8)
        )
        win.protocol("WM_DELETE_WINDOW", _cancel)
        win.wait_window()
        return outcome["value"]

    def _do_start_org_repeat(self):
        if not self.ctrl.connected:
            self._flash_banner("原點復歸重現性量測需要先連線 DS102")
            return
        if self.ctrl.ems_active:
            self._flash_banner("緊急停止中，請先解除後再開始量測")
            return
        if (
            self.ctrl.measuring_active
            or self._org_repeat_running.is_set()
            or self.ctrl.playback_running
            or self.ctrl.scanning_active
            or self._homing.is_set()
            or self._scanning.is_set()
        ):
            # ⚠ 上面的條件刻意**不**改成 _any_long_op_running()：它額外含有
            # ctrl.measuring_active / ctrl.scanning_active 這兩個控制器層級
            # 旗標，那是註冊表（只服務 UI 忙碌顯示與停止分派）不該涵蓋的。
            # 這裡只借用 _busy_reasons() 讓訊息說出實際在忙什麼；若擋下來的
            # 是那兩個控制器旗標，_busy_reasons() 會是空的，退回原本的通稱。
            reasons = "／".join(self._busy_reasons()) or "重播／尋光／復歸／量測"
            self._flash_banner(f"已有其他作業（{reasons}）進行中，請稍後再試")
            return

        selected_axes = [ax for ax in AXES if self._org_repeat_axis_vars[ax].get()]
        if not selected_axes:
            self._flash_banner("量測需要至少選擇一個軸")
            return

        offsets = self._collect_org_repeat_offsets()
        if not offsets:
            self._flash_banner("量測需要至少一個有效的測試 offset")
            return

        try:
            trials = int(self._org_repeat_trials_var.get())
            if trials <= 0:
                raise ValueError
        except ValueError:
            self._flash_banner("每組輪數必須是正整數")
            return

        self._org_repeat_start_btn.config(state="disabled")
        self._org_repeat_status_var.set("方向判定中…")

        # 方向預檢會查詢控制器狀態（序列 I/O），依專案既有規則不可在 Tk
        # 主執行緒做（見 _sync_org_mode 同一類寫法），放到背景執行緒查完
        # 再用 root.after 回主執行緒繼續後續流程（可能彈出方向指定視窗）。
        def _precheck():
            directions: Dict[str, str] = {}
            unresolved: List[str] = []
            for ax in selected_axes:
                axis_no = AXIS_NO[ax]
                st, _ = self.ctrl.query_status(axis_no)
                side = self.ctrl.limit_direction(st)
                if side in ("CW", "CCW"):
                    # 離開方向＝目前所壓限位的反方向——原點復歸後座標
                    # 幾乎必然停在某一側限位附近（見 CLAUDE.md〈座標 0
                    # 幾乎就落在限位開關上〉），這是唯一站得住腳的自動
                    # 判定依據。
                    directions[ax] = "CCW" if side == "CW" else "CW"
                else:
                    unresolved.append(ax)
            self.root.after(
                0,
                lambda: self._after_org_repeat_precheck(
                    selected_axes, offsets, trials, directions, unresolved
                ),
            )

        threading.Thread(target=_precheck, daemon=True).start()

    def _after_org_repeat_precheck(
        self, selected_axes, offsets, trials, directions, unresolved
    ):
        if unresolved:
            manual = self._ask_org_repeat_directions(unresolved)
            if manual is None:
                self._org_repeat_start_btn.config(state="normal")
                self._org_repeat_status_var.set("已取消")
                return
            directions.update(manual)
        self._confirm_and_launch_org_repeat(selected_axes, offsets, trials, directions)

    def _confirm_and_launch_org_repeat(self, selected_axes, offsets, trials, directions):
        axes_txt = "、".join(selected_axes)
        dir_txt = "、".join(f"{ax}={directions[ax]}" for ax in selected_axes)
        offsets_txt = "、".join(str(o) for o in offsets)
        n_combos = len(selected_axes) * len(offsets)

        if not messagebox.askyesno(
            "確認開始量測",
            f"量測軸：{axes_txt}\n"
            f"方向（自動判定或手動指定）：{dir_txt}\n"
            f"測試 Offset：{offsets_txt}\n"
            f"每組輪數：{trials}\n"
            f"組合總數：{n_combos}\n\n"
            f"預估時間：無法精確預估，視軸數／offset／速度而定，"
            f"可能長達數十分鐘。\n\n"
            f"⚠ 每個軸開始量測前，會先執行一次原點復歸建立基準\n"
            f"　（不假設目前位置就是原點）。\n"
            f"⚠ 量測期間會暫停該軸的韌體軟體限位，結束後自動還原。\n"
            f"⚠ 量測進行中請勿手動點動同一軸，避免與量測動作互相干擾。\n"
            f"⚠ 每個 (軸, offset) 組合只有在成功完成一次原點復歸時才會把\n"
            f"　座標強制寫回 0；若中途失敗、逾時或被緊急停止中斷，滑台\n"
            f"　可能停在非原點的任意位置，此時**不會**自動歸零，畫面會\n"
            f"　明確警示，需重新執行原點復歸後才能繼續其他操作。\n\n"
            f"確定要開始嗎？",
            icon="warning", default="no",
        ):
            self._org_repeat_start_btn.config(state="normal")
            self._org_repeat_status_var.set("已取消")
            return

        l, f_spd, r, s = self._get_spd()

        # 早於執行緒啟動設旗標，避免 _update_stat_ui 的窗口期把按鈕解鎖
        # （比照 _do_start_scan 的既有寫法）。
        self._org_repeat_stop_event.clear()
        self._org_repeat_running.set()
        self._org_repeat_start_btn.config(state="disabled")
        self._org_repeat_stop_btn.config(state="normal")
        self._set_drive_buttons_state("disabled")
        self._org_repeat_status_var.set("量測中…")
        self._org_repeat_start_time = time.time()
        self._org_repeat_progress_var.set("—")
        for item in self._org_repeat_tree.get_children():
            self._org_repeat_tree.delete(item)
        self._org_repeat_csv_path_var.set("—")
        self._org_repeat_json_path_var.set("—")

        def _progress(axis, offset, trial, total_trials, residual, status):
            self.root.after(
                0,
                lambda: self._on_org_repeat_progress(
                    axis, offset, trial, total_trials, residual, status
                ),
            )

        def _combo_done(axis, offset, result_dict):
            self.root.after(0, lambda: self._on_org_repeat_combo_done(result_dict))

        def _run():
            try:
                result = self.ctrl.measure_homing_repeatability(
                    axes=selected_axes,
                    offsets=offsets,
                    trials=trials,
                    l_speed=l, f_speed=f_spd, rate=r, s_rate=s,
                    directions=directions,
                    progress_cb=_progress,
                    combo_done_cb=_combo_done,
                    stop_event=self._org_repeat_stop_event,
                )
            except Exception as e:  # 背景執行緒的例外不可讓旗標卡在 set
                # ⚠ `except X as e` 的 e 會在區塊結束時被自動 del，
                # root.after(0, ...) 的 lambda 是非同步排程、真正執行時
                # 區塊早已結束——直接在 lambda 裡引用 e 會是 NameError
                # （_do_start_scan 的 _run() 已踩過同一個坑）。先轉成字串
                # 存進區域變數，讓 lambda 捕捉的是它而非 e。
                logger.exception("原點復歸重現性量測執行緒發生未預期例外")
                err_msg = f"未預期例外: {e}"
                self.root.after(0, lambda: self._on_org_repeat_done(None, err_msg))
                return
            self.root.after(0, lambda: self._on_org_repeat_done(result, None))

        threading.Thread(target=_run, daemon=True).start()

    def _on_org_repeat_progress(self, axis, offset, trial, total_trials, residual, status):
        r_txt = "—" if residual is None else f"{residual:.1f} pulse"
        self._org_repeat_progress_var.set(
            f"{axis} 軸 · offset={offset} · 第 {trial}/{total_trials} 輪 · "
            f"殘差 {r_txt} · {status}"
        )

    def _on_org_repeat_combo_done(self, result_dict: dict):
        """每完成一個 (軸, offset) 組合就附加一列，不等全部跑完才顯示。"""
        stats = result_dict.get("stats") or {}

        def _fmt(v):
            return "—" if v is None else f"{v:.2f}"

        drift = stats.get("drift_detected")
        if drift is None:
            drift_txt = "—"
        elif drift:
            drift_txt = f"是（{_fmt(stats.get('drift_rate'))}/輪）"
        else:
            drift_txt = "否"

        # offset 未脫離出發側限位開關作用區：資料有效但跟其他 offset
        # 不可直接比較（M1 的免費副產品 left_switch 彙整而成），用
        # CLR_WARN 標色而非 CLR_DANGER——那個顏色保留給 origin_lost
        # 這種真正失敗、座標系已失準的情境，兩者嚴重度不同。
        row_tags = ("below_switch",) if result_dict.get("offset_below_switch") else ()

        self._org_repeat_tree.insert(
            "", "end",
            values=(
                result_dict.get("axis", ""),
                result_dict.get("offset", ""),
                stats.get("n", 0),
                _fmt(stats.get("range")),
                _fmt(stats.get("sigma")),
                _fmt(stats.get("median")),
                drift_txt,
                result_dict.get("note", "") or "",
            ),
            tags=row_tags,
        )

        # 🔴 座標系已失準是比一般失敗更嚴重的狀態（之後 goto 教點／限位
        # 比對全部會跟著偏移），不能只靜靜躺在結果表格的備註欄裡等使用者
        # 自己發現——跟撞限位同等級的嚴重度，用橫幅＋CLR_DANGER 主動示警
        # （architect 2026-08-21 審查要求）。
        if result_dict.get("origin_lost"):
            ax = result_dict.get("axis", "?")
            self._flash_banner(
                f"🔴 {ax} 軸座標系已失準（原點復歸重現性量測中途中止，"
                f"未強制歸零），請重新執行原點復歸後再操作",
                20000,
                color=CLR_DANGER,
            )

    def _on_org_repeat_done(self, result: Optional[dict], err: Optional[str]):
        self._org_repeat_running.clear()
        self._org_repeat_stop_btn.config(state="disabled")
        self._org_repeat_start_btn.config(state="normal")
        if self.ctrl.connected and not self.ctrl.ems_active:
            self._set_drive_buttons_state("normal")

        if err is not None:
            self._org_repeat_status_var.set("發生例外")
            self._flash_banner(f"⚠ 原點復歸重現性量測發生例外：{err}", 12000)
            return
        if result is None:
            self._org_repeat_status_var.set("已中止")
            return

        aborted = bool(result.get("aborted"))
        try:
            csv_path, json_path = save_homing_repeat_result(result)
        except OSError as e:
            self._org_repeat_status_var.set("完成，但存檔失敗")
            self._flash_banner(f"⚠ 量測完成，但存檔失敗：{e}", 12000)
            self.ctrl._log("ERROR", f"[復歸重現性量測] 存檔失敗: {e}")
            return

        self._org_repeat_csv_path_var.set(csv_path)
        self._org_repeat_json_path_var.set(json_path)
        if aborted:
            self._org_repeat_status_var.set("已中止，部分資料已存檔")
            self._flash_banner("⚠ 原點復歸重現性量測已中止，部分資料已存檔", 8000)
        else:
            self._org_repeat_status_var.set("完成，已存檔")
            self._flash_banner("✔ 原點復歸重現性量測完成，已存檔", 6000)

    def _do_stop_org_repeat(self):
        if self.ctrl.connected:
            self.ctrl.stop()
        self._org_repeat_stop_event.set()
        self._org_repeat_status_var.set("停止中…")
        self._org_repeat_stop_btn.config(state="disabled")

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
        tk.Label(
            data_card,
            text="含光功率(dBm)欄位——需先在「光功率」分頁連線並開啟自動輪詢，否則該欄留空",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=12, pady=(0, 8))

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
        # 調軸時常需要同時盯著光功率——開一個不搶焦點的浮動視窗，
        # 不必來回切到「光功率」分頁。與該分頁的核取方塊共用同一個 BooleanVar。
        ttk.Checkbutton(
            info_row, text="📊 浮動視窗",
            variable=self._pm_float_open, command=self._toggle_pm_float_window,
        ).pack(side="left", padx=(16, 0))

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
        ).pack(side="left", padx=(0, 4))
        # 附加估算顯示，比照儀表板／Teaching 分頁的既有用法：沒有校正參數
        # 就是空字串，不佔版面也不誤導。
        tk.Label(
            pos_row,
            textvariable=self._ctrl_pos_um_var,
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 8),
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

        # ── 原點復歸重現性量測，緊接在驅動按鈕／速度設定之後 ──
        # 跟下方「連線後設定一次」那些卡片不同類：這是每次都要主動按
        # 「開始量測」才會動的操作，不是連線後設一次就好的靜態設定，
        # 所以放在分隔線之前。
        self._build_card_origin_repeatability(scr)

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
        self._build_card_axis_calib(scr)

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
        """
        「■ Stop」：送出硬體停止指令，並請求所有長時間背景作業收工。

        ctrl.stop() 只讓馬達停下來，攔不住那些跑在自己的背景執行緒、
        只認旗標的作業（重播、原點復歸重現性量測、尋光）——少了下面那行，
        滑台會停一下、然後被背景執行緒送出的下一組指令帶著繼續走。
        逐項接線的舊寫法漏過三次，現在統一走註冊表（見 _register_long_ops）。
        """
        self.ctrl.stop()
        self._request_stop_long_ops()

    def _on_escape(self, event=None):
        """Escape：停止所有軸，並中止進行中的重播／量測／尋光。"""
        if not self.ctrl.connected:
            return
        self.ctrl.stop()
        self._request_stop_long_ops()
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
        # ⚠ 這裡要的是**機械座標**：set_position() 寫的是控制器的 POS 暫存器，
        # 而畫面上的「Position:」顯示的是工作座標（已扣 offset）。設過工作原點
        # 之後兩者差一個 offset，直接拿顯示值當「由 X 改寫為 Y」會誤導使用者。
        cur_mach = self.ctrl.positions_machine.get(ax)
        cur = f"{cur_mach:,.0f}" if cur_mach is not None else "—"
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
                    # 回傳的 pos 刻意不寫進 _ctrl_pos_var：query_status() 已經
                    # 把它寫進 _positions_pulse 快取，畫面統一由
                    # _redraw_positions() 供應（見該函式的座標同步段落）。
                    status, _pos = self.ctrl.query_status(self.ctrl.axis_no)
                    self.root.after(0, lambda s=status: self._ctrl_status_var.set(s))
                    if status != "Driving":
                        return
                    time.sleep(0.1)
            finally:
                self._poll_busy.clear()

        threading.Thread(target=_check, daemon=True).start()

    def _async_query(self):
        def _q():
            # 同 _poll_status：只更新狀態文字，座標交給 _redraw_positions()。
            status, _pos = self.ctrl.query_status(self.ctrl.axis_no)
            self.root.after(0, lambda: self._ctrl_status_var.set(status))

        threading.Thread(target=_q, daemon=True).start()

    def _update_ctrl_pos_um(self, *_args):
        """
        移動控制分頁「Position:」旁的 um 估算附加顯示，純格式化、無 I/O。

        掛在 _ctrl_pos_var 的 write trace 上，行為比照 _redraw_positions()／
        _refresh_points() 既有的 estimate_um() 用法：沒有校正參數或當前軸
        不明時顯示空字串，不猜測、不顯示 0。
        """
        ax = NO_AXIS.get(self.ctrl.axis_no)
        um = None
        if ax:
            try:
                # 來源字串是 _redraw_positions() 格式化過的顯示值，帶千分位
                # 逗號（未連線時是「—」）。先去掉逗號再解析，解析不出來就
                # 當成「沒有可估算的數值」，不猜測。
                raw = self._ctrl_pos_var.get().replace(",", "")
                um = self.ctrl.estimate_um(ax, float(raw))
            except ValueError:
                um = None
        self._ctrl_pos_um_var.set(f"≈ {um:,.1f} μm" if um is not None else "")

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
                txt = f"{pp[ax]:.0f}"
                um = self.ctrl.estimate_um(ax, pp[ax])
                if um is not None:
                    txt += f" ≈ {um:,.1f} μm"
                return txt

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
    # TAB：光功率（HP 8153A / GPIB，監看面板——不整合尋光演算法）
    # =========================================================================
    def _build_tab_power(self, parent):
        self._add_status_bar(parent)
        scr = self._scrollable(parent)

        # ── 卡片一：連線設定 ──
        conn_card = self._card(scr, "連線設定")
        conn_f = tk.Frame(conn_card, bg=CLR_CARD)
        conn_f.pack(fill="x", padx=12, pady=8)

        tk.Label(
            conn_f, text="GPIB 位址", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).grid(row=0, column=0, sticky="w", pady=3)
        self._pm_addr_entry = ttk.Entry(
            conn_f, textvariable=self._pm_gpib_addr_var, width=8
        )
        self._pm_addr_entry.grid(row=0, column=1, padx=(6, 20), sticky="w")

        tk.Label(
            conn_f, text="Channel", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).grid(row=0, column=2, sticky="w", pady=3)
        self._pm_ch_cb = ttk.Combobox(
            conn_f,
            textvariable=self._pm_channel_var,
            values=["1", "2"],
            width=6,
            state="readonly",
        )
        self._pm_ch_cb.grid(row=0, column=3, padx=(6, 20), sticky="w")

        tk.Label(
            conn_f, text="波長 (nm)", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).grid(row=0, column=4, sticky="w", pady=3)
        self._pm_wl_cb = ttk.Combobox(
            conn_f,
            textvariable=self._pm_wavelength_var,
            values=["1310", "1550"],
            width=8,
        )
        self._pm_wl_cb.grid(row=0, column=5, padx=(6, 20), sticky="w")

        # 連線狀態指示燈（獨立於卡片二那顆——這顆只反映連線是否建立）
        pm_conn_f = tk.Frame(conn_f, bg=CLR_CARD)
        pm_conn_f.grid(row=0, column=6, padx=(0, 12))
        self._pm_conn_dot = tk.Canvas(
            pm_conn_f, width=10, height=10, bg=CLR_CARD, highlightthickness=0
        )
        self._pm_conn_dot.pack(side="left", padx=(0, 4))
        self._pm_conn_dot_id = self._pm_conn_dot.create_oval(
            1, 1, 9, 9, fill=CLR_DANGER, outline=""
        )
        self._pm_conn_lbl = tk.Label(
            pm_conn_f, text="未連線", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 10)
        )
        self._pm_conn_lbl.pack(side="left")

        # 手動 bg 三態切換按鈕——比照 main_ai.py 頂部 _conn_btn 既有寫法，
        # 這是全程式唯一按鈕動態變色的先例，新面板延用同一手法而非另創。
        self._pm_conn_btn = tk.Button(
            conn_f,
            text="連線",
            bg=CLR_ACCENT,
            fg="white",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
            command=self._toggle_meter_connect,
        )
        self._pm_conn_btn.grid(row=0, column=7)

        # ── 卡片二：光功率讀值 ──
        pow_card = self._card(scr, "光功率讀值")
        top_row = tk.Frame(pow_card, bg=CLR_CARD)
        top_row.pack(fill="x", padx=12, pady=(6, 0))

        status_f = tk.Frame(top_row, bg=CLR_CARD)
        status_f.pack(side="left")
        self._pm_status_dot = tk.Canvas(
            status_f, width=10, height=10, bg=CLR_CARD, highlightthickness=0
        )
        self._pm_status_dot.pack(side="left", padx=(0, 4))
        self._pm_status_dot_id = self._pm_status_dot.create_oval(
            1, 1, 9, 9, fill=CLR_MUTED, outline=""
        )
        self._pm_status_lbl = tk.Label(
            status_f,
            textvariable=self._pm_status_var,
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 10, "bold"),
        )
        self._pm_status_lbl.pack(side="left")

        self._pm_ch_wl_var = tk.StringVar(value="")
        tk.Label(
            top_row,
            textvariable=self._pm_ch_wl_var,
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=16)

        age_f = tk.Frame(top_row, bg=CLR_CARD)
        age_f.pack(side="right")
        tk.Label(
            age_f, text="最後更新：", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left")
        tk.Label(
            age_f,
            textvariable=self._pm_age_var,
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left")

        # 尋光執行中的狀態提示列——狀態驅動（跟著 ctrl.scanning_active），
        # 不是 _flash_banner 那種計時後自動消失的提示。預設不 pack，顯示/
        # 隱藏交給 _pm_sync_scan_notice()（2026-08-20 起掛在 _start_poller
        # 既有的 100ms 節奏上，見該方法與 main_ai.py 架構說明的
        # 〈scanning_active 與 scan_move_step〉一節）。
        self._pm_scan_notice = tk.Label(
            pow_card,
            text="🔍 尋光進行中 — 讀值由「尋光」分頁提供，本頁自動輪詢已暫停",
            bg=CLR_WARN,
            fg="white",
            font=("Segoe UI", 9, "bold"),
            anchor="w",
            padx=10,
            pady=4,
        )

        num_row = tk.Frame(pow_card, bg=CLR_CARD)
        self._pm_num_row = num_row
        num_row.pack(pady=(4, 4))
        self._pm_power_lbl = tk.Label(
            num_row,
            textvariable=self._pm_power_var,
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Consolas", 56, "bold"),
        )
        self._pm_power_lbl.pack(side="left")
        tk.Label(
            num_row,
            textvariable=self._pm_unit_var,
            bg=CLR_CARD,
            fg=CLR_TEXT,
            font=("Segoe UI", 16),
        ).pack(side="left", padx=(6, 0), anchor="s", pady=(0, 12))

        query_row = tk.Frame(pow_card, bg=CLR_CARD)
        query_row.pack(fill="x", padx=12, pady=(0, 10))
        self._pm_query_btn = ttk.Button(
            query_row,
            text="立即查詢",
            style="Info.TButton",
            state="disabled",
            command=self._query_power_once,
        )
        self._pm_query_btn.pack(side="right")
        ttk.Checkbutton(
            query_row, text="📊 浮動視窗",
            variable=self._pm_float_open, command=self._toggle_pm_float_window,
        ).pack(side="right", padx=(0, 8))

        # ── 卡片三：自動更新與量程 ──
        auto_card = self._card(scr, "自動更新與量程")
        auto_row = tk.Frame(auto_card, bg=CLR_CARD)
        auto_row.pack(fill="x", padx=12, pady=8)
        self._pm_auto_poll_cb = ttk.Checkbutton(
            auto_row,
            text="自動輪詢",
            variable=self._pm_auto_poll,
            state="disabled",
            command=self._pm_sync_poll_interval_state,
        )
        self._pm_auto_poll_cb.pack(side="left")
        tk.Label(
            auto_row, text="間隔 (秒)", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left", padx=(16, 4))
        self._pm_interval_entry = ttk.Entry(
            auto_row, textvariable=self._pm_poll_interval, width=6, state="disabled"
        )
        self._pm_interval_entry.pack(side="left")

        sep = tk.Frame(auto_card, bg=CLR_BORDER, height=1)
        sep.pack(fill="x", padx=12, pady=(4, 8))

        range_row = tk.Frame(auto_card, bg=CLR_CARD)
        range_row.pack(fill="x", padx=12, pady=(0, 10))
        tk.Label(
            range_row, text="量程:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left")
        self._pm_range_auto_rb = tk.Radiobutton(
            range_row,
            text="自動",
            variable=self._pm_range_mode_var,
            value="auto",
            bg=CLR_CARD,
            fg=CLR_TEXT,
            font=("Segoe UI", 9),
            activebackground=CLR_CARD,
            state="disabled",
            command=self._pm_sync_range_entry_state,
        )
        self._pm_range_auto_rb.pack(side="left", padx=(8, 4))
        self._pm_range_manual_rb = tk.Radiobutton(
            range_row,
            text="手動",
            variable=self._pm_range_mode_var,
            value="manual",
            bg=CLR_CARD,
            fg=CLR_TEXT,
            font=("Segoe UI", 9),
            activebackground=CLR_CARD,
            state="disabled",
            command=self._pm_sync_range_entry_state,
        )
        self._pm_range_manual_rb.pack(side="left", padx=4)
        self._pm_range_manual_entry = ttk.Entry(
            range_row, textvariable=self._pm_range_manual_var, width=8, state="disabled"
        )
        self._pm_range_manual_entry.pack(side="left", padx=(4, 4))
        tk.Label(
            range_row, text="dBm", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left", padx=(0, 12))
        self._pm_apply_range_btn = ttk.Button(
            range_row,
            text="套用量程",
            style="Accent.TButton",
            state="disabled",
            command=self._apply_meter_range,
        )
        self._pm_apply_range_btn.pack(side="left")

    # =========================================================================
    # TAB：尋光（FiberAlignmentScanner）。第一階段骨架＋第三階段的嵌入式
    # matplotlib 即時軌跡圖／收斂圖；三層設定卡片與完整確認文案留給第四階段。
    # =========================================================================
    def _on_scan_stage2_toggle(self):
        """
        階段二勾選狀態連動「進階設定」裡的局部半徑／軸縮放係數 Entry 可否編輯。

        改走 `_refresh_scan_entry_states()`，不再自己整批 `.config(state=...)`
        ——那樣會覆寫掉「軸未勾選」這個獨立條件算出來的 disabled 狀態
        （AND 合成邏輯必須集中在單一函式，兩個 handler 各自局部覆寫會
        重演 `_update_stat_ui` 曾經不認得「復歸中」被覆寫的那類 bug）。
        """
        self._refresh_scan_entry_states()

    def _refresh_scan_entry_states(self):
        """
        依「軸是否勾選」×「階段二總開關」兩個獨立條件的 AND，合成起始
        步長／階段二 Entry 的可編輯狀態。任何一個條件改變（軸勾選框、
        階段二總開關）都呼叫這個函式重新算一次，不要各自局部
        `.config(state=...)`——那樣一個 handler 的結果會被另一個蓋掉。

        matplotlib 未安裝時 `_build_tab_scan` 提早 return，這些 widget
        字典根本不存在，這裡直接跳過（呼叫端可能是 `_on_connect_result`
        這類不知道尋光分頁有沒有建成的通用流程）。
        """
        if not hasattr(self, "_scan_axis_selected_vars"):
            return
        stage2_on = self._scan_stage2_var.get()
        is_powell = self._scan_algo_is_powell()
        for ax in AXES:
            axis_on = self._scan_axis_selected_vars[ax].get()
            # Powell 不吃「起始步長」（它自己決定初始方向集合的量級），
            # 選 Powell 時整排灰階，避免使用者以為調這個欄位有效果。
            step_state = "normal" if (axis_on and not is_powell) else "disabled"
            for w in self._scan_axis_step_entries.get(ax, []):
                w.config(state=step_state)
            # Powell 已涵蓋原本階段一＋二的範圍，不論階段二勾選框本身
            # 是什麼狀態，這排 Entry 一律鎖住（勾選框本身的鎖定在下面）。
            stage2_state = "normal" if (axis_on and stage2_on and not is_powell) else "disabled"
            for w in self._scan_axis_stage2_entries.get(ax, []):
                w.config(state=stage2_state)
        if hasattr(self, "_scan_stage2_cb"):
            self._scan_stage2_cb.config(state="disabled" if is_powell else "normal")
        # 盲搜平面的可選配對完全由「這次勾了哪些軸」決定，跟階段二無關，
        # 但觸發時機一模一樣（軸勾選變動），所以掛在同一個重算入口，
        # 不另外綁一組 command——那正是本函式 docstring 警告的「兩個
        # handler 各自改狀態、互相覆蓋」的來源。
        self._refresh_blind_plane_options()

    def _scan_algo_is_powell(self) -> bool:
        """目前演算法下拉是否選到 Powell。UI 建立過程中可能被提早呼叫
        （_scan_algo_var 尚未建立），一律先判斷 hasattr 再讀，不誤判成 False
        以外的例外狀況（False 是安全的預設：一律當成座標下降處理）。"""
        if not hasattr(self, "_scan_algo_var"):
            return False
        return self._scan_algo_label_algos.get(self._scan_algo_var.get()) == "powell"

    def _on_scan_algo_changed(self):
        """
        演算法下拉切換：起始步長／階段二相關 Entry 的可編輯狀態改走
        `_refresh_scan_entry_states()`（不在這裡另外局部 `.config`，理由
        跟該函式 docstring 一樣），這裡只處理它不管的兩件事——警示文字
        與 Powell 專屬進階參數子區塊的顯示/隱藏。
        """
        self._refresh_scan_entry_states()
        is_powell = self._scan_algo_is_powell()
        if is_powell:
            # 🔴 用預設的 pack() 重新顯示會被接到 card1 最尾端（此時
            # self._scan_stage2_cb 已經 pack 過），警示文字會跑到勾選框
            # 下面而非原本設計的位置。用 before= 釘回它原本建立時的位置
            # （階段二 checkbox 之前）。
            self._scan_algo_warn_lbl.pack(
                before=self._scan_stage2_cb, anchor="w", padx=12, pady=(0, 8)
            )
        else:
            self._scan_algo_warn_lbl.pack_forget()
        if hasattr(self, "_scan_powell_params_frame"):
            if is_powell:
                self._scan_powell_params_frame.pack(fill="x", padx=12, pady=(4, 8))
            else:
                self._scan_powell_params_frame.pack_forget()

    def _refresh_blind_plane_options(self):
        """
        依目前勾選的搜尋軸，重算盲搜「掃描平面」下拉的可選配對。

        沿用 `_scan_pair_combo` 已建立的既有慣例：**存字串（"X-Y"）而非
        index**，清單長度隨勾選軸變動，存 index 一定會錯位。使用者原本
        選的配對若仍在新清單裡就保留，否則退回空字串（＝交給演算法自動
        取前兩軸），不要靜默改選成另一組軸——那會讓滑台往使用者沒預期
        的方向掃。
        """
        if not hasattr(self, "_scan_blind_plane_combo"):
            return
        selected = [ax for ax in AXES if self._scan_axis_selected_vars[ax].get()]
        options = [
            f"{a}-{b}"
            for i, a in enumerate(selected)
            for b in selected[i + 1:]
        ]
        self._scan_blind_plane_combo["values"] = [""] + options
        if self._scan_blind_plane_var.get() not in options:
            self._scan_blind_plane_var.set("")

    def _update_blind_estimate(self):
        """
        即時估算盲搜的格點數與粗略耗時，顯示在設定欄位下方。

        格點數是 `(2×半徑÷格距 + 1)²`——平方成長，把半徑加倍或格距減半
        都會讓點數變成四倍。使用者很容易在不知情下設出一個要跑數小時的
        組合，而那個後果要等滑台真的開始掃才會顯現。每點耗時用
        `settle_sec + 一次 GPIB 讀值 + 一次移動` 的保守估計值，只是量級
        參考，不是準確預測。
        """
        if not hasattr(self, "_scan_blind_estimate_var"):
            return
        try:
            step = int(self._scan_blind_step_var.get())
            radius = int(self._scan_blind_radius_var.get())
        except (ValueError, AttributeError):
            self._scan_blind_estimate_var.set("⚠ 格距／半徑需為整數")
            return
        if step <= 0 or radius < 0:
            self._scan_blind_estimate_var.set("⚠ 格距需 > 0、半徑需 ≥ 0")
            return
        n_rings = radius // step
        points = (2 * n_rings + 1) ** 2
        # 每點約 0.25s：settle(0.03) + GPIB 讀值(約 0.15) + 一步移動與等待。
        # 刻意高估而非低估——低估會讓使用者以為只要幾分鐘而放著不管。
        secs = points * 0.25
        if secs < 90:
            dur = f"{secs:.0f} 秒"
        elif secs < 5400:
            dur = f"{secs / 60:.0f} 分鐘"
        else:
            dur = f"{secs / 3600:.1f} 小時"
        self._scan_blind_estimate_var.set(f"→ {points:,} 個格點，粗估 {dur}")

    def _set_scan_axis_selection(self, value: bool):
        """
        「全選」／「全不選」：只操作目前未被 disable 的軸——硬體偵測不到
        的軸維持原狀（`BooleanVar` 早已在 `_on_connect_result` 被強制設
        `False`，這裡再碰它沒有意義，channel 讀取時反正也不會算進去）。
        """
        for ax in AXES:
            cb = self._scan_axis_checkbuttons.get(ax)
            if cb is None or cb.instate(["disabled"]):
                continue
            self._scan_axis_selected_vars[ax].set(value)
        self._refresh_scan_entry_states()

    def _on_scan_abort_toggle(self):
        """取消勾選「無訊號時中止」要顯示警示；沒有對應收工旗標，純粹是文字顯示。"""
        if self._scan_abort_no_signal_var.get():
            self._scan_abort_warn_lbl.pack_forget()
        else:
            self._scan_abort_warn_lbl.pack(anchor="w", padx=12, pady=(0, 8))

    def _toggle_scan_advanced(self):
        self._scan_adv_visible = not self._scan_adv_visible
        if self._scan_adv_visible:
            self._scan_adv_frame.pack(fill="x")
            self._scan_adv_toggle_btn.config(text="▾ 隱藏進階設定")
        else:
            self._scan_adv_frame.pack_forget()
            self._scan_adv_toggle_btn.config(text="▸ 顯示進階設定")

    def _build_tab_scan(self, parent):
        if not _MATPLOTLIB_AVAILABLE:
            tk.Label(
                parent,
                text=f"尋光功能需要 matplotlib，目前未安裝，此分頁不可用。\n"
                     f"（{_MATPLOTLIB_IMPORT_ERROR}）\n"
                     f"請執行：venv\\Scripts\\python.exe -m pip install matplotlib",
                fg=CLR_WARN, bg=CLR_BG, justify="left",
            ).pack(padx=20, pady=20)
            return

        cfg = self._scanner_cfg_pending  # __init__ 已載入的 scanner_config.json 內容，可能是空 dict
        # 固定顯示六軸，不再依連線狀態決定要建立哪些 Entry——使用者現在
        # 可以自行勾選要搜尋的軸（見下方「搜尋軸」子區塊），停用的軸只是
        # 灰階，不是不存在。_scan_active_axes() 已隨這次改動移除。
        scan_axes = AXES

        self._add_status_bar(parent)

        # ── 頂端操作列：開始/停止/狀態/耗時，固定在最上方 ──
        toolbar = tk.Frame(parent, bg=CLR_CARD, highlightbackground=CLR_BORDER, highlightthickness=1)
        toolbar.pack(side="top", fill="x")
        self._scan_start_btn = ttk.Button(
            toolbar, text="▶ 開始尋光", style="Accent.TButton", command=self._do_start_scan
        )
        self._scan_start_btn.pack(side="left", padx=(12, 4), pady=8)
        self._scan_stop_btn = ttk.Button(
            toolbar, text="■ 停止尋光", style="Danger.TButton", command=self._do_stop_scan, state="disabled"
        )
        self._scan_stop_btn.pack(side="left", padx=4, pady=8)
        # 匯出鍵刻意**不**放進 _drive_buttons，也不隨尋光進行中 disable：
        # 它只讀主執行緒的 self._scan_samples、不碰序列埠也不碰 GPIB，
        # 搜尋跑到一半想先撈一份中途資料出來看是完全合理的操作。
        self._scan_export_btn = ttk.Button(
            toolbar, text="⤓ 匯出 Excel", style="Info.TButton",
            command=self._export_scan_xlsx,
        )
        self._scan_export_btn.pack(side="left", padx=4, pady=8)
        if not _XLSXWRITER_AVAILABLE:
            # 跟 matplotlib 缺席時整個分頁停用同一種處理：講清楚為什麼不能按，
            # 而不是讓使用者按下去才看到例外訊息。
            self._scan_export_btn.config(state="disabled", text="⤓ 匯出 Excel（缺 xlsxwriter）")
        tk.Label(
            toolbar, textvariable=self._scan_status_var, bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 11, "bold")
        ).pack(side="left", padx=(16, 6))
        tk.Label(toolbar, text="已耗時", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)).pack(side="left", padx=(16, 2))
        tk.Label(
            toolbar, textvariable=self._scan_elapsed_var, bg=CLR_CARD, fg=CLR_TEXT, font=("Consolas", 10, "bold")
        ).pack(side="left")
        self._scan_toolbar = toolbar  # _show_scan_no_signal_notice 定位用（插在頂端操作列之後）

        # ── 無訊號中止的常駐提示列：狀態驅動、需使用者主動關閉，跟
        # _pm_scan_notice（尋光進行中、跟著 scanning_active 自動收放）不是
        # 同一種東西——這條顯示後留著，直到按「知道了」才收起，避免使用者
        # 沒注意到搜尋已經無訊號中止。預設不 pack，見 _show/_hide_scan_no_signal_notice。
        self._scan_no_signal_notice = tk.Frame(parent, bg=CLR_WARN)
        self._scan_no_signal_notice_label = tk.Label(
            self._scan_no_signal_notice, text="", bg=CLR_WARN, fg="white",
            font=("Segoe UI", 9, "bold"), anchor="w", padx=10, pady=6,
        )
        self._scan_no_signal_notice_label.pack(side="left", fill="x", expand=True)
        ttk.Button(
            self._scan_no_signal_notice, text="知道了", style="Flat.TButton",
            command=self._hide_scan_no_signal_notice,
        ).pack(side="right", padx=10)

        # ── 中間主體：左右分欄 ──
        body = tk.Frame(parent, bg=CLR_BG)
        body.pack(side="top", fill="both", expand=True)

        left_outer = tk.Frame(body, bg=CLR_BG, width=340)
        left_outer.pack(side="left", fill="y")
        left_outer.pack_propagate(False)  # 固定左欄寬度，不被右欄的圖表擠壓變形
        left = self._scrollable(left_outer)

        right = tk.Frame(body, bg=CLR_BG)
        right.pack(side="left", fill="both", expand=True)

        # =================== 左欄卡片一：掃描設定 ===================
        card1 = self._card(left, "掃描設定")

        # ── 搜尋軸：使用者勾選這次尋光要用哪幾軸，不再只是被動跟著硬體
        # 偵測到的軸數走。兩列排列（X/Y/Z、U/V/W），停用的軸（`_on_connect_
        # result` 依 axis_count 判斷）只是灰階、Checkbutton 本身仍在——
        # 固定六軸版面，不隨連線狀態重建。
        axis_sel_f = tk.Frame(card1, bg=CLR_CARD)
        axis_sel_f.pack(fill="x", padx=12, pady=(6, 2))
        axis_sel_head = tk.Frame(axis_sel_f, bg=CLR_CARD)
        axis_sel_head.pack(fill="x")
        tk.Label(
            axis_sel_head, text="搜尋軸", bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 9)
        ).pack(side="left")
        ttk.Button(
            axis_sel_head, text="全不選", style="Flat.TButton",
            command=lambda: self._set_scan_axis_selection(False),
        ).pack(side="right")
        ttk.Button(
            axis_sel_head, text="全選", style="Flat.TButton",
            command=lambda: self._set_scan_axis_selection(True),
        ).pack(side="right", padx=(0, 4))

        self._scan_axis_selected_vars: Dict[str, tk.BooleanVar] = {}
        self._scan_axis_checkbuttons: Dict[str, ttk.Checkbutton] = {}
        # 舊格式（或第一次啟動）沒有這個欄位時六軸皆預設勾選——維持
        # 「今天的行為＝搜尋全部偵測到的軸」這個既有預期，向下相容。
        saved_selected_axes = cfg.get("selected_axes")
        for row_axes in (("X", "Y", "Z"), ("U", "V", "W")):
            row_f = tk.Frame(axis_sel_f, bg=CLR_CARD)
            row_f.pack(fill="x", pady=(4, 0))
            for ax in row_axes:
                initial = True if saved_selected_axes is None else (ax in saved_selected_axes)
                var = tk.BooleanVar(value=initial)
                self._scan_axis_selected_vars[ax] = var
                cb = ttk.Checkbutton(
                    row_f, text=ax, variable=var, command=self._refresh_scan_entry_states,
                )
                cb.pack(side="left", padx=(0, 10))
                self._scan_axis_checkbuttons[ax] = cb

        ttk.Separator(card1, orient="horizontal").pack(fill="x", padx=12, pady=(6, 6))

        self._scan_axis_step_entries: Dict[str, List[ttk.Entry]] = {}
        step_f = tk.Frame(card1, bg=CLR_CARD)
        step_f.pack(fill="x", padx=12, pady=(0, 2))
        tk.Label(
            step_f, text="起始步長（每軸，pulse）", bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 9)
        ).pack(anchor="w")
        axes_row = tk.Frame(step_f, bg=CLR_CARD)
        axes_row.pack(fill="x", pady=(4, 0))
        for ax in scan_axes:
            col = tk.Frame(axes_row, bg=CLR_CARD)
            col.pack(side="left", padx=(0, 8))
            tk.Label(col, text=ax, bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8)).pack(anchor="w")
            # 預設值不可以是 0——那幾乎就是「原地不動」，對座標下降演算法毫無意義。
            var = tk.StringVar(value="64")
            self._scan_axis_step_vars[ax] = var
            ent = ttk.Entry(col, textvariable=var, width=8)
            ent.pack()
            self._scan_axis_step_entries.setdefault(ax, []).append(ent)
        tk.Label(
            card1,
            text="勾選要搜尋的軸；起始步長越大收斂越快但越容易跳過訊號峰值。"
                 "灰階＝控制器目前未偵測到此軸。",
            bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8), justify="left", wraplength=300,
        ).pack(anchor="w", padx=12, pady=(2, 8))

        # =================== 演算法選擇（2026-08-28 新增）===================
        # 與 ORG_MODES 相反、跟 _scan_blind_mode_labels 同一個既有慣例：
        # Combobox 存**顯示標籤字串**，換算回代碼一律查反向 map
        # （self._scan_algo_label_algos），不可用 .index()。
        self._scan_algo_label_algos = {v: k for k, v in ALGO_LABELS.items()}
        _saved_algo = cfg.get("algorithm", "coordinate_descent")
        if _saved_algo not in ALGO_LABELS or (
            _saved_algo == "powell" and not fiber_scanner_advanced._SCIPY_AVAILABLE
        ):
            # 不合法的存檔值，或存的是 powell 但目前環境沒裝 scipy：一律
            # 退回座標下降。不偷偷把存檔值本身改掉——_do_start_scan 存檔
            # 時仍會照使用者「這次實際選了什麼」寫回去，環境裝好 scipy
            # 後應該要能自動恢復先前選過的 powell（見 CLAUDE.md 落地規格）。
            _saved_algo = "coordinate_descent"
        algo_f = tk.Frame(card1, bg=CLR_CARD)
        algo_f.pack(fill="x", padx=12, pady=(0, 4))
        tk.Label(
            algo_f, text="演算法", bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 9)
        ).pack(anchor="w")
        self._scan_algo_var = tk.StringVar(value=ALGO_LABELS[_saved_algo])
        # 未裝 scipy 時 Combobox 的 values 只放座標下降這一項，不讓使用者
        # 選到裝不了的選項（readonly Combobox 仍可能被程式或設定檔塞進
        # 不在 values 裡的字串，所以上面的白名單驗證仍是必要的第二道防線）。
        _algo_values = [ALGO_LABELS["coordinate_descent"]]
        if fiber_scanner_advanced._SCIPY_AVAILABLE:
            _algo_values.append(ALGO_LABELS["powell"])
        self._scan_algo_combo = ttk.Combobox(
            algo_f, textvariable=self._scan_algo_var, state="readonly", width=40,
            values=_algo_values,
        )
        self._scan_algo_combo.pack(anchor="w", pady=(2, 2))
        self._scan_algo_combo.bind("<<ComboboxSelected>>", lambda e: self._on_scan_algo_changed())
        if not fiber_scanner_advanced._SCIPY_AVAILABLE:
            tk.Label(
                algo_f,
                text=f"未安裝 scipy，Powell 選項暫不可用（{fiber_scanner_advanced._SCIPY_IMPORT_ERROR}）",
                bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8), justify="left", wraplength=300,
            ).pack(anchor="w", pady=(0, 4))
        self._scan_algo_warn_lbl = tk.Label(
            card1,
            text="⚠ Powell 共軛方向法目前僅通過假物件測試，尚未真機驗證；"
                 "xtol/ftol/懲罰係數皆為保守起跳值。建議先在低風險行程小範圍試跑並全程留意。",
            bg=CLR_CARD, fg=CLR_WARN, font=("Segoe UI", 8), justify="left", wraplength=300,
        )
        if self._scan_algo_label_algos.get(self._scan_algo_var.get()) == "powell":
            self._scan_algo_warn_lbl.pack(anchor="w", padx=12, pady=(0, 8))

        self._scan_stage2_var = tk.BooleanVar(value=cfg.get("enable_stage2", False))
        self._scan_stage2_cb = ttk.Checkbutton(
            card1, text="啟用階段二局部精修（K 近鄰）",
            variable=self._scan_stage2_var, command=self._on_scan_stage2_toggle,
        )
        self._scan_stage2_cb.pack(anchor="w", padx=12, pady=(0, 10))

        # =================== 左欄卡片二：訊號有效性判準 ===================
        card2 = self._card(left, "訊號有效性判準")
        tk.Label(
            card2, text="選填 · 需真機校準", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8)
        ).pack(anchor="w", padx=12, pady=(0, 6))

        tk.Label(
            card2, text="有效功率下限 (dBm)", bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 9)
        ).pack(anchor="w", padx=12)
        self._scan_min_valid_power_var = tk.StringVar(value=cfg.get("min_valid_power_dbm", ""))
        ttk.Entry(card2, textvariable=self._scan_min_valid_power_var, width=14).pack(
            anchor="w", padx=12, pady=(2, 2)
        )
        tk.Label(
            card2,
            text="0 是有效的功率下限值，不代表停用；要停用請保持空白。此值需以真機「刻意不"
                 "耦光」量出的暗電流基準校準，校準前建議留空。",
            bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8), justify="left", wraplength=300,
        ).pack(anchor="w", padx=12, pady=(0, 8))

        tk.Label(
            card2, text="無訊號判定倍數", bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 9)
        ).pack(anchor="w", padx=12)
        self._scan_range_mult_var = tk.StringVar(
            value=cfg.get("no_signal_range_mult", str(DEFAULT_NO_SIGNAL_RANGE_MULT))
        )
        ttk.Entry(card2, textvariable=self._scan_range_mult_var, width=14).pack(
            anchor="w", padx=12, pady=(2, 8)
        )

        self._scan_abort_no_signal_var = tk.BooleanVar(value=cfg.get("abort_if_no_signal", True))
        ttk.Checkbutton(
            card2, text="無訊號時中止搜尋",
            variable=self._scan_abort_no_signal_var, command=self._on_scan_abort_toggle,
        ).pack(anchor="w", padx=12, pady=(0, 2))
        self._scan_abort_warn_lbl = tk.Label(
            card2, text="⚠ 取消勾選後，偵測不到訊號也會繼續跑完整段搜尋",
            bg=CLR_CARD, fg=CLR_WARN, font=("Segoe UI", 8), justify="left", wraplength=300,
        )
        if not self._scan_abort_no_signal_var.get():
            self._scan_abort_warn_lbl.pack(anchor="w", padx=12, pady=(0, 8))

        # =================== 左欄卡片二之二：階段零盲搜 ===================
        # 2026-08-26 新增。動機見 fiber_scanner.run_stage0_blind()：座標下降
        # 需要梯度，而尋光起點本來就常常完全無光，那時演算法在原地一步都
        # 不會動。這張卡片是唯一能讓使用者控制「掃多大、掃多密」的地方。
        card_blind = self._card(left, "階段零：盲搜粗掃（無訊號時）")

        tk.Label(
            card_blind, text="模式", bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 9)
        ).pack(anchor="w", padx=12)
        # 與 ORG_MODES 同一個既有慣例：Combobox 存的是**字串本身**而不是
        # index，避免清單增刪時整組差一位（CLAUDE.md〈第三批修正〉記載過
        # ORG_MODES.index() 的差一位事故）。
        self._scan_blind_mode_labels = {
            "off": "關閉（維持舊行為）",
            "auto": "自動：階段一判定無訊號才盲搜",
            "always": "一律先盲搜再進階段一",
        }
        self._scan_blind_label_modes = {v: k for k, v in self._scan_blind_mode_labels.items()}
        _saved_mode = cfg.get("blind_mode", GUI_DEFAULT_BLIND_MODE)
        if _saved_mode not in BLIND_MODES:
            _saved_mode = GUI_DEFAULT_BLIND_MODE
        self._scan_blind_mode_var = tk.StringVar(value=self._scan_blind_mode_labels[_saved_mode])
        self._scan_blind_mode_combo = ttk.Combobox(
            card_blind, textvariable=self._scan_blind_mode_var, state="readonly", width=30,
            values=[self._scan_blind_mode_labels[m] for m in BLIND_MODES],
        )
        self._scan_blind_mode_combo.pack(anchor="w", padx=12, pady=(2, 8))

        tk.Label(
            card_blind, text="掃描平面（兩軸）", bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 9)
        ).pack(anchor="w", padx=12)
        self._scan_blind_plane_var = tk.StringVar(value=cfg.get("blind_plane", ""))
        self._scan_blind_plane_combo = ttk.Combobox(
            card_blind, textvariable=self._scan_blind_plane_var, state="readonly", width=10,
        )
        self._scan_blind_plane_combo.pack(anchor="w", padx=12, pady=(2, 2))
        tk.Label(
            card_blind,
            text="盲搜固定掃兩個軸。第三軸用同樣密度掃會讓格點數變成立方，"
                 "以光纖對準需要的格距估算根本跑不完——耦合距離不對時請先"
                 "單獨調整該軸再重掃。留空＝自動取本次搜尋軸的前兩軸。",
            bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8), justify="left", wraplength=300,
        ).pack(anchor="w", padx=12, pady=(0, 8))

        grid_f = tk.Frame(card_blind, bg=CLR_CARD)
        grid_f.pack(fill="x", padx=12, pady=(0, 2))
        tk.Label(
            grid_f, text="格距 (pulse)", bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 9)
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            grid_f, text="最大半徑 (pulse)", bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 9)
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        self._scan_blind_step_var = tk.StringVar(
            value=cfg.get("blind_step", str(DEFAULT_BLIND_STEP))
        )
        self._scan_blind_radius_var = tk.StringVar(
            value=cfg.get("blind_max_radius", str(DEFAULT_BLIND_MAX_RADIUS))
        )
        ttk.Entry(grid_f, textvariable=self._scan_blind_step_var, width=12).grid(
            row=1, column=0, sticky="w", pady=(2, 0)
        )
        ttk.Entry(grid_f, textvariable=self._scan_blind_radius_var, width=12).grid(
            row=1, column=1, sticky="w", padx=(10, 0), pady=(2, 0)
        )

        # 格點數是 (2×半徑÷格距+1)²，成長極快——使用者很容易在不知情的
        # 情況下設出一個要跑好幾小時的組合。這行即時估算是把那個後果
        # 攤在設定當下，而不是等滑台已經開始掃了才發現。
        self._scan_blind_estimate_var = tk.StringVar(value="")
        tk.Label(
            card_blind, textvariable=self._scan_blind_estimate_var,
            bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8), justify="left", wraplength=300,
        ).pack(anchor="w", padx=12, pady=(4, 8))
        self._scan_blind_step_var.trace_add("write", lambda *_: self._update_blind_estimate())
        self._scan_blind_radius_var.trace_add("write", lambda *_: self._update_blind_estimate())
        self._update_blind_estimate()

        # =================== 左欄卡片三：進階設定（可折疊）===================
        card3 = self._card(left, "")
        self._scan_adv_visible = False
        self._scan_adv_toggle_btn = ttk.Button(
            card3, text="▸ 顯示進階設定", style="Flat.TButton", command=self._toggle_scan_advanced,
        )
        self._scan_adv_toggle_btn.pack(anchor="w", padx=12, pady=(6, 0))

        self._scan_adv_frame = tk.Frame(card3, bg=CLR_CARD)
        # 預設收合，不 pack——_toggle_scan_advanced 負責顯示/隱藏。

        # 速度四參數：沿用「移動控制」分頁速度設定卡（_build_card_speed）的
        # 既有標籤命名，不要另外發明一套詞彙。
        spd_f = tk.Frame(self._scan_adv_frame, bg=CLR_CARD)
        spd_f.pack(fill="x", padx=12, pady=(8, 4))
        self._scan_l_speed_var = tk.StringVar(value=cfg.get("l_speed", "50"))
        self._scan_f_speed_var = tk.StringVar(value=cfg.get("f_speed", "10000"))
        self._scan_rate_var = tk.StringVar(value=cfg.get("rate", "1000"))
        self._scan_s_rate_var = tk.StringVar(value=cfg.get("s_rate", "50"))
        for r, (lbl, var) in enumerate(
            [
                ("Start-up Speed (L)", self._scan_l_speed_var),
                ("Driving Speed (F)", self._scan_f_speed_var),
                ("Accel/Decel Rate (R)", self._scan_rate_var),
                ("S-curve Rate (S)", self._scan_s_rate_var),
            ]
        ):
            tk.Label(
                spd_f, text=lbl, bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9), width=20, anchor="w"
            ).grid(row=r, column=0, sticky="w", pady=3)
            ttk.Entry(spd_f, textvariable=var, width=10).grid(row=r, column=1, padx=8)

        grid2 = tk.Frame(self._scan_adv_frame, bg=CLR_CARD)
        grid2.pack(fill="x", padx=12, pady=(0, 4))
        self._scan_step_min_var = tk.StringVar(value=cfg.get("step_min", str(DEFAULT_STEP_MIN)))
        self._scan_settle_sec_var = tk.StringVar(value=cfg.get("settle_sec", str(DEFAULT_SETTLE_SEC)))
        self._scan_max_cycles_var = tk.StringVar(value=cfg.get("max_cycles", str(DEFAULT_MAX_CYCLES)))
        self._scan_noise_sigma_mult_var = tk.StringVar(
            value=cfg.get("noise_sigma_mult", str(DEFAULT_NOISE_SIGMA_MULT))
        )
        # 盲搜的「找到訊號」門檻＝max(σ倍數×σ, 絕對下限dB)，兩者取大。
        # 底噪很穩定時 σ→0，只靠倍數會退化成「比基準大一點點就算找到」，
        # 一個雜訊尖峰就能讓盲搜停在沒有光的地方並回報成功。
        self._scan_blind_sigma_mult_var = tk.StringVar(
            value=cfg.get("blind_signal_sigma_mult", str(DEFAULT_BLIND_SIGNAL_SIGMA_MULT))
        )
        self._scan_blind_min_delta_var = tk.StringVar(
            value=cfg.get("blind_signal_min_delta_db", str(DEFAULT_BLIND_SIGNAL_MIN_DELTA_DB))
        )
        for r, (lbl, var) in enumerate(
            [
                ("最小步長 step_min", self._scan_step_min_var),
                ("震動衰減 settle_sec", self._scan_settle_sec_var),
                ("最多輪數 max_cycles", self._scan_max_cycles_var),
                ("雜訊倍數 noise_sigma_mult", self._scan_noise_sigma_mult_var),
                ("盲搜門檻 σ 倍數", self._scan_blind_sigma_mult_var),
                ("盲搜門檻下限 (dB)", self._scan_blind_min_delta_var),
            ]
        ):
            tk.Label(
                grid2, text=lbl, bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9), width=20, anchor="w"
            ).grid(row=r, column=0, sticky="w", pady=3)
            ttk.Entry(grid2, textvariable=var, width=10).grid(row=r, column=1, padx=8)

        f_min_f = tk.Frame(self._scan_adv_frame, bg=CLR_CARD)
        f_min_f.pack(fill="x", padx=12, pady=(4, 4))
        tk.Label(
            f_min_f, text="最低速度 f_speed_min", bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 9)
        ).pack(anchor="w")
        self._scan_f_speed_min_var = tk.StringVar(value=cfg.get("f_speed_min", ""))
        ttk.Entry(f_min_f, textvariable=self._scan_f_speed_min_var, width=14).pack(anchor="w", pady=(2, 0))
        tk.Label(
            f_min_f, text="留空 = 自動（最高速的 1/5）", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8)
        ).pack(anchor="w", pady=(1, 0))

        # 調速下限/上限刻意不存進 scanner_config.json（見 _do_start_scan 存檔
        # 那段的欄位清單），每次開分頁都是空白、交給函式庫自動決定。
        scale_f = tk.Frame(self._scan_adv_frame, bg=CLR_CARD)
        scale_f.pack(fill="x", padx=12, pady=(4, 4))
        tk.Label(
            scale_f, text="調速下限／上限 (pulse)", bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 9)
        ).pack(anchor="w")
        scale_row = tk.Frame(scale_f, bg=CLR_CARD)
        scale_row.pack(fill="x", pady=(2, 0))
        self._scan_speed_scale_lo_var = tk.StringVar(value="")
        self._scan_speed_scale_hi_var = tk.StringVar(value="")
        ttk.Entry(scale_row, textvariable=self._scan_speed_scale_lo_var, width=9).pack(side="left")
        tk.Label(scale_row, text="～", bg=CLR_CARD, fg=CLR_MUTED).pack(side="left", padx=4)
        ttk.Entry(scale_row, textvariable=self._scan_speed_scale_hi_var, width=9).pack(side="left")
        tk.Label(
            scale_f, text="留空 = 自動", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8)
        ).pack(anchor="w", pady=(1, 0))

        # 階段二專用：局部取樣半徑／軸縮放係數，只在啟用階段二時可編輯。
        # 同樣不存檔——依當次搜尋範圍而定，存檔只會誘使使用者延用不適合的舊值。
        tk.Label(
            self._scan_adv_frame, text="階段二專用（僅啟用階段二局部精修時生效）",
            bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", padx=12, pady=(6, 2))

        self._scan_stage2_radius_vars: Dict[str, tk.StringVar] = {}
        self._scan_stage2_axis_scale_vars: Dict[str, tk.StringVar] = {}
        # 按軸分桶（取代舊的扁平 list）——_refresh_scan_entry_states 要能
        # 針對單一軸把「軸勾選」×「階段二總開關」AND 起來單獨判斷可編輯性，
        # 扁平 list 做不到這件事。初始狀態這裡先算一次正確值（避免建立瞬間
        # 閃一下錯的狀態），_build_tab_scan 結尾仍會呼叫
        # _refresh_scan_entry_states() 做最終校正。
        self._scan_axis_stage2_entries: Dict[str, List[ttk.Entry]] = {}
        stage2_on = self._scan_stage2_var.get()

        radius_f = tk.Frame(self._scan_adv_frame, bg=CLR_CARD)
        radius_f.pack(fill="x", padx=12, pady=(0, 2))
        tk.Label(
            radius_f, text="局部取樣半徑（每軸，pulse）", bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 9)
        ).pack(anchor="w")
        radius_row = tk.Frame(radius_f, bg=CLR_CARD)
        radius_row.pack(fill="x", pady=(2, 6))
        for ax in scan_axes:
            col = tk.Frame(radius_row, bg=CLR_CARD)
            col.pack(side="left", padx=(0, 8))
            tk.Label(col, text=ax, bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8)).pack(anchor="w")
            var = tk.StringVar(value="")
            self._scan_stage2_radius_vars[ax] = var
            axis_on = self._scan_axis_selected_vars[ax].get()
            ent = ttk.Entry(
                col, textvariable=var, width=8,
                state="normal" if (axis_on and stage2_on) else "disabled",
            )
            ent.pack()
            self._scan_axis_stage2_entries.setdefault(ax, []).append(ent)

        scale_f2 = tk.Frame(self._scan_adv_frame, bg=CLR_CARD)
        scale_f2.pack(fill="x", padx=12, pady=(0, 2))
        tk.Label(
            scale_f2, text="軸縮放係數 axis_scale（每軸）", bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 9)
        ).pack(anchor="w")
        scale_row2 = tk.Frame(scale_f2, bg=CLR_CARD)
        scale_row2.pack(fill="x", pady=(2, 6))
        for ax in scan_axes:
            col = tk.Frame(scale_row2, bg=CLR_CARD)
            col.pack(side="left", padx=(0, 8))
            tk.Label(col, text=ax, bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8)).pack(anchor="w")
            var = tk.StringVar(value="")
            self._scan_stage2_axis_scale_vars[ax] = var
            axis_on = self._scan_axis_selected_vars[ax].get()
            ent = ttk.Entry(
                col, textvariable=var, width=8,
                state="normal" if (axis_on and stage2_on) else "disabled",
            )
            ent.pack()
            self._scan_axis_stage2_entries.setdefault(ax, []).append(ent)

        tk.Label(
            self._scan_adv_frame,
            text="以上皆為函式庫內建的保守預設值，尚未以真機校準；調整前建議先以預設值跑過至少一次完整搜尋。",
            bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8), justify="left", wraplength=300,
        ).pack(anchor="w", padx=12, pady=(4, 8))

        # Powell 專屬進階參數：只開放 max_iterations（maxfev）。xtol_pulse／
        # ftol_sigma_mult／penalty_lambda 刻意不開放輸入框——architect 明確
        # 結論：這三個值錯了會靜默失效（搜尋提早停在錯的地方或跑到
        # maxfev 才停），且操作員目前沒有回饋依據能校準它們，開放輸入
        # 只會製造「調錯了也不知道」的風險。這個子區塊只在選 Powell 時
        # 顯示，由 _on_scan_algo_changed() 控制 pack/pack_forget。
        self._scan_powell_params_frame = tk.Frame(self._scan_adv_frame, bg=CLR_CARD)
        tk.Label(
            self._scan_powell_params_frame, text="Powell 專用",
            bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", pady=(0, 2))
        iter_row = tk.Frame(self._scan_powell_params_frame, bg=CLR_CARD)
        iter_row.pack(fill="x")
        tk.Label(
            iter_row, text="最多函式評估次數 max_iterations", bg=CLR_CARD, fg=CLR_TEXT,
            font=("Segoe UI", 9), width=26, anchor="w",
        ).pack(side="left")
        self._scan_powell_max_iter_var = tk.StringVar(
            value=cfg.get("powell_max_iterations", "200")
        )
        ttk.Entry(iter_row, textvariable=self._scan_powell_max_iter_var, width=10).pack(
            side="left", padx=8
        )
        tk.Label(
            self._scan_powell_params_frame,
            text="xtol／ftol／懲罰係數皆為函式庫內建起跳值，暫不開放調整（見上方警示）。",
            bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8), justify="left", wraplength=300,
        ).pack(anchor="w", pady=(2, 0))
        if self._scan_algo_is_powell():
            self._scan_powell_params_frame.pack(fill="x", padx=12, pady=(4, 8))
        # is_powell 若為 False 就不 pack——維持與其餘按需顯示的子區塊一致
        # 的「預設收合」慣例。

        # =================== 右欄：即時圖表（第三階段既有邏輯，不動）===================
        self._build_scan_plot(right)

        # 這個分頁建立過程分散在多處設定初始 Entry state（軸勾選、階段二
        # 各自算過一次），這裡收尾統一重算一次最終狀態，確保兩個條件的
        # AND 合成結果正確，不受建立順序影響。
        self._refresh_scan_entry_states()

        # 資料寫入（sample_cb，背景執行緒）與重繪（此迴圈，主執行緒）分離，
        # 見 self._on_scan_sample / self._redraw_scan_plot 的說明。跟
        # _start_poller 同一種「自我重新排程」模式，開分頁時啟動一次即可
        # 持續跑到程式結束，不必等尋光開始/結束再啟動/停止。
        self.root.after(SCAN_PLOT_REDRAW_INTERVAL, self._redraw_scan_plot)

    def _build_scan_plot(self, parent):
        """
        建立尋光分頁的嵌入式即時圖：上排「配對投影／座標變化趨勢」並排、
        下排功率收斂全寬，圖表下方一列數值摘要（目前功率／最佳功率／
        樣本數／目前座標）。

        用單一 Figure + gridspec（而非三張獨立 Figure）：三張子圖共用一次
        draw_idle()，重繪成本比三個獨立 canvas 各自 draw 低。

        2026-08-19「尋光彈性選軸」（1~6 軸）之前，左上／右上固定畫 XY／XZ
        投影，選了 X/Y/Z 以外的軸組合時兩張子圖都會半殘。改成：左上是
        可切換軸對的 2D 投影（`_scan_ax_xy`，名字沿用但語意變成「配對
        投影」，可切換到任意兩軸組合）、右上是多軸 1D 相對位移趨勢線
        （`_scan_ax_xz`，語意變成「趨勢線」，天生支援任意 1~6 軸、不需要
        配對）。gridspec 骨架不變。
        """
        chart_card = self._card(parent, "即時軌跡與收斂")

        # ── 「投影軸對」控制列：搜尋軸數決定顯示模式，見 _update_scan_pair_
        # controls。三個元件建立時就都建好，之後只切換 pack/pack_forget，
        # 不把整條列 pack_forget——避免像橫幅那樣造成版面跳動（見 CLAUDE.md
        # 〈第四批修正〉）。內容物切換不影響這條列本身的存在。
        pair_ctrl = tk.Frame(chart_card, bg=CLR_CARD)
        pair_ctrl.pack(fill="x", padx=12, pady=(2, 0))
        self._scan_pair_label = tk.Label(
            pair_ctrl, text="投影軸對", bg=CLR_CARD, fg=CLR_TEXT, font=("Segoe UI", 8)
        )
        self._scan_pair_combo = ttk.Combobox(pair_ctrl, state="readonly", width=6)
        self._scan_pair_combo.bind("<<ComboboxSelected>>", self._on_scan_pair_change)
        self._scan_pair_static_label = tk.Label(
            pair_ctrl, text="", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8)
        )
        # 目前 Combobox／靜態文字所代表的候選配對清單（"X-Y" 這種字串），
        # 由 _update_scan_pair_controls 依這次搜尋軸重建；空清單＝只選了
        # 1 軸、沒有配對可言。存字串而非 index，理由跟 ORG_MODES 一樣：
        # 避免「清單起始位置不同、index 換算差一位」這類錯誤。
        self._scan_pair_options: List[str] = []
        # 這次搜尋涵蓋哪些軸（_scan_plot_reset 當下算出的 selected_axes，
        # 依 AXES 固定順序排序）。scanner.active_axes 落地前的過渡態
        # （active is None）用這份頂替，見 _scan_redraw_figure。
        self._scan_last_selected_axes: List[str] = []

        self._scan_fig = Figure(figsize=(8, 5.5), dpi=100, facecolor=CLR_CARD)
        gs = self._scan_fig.add_gridspec(2, 2, height_ratios=[1, 1.1], hspace=0.45, wspace=0.28)
        self._scan_ax_xy = self._scan_fig.add_subplot(gs[0, 0])   # 配對投影（軸對可切換）
        self._scan_ax_xz = self._scan_fig.add_subplot(gs[0, 1])   # 多軸相對位移趨勢線
        self._scan_ax_pwr = self._scan_fig.add_subplot(gs[1, :])

        for ax, title, xlabel, ylabel in (
            (self._scan_ax_xy, "投影", "", ""),
            (self._scan_ax_xz, "座標變化（相對起點）", "樣本編號", "Δ位置 (pulse)"),
            (self._scan_ax_pwr, "功率收斂", "樣本編號", "功率 (dBm)"),
        ):
            ax.set_facecolor(CLR_CARD)
            ax.set_title(title, color=CLR_TEXT, fontsize=9)
            ax.set_xlabel(xlabel, color=CLR_TEXT, fontsize=8)
            ax.set_ylabel(ylabel, color=CLR_TEXT, fontsize=8)
            ax.tick_params(colors=CLR_TEXT, labelsize=7)
            ax.grid(True, color=CLR_BORDER, linewidth=0.6)
            for spine in ax.spines.values():
                spine.set_color(CLR_BORDER)

        # 配對投影：走過的路徑（細線、低對比）+ 起點／目前位置／最佳點
        # （不同色 marker）+ 無效樣本（叉號）。用 set_data / set_offsets
        # 重繪既有 artist，不必每輪 ax.clear() 重畫全部。標題與軸標籤隨
        # 目前選取的軸對動態更新（見 _scan_redraw_figure），這裡先給
        # 通用預設值。
        (self._scan_line_xy_path,) = self._scan_ax_xy.plot(
            [], [], "-", color=CLR_MUTED, linewidth=0.8, zorder=1
        )
        self._scan_scatter_xy_bad = self._scan_ax_xy.scatter(
            [], [], color=CLR_DANGER, marker="x", s=28, zorder=2, label="無效"
        )
        self._scan_scatter_xy_start = self._scan_ax_xy.scatter(
            [], [], color=CLR_INFO, marker="o", s=32, zorder=3, label="起點"
        )
        self._scan_scatter_xy_best = self._scan_ax_xy.scatter(
            [], [], color=CLR_WARN, marker="*", s=90, zorder=4, label="最佳"
        )
        self._scan_scatter_xy_cur = self._scan_ax_xy.scatter(
            [], [], color=CLR_ACCENT, marker="o", s=40, zorder=5, label="目前"
        )
        self._scan_ax_xy.legend(
            fontsize=6, facecolor=CLR_CARD, edgecolor=CLR_BORDER, labelcolor=CLR_TEXT, loc="best"
        )

        # 「僅選取 1 軸，無法顯示 2D 投影」防呆文字——2 軸以上都至少有一組
        # 配對可畫，只有剛好 1 軸時完全沒有配對可言。用疊在子圖中央的文字
        # 取代散點/軌跡，見 _scan_redraw_figure。
        self._scan_pair_unavail_text = self._scan_ax_xy.text(
            0.5, 0.5, "", transform=self._scan_ax_xy.transAxes,
            ha="center", va="center", color=CLR_MUTED, fontsize=8, wrap=True,
        )

        # 座標變化趨勢：固定六條線（每軸一條），重繪時只更新這次搜尋涵蓋的
        # 軸，不動態增減 artist 數量。顏色刻意不用 CLR_ACCENT／CLR_DANGER／
        # CLR_WARN／CLR_INFO——這幾色在本專案是「目前選取軸」「警報」等
        # 全域語意，這裡的「軸」是搜尋範圍，混用會誤導。X/Y/Z 用 CLR_TEXT、
        # U/V/W 用 CLR_MUTED 分群，同群組內再用線型分軸。
        trend_style = {
            "X": (CLR_TEXT, "-"), "Y": (CLR_TEXT, "--"), "Z": (CLR_TEXT, ":"),
            "U": (CLR_MUTED, "-"), "V": (CLR_MUTED, "--"), "W": (CLR_MUTED, ":"),
        }
        self._scan_trend_lines = {}
        for ax_name in AXES:
            color, style = trend_style[ax_name]
            (line,) = self._scan_ax_xz.plot(
                [], [], style, color=color, linewidth=1.1, label=ax_name
            )
            self._scan_trend_lines[ax_name] = line

        # 功率收斂：即時功率折線 + 累積最佳（逐點 running max）虛線
        (self._scan_line_pwr_cur,) = self._scan_ax_pwr.plot(
            [], [], "-", color=CLR_ACCENT, linewidth=1.3, label="即時功率"
        )
        (self._scan_line_pwr_best,) = self._scan_ax_pwr.plot(
            [], [], "--", color=CLR_WARN, linewidth=1.3, label="累積最佳"
        )
        self._scan_ax_pwr.legend(
            fontsize=7, facecolor=CLR_CARD, edgecolor=CLR_BORDER, labelcolor=CLR_TEXT, loc="best"
        )

        self._scan_canvas = FigureCanvasTkAgg(self._scan_fig, master=chart_card)
        # 用 draw_idle() 而非 draw()：建構當下圖表全空（沒有任何樣本點），
        # 沒有必要在 __init__ 同步完成算圖，改成排進 Tk 主迴圈下一輪 idle
        # 才畫，量測約省下 180~230ms 的啟動阻塞時間（gridspec + 中文字型
        # 標籤的首次算版成本），視覺上沒有任何差異。
        self._scan_canvas.draw_idle()
        self._scan_canvas.get_tk_widget().pack(fill="both", expand=True, padx=8, pady=(4, 8))

        # 圖表下方數值摘要：仿光功率分頁大數字卡片的視覺語言，字級小很多。
        stat_row = tk.Frame(parent, bg=CLR_BG)
        stat_row.pack(fill="x", pady=(4, 0))
        for label, var in (
            ("目前功率 (dBm)", self._scan_cur_power_var),
            ("最佳功率 (dBm)", self._scan_best_power_var),
            ("樣本數", self._scan_n_var),
            ("目前座標", self._scan_coord_var),
        ):
            cell = tk.Frame(
                stat_row, bg=CLR_CARD, highlightbackground=CLR_BORDER, highlightthickness=1
            )
            cell.pack(side="left", fill="both", expand=True, padx=4)
            tk.Label(
                cell, text=label, bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8)
            ).pack(anchor="w", padx=8, pady=(6, 0))
            tk.Label(
                cell, textvariable=var, bg=CLR_CARD, fg=CLR_TEXT, font=("Consolas", 13, "bold")
            ).pack(anchor="w", padx=8, pady=(0, 6))

    def _update_scan_pair_controls(self, selected_axes):
        """
        依這次搜尋涵蓋的軸數，切換左上「投影軸對」控制列的顯示模式：
        ≥3 軸顯示 Combobox（可切換配對，預設選固定順序最前面兩軸的組合）、
        剛好 2 軸顯示靜態文字（只有一種可能，不需要互動元件）、剛好 1 軸
        整條列不顯示任何文字（沒有配對可言）。三個元件在 _build_scan_plot
        就都建好，這裡只切換 pack/pack_forget，不整條列一起隱藏。
        """
        ordered = [ax for ax in AXES if ax in selected_axes]
        self._scan_pair_options = [f"{a}-{b}" for a, b in itertools.combinations(ordered, 2)]

        self._scan_pair_label.pack_forget()
        self._scan_pair_combo.pack_forget()
        self._scan_pair_static_label.pack_forget()

        if len(ordered) >= 3:
            self._scan_pair_combo["values"] = self._scan_pair_options
            self._scan_pair_combo.set(self._scan_pair_options[0])
            self._scan_pair_label.pack(side="left")
            self._scan_pair_combo.pack(side="left", padx=(6, 0))
        elif len(ordered) == 2:
            self._scan_pair_static_label.config(text=f"投影軸對：{self._scan_pair_options[0]}")
            self._scan_pair_static_label.pack(side="left")
        # len(ordered) <= 1（0 理論上不會發生，_do_start_scan 已在打開確認
        # 視窗前擋下 0 軸）：三個元件都不顯示，配對投影子圖改顯示提示文字。

    def _update_scan_trend_legend(self, selected_axes):
        """
        重建座標變化趨勢子圖的圖例，只列出這次搜尋涵蓋的軸——沒畫的線
        不該出現在圖例裡造成困惑。
        """
        ordered = [ax for ax in AXES if ax in selected_axes]
        legend = self._scan_ax_xz.get_legend()
        if legend is not None:
            legend.remove()
        if ordered:
            handles = [self._scan_trend_lines[ax] for ax in ordered]
            self._scan_ax_xz.legend(
                handles, ordered, fontsize=6, facecolor=CLR_CARD, edgecolor=CLR_BORDER,
                labelcolor=CLR_TEXT, loc="best",
            )

    def _on_scan_pair_change(self, event=None):
        """
        「投影軸對」Combobox 的 command。這次搜尋頂多幾百筆樣本，直接重繪
        整個 figure 即可，不特別只重繪左上子圖——資料量小，拆分優化只會
        犧牲程式碼清晰度換不到有意義的效能差距。
        """
        if _MATPLOTLIB_AVAILABLE:
            self._scan_redraw_figure()

    def _scan_plot_reset(self, selected_axes=None):
        """
        清空尋光圖表資料與數值摘要，並依這次搜尋涵蓋的軸數重建「投影軸對」
        控制列與趨勢線圖例。在 _do_start_scan 開始新一輪尋光時呼叫
        （主執行緒／按鈕回呼，早於背景執行緒啟動，此時 scanner.active_axes
        還沒有值），避免上一輪殘留的軌跡疊在新一輪上面。

        selected_axes 對應 _do_start_scan 裡使用者這次勾選的搜尋軸。省略
        （None）時退回讀取目前 GUI 勾選狀態——供既有呼叫端（測試在案例
        之間單純想清空圖表、不關心控制列細節）沿用舊的免參數呼叫方式。
        """
        if selected_axes is None:
            selected_axes = [ax for ax in AXES if self._scan_axis_selected_vars[ax].get()]

        with self._scan_plot_lock:
            self._scan_plot_pending = []
        self._scan_samples = []
        self._scan_best_power = None
        self._scan_sample_count = 0
        self._scan_cur_power_var.set("—")
        self._scan_best_power_var.set("—")
        self._scan_n_var.set("0")
        self._scan_coord_var.set("—")

        if not _MATPLOTLIB_AVAILABLE:
            return

        ordered = [ax for ax in AXES if ax in selected_axes]
        self._scan_last_selected_axes = ordered

        empty_xy = np.empty((0, 2))  # set_offsets 內部要求 2D 形狀，空 list 會被當成 1D 陣列而炸掉
        self._scan_line_xy_path.set_data([], [])
        self._scan_line_pwr_cur.set_data([], [])
        self._scan_line_pwr_best.set_data([], [])
        for scatter in (
            self._scan_scatter_xy_bad, self._scan_scatter_xy_start,
            self._scan_scatter_xy_best, self._scan_scatter_xy_cur,
        ):
            scatter.set_offsets(empty_xy)
        self._scan_pair_unavail_text.set_text("")
        for line in self._scan_trend_lines.values():
            line.set_data([], [])

        self._update_scan_pair_controls(ordered)
        self._update_scan_trend_legend(ordered)

        self._scan_canvas.draw_idle()

    def _on_scan_sample(self, sample):
        """
        `FiberAlignmentScanner` 的 sample_cb，跑在 scanner 的背景執行緒。

        🔴 只能做資料寫入，不能碰 matplotlib 或 tkinter widget——實測
        FigureCanvasTkAgg.draw_idle() 從背景執行緒呼叫會丟
        RuntimeError: main thread is not in main loop（Python 3.14 的
        tkinter 強制檢查）。真正的重繪在 _redraw_scan_plot（主執行緒，
        root.after 固定節奏）進行，這裡只把樣本堆進待處理佇列。
        """
        with self._scan_plot_lock:
            self._scan_plot_pending.append(sample)

    def _redraw_scan_plot(self):
        """
        主執行緒、固定節奏（SCAN_PLOT_REDRAW_INTERVAL）把 sample_cb 累積的
        樣本套進圖表。跟 _start_poller 同一種自我重新排程模式，在
        _build_tab_scan 建立分頁時啟動一次，之後跑到程式結束為止——不需要
        尋光開始/結束時另外啟動/停止這條迴圈。

        尋光沒在跑時 _scan_plot_pending 理應是空的（sample_cb 不會被呼叫），
        這裡仍然檢查 self._scanning 再處理，避免尋光剛結束、佇列裡還有
        最後幾筆待處理樣本時被跳過而遺漏。

        ⚠ `_pm_sync_scan_notice()` 的呼叫已於 2026-08-20 搬去 `_start_poller`
        （100ms、不受「有沒有裝 matplotlib」影響的節奏）——這條迴圈整條
        在沒裝 matplotlib 時不會執行，之前掛在這裡會讓光功率分頁的提示列
        在那種環境下永遠不會被同步，這裡不再重複呼叫。
        """
        try:
            with self._scan_plot_lock:
                pending, self._scan_plot_pending = self._scan_plot_pending, []
            if pending:
                self._scan_plot_extend(pending)
        except tk.TclError:
            return  # widget 已被銷毀（關閉流程中），安靜收工
        except Exception:
            # 🔴 這條迴圈靠自我重新排程（最下面那行 root.after）延續到程式
            # 結束。若重新排程那行在 try 區塊外，_scan_plot_extend／
            # _scan_redraw_figure 丟出 tk.TclError 以外的任何例外都會讓
            # 這條鏈結永久斷掉，且 --windowed 打包後 sys.stderr 是 None、
            # 連 traceback 都看不到，等同靜默失效。記錄但不 return，讓下面
            # 的重新排程照樣執行，下一輪還有機會恢復正常。
            logger.exception("尋光即時圖表重繪失敗，本輪跳過")
        self.root.after(SCAN_PLOT_REDRAW_INTERVAL, self._redraw_scan_plot)

    def _scan_plot_extend(self, samples):
        """
        把一批新樣本併入 self._scan_samples 並更新數值摘要／圖表。

        只在主執行緒（_redraw_scan_plot）呼叫。以整份 self._scan_samples
        重建圖表資料而非逐筆累加差量——單輪掃描頂多幾百筆，重建成本可忽略，
        換來的是不必額外維護一堆平行 list 的正確性風險。
        """
        self._scan_samples.extend(samples)
        self._scan_sample_count = len(self._scan_samples)
        self._scan_n_var.set(str(self._scan_sample_count))

        last = samples[-1]
        self._scan_coord_var.set(
            f"X={last.coords.get('X', 0.0):.0f} "
            f"Y={last.coords.get('Y', 0.0):.0f} "
            f"Z={last.coords.get('Z', 0.0):.0f}"
        )
        for s in samples:
            if s.ok and s.power is not None:
                if self._scan_best_power is None or s.power > self._scan_best_power:
                    self._scan_best_power = s.power
        if last.ok and last.power is not None:
            self._scan_cur_power_var.set(f"{last.power:.2f}")
        else:
            # 最新樣本無效就照實顯示「—」，不要留著前一筆有效讀值——
            # 那會讓使用者誤以為目前位置訊號依然良好。
            self._scan_cur_power_var.set("—")
        if self._scan_best_power is not None:
            self._scan_best_power_var.set(f"{self._scan_best_power:.2f}")

        # 這批樣本裡最新一筆有效讀值也轉貼給光功率分頁——尋光期間背景輪詢
        # 已暫停（見 _pm_should_poll() 的 motion_active 判斷），光功率分頁
        # 不會有其他資料來源。
        #
        # 🔴 這裡只呼叫一個具名方法，不直接碰光功率分頁的任何欄位或 widget。
        # 舊寫法曾是「A 分頁的繪圖函式直接指派 B 分頁的快取欄位＋三個
        # widget」，跟 _pm_refresh_status_line() 的 scanning_active 分支
        # 重複，是兩份會走鐘的真相來源。狀態文字與前景色現在完全由
        # _pm_refresh_status_line() 決定（_pm_note_reading 會呼叫它）。
        if valid_samples := [s for s in samples if s.ok and s.power is not None]:
            self._pm_note_reading(valid_samples[-1].power, source="scan")

        if _MATPLOTLIB_AVAILABLE:
            self._scan_redraw_figure()

    def _scan_redraw_figure(self):
        """
        用 self._scan_samples 的完整歷史重建三張子圖的 artist 資料。
        呼叫端（_scan_plot_extend／_on_scan_pair_change）已確保只在主
        執行緒、matplotlib 可用時呼叫。
        """
        samples = self._scan_samples
        if not samples:
            return

        valid = [s for s in samples if s.ok]
        bad = [s for s in samples if not s.ok]
        empty_xy = np.empty((0, 2))  # set_offsets 內部要求 2D 形狀，空 list 會被當成 1D 陣列而炸掉

        # ── 配對投影：目前 Combobox／靜態文字選取的軸對，一定源自
        # _scan_plot_reset 依這次搜尋軸算出的 _scan_pair_options，本身就是
        # 選定範圍內的合法配對，不需要再對 active_axes 額外判斷可用性——
        # 跟舊版 XY/XZ 寫死時不同（那時使用者可能只搜 X/Z，卻仍畫著沒
        # 意義的 XY 投影）。
        pair_options = self._scan_pair_options
        if not pair_options:
            axis_name = self._scan_last_selected_axes[0] if self._scan_last_selected_axes else "?"
            self._scan_pair_unavail_text.set_text(
                f"僅選取 1 軸（{axis_name}），無法顯示 2D 投影，請見右側座標變化趨勢"
            )
            self._scan_ax_xy.set_title("投影", color=CLR_TEXT, fontsize=9)
            self._scan_ax_xy.set_xlabel("", color=CLR_TEXT, fontsize=8)
            self._scan_ax_xy.set_ylabel("", color=CLR_TEXT, fontsize=8)
            self._scan_line_xy_path.set_data([], [])
            for scatter in (
                self._scan_scatter_xy_bad, self._scan_scatter_xy_start,
                self._scan_scatter_xy_best, self._scan_scatter_xy_cur,
            ):
                scatter.set_offsets(empty_xy)
        else:
            current = pair_options[0] if len(pair_options) == 1 else (
                self._scan_pair_combo.get() or pair_options[0]
            )
            a, b = current.split("-")
            self._scan_pair_unavail_text.set_text("")
            self._scan_ax_xy.set_title(f"{a}-{b} 投影", color=CLR_TEXT, fontsize=9)
            self._scan_ax_xy.set_xlabel(f"{a} (pulse)", color=CLR_TEXT, fontsize=8)
            self._scan_ax_xy.set_ylabel(f"{b} (pulse)", color=CLR_TEXT, fontsize=8)

            xs = [s.coords.get(a, 0.0) for s in valid]
            ys = [s.coords.get(b, 0.0) for s in valid]
            self._scan_line_xy_path.set_data(xs, ys)

            start, cur = samples[0], samples[-1]
            self._scan_scatter_xy_start.set_offsets(
                [[start.coords.get(a, 0.0), start.coords.get(b, 0.0)]]
            )
            self._scan_scatter_xy_cur.set_offsets(
                [[cur.coords.get(a, 0.0), cur.coords.get(b, 0.0)]]
            )

            best_sample = None
            for s in valid:
                if s.power is not None and (best_sample is None or s.power > best_sample.power):
                    best_sample = s
            if best_sample is not None:
                self._scan_scatter_xy_best.set_offsets(
                    [[best_sample.coords.get(a, 0.0), best_sample.coords.get(b, 0.0)]]
                )
            else:
                self._scan_scatter_xy_best.set_offsets(empty_xy)

            if bad:
                self._scan_scatter_xy_bad.set_offsets(
                    [[s.coords.get(a, 0.0), s.coords.get(b, 0.0)] for s in bad]
                )
            else:
                self._scan_scatter_xy_bad.set_offsets(empty_xy)

        # ── 座標變化趨勢：active_axes 是 scanner.run() 開始後才會有值，
        # 尚未落地（active is None）的極短暫過渡態沿用 _scan_plot_reset
        # 當時算好的 selected_axes，等下一輪重繪自然校正（已知的既有
        # 結論，見 CLAUDE.md〈尋光彈性選軸〉，不特別處理）。x 軸序號邏輯
        # 跟下面的功率收斂子圖同一套：用樣本在整批歷史裡的原始序號，
        # 無效樣本直接跳過（不重新壓縮編號），y 軸是相對起點的位移量。
        scanner = self._active_scanner
        active = getattr(scanner, "active_axes", None) if scanner else None
        trend_axes = active if active else self._scan_last_selected_axes
        base = samples[0]
        base_vals = {ax_name: base.coords.get(ax_name, 0.0) for ax_name in AXES}
        trend_idx = []
        trend_delta = {ax_name: [] for ax_name in AXES}
        for i, s in enumerate(samples, start=1):
            if s.ok:
                trend_idx.append(i)
                for ax_name in AXES:
                    trend_delta[ax_name].append(s.coords.get(ax_name, base_vals[ax_name]) - base_vals[ax_name])
        for ax_name, line in self._scan_trend_lines.items():
            if ax_name in trend_axes:
                line.set_data(trend_idx, trend_delta[ax_name])
            else:
                line.set_data([], [])

        # 功率收斂：x 軸用樣本在整批歷史裡的序號（1-based），無效樣本沒有
        # power 可畫，直接跳過——折線會連過那些序號，不畫斷點，這是合理的
        # 「只連有效讀值」呈現，不是資料遺失。
        pwr_idx, pwr_val, pwr_best = [], [], []
        running_best = None
        for i, s in enumerate(samples, start=1):
            if s.ok and s.power is not None:
                if running_best is None or s.power > running_best:
                    running_best = s.power
                pwr_idx.append(i)
                pwr_val.append(s.power)
                pwr_best.append(running_best)
        self._scan_line_pwr_cur.set_data(pwr_idx, pwr_val)
        self._scan_line_pwr_best.set_data(pwr_idx, pwr_best)

        for ax in (self._scan_ax_xy, self._scan_ax_xz, self._scan_ax_pwr):
            ax.relim()
            ax.autoscale_view()

        self._scan_canvas.draw_idle()

    def _do_start_scan(self):
        if not self.ctrl.connected:
            self._flash_banner("尋光需要先連線 DS102")
            return
        if self.meter is None:
            self._flash_banner("尋光需要先連線光功率計")
            return
        if self.ctrl.scanning_active or self._scanning.is_set():
            self._flash_banner("已有搜尋在進行中")
            return

        # 搜尋軸清單必須在主執行緒讀出來（BooleanVar.get()），理由跟下面
        # initial_step 那段一致——tkinter Variable 不可從背景執行緒讀取。
        # 0 軸要在打開確認對話框之前擋下，不要讓使用者看到一個注定沒有
        # 意義的確認視窗。
        selected_axes = [ax for ax in AXES if self._scan_axis_selected_vars[ax].get()]
        if not selected_axes:
            self._flash_banner("尋光需要至少選擇一個軸")
            return

        # 四個速度欄位與可選的 f_speed_min 都是原始文字輸入，直接組進
        # DS102 指令字串或送進 float()。float() 能接受 "nan"／"inf"／負值，
        # 但這些值送進控制器毫無意義（可能被整條拒收，或讓滑台用未定義
        # 速度移動）——一律在打開確認對話框之前擋下，不該讓使用者看到注定
        # 失敗的確認視窗，也不能讓壞值走到已送出序列埠指令那一步才發現。
        def _validate_speed(raw: str, label: str) -> float:
            try:
                value = float(raw)
            except ValueError:
                raise ValueError(f"{label} 必須是數字，目前是「{raw}」") from None
            if not math.isfinite(value):
                raise ValueError(f"{label} 必須是有限數值，不可為 NaN 或無限大")
            if value <= 0:
                raise ValueError(f"{label} 必須是正值，目前是 {value:g}")
            return value

        try:
            _validate_speed(self._scan_l_speed_var.get().strip() or "50", "Start-up Speed (L)")
            f_speed_val = _validate_speed(self._scan_f_speed_var.get().strip() or "10000", "Driving Speed (F)")
            _validate_speed(self._scan_rate_var.get().strip() or "1000", "Accel/Decel Rate (R)")
            _validate_speed(self._scan_s_rate_var.get().strip() or "50", "S-curve Rate (S)")
            f_speed_min_raw = self._scan_f_speed_min_var.get().strip()
            if f_speed_min_raw:
                f_speed_min_val = _validate_speed(f_speed_min_raw, "最低速度 f_speed_min")
                if f_speed_min_val > f_speed_val:
                    raise ValueError(
                        f"最低速度 f_speed_min（{f_speed_min_val:g}）不可大於 "
                        f"Driving Speed F（{f_speed_val:g}）"
                    )
        except ValueError as e:
            self._flash_banner(f"尋光參數錯誤：{e}")
            return

        axis_summary = " ".join(f"{ax}={self._scan_axis_step_vars[ax].get()}" for ax in selected_axes)
        axes_txt = "、".join(selected_axes)
        stage2_txt = "啟用" if self._scan_stage2_var.get() else "不啟用"
        # 演算法：跟 blind_mode 同一個既有慣例，Combobox 存標籤字串，這裡
        # 反解成內部代碼。不合法的值（理論上不會發生，防禦用）一律退回
        # 座標下降；「選了 Powell 但 scipy 不可用」這個情況刻意不在這裡
        # 靜默降級——留給下面 self._scanning.set() 之前那道明確的防線，
        # 讓使用者看到清楚的錯誤訊息，而不是被悄悄改成別的演算法。
        algo_label = self._scan_algo_var.get()
        algorithm = self._scan_algo_label_algos.get(algo_label, "coordinate_descent")
        if algorithm not in ("coordinate_descent", "powell"):
            algorithm = "coordinate_descent"
        if algorithm == "powell":
            algo_line = "Powell 共軛方向法（⚠ 未真機驗證，起跳參數）"
            # Powell 不吃「起始步長」（見 _refresh_scan_entry_states 的
            # 註解），對使用者顯示那一行是誤導；真正生效的是這裡的
            # max_iterations（函式評估次數上限），改列這個才對得上實際
            # 行為。跟下面 _run() 讀 powell_max_iter 時同一個變數來源，
            # 這裡提早、獨立解析一次只是給確認對話框看，不影響那邊。
            try:
                _powell_max_iter_preview = int(self._scan_powell_max_iter_var.get())
            except (ValueError, AttributeError):
                _powell_max_iter_preview = 200
            step_or_iter_line = f"最多函式評估次數：{_powell_max_iter_preview}\n"
        else:
            algo_line = f"座標下降（階段二精修：{stage2_txt}）"
            step_or_iter_line = f"起始步長：{axis_summary}\n"
        floor_val = self._scan_min_valid_power_var.get().strip()
        floor_txt = "未設定（僅依讀值相對變化判斷）" if not floor_val else f"{floor_val} dBm"
        abort_txt = "是" if self._scan_abort_no_signal_var.get() else "否"

        # 盲搜是本專案單次自動運動量最大的操作（可達上千個格點），規模必須
        # 攤在確認對話框裡，不能只寫在設定卡片上——使用者按下開始的那一刻
        # 才是真正要為這段機械運動負責的時點。
        blind_mode = self._scan_blind_label_modes.get(
            self._scan_blind_mode_var.get(), GUI_DEFAULT_BLIND_MODE
        )
        if blind_mode == "off":
            blind_txt = "關閉"
        else:
            plane_txt = self._scan_blind_plane_var.get() or f"自動（{axes_txt} 的前兩軸）"
            mode_txt = "無訊號時才啟動" if blind_mode == "auto" else "一律先執行"
            blind_txt = (
                f"{mode_txt}｜平面 {plane_txt}｜"
                f"{self._scan_blind_estimate_var.get().lstrip('→ ') or '規模未知'}"
            )

        if not messagebox.askyesno(
            "確認開始尋光",
            f"搜尋軸：{axes_txt}\n\n"
            f"即將開始自動尋光，滑台會依演算法自主移動並持續量測光功率。\n\n"
            f"{step_or_iter_line}"
            f"演算法：{algo_line}\n"
            f"訊號有效性下限：{floor_txt}\n"
            f"無訊號時中止：{abort_txt}\n"
            f"階段零盲搜：{blind_txt}\n"
            f"預估時間：無法精確預估，過去測試單輪落在數十秒到數分鐘不等\n\n"
            f"⚠ 尋光不保證找到訊號，也不代表光纖已對準——搜尋結束仍請自行確認\n"
            f"　光功率讀值是否落在可接受範圍。\n"
            f"⚠ 過程中「光功率」分頁的讀值改由本頁提供，該分頁的自動輪詢會暫停。\n"
            f"⚠ 開始前請確認：光纖已初步耦合、光功率計已連線且讀值正常、\n"
            f"　目前位置在行程範圍內有足夠的移動空間可供搜尋。\n\n"
            f"確定要開始嗎？",
            icon="warning", default="no",
        ):
            return

        # 使用者確認後才存檔——只存跨次搜尋穩定的參數。initial_step、
        # stage2_local_radius、axis_scale 依當次搜尋範圍而定，刻意不存，
        # 存了只會誘使使用者延用不適合這次的舊值。
        _save_scanner_config(
            {
                "l_speed": self._scan_l_speed_var.get(),
                "f_speed": self._scan_f_speed_var.get(),
                "rate": self._scan_rate_var.get(),
                "s_rate": self._scan_s_rate_var.get(),
                "f_speed_min": self._scan_f_speed_min_var.get(),
                "step_min": self._scan_step_min_var.get(),
                "settle_sec": self._scan_settle_sec_var.get(),
                "max_cycles": self._scan_max_cycles_var.get(),
                "noise_sigma_mult": self._scan_noise_sigma_mult_var.get(),
                "no_signal_range_mult": self._scan_range_mult_var.get(),
                "abort_if_no_signal": self._scan_abort_no_signal_var.get(),
                "min_valid_power_dbm": self._scan_min_valid_power_var.get(),
                "enable_stage2": self._scan_stage2_var.get(),
                # 跟 blind_mode 同類——跨次搜尋穩定的設定，該存。
                # 🔴 scipy 不可用時 Combobox 不包含 Powell 選項，使用者選
                # 不到它，這裡存的 `algorithm` 必定是 coordinate_descent；
                # 若設定檔原本有 "powell"，每次在 scipy 不可用的環境啟動
                # 都會被覆寫掉。裝回 scipy 後使用者需要重新手動選一次。
                "algorithm": algorithm,
                "powell_max_iterations": self._scan_powell_max_iter_var.get(),
                "selected_axes": selected_axes,
                # 盲搜參數跟 l_speed／step_min 同類：跟裝置物理配置綁定、
                # 跨次搜尋穩定，該存。（initial_step／stage2 半徑那種依當次
                # 搜尋範圍而定的才刻意不存，見上方註解。）
                "blind_mode": blind_mode,
                "blind_plane": self._scan_blind_plane_var.get(),
                "blind_step": self._scan_blind_step_var.get(),
                "blind_max_radius": self._scan_blind_radius_var.get(),
                "blind_signal_sigma_mult": self._scan_blind_sigma_mult_var.get(),
                "blind_signal_min_delta_db": self._scan_blind_min_delta_var.get(),
            },
            log=self.ctrl._log,
        )

        # 🔴 最後一道防線：選了 Powell 但這個環境沒裝 scipy。UI 端的
        # Combobox 已經不讓使用者選到這個組合（未裝 scipy 時 values 只有
        # 座標下降），但設定檔可能存過舊的 powell 選擇、或未來 UI 邏輯
        # 有漏洞——一律在啟動背景執行緒之前擋下並給清楚訊息，不要讓它
        # 進到 _run() 裡才炸：那會被 _run() 的 `except Exception` 接住，
        # 誤分類成「未預期例外」，使用者看不出真正原因是缺套件。
        if algorithm == "powell" and not fiber_scanner_advanced._SCIPY_AVAILABLE:
            messagebox.showerror(
                "缺少 scipy",
                "已選擇 Powell 共軛方向法，但目前環境未安裝 scipy，無法執行"
                f"（{fiber_scanner_advanced._SCIPY_IMPORT_ERROR}）。\n\n"
                "請安裝 scipy 後再試，或改選「座標下降」演算法。",
            )
            return

        self._scanning.set()  # 早於執行緒啟動，避免 _update_stat_ui 的窗口期把按鈕解鎖
        self._scan_start_btn.config(state="disabled")
        self._scan_stop_btn.config(state="normal")
        self._scan_status_var.set("初始化中…")
        self._scan_start_time = time.time()
        self._scan_plot_reset(selected_axes)  # 清掉上一輪殘留的軌跡與數值摘要，並重建投影軸對控制列
        # 上一輪如果是無訊號中止、使用者還沒按「知道了」就直接開始下一輪，
        # 這條常駐提示不該繼續掛著誤導這一輪的狀態。
        self._hide_scan_no_signal_notice()
        # 同理必須重設：新掃描還在跑的時候若按下「匯出 Excel」
        # （_export_scan_xlsx），不重設這兩個會把上一輪的完成／中止狀態
        # 誤標到這一輪還在進行中的報表上。
        self._scan_last_completed = None
        self._scan_last_abort_reason = None

        initial_step = {}
        for ax, var in self._scan_axis_step_vars.items():
            try:
                initial_step[ax] = int(var.get())
            except ValueError:
                initial_step[ax] = 64
        # 以下全部必須在主執行緒讀出來存進區域變數——tkinter Variable.get()
        # 不可從背景執行緒呼叫（Python 3.14 的 tkinter 會直接丟
        # RuntimeError: main thread is not in main loop，2026-08-17 實測
        # 踩到），下面的 _run() 跑在背景執行緒，不能在裡面呼叫任何
        # self._scan_*_var.get()。
        enable_stage2 = self._scan_stage2_var.get()

        def _parse_float_or_none(s):
            s = s.strip()
            if not s:
                return None
            try:
                return float(s)
            except ValueError:
                return None

        def _parse_float(s, default):
            try:
                return float(s)
            except (ValueError, AttributeError):
                return default

        def _parse_int(s, default):
            try:
                return int(s)
            except (ValueError, AttributeError):
                return default

        # Powell 專用：maxfev。跟 enable_stage2 一樣必須在主執行緒讀出來，
        # algorithm 本身已經在確認對話框那段讀過、驗證過，這裡直接沿用
        # 同一個變數，不重新反解一次 Combobox（同一個理由：那時已經進不
        # 了主執行緒，兩次讀取也有機會不一致）。
        powell_max_iter = _parse_int(self._scan_powell_max_iter_var.get(), 200)

        # 型別刻意混雜（str/int/float/bool），Pylance 對 **kwargs 展開會因此
        # 把每個參數都推論成聯集型別而報一串資訊等級提示——都是誤報，
        # 實際值在 _parse_int/_parse_float 已轉成 FiberAlignmentScanner
        # 建構子要求的正確型別。
        scanner_kwargs = {
            "l_speed": self._scan_l_speed_var.get().strip() or "50",
            "f_speed": self._scan_f_speed_var.get().strip() or "10000",
            "rate": self._scan_rate_var.get().strip() or "1000",
            "s_rate": self._scan_s_rate_var.get().strip() or "50",
            "step_min": _parse_int(self._scan_step_min_var.get(), DEFAULT_STEP_MIN),
            "settle_sec": _parse_float(self._scan_settle_sec_var.get(), DEFAULT_SETTLE_SEC),
            "max_cycles": _parse_int(self._scan_max_cycles_var.get(), DEFAULT_MAX_CYCLES),
            "noise_sigma_mult": _parse_float(self._scan_noise_sigma_mult_var.get(), DEFAULT_NOISE_SIGMA_MULT),
            "min_valid_power_dbm": _parse_float_or_none(self._scan_min_valid_power_var.get()),
            "no_signal_range_mult": _parse_float(self._scan_range_mult_var.get(), DEFAULT_NO_SIGNAL_RANGE_MULT),
            "abort_if_no_signal": self._scan_abort_no_signal_var.get(),
            # blind_mode 已在確認對話框那段從 Combobox 標籤反解成內部代碼
            # （"off"/"auto"/"always"），這裡直接沿用同一個值，不要再讀一次
            # Combobox——那時已經進不了主執行緒，而且兩次讀取有機會不一致。
            "blind_mode": blind_mode,
            "blind_step": _parse_int(self._scan_blind_step_var.get(), DEFAULT_BLIND_STEP),
            "blind_max_radius": _parse_int(
                self._scan_blind_radius_var.get(), DEFAULT_BLIND_MAX_RADIUS
            ),
            "blind_signal_sigma_mult": _parse_float(
                self._scan_blind_sigma_mult_var.get(), DEFAULT_BLIND_SIGNAL_SIGMA_MULT
            ),
            "blind_signal_min_delta_db": _parse_float(
                self._scan_blind_min_delta_var.get(), DEFAULT_BLIND_SIGNAL_MIN_DELTA_DB
            ),
            "on_signal_found": self._on_scan_signal_found,
        }
        # 掃描平面存的是 "X-Y" 這種字串（跟 _scan_pair_combo 同一個既有慣例，
        # 存 index 會在清單長度變動時整組差一位）。留空＝交給演算法自動取
        # 本次搜尋軸的前兩軸，這裡就不傳這個參數。
        blind_plane_raw = self._scan_blind_plane_var.get().strip()
        if blind_plane_raw and "-" in blind_plane_raw:
            pa, _, pb = blind_plane_raw.partition("-")
            if pa in AXES and pb in AXES:
                scanner_kwargs["blind_axes"] = (pa, pb)
        f_speed_min_raw = self._scan_f_speed_min_var.get().strip()
        if f_speed_min_raw:
            scanner_kwargs["f_speed_min"] = f_speed_min_raw

        lo_raw = self._scan_speed_scale_lo_var.get().strip()
        hi_raw = self._scan_speed_scale_hi_var.get().strip()
        if lo_raw and hi_raw:
            try:
                scanner_kwargs["speed_scale_pulses"] = (int(lo_raw), int(hi_raw))
            except ValueError:
                pass  # 格式錯就讓函式庫走自動預設，不因為進階欄位打錯字而擋下整個尋光

        # 階段二專用參數是 run() 的參數，不是建構子參數——只有勾選階段二
        # 且欄位有填才組字典傳入，沒填就讓函式庫用它自己的預設半徑/係數。
        run_kwargs = {}
        if enable_stage2:
            radius = {}
            for ax, var in self._scan_stage2_radius_vars.items():
                raw = var.get().strip()
                if raw:
                    try:
                        radius[ax] = int(raw)
                    except ValueError:
                        pass
            if radius:
                run_kwargs["stage2_local_radius"] = radius

            axis_scale = {}
            for ax, var in self._scan_stage2_axis_scale_vars.items():
                raw = var.get().strip()
                if raw:
                    try:
                        axis_scale[ax] = float(raw)
                    except ValueError:
                        pass
            if axis_scale:
                run_kwargs["axis_scale"] = axis_scale

        def _progress(msg):
            self.root.after(0, lambda: self._scan_status_var.set(msg))
            self.ctrl._log("INFO", f"[尋光] {msg}")

        # 🔴 f_speed／f_speed_min 是未經驗證的原始文字輸入，FiberAlignmentScanner
        # 建構子內部直接 float() 轉換、失敗就丟 ValueError——此時 _scanning
        # 已經 set()、兩個按鈕也已切換成「搜尋中」狀態，若不在這裡接住，
        # 例外會直接炸穿 _do_start_scan()：背景執行緒從未啟動，
        # _on_scan_done() 也就永遠不會被呼叫去解鎖畫面，UI 會卡死在
        # 「掃描中」，開始鍵永久按不下去。
        try:
            scanner = FiberAlignmentScanner(
                self.ctrl, self._scanner_power_query,
                progress_cb=_progress,
                sample_cb=self._on_scan_sample,
                selected_axes=selected_axes,
                **scanner_kwargs,
            )
        except (ValueError, TypeError) as e:
            self._scanning.clear()
            self._scan_start_btn.config(state="normal")
            self._scan_stop_btn.config(state="disabled")
            self._scan_status_var.set("尚未開始")
            self.ctrl._log("ERROR", f"[尋光] 參數錯誤，無法建立 scanner: {e}")
            messagebox.showerror("參數錯誤", f"尋光參數輸入有誤，無法開始：\n{e}")
            return
        self._active_scanner = scanner

        def _run():
            try:
                result = scanner.run(
                    initial_step=initial_step,
                    enable_stage2=enable_stage2,
                    algorithm=algorithm,
                    powell_max_iterations=powell_max_iter,
                    **run_kwargs,
                )
                # 🔴 FiberAlignmentScanner.run() 內部把所有中止事件都自己接住、
                # 正常 return（刻意設計，中止是正常結束路徑），所以這裡的
                # `except ScanAbort` 只會接到 run() 開頭那兩個前置檢查（防呆）。
                # 要分辨「真的收斂完成」還是「中途被中止」，必須在 run() 正常
                # 返回後讀 scanner.last_abort_reason（曾漏讀，導致 EMS／使用者
                # 停止／無訊號中止全部被誤報成「✔ 尋光完成」）。
                if scanner.last_abort_reason is None:
                    self.root.after(0, lambda: self._on_scan_done(kind="completed", result=result, err=None))
                else:
                    err_msg = scanner.last_abort_reason
                    kind = self._classify_scan_abort(
                        err_msg, getattr(scanner, "last_abort_kind", None)
                    )
                    self.root.after(0, lambda: self._on_scan_done(kind=kind, result=None, err=err_msg))
            except ScanAbort as e:
                # ⚠ `except X as e` 的 e 會在 except 區塊結束時被自動 del，
                # 而 root.after(0, ...) 是非同步排程、lambda 真正執行時區塊
                # 早已結束——直接在 lambda 裡引用 e 會是 NameError。
                # 先把訊息轉成字串存進區域變數，讓 lambda 捕捉的是它而非 e。
                err_msg = str(e)
                kind = self._classify_scan_abort(err_msg)
                self.root.after(0, lambda: self._on_scan_done(kind=kind, result=None, err=err_msg))
            except Exception as e:
                logger.exception("尋光執行緒發生未預期例外")
                err_msg = f"未預期例外: {e}"
                self.root.after(0, lambda: self._on_scan_done(kind="exception", result=None, err=err_msg))

        threading.Thread(target=_run, daemon=True).start()

    @staticmethod
    def _classify_scan_abort(err_msg: str, abort_kind: Optional[str] = None) -> str:
        """
        把中止資訊分類成 _on_scan_done 認得的 kind。

        `abort_kind` 是 `scanner.last_abort_kind`（型別化分類，"no_signal"／
        "other"／None）。**有值時優先採信**——它由 fiber_scanner 依例外型別
        直接產生，不受訊息文字改動影響。2026-08-26 新增盲搜時一口氣多了四
        種無訊號中止訊息，全都不含舊版比對的那個子字串，純字串比對會讓它們
        統統掉進「其他錯誤」分支、失去專屬的 no_signal 呈現。

        字串比對保留兩個用途：一是判斷 user_stopped（`_check_abort()` 拋的
        兩則訊息是完整字串、逐字相符，型別上都是普通 ScanAbort 無從分辨）；
        二是 `abort_kind` 為 None 時的向下相容（舊版 scanner 物件、或直接
        以訊息字串呼叫本函式的既有測試）。
        """
        if err_msg in ("使用者中止搜尋", "EMS 觸發，搜尋中止"):
            return "user_stopped"
        if abort_kind == "no_signal":
            return "no_signal"
        if abort_kind is None and "沒有偵測到高於雜訊的訊號" in err_msg:
            return "no_signal"
        return "aborted_other"

    def _do_stop_scan(self):
        """尋光分頁自己的停止鍵：只停尋光，不動其他長時間作業。"""
        if self.ctrl.connected:
            self.ctrl.stop()
        # 走 _request_stop_scanner() 而非自己再判一次 None：_active_scanner
        # 的生命週期判斷只留一份，跟註冊表用的是同一條路徑。
        self._request_stop_scanner()
        self._scan_status_var.set("停止中…")
        self._scan_stop_btn.config(state="disabled")

    def _on_scan_done(self, kind: str, result, err):
        self._scanning.clear()
        # 先把 scanner 身上這輪的收尾資訊撈出來再放掉它——下面就把
        # _active_scanner 清成 None 了，之後（例如使用者稍後才按「匯出
        # Excel」）沒有第二次機會問。
        scanner = self._active_scanner
        auto_xlsx = getattr(scanner, "last_xlsx_path", None) if scanner else None
        self._scan_last_completed = kind == "completed"
        self._scan_last_abort_reason = None if kind == "completed" else err
        self._active_scanner = None
        self._scan_start_btn.config(state="normal")
        self._scan_stop_btn.config(state="disabled")
        self._restore_meter_auto_range()

        # scanner.run() 內 sample_cb 是同步呼叫，跑到這裡時所有樣本理論上
        # 早就已經 append 進 _scan_plot_pending——但 _redraw_scan_plot 把
        # pending 併入 _scan_samples／更新 _scan_sample_count 是靠 250ms
        # 節奏的 root.after，而這裡的 root.after(0, ...) 有機會搶在下一輪
        # 節奏之前先執行，導致讀到的 _scan_sample_count 少算最後一批。
        # 手動跑一次跟 _redraw_scan_plot 一樣的搬移邏輯，確保訊息裡的樣本數
        # 是這輪真正的最終值。
        try:
            with self._scan_plot_lock:
                pending, self._scan_plot_pending = self._scan_plot_pending, []
            if pending:
                self._scan_plot_extend(pending)
        except tk.TclError:
            pass  # widget 已被銷毀（關閉流程中），安靜略過，不影響下方狀態文字

        if kind == "completed":
            self._scan_status_var.set(f"完成 — 最終座標 {result}")
            self._flash_banner(
                f"✔ 尋光完成 — 最終座標 {result}（共 {self._scan_sample_count} 筆樣本，"
                f"耗時 {self._scan_elapsed_var.get()}）"
                + (f"　報表：{auto_xlsx.name}" if auto_xlsx else "")
            )
        elif kind == "user_stopped":
            self._scan_status_var.set("已停止（使用者中止）")
            self._flash_banner(f"■ 已停止尋光（使用者中止）— 已收集 {self._scan_sample_count} 筆樣本")
        elif kind == "no_signal":
            self._scan_status_var.set("已中止（未偵測到訊號）")
            self._show_scan_no_signal_notice(
                "未偵測到可用訊號，搜尋已中止 — 請確認光纖已耦合、光功率計連線正常後再重試"
            )
            self.ctrl._log("WARN", f"[尋光] {err}")
        else:  # "aborted_other" 或 "exception"
            self._scan_status_var.set(f"已結束（{err}）")
            messagebox.showerror(
                "尋光異常結束",
                f"{err}\n\n滑台可能停在搜尋過程中的任意位置，請確認目前座標與光纖狀態後再繼續操作。",
            )
            self.ctrl._log("ERROR", f"[尋光] {err}")

        # ctrl.scanning_active 這時已經是 False，靠下一輪 _redraw_scan_plot
        # （250ms 節奏）也會自然收回提示列，但這裡主動呼叫一次讓收尾更
        # 即時，不必讓使用者多等最多一個節奏週期。
        self._pm_sync_scan_notice()

    def _export_scan_xlsx(self):
        """
        把目前記憶體裡的尋光樣本另存成 Excel。

        跟 `FiberAlignmentScanner.persist_samples()` 每輪自動寫進
        `recordings/scans/` 的那份是**同一個產生器**（`export_samples_xlsx`），
        差別只在這裡讓使用者挑存檔位置——要交出去給別人看的那份通常不會想
        放在程式目錄底下。

        資料來源是 `self._scan_samples`（主執行緒專用的完整歷史），不是
        scanner 實例——搜尋結束後 `_active_scanner` 已經被清成 None，但畫面
        上的樣本還在，使用者這時才想到要匯出是很正常的操作順序。
        """
        # 併入還沒被 250ms 重繪節奏搬過來的最後一批，理由同 _on_scan_done：
        # 搜尋剛結束就馬上按匯出時，尾巴那幾筆有機會還卡在 pending。
        try:
            with self._scan_plot_lock:
                pending, self._scan_plot_pending = self._scan_plot_pending, []
            if pending:
                self._scan_plot_extend(pending)
        except tk.TclError:
            pass

        samples = list(self._scan_samples)  # 取快照：對話框開著時背景仍可能追加
        if not samples:
            self._flash_banner("尚未有任何尋光樣本可匯出", 5000)
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel 活頁簿", "*.xlsx"), ("All", "*.*")],
            initialfile=f"scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        )
        if not path:
            return
        try:
            export_samples_xlsx(
                samples,
                Path(path),
                completed=self._scan_last_completed,
                abort_reason=self._scan_last_abort_reason,
                extra_meta={"匯出方式": "使用者手動匯出（尋光分頁）"},
            )
        except Exception as e:
            # PermissionError 是這裡最常見的失敗：目標檔正被 Excel 開著。
            # 訊息直接把它講出來，比讓使用者自己猜「為什麼存不了」有用。
            messagebox.showerror("匯出失敗", f"Excel 匯出失敗：\n{e}")
            self.ctrl._log("ERROR", f"[尋光] Excel 匯出失敗: {e}")
            return
        self.ctrl._log("INFO", f"[尋光] 已匯出 Excel：{path}（{len(samples)} 筆樣本）")
        messagebox.showinfo("完成", f"已匯出 {len(samples)} 筆樣本:\n{path}")

    def _show_scan_no_signal_notice(self, msg: str):
        """
        顯示「無訊號中止」常駐提示列。狀態驅動但不跟著 scanning_active
        自動收回——刻意留著直到使用者按「知道了」，避免搜尋已經因為
        無訊號中止而使用者沒注意到（跟 _pm_scan_notice 那種「尋光進行中」
        的自動收放提示不是同一種語意）。
        """
        self._scan_no_signal_notice_label.config(text=f"⚠ {msg}")
        # ⚠ 用 winfo_manager() 而非 winfo_ismapped() 判斷是否已顯示：這個
        # 提示列在「尋光」分頁裡，切到別的分頁時 winfo_ismapped() 對未選取
        # 分頁下的元件一律回傳 False，會讓收回邏輯誤判「本來就沒顯示」而
        # 不呼叫 pack_forget()，提示殘留到切回分頁時還在。winfo_manager()
        # 只反映 pack()/pack_forget() 呼叫過沒有，不受分頁選取影響（同
        # _pm_scan_notice 的判斷方式）。
        if self._scan_no_signal_notice.winfo_manager() == "":
            self._scan_no_signal_notice.pack(side="top", fill="x", after=self._scan_toolbar)

    def _hide_scan_no_signal_notice(self):
        if self._scan_no_signal_notice.winfo_manager() != "":
            self._scan_no_signal_notice.pack_forget()

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
            # 所有長時間背景作業都要在 disconnect() 之前收到停止請求：它們
            # 只認自己的旗標、不看連線狀態，少了這步就會繼續對已經關閉的
            # 序列埠打指令。原點復歸重現性量測最壞情況會空等一輪 30s
            # （_wait_axis_stop）加一輪 180s（_wait_origin_done）逾時才發現，
            # 卡住一個多小時（architect 2026-08-21 審查）；尋光則是同一個
            # bug 的第二個入口（_on_close 修過、這裡以前漏了，2026-08-31 審查）。
            # 分派放在 ctrl.stop() 之前，讓背景執行緒盡早知道要收工。
            self._request_stop_long_ops()
            # 中斷期間鎖住按鈕：stop()/disconnect() 搬進背景執行緒後，
            # _toggle_connect() 會立刻返回，此時 self.ctrl.connected 仍是
            # True，若不 disable 按鈕，使用者連點會疊出第二條中斷執行緒
            # （architect 2026-09-03 審查）。
            self._conn_btn.config(text="中斷中...", state="disabled", bg=CLR_WARN)

            def _do_disconnect():
                # 先停再斷。少了這行，移動中按「中斷」會關掉 port 卻讓
                # 馬達繼續跑，程式從此失去對它的控制
                # （_on_close 有做，這裡以前漏了）。
                try:
                    self.ctrl.stop()
                    self.ctrl.disconnect()
                except Exception:
                    logger.exception("中斷連線流程失敗")
                finally:
                    # 關窗流程已經 set 這個旗標並準備 destroy root，此時
                    # 排 after 只會對已銷毀的 widget 操作，直接放棄。
                    if not self._shutting_down.is_set():
                        try:
                            self.root.after(0, self._on_disconnect_result)
                        except Exception:
                            pass

            threading.Thread(target=_do_disconnect, daemon=True).start()
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
                # 關窗流程可能與連線流程同時在跑，root 這時可能已 destroy
                # （中斷分支的同一個既有缺陷，一併補上，見 _do_disconnect）。
                if not self._shutting_down.is_set():
                    try:
                        self.root.after(0, lambda: self._on_connect_result(ok, msg))
                    except Exception:
                        pass

            threading.Thread(target=_do, daemon=True).start()

    def _on_disconnect_result(self) -> None:
        """`_do_disconnect()` 背景執行緒收工後，回主執行緒做的 UI 更新。"""
        self._conn_btn.config(state="normal", text="連線", bg=CLR_ACCENT)
        self._conn_dot.itemconfig(self._conn_dot_id, fill=CLR_DANGER)
        self._conn_lbl.config(text="未連線")
        self._fw_var.set("（未連線）")
        self._set_drive_buttons_state("disabled")
        self._set_axis_btns_state("disabled")

    def _on_connect_result(self, ok: bool, msg: str):
        self._conn_btn.config(state="normal")
        if ok:
            self._conn_dot.itemconfig(self._conn_dot_id, fill=CLR_ACCENT)
            self._conn_lbl.config(text=f"{self.ctrl.port} @ {self.ctrl.baudrate}")
            self._conn_btn.config(text="中斷", bg=CLR_DANGER)
            _drdiv_txt = "、".join(f"{ax}={v}" for ax, v in self.ctrl.axis_drdiv.items())
            self._fw_var.set(
                f"韌體: {self.ctrl.firmware} | {self.ctrl.axis_count} 軸"
                + (f" | DRDIV {_drdiv_txt}" if _drdiv_txt else "")
            )
            # 軸機械校正卡片的「韌體 DRDIV 參考」欄：純資訊性顯示，
            # 跟 division 輸入框不互相驗證（見卡片建構處的說明）。
            for ax, var in self._calib_drdiv_vars.items():
                var.set(self.ctrl.axis_drdiv.get(ax, "—"))
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
            # 尋光分頁的搜尋軸勾選框比照同一套 axis_count 判斷 enable/disable；
            # matplotlib 未安裝時尋光分頁沒有建立這些 widget，用 getattr 保護。
            scan_checkbuttons = getattr(self, "_scan_axis_checkbuttons", None)
            if scan_checkbuttons is not None:
                for ax in AXES:
                    enabled = int(AXIS_NO[ax]) <= self.ctrl.axis_count
                    scan_checkbuttons[ax].config(state="normal" if enabled else "disabled")
                    if not enabled:
                        self._scan_axis_selected_vars[ax].set(False)
                self._refresh_scan_entry_states()
            # 原點復歸重現性量測分頁的量測軸勾選框，比照同一套邏輯。
            org_repeat_checkbuttons = getattr(self, "_org_repeat_axis_checkbuttons", None)
            if org_repeat_checkbuttons is not None:
                for ax in AXES:
                    enabled = int(AXIS_NO[ax]) <= self.ctrl.axis_count
                    org_repeat_checkbuttons[ax].config(state="normal" if enabled else "disabled")
                    if not enabled:
                        self._org_repeat_axis_vars[ax].set(False)
            # 控制器設定是 RAM-only，斷電就沒了。連線時已嘗試從設定檔補回，
            # 這裡把結果告訴使用者：補了什麼、還缺什麼。
            # 復歸樣式下拉要填當前軸的實際值（設定檔還原之後才讀才準）
            self._sync_org_mode()
            restored = self.ctrl.config_restored
            unset = self.ctrl.homing_unconfigured
            # 韌體軟體限位（CWSLP?/CCWSLP?）連線時已同步進 ctrl.sw_limits
            # （見 ds102_ctrl.sync_sw_limits_from_controller）。先刷新
            # 〈軟體行程限制〉卡片的「目前生效」欄，再把結果告訴使用者：
            # 這治不好「預設沒保護」的病根（韌體限位出廠即停用），只是
            # 讓兩層限位彼此同步、把無保護狀態從不可見變成可見。
            self._refresh_sw_limit_display()
            sw_synced = self.ctrl.sw_limits_synced
            sw_unprotected = self.ctrl.sw_limits_unprotected
            # 「沒有任何行程保護」是這幾則橫幅裡唯一跟立即安全有關的一則
            # ——_flash_banner 是序列播放、不重疊，排最後要等其他幾則播完
            # 才會出現，這段期間使用者已經可以點動了。因此刻意排在最前面，
            # 讓最重要的警示最先被看到（architect 審查抓到的排序問題）。
            if sw_unprotected:
                self._flash_banner(
                    f"⚠ 軸 {'、'.join(sw_unprotected)} 沒有任何行程保護"
                    f"（韌體限位停用、程式端未設定），長按點動只靠機械限位擋",
                    14000,
                )
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
            if sw_synced:
                self._flash_banner(
                    "✔ 已從韌體限位同步程式端行程限制：" + "、".join(sw_synced),
                    10000,
                )
        else:
            self._conn_btn.config(text="連線", bg=CLR_ACCENT)
            messagebox.showerror("連線失敗", msg)

    # =========================================================================
    # 光功率計連線（HP 8153A / GPIB，獨立於 DS102 連線，互不影響按鈕啟用邏輯）
    # =========================================================================
    def _toggle_meter_connect(self):
        if self.meter is not None:
            self._disconnect_meter()
            return
        try:
            addr = int(self._pm_gpib_addr_var.get())
            ch = int(self._pm_channel_var.get())
            wl = int(self._pm_wavelength_var.get())
        except ValueError:
            messagebox.showerror("設定錯誤", "GPIB 位址／Channel／波長必須是整數")
            return
        self._pm_conn_btn.config(text="連線中...", state="disabled", bg=CLR_WARN)
        self._pm_set_inputs_state("disabled")

        def _do():
            try:
                m = HP8153APowerMeter(gpib_address=addr, channel=ch, wavelength_nm=wl)
                err = None
            except Exception as e:
                m, err = None, str(e)
            self.root.after(0, lambda: self._on_meter_connect_result(m, err, addr, ch, wl))

        threading.Thread(target=_do, daemon=True).start()

    def _on_meter_connect_result(self, m, err, addr, ch, wl):
        if m is None:
            messagebox.showerror("連線失敗", f"無法連線 HP 8153A：{err}")
            self._pm_conn_btn.config(text="連線", state="normal", bg=CLR_ACCENT)
            self._pm_set_inputs_state("normal")
            return
        self.meter = m
        self._pm_conn_btn.config(text="中斷", state="normal", bg=CLR_DANGER)
        self._pm_conn_dot.itemconfig(self._pm_conn_dot_id, fill=CLR_ACCENT)
        self._pm_conn_lbl.config(text="已連線")
        self._pm_status_var.set("已連線")
        self._pm_status_lbl.config(fg=CLR_ACCENT)
        self._pm_set_status_dot(CLR_ACCENT)
        self._pm_comm_failures = 0
        self._pm_clear_reading()
        self._pm_power_var.set("—")
        self._pm_unit_var.set("")
        self._pm_power_lbl.config(fg=CLR_MUTED)
        self._pm_ch_wl_var.set(f"Ch{ch}·{wl}nm")
        self._pm_set_widgets_state("connected")
        _save_meter_config({"gpib_address": addr, "channel": ch, "wavelength_nm": wl}, log=self.ctrl._log)
        self.ctrl._log("INFO", f"HP 8153A 已連線：GPIB{addr}, Ch{ch}, {wl}nm")

    def _disconnect_meter(self):
        self._pm_auto_poll.set(False)
        if self.meter is not None:
            try:
                self.meter.close()
            except Exception as e:
                self.ctrl._log("ERROR", f"HP8153A close 失敗: {e}")
            self.meter = None
        self._pm_comm_failures = 0
        self._pm_clear_reading()
        self._pm_conn_btn.config(text="連線", state="normal", bg=CLR_ACCENT)
        self._pm_conn_dot.itemconfig(self._pm_conn_dot_id, fill=CLR_DANGER)
        self._pm_conn_lbl.config(text="未連線")
        self._pm_set_inputs_state("normal")
        self._pm_status_var.set("未連線")
        self._pm_status_lbl.config(fg=CLR_MUTED)
        self._pm_set_status_dot(CLR_MUTED)
        self._pm_power_var.set("—")
        self._pm_unit_var.set("")
        self._pm_power_lbl.config(fg=CLR_MUTED)
        self._pm_age_var.set("—")
        self._pm_ch_wl_var.set("")
        self._pm_set_widgets_state("disconnected")

    def _pm_set_status_dot(self, color):
        """
        同步狀態燈顏色到主分頁與浮動視窗（若開著）。
        兩個 Canvas 各自持有自己的 item id，浮動視窗若未開啟則 self._pm_float_win
        為 None，直接略過；TclError 則是視窗剛好在這次呼叫與下次存取之間被
        使用者關掉的競態，同樣忽略即可。
        """
        self._pm_status_dot.itemconfig(self._pm_status_dot_id, fill=color)
        if self._pm_float_win is not None:
            try:
                self._pm_status_dot_float.itemconfig(self._pm_status_dot_float_id, fill=color)
            except tk.TclError:
                pass

    @staticmethod
    def _pm_float_position(root_x, root_y, root_w, root_h,
                           screen_w, screen_h, win_w, win_h, margin=10):
        """
        算出光功率浮動視窗要開在哪裡，回傳 (x, y)。

        🔴 **主視窗預設是最大化開機**（`_build_window()` 的
        `state("zoomed")`），此時「主視窗右緣 + 10」必定落在螢幕外，
        視窗會被建立在看不到的地方——2026-08-26 使用者回報「全螢幕下
        會消失」即此。原本那段程式的註解還寫著「簡單且不會跑到螢幕外」，
        正好寫反：它只在主視窗沒有佔滿螢幕寬度時才成立。

        規則：優先放在主視窗右緣外側（不擋操作區）；只要那個位置會超出
        螢幕右緣，就退回主視窗**內側**右上角。內側位置只要主視窗本身看得
        到就一定看得到，所以多螢幕情境下最差也只是保守地放進主視窗裡，
        不會消失。最後再把 x／y 夾回螢幕範圍，避免主視窗被拖到負座標
        或螢幕外時把浮動視窗一起帶出去。

        寫成不碰 tkinter 的純函式（引數全部由呼叫端量測後傳入），是為了
        能直接用假數值驗證各種螢幕／視窗尺寸組合，不必真的把測試視窗
        最大化——見 verify_meter_panel.py::TestPmFloatPosition。
        """
        x = root_x + root_w + margin
        if x + win_w > screen_w:
            # 放不下 → 改放主視窗內側右上角
            x = root_x + root_w - win_w - margin
        y = root_y + margin

        # 夾回螢幕範圍（左上優先，寧可蓋住一點也不要看不見）
        x = max(0, min(x, screen_w - win_w))
        y = max(0, min(y, screen_h - win_h))
        return x, y

    def _toggle_pm_float_window(self):
        """
        開關獨立光功率浮動視窗（供移動控制／光功率兩分頁的核取方塊共用）。

        ⚠ ttk.Checkbutton 是「先翻轉 variable，再呼叫 command」，所以本函式
        必須依 self._pm_float_open 的**新值**決定要開還是要關。原本的寫法
        無條件當成「開」，取消勾選時只會 lift() 而不會關閉視窗，核取方塊
        與視窗狀態從此永久不同步（勾選框顯示未勾、視窗卻還在，之後每次點
        擊都只是把它拉到最上層，視窗再也關不掉）——2026-08-26 使用者回報
        「浮動視窗失效」即此。兩個分頁共用同一個 BooleanVar，所以任一邊
        desync 之後另一邊也跟著失效。
        """
        if not self._pm_float_open.get():
            self._close_pm_float_window()
            return

        if self._pm_float_win is not None and self._pm_float_win.winfo_exists():
            self._pm_float_win.lift()
            self._pm_float_win.focus_force()
            return

        win = tk.Toplevel(self.root)
        win.title("光功率")
        win.configure(bg=CLR_CARD)
        win.resizable(False, False)
        win.transient(self.root)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self._close_pm_float_window)

        # 定位在主視窗右上角外側，避免蓋住主視窗操作區；放不下就改放到
        # 主視窗**內側**右上角（見 _pm_float_position() 的成因說明）。
        # 不記憶上次位置——每次開啟都重新算一次。
        self.root.update_idletasks()
        x, y = self._pm_float_position(
            self.root.winfo_x(), self.root.winfo_y(),
            self.root.winfo_width(), self.root.winfo_height(),
            self.root.winfo_screenwidth(), self.root.winfo_screenheight(),
            PM_FLOAT_W, PM_FLOAT_H,
        )
        win.geometry(f"{PM_FLOAT_W}x{PM_FLOAT_H}+{x}+{y}")

        # --- 狀態列（燈 + 文字 / channel·波長）---
        status_row = tk.Frame(win, bg=CLR_CARD)
        status_row.pack(fill="x", padx=10, pady=(10, 4))
        self._pm_status_dot_float = tk.Canvas(
            status_row, width=10, height=10, bg=CLR_CARD, highlightthickness=0
        )
        self._pm_status_dot_float.pack(side="left")
        self._pm_status_dot_float_id = self._pm_status_dot_float.create_oval(
            1, 1, 9, 9,
            fill=self._pm_status_dot.itemcget(self._pm_status_dot_id, "fill"),
            outline="",
        )
        tk.Label(
            status_row, textvariable=self._pm_status_var, bg=CLR_CARD, fg=CLR_TEXT,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(4, 0))
        tk.Label(
            status_row, textvariable=self._pm_ch_wl_var, bg=CLR_CARD, fg=CLR_MUTED,
            font=("Segoe UI", 9),
        ).pack(side="right")

        # --- 大字數值 ---
        value_row = tk.Frame(win, bg=CLR_CARD)
        value_row.pack(expand=True)
        tk.Label(
            value_row, textvariable=self._pm_power_var, bg=CLR_CARD, fg=CLR_TEXT,
            font=("Consolas", 46, "bold"),
        ).pack(side="left")
        tk.Label(
            value_row, textvariable=self._pm_unit_var, bg=CLR_CARD, fg=CLR_MUTED,
            font=("Segoe UI", 14),
        ).pack(side="left", padx=(4, 0), anchor="s", pady=(0, 8))

        # --- 資料年齡 ---
        tk.Label(
            win, text="最後更新：", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9),
        ).pack()
        tk.Label(
            win, textvariable=self._pm_age_var, bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9),
        ).pack(pady=(0, 8))

        # --- 操作列 ---
        op_row = tk.Frame(win, bg=CLR_CARD)
        op_row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Checkbutton(
            op_row, text="自動輪詢", variable=self._pm_auto_poll,
            command=self._pm_sync_poll_interval_state,
        ).pack(side="left")
        ttk.Button(
            op_row, text="立即查詢", style="Info.TButton", command=self._query_power_once,
        ).pack(side="right")

        self._pm_float_win = win
        self._pm_float_open.set(True)

    def _close_pm_float_window(self):
        self._pm_float_open.set(False)
        if self._pm_float_win is not None:
            try:
                self._pm_float_win.destroy()
            except tk.TclError:
                pass
        self._pm_float_win = None

    def _pm_set_inputs_state(self, state: str):
        """連線參數輸入框（位址／channel／波長）的啟用狀態。"""
        for w in (self._pm_addr_entry, self._pm_ch_cb, self._pm_wl_cb):
            try:
                w.config(state=state)
            except tk.TclError:
                pass

    def _pm_set_widgets_state(self, phase: str):
        """
        同步「立即查詢／自動輪詢／輪詢間隔／量程」這幾類元件的啟用狀態。

        phase: "disconnected" / "connected"（"connecting" 由呼叫端各自處理
        連線按鈕本身，不影響這裡管的其他元件——它們在連線中與未連線時
        狀態相同，都是 disabled）。
        """
        connected = phase == "connected"
        state = "normal" if connected else "disabled"
        try:
            self._pm_query_btn.config(state=state)
        except tk.TclError:
            pass
        try:
            self._pm_auto_poll_cb.config(state=state)
        except tk.TclError:
            pass
        if not connected:
            self._pm_auto_poll.set(False)
        self._pm_sync_poll_interval_state()
        for w in (self._pm_range_auto_rb, self._pm_range_manual_rb, self._pm_apply_range_btn):
            try:
                w.config(state=state)
            except tk.TclError:
                pass
        self._pm_sync_range_entry_state()

    def _pm_sync_scan_notice(self):
        """
        依 ctrl.scanning_active 同步光功率分頁的狀態提示列與操作按鈕。

        沒有另開輪詢——搭 _start_poller 既有的 100ms 節奏（2026-08-20 從
        _redraw_scan_plot 搬過來，原本掛在那條鏈上時，沒裝 matplotlib
        的環境整條迴圈不會執行，會讓這裡永遠不同步；_start_poller 不做
        任何 I/O 且一定會跑，是更合適的節奏來源），加上 _on_scan_done
        收尾時額外呼叫一次，讓收回不必等到下一輪節奏。
        """
        scanning = self.ctrl.scanning_active
        notice = self._pm_scan_notice
        # ⚠ 用 winfo_manager() 而非 winfo_ismapped() 判斷是否已顯示：使用者
        # 尋光期間通常留在「尋光」分頁，這代表「光功率」分頁十之八九不是
        # 當下選取的分頁，而 winfo_ismapped() 只反映目前實際畫在螢幕上，
        # 未選取分頁底下的元件永遠回傳 False。用它當守衛時，尋光結束但
        # 使用者不在「光功率」分頁會讓還原邏輯整段被誤判跳過，畫面卡在
        # 「尋光中」直到下次尋光剛好切到這個分頁才被動修正。winfo_manager()
        # 只反映 pack()/pack_forget() 呼叫過沒有，不受分頁選取影響。
        mapped = notice.winfo_manager() != ""
        if scanning and not mapped:
            notice.pack(fill="x", padx=8, pady=(4, 0), before=self._pm_num_row)
            # 尋光進行中不能讓使用者手動觸發查詢/量程變更跟尋光搶 GPIB——
            # 直接鎖死這三顆，不透過 _pm_set_widgets_state（它只認
            # connected/disconnected 兩態，不知道「已連線但尋光中」這個
            # 第三態）。
            for w in (self._pm_query_btn, self._pm_auto_poll_cb, self._pm_apply_range_btn):
                try:
                    w.config(state="disabled")
                except tk.TclError:
                    pass
        elif not scanning and mapped:
            notice.pack_forget()
            # 還原成「目前連線狀態」該有的 enable/disable，不是無條件
            # normal——尋光中途若 self.meter 被設回 None（使用者按了
            # 「中斷」），這三顆本來就該維持 disabled。
            self._pm_set_widgets_state("connected" if self.meter is not None else "disconnected")
            # _scan_plot_extend 尋光期間把 _pm_status_var 改成「尋光中…」，
            # 收尾後要還原成正常狀態文字，否則會卡在「尋光中」字樣不放，
            # 使用者看畫面會誤以為尋光還沒真的結束。還原邏輯交給
            # _pm_refresh_status_line()（唯一寫入者），不再自己判斷。
            self._pm_refresh_status_line()

    def _pm_sync_poll_interval_state(self):
        """輪詢間隔輸入框：僅當已連線且勾選自動輪詢時才 enabled。"""
        enabled = self.meter is not None and self._pm_auto_poll.get()
        try:
            self._pm_interval_entry.config(state="normal" if enabled else "disabled")
        except tk.TclError:
            pass

    def _pm_sync_range_entry_state(self):
        """手動量程輸入框：僅當已連線且選中「手動」時才 enabled。"""
        enabled = self.meter is not None and self._pm_range_mode_var.get() == "manual"
        try:
            self._pm_range_manual_entry.config(state="normal" if enabled else "disabled")
        except tk.TclError:
            pass

    def _query_power_once(self):
        if self.meter is None:
            return
        self._pm_query_btn.config(text="查詢中...", state="disabled")

        def _do():
            try:
                ok, val = self.meter.get_power()
            except Exception:
                ok, val = False, 0.0
            self.root.after(0, lambda: self._on_meter_reading(ok, val))

        threading.Thread(target=_do, daemon=True).start()

    def _on_meter_reading(self, ok: bool, val: float):
        if self.meter is None:
            # 已經按過「中斷」，這是斷線前卡在 GPIB 忙碌裡、事後才回來的
            # 舊查詢結果——不能再更新畫面，否則會在使用者已經看到「未連線」
            # 之後又跳出一則「已停止更新」橫幅，誤導成剛才的操作出了問題。
            return
        self._pm_query_btn.config(text="立即查詢", state="normal")
        if ok:
            if self._pm_comm_failures >= COMM_FAIL_THRESHOLD:
                self.ctrl._log("INFO", "光功率讀取已恢復正常")
                self._pm_set_status_dot(CLR_ACCENT)
            self._pm_comm_failures = 0
            # 先歸零 _pm_comm_failures 再記錄讀值：_pm_note_reading() 內部
            # 會呼叫 _pm_refresh_status_line()，而那支的第二順位分支就是看
            # _pm_comm_failures，順序反過來會讓恢復當下的那一輪仍顯示
            # 「⚠ 已停止更新」，多等一輪 100ms 才校正回來。
            self._pm_note_reading(val, source="meter")
        else:
            self._pm_comm_failures += 1
            if self._pm_comm_failures == COMM_FAIL_THRESHOLD:
                detail = self.meter.last_error_detail if self.meter is not None else ""
                self.ctrl._log("WARN", f"連續讀不到光功率，畫面數值已不可信：{detail}")
                banner = "⚠ 光功率讀值已停止更新，請檢查 GPIB 連線"
                if detail:
                    banner += f"（{detail}）"
                self._flash_banner(banner)
                self._pm_set_status_dot(CLR_DANGER)
                self._pm_power_var.set("—")
                self._pm_unit_var.set("")
        # 文字與前景色統一交給 _pm_refresh_status_line()（唯一寫入者），
        # 這裡只需確保 _pm_comm_failures 已更新到最新值再呼叫；即使不主動
        # 呼叫，_start_poller 的 100ms 節奏最終也會校正，這裡呼叫純粹是
        # 讓事件當下就反映，不必多等一輪。
        self._pm_refresh_status_line()
        self._pm_update_age_label()

    def _get_last_pm_value(self) -> Optional[float]:
        """
        供 DS102Controller.set_power_reader 使用：回傳最近一次成功讀值的快取。

        刻意不在這裡呼叫 self.meter.get_power()——這個方法會被
        _record_data_point() 從移動等待迴圈裡呼叫，觸發新的 GPIB 查詢會
        把單次約 110~130ms 的 I/O 延遲疊加進軸的到位判斷。連續失敗達
        COMM_FAIL_THRESHOLD（畫面已顯示「已停止更新」）時視為不可信，
        回傳 None 讓 CSV 該欄留空，而不是寫入一個過期的舊數值。

        🔴 **這是 `self._pm_reading` 唯一的跨執行緒讀取點**（跑在移動執行緒
        上，其餘讀寫全在 Tk 主執行緒）。`PowerReading` 是 frozen 的，所以
        這裡拿到的必定是某一次讀值的完整快照，不會出現「新的 value 配舊的
        ok_time」——但沒有鎖，也不需要鎖，理由見 PowerReading 的 docstring。
        """
        if self.meter is None or self._pm_comm_failures >= COMM_FAIL_THRESHOLD:
            return None
        return self._pm_reading.value

    def _scanner_power_query(self) -> Tuple[bool, float]:
        """
        每次呼叫都重新讀 self.meter（連線後物件可能被整個換掉，不可快取
        bound method）。這裡的 try/except 防的是 self.meter 在讀取瞬間與
        呼叫 get_power() 之間被另一執行緒設成 None 的競態（例如尋光進行中
        使用者手動按了光功率計的「中斷」），以及 get_power() 內部只接住
        VisaIOError/ValueError、未涵蓋的其他例外類型。
        """
        meter = self.meter
        if meter is None:
            return False, 0.0
        try:
            return meter.get_power()
        except Exception as e:
            logger.debug(f"尋光讀取光功率例外: {e}")
            return False, 0.0

    def _restore_meter_auto_range(self):
        """
        尋光結束後把光功率計還原成自動量程。

        🔴 少了這一步，「尋光在無光位置沒反應」這個 bug 會以更隱蔽的形式
        復發：上一輪尋光鎖定的量程會留到下一輪，而下一輪的起點常常又是
        無光的——固定量程在那裡必定 underrange，回傳 sentinel，演算法又
        變成一步都不動。鎖定量程是**單次搜尋內**的最佳化，不該跨輪存活。

        不論這一輪是完成、中止還是出錯都要還原，所以掛在 _on_scan_done
        的最前面（那是所有結束路徑的唯一匯流點）。
        """
        meter = self.meter
        if meter is None or getattr(meter, "range_auto", True):
            return
        try:
            meter.set_range_auto(True)
            self.ctrl._log("INFO", "[尋光] 已將光功率計還原為自動量程")
        except Exception as e:
            logger.debug(f"還原自動量程失敗: {e}")

    def _on_scan_signal_found(self, power: float):
        """
        尋光第一次確認「真的量到訊號」時由演算法呼叫（背景執行緒）。

        用途是把光功率計從自動量程切成鎖定量程：自動量程每次讀值都要讓
        儀器自己找檔位，尋光動輒上百次量測，省下來的時間相當可觀。

        🔴 這是**樂觀最佳化，不是必要步驟**。鎖定之後功率會從底噪一路爬
        到耦合峰值，可能跨數十 dB 而超出鎖定檔位——那時由
        meter_GPIB.get_power() 自己偵測 sentinel、切回自動量程並重讀
        （見該檔的自動退回邏輯）。**沒有那道保護就絕對不可以在這裡鎖**，
        否則演算法會在接近峰值時突然拿到一連串無效讀值，剛好在最需要
        精度的地方失明。

        跑在 scanner 的背景執行緒上，所以：不碰任何 tkinter widget（狀態
        文字一律 root.after 回主執行緒），meter 本身有自己的 RLock，而且
        尋光期間 _pm_should_poll() 因 scanning_active 為真而暫停背景輪詢，
        這條執行緒是當下唯一的 GPIB 使用者。
        """
        meter = self.meter
        if meter is None or not getattr(meter, "range_auto", False):
            return  # 已經是鎖定量程，或光功率計中途被斷開，都不必動
        try:
            meter.set_range_auto(False)
        except Exception as e:
            # 鎖不成只是少了一項最佳化，不可拖垮搜尋（比照 _scanner_power_query
            # 的既有寫法：尋光路徑上的例外一律收斂成記錄後繼續）。
            logger.debug(f"尋光鎖定量程失敗（不影響搜尋）: {e}")
            return
        self.ctrl._log("INFO", f"[尋光] 已量到訊號 {power:.4f} dBm，光功率計改為鎖定量程以加快讀值")

    def _pm_note_reading(self, value: float, source: str) -> None:
        """
        記錄一筆成功的光功率讀值——`self._pm_reading` 與數值顯示的唯一寫入者。

        兩個資料來源共用這一個入口：
          source="meter"  光功率分頁自己的「立即查詢」與背景輪詢（`_on_meter_reading`）
          source="scan"   尋光分頁的 sample callback 轉貼（`_scan_plot_extend`）

        🔴 **這個方法存在的目的就是讓「尋光」分頁不必知道光功率分頁有哪些
        欄位與 widget。** 尋光期間背景輪詢因 `_pm_should_poll()` 的
        `motion_active` 判斷而暫停，光功率分頁沒有其他資料來源，所以尋光
        必須把讀值轉貼過來——但轉貼的方式應該是呼叫一個具名方法，不是伸手
        進另一個分頁改五個欄位。

        刻意**不碰** `_pm_comm_failures`：那是背景輪詢自己的失聯計數。尋光
        期間它本來就沒在跑，被這裡累加或歸零都會污染尋光結束後的失聯判斷
        （這是搬進來之前 `_scan_plot_extend` 就已經寫明的既有約定，不是新
        規則）。文字說明與前景色一律交給 `_pm_refresh_status_line()`（它
        自己是那一組的唯一寫入者），這裡只負責數值與狀態燈。
        """
        self._pm_reading = PowerReading(value=value, ok_time=time.time(), source=source)
        self._pm_power_var.set(f"{value:.2f}")
        self._pm_unit_var.set("dBm")
        # 讀到值就代表通訊正常。原本只有「從失聯狀態恢復」時才改燈號，但
        # 可達狀態只有 ACCENT／DANGER／MUTED 三種，成功讀值時燈號本來就
        # 只可能是 ACCENT（正常）或剛從 DANGER 恢復，無條件設 ACCENT 與
        # 原行為等價，且少一個「忘了恢復燈號」的分支。
        self._pm_set_status_dot(CLR_ACCENT)
        self._pm_refresh_status_line()
        self._pm_update_age_label()

    def _pm_clear_reading(self) -> None:
        """
        清掉快取，回到「沒有任何可信讀值」的初始狀態。

        連線成功與中斷連線兩條路徑共用。⚠ **這裡順帶修掉一處不對稱**：
        原本 `_on_meter_connect_result` 只把 `_pm_last_ok_time` 歸零、
        沒有清 `_pm_last_value`，而 `_disconnect_meter` 只清 `_pm_last_value`、
        沒有歸零 `_pm_last_ok_time`。前者的後果是「連上新的光功率計、還沒
        讀到第一筆值之前，`_get_last_pm_value()` 會回傳上一個 session 的舊
        數值」，那個值會直接寫進 data/*.csv 的 dbm 欄位而沒有任何標記。
        實務上很難踩到（UI 上連線鍵是 toggle，連線前必定經過
        `_disconnect_meter`），但既然兩條路徑本來就該表達同一件事，就沒有
        理由讓它們各清一半。

        不碰 `_pm_power_var` / `_pm_unit_var` / 燈號：那兩條路徑對這些
        widget 的處理各不相同（連線成功要顯示「—」＋MUTED、中斷還要改連線
        鈕與整批 widget 狀態），維持各自處理，這裡只管快取本身。
        """
        self._pm_reading = PowerReading()

    def _pm_update_age_label(self):
        if self.meter is None or self._pm_reading.ok_time <= 0:
            self._pm_age_var.set("—")
            return
        age = time.time() - self._pm_reading.ok_time
        self._pm_age_var.set("剛更新" if age < 1.5 else f"{age:.0f}s 前")

    def _pm_refresh_status_line(self):
        """
        `_pm_status_var` / `_pm_status_lbl` / `_pm_power_lbl` 前景色的
        唯一寫入者。優先序：未連線 > 通訊失敗 > 尋光中 > 移動中 > 正常。

        掛在 `_start_poller`（100ms、不做 I/O、一定會跑）而非
        `_redraw_scan_plot`（250ms，僅在裝了 matplotlib 時才會執行——
        掛在那條鏈上會讓「移動中」這個新狀態在沒裝 matplotlib 的環境下
        完全失效）。`_on_meter_reading`／`_pm_sync_scan_notice` 也會在
        各自的事件當下呼叫一次，讓畫面立即反映，不必多等一輪 100ms。

        只管「文字說明＋前景色」這組顯示狀態，不碰 `_pm_power_var`／
        `_pm_unit_var`（實際數值文字）——那些各自的資料來源（背景輪詢／
        尋光 sample callback）本來就知道該顯示什麼數字，不屬於這裡。
        """
        if self.meter is None:
            self._pm_status_var.set("未連線")
            self._pm_status_lbl.config(fg=CLR_MUTED)
            self._pm_power_lbl.config(fg=CLR_MUTED)
            return
        if self._pm_comm_failures >= COMM_FAIL_THRESHOLD:
            detail = self.meter.last_error_detail if self.meter is not None else ""
            self._pm_status_var.set(
                f"⚠ 已停止更新（{detail}）" if detail else "⚠ 已停止更新"
            )
            self._pm_status_lbl.config(fg=CLR_DANGER)
            self._pm_power_lbl.config(fg=CLR_DANGER)
            return
        if self.ctrl.scanning_active:
            self._pm_status_var.set("尋光中（讀值由尋光分頁提供）")
            self._pm_status_lbl.config(fg=CLR_ACCENT)
            self._pm_power_lbl.config(fg=CLR_TEXT)
            return
        if self.ctrl.motion_active:
            # 移動中：數值本身不清空（清成「—」會誤導成斷線），只改
            # label 文字跟顏色——不用 pack/pack_forget 切換，那是
            # _pm_scan_notice 給長狀態用的模式，移動是次秒級高頻切換，
            # 用那招會讓版面一直跳動（見 CLAUDE.md〈第四批修正〉）。
            self._pm_status_var.set("⏸ 滑台移動中，暫停讀取")
            self._pm_status_lbl.config(fg=CLR_MUTED)
            self._pm_power_lbl.config(fg=CLR_MUTED)
            return
        self._pm_status_var.set("已連線")
        self._pm_status_lbl.config(fg=CLR_ACCENT)
        self._pm_power_lbl.config(fg=CLR_TEXT)

    def _pm_should_poll(self) -> bool:
        """
        光功率背景輪詢這一輪要不要真的送出 GPIB 查詢。

        `motion_active` 涵蓋點動/步進/原點復歸/重播/尋光——序列埠移動中
        GPIB 讀值會混進馬達震動雜訊，且會悄悄流進 data/*.csv 沒有任何
        標記。這裡整批擋下，不逐一列舉個別旗標（scanning_active 已經是
        motion_active 的一部分，不必重複檢查）。
        """
        return (
            self.meter is not None
            and self._pm_auto_poll.get()
            and not self.ctrl.motion_active
        )

    def _start_meter_poll_worker(self):
        """
        光功率背景輪詢，獨立執行緒——不塞進既有四條輪詢迴圈任何一條。

        絕不可用 root.after 排下一輪查詢：那會把阻塞式 GPIB I/O 搬回
        Tk 主執行緒造成凍結，跟既有四條輪詢迴圈的鐵律相同。
        """
        def _worker():
            while not self._shutting_down.is_set():
                if self._pm_should_poll():
                    try:
                        ok, val = self.meter.get_power()
                    except Exception as e:
                        ok, val = False, 0.0
                        logger.debug(f"背景光功率讀取失敗: {e}")
                    self.root.after(0, lambda o=ok, v=val: self._on_meter_reading(o, v))
                try:
                    interval = float(self._pm_poll_interval.get())
                except ValueError:
                    interval = METER_POLL_INTERVAL
                self._shutting_down.wait(max(interval, 0.1))

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_meter_range(self):
        if self.meter is None:
            return
        manual = self._pm_range_mode_var.get() == "manual"
        dbm = None
        if manual:
            try:
                dbm = float(self._pm_range_manual_var.get())
            except ValueError:
                messagebox.showerror("格式錯誤", "手動量程必須是數字（dBm）")
                return

        self._pm_apply_range_btn.config(text="套用中...", state="disabled")

        def _do():
            try:
                if manual:
                    self.meter.set_range_auto(False)
                    self.meter.set_range(dbm)
                else:
                    self.meter.set_range_auto(True)
                err = None
            except Exception as e:
                self.ctrl._log("ERROR", f"設定量程失敗: {e}")
                err = str(e)
            self.root.after(0, lambda: self._on_meter_range_result(err))

        threading.Thread(target=_do, daemon=True).start()

    def _on_meter_range_result(self, err):
        self._pm_apply_range_btn.config(
            text="套用量程", state="normal" if self.meter is not None else "disabled"
        )
        if err is not None:
            messagebox.showerror("設定失敗", f"無法套用量程設定：{err}")

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
        if state == "disabled":
            # 斷線時尋光分頁的六個搜尋軸勾選框比照軸選擇按鈕組一併關閉——
            # 只在關閉方向連動：重新 enable 要靠 _on_connect_result 依
            # axis_count 個別判斷，不能整批 normal（那會把偵測不到的軸也
            # 開放勾選）。matplotlib 未安裝時用 getattr 保護。
            for ax in AXES:
                cb = getattr(self, "_scan_axis_checkbuttons", {}).get(ax)
                if cb is not None:
                    cb.config(state="disabled")
            self._refresh_scan_entry_states()
            # 原點復歸重現性量測的量測軸勾選框比照同一套邏輯——只在關閉
            # 方向連動，重新 enable 一樣要靠 _on_connect_result 依
            # axis_count 個別判斷。
            for ax in AXES:
                cb2 = getattr(self, "_org_repeat_axis_checkbuttons", {}).get(ax)
                if cb2 is not None:
                    cb2.config(state="disabled")

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
                # except-as 變數在區塊結束就會被 Python 自動 unbind，
                # 下面的 lambda 是透過 root.after 延後執行的閉包，直接
                # 引用 e 會在真正執行時撞上 NameError（蓋掉原始例外訊息）
                err_msg = f"復歸過程發生例外: {e}"
                self.root.after(0, lambda: _done(False, err_msg))
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
        # 現在改由長時間作業註冊表供應：新增作業只要在 _register_long_ops()
        # 註冊一筆，這裡自動涵蓋，不需要（也不應該）再手動加旗標。
        if self._any_long_op_running():
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

            pos_val = pos_work.get(ax, 0.0)
            txt = f"{pos_val:,.0f}"
            um = self.ctrl.estimate_um(ax, pos_val)
            if um is not None:
                txt += f" ≈ {um:,.1f} μm"
            var.set(txt)
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

        # 移動控制分頁的「Position:」以前只由 _poll_status()／_async_query()
        # 寫入，疊起來造成「ORG 時分頁座標與 StatusBar 座標對不起來」：
        #   1. _poll_status() 只在 status == "Driving" 時續輪，復歸途中的
        #      「Detect origin」與壓限位（樣式 5/6）都不是 Driving，數字
        #      停在中途值不再更新。
        #   2. 全軸原點復歸（_do_home_all → origin_all）根本沒觸發過這兩條
        #      路徑，整趟復歸這顆數字完全不動，收尾強制寫 POS 0 時才突然
        #      跳到 0，跟 StatusBar 差距最刺眼。
        #   3. 它們寫的是 query_status() 的機械座標（未扣 offset），而
        #      StatusBar／儀表板顯示工作座標，設過工作原點後兩者永遠差一
        #      個 offset，跟有沒有在復歸無關。
        # 統一改由這條 100ms 重繪迴圈供應，與其餘座標顯示同一份快取、同一
        # 節奏；移動中的高頻更新不受影響，query_status() 仍寫 _positions_pulse，
        # 只是不再自己畫。
        cur_pos_txt = "—"
        if connected and cur_ax and int(AXIS_NO[cur_ax]) <= n_axes:
            cur_pos_txt = f"{pos_work.get(cur_ax, 0.0):,.0f}"
        if self._ctrl_pos_var.get() != cur_pos_txt:
            # 只在值真的變了才 set：StringVar.set() 即使同值也會觸發 write
            # trace（_update_ctrl_pos_um），沒必要每 100ms 白算一次。
            self._ctrl_pos_var.set(cur_pos_txt)

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
                # 各長時間作業的經過時間顯示，統一由註冊表驅動
                # （見 _update_long_op_elapsed / _register_long_ops）。
                self._update_long_op_elapsed()
                # 這兩個都不做 I/O，純畫面同步。掛在這裡（而非
                # _redraw_scan_plot）是因為那條鏈只在裝了 matplotlib 時
                # 才會執行；這裡是唯一保證一定會跑的節奏。
                self._pm_refresh_status_line()
                self._pm_sync_scan_notice()
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
                # scanning_active 排除比照 playback_running 的既有模式：
                # 演算法的 hot loop 對移動+量測的時間預算很緊（0.5~1s 量級），
                # 這條背景輪詢若繼續搶 _serial_lock，會讓演算法自己的
                # move_step / _wait_axis_stop 排隊等候，白白吃掉預算。
                if (
                    self.ctrl.connected
                    and not self.ctrl.playback_running
                    and not self.ctrl.scanning_active
                    and not self.ctrl.measuring_active
                ):
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
        # 所有長時間背景作業都跑在自己的迴圈裡（不是靠 _shutting_down 這類
        # 現成旗標），關窗時要讓它們知道視窗正在關閉才會主動收工。少了這步，
        # 例如尋光的 _run() 會繼續跑 scanner.run()，下一步對已經 disconnect()
        # 的序列埠操作大機率拋例外，且 _run() 例外處理排的 root.after(0, ...)
        # 這時 root 可能已經 destroy()，會在背景執行緒炸出未接住的例外
        # （architect 審查抓到的問題）。
        self._request_stop_long_ops()
        if self.ctrl.connected:
            self.ctrl.stop()
            self.ctrl.disconnect()
        if self.meter is not None:
            try:
                self.meter.close()
            except Exception as e:
                self.ctrl._log("ERROR", f"HP8153A close 失敗（關窗流程）: {e}")
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
