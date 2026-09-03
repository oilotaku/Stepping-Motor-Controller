# -*- coding: utf-8 -*-
"""
pytest 共用工具與 fixture（verify_scan_tab.py / verify_meter_panel.py 共用）。

這兩支測試檔案原本是獨立可執行、用自訂 record()/check() 收集結果的回歸
腳本；2026-08 轉換為 pytest 測試檔，讓 VS Code Test Explorer 能個別發現、
個別重跑單一案例。這裡收斂兩份腳本共通的樣板：Tk 事件迴圈的等待方式、
建立/收拾一個以暫存目錄為 RECORDING_DIR 的 DS102GUI。

── 安全規則（兩支測試檔案的每一個測試都必須遵守，勿破壞）──
  - 不連真實硬體：一律用真的 DS102Controller() 實例（gui.ctrl.ser 全程
    維持 None，從未真正 connect() 過）；需要控制器行為時逐一 monkeypatch
    個別方法/屬性（scan_move_step / query_status / wait_axis_stop 等），
    不要整個替換掉 ctrl 物件本身。
  - 不寫入真實 recordings/：main_ai.RECORDING_DIR 一律透過 make_gui()
    導向 pytest 的 tmp_path / tmp_path_factory 產生的暫存目錄，絕不落在
    專案目錄內，也絕不使用手動 tempfile.mkdtemp()（那不會自動清理，
    也容易漏改到真正的 recordings/）。
  - 全程不使用 winfo_ismapped()（root.withdraw() 之後恆為 False，會產生
    假陽性）；一律用 widget.winfo_manager() != "" 判斷是否已 pack。
  - 任何會啟動背景執行緒的動作（_do_start_scan() 等）在測試/fixture
    結束前務必讓它自然結束或主動停止（呼叫對應的 stop，並用
    pump_until() 等旗標清除），避免殘留執行緒跨到下一個測試搶同一個
    gui/ctrl，或在 pytest 行程結束後仍卡在背景。

── Python 3.14 tkinter 的 mainloop 限制 ──
  main_ai.py 背景執行緒回主執行緒一律用 self.root.after(0, ...)，而
  Python 3.14 的 tkinter 要求背景執行緒呼叫 after()／存取 widget 前，
  本執行緒必須「正在跑 mainloop」——單純輪詢 root.update() 不算數
  （實測仍會拋 RuntimeError: main thread is not in main loop）。因此
  等待背景執行緒回呼完成一律使用 pump_until()，不要自己寫 update() 迴圈。

── VS Code Test Explorer 的編碼 ──
  VS Code 呼叫 pytest 時不保證套用 .vscode/settings.json 的
  terminal.integrated.env.windows（PYTHONUTF8/PYTHONIOENCODING），這裡
  在 conftest 收集階段就先補上，確保中文輸出不會因 cp950 主控台編碼
  UnicodeEncodeError（兩支原始腳本開頭本來就有同一招，這裡集中一份）。
"""

import os
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import main_ai  # noqa: E402  （需先設好上面的環境變數再 import，避免中文 log 亂碼）
import ds102_ctrl  # noqa: E402


def pump_until(root: tk.Tk, condition_fn, timeout: float = 15.0) -> None:
    """
    真正把 Tk 事件迴圈跑起來，直到 condition_fn() 為真或逾時。

    root.after(0, ...) 排的 callback（main_ai.py 背景執行緒回主執行緒的
    既有寫法）必須讓 mainloop() 真的轉起來才會被執行——Python 3.14 的
    tkinter 要求背景執行緒呼叫 after()／存取 widget 前，本執行緒必須
    正在跑 mainloop，光是 root.update() 迴圈不算數（實測仍會拋
    RuntimeError: main thread is not in main loop）。

    做法：排一個會自我重新排程的 watchdog，條件達成或逾時就呼叫
    root.quit()（只會結束 mainloop，不會 destroy），讓 mainloop() 這次
    呼叫回到呼叫端，測試可以繼續往下走。

    刻意設計成一個「純函式」而不是 pytest fixture：它常被 class-scope /
    module-scope 的 fixture 呼叫（例如「跑完一輪尋光」這種一次性的
    共用前置動作），而 pytest 的 fixture 有嚴格的作用域限制——scope
    較廣的 fixture 不能依賴 scope 較窄的 fixture（例如函式層級的
    fixture）。純函式沒有這個限制，直接 import 就能在任何作用域使用。
    """
    deadline = time.time() + timeout

    def poll():
        if condition_fn() or time.time() > deadline:
            root.quit()
        else:
            root.after(20, poll)

    root.after(20, poll)
    root.mainloop()


