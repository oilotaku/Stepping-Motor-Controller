# -*- coding: utf-8 -*-
"""
光功率顯示面板回歸測試（pytest，假物件，不碰真實硬體）。

原本是獨立可執行、用 assert 陳述句、失敗時印出案例名稱與預期/實際值的
回歸腳本，2026-08 轉換為 pytest 測試檔以便 VS Code Test Explorer 個別
發現、個別重跑單一案例。案例編號沿用原腳本的分組（見下方各 class 開頭
註解對應「案例 N」），總計 66 項斷言、一項不少——每個原本的 check()
呼叫都對應一個獨立的 `def test_xxx():`，沒有把多個案例合併進同一個
測試函式，確保 `pytest -v` 的項目數與原腳本的「共 66 項」一一對應。

原腳本裡有些案例共用同一個 gui、依賴彼此執行順序留下的狀態（例如案例
14b/14c 原本沿用案例 14a 連線後留下的 gui.meter）。為了讓每個測試函式
都能在 VS Code 裡被單獨選取、單獨重跑而不必依賴其他測試先跑過，這裡
把這類情境改成各自在自己的測試/fixture 內重新建立必要的前置狀態
（詳見對應函式的註解），驗證的斷言邏輯與涵蓋範圍未改變。

不連真實 GPIB / pyvisa，不觸發真的 _toggle_meter_connect()，不修改
recordings/ 目錄裡使用者既有的設定檔內容（第二層的 gui fixture額外把
main_ai.RECORDING_DIR 導向暫存目錄，比原腳本更進一步——原腳本沒有
patch，DS102GUI() 建構時會讀專案真正的 recordings/ 設定檔，雖然只讀
不寫、原本就不違反安全規則，但這裡加上暫存目錄後，測試結果徹底不受
專案裡目前有哪些既有設定檔內容影響）。

涵蓋兩層：
  1. meter_GPIB.HP8153APowerMeter：overflow 判斷、指令字串格式、鎖互斥
  2. main_ai.DS102GUI 的光功率面板狀態機：連線/斷線/讀值狀態轉移、
     COMM_FAIL_THRESHOLD 附近的行為、_apply_meter_range 是否真的非同步

執行方式（VS Code Test Explorer 或指令列皆可）：
    venv/Scripts/python.exe -m pytest verify_meter_panel.py -v
    venv/Scripts/python.exe -m pytest verify_meter_panel.py::TestGetPowerOverflowBoundary -v
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import main_ai

from conftest import close_gui, make_gui, pump_until


# =============================================================================
# 第一層：meter_GPIB.HP8153APowerMeter（假 pyvisa，不需要 tkinter）
# =============================================================================
class FakeInstrument:
    """假的 pyvisa instrument resource。"""

    def __init__(self):
        self.write_calls = []
        self.query_responses = {}  # cmd -> response string (exact match)
        self.default_query_response = "0.0"
        self.close_should_raise = False
        self.closed = False
        self.timeout = None
        # 供併發測試使用的事件序列
        self.event_log = None  # list，由測試案例注入
        self.query_gate = None  # (start_event, release_event) 供慢查詢模擬用

    def write(self, cmd):
        self.write_calls.append(cmd)

    def query(self, cmd):
        if self.query_gate is not None:
            start_evt, release_evt = self.query_gate
            if self.event_log is not None:
                self.event_log.append("query_start")
            start_evt.set()
            release_evt.wait(timeout=5)
            if self.event_log is not None:
                self.event_log.append("query_end")
        return self.query_responses.get(cmd, self.default_query_response)

    def close(self):
        if self.event_log is not None:
            self.event_log.append("close_effect")
        if self.close_should_raise:
            raise RuntimeError("模擬 close 失敗")
        self.closed = True


def make_meter(fake_instrument=None, gpib_address=21, channel=1, wavelength_nm=1550):
    """
    用 patch("pyvisa.ResourceManager") 建立一個 HP8153APowerMeter，
    內部的 self.instrument 換成我們可控的 FakeInstrument。
    """
    if fake_instrument is None:
        fake_instrument = FakeInstrument()
    fake_rm = MagicMock()
    fake_rm.open_resource.return_value = fake_instrument
    with patch("pyvisa.ResourceManager", return_value=fake_rm):
        import meter_GPIB
        meter = meter_GPIB.HP8153APowerMeter(
            gpib_address=gpib_address, channel=channel, wavelength_nm=wavelength_nm
        )
    return meter, fake_instrument, fake_rm


class TestGetPowerOverflowBoundary:
    """案例 1a ~ 1b-5：get_power() 的數值解析與 overflow/underflow 邊界。"""

    def test_valid_reading_returns_ok_true(self):
        """案例 1a：get_power() 正常數值 -> (True, -12.34)。"""
        meter, inst, rm = make_meter()
        inst.query_responses[f":READ{meter.ch}:POW?"] = "-12.34"
        ok, val = meter.get_power()
        assert ok is True
        assert val == -12.34

    def test_overflow_sentinel_returns_ok_false(self):
        """案例 1b：get_power() overflow sentinel -> ok=False。"""
        meter, inst, rm = make_meter()
        inst.query_responses[f":READ{meter.ch}:POW?"] = "+9.9E+37"
        ok, val = meter.get_power()
        assert ok is False

    def test_underflow_sentinel_negative_returns_ok_false(self):
        """案例 1b-2：get_power() underflow sentinel(負值) -> ok=False。"""
        meter, inst, rm = make_meter()
        inst.query_responses[f":READ{meter.ch}:POW?"] = "-9.9E+37"
        ok, val = meter.get_power()
        assert ok is False

    def test_value_just_below_threshold_is_valid(self):
        """案例 1b-3：get_power() 略小於門檻(9e29) -> ok=True（門檻邊界）。"""
        meter, inst, rm = make_meter()
        inst.query_responses[f":READ{meter.ch}:POW?"] = "9e29"  # < 1e30
        ok, val = meter.get_power()
        assert ok is True
        assert val == 9e29

    def test_unparseable_value_returns_false_zero(self):
        """案例 1b-4：get_power() 無法解析(N/A) -> (False, 0.0)。"""
        meter, inst, rm = make_meter()
        inst.query_responses[f":READ{meter.ch}:POW?"] = "N/A"
        ok, val = meter.get_power()
        assert ok is False
        assert val == 0.0

    def test_visa_io_error_returns_false_zero_not_raised(self):
        """
        案例 1b-5：get_power() VisaIOError -> (False, 0.0) 不往外拋。

        注意：建構子本身也會呼叫 _query("*IDN?")，所以不能讓假 instrument
        從一開始就對所有 query 拋例外（那會讓連線階段本身失敗）。先讓
        連線正常完成，再把 query 換成會拋例外的版本，只測 get_power()
        這條路徑。
        """
        import pyvisa

        meter, inst, rm = make_meter()

        def raising_query(cmd):
            raise pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)

        inst.query = raising_query
        ok, val = meter.get_power()  # 不應拋例外
        assert ok is False
        assert val == 0.0


class TestInstrumentCommandFormat:
    """案例 2a ~ 2d：set_wavelength / set_range / set_range_auto 的指令字串格式。"""

    def test_set_wavelength_command_format(self):
        """案例 2a：set_wavelength(1310) 送出正確格式。"""
        meter, inst, rm = make_meter(channel=2)
        inst.write_calls.clear()
        meter.set_wavelength(1310)
        assert ":SENS2:POW:WAVE 1310NM" in inst.write_calls

    def test_set_range_command_format(self):
        """案例 2b：set_range(-30) 送出正確格式。"""
        meter, inst, rm = make_meter(channel=2)
        inst.write_calls.clear()
        meter.set_range(-30)
        assert ":SENS2:POW:RANG -30DBM" in inst.write_calls

    def test_set_range_auto_command_format(self):
        """案例 2c：set_range_auto(True/False) 送出正確格式。"""
        meter, inst, rm = make_meter(channel=2)
        inst.write_calls.clear()
        meter.set_range_auto(True)
        meter.set_range_auto(False)
        assert ":SENS2:POW:RANG:AUTO ON" in inst.write_calls
        assert ":SENS2:POW:RANG:AUTO OFF" in inst.write_calls

    def test_channel_one_uses_sens1_prefix(self):
        """案例 2d：channel=1 時指令帶 SENS1。"""
        meter, inst, rm = make_meter(channel=1)
        inst.write_calls.clear()
        meter.set_wavelength(1550)
        assert ":SENS1:POW:WAVE 1550NM" in inst.write_calls


class TestCloseWaitsForSlowQuery:
    """案例 3a、3a-2、3a-3：close() 與慢查詢互斥（曾經修過的 bug）。"""

    @pytest.fixture(scope="class")
    @classmethod
    def close_vs_query_result(cls):
        """
        只執行一次真正的執行緒編排（啟動慢查詢 -> 確認它已卡在
        critical section -> 啟動 close() -> 驗證 close() 沒有搶先跑
        -> 釋放慢查詢 -> 驗證兩者的先後順序），供 3a/3a-2/3a-3 三個
        獨立斷言案例共用。
        """
        events = []
        query_start_evt = threading.Event()
        release_query_evt = threading.Event()
        meter, inst, rm = make_meter()
        inst.event_log = events
        inst.query_gate = (query_start_evt, release_query_evt)

        def slow_query():
            meter._query(":SLOW?")

        t_query = threading.Thread(target=slow_query)
        t_query.start()
        # 等慢查詢真的進入 critical section 並卡住
        assert query_start_evt.wait(timeout=5), "慢查詢執行緒沒有在時限內進入 query"

        def do_close():
            meter.close()  # 實際生效時機以 FakeInstrument.close() 內 append 的
            # "close_effect" 為準——不能在呼叫前就記事件，那只代表執行緒
            # 「開始嘗試」，不代表真的搶到鎖、進了 critical section。

        t_close = threading.Thread(target=do_close)
        t_close.start()
        # 給 close 執行緒一點時間，讓它有機會（若沒鎖保護）搶進去
        time.sleep(0.3)
        # 此時 close 應該還卡在鎖外面，還沒 append "close_effect"
        order_before_release = list(events)
        release_query_evt.set()
        t_query.join(timeout=5)
        t_close.join(timeout=5)

        return {
            "events": events,
            "order_before_release": order_before_release,
            "instrument_closed": inst.closed,
        }

    def test_close_waits_for_slow_query_event_order(self, close_vs_query_result):
        """案例 3a：close() 等待慢查詢釋放鎖，事件順序正確。"""
        assert close_vs_query_result["events"] == ["query_start", "query_end", "close_effect"]

    def test_close_does_not_preempt_slow_query(self, close_vs_query_result):
        """案例 3a-2：close() 沒有在慢查詢完成前搶先執行。"""
        assert close_vs_query_result["order_before_release"] == ["query_start"]

    def test_close_actually_calls_instrument_close(self, close_vs_query_result):
        """案例 3a-3：close() 期間 instrument.close() 真的被呼叫。"""
        assert close_vs_query_result["instrument_closed"] is True


class TestConcurrentAccessNoOverlap:
    """案例 3b、3c：_query()/_write() 的鎖互斥，多執行緒併發不交錯。"""

    def test_concurrent_query_calls_do_not_overlap(self):
        """案例 3b：兩個並發 _query() 呼叫不會交錯進入 critical section。"""
        lock_probe = threading.Lock()
        overlap_detected = [False]
        in_critical = [False]

        meter, inst, rm = make_meter()

        def guarded_query(cmd):
            with lock_probe:
                if in_critical[0]:
                    overlap_detected[0] = True
                in_critical[0] = True
            time.sleep(0.05)  # 製造重疊機會
            with lock_probe:
                in_critical[0] = False
            return "1.23"

        inst.query = guarded_query

        threads = [threading.Thread(target=lambda: meter._query(":X?")) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert overlap_detected[0] is False

    def test_concurrent_write_and_query_do_not_overlap(self):
        """案例 3c：_write() 也受同一把鎖保護，不與 _query() 交錯。"""
        lock_probe = threading.Lock()
        overlap_detected = [False]
        in_critical = [False]

        meter, inst, rm = make_meter()

        def guarded_write(cmd):
            with lock_probe:
                if in_critical[0]:
                    overlap_detected[0] = True
                in_critical[0] = True
            time.sleep(0.05)
            with lock_probe:
                in_critical[0] = False

        def guarded_query(cmd):
            with lock_probe:
                if in_critical[0]:
                    overlap_detected[0] = True
                in_critical[0] = True
            time.sleep(0.05)
            with lock_probe:
                in_critical[0] = False
            return "1.0"

        inst.write = guarded_write
        inst.query = guarded_query

        mixed_threads = []
        for i in range(4):
            if i % 2 == 0:
                mixed_threads.append(threading.Thread(target=lambda: meter._write(":Y 1")))
            else:
                mixed_threads.append(threading.Thread(target=lambda: meter._query(":Y?")))
        for t in mixed_threads:
            t.start()
        for t in mixed_threads:
            t.join(timeout=5)

        assert overlap_detected[0] is False


# =============================================================================
# 第二層：main_ai.DS102GUI 光功率面板狀態機
# =============================================================================
class FakeMeter:
    """假的 HP8153APowerMeter，供 GUI 層測試用。"""

    def __init__(self):
        self.closed = False
        self.set_range_auto_calls = []
        self.set_range_calls = []
        self._range_auto_delay = 0.0
        self.last_error_detail = ""
        self.close_should_raise = False

    def get_power(self):
        return True, -10.0

    def close(self):
        if self.close_should_raise:
            raise RuntimeError("模擬 close 失敗")
        self.closed = True

    def set_range_auto(self, status):
        if self._range_auto_delay:
            time.sleep(self._range_auto_delay)
        self.set_range_auto_calls.append(status)

    def set_range(self, dbm):
        self.set_range_calls.append(dbm)


@pytest.fixture(scope="module")
def gui(tmp_path_factory):
    """
    整份「第二層」測試共用的單一 (root, gui)。除了下面明確標註的例外，
    其餘所有第二層測試都透過這個 fixture 取用同一個視窗（原因與
    verify_scan_tab.py 的同名 fixture 相同：在同一個 process 內快速連續
    建構多個 tk.Tk() 執行個體會偶發環境層級的 TclError，共用視窗把
    建構次數壓低，同時不影響「每個測試各自安排自己的前置狀態」這個
    獨立性）。
    """
    recording_dir = tmp_path_factory.mktemp("meter_panel_rec")
    root, g, patchers = make_gui(recording_dir)
    yield root, g
    # 收尾：避免背景 poll worker 在關閉期間亂摸最後一個測試留下的假物件
    # （原腳本在 test_layer2() 結尾也有同樣的收尾動作）。
    g.meter = None
    close_gui(root, g, patchers)


class TestMeterConnectSuccess:
    """案例 4a ~ 4f：_on_meter_connect_result() 連線成功狀態機。"""

    @pytest.fixture
    def connected_result(self, gui):
        root, g = gui
        fake_meter = FakeMeter()
        g._on_meter_connect_result(fake_meter, None, 21, 1, 1550)
        return g, fake_meter

    def test_meter_assigned(self, connected_result):
        """案例 4a：gui.meter is fake_meter。"""
        g, fake_meter = connected_result
        assert g.meter is fake_meter

    def test_power_var_shows_placeholder(self, connected_result):
        """案例 4b：連線後 power_var 顯示 —（尚未查詢過）。"""
        g, _ = connected_result
        assert g._pm_power_var.get() == "—"

    def test_unit_var_empty(self, connected_result):
        """案例 4c：連線後 unit_var 為空字串。"""
        g, _ = connected_result
        assert g._pm_unit_var.get() == ""

    def test_comm_failures_reset(self, connected_result):
        """案例 4d：連線後 comm_failures 歸零。"""
        g, _ = connected_result
        assert g._pm_comm_failures == 0

    def test_last_ok_time_reset(self, connected_result):
        """案例 4e：連線後 last_ok_time 歸零（不殘留上段連線時間戳）。"""
        g, _ = connected_result
        assert g._pm_last_ok_time == 0.0

    def test_ch_wl_var_shows_channel_and_wavelength(self, connected_result):
        """案例 4f：ch_wl_var 顯示 Ch1·1550nm。"""
        g, _ = connected_result
        assert g._pm_ch_wl_var.get() == "Ch1·1550nm"


class TestMeterConnectFailure:
    """案例 5a ~ 5c：_on_meter_connect_result() 連線失敗狀態機。"""

    @pytest.fixture
    def failed_connect_result(self, gui):
        root, g = gui
        g.meter = None  # 重置
        with patch("main_ai.messagebox.showerror") as mock_err:
            g._on_meter_connect_result(None, "模擬連線逾時", 21, 1, 1550)
        return g, mock_err

    def test_meter_stays_none(self, failed_connect_result):
        """案例 5a：連線失敗後 gui.meter is None。"""
        g, _ = failed_connect_result
        assert g.meter is None

    def test_showerror_called(self, failed_connect_result):
        """案例 5b：連線失敗跳出 showerror。"""
        _, mock_err = failed_connect_result
        assert mock_err.called

    def test_addr_entry_reenabled(self, failed_connect_result):
        """案例 5c：連線輸入框恢復可編輯 state=normal。"""
        g, _ = failed_connect_result
        assert str(g._pm_addr_entry.cget("state")) == "normal"


class TestMeterReadingStateMachine:
    """案例 6a ~ 6d-2：—/dBm 互斥、COMM_FAIL_THRESHOLD 邊界。"""

    @pytest.fixture(scope="class")
    @classmethod
    def state_machine_result(cls, gui):
        """
        這是一個連續的狀態機情境（有效讀值 -> 連續失敗未達門檻 -> 剛好
        達到門檻 -> 恢復），10 個檢查點全部來自同一段連續操作，只執行
        一次、依序記錄每個檢查點當下的狀態，供 10 個獨立斷言案例共用。
        """
        root, g = gui
        threshold = main_ai.COMM_FAIL_THRESHOLD
        fake_meter = FakeMeter()
        g.meter = fake_meter
        g._pm_comm_failures = 0

        result = {"threshold": threshold}

        g._on_meter_reading(True, -12.34)
        result["after_valid_power"] = g._pm_power_var.get()
        result["after_valid_unit"] = g._pm_unit_var.get()

        # 連續失敗，未達門檻
        for _ in range(threshold - 1):
            g._on_meter_reading(False, 0.0)
        result["below_threshold_power"] = g._pm_power_var.get()
        result["below_threshold_unit"] = g._pm_unit_var.get()

        # 再一次，恰好達到門檻
        banner_calls = []
        with patch.object(g, "_flash_banner", side_effect=lambda *a, **k: banner_calls.append((a, k))):
            g._on_meter_reading(False, 0.0)
        result["at_threshold_power"] = g._pm_power_var.get()
        result["at_threshold_unit"] = g._pm_unit_var.get()
        result["at_threshold_status"] = g._pm_status_var.get()
        result["at_threshold_banner_calls"] = banner_calls

        # 恢復
        g._on_meter_reading(True, -5.0)
        result["recovered_power"] = g._pm_power_var.get()
        result["recovered_unit"] = g._pm_unit_var.get()

        g.meter = None  # 收尾
        return result

    def test_valid_reading_updates_power_var(self, state_machine_result):
        """案例 6a：有效讀值 -> power_var == -12.34。"""
        assert state_machine_result["after_valid_power"] == "-12.34"

    def test_valid_reading_updates_unit_var(self, state_machine_result):
        """案例 6a-2：有效讀值 -> unit_var == dBm。"""
        assert state_machine_result["after_valid_unit"] == "dBm"

    def test_failures_below_threshold_keep_power_var(self, state_machine_result):
        """案例 6b：失敗次數(未達門檻) -> power_var 維持舊值。"""
        assert state_machine_result["below_threshold_power"] == "-12.34"

    def test_failures_below_threshold_keep_unit_var(self, state_machine_result):
        """案例 6b-2：失敗次數(未達門檻) -> unit_var 仍是 dBm。"""
        assert state_machine_result["below_threshold_unit"] == "dBm"

    def test_failures_at_threshold_blanks_power_var(self, state_machine_result):
        """案例 6c：失敗恰好達到門檻 -> power_var 變 —。"""
        assert state_machine_result["at_threshold_power"] == "—"

    def test_failures_at_threshold_blanks_unit_var(self, state_machine_result):
        """案例 6c-2：達門檻 -> unit_var 變空字串。"""
        assert state_machine_result["at_threshold_unit"] == ""

    def test_failures_at_threshold_status_message(self, state_machine_result):
        """案例 6c-3：達門檻 -> status_var 含「已停止更新」。"""
        assert "已停止更新" in state_machine_result["at_threshold_status"]

    def test_failures_at_threshold_triggers_banner(self, state_machine_result):
        """案例 6c-4：達門檻 -> 觸發 _flash_banner。"""
        assert len(state_machine_result["at_threshold_banner_calls"]) >= 1

    def test_recovery_updates_power_var(self, state_machine_result):
        """案例 6d：恢復後 power_var 顯示新數值。"""
        assert state_machine_result["recovered_power"] == "-5.00"

    def test_recovery_updates_unit_var(self, state_machine_result):
        """案例 6d-2：恢復後 unit_var 變回 dBm。"""
        assert state_machine_result["recovered_unit"] == "dBm"


class TestDelayedCallbackAfterDisconnect:
    """案例 7a ~ 7f：斷線後延遲回呼不能造成誤導性警報（曾經修過的關鍵 bug）。"""

    @pytest.fixture(scope="class")
    @classmethod
    def delayed_callback_result(cls, gui):
        """
        連續操作（累積失敗 -> 斷線 -> 模擬斷線前卡住、事後才回來的舊
        查詢結果），只執行一次，供 7a~7f 六個獨立斷言案例共用。
        """
        root, g = gui
        threshold = main_ai.COMM_FAIL_THRESHOLD
        assert threshold >= 3, "此案例假設門檻至少為 3，目前不是，需要調整腳本"

        fake_meter = FakeMeter()
        g.meter = fake_meter
        g._pm_comm_failures = 0
        g._pm_power_var.set("-1.00")
        g._pm_unit_var.set("dBm")

        g._on_meter_reading(False, 0.0)
        g._on_meter_reading(False, 0.0)
        result = {"after_two_failures_comm_failures": g._pm_comm_failures}

        g._disconnect_meter()
        result["after_disconnect_comm_failures"] = g._pm_comm_failures
        result["after_disconnect_meter_is_none"] = g.meter is None

        status_before = g._pm_status_var.get()
        banner_calls = []
        with patch.object(g, "_flash_banner", side_effect=lambda *a, **k: banner_calls.append((a, k))):
            g._on_meter_reading(False, 0.0)  # 模擬斷線前卡住、事後才回來的舊查詢結果
        result["status_before"] = status_before
        result["status_after_stale_callback"] = g._pm_status_var.get()
        result["stale_callback_banner_calls"] = banner_calls
        result["comm_failures_after_stale_callback"] = g._pm_comm_failures
        return result

    def test_two_failures_recorded(self, delayed_callback_result):
        """案例 7a：累積 2 次失敗(門檻至少 3)，尚未觸發橫幅時 comm_failures == 2。"""
        assert delayed_callback_result["after_two_failures_comm_failures"] == 2

    def test_disconnect_resets_comm_failures(self, delayed_callback_result):
        """案例 7b：_disconnect_meter() 後 comm_failures 歸零。"""
        assert delayed_callback_result["after_disconnect_comm_failures"] == 0

    def test_disconnect_clears_meter(self, delayed_callback_result):
        """案例 7c：_disconnect_meter() 後 gui.meter is None。"""
        assert delayed_callback_result["after_disconnect_meter_is_none"]

    def test_stale_callback_status_unchanged(self, delayed_callback_result):
        """案例 7d：斷線後的延遲回呼是 no-op：status_var 不變。"""
        r = delayed_callback_result
        assert r["status_after_stale_callback"] == r["status_before"]

    def test_stale_callback_no_banner(self, delayed_callback_result):
        """案例 7e：斷線後的延遲回呼是 no-op：不觸發 _flash_banner。"""
        assert len(delayed_callback_result["stale_callback_banner_calls"]) == 0

    def test_stale_callback_comm_failures_not_accumulated(self, delayed_callback_result):
        """案例 7f：斷線後的延遲回呼是 no-op：comm_failures 仍是 0（沒有被意外累加）。"""
        assert delayed_callback_result["comm_failures_after_stale_callback"] == 0


class TestDisconnectMeterReset:
    """案例 8a ~ 8f：_disconnect_meter() 的完整重置。"""

    @pytest.fixture(scope="class")
    @classmethod
    def disconnect_reset_result(cls, gui):
        root, g = gui
        fake_meter = FakeMeter()
        g.meter = fake_meter
        g._pm_power_var.set("-7.50")
        g._pm_unit_var.set("dBm")
        g._pm_auto_poll.set(True)

        g._disconnect_meter()

        return {
            "meter_is_none": g.meter is None,
            "power_var": g._pm_power_var.get(),
            "unit_var": g._pm_unit_var.get(),
            "auto_poll": g._pm_auto_poll.get(),
            "query_btn_state": str(g._pm_query_btn.cget("state")),
            "fake_meter_closed": fake_meter.closed,
        }

    def test_meter_cleared(self, disconnect_reset_result):
        """案例 8a：gui.meter is None。"""
        assert disconnect_reset_result["meter_is_none"]

    def test_power_var_reset(self, disconnect_reset_result):
        """案例 8b：power_var 重置為 —。"""
        assert disconnect_reset_result["power_var"] == "—"

    def test_unit_var_reset(self, disconnect_reset_result):
        """案例 8c：unit_var 重置為空字串。"""
        assert disconnect_reset_result["unit_var"] == ""

    def test_auto_poll_forced_off(self, disconnect_reset_result):
        """案例 8d：auto_poll 被強制關閉。"""
        assert disconnect_reset_result["auto_poll"] is False

    def test_query_button_disabled(self, disconnect_reset_result):
        """案例 8e：立即查詢按鈕回到 disabled。"""
        assert disconnect_reset_result["query_btn_state"] == "disabled"

    def test_fake_instrument_close_called(self, disconnect_reset_result):
        """案例 8f：假 instrument close() 真的被呼叫。"""
        assert disconnect_reset_result["fake_meter_closed"] is True


class TestApplyMeterRangeAsyncAuto:
    """案例 9a ~ 9f：_apply_meter_range() 在 auto 模式下不阻塞主執行緒。"""

    @pytest.fixture(scope="class")
    @classmethod
    def range_auto_result(cls, gui):
        """
        呼叫本身立即返回（背景執行緒做完事再用 self.root.after(0, ...)
        排回主執行緒）與「背景執行緒真正跑完後」是同一次呼叫的前後兩個
        時間點，只執行一次，供 9a~9f 六個獨立斷言案例共用。

        Python 3.14 的 tkinter 要求背景執行緒呼叫 self.root.after(0, ...)
        時，本執行緒必須正在跑 mainloop，因此等待背景執行緒跑完一律用
        conftest 的 pump_until()，不能只用 root.update() 迴圈（實測仍會
        拋 RuntimeError: main thread is not in main loop，導致
        _on_meter_range_result 永遠不會被呼叫、按鈕卡在 disabled）。
        """
        root, g = gui
        fake_meter = FakeMeter()
        fake_meter._range_auto_delay = 0.3
        g.meter = fake_meter
        g._pm_range_mode_var.set("auto")

        t0 = time.time()
        g._apply_meter_range()
        elapsed = time.time() - t0
        result = {
            "elapsed": elapsed,
            "btn_state_immediately": str(g._pm_apply_range_btn.cget("state")),
            "btn_text_immediately": str(g._pm_apply_range_btn.cget("text")),
        }

        pump_until(
            root,
            lambda: str(g._pm_apply_range_btn.cget("state")) == "normal",
            timeout=2.0,
        )

        result["btn_state_after"] = str(g._pm_apply_range_btn.cget("state"))
        result["btn_text_after"] = str(g._pm_apply_range_btn.cget("text"))
        result["set_range_auto_calls"] = list(fake_meter.set_range_auto_calls)
        return result

    def test_apply_range_returns_immediately(self, range_auto_result):
        """案例 9a：_apply_meter_range() 呼叫本身立即返回（遠小於 0.3s）。"""
        assert range_auto_result["elapsed"] < 0.15

    def test_apply_range_button_disabled_immediately(self, range_auto_result):
        """案例 9b：呼叫後立刻檢查按鈕已是 disabled。"""
        assert range_auto_result["btn_state_immediately"] == "disabled"

    def test_apply_range_button_text_immediately(self, range_auto_result):
        """案例 9c：呼叫後立刻檢查按鈕文字為「套用中...」。"""
        assert range_auto_result["btn_text_immediately"] == "套用中..."

    def test_apply_range_button_restored_after_completion(self, range_auto_result):
        """案例 9d：背景執行緒跑完後按鈕恢復 normal。"""
        assert range_auto_result["btn_state_after"] == "normal"

    def test_apply_range_button_text_restored(self, range_auto_result):
        """案例 9e：背景執行緒跑完後按鈕文字恢復「套用量程」。"""
        assert range_auto_result["btn_text_after"] == "套用量程"

    def test_apply_range_calls_set_range_auto_true(self, range_auto_result):
        """案例 9f：set_range_auto(True) 真的被呼叫（auto 模式）。"""
        assert range_auto_result["set_range_auto_calls"] == [True]


class TestApplyMeterRangeManualError:
    """案例 9g、9g-2：手動量程格式錯誤時不呼叫 set_range，且跳錯誤訊息。"""

    @pytest.fixture(scope="class")
    @classmethod
    def range_manual_error_result(cls, gui):
        root, g = gui
        fake_meter = FakeMeter()
        fake_meter.set_range = MagicMock()
        g.meter = fake_meter
        g._pm_range_mode_var.set("manual")
        g._pm_range_manual_var.set("abc")

        with patch("main_ai.messagebox.showerror") as mock_err:
            g._apply_meter_range()

        g.meter = None  # 收尾，避免背景 poll worker 在關閉期間亂摸假物件
        return fake_meter, mock_err

    def test_manual_format_error_does_not_call_set_range(self, range_manual_error_result):
        """案例 9g：手動量程格式錯誤不呼叫 set_range。"""
        fake_meter, _ = range_manual_error_result
        assert not fake_meter.set_range.called

    def test_manual_format_error_shows_error_dialog(self, range_manual_error_result):
        """案例 9g-2：手動量程格式錯誤跳出 showerror。"""
        _, mock_err = range_manual_error_result
        assert mock_err.called


class TestMeterConfigPersistence:
    """案例 10a：meter_config.json 持久化 —— 只做唯讀驗證，不寫檔。"""

    def test_meter_config_in_non_recording_json(self):
        """案例 10a：NON_RECORDING_JSON 包含 meter_config.json。"""
        assert "meter_config.json" in main_ai.NON_RECORDING_JSON


class TestGetLastPmValue:
    """案例 12a ~ 12c：_get_last_pm_value()（供 CSV dbm 欄位取用的快取讀值邏輯）。"""

    def test_no_meter_returns_none(self, gui):
        """案例 12a：未連線 -> _get_last_pm_value() 回傳 None。"""
        root, g = gui
        g.meter = None
        assert g._get_last_pm_value() is None

    def test_connected_below_threshold_returns_cached_value(self, gui):
        """案例 12b：已連線且未達失敗門檻 -> 回傳快取值。"""
        root, g = gui
        fake_meter = FakeMeter()
        g.meter = fake_meter
        g._pm_comm_failures = 0
        g._pm_last_value = -8.5
        try:
            assert g._get_last_pm_value() == -8.5
        finally:
            g.meter = None
            g._pm_comm_failures = 0
            g._pm_last_value = None

    def test_failures_at_threshold_returns_none(self, gui):
        """案例 12c：連續失敗達門檻 -> 回傳 None（不把過期數值當成當下讀值）。"""
        root, g = gui
        fake_meter = FakeMeter()
        g.meter = fake_meter
        g._pm_last_value = -8.5
        g._pm_comm_failures = main_ai.COMM_FAIL_THRESHOLD
        try:
            assert g._get_last_pm_value() is None
        finally:
            g.meter = None
            g._pm_comm_failures = 0
            g._pm_last_value = None


class TestRecordDataPointDbm:
    """案例 13a ~ 13c：_record_data_point() 的 dbm 欄位（CSV 資料記錄接上光功率）。"""

    def test_no_reading_gives_empty_string_dbm(self, gui):
        """案例 13a：無光功率讀值時 -> dbm 欄位為空字串（不是 None，DictWriter 才不會寫成 'None'）。"""
        root, g = gui
        g.meter = None
        g._pm_comm_failures = 0
        g._pm_last_value = None
        g.ctrl._data_log = []
        g.ctrl._record_data_point()
        assert g.ctrl._data_log[-1]["dbm"] == ""

    @pytest.fixture
    def recorded_point_with_reading(self, gui):
        root, g = gui
        fake_meter = FakeMeter()
        g.meter = fake_meter
        g._pm_comm_failures = 0
        g._pm_last_value = -3.21
        g.ctrl._data_log = []
        g.ctrl._record_data_point()
        entry = g.ctrl._data_log[-1]
        yield entry
        g.meter = None
        g._pm_comm_failures = 0
        g._pm_last_value = None
        g.ctrl._data_log = []

    def test_cached_reading_fills_dbm_field(self, recorded_point_with_reading):
        """案例 13b：有快取讀值時 -> dbm 欄位帶入該值。"""
        assert recorded_point_with_reading["dbm"] == -3.21

    def test_axis_positions_still_recorded(self, recorded_point_with_reading):
        """案例 13c：仍照常記錄各軸位置（未破壞既有欄位）。"""
        assert all(ax in recorded_point_with_reading for ax in main_ai.AXES)


class TestActionHistoryLogging:
    """
    案例 14a ~ 14d：光功率相關事件走統一 LOG 入口（進 action_history，
    不再只寫進 logger 檔案）——連線成功／讀值失聯與恢復／中斷時
    meter.close() 失敗 三種情境。
    """

    def test_connect_success_logs_connected(self, gui):
        """案例 14a：連線成功 -> action_history 有一筆含「已連線」的紀錄。"""
        root, g = gui
        g.ctrl.action_history = []
        fake_meter = FakeMeter()
        try:
            g._on_meter_connect_result(fake_meter, None, 21, 1, 1550)
            assert any("已連線" in e["msg"] for e in g.ctrl.action_history)
        finally:
            g.ctrl.action_history = []
            g.meter = None

    def test_reading_failure_at_threshold_logs_message(self, gui):
        """案例 14b：讀值連續失敗達門檻 -> action_history 有一筆含「連續讀不到」的紀錄。"""
        root, g = gui
        g.ctrl.action_history = []
        g.meter = FakeMeter()  # 獨立於案例 14a：自行指定 meter，不依賴其他測試先跑過
        g._pm_comm_failures = main_ai.COMM_FAIL_THRESHOLD - 1
        try:
            with patch.object(g, "_flash_banner"):
                g._on_meter_reading(False, 0.0)
            assert any("連續讀不到" in e["msg"] for e in g.ctrl.action_history)
        finally:
            g.ctrl.action_history = []
            g._pm_comm_failures = 0
            g.meter = None

    def test_reading_recovery_logs_message(self, gui):
        """案例 14c：讀值從失聯恢復 -> action_history 有一筆含「恢復正常」的紀錄。"""
        root, g = gui
        g.ctrl.action_history = []
        g.meter = FakeMeter()  # 獨立於案例 14a/14b：自行指定 meter
        g._pm_comm_failures = main_ai.COMM_FAIL_THRESHOLD
        try:
            g._on_meter_reading(True, -1.0)
            assert any("恢復正常" in e["msg"] for e in g.ctrl.action_history)
        finally:
            g.ctrl.action_history = []
            g._pm_comm_failures = 0
            g.meter = None

    def test_close_failure_logs_message(self, gui):
        """案例 14d：meter.close() 失敗 -> action_history 有一筆含「close 失敗」的紀錄。"""
        root, g = gui
        g.ctrl.action_history = []
        fake_meter = FakeMeter()
        fake_meter.close_should_raise = True
        g.meter = fake_meter
        try:
            g._disconnect_meter()
            assert any("close 失敗" in e["msg"] for e in g.ctrl.action_history)
        finally:
            g.ctrl.action_history = []
            g.meter = None
            g._pm_comm_failures = 0


class TestRegressionGuardValidity:
    """
    案例 11（regression 有效性驗證）：證明 TestDelayedCallbackAfterDisconnect
    （案例 7d/7e/7f）真的能抓到「斷線後延遲回呼」這個 bug，不是空洞斷言。
    """

    def test_broken_guard_would_have_missed_stale_callback_bug(self, gui):
        """
        暫時「拿掉」_on_meter_reading() 開頭 `if self.meter is None: return`
        這段守衛，證明拿掉之後會誤觸發橫幅——藉此證明案例 7d/7e/7f 的
        斷言方向是對的。做法：複製一份「拿掉開頭 guard」的
        _on_meter_reading 邏輯，綁到 gui 上當替身方法直接呼叫，不修改
        main_ai.py 原始碼檔案本身。
        """
        root, g = gui

        def broken_on_meter_reading(self, ok: bool, val: float):
            # 這是「拿掉 if self.meter is None: return」之後的版本
            self._pm_query_btn.config(text="立即查詢", state="normal")
            if ok:
                self._pm_comm_failures = 0
            else:
                self._pm_comm_failures += 1
                if self._pm_comm_failures == main_ai.COMM_FAIL_THRESHOLD:
                    self._flash_banner("⚠ 光功率讀值已停止更新，請檢查 GPIB 連線")
                    self._pm_status_var.set("⚠ 已停止更新")

        g.meter = None
        g._pm_comm_failures = main_ai.COMM_FAIL_THRESHOLD - 1
        banner_calls = []
        try:
            with patch.object(g, "_flash_banner", side_effect=lambda *a, **k: banner_calls.append(a)):
                broken_on_meter_reading(g, False, 0.0)
            triggered_bug = len(banner_calls) > 0 and "已停止更新" in g._pm_status_var.get()
            assert triggered_bug, "拿掉 guard 的版本應該要誤觸發橫幅，若沒有觸發代表這個 regression 測試本身失去意義"
        finally:
            g._pm_comm_failures = 0
            g._pm_status_var.set("未連線")
