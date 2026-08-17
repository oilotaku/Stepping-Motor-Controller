# -*- coding: utf-8 -*-
"""
尋光分頁（main_ai.DS102GUI 的「尋光」分頁 + fiber_scanner.FiberAlignmentScanner
整合）驗證腳本（假物件，不碰真實硬體）。

比照專案既有的 verify_meter_panel.py／scratchpad verify_fiber_scanner_signal.py
慣例：獨立可執行、用 record()/check() helper 收集案例結果、失敗時清楚印出
案例編號與 expected/actual、結尾印總結並在有失敗時 sys.exit(1)。不是
pytest 測試檔，不需要安裝任何額外套件。

執行方式：
    PYTHONUTF8=1 venv/Scripts/python.exe verify_scan_tab.py

涵蓋範圍（見案例編號 1~26）：
  一、_do_start_scan() 前置檢查與確認視窗（1~5）
  二、正常收斂完成（6~8）
  三、使用者中止（9）
  四、EMS 觸發（10）
  五、無訊號中止（11~13）
  六、非預期例外（14）
  七、_scanner_power_query() 例外邊界（15~16）
  八、_pm_sync_scan_notice() 光功率分頁協調（17~20）
  九、scanner_config.json 持久化（21~24）
  十、_redraw_scan_plot() 例外容錯（25）
  十一、_on_close() 通知背景尋光執行緒（26）

安全規則（絕對遵守）：
  - 不連真實硬體：ctrl 一律用真的 DS102Controller() 實例（DS102GUI.__init__
    內部會建立，不整個替換掉），但 self.ser 全程維持 None（從未真正
    connect() 過），逐一 monkeypatch scan_move_step/wait_axis_stop/
    query_status 等個別方法/屬性。gui.meter 直接用簡單假物件替換
    （這個屬性本身設計成可替換）。
  - 不寫入真實 recordings/：main_ai.RECORDING_DIR 全程 monkeypatch 到
    tempfile.mkdtemp() 產生的暫存目錄。
  - fiber_scanner.persist_samples() 預設輸出目錄
    `Path(fiber_scanner.__file__).parent / "recordings" / "scans"`
    是獨立算出來的、不受 main_ai.RECORDING_DIR 影響——同樣 monkeypatch
    fiber_scanner._default_scan_dir 到暫存目錄，避免真的跑 scanner.run()
    時把樣本寫進專案的 recordings/scans/。
  - 全程不使用 winfo_ismapped()（root.withdraw() 之後恆為 False，會
    產生假陽性）；一律用 widget.winfo_manager() != "" 判斷是否已 pack。
"""

import math
import os
import random
import shutil
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import main_ai
from fiber_scanner import Sample

# ── 案例結果收集 ──
_results = []  # list[(name, passed, detail)]


def record(name: str, passed: bool, detail: str = ""):
    _results.append((name, passed, detail))
    tag = "PASS" if passed else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail and not passed else ""))


def check(name: str, condition: bool, expected="", actual=""):
    if condition:
        record(name, True)
    else:
        detail = f"expected={expected!r} actual={actual!r}" if (expected or actual) else ""
        record(name, False, detail)


def pump_until(root, condition_fn, timeout=15.0):
    """
    真正把 Tk 事件迴圈跑起來，直到 condition_fn() 為真或逾時。

    root.after(0, ...) 排的 callback（main_ai.py 背景執行緒回主執行緒的
    既有寫法）必須讓 mainloop() 真的轉起來才會被執行——Python 3.14 的
    tkinter 要求背景執行緒呼叫 after()／存取 widget 前，本執行緒必須
    正在跑 mainloop，光是 root.update() 迴圈不算數（實測仍會拋
    RuntimeError: main thread is not in main loop）。
    """
    deadline = time.time() + timeout

    def poll():
        if condition_fn() or time.time() > deadline:
            root.quit()
        else:
            root.after(20, poll)

    root.after(20, poll)
    root.mainloop()


# =============================================================================
# 假物件與 fake ctrl 建置
# =============================================================================
class FakeMeter:
    """簡單假光功率計：固定值（可選帶雜訊），沒有依賴 pyvisa。"""

    def __init__(self, value=-10.0, ok=True, noise=0.0):
        self.value = value
        self.ok = ok
        self.noise = noise
        self.closed = False

    def get_power(self):
        v = self.value + (random.gauss(0, self.noise) if self.noise else 0.0)
        return self.ok, v

    def close(self):
        self.closed = True


class RaisingMeter:
    """get_power() 一律拋例外，用於驗證 _scanner_power_query 的例外邊界。"""

    def get_power(self):
        raise RuntimeError("模擬 GPIB 例外")


