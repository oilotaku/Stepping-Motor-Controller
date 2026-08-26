# -*- coding: utf-8 -*-
"""
移動控制分頁「Position:」與 StatusBar／儀表板座標同步（2026-08-26）回歸測試
（pytest，假物件/暫存目錄，不碰真實硬體、不碰真實 recordings/）。

背景——使用者回報「ORG 時分頁座標與狀態座標不同步」。根因是移動控制分頁
的「Position:」與其餘座標顯示是**兩條完全不同的供應鏈**：

  1. 它以前只由 `_poll_status()`／`_async_query()` 寫入，而 `_poll_status()`
     的迴圈只在 `status == "Driving"` 時續輪——復歸途中的「Detect origin」
     與壓到限位（樣式 5/6 本來就靠限位感測器定位）都不是 Driving，於是
     數字停在中途值不再更新。
  2. 全軸原點復歸（`_do_home_all` → `origin_all`）根本沒觸發過那兩條路徑，
     整趟復歸這顆數字完全不動；而復歸收尾會強制寫 `POS 0`，StatusBar／
     儀表板隨即跳到 0，兩邊差距最刺眼。
  3. 它們寫的是 `query_status()` 回傳的**機械座標**（未扣 offset），而
     StatusBar／儀表板顯示工作座標——設過工作原點之後兩者永遠差一個
     offset，跟有沒有在復歸無關。

修法是把這顆數字改由 `_redraw_positions()`（100ms 重繪迴圈）統一供應，
與其餘座標同一份快取、同一個座標系、同一個節奏。本檔案鎖住這個行為。

── 安全規則（見 conftest.py 開頭，既有測試檔案共同適用，這裡沿用）──
  - 不連真實硬體：用真的 DS102Controller() 實例（.ser 全程 None，從未
    connect() 過），需要控制器行為時逐一 monkeypatch 個別方法/屬性。
  - 不寫入真實 recordings/：一律透過 conftest.make_gui() 導向暫存目錄。
  - 全程不使用 winfo_ismapped()（root.withdraw() 之後恆為 False）。

執行方式：
    venv/Scripts/python.exe -m pytest verify_ctrl_pos_sync.py -v
"""

from unittest.mock import patch

import pytest

import main_ai

from conftest import close_gui, make_gui, pump_until


@pytest.fixture(scope="module")
def gui(tmp_path_factory):
    """一個 module 共用的 GUI（建 Tk 成本高，且這裡的測試彼此不留狀態）。"""
    rec_dir = tmp_path_factory.mktemp("recordings")
    root, g, patchers = make_gui(rec_dir)
    yield g
    close_gui(root, g, patchers)


@pytest.fixture(autouse=True)
def reset_ctrl_state(gui):
    """每個測試前把連線/座標/offset 狀態還原成乾淨的未連線狀態。"""
    ctrl = gui.ctrl
    ctrl.connected = False
    ctrl.comm_failures = 0
    ctrl.axis_count = 0
    ctrl.axis_no = "1"
    with ctrl._lock:
        for ax in main_ai.AXES:
            ctrl._positions_pulse[ax] = 0.0
            ctrl._offsets[ax] = 0.0
    ctrl.axis_calib.clear()
    yield


def _set_connected(ctrl, axis_count=3):
    ctrl.connected = True
    ctrl.axis_count = axis_count
    ctrl.comm_failures = 0


def _statusbar_text(gui, ax):
    """StatusBar（第一條）顯示的該軸座標文字。"""
    return gui._status_bars[0]._coord_labels[ax].cget("text")


