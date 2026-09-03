# -*- coding: utf-8 -*-
"""
尋光分頁（main_ai.DS102GUI 的「尋光」分頁 + fiber_scanner.FiberAlignmentScanner
整合）回歸測試（pytest，假物件，不碰真實硬體）。

原本是獨立可執行、用 record()/check() 收集結果的腳本，2026-08 轉換為
pytest 測試檔以便 VS Code Test Explorer 個別發現、個別重跑單一案例。
案例編號沿用原腳本的分組（見下方各 class 開頭註解對應「案例 N」），總計
57 項斷言、一項不少——每個原本的 check() 呼叫都對應一個獨立的
`def test_xxx():`，沒有把多個案例合併進同一個測試函式，確保
`pytest -v` 的項目數與原腳本的「共 57 項」一一對應，方便核對沒有案例被
意外合併或漏掉。

原腳本裡許多案例共用同一個 gui、依賴彼此執行順序留下的狀態（例如案例
21 原本讀取的是案例 1~5 執行 _do_start_scan() 時寫出的 scanner_config.json）。
為了讓每個測試函式都能在 VS Code 裡被單獨選取、單獨重跑而不必依賴其他
測試先跑過，這裡把「多個案例共用同一次昂貴前置動作（例如跑一輪尋光）」
的情境改寫成 fixture：該 fixture 只在同一輪測試之間快取，但單獨選取
其中一個測試時，pytest 一樣會重新完整建立這個 fixture——不需要仰賴
其他測試先跑過。少數案例（8、12、13、21）因此在前置動作上做了小幅
調整（詳見各自的 docstring），但驗證的斷言邏輯與涵蓋範圍未改變。

⚠ 整份檔案（除了少數必須各自建構的案例）共用同一個 (root, gui)
（見下方 `gui` fixture，scope="module"）：實測在同一個 process 內快速
連續建構超過 10 個 tk.Tk() 執行個體會偶發
`_tkinter.TclError: couldn't read file ...treeview.tcl`（檔案其實存在，
屬於快速連續建立/銷毀 Tcl 直譯器時的環境層級競爭，不是程式邏輯錯誤，
但每個 class 各自一個 root 會把總數推到十幾個，明顯提高中獎機率）。
共用同一個 Tk 視窗，不影響「每個測試各自 setup_fake_ctrl()／指定
gui.meter」帶來的獨立性——共用的只是視窗本身。

安全規則（見 conftest.py 開頭，兩支測試檔案共同適用）：
  - 不連真實硬體：ctrl 一律用真的 DS102Controller() 實例（DS102GUI.__init__
    內部會建立，不整個替換掉），但 self.ser 全程維持 None（從未真正
    connect() 過），逐一 monkeypatch scan_move_step/wait_axis_stop/
    query_status 等個別方法/屬性。gui.meter 直接用簡單假物件替換
    （這個屬性本身設計成可替換）。
  - 不寫入真實 recordings/：main_ai.RECORDING_DIR 全程透過 conftest 的
    make_gui() 導向暫存目錄。
  - fiber_scanner.persist_samples() 預設輸出目錄
    `Path(fiber_scanner.__file__).parent / "recordings" / "scans"`
    是獨立算出來的、不受 main_ai.RECORDING_DIR 影響——同樣 monkeypatch
    fiber_scanner._default_scan_dir 到暫存目錄，避免真的跑 scanner.run()
    時把樣本寫進專案的 recordings/scans/。
  - 全程不使用 winfo_ismapped()（root.withdraw() 之後恆為 False，會
    產生假陽性）；一律用 widget.winfo_manager() != "" 判斷是否已 pack。

執行方式（VS Code Test Explorer 或指令列皆可）：
    venv/Scripts/python.exe -m pytest verify_scan_tab.py -v
    venv/Scripts/python.exe -m pytest verify_scan_tab.py::TestStartScanPreflight -v
"""

import json
import math
import random
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import main_ai
from core.fiber_scanner import Sample

from conftest import close_gui, make_gui, pump_until


# =============================================================================
# 假物件與 fake ctrl 建置（沿用原腳本，供各測試建構假 ctrl / 假光功率計）
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


@pytest.fixture(scope="module")
def gui(tmp_path_factory):
    """
    整份檔案共用的單一 (root, gui)。除了下面明確標註「必須各自建構」的
    案例外，其餘所有測試都透過這個 fixture 取用同一個視窗（見檔案開頭
    docstring 說明為什麼共用、以及為什麼不影響各案例的獨立性）。
    """
    recording_dir = tmp_path_factory.mktemp("scan_shared_rec")
    scan_dir = tmp_path_factory.mktemp("scan_shared_scan")
    root, g, patchers = make_gui(
        recording_dir,
        extra_patches=[patch("core.fiber_scanner._default_scan_dir", return_value=scan_dir)],
    )
    yield root, g
    close_gui(root, g, patchers)


# =============================================================================
# 一、_do_start_scan() 前置檢查與確認視窗（案例 1~5）
# =============================================================================
class TestStartScanPreflight:
    @pytest.fixture(autouse=True)
    def _arrange(self, gui):
        root, g = gui
        setup_fake_ctrl(g)
        g.meter = FakeMeter(value=-10.0, ok=True)

    def test_not_connected_blocks_start(self, gui):
        """案例 1：ctrl.connected=False -> 不啟動、_scanning 不會被 set。"""
        root, g = gui
        g.ctrl.connected = False
        banner_calls = []
        try:
            with patch.object(g, "_flash_banner", side_effect=lambda *a, **k: banner_calls.append(a)):
                g._do_start_scan()
            assert not g._scanning.is_set()
            assert any("先連線 DS102" in str(a) for a in banner_calls)
        finally:
            g.ctrl.connected = True

    def test_meter_missing_blocks_start(self, gui):
        """案例 2：gui.meter is None -> 不啟動、橫幅含「先連線光功率計」。"""
        root, g = gui
        saved_meter = g.meter
        g.meter = None
        banner_calls = []
        try:
            with patch.object(g, "_flash_banner", side_effect=lambda *a, **k: banner_calls.append(a)):
                g._do_start_scan()
            assert not g._scanning.is_set()
            assert any("先連線光功率計" in str(a) for a in banner_calls)
        finally:
            g.meter = saved_meter

    def test_scan_already_active_blocks_start(self, gui):
        """案例 3：ctrl.scanning_active=True -> 不啟動、橫幅含「已有搜尋在進行中」。"""
        root, g = gui
        g.ctrl.scanning_active = True
        banner_calls = []
        try:
            with patch.object(g, "_flash_banner", side_effect=lambda *a, **k: banner_calls.append(a)):
                g._do_start_scan()
            assert not g._scanning.is_set()
            assert any("已有搜尋在進行中" in str(a) for a in banner_calls)
        finally:
            g.ctrl.scanning_active = False

    def test_confirm_dialog_declined_blocks_start(self, gui):
        """案例 4：確認視窗按「否」-> 不啟動。"""
        root, g = gui
        with patch("main_ai.messagebox.askyesno", return_value=False) as mock_ask:
            g._do_start_scan()
        assert mock_ask.called
        assert not g._scanning.is_set()

    @pytest.fixture
    def started_scan(self, gui):
        """
        案例 5 前半：確認視窗按「是」，實際啟動一次尋光。scope="function"
        （預設）+ _arrange autouse fixture 確保每次都是全新的假 ctrl，
        5a/5b/5c 共用同一個 pytest 測試「呼叫」時的結果快取（fixture
        在同一個測試函式內只執行一次），單獨執行其中任一測試時 pytest
        一樣會重新完整建立這個 fixture，不依賴其他測試先跑過。
        """
        root, g = gui
        with patch("main_ai.messagebox.askyesno", return_value=True):
            g._do_start_scan()
        yield root, g
        # 收尾：確保任何殘留的尋光都被停止，避免影響後續測試。
        if g._scanning.is_set():
            g._do_stop_scan()
            pump_until(root, lambda: not g._scanning.is_set(), timeout=15.0)

    def test_confirm_accepted_sets_scanning_flag(self, started_scan):
        """案例 5a：按「是」後 _scanning.is_set()。"""
        root, g = started_scan
        assert g._scanning.is_set() is True

    def test_confirm_accepted_disables_start_button(self, started_scan):
        """案例 5b：開始鍵 disabled。"""
        root, g = started_scan
        assert str(g._scan_start_btn.cget("state")) == "disabled"

    def test_confirm_accepted_enables_stop_button(self, started_scan):
        """案例 5c：停止鍵 normal。"""
        root, g = started_scan
        assert str(g._scan_stop_btn.cget("state")) == "normal"

    def test_stop_after_confirm_clears_scanning_flag(self, started_scan):
        """案例 5d：停止後 _scanning 清除（收尾）。"""
        root, g = started_scan
        g._do_stop_scan()
        pump_until(root, lambda: not g._scanning.is_set(), timeout=15.0)
        assert not g._scanning.is_set()