class GaussianMeter:
    """
    合成有真正峰值的功率計：功率隨機械座標到 peak 的距離呈高斯衰減。
    用於驗證尋光演算法「正常收斂完成」情境——純隨機雜訊無法保證
    _check_signal_detectable 不會誤判為無訊號，必須是有結構的峰值函式。
    """

    def __init__(self, ctrl, peak, amplitude=15.0, floor=-60.0, sigma=150.0, noise=0.02):
        self.ctrl = ctrl
        self.peak = peak
        self.amplitude = amplitude
        self.floor = floor
        self.sigma = sigma
        self.noise = noise

    def get_power(self):
        pos = self.ctrl.positions_machine
        d2 = sum((pos.get(ax, 0.0) - self.peak.get(ax, 0.0)) ** 2 for ax in self.peak)
        val = self.floor + self.amplitude * math.exp(-d2 / (2.0 * self.sigma * self.sigma))
        val += random.gauss(0, self.noise)
        return True, val

    def close(self):
        pass


def make_fake_scan_move_step(ctrl, delay=0.0, always_fail=False):
    """
    取代 ctrl.scan_move_step：不碰序列埠，直接依方向/距離同步更新
    ctrl._positions_pulse，並回傳 True（除非 always_fail）。

    簽章比照 DS102Controller.scan_move_step / _do_move_step：
    (axis_no, direction, amount, l_speed, f_speed, rate, s_rate, wait_done=True)。
    """

    def _fake(axis_no, direction, amount, l_speed, f_speed, rate, s_rate, wait_done=True):
        if delay:
            time.sleep(delay)
        if always_fail:
            return False
        ax = main_ai.NO_AXIS.get(axis_no)
        if ax is None:
            return False
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            return False
        delta = amt if direction == "CW" else -amt
        with ctrl._lock:
            ctrl._positions_pulse[ax] = ctrl._positions_pulse.get(ax, 0.0) + delta
        return True

    return _fake


def make_raising_scan_move_step(exc: Exception):
    """取代 ctrl.scan_move_step，第一次呼叫就丟出例外——驗證「非預期例外」情境。"""

    def _fake(axis_no, direction, amount, l_speed, f_speed, rate, s_rate, wait_done=True):
        raise exc

    return _fake


def fake_wait_axis_stop(axis_no, timeout=None):
    """假 scan_move_step 已同步完成移動，wait_axis_stop 恆回 True。"""
    return True


def make_fake_query_status(axis_count):
    """
    比照 _active_axes() 依賴的語意：軸號超過 axis_count 視為
    「Stage not connected」而被排除，其餘一律回 Stop（不 Driving、不撞限位）。
    """

    def _fake(axis_no):
        try:
            n = int(axis_no)
        except (TypeError, ValueError):
            return "通訊錯誤", ""
        if n > axis_count:
            return "Stage not connected", ""
        return "Stop", "0"

    return _fake


def setup_fake_ctrl(gui, axis_count=3, move_delay=0.0, always_fail_move=False):
    """
    把 gui.ctrl（真的 DS102Controller 實例，self.ser 全程 None）的個別
    方法/屬性換成假物件——不整個替換掉 ctrl，維持 DS102GUI.__init__
    已經建立好的大量既有屬性。
    """
    ctrl = gui.ctrl
    ctrl.connected = True
    ctrl.scanning_active = False
    ctrl.ems_active = False
    ctrl.axis_count = axis_count
    for ax in main_ai.AXES:
        ctrl._positions_pulse[ax] = 0.0
    ctrl.scan_move_step = make_fake_scan_move_step(ctrl, delay=move_delay, always_fail=always_fail_move)
    ctrl.wait_axis_stop = fake_wait_axis_stop
    ctrl.query_status = make_fake_query_status(axis_count)
    return ctrl


def make_gui(tmp_recording_dir, tmp_scan_dir):
    """
    建立一個新的 (root, gui)，並把 main_ai.RECORDING_DIR /
    fiber_scanner._default_scan_dir 都導向暫存目錄。呼叫端負責在使用完後
    呼叫 gui._on_close() 或至少 gui._shutting_down.set() + root.destroy()。

    回傳的兩個 patcher 由呼叫端持有，等 gui 生命週期結束再 stop()——
    這樣才能涵蓋 gui 存活期間任何時間點觸發的 _save_scanner_config() /
    scanner.persist_samples()。
    """
    p1 = patch.object(main_ai, "RECORDING_DIR", new=Path(tmp_recording_dir))
    p2 = patch("fiber_scanner._default_scan_dir", return_value=Path(tmp_scan_dir))
    p1.start()
    p2.start()
    root = tk.Tk()
    root.withdraw()
    gui = main_ai.DS102GUI(root)
    return root, gui, (p1, p2)


def teardown_gui(root, gui, patchers, use_on_close=False):
    if use_on_close:
        try:
            gui._on_close()  # 內部會呼叫 root.destroy()
        except tk.TclError:
            pass
    else:
        gui._shutting_down.set()
        try:
            root.destroy()
        except tk.TclError:
            pass
    for p in patchers:
        p.stop()