def _new_tk_root_with_retry(max_attempts: int = 3, retry_delay: float = 0.5) -> tk.Tk:
    """
    建立 tk.Tk()，遇到暫時性的 TclError 就重試。

    實測（2026-08）在同一個 process 內連續建構 tk.Tk() 執行個體時，偶發
    `_tkinter.TclError: couldn't read file ...treeview.tcl`（檔案其實
    存在，屬於快速連續建立/銷毀 Tcl 直譯器時的環境層級競爭——這台機器
    另外也有 SentinelOne 即時掃描的已知前科，見 DRIVER_ISSUE_REPORT.md，
    不無可能是同一類瞬時鎖檔）。即使把整份測試檔案的 Tk() 建構次數壓到
    最低（5 次左右），仍會偶爾中獎，重跑幾次就會通過，證實是暫時性
    問題而非程式邏輯錯誤。這裡直接在源頭重試，避免每個呼叫端都要各自
    處理。
    """
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return tk.Tk()
        except tk.TclError as exc:
            last_exc = exc
            if attempt < max_attempts:
                time.sleep(retry_delay)
    raise last_exc


def make_gui(recording_dir, extra_patches=()):
    """
    建立一個新的 (root, gui)，並把 RECORDING_DIR 導向指定的暫存目錄
    （絕不使用真實 recordings/）。extra_patches 可傳入額外的、尚未
    start() 的 unittest.mock._patch 物件——例如 verify_scan_tab.py 需要
    另外 patch fiber_scanner._default_scan_dir，避免 FiberAlignmentScanner
    的 persist_samples() 把樣本寫進專案的 recordings/scans/。

    🔴 同時 patch `main_ai.RECORDING_DIR` 與 `ds102_ctrl.RECORDING_DIR`
    ——這是 2026-08-19 補 verify_axis_calib.py 時實測抓到的真坑：
    `main_ai.py` 是用 `from ds102_ctrl import RECORDING_DIR` 重新引入，
    這只是另一個獨立綁定同一初始物件的名字，patch 其中一個完全不影響
    另一個。`DS102Controller` 的持久化方法（`save_point`／`set_axis_calib`
    ／`save_recording`／`capture_controller_config` 等）全部定義在
    `ds102_ctrl.py`，內部引用的是該模組自己的 `RECORDING_DIR`——只 patch
    `main_ai.RECORDING_DIR` 的話，這些方法完全不會被攔到，會直接寫進
    專案真正的 `recordings/`。`verify_scan_tab.py`／`verify_meter_panel.py`
    之前沒踩到純粹是因為沒呼叫到這些方法，不代表這層防護真的有效——
    跟 CLAUDE.md 記載「測試腳本清空過兩次 teaching points」是同一類風險，
    這裡直接在共用 fixture 補起來，往後任何新測試檔都不用重新踩一次。

    🔴 `DATA_DIR` 是同一類風險，2026-08-21 補上：`save_homing_repeat_result()`
    （原點復歸重現性量測結果存檔）與既有的 `_record_data_point()`（實驗
    數據 CSV）都寫模組層級的 `ds102_ctrl.DATA_DIR`，`main_ai.py` 一樣是
    `from ds102_ctrl import DATA_DIR` 重新引入同一個獨立綁定。只 patch
    `RECORDING_DIR` 完全攔不到這兩個方法，會直接寫進專案真正的 `data/`。

    回傳 (root, gui, patchers)；呼叫端負責在使用完後呼叫 close_gui()。
    """
    data_dir = Path(recording_dir) / "data"
    patchers = [
        patch.object(main_ai, "RECORDING_DIR", new=Path(recording_dir)),
        patch.object(ds102_ctrl, "RECORDING_DIR", new=Path(recording_dir)),
        patch.object(main_ai, "DATA_DIR", new=data_dir),
        patch.object(ds102_ctrl, "DATA_DIR", new=data_dir),
    ]
    patchers.extend(extra_patches)
    for p in patchers:
        p.start()
    root = _new_tk_root_with_retry()
    root.withdraw()
    gui = main_ai.DS102GUI(root)
    return root, gui, patchers


def close_gui(root, gui, patchers, use_on_close=False):
    """
    收拾 make_gui() 建立的 (root, gui, patchers)。

    use_on_close=True 時走 gui._on_close()（其內部本身就會呼叫
    root.destroy()），供驗證 _on_close() 行為本身的案例使用；否則只是
    單純把這個 gui/root 收乾淨，直接 set 收工旗標 + root.destroy()。
    """
    if use_on_close:
        try:
            gui._on_close()
        except tk.TclError:
            pass
    else:
        try:
            gui._shutting_down.set()
        except Exception:
            pass
        try:
            root.destroy()
        except tk.TclError:
            pass
    for p in patchers:
        try:
            p.stop()
        except RuntimeError:
            pass  # 已經 stop 過（例如例外路徑中途已 stop）