# =============================================================================
# 一之一、速度欄位驗證（NaN／Inf／0／負值／f_speed_min > f_speed）
# =============================================================================
class TestSpeedInputValidation:
    """
    迴歸測試：l_speed／f_speed／rate／s_rate／f_speed_min 都是原始文字
    輸入。float() 能接受 "nan"／"inf"／"0"／負值，但這些值直接組進
    DS102 指令字串（L0／R0／S0／F0）或送進 FiberAlignmentScanner 建構子，
    對控制器毫無意義。驗證必須發生在打開確認對話框之前（跟 0 軸檢查
    同一個位置），失敗時 askyesno 完全不會被呼叫、_scanning 也不會被
    set()。
    """

    @pytest.fixture(autouse=True)
    def _arrange(self, gui):
        root, g = gui
        setup_fake_ctrl(g)
        g.meter = FakeMeter(value=-10.0, ok=True)

    def _assert_blocked(self, g, mock_ask, banner_calls):
        assert not mock_ask.called, "驗證應在確認對話框之前擋下，不該讓使用者看到注定失敗的確認視窗"
        assert not g._scanning.is_set()
        assert any("尋光參數錯誤" in str(a) for a in banner_calls)

    def _run_blocked(self, g):
        banner_calls = []
        with patch("main_ai.messagebox.askyesno", return_value=True) as mock_ask, \
             patch.object(g, "_flash_banner", side_effect=lambda *a, **k: banner_calls.append(a)):
            g._do_start_scan()
        self._assert_blocked(g, mock_ask, banner_calls)

    @pytest.mark.parametrize("bad_value", ["nan", "inf", "-inf", "0", "-100"])
    def test_f_speed_rejects_non_positive_or_non_finite(self, gui, bad_value):
        root, g = gui
        saved = g._scan_f_speed_var.get()
        g._scan_f_speed_var.set(bad_value)
        try:
            self._run_blocked(g)
        finally:
            g._scan_f_speed_var.set(saved)

    def test_l_speed_rejects_non_numeric(self, gui):
        """l_speed 完全未經轉換就直接送進 L0——同樣要擋下非數字輸入。"""
        root, g = gui
        saved = g._scan_l_speed_var.get()
        g._scan_l_speed_var.set("abc")
        try:
            self._run_blocked(g)
        finally:
            g._scan_l_speed_var.set(saved)

    def test_rate_rejects_non_numeric(self, gui):
        root, g = gui
        saved = g._scan_rate_var.get()
        g._scan_rate_var.set("abc")
        try:
            self._run_blocked(g)
        finally:
            g._scan_rate_var.set(saved)

    def test_s_rate_rejects_zero(self, gui):
        root, g = gui
        saved = g._scan_s_rate_var.get()
        g._scan_s_rate_var.set("0")
        try:
            self._run_blocked(g)
        finally:
            g._scan_s_rate_var.set(saved)

    def test_f_speed_min_exceeding_f_speed_rejected(self, gui):
        """f_speed_min 本身合法，但大於 f_speed 時邏輯矛盾——一樣要擋下。"""
        root, g = gui
        saved_min = g._scan_f_speed_min_var.get()
        saved_f = g._scan_f_speed_var.get()
        g._scan_f_speed_var.set("1000")
        g._scan_f_speed_min_var.set("2000")
        try:
            self._run_blocked(g)
        finally:
            g._scan_f_speed_min_var.set(saved_min)
            g._scan_f_speed_var.set(saved_f)

    def test_f_speed_min_blank_is_not_validated(self, gui):
        """f_speed_min 留空是合法的自動模式，不該被驗證擋下。"""
        root, g = gui
        saved_min = g._scan_f_speed_min_var.get()
        g._scan_f_speed_min_var.set("")
        banner_calls = []
        try:
            with patch("main_ai.messagebox.askyesno", return_value=True) as mock_ask, \
                 patch.object(g, "_flash_banner", side_effect=lambda *a, **k: banner_calls.append(a)):
                g._do_start_scan()
            assert mock_ask.called, "空白 f_speed_min 不應被參數驗證擋下，應正常進到確認對話框"
            assert not any("尋光參數錯誤" in str(a) for a in banner_calls)
        finally:
            g._scan_f_speed_min_var.set(saved_min)
            if g._scanning.is_set():
                g._do_stop_scan()
                pump_until(root, lambda: not g._scanning.is_set(), timeout=15.0)


# =============================================================================
# 一之二、建構子輸入驗證失敗時必須回復 UI（不可鎖死開始鍵）
# =============================================================================
class TestConstructorValidationRecovers:
    """
    迴歸測試：FiberAlignmentScanner.__init__ 仍可能因為 TestSpeedInputValidation
    沒涵蓋到的原因丟出 ValueError／TypeError（例如未來新增的建構子參數、
    或驗證邏輯本身遺漏的邊界情況）。_do_start_scan() 在建構 scanner 之前
    已經 set() 了 _scanning、切換了兩個按鈕狀態——若不接住這個例外，UI
    會永久卡在「掃描中」，開始鍵按不下去，因為背景執行緒從未啟動、
    _on_scan_done() 永遠不會被呼叫去解鎖畫面。

    這裡直接 mock FiberAlignmentScanner 本身讓建構必定失敗，不依賴任何
    特定欄位的壞值——欄位層級的驗證由 TestSpeedInputValidation 負責，
    兩者刻意分開，其中一邊的驗證範圍擴大也不會讓另一邊的測試失去意義。
    """

    @pytest.fixture(autouse=True)
    def _arrange(self, gui):
        root, g = gui
        setup_fake_ctrl(g)
        g.meter = FakeMeter(value=-10.0, ok=True)

    def _start_with_failing_constructor(self, g, exc):
        with patch("main_ai.messagebox.askyesno", return_value=True), \
             patch("main_ai.messagebox.showerror") as mock_error, \
             patch("main_ai.FiberAlignmentScanner", side_effect=exc):
            g._do_start_scan()
        return mock_error

    def test_constructor_value_error_recovers_ui(self, gui):
        """建構子丟 ValueError -> _scanning 不殘留 set、開始鍵回復可按。"""
        root, g = gui
        mock_error = self._start_with_failing_constructor(g, ValueError("could not convert string to float: 'abc'"))
        assert not g._scanning.is_set()
        assert str(g._scan_start_btn.cget("state")) == "normal"
        assert str(g._scan_stop_btn.cget("state")) == "disabled"
        assert g._scan_status_var.get() == "尚未開始"
        assert mock_error.called

    def test_constructor_type_error_recovers_ui(self, gui):
        """建構子丟 TypeError（例如參數型別不合）-> 同樣要回復。"""
        root, g = gui
        mock_error = self._start_with_failing_constructor(g, TypeError("unexpected keyword argument"))
        assert not g._scanning.is_set()
        assert str(g._scan_start_btn.cget("state")) == "normal"
        assert str(g._scan_stop_btn.cget("state")) == "disabled"
        assert mock_error.called

    def test_constructor_failure_does_not_start_background_thread(self, gui):
        """建構失敗後 _active_scanner 不應被設成新 scanner（維持 None）。"""
        root, g = gui
        self._start_with_failing_constructor(g, ValueError("bad"))
        assert g._active_scanner is None