# =============================================================================
# 一、前置檢查與確認視窗（案例 1~5）
# =============================================================================
def test_section1_preconditions(root, gui):
    print("\n=== 一、_do_start_scan() 前置檢查與確認視窗（案例 1~5）===")
    setup_fake_ctrl(gui)
    gui.meter = FakeMeter(value=-10.0, ok=True)

    # --- 案例 1：未連線 ---
    gui.ctrl.connected = False
    banner_calls = []
    with patch.object(gui, "_flash_banner", side_effect=lambda *a, **k: banner_calls.append(a)):
        gui._do_start_scan()
    check(
        "1. ctrl.connected=False -> 不啟動、_scanning 不會被 set",
        (not gui._scanning.is_set()) and any("先連線 DS102" in str(a) for a in banner_calls),
        expected="_scanning 不 set 且橫幅含「先連線 DS102」",
        actual=(gui._scanning.is_set(), banner_calls),
    )
    gui.ctrl.connected = True

    # --- 案例 2：已連線但未連光功率計 ---
    saved_meter = gui.meter
    gui.meter = None
    banner_calls = []
    with patch.object(gui, "_flash_banner", side_effect=lambda *a, **k: banner_calls.append(a)):
        gui._do_start_scan()
    check(
        "2. gui.meter is None -> 不啟動、橫幅含「先連線光功率計」",
        (not gui._scanning.is_set()) and any("先連線光功率計" in str(a) for a in banner_calls),
        expected="不啟動且橫幅含「先連線光功率計」",
        actual=(gui._scanning.is_set(), banner_calls),
    )
    gui.meter = saved_meter

    # --- 案例 3：已有搜尋在進行中 ---
    gui.ctrl.scanning_active = True
    banner_calls = []
    with patch.object(gui, "_flash_banner", side_effect=lambda *a, **k: banner_calls.append(a)):
        gui._do_start_scan()
    check(
        "3. ctrl.scanning_active=True -> 不啟動、橫幅含「已有搜尋在進行中」",
        (not gui._scanning.is_set()) and any("已有搜尋在進行中" in str(a) for a in banner_calls),
        expected="不啟動且橫幅含「已有搜尋在進行中」",
        actual=(gui._scanning.is_set(), banner_calls),
    )
    gui.ctrl.scanning_active = False

    # --- 案例 4：確認視窗按「否」 ---
    with patch("main_ai.messagebox.askyesno", return_value=False) as mock_ask:
        gui._do_start_scan()
    check(
        "4. 確認視窗按「否」-> 不啟動",
        mock_ask.called and not gui._scanning.is_set(),
        expected="askyesno 被呼叫且 _scanning 不 set",
        actual=(mock_ask.called, gui._scanning.is_set()),
    )

    # --- 案例 5：全部通過、確認視窗按「是」---
    with patch("main_ai.messagebox.askyesno", return_value=True):
        gui._do_start_scan()
    check("5a. 按「是」後 _scanning.is_set()", gui._scanning.is_set() is True)
    check(
        "5b. 開始鍵 disabled",
        str(gui._scan_start_btn.cget("state")) == "disabled",
        expected="disabled", actual=str(gui._scan_start_btn.cget("state")),
    )
    check(
        "5c. 停止鍵 normal",
        str(gui._scan_stop_btn.cget("state")) == "normal",
        expected="normal", actual=str(gui._scan_stop_btn.cget("state")),
    )

    # 收尾：停止這一輪，避免殘留背景執行緒干擾後續案例。
    gui._do_stop_scan()
    pump_until(root, lambda: not gui._scanning.is_set(), timeout=15.0)
    check("5d. 停止後 _scanning 清除（收尾）", not gui._scanning.is_set())