# =============================================================================
# 一、兩處顯示同源同座標系
# =============================================================================
class TestPositionSourceUnified:

    def test_disconnected_shows_dash_not_zero(self, gui):
        """
        未連線一律「—」，絕不顯示 0——0 幾乎就落在限位開關上，顯示 0
        等於畫一個「軸壓在端點」的假座標（既有的顯示原則）。
        """
        gui._redraw_positions()
        assert gui._ctrl_pos_var.get() == "—"
        assert _statusbar_text(gui, "X") == "—"

    def test_matches_statusbar_when_connected(self, gui):
        """連線後兩處顯示同一個數字（含千分位格式）。"""
        ctrl = gui.ctrl
        _set_connected(ctrl)
        with ctrl._lock:
            ctrl._positions_pulse["X"] = 10613.0
        gui._redraw_positions()
        gui._status_bars[0].update_coords()
        assert gui._ctrl_pos_var.get() == "10,613"
        assert _statusbar_text(gui, "X") == "10,613"

    def test_shows_work_coord_not_machine_coord(self, gui):
        """
        設過工作原點之後顯示的是工作座標（機械 − offset），與 StatusBar
        一致——這正是修正前「永遠差一個 offset」的那項不同步。
        """
        ctrl = gui.ctrl
        _set_connected(ctrl)
        with ctrl._lock:
            ctrl._positions_pulse["X"] = 5000.0
            ctrl._offsets["X"] = 1500.0
        gui._redraw_positions()
        gui._status_bars[0].update_coords()
        assert gui._ctrl_pos_var.get() == "3,500"
        assert _statusbar_text(gui, "X") == "3,500"
        assert ctrl.positions_machine["X"] == 5000.0  # 機械座標本身沒被動到

    def test_follows_current_axis(self, gui):
        """切軸之後下一輪重繪就跟著切，不需要額外監聽軸切換事件。"""
        ctrl = gui.ctrl
        _set_connected(ctrl)
        with ctrl._lock:
            ctrl._positions_pulse["X"] = 100.0
            ctrl._positions_pulse["Y"] = -4145.0
        gui._redraw_positions()
        assert gui._ctrl_pos_var.get() == "100"
        ctrl.axis_no = "2"  # Y
        gui._redraw_positions()
        assert gui._ctrl_pos_var.get() == "-4,145"

    def test_axis_beyond_axis_count_shows_dash(self, gui):
        """當前軸超出實際軸數（例如 3 軸機選到 W）一律「—」，不顯示殘值。"""
        ctrl = gui.ctrl
        _set_connected(ctrl, axis_count=3)
        with ctrl._lock:
            ctrl._positions_pulse["W"] = 999.0
        ctrl.axis_no = "6"  # W
        gui._redraw_positions()
        assert gui._ctrl_pos_var.get() == "—"


# =============================================================================
# 二、ORG（原點復歸）情境的回歸鎖
# =============================================================================
class TestOrgScenario:

    def test_non_driving_status_does_not_freeze_position(self, gui):
        """
        復歸途中會出現「Detect origin」／限位等非 Driving 狀態（那正是
        _poll_status() 收工的條件）。收工之後座標仍必須跟著快取繼續更新。
        """
        ctrl = gui.ctrl
        _set_connected(ctrl)
        with ctrl._lock:
            ctrl._positions_pulse["X"] = 8000.0
        gui._redraw_positions()
        assert gui._ctrl_pos_var.get() == "8,000"

        with patch.object(ctrl, "query_status", return_value=("Detect origin", "8000")):
            gui._poll_status()
            pump_until(gui.root, lambda: not gui._poll_busy.is_set())

        # 復歸持續進行，背景 position worker 把新值寫進快取
        with ctrl._lock:
            ctrl._positions_pulse["X"] = 41266.0
        gui._redraw_positions()
        assert gui._ctrl_pos_var.get() == "41,266"

    def test_forced_zero_after_homing_is_reflected(self, gui):
        """
        復歸收尾會強制寫 POS 0（既有行為）。修正前 StatusBar 跳 0、分頁
        數字停在中途值，正是使用者看到的落差；現在兩處必須同時歸 0。
        """
        ctrl = gui.ctrl
        _set_connected(ctrl)
        with ctrl._lock:
            ctrl._positions_pulse["X"] = 7.0
        gui._redraw_positions()
        gui._status_bars[0].update_coords()
        assert gui._ctrl_pos_var.get() == "7"
        assert _statusbar_text(gui, "X") == "7"

        ctrl.set_position("1", "0")  # ser 為 None，只會更新內部快取
        gui._redraw_positions()
        gui._status_bars[0].update_coords()
        assert gui._ctrl_pos_var.get() == "0"
        assert _statusbar_text(gui, "X") == "0"

    def test_poll_status_no_longer_writes_position(self, gui):
        """
        回歸鎖：_poll_status() 只更新狀態文字，不得再自己寫座標——否則
        會有兩個寫入者用不同座標系互相覆蓋，畫面在移動中閃爍。
        """
        ctrl = gui.ctrl
        _set_connected(ctrl)
        with ctrl._lock:
            ctrl._positions_pulse["X"] = 1234.0
            ctrl._offsets["X"] = 200.0
        gui._redraw_positions()
        assert gui._ctrl_pos_var.get() == "1,034"

        # 回傳一個與快取無關的機械座標字串；它不該出現在畫面上
        with patch.object(ctrl, "query_status", return_value=("Stop", "99999")):
            gui._poll_status()
            pump_until(gui.root, lambda: not gui._poll_busy.is_set())
        assert gui._ctrl_pos_var.get() == "1,034"
        assert gui._ctrl_status_var.get() == "Stop"

    def test_async_query_no_longer_writes_position(self, gui):
        """同上，切軸時走的 _async_query() 也不得寫座標。"""
        ctrl = gui.ctrl
        _set_connected(ctrl)
        gui._ctrl_status_var.set("—")
        with ctrl._lock:
            ctrl._positions_pulse["X"] = 500.0
        gui._redraw_positions()
        assert gui._ctrl_pos_var.get() == "500"

        with patch.object(ctrl, "query_status", return_value=("Detect origin", "77777")):
            gui._async_query()
            pump_until(
                gui.root, lambda: gui._ctrl_status_var.get() == "Detect origin"
            )
        assert gui._ctrl_pos_var.get() == "500"