# =============================================================================
# 二、正常收斂完成（案例 6~8）
# =============================================================================
class TestScanCompletesNormally:
    PEAK = {"X": 120.0, "Y": -80.0, "Z": 50.0}

    @pytest.fixture
    def completed_scan(self, gui):
        """
        案例 6/7 共用前置：跑完一輪正常收斂的尋光（GaussianMeter 提供真正
        有結構的峰值，純隨機雜訊無法保證 _check_signal_detectable 不會
        誤判為無訊號）。同一個測試函式內只執行一次，多個測試各自獨立
        重新執行一次（見 fixture docstring 慣例說明）。
        """
        root, g = gui
        setup_fake_ctrl(g, move_delay=0.0)
        g.meter = GaussianMeter(g.ctrl, self.PEAK)
        g._scan_settle_sec_var.set("0.01")  # 加速測試，不影響邏輯
        with patch("main_ai.messagebox.askyesno", return_value=True):
            g._do_start_scan()
        pump_until(root, lambda: not g._scanning.is_set(), timeout=60.0)
        return root, g

    def test_completed_scan_status_shows_done(self, completed_scan):
        """案例 6a：跑完一輪 -> _scan_status_var 含「完成」。"""
        root, g = completed_scan
        assert "完成" in g._scan_status_var.get()

    def test_completed_scan_hides_no_signal_notice(self, completed_scan):
        """案例 6b：_scan_no_signal_notice 未顯示。"""
        root, g = completed_scan
        assert g._scan_no_signal_notice.winfo_manager() == ""

    def test_completed_scan_updates_sample_count(self, completed_scan):
        """案例 7a：_scan_n_var 不是初始值 0。"""
        root, g = completed_scan
        assert g._scan_n_var.get() != "0"

    def test_completed_scan_updates_current_power(self, completed_scan):
        """案例 7b：_scan_cur_power_var 不是初始值 —。"""
        root, g = completed_scan
        assert g._scan_cur_power_var.get() != "—"

    def test_completed_scan_updates_best_power(self, completed_scan):
        """案例 7c：_scan_best_power_var 不是初始值 —。"""
        root, g = completed_scan
        assert g._scan_best_power_var.get() != "—"

    def test_completed_scan_updates_coord(self, completed_scan):
        """案例 7d：_scan_coord_var 不是初始值 —。"""
        root, g = completed_scan
        assert g._scan_coord_var.get() != "—"

    def test_second_scan_clears_previous_samples(self, completed_scan):
        """
        案例 8：第二輪開始後 _scan_samples 立即清空（不是累加上一輪）。

        沿用 completed_scan（第一輪已完成、_scan_samples 有殘留資料），
        立刻開始第二輪。_scan_plot_reset() 在 _do_start_scan() 內是同步
        呼叫（早於背景執行緒啟動），呼叫一結束就該已經清空，不必等這一輪
        真的跑完再驗證。
        """
        root, g = completed_scan
        assert len(g._scan_samples) > 0, "前置條件不成立：第一輪應留下殘留樣本才能驗證『第二輪清空』"
        with patch("main_ai.messagebox.askyesno", return_value=True):
            g._do_start_scan()
        try:
            assert len(g._scan_samples) == 0
            assert g._scan_sample_count == 0
        finally:
            # 收尾：讓這一輪跑完，避免殘留背景執行緒影響後續測試。
            pump_until(root, lambda: not g._scanning.is_set(), timeout=60.0)

    def test_second_scan_resets_last_completed_and_abort_reason(self, completed_scan):
        """
        案例 8b：第二輪開始後 _scan_last_completed / _scan_last_abort_reason
        立即重設為 None（不是沿用上一輪的收尾狀態）。

        沿用 completed_scan（第一輪已正常完成，此時 _scan_last_completed
        應已是 True）。掃描進行中若按「匯出 Excel」
        （_export_scan_xlsx）會直接讀這兩個屬性，不重設就會把上一輪的
        完成／中止狀態誤標到這一輪還在進行中的報表上。同樣是
        _do_start_scan() 內同步完成的重設，呼叫一結束就該生效，不必等
        這一輪真的跑完再驗證。
        """
        root, g = completed_scan
        assert g._scan_last_completed is True, "前置條件不成立：第一輪應已標記完成才能驗證『第二輪重設』"
        with patch("main_ai.messagebox.askyesno", return_value=True):
            g._do_start_scan()
        try:
            assert g._scan_last_completed is None
            assert g._scan_last_abort_reason is None
        finally:
            # 收尾：讓這一輪跑完，避免殘留背景執行緒影響後續測試。
            pump_until(root, lambda: not g._scanning.is_set(), timeout=60.0)


# =============================================================================
# 三、使用者中止（案例 9）
# =============================================================================
class TestUserStop:
    @pytest.fixture
    def user_stopped_scan(self, gui):
        root, g = gui
        setup_fake_ctrl(g, move_delay=0.15)  # 讓移動有明顯延遲，確保有時間介入按下停止
        g.meter = FakeMeter(value=-10.0, ok=True, noise=0.0)  # 恆定值：sigma<=0，_check_signal_detectable 直接放行
        banner_calls = []
        with patch("main_ai.messagebox.askyesno", return_value=True), \
             patch.object(g, "_flash_banner", side_effect=lambda *a, **k: banner_calls.append(a)):
            g._do_start_scan()
            pump_until(root, lambda: g._scan_sample_count >= 1, timeout=15.0)
            g._do_stop_scan()
            pump_until(root, lambda: not g._scanning.is_set(), timeout=20.0)
        return g._scan_status_var.get(), banner_calls

    def test_user_stop_status_shows_aborted_not_done(self, user_stopped_scan):
        """案例 9a：最終狀態含「中止」且不含「完成」。"""
        status, _ = user_stopped_scan
        assert "中止" in status
        assert "完成" not in status

    def test_user_stop_banner_mentions_user_abort(self, user_stopped_scan):
        """案例 9b：_flash_banner 有被呼叫且訊息含「使用者中止」。"""
        _, banner_calls = user_stopped_scan
        assert any("使用者中止" in str(a) for a in banner_calls)

    def test_user_stop_banner_excludes_done(self, user_stopped_scan):
        """案例 9c：_flash_banner 訊息不含「完成」。"""
        _, banner_calls = user_stopped_scan
        assert not any("完成" in str(a) for a in banner_calls)