# =============================================================================
# 二、正常收斂完成（案例 6~8）
# =============================================================================
def test_section2_completed(root, gui):
    print("\n=== 二、正常收斂完成（案例 6~8）===")
    setup_fake_ctrl(gui, move_delay=0.0)
    peak = {"X": 120.0, "Y": -80.0, "Z": 50.0}
    gui.meter = GaussianMeter(gui.ctrl, peak)
    gui._scan_settle_sec_var.set("0.01")  # 加速測試，不影響邏輯

    with patch("main_ai.messagebox.askyesno", return_value=True):
        gui._do_start_scan()
    pump_until(root, lambda: not gui._scanning.is_set(), timeout=60.0)

    status = gui._scan_status_var.get()
    check(
        "6a. 跑完一輪 -> _scan_status_var 含「完成」",
        "完成" in status, expected="含 完成", actual=status,
    )
    check(
        "6b. _scan_no_signal_notice 未顯示",
        gui._scan_no_signal_notice.winfo_manager() == "",
        expected="", actual=gui._scan_no_signal_notice.winfo_manager(),
    )

    check(
        "7a. _scan_n_var 不是初始值 0",
        gui._scan_n_var.get() != "0", expected="!=0", actual=gui._scan_n_var.get(),
    )
    check(
        "7b. _scan_cur_power_var 不是初始值 —",
        gui._scan_cur_power_var.get() != "—",
        expected="!=—", actual=gui._scan_cur_power_var.get(),
    )
    check(
        "7c. _scan_best_power_var 不是初始值 —",
        gui._scan_best_power_var.get() != "—",
        expected="!=—", actual=gui._scan_best_power_var.get(),
    )
    check(
        "7d. _scan_coord_var 不是初始值 —",
        gui._scan_coord_var.get() != "—",
        expected="!=—", actual=gui._scan_coord_var.get(),
    )

    # --- 案例 8：第二輪開始時 _scan_plot_reset() 清掉殘留 ---
    with patch("main_ai.messagebox.askyesno", return_value=True):
        gui._do_start_scan()
    # _scan_plot_reset() 在 _do_start_scan() 內是同步呼叫（早於背景執行緒
    # 啟動），呼叫一結束就該已經清空，不必等這一輪真的跑完再驗證。
    check(
        "8. 第二輪開始後 _scan_samples 立即清空（不是累加上一輪）",
        len(gui._scan_samples) == 0 and gui._scan_sample_count == 0,
        expected=(0, 0), actual=(len(gui._scan_samples), gui._scan_sample_count),
    )
    pump_until(root, lambda: not gui._scanning.is_set(), timeout=60.0)


# =============================================================================
# 三、使用者中止（案例 9）
# =============================================================================
def test_section3_user_stopped(root, gui):
    print("\n=== 三、使用者中止（案例 9）===")
    setup_fake_ctrl(gui, move_delay=0.15)  # 讓移動有明顯延遲，確保有時間介入按下停止
    gui.meter = FakeMeter(value=-10.0, ok=True, noise=0.0)  # 恆定值：sigma<=0，_check_signal_detectable 直接放行

    banner_calls = []
    with patch("main_ai.messagebox.askyesno", return_value=True), \
         patch.object(gui, "_flash_banner", side_effect=lambda *a, **k: banner_calls.append(a)):
        gui._do_start_scan()
        pump_until(root, lambda: gui._scan_sample_count >= 1, timeout=15.0)
        gui._do_stop_scan()
        pump_until(root, lambda: not gui._scanning.is_set(), timeout=20.0)

    status = gui._scan_status_var.get()
    check(
        "9a. 最終狀態含「中止」且不含「完成」",
        ("中止" in status) and ("完成" not in status),
        expected="含 中止、不含 完成", actual=status,
    )
    check(
        "9b. _flash_banner 有被呼叫且訊息含「使用者中止」",
        any("使用者中止" in str(a) for a in banner_calls),
        expected="含 使用者中止", actual=banner_calls,
    )
    check(
        "9c. _flash_banner 訊息不含「完成」",
        not any("完成" in str(a) for a in banner_calls),
        expected="不含 完成", actual=banner_calls,
    )


# =============================================================================
# 四、EMS 觸發（案例 10）
# =============================================================================
def test_section4_ems(root, gui):
    print("\n=== 四、EMS 觸發（案例 10）===")
    setup_fake_ctrl(gui, move_delay=0.15)
    gui.meter = FakeMeter(value=-10.0, ok=True, noise=0.0)

    banner_calls = []
    with patch("main_ai.messagebox.askyesno", return_value=True), \
         patch.object(gui, "_flash_banner", side_effect=lambda *a, **k: banner_calls.append(a)):
        gui._do_start_scan()
        pump_until(root, lambda: gui._scan_sample_count >= 1, timeout=15.0)
        gui.ctrl.ems_active = True
        pump_until(root, lambda: not gui._scanning.is_set(), timeout=20.0)

    status = gui._scan_status_var.get()
    check(
        "10a. EMS 觸發 -> 最終狀態不含「完成」",
        "完成" not in status, expected="不含 完成", actual=status,
    )
    check(
        "10b. EMS 觸發 -> _flash_banner 訊息不含「完成」",
        not any("完成" in str(a) for a in banner_calls),
        expected="不含 完成", actual=banner_calls,
    )
    gui.ctrl.ems_active = False  # 收尾，避免影響後續案例