# =============================================================================
# 三、μm 估算附加顯示（格式相依）
# =============================================================================
class TestUmEstimate:

    def test_um_parses_thousands_separator(self, gui):
        """
        座標字串改由 _redraw_positions() 格式化後帶千分位逗號，
        _update_ctrl_pos_um() 必須照樣解析得出來（float("10,000") 會炸）。
        """
        ctrl = gui.ctrl
        _set_connected(ctrl)
        ctrl.axis_calib["X"] = {
            "lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1
        }
        with ctrl._lock:
            ctrl._positions_pulse["X"] = 10000.0
        gui._redraw_positions()
        assert gui._ctrl_pos_var.get() == "10,000"
        # 10000 pulse × 2.0 μm/pulse = 20000.0 μm
        assert gui._ctrl_pos_um_var.get() == "≈ 20,000.0 μm"

    def test_um_empty_when_disconnected(self, gui):
        """未連線時座標是「—」，μm 欄位必須是空字串而不是 0 或例外。"""
        ctrl = gui.ctrl
        ctrl.axis_calib["X"] = {
            "lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1
        }
        gui._redraw_positions()
        assert gui._ctrl_pos_var.get() == "—"
        assert gui._ctrl_pos_um_var.get() == ""

    def test_um_empty_without_calibration(self, gui):
        """沒有校正參數就不顯示（既有慣例：不猜測、不顯示 0）。"""
        ctrl = gui.ctrl
        _set_connected(ctrl)
        with ctrl._lock:
            ctrl._positions_pulse["X"] = 3000.0
        gui._redraw_positions()
        assert gui._ctrl_pos_var.get() == "3,000"
        assert gui._ctrl_pos_um_var.get() == ""


# =============================================================================
# 四、改寫座標的確認視窗（座標系不可混用）
# =============================================================================
class TestSetPositionDialog:

    def test_confirm_dialog_shows_machine_coord(self, gui):
        """
        set_position() 寫的是控制器 POS 暫存器＝機械座標，所以確認視窗的
        「由 X 改寫為 Y」必須拿機械座標，不能拿畫面上的工作座標。
        """
        ctrl = gui.ctrl
        _set_connected(ctrl)
        with ctrl._lock:
            ctrl._positions_pulse["X"] = 5000.0
            ctrl._offsets["X"] = 1500.0
        gui._redraw_positions()
        assert gui._ctrl_pos_var.get() == "3,500"  # 畫面是工作座標

        gui._setpos_var.set("0")
        seen = {}

        def _fake_ask(title, msg, **kw):
            seen["msg"] = msg
            return False  # 不確認，不會真的送出指令

        with patch.object(main_ai.messagebox, "askyesno", side_effect=_fake_ask):
            gui._do_set_position()
        assert "5,000" in seen["msg"]
        assert "3,500" not in seen["msg"]
        assert ctrl.positions_machine["X"] == 5000.0  # 確實沒有寫入