# =============================================================================
# 四、EMS 觸發（案例 10）
# =============================================================================
class TestEmsTrigger:
    @pytest.fixture
    def ems_triggered_scan(self, gui):
        root, g = gui
        setup_fake_ctrl(g, move_delay=0.15)
        g.meter = FakeMeter(value=-10.0, ok=True, noise=0.0)
        banner_calls = []
        with patch("main_ai.messagebox.askyesno", return_value=True), \
             patch.object(g, "_flash_banner", side_effect=lambda *a, **k: banner_calls.append(a)):
            g._do_start_scan()
            pump_until(root, lambda: g._scan_sample_count >= 1, timeout=15.0)
            g.ctrl.ems_active = True
            pump_until(root, lambda: not g._scanning.is_set(), timeout=20.0)
        status = g._scan_status_var.get()
        g.ctrl.ems_active = False  # 收尾，避免影響後續測試
        return status, banner_calls

    def test_ems_trigger_status_not_done(self, ems_triggered_scan):
        """案例 10a：EMS 觸發 -> 最終狀態不含「完成」。"""
        status, _ = ems_triggered_scan
        assert "完成" not in status

    def test_ems_trigger_banner_not_done(self, ems_triggered_scan):
        """案例 10b：EMS 觸發 -> _flash_banner 訊息不含「完成」。"""
        _, banner_calls = ems_triggered_scan
        assert not any("完成" in str(a) for a in banner_calls)


# =============================================================================
# 五、無訊號中止（案例 11~13）
# =============================================================================
class TestNoSignalAbort:
    MAX_ATTEMPTS = 12

    @pytest.fixture
    def no_signal_scan(self, gui):
        """
        案例 11 前置：重試最多 12 次直到觸發「無訊號中止」。FakeMeter 給
        固定極小值+極小雜訊，是否觸發取決於演算法內部的統計判斷，並非
        每次必然發生，故沿用原腳本的重試迴圈（統計性案例）。
        """
        root, g = gui
        triggered = False
        attempt = 0
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            setup_fake_ctrl(g, move_delay=0.0)
            g._scan_settle_sec_var.set("0.01")
            g.ctrl.scanning_active = False
            g.ctrl.ems_active = False
            g.meter = FakeMeter(value=0.02, ok=True, noise=0.005)  # 固定值+極小雜訊
            # 2026-08-26：GUI 預設啟用階段零盲搜（"auto"），無訊號時會先掃過
            # 整個螺旋才回報。本案例測的是「無訊號提示 UI」，不是盲搜本身
            # （那在 verify_blind_scan.py），這裡明確關掉才能維持原本的
            # 判定路徑與可接受的執行時間。
            g._scan_blind_mode_var.set(g._scan_blind_mode_labels["off"])
            with patch("main_ai.messagebox.askyesno", return_value=True):
                g._do_start_scan()
            pump_until(root, lambda: not g._scanning.is_set(), timeout=30.0)
            if "未偵測到訊號" in g._scan_status_var.get():
                triggered = True
                break
        return root, g, triggered, attempt

    def test_no_signal_triggered_within_retry_budget(self, no_signal_scan):
        """案例 11-setup：重試 N 次內觸發無訊號中止。"""
        _, _, triggered, attempt = no_signal_scan
        assert triggered, f"重試 {self.MAX_ATTEMPTS} 次仍未觸發無訊號中止"

    def test_no_signal_notice_visible(self, no_signal_scan):
        """案例 11a：_scan_no_signal_notice 顯示。"""
        root, g, triggered, attempt = no_signal_scan
        if not triggered:
            pytest.skip("前置條件（觸發無訊號中止）未成立，見 test_no_signal_triggered_within_retry_budget")
        assert g._scan_no_signal_notice.winfo_manager() != ""

    def test_no_signal_notice_text(self, no_signal_scan):
        """案例 11b：提示文字含「未偵測到可用訊號」。"""
        root, g, triggered, attempt = no_signal_scan
        if not triggered:
            pytest.skip("前置條件（觸發無訊號中止）未成立，見 test_no_signal_triggered_within_retry_budget")
        assert "未偵測到可用訊號" in g._scan_no_signal_notice_label.cget("text")

    def test_no_signal_status_text(self, no_signal_scan):
        """案例 11c：_scan_status_var 含「未偵測到訊號」。"""
        root, g, triggered, attempt = no_signal_scan
        if not triggered:
            pytest.skip("前置條件（觸發無訊號中止）未成立，見 test_no_signal_triggered_within_retry_budget")
        assert "未偵測到訊號" in g._scan_status_var.get()

    def test_hide_no_signal_notice_via_ack(self, gui):
        """
        案例 12：按「知道了」後提示收起。

        與原腳本的差異：原腳本沿用案例 11 觸發的真實提示狀態；這裡改成
        自行呼叫 _show_scan_no_signal_notice() 先顯示一次再驗證隱藏行為，
        讓這個案例不依賴案例 11 的統計性觸發是否成功，也能單獨重跑。
        驗證的仍是同一個函式 _hide_scan_no_signal_notice() 的行為，斷言
        邏輯未變。
        """
        root, g = gui
        g._show_scan_no_signal_notice("測試用：手動顯示以驗證按「知道了」的收起行為")
        g._hide_scan_no_signal_notice()
        assert g._scan_no_signal_notice.winfo_manager() == ""

    @pytest.fixture
    def stale_notice_then_new_scan(self, gui):
        """
        案例 13 前置：手動重新顯示一則「上一輪殘留」的提示，再開始新一輪
        尋光，驗證是否自動隱藏。與原腳本相同，這裡本來就不依賴案例 11/12
        的統計性觸發（原腳本也是手動重新顯示），只是額外自帶
        setup_fake_ctrl 讓這個 fixture 不必依賴其他測試先跑過。
        """
        root, g = gui
        setup_fake_ctrl(g, move_delay=0.0)
        g._scan_settle_sec_var.set("0.01")
        g.meter = FakeMeter(value=0.02, ok=True, noise=0.005)
        g._show_scan_no_signal_notice("模擬上一輪殘留、使用者還沒按知道了")
        shown = g._scan_no_signal_notice.winfo_manager() != ""
        with patch("main_ai.messagebox.askyesno", return_value=True):
            g._do_start_scan()
        yield root, g, shown
        # 收尾：讓這一輪跑完，避免殘留背景執行緒影響後續測試。
        pump_until(root, lambda: not g._scanning.is_set(), timeout=30.0)

    def test_stale_notice_manually_shown(self, stale_notice_then_new_scan):
        """案例 13-setup：手動重新顯示提示成功。"""
        _, _, shown = stale_notice_then_new_scan
        assert shown

    def test_new_scan_hides_stale_notice(self, stale_notice_then_new_scan):
        """案例 13：下一輪開始尋光時自動隱藏上一輪殘留的無訊號提示。"""
        root, g, shown = stale_notice_then_new_scan
        assert g._scan_no_signal_notice.winfo_manager() == ""


# =============================================================================
# 六、非預期例外（案例 14）
# =============================================================================
class TestUnexpectedException:
    @pytest.fixture
    def exception_scan_result(self, gui):
        root, g = gui
        setup_fake_ctrl(g, move_delay=0.0)
        g.meter = FakeMeter(value=-10.0, ok=True, noise=0.0)
        g.ctrl.scan_move_step = make_raising_scan_move_step(
            RuntimeError("模擬 scan_move_step 非預期例外")
        )
        with patch("main_ai.messagebox.askyesno", return_value=True), \
             patch("main_ai.messagebox.showerror") as mock_err:
            g._do_start_scan()
            pump_until(root, lambda: not g._scanning.is_set(), timeout=20.0)
        title = ""
        if mock_err.call_args is not None and mock_err.call_args.args:
            title = mock_err.call_args.args[0]
        result = {
            "showerror_called": mock_err.called,
            "showerror_title": title,
            "start_btn_state": str(g._scan_start_btn.cget("state")),
        }
        # 收尾，還原成正常可用的 scan_move_step，供後續測試使用。
        setup_fake_ctrl(g, move_delay=0.0)
        return result

    def test_exception_triggers_showerror(self, exception_scan_result):
        """案例 14a：kind=exception -> messagebox.showerror 被呼叫。"""
        assert exception_scan_result["showerror_called"]

    def test_exception_showerror_title(self, exception_scan_result):
        """案例 14b：showerror 標題含「尋光異常結束」。"""
        assert "尋光異常結束" in str(exception_scan_result["showerror_title"])

    def test_exception_recovery_reenables_start_button(self, exception_scan_result):
        """案例 14c：收尾後開始鍵恢復 normal。"""
        assert exception_scan_result["start_btn_state"] == "normal"