# =============================================================================
# 五、無訊號中止（案例 11~13）
# =============================================================================
def test_section5_no_signal(root, gui):
    print("\n=== 五、無訊號中止（案例 11~13）===")
    setup_fake_ctrl(gui, move_delay=0.0)
    gui._scan_settle_sec_var.set("0.01")

    max_attempts = 12
    triggered = False
    for attempt in range(1, max_attempts + 1):
        gui.ctrl.scanning_active = False
        gui.ctrl.ems_active = False
        gui.meter = FakeMeter(value=0.02, ok=True, noise=0.005)  # 固定值+極小雜訊
        with patch("main_ai.messagebox.askyesno", return_value=True):
            gui._do_start_scan()
        pump_until(root, lambda: not gui._scanning.is_set(), timeout=30.0)
        if "未偵測到訊號" in gui._scan_status_var.get():
            triggered = True
            break

    check(
        f"11-setup. 重試 {attempt} 次內觸發無訊號中止",
        triggered,
        expected="觸發", actual=f"重試 {max_attempts} 次仍未觸發",
    )
    if not triggered:
        return  # 統計性案例：無法觸發就不繼續往下驗證，避免對不存在的狀態斷言

    check(
        "11a. _scan_no_signal_notice 顯示",
        gui._scan_no_signal_notice.winfo_manager() != "",
        expected="!=''", actual=gui._scan_no_signal_notice.winfo_manager(),
    )
    check(
        "11b. 提示文字含「未偵測到可用訊號」",
        "未偵測到可用訊號" in gui._scan_no_signal_notice_label.cget("text"),
        expected="含 未偵測到可用訊號", actual=gui._scan_no_signal_notice_label.cget("text"),
    )
    check(
        "11c. _scan_status_var 含「未偵測到訊號」",
        "未偵測到訊號" in gui._scan_status_var.get(),
        expected="含 未偵測到訊號", actual=gui._scan_status_var.get(),
    )

    # --- 案例 12：按「知道了」---
    gui._hide_scan_no_signal_notice()
    check(
        "12. 按「知道了」後提示收起",
        gui._scan_no_signal_notice.winfo_manager() == "",
        expected="", actual=gui._scan_no_signal_notice.winfo_manager(),
    )

    # --- 案例 13：下一輪開始尋光時自動隱藏殘留的無訊號提示 ---
    gui._show_scan_no_signal_notice("模擬上一輪殘留、使用者還沒按知道了")
    check(
        "13-setup. 手動重新顯示提示成功",
        gui._scan_no_signal_notice.winfo_manager() != "",
    )
    with patch("main_ai.messagebox.askyesno", return_value=True):
        gui._do_start_scan()
    check(
        "13. 下一輪開始尋光時自動隱藏上一輪殘留的無訊號提示",
        gui._scan_no_signal_notice.winfo_manager() == "",
        expected="", actual=gui._scan_no_signal_notice.winfo_manager(),
    )
    # 讓這一輪跑完收尾，避免殘留背景執行緒影響後續案例。
    pump_until(root, lambda: not gui._scanning.is_set(), timeout=30.0)


# =============================================================================
# 六、非預期例外（案例 14）
# =============================================================================
def test_section6_exception(root, gui):
    print("\n=== 六、非預期例外（案例 14）===")
    setup_fake_ctrl(gui, move_delay=0.0)
    gui.meter = FakeMeter(value=-10.0, ok=True, noise=0.0)
    gui.ctrl.scan_move_step = make_raising_scan_move_step(RuntimeError("模擬 scan_move_step 非預期例外"))

    with patch("main_ai.messagebox.askyesno", return_value=True), \
         patch("main_ai.messagebox.showerror") as mock_err:
        gui._do_start_scan()
        pump_until(root, lambda: not gui._scanning.is_set(), timeout=20.0)

    check("14a. kind=exception -> messagebox.showerror 被呼叫", mock_err.called)
    title = ""
    if mock_err.call_args is not None and mock_err.call_args.args:
        title = mock_err.call_args.args[0]
    check(
        "14b. showerror 標題含「尋光異常結束」",
        "尋光異常結束" in str(title), expected="含 尋光異常結束", actual=title,
    )
    check(
        "14c. 收尾後開始鍵恢復 normal",
        str(gui._scan_start_btn.cget("state")) == "normal",
        expected="normal", actual=str(gui._scan_start_btn.cget("state")),
    )

    # 收尾，還原成正常可用的 scan_move_step，供後續案例使用。
    setup_fake_ctrl(gui, move_delay=0.0)


# =============================================================================
# 七、_scanner_power_query() 例外邊界（案例 15~16）
# =============================================================================
def test_section7_power_query(gui):
    print("\n=== 七、_scanner_power_query() 例外邊界（案例 15~16）===")
    saved_meter = gui.meter

    gui.meter = None
    ok, val = gui._scanner_power_query()
    check(
        "15. gui.meter is None -> 回傳 (False, 0.0)，不拋例外",
        ok is False and val == 0.0, expected=(False, 0.0), actual=(ok, val),
    )

    gui.meter = RaisingMeter()
    try:
        ok, val = gui._scanner_power_query()
        raised = False
    except Exception:
        ok, val = None, None
        raised = True
    check(
        "16. gui.meter.get_power() 拋例外 -> 仍回傳 (False, 0.0)，不往上傳",
        (not raised) and ok is False and val == 0.0,
        expected=(False, 0.0), actual=(ok, val, f"raised={raised}"),
    )

    gui.meter = saved_meter