# =============================================================================
# 七、_scanner_power_query() 例外邊界（案例 15~16）
# =============================================================================
class TestScannerPowerQueryBoundary:
    def test_meter_none_returns_false_zero(self, gui):
        """案例 15：gui.meter is None -> 回傳 (False, 0.0)，不拋例外。"""
        root, g = gui
        saved_meter = g.meter
        g.meter = None
        try:
            ok, val = g._scanner_power_query()
            assert ok is False
            assert val == 0.0
        finally:
            g.meter = saved_meter

    def test_meter_raises_returns_false_zero(self, gui):
        """案例 16：gui.meter.get_power() 拋例外 -> 仍回傳 (False, 0.0)，不往上傳。"""
        root, g = gui
        saved_meter = g.meter
        g.meter = RaisingMeter()
        try:
            ok, val = g._scanner_power_query()  # 不應拋例外
            assert ok is False
            assert val == 0.0
        finally:
            g.meter = saved_meter


# =============================================================================
# 八、_pm_sync_scan_notice() 光功率分頁協調（案例 17~20）
# =============================================================================
class TestPmSyncScanNotice:
    @pytest.fixture(autouse=True)
    def _arrange(self, gui):
        root, g = gui
        setup_fake_ctrl(g)

    def test_scanning_active_shows_pm_notice(self, gui):
        """案例 17a：scanning_active=True -> _pm_scan_notice 顯示。"""
        root, g = gui
        g.meter = FakeMeter()
        g.ctrl.scanning_active = True
        try:
            g._pm_sync_scan_notice()
            assert g._pm_scan_notice.winfo_manager() != ""
        finally:
            g.ctrl.scanning_active = False
            g._pm_sync_scan_notice()

    def test_scanning_active_disables_pm_buttons(self, gui):
        """案例 17b：三顆按鈕皆 disabled。"""
        root, g = gui
        g.meter = FakeMeter()
        g.ctrl.scanning_active = True
        try:
            g._pm_sync_scan_notice()
            for w in (g._pm_query_btn, g._pm_auto_poll_cb, g._pm_apply_range_btn):
                assert str(w.cget("state")) == "disabled"
        finally:
            g.ctrl.scanning_active = False
            g._pm_sync_scan_notice()

    def test_scan_finished_connected_hides_notice(self, gui):
        """案例 18a：scanning_active=False 且已連線 -> 提示收起。"""
        root, g = gui
        g.meter = FakeMeter()
        g.ctrl.scanning_active = False
        g._pm_sync_scan_notice()
        assert g._pm_scan_notice.winfo_manager() == ""

    def test_scan_finished_connected_restores_buttons(self, gui):
        """案例 18b：三顆按鈕恢復 normal。"""
        root, g = gui
        g.meter = FakeMeter()
        g.ctrl.scanning_active = False
        g._pm_sync_scan_notice()
        for w in (g._pm_query_btn, g._pm_auto_poll_cb, g._pm_apply_range_btn):
            assert str(w.cget("state")) == "normal"

    def test_scan_finished_connected_status_var(self, gui):
        """案例 18c：_pm_status_var == 已連線。"""
        root, g = gui
        g.meter = FakeMeter()
        g.ctrl.scanning_active = False
        g._pm_sync_scan_notice()
        assert g._pm_status_var.get() == "已連線"

    def test_scan_finished_meter_disconnected_status(self, gui):
        """案例 19：scanning_active=False 且 meter=None -> _pm_status_var == 未連線（非無條件變已連線）。"""
        root, g = gui
        g.meter = FakeMeter()
        g.ctrl.scanning_active = True
        g._pm_sync_scan_notice()  # 先顯示，才有「收回」這個轉變可觀察
        g.meter = None
        g.ctrl.scanning_active = False
        g._pm_sync_scan_notice()
        try:
            assert g._pm_status_var.get() == "未連線"
        finally:
            g.meter = FakeMeter()
            g.ctrl.scanning_active = False
            g._pm_sync_scan_notice()

    def test_scan_plot_extend_syncs_pm_power_var(self, gui):
        """案例 20a：_pm_power_var 隨樣本同步更新。"""
        root, g = gui
        g.meter = FakeMeter()
        g._scan_plot_reset()
        sample = Sample(coords={"X": 10.0, "Y": 20.0, "Z": 30.0}, ok=True, power=-5.0)
        g._scan_plot_extend([sample])
        try:
            assert g._pm_power_var.get() == "-5.00"
        finally:
            g._scan_plot_reset()

    def test_scan_plot_extend_syncs_pm_last_value(self, gui):
        """案例 20b：_pm_reading.value 隨樣本同步更新（前置2 後改由 _pm_note_reading 寫入）。"""
        root, g = gui
        g.meter = FakeMeter()
        g._scan_plot_reset()
        sample = Sample(coords={"X": 10.0, "Y": 20.0, "Z": 30.0}, ok=True, power=-5.0)
        g._scan_plot_extend([sample])
        try:
            assert g._pm_reading.value == -5.0
        finally:
            g._scan_plot_reset()

    def test_scan_plot_extend_tags_reading_source(self, gui):
        """案例 20c：尋光轉貼的讀值 source 標成 "scan"。

        純診斷欄位，沒有邏輯依它分流；鎖住它是為了讓將來看到一筆可疑讀值時
        能直接知道它從哪條路徑進來（前置2，見 docs/modularization.md〈九〉）。
        """
        root, g = gui
        g.meter = FakeMeter()
        g._scan_plot_reset()
        sample = Sample(coords={"X": 10.0, "Y": 20.0, "Z": 30.0}, ok=True, power=-5.0)
        g._scan_plot_extend([sample])
        try:
            assert g._pm_reading.source == "scan"
        finally:
            g._scan_plot_reset()

    def test_scan_plot_extend_delegates_status_text(self, gui):
        """案例 20d：_scan_plot_extend() 不再自己寫光功率分頁的狀態文字。

        🔴 這是前置2 真正要鎖住的東西。改動前，尋光的繪圖函式直接把
        `_pm_status_var` 設成「尋光中…」，而 `_pm_refresh_status_line()` 的
        scanning_active 分支也設同一句話——兩份會走鐘的真相來源。現在狀態文字
        完全由 `_pm_refresh_status_line()` 依 `ctrl.scanning_active` 決定。

        失效情境（若有人把那兩行加回去）：尋光結束後最後一批樣本才進到繪圖
        函式，`scanning_active` 已經是 False，畫面卻會被改回「尋光中」，而
        `_pm_sync_scan_notice()` 的還原分支這一輪已經跑過，要等下一輪 100ms
        才會校正——使用者會看到狀態文字閃一下「尋光中」。
        """
        root, g = gui
        g.meter = FakeMeter()
        g.ctrl.scanning_active = False
        g._scan_plot_reset()
        sample = Sample(coords={"X": 10.0, "Y": 20.0, "Z": 30.0}, ok=True, power=-5.0)
        g._scan_plot_extend([sample])
        try:
            assert g._pm_status_var.get() == "已連線"
        finally:
            g._scan_plot_reset()
            g.meter = None
            g.ctrl.scanning_active = False
            g._pm_clear_reading()