# =============================================================================
# 八、_pm_sync_scan_notice() 光功率分頁協調（案例 17~20）
# =============================================================================
def test_section8_pm_sync(gui):
    print("\n=== 八、_pm_sync_scan_notice() 光功率分頁協調（案例 17~20）===")
    setup_fake_ctrl(gui)
    saved_meter = gui.meter

    # --- 案例 17：尋光進行中（scanning_active=True）---
    gui.meter = FakeMeter()
    gui.ctrl.scanning_active = True
    gui._pm_sync_scan_notice()
    check(
        "17a. scanning_active=True -> _pm_scan_notice 顯示",
        gui._pm_scan_notice.winfo_manager() != "",
        expected="!=''", actual=gui._pm_scan_notice.winfo_manager(),
    )
    check(
        "17b. 三顆按鈕皆 disabled",
        all(
            str(w.cget("state")) == "disabled"
            for w in (gui._pm_query_btn, gui._pm_auto_poll_cb, gui._pm_apply_range_btn)
        ),
        expected="全部 disabled",
        actual=[str(w.cget("state")) for w in (gui._pm_query_btn, gui._pm_auto_poll_cb, gui._pm_apply_range_btn)],
    )

    # --- 案例 18：尋光結束、已連線 ---
    gui.ctrl.scanning_active = False
    gui._pm_sync_scan_notice()
    check(
        "18a. scanning_active=False 且已連線 -> 提示收起",
        gui._pm_scan_notice.winfo_manager() == "",
        expected="", actual=gui._pm_scan_notice.winfo_manager(),
    )
    check(
        "18b. 三顆按鈕恢復 normal",
        all(
            str(w.cget("state")) == "normal"
            for w in (gui._pm_query_btn, gui._pm_auto_poll_cb, gui._pm_apply_range_btn)
        ),
        expected="全部 normal",
        actual=[str(w.cget("state")) for w in (gui._pm_query_btn, gui._pm_auto_poll_cb, gui._pm_apply_range_btn)],
    )
    check(
        "18c. _pm_status_var == 已連線",
        gui._pm_status_var.get() == "已連線",
        expected="已連線", actual=gui._pm_status_var.get(),
    )

    # --- 案例 19：尋光結束、未連線光功率計 ---
    gui.ctrl.scanning_active = True
    gui._pm_sync_scan_notice()  # 先顯示，才有「收回」這個轉變可觀察
    gui.meter = None
    gui.ctrl.scanning_active = False
    gui._pm_sync_scan_notice()
    check(
        "19. scanning_active=False 且 meter=None -> _pm_status_var == 未連線（非無條件變已連線）",
        gui._pm_status_var.get() == "未連線",
        expected="未連線", actual=gui._pm_status_var.get(),
    )

    gui.meter = saved_meter
    gui.ctrl.scanning_active = False
    gui._pm_sync_scan_notice()

    # --- 案例 20：_scan_plot_extend 同步光功率分頁變數 ---
    gui.meter = FakeMeter()
    gui._scan_plot_reset()
    sample = Sample(coords={"X": 10.0, "Y": 20.0, "Z": 30.0}, ok=True, power=-5.0)
    gui._scan_plot_extend([sample])
    check(
        "20a. _pm_power_var 隨樣本同步更新",
        gui._pm_power_var.get() == "-5.00",
        expected="-5.00", actual=gui._pm_power_var.get(),
    )
    check(
        "20b. _pm_last_value 隨樣本同步更新",
        gui._pm_last_value == -5.0,
        expected=-5.0, actual=gui._pm_last_value,
    )
    gui._scan_plot_reset()
    gui.meter = saved_meter