# =============================================================================
# 九、scanner_config.json 持久化（案例 21~24）
# =============================================================================
class TestScannerConfigPersistence:
    """
    這個 class 的每個 fixture 都刻意「不」使用共用的 `gui`：每個案例驗證
    的都是「用某種特定的 scanner_config.json 內容開機建構 DS102GUI()」
    這個行為本身，天生就需要各自獨立、全新建構的 GUI 執行個體，無法
    沿用共用視窗。
    """

    @pytest.fixture(scope="class")
    @classmethod
    def written_config(cls, tmp_path_factory):
        """
        案例 21 前置：自行觸發一次 _do_start_scan()，確保 scanner_config.json
        確實被寫出。

        與原腳本的差異：原腳本依賴案例 1~5（TestStartScanPreflight）先
        執行過 _do_start_scan() 才會寫出這個檔案；這裡改為自給自足，在
        本 fixture 內自己觸發一次，讓這個案例不必依賴其他測試先跑過就能
        單獨重跑。_save_scanner_config() 在 _do_start_scan() 內是同步
        呼叫（確認視窗按「是」之後、背景執行緒啟動之前），呼叫一結束就
        已經寫檔，因此啟動後立刻停止即可，不需要等這一輪跑完。驗證的
        斷言（存檔內容應排除哪些欄位）與原腳本完全相同。
        """
        recording_dir = tmp_path_factory.mktemp("scan_cfg21_rec")
        scan_dir = tmp_path_factory.mktemp("scan_cfg21_scan")
        root, g, patchers = make_gui(
            recording_dir,
            extra_patches=[patch("core.fiber_scanner._default_scan_dir", return_value=scan_dir)],
        )
        setup_fake_ctrl(g)
        g.meter = FakeMeter(value=-10.0, ok=True)
        with patch("main_ai.messagebox.askyesno", return_value=True):
            g._do_start_scan()
        g._do_stop_scan()
        pump_until(root, lambda: not g._scanning.is_set(), timeout=30.0)
        cfg_path = Path(recording_dir) / "scanner_config.json"
        yield cfg_path
        close_gui(root, g, patchers)

    def test_config_file_written(self, written_config):
        """案例 21-setup：scanner_config.json 確實由 _do_start_scan() 寫出。"""
        assert written_config.exists()

    def test_config_excludes_initial_step(self, written_config):
        """案例 21：存檔內容不含 initial_step 欄位。"""
        data = json.loads(written_config.read_text(encoding="utf-8"))
        assert "initial_step" not in data

    def test_config_excludes_stage2_runtime_fields(self, written_config):
        """案例 21b：存檔內容也不含 stage2_local_radius / axis_scale（同一批刻意不存欄位）。"""
        data = json.loads(written_config.read_text(encoding="utf-8"))
        assert "stage2_local_radius" not in data
        assert "axis_scale" not in data

    @pytest.fixture(scope="class")
    @classmethod
    def gui_with_seeded_config(cls, tmp_path_factory):
        """案例 22 前置：開機前先在暫存目錄放一份既有 scanner_config.json。"""
        recording_dir = tmp_path_factory.mktemp("scan_cfg22_rec")
        scan_dir = tmp_path_factory.mktemp("scan_cfg22_scan")
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
        (Path(recording_dir) / "scanner_config.json").write_text(
            json.dumps(seed_cfg, ensure_ascii=False), encoding="utf-8"
        )
        root, g, patchers = make_gui(
            recording_dir,
            extra_patches=[patch("core.fiber_scanner._default_scan_dir", return_value=scan_dir)],
        )
        yield root, g
        close_gui(root, g, patchers)

    def test_seeded_config_prefills_l_speed(self, gui_with_seeded_config):
        """案例 22a：l_speed 預填正確。"""
        _, g = gui_with_seeded_config
        assert g._scan_l_speed_var.get() == "7"

    def test_seeded_config_prefills_f_speed(self, gui_with_seeded_config):
        """案例 22b：f_speed 預填正確。"""
        _, g = gui_with_seeded_config
        assert g._scan_f_speed_var.get() == "1234"

    def test_seeded_config_prefills_step_min(self, gui_with_seeded_config):
        """案例 22c：step_min 預填正確。"""
        _, g = gui_with_seeded_config
        assert g._scan_step_min_var.get() == "9"

    def test_seeded_config_prefills_min_valid_power(self, gui_with_seeded_config):
        """案例 22d：min_valid_power_dbm 預填正確。"""
        _, g = gui_with_seeded_config
        assert g._scan_min_valid_power_var.get() == "-33.5"

    def test_seeded_config_prefills_enable_stage2(self, gui_with_seeded_config):
        """案例 22e：enable_stage2（BooleanVar）預填正確。"""
        _, g = gui_with_seeded_config
        assert g._scan_stage2_var.get() is True

    def test_seeded_config_prefills_abort_if_no_signal(self, gui_with_seeded_config):
        """案例 22f：abort_if_no_signal（BooleanVar）預填正確。"""
        _, g = gui_with_seeded_config
        assert g._scan_abort_no_signal_var.get() is False

    def test_load_scanner_config_tolerates_list_top_level(self, tmp_path):
        """案例 23：頂層是 list 時 _load_scanner_config() 回傳空字典、不拋例外。"""
        (tmp_path / "scanner_config.json").write_text("[1, 2, 3]", encoding="utf-8")
        with patch.object(main_ai, "RECORDING_DIR", new=Path(tmp_path)):
            result = main_ai._load_scanner_config()  # 不應拋例外
        assert result == {}

    def test_gui_construction_tolerates_malformed_config(self, tmp_path):
        """
        案例 24：設定檔頂層是 list 時 DS102GUI() 仍能正常建構完成。

        make_gui() 內部若拋出例外，pytest 會直接把這個測試標記為
        failed／error（帶完整 traceback），效果等同原腳本手動包
        try/except 後再斷言 constructed_ok，不需要額外包一層。
        """
        (tmp_path / "scanner_config.json").write_text("[1, 2, 3]", encoding="utf-8")
        scan_dir = tmp_path / "scans"
        scan_dir.mkdir()
        root, g, patchers = make_gui(
            tmp_path,
            extra_patches=[patch("core.fiber_scanner._default_scan_dir", return_value=scan_dir)],
        )
        close_gui(root, g, patchers)


# =============================================================================
# 十、_redraw_scan_plot() 例外容錯（案例 25）
# =============================================================================
class TestRedrawScanPlotTolerance:
    @pytest.fixture
    def redraw_tolerance_result(self, gui):
        """
        案例 25 前置：連續三次讓 _scan_plot_extend 拋出例外，觀察
        _redraw_scan_plot() 是否吞下例外、是否持續運作、是否仍重新排程。
        三個步驟彼此依序相依（第二次呼叫要證明第一次的例外沒讓迴圈死掉），
        只執行一次，供 25a~25d 四個獨立斷言案例共用。
        """
        root, g = gui
        call_count = [0]

        def _raising_extend(samples):
            call_count[0] += 1
            raise ValueError("模擬 _scan_plot_extend 非預期例外")

        result = {}
        with patch.object(g, "_scan_plot_extend", side_effect=_raising_extend):
            with g._scan_plot_lock:
                g._scan_plot_pending.append(
                    Sample(coords={"X": 0.0, "Y": 0.0, "Z": 0.0}, ok=True, power=-1.0)
                )
            raised = False
            try:
                g._redraw_scan_plot()
            except Exception:
                raised = True
            result["raised_after_first_call"] = raised
            result["call_count_after_first"] = call_count[0]

            # 第二次呼叫：證明第一次的例外沒有讓這條迴圈「死掉」。
            with g._scan_plot_lock:
                g._scan_plot_pending.append(
                    Sample(coords={"X": 1.0, "Y": 1.0, "Z": 1.0}, ok=True, power=-2.0)
                )
            g._redraw_scan_plot()
            result["call_count_after_second"] = call_count[0]

            # 間接驗證有嘗試重新排程下一輪：暫時把 root.after 換成
            # MagicMock，呼叫一次確認有排程呼叫（引數含 SCAN_PLOT_REDRAW_INTERVAL）。
            with g._scan_plot_lock:
                g._scan_plot_pending.append(
                    Sample(coords={"X": 2.0, "Y": 2.0, "Z": 2.0}, ok=True, power=-3.0)
                )
            with patch.object(g.root, "after") as mock_after:
                g._redraw_scan_plot()
            result["rescheduled"] = any(
                call.args and call.args[0] == main_ai.SCAN_PLOT_REDRAW_INTERVAL
                for call in mock_after.call_args_list
            )

        # 收尾：清空可能殘留的 pending，避免真的 _scan_plot_extend 在下一輪
        # 自然重新排程時對殘留的假樣本重繪。
        with g._scan_plot_lock:
            g._scan_plot_pending.clear()
        g._scan_plot_reset()
        return result

    def test_exception_does_not_propagate(self, redraw_tolerance_result):
        """案例 25a：_scan_plot_extend 丟出非 TclError 例外時，_redraw_scan_plot() 不往外拋。"""
        assert redraw_tolerance_result["raised_after_first_call"] is False

    def test_extend_called_once_after_first_exception(self, redraw_tolerance_result):
        """案例 25b：_scan_plot_extend 確實被呼叫過一次。"""
        assert redraw_tolerance_result["call_count_after_first"] == 1

    def test_extend_called_again_after_exception(self, redraw_tolerance_result):
        """案例 25c：第二次呼叫 _scan_plot_extend 又被呼叫一次（沒有因例外而停擺）。"""
        assert redraw_tolerance_result["call_count_after_second"] == 2

    def test_reschedules_after_exception(self, redraw_tolerance_result):
        """案例 25d：例外發生後仍呼叫 root.after(SCAN_PLOT_REDRAW_INTERVAL, ...) 重新排程。"""
        assert redraw_tolerance_result["rescheduled"] is True


# =============================================================================
# 十一、_on_close() 通知背景尋光執行緒（案例 26）
# =============================================================================
class TestOnCloseNotifiesScanner:
    @pytest.fixture(scope="class")
    @classmethod
    def closed_gui(cls, tmp_path_factory):
        """
        案例 26：_on_close() 需要真的呼叫（它結尾會 root.destroy()），所以
        這個 fixture 必須用自己專屬的 root，不能沿用共用的 `gui`——共用的
        那個視窗還要留給同一輪測試裡其他 class 使用，不能被這裡銷毀。

        _on_close() 內部依序：set 收工旗標 -> _active_scanner.request_stop()
        -> （ctrl.connected 時）stop()+disconnect() -> （meter 存在時）
        close() -> root.destroy()。ctrl.ser 全程是 None，stop()/
        disconnect() 對它安全（皆有守衛），meter 未設定，整段可以放心
        直接呼叫真正的 _on_close()，不需要另外 monkeypatch 掉它。
        """
        recording_dir = tmp_path_factory.mktemp("scan_onclose_rec")
        scan_dir = tmp_path_factory.mktemp("scan_onclose_scan")
        root, g, patchers = make_gui(
            recording_dir,
            extra_patches=[patch("core.fiber_scanner._default_scan_dir", return_value=scan_dir)],
        )

        class FakeScanner:
            def __init__(self):
                self.stop_requested = False

            def request_stop(self):
                self.stop_requested = True

        fake_scanner = FakeScanner()
        g._active_scanner = fake_scanner
        g.ctrl.ems_active = False
        was_set = g._active_scanner is fake_scanner

        g._on_close()

        for p in patchers:
            try:
                p.stop()
            except RuntimeError:
                pass
        return fake_scanner, was_set

    def test_active_scanner_assigned_before_close(self, closed_gui):
        """案例 26-setup：_active_scanner 已設為假物件。"""
        _, was_set = closed_gui
        assert was_set

    def test_on_close_requests_scanner_stop(self, closed_gui):
        """案例 26：_on_close() 呼叫了 _active_scanner.request_stop()。"""
        fake_scanner, _ = closed_gui
        assert fake_scanner.stop_requested is True


# =============================================================================
# 十二、全域停止／中斷連線入口通知背景尋光執行緒（案例 27）
# =============================================================================
class TestGlobalStopEntriesNotifyScanner:
    """
    architect 2026-08-31 審查抓到：_on_close() 已經會呼叫
    _active_scanner.request_stop()（案例 26），但 _do_stop()（移動控制分頁
    「■ Stop」）、_on_escape()（Esc 快捷鍵）、_toggle_connect() 的中斷連線
    分支這三個入口原本漏接。尋光背景執行緒（fiber_scanner._check_abort()）
    只看 request_stop() 設的 _stop_event，不看 ctrl.stop() 有沒有送出，
    少了這行，使用者按下停止/中斷後尋光仍會在下一輪迴圈送出下一步指令。
    """

    class FakeScanner:
        def __init__(self):
            self.stop_requested = False

        def request_stop(self):
            self.stop_requested = True

    def test_do_stop_requests_scanner_stop(self, gui):
        """案例 27a：_do_stop() 呼叫了 _active_scanner.request_stop()。"""
        root, g = gui
        fake_scanner = self.FakeScanner()
        g._active_scanner = fake_scanner
        try:
            g._do_stop()
            assert fake_scanner.stop_requested is True
        finally:
            g._active_scanner = None

    def test_on_escape_requests_scanner_stop(self, gui):
        """案例 27b：_on_escape() 呼叫了 _active_scanner.request_stop()。"""
        root, g = gui
        fake_scanner = self.FakeScanner()
        g._active_scanner = fake_scanner
        saved_connected = g.ctrl.connected
        g.ctrl.connected = True
        try:
            with patch.object(g, "_flash_banner"):
                g._on_escape()
            assert fake_scanner.stop_requested is True
        finally:
            g._active_scanner = None
            g.ctrl.connected = saved_connected

    def test_toggle_connect_disconnect_requests_scanner_stop(self, gui):
        """
        案例 27c：_toggle_connect() 的中斷連線分支呼叫了 _active_scanner.request_stop()。

        2026-09-03 architect 審查後，stop()/disconnect() 搬進背景執行緒，
        UI 更新透過 root.after() 回主執行緒——必須用 pump_until() 讓
        mainloop 真的轉起來，callback 才會被執行（見 conftest.pump_until
        docstring）。request_stop() 分派仍在主執行緒同步呼叫，不受影響。
        """
        root, g = gui
        fake_scanner = self.FakeScanner()
        g._active_scanner = fake_scanner
        saved_connected = g.ctrl.connected
        g.ctrl.connected = True
        try:
            g._toggle_connect()
            assert fake_scanner.stop_requested is True
            # 中斷期間按鈕應立即鎖住，避免連點疊出第二條中斷執行緒。
            assert str(g._conn_btn["state"]) == "disabled"
            # 等按鈕狀態本身回到 normal，而不是等 ctrl.connected 翻 False——
            # 後者在 disconnect() 內部就已翻好，早於 _on_disconnect_result
            # 這個 after callback 真正執行，用它當條件會有競態，偶爾在
            # callback 執行前就通過。
            pump_until(root, lambda: str(g._conn_btn["state"]) == "normal")
            assert g.ctrl.connected is False
        finally:
            g._active_scanner = None
            g.ctrl.connected = saved_connected