# =============================================================================
# 九、scanner_config.json 持久化（案例 21~24）
# =============================================================================
def test_section9_config_persistence(root, gui, primary_recording_dir):
    print("\n=== 九、scanner_config.json 持久化（案例 21~24）===")

    # --- 案例 21：存檔內容排除 initial_step ---
    cfg_path = Path(primary_recording_dir) / "scanner_config.json"
    check("21-setup. scanner_config.json 已由先前案例的 _do_start_scan() 寫出", cfg_path.exists())
    if cfg_path.exists():
        import json as _json
        data = _json.loads(cfg_path.read_text(encoding="utf-8"))
        check(
            "21. 存檔內容不含 initial_step 欄位",
            "initial_step" not in data,
            expected="不含 initial_step", actual=list(data.keys()),
        )
        check(
            "21b. 存檔內容也不含 stage2_local_radius / axis_scale（同一批刻意不存欄位）",
            "stage2_local_radius" not in data and "axis_scale" not in data,
            expected="都不含", actual=list(data.keys()),
        )

    # --- 案例 22：開機時讀取既有設定檔正確預填輸入框 ---
    dir22 = tempfile.mkdtemp(prefix="verify_scan_tab_cfg22_")
    scan22 = tempfile.mkdtemp(prefix="verify_scan_tab_scan22_")
    try:
        seed_cfg = {
            "l_speed": "7",
            "f_speed": "1234",
            "rate": "77",
            "s_rate": "3",
            "step_min": "9",
            "settle_sec": "0.5",
            "max_cycles": "11",
            "noise_sigma_mult": "2.5",
            "no_signal_range_mult": "1.5",
            "abort_if_no_signal": False,
            "min_valid_power_dbm": "-33.5",
            "enable_stage2": True,
        }
        import json as _json
        (Path(dir22) / "scanner_config.json").write_text(
            _json.dumps(seed_cfg, ensure_ascii=False), encoding="utf-8"
        )
        root22, gui22, patchers22 = make_gui(dir22, scan22)
        try:
            check(
                "22a. l_speed 預填正確",
                gui22._scan_l_speed_var.get() == "7",
                expected="7", actual=gui22._scan_l_speed_var.get(),
            )
            check(
                "22b. f_speed 預填正確",
                gui22._scan_f_speed_var.get() == "1234",
                expected="1234", actual=gui22._scan_f_speed_var.get(),
            )
            check(
                "22c. step_min 預填正確",
                gui22._scan_step_min_var.get() == "9",
                expected="9", actual=gui22._scan_step_min_var.get(),
            )
            check(
                "22d. min_valid_power_dbm 預填正確",
                gui22._scan_min_valid_power_var.get() == "-33.5",
                expected="-33.5", actual=gui22._scan_min_valid_power_var.get(),
            )
            check(
                "22e. enable_stage2 (BooleanVar) 預填正確",
                gui22._scan_stage2_var.get() is True,
                expected=True, actual=gui22._scan_stage2_var.get(),
            )
            check(
                "22f. abort_if_no_signal (BooleanVar) 預填正確",
                gui22._scan_abort_no_signal_var.get() is False,
                expected=False, actual=gui22._scan_abort_no_signal_var.get(),
            )
        finally:
            teardown_gui(root22, gui22, patchers22)
    finally:
        shutil.rmtree(dir22, ignore_errors=True)
        shutil.rmtree(scan22, ignore_errors=True)

    # --- 案例 23：頂層是 list 時 _load_scanner_config() 回空字典、不拋例外 ---
    dir23 = tempfile.mkdtemp(prefix="verify_scan_tab_cfg23_")
    try:
        (Path(dir23) / "scanner_config.json").write_text("[1, 2, 3]", encoding="utf-8")
        with patch.object(main_ai, "RECORDING_DIR", new=Path(dir23)):
            try:
                result = main_ai._load_scanner_config()
                raised = False
            except Exception:
                result = None
                raised = True
        check(
            "23. 頂層是 list -> _load_scanner_config() 回傳空字典、不拋例外",
            (not raised) and result == {},
            expected="{} 且不拋例外", actual=(result, f"raised={raised}"),
        )

        # --- 案例 24：DS102GUI() 在讀到格式不符的設定檔時不會 crash ---
        scan24 = tempfile.mkdtemp(prefix="verify_scan_tab_scan24_")
        try:
            try:
                root24, gui24, patchers24 = make_gui(dir23, scan24)
                constructed_ok = True
            except Exception as e:
                root24 = gui24 = patchers24 = None
                constructed_ok = False
                construct_err = e
            check(
                "24. 設定檔頂層是 list 時 DS102GUI() 仍能正常建構完成",
                constructed_ok,
                expected="不 crash",
                actual="正常" if constructed_ok else f"例外: {construct_err!r}",
            )
            if constructed_ok:
                teardown_gui(root24, gui24, patchers24)
        finally:
            shutil.rmtree(scan24, ignore_errors=True)
    finally:
        shutil.rmtree(dir23, ignore_errors=True)