# =============================================================================
# 十三、長時間背景作業註冊表（案例 28，2026-08-31 前置1）
# =============================================================================
class TestLongOperationRegistry:
    """
    architect 2026-08-31 前置1：把「長時間背景作業」變成第一類概念
    （main_ai.LongOperation ＋ DS102GUI._register_long_ops()）。

    在這之前，每種長時間作業都要作者自己記得手動接進 6 個散落的位置
    （_update_stat_ui 的 busy、_start_poller 的經過時間、_do_stop、
    _on_escape、_toggle_connect、_on_close），而且實際上已經漏過三次
    （案例 26／27 就是在補那些漏接）。這一組測試鎖住註冊表的三個約定：

      1. 四項既有作業都在表上（少一項就等於某個入口又會漏接）；
      2. 停止分派是「無條件、逐項獨立」的——不先看 is_running()，
         且任何一項拋例外都不可以吃掉其餘作業的停止請求；
      3. 經過時間只在作業進行中更新，收工後保留最後一次的值
         （_on_scan_done 的完成橫幅要讀 _scan_elapsed_var.get()）。
    """

    EXPECTED_KEYS = {"playback", "homing", "scan", "org_repeat"}

    def test_registry_covers_all_known_long_ops(self, gui):
        """案例 28a：註冊表涵蓋重播／全軸復歸／尋光／原點復歸重現性量測四項。"""
        _, g = gui
        assert {op.key for op in g._long_ops} == self.EXPECTED_KEYS

    def test_homing_has_no_request_stop(self, gui):
        """
        案例 28b：全軸原點復歸的 request_stop 是 None（不是漏寫）。

        ctrl.origin_all() 不收 stop_event，沒有任何軟體旗標能讓它提前收工；
        註冊表必須把「沒有停止機制」表達出來，而不是假裝有。
        """
        _, g = gui
        homing = next(op for op in g._long_ops if op.key == "homing")
        assert homing.request_stop is None

    def test_any_long_op_running_follows_each_flag(self, gui):
        """案例 28c：四個旗標分別 set 時，_any_long_op_running() 都要成立。"""
        _, g = gui
        assert g._any_long_op_running() is False
        for ev in (g._homing, g._scanning, g._org_repeat_running):
            ev.set()
            try:
                assert g._any_long_op_running() is True
            finally:
                ev.clear()
        assert g._any_long_op_running() is False

    def test_busy_reasons_reports_labels(self, gui):
        """案例 28d：_busy_reasons() 說得出正在忙哪一項。"""
        _, g = gui
        g._scanning.set()
        try:
            assert g._busy_reasons() == ["尋光"]
        finally:
            g._scanning.clear()
        assert g._busy_reasons() == []

    def test_request_stop_dispatches_to_all_targets(self, gui):
        """
        案例 28e：_request_stop_long_ops() 一次分派到三個停止目標，
        且**不以 is_running() 為前提**（旗標全部是 clear 的狀態下仍分派）。

        這是刻意的設計：進行中旗標與停止目標不是同一個物件，兩者的
        set/clear 有極短的交錯窗口（_scanning.set() 早於 _active_scanner
        指派），拿 is_running() 當閘門等於把該窗口變成漏接窗口。
        """
        _, g = gui
        fake_scanner = TestGlobalStopEntriesNotifyScanner.FakeScanner()
        g._active_scanner = fake_scanner
        g._stop_playback.clear()
        g._org_repeat_stop_event.clear()
        try:
            g._request_stop_long_ops()
            assert g._stop_playback.is_set() is True
            assert g._org_repeat_stop_event.is_set() is True
            assert fake_scanner.stop_requested is True
        finally:
            g._active_scanner = None
            g._stop_playback.clear()
            g._org_repeat_stop_event.clear()

    def test_one_failing_stop_does_not_block_the_others(self, gui):
        """
        案例 28f：某一項 request_stop 拋例外時，其餘作業仍收得到停止請求。

        失效情境：尋光的 request_stop 因任何原因拋例外（例如 scanner 物件
        狀態異常），若分派迴圈沒有逐項 try/except，排在它後面的
        原點復歸重現性量測就永遠等不到停止旗標——使用者按下停止之後，
        量測執行緒會繼續對硬體送下一輪指令。
        """
        _, g = gui

        class ExplodingScanner:
            def request_stop(self):
                raise RuntimeError("故意炸給測試看")

        g._active_scanner = ExplodingScanner()
        g._stop_playback.clear()
        g._org_repeat_stop_event.clear()
        try:
            g._request_stop_long_ops()  # 不可往外拋
            assert g._stop_playback.is_set() is True
            assert g._org_repeat_stop_event.is_set() is True
        finally:
            g._active_scanner = None
            g._stop_playback.clear()
            g._org_repeat_stop_event.clear()

    def test_elapsed_updates_only_while_running(self, gui):
        """
        案例 28g：經過時間只在作業進行中更新，收工後保留最後一次的值。

        後半段是真的會出事的部分：_on_scan_done() 的完成橫幅會讀
        _scan_elapsed_var.get() 把總耗時寫進訊息，若收工時歸零或繼續累加，
        那則訊息就會顯示錯誤的耗時。
        """
        _, g = gui
        g._scan_elapsed_var.set("--:--")
        g._update_long_op_elapsed()
        assert g._scan_elapsed_var.get() == "--:--", "未進行中不該被更新"

        g._scan_start_time = time.time() - 125  # 2 分 5 秒前開始
        g._scanning.set()
        try:
            g._update_long_op_elapsed()
            assert g._scan_elapsed_var.get() == "02:05"
        finally:
            g._scanning.clear()
            g._scan_start_time = 0.0

        # 收工後再跑一輪，值必須原地不動（而不是被歸零或繼續累加）
        g._update_long_op_elapsed()
        assert g._scan_elapsed_var.get() == "02:05"

    def test_org_repeat_elapsed_var_is_late_bound(self, gui):
        """
        案例 28h：原點復歸重現性量測的 show_elapsed 是延後求值。

        _org_repeat_elapsed_var 要等 _build_card_origin_repeatability() 才
        建立，而 _register_long_ops() 跑在 _build_notebook() 之前。若當初
        寫成 `self._org_repeat_elapsed_var.set`（綁定方法），註冊當下就會
        AttributeError；寫成 lambda 才能在這裡真的把值寫進去。
        """
        _, g = gui
        g._org_repeat_elapsed_var.set("--:--")
        g._org_repeat_start_time = time.time() - 61
        g._org_repeat_running.set()
        try:
            g._update_long_op_elapsed()
            assert g._org_repeat_elapsed_var.get() == "01:01"
        finally:
            g._org_repeat_running.clear()
            g._org_repeat_start_time = 0.0
            g._org_repeat_elapsed_var.set("00:00")

    def test_global_stop_entries_go_through_the_registry(self, gui):
        """
        案例 28i：_do_stop() / _on_escape() 都會讓重播與量測旗標一起被設。

        案例 27 只驗了尋光那一項；這裡補驗同一次呼叫也涵蓋另外兩項，
        確認三個入口確實改走註冊表、而不是又各自接了一份手動接線。
        """
        _, g = gui
        saved_connected = g.ctrl.connected
        g.ctrl.connected = True
        g._stop_playback.clear()
        g._org_repeat_stop_event.clear()
        try:
            g._do_stop()
            assert g._stop_playback.is_set() is True
            assert g._org_repeat_stop_event.is_set() is True

            g._stop_playback.clear()
            g._org_repeat_stop_event.clear()
            with patch.object(g, "_flash_banner"):
                g._on_escape()
            assert g._stop_playback.is_set() is True
            assert g._org_repeat_stop_event.is_set() is True
        finally:
            g._stop_playback.clear()
            g._org_repeat_stop_event.clear()
            g.ctrl.connected = saved_connected