# =============================================================================
# 十、_redraw_scan_plot() 例外容錯（案例 25）
# =============================================================================
def test_section10_redraw_tolerance(gui):
    print("\n=== 十、_redraw_scan_plot() 例外容錯（案例 25）===")

    call_count = [0]

    def _raising_extend(samples):
        call_count[0] += 1
        raise ValueError("模擬 _scan_plot_extend 非預期例外")

    with patch.object(gui, "_scan_plot_extend", side_effect=_raising_extend):
        # 塞一筆待處理樣本，確保 _redraw_scan_plot 真的會呼叫 _scan_plot_extend。
        with gui._scan_plot_lock:
            gui._scan_plot_pending.append(
                Sample(coords={"X": 0.0, "Y": 0.0, "Z": 0.0}, ok=True, power=-1.0)
            )
        raised = False
        try:
            gui._redraw_scan_plot()
        except Exception:
            raised = True
        check(
            "25a. _scan_plot_extend 丟出非 TclError 例外時，_redraw_scan_plot() 不往外拋",
            not raised, expected="不拋例外", actual=f"raised={raised}",
        )
        check("25b. _scan_plot_extend 確實被呼叫過一次", call_count[0] == 1, expected=1, actual=call_count[0])

        # 第二次呼叫：證明第一次的例外沒有讓這條迴圈「死掉」。
        with gui._scan_plot_lock:
            gui._scan_plot_pending.append(
                Sample(coords={"X": 1.0, "Y": 1.0, "Z": 1.0}, ok=True, power=-2.0)
            )
        gui._redraw_scan_plot()
        check(
            "25c. 第二次呼叫 _scan_plot_extend 又被呼叫一次（沒有因例外而停擺）",
            call_count[0] == 2, expected=2, actual=call_count[0],
        )

        # 間接驗證有嘗試重新排程下一輪：暫時把 root.after 換成 MagicMock，
        # 呼叫一次確認有排程呼叫（引數含 SCAN_PLOT_REDRAW_INTERVAL）。
        with gui._scan_plot_lock:
            gui._scan_plot_pending.append(
                Sample(coords={"X": 2.0, "Y": 2.0, "Z": 2.0}, ok=True, power=-3.0)
            )
        with patch.object(gui.root, "after") as mock_after:
            gui._redraw_scan_plot()
        rescheduled = any(
            call.args and call.args[0] == main_ai.SCAN_PLOT_REDRAW_INTERVAL
            for call in mock_after.call_args_list
        )
        check(
            "25d. 例外發生後仍呼叫 root.after(SCAN_PLOT_REDRAW_INTERVAL, ...) 重新排程",
            rescheduled, expected=True, actual=mock_after.call_args_list,
        )

    # 收尾：清空可能殘留的 pending，避免真的 _scan_plot_extend 在下一輪自然
    # 重新排程時對殘留的假樣本重繪。
    with gui._scan_plot_lock:
        gui._scan_plot_pending.clear()
    gui._scan_plot_reset()


# =============================================================================
# 十一、_on_close() 通知背景尋光執行緒（案例 26）
# =============================================================================
def test_section11_on_close(root, gui):
    print("\n=== 十一、_on_close() 通知背景尋光執行緒（案例 26）===")

    class FakeScanner:
        def __init__(self):
            self.stop_requested = False

        def request_stop(self):
            self.stop_requested = True

    fake_scanner = FakeScanner()
    gui._active_scanner = fake_scanner
    gui.ctrl.ems_active = False
    check("26-setup. _active_scanner 已設為假物件", gui._active_scanner is fake_scanner)

    # _on_close() 內部依序：set 收工旗標 -> _active_scanner.request_stop()
    # -> （ctrl.connected 時）stop()+disconnect() -> （meter 存在時）close()
    # -> root.destroy()。ctrl.ser 全程是 None，stop()/disconnect() 對它
    # 安全（皆有 `if not (self.ser and self.ser.is_open): return` 這類
    # 守衛），meter 是假物件也有 close()，整段可以放心直接呼叫真正的
    # _on_close()，不需要另外 monkeypatch 掉它。這也是本腳本主要
    # gui/root 的最終清理步驟。
    gui._on_close()

    check(
        "26. _on_close() 呼叫了 _active_scanner.request_stop()",
        fake_scanner.stop_requested is True,
        expected=True, actual=fake_scanner.stop_requested,
    )


# =============================================================================
# 主流程
# =============================================================================
def main():
    primary_recording_dir = tempfile.mkdtemp(prefix="verify_scan_tab_rec_")
    primary_scan_dir = tempfile.mkdtemp(prefix="verify_scan_tab_scan_")

    root, gui, patchers = make_gui(primary_recording_dir, primary_scan_dir)

    try:
        test_section1_preconditions(root, gui)
        test_section2_completed(root, gui)
        test_section3_user_stopped(root, gui)
        test_section4_ems(root, gui)
        test_section5_no_signal(root, gui)
        test_section6_exception(root, gui)
        test_section7_power_query(gui)
        test_section8_pm_sync(gui)
        test_section9_config_persistence(root, gui, primary_recording_dir)
        test_section10_redraw_tolerance(gui)
        # 案例 26 放最後：_on_close() 會呼叫 root.destroy()，是這個
        # gui/root 生命週期的自然終點，不需要額外的 teardown_gui()。
        test_section11_on_close(root, gui)
    finally:
        for p in patchers:
            try:
                p.stop()
            except RuntimeError:
                pass  # 已經 stop 過（例如例外路徑中途已 stop）
        shutil.rmtree(primary_recording_dir, ignore_errors=True)
        shutil.rmtree(primary_scan_dir, ignore_errors=True)

    print("\n=== 總結 ===")
    total = len(_results)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = total - passed
    print(f"共 {total} 項：PASS {passed}、FAIL {failed}")
    if failed:
        print("\n失敗案例：")
        for name, ok, detail in _results:
            if not ok:
                print(f"  - {name}" + (f"（{detail}）" if detail else ""))
        sys.exit(1)
    else:
        print("全部通過。")
        sys.exit(0)


if __name__ == "__main__":
    main()
