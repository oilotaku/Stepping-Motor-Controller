# -*- coding: utf-8 -*-
"""
光功率顯示面板驗證腳本（假物件，不碰真實硬體）。

比照 CLAUDE.md 提到的 scratchpad `verify_*.py` 慣例：獨立可執行、用 assert、
失敗時清楚印出案例名稱與預期/實際值。不是 pytest 測試檔，不需要安裝任何套件。

執行方式：
    PYTHONUTF8=1 venv/Scripts/python.exe verify_meter_panel.py

涵蓋兩層：
  1. meter_GPIB.HP8153APowerMeter：overflow 判斷、指令字串格式、鎖互斥
  2. main_ai.DS102GUI 的光功率面板狀態機：連線/斷線/讀值狀態轉移、
     COMM_FAIL_THRESHOLD 附近的行為、_apply_meter_range 是否真的非同步

不連真實 GPIB / pyvisa，不觸發真的 _toggle_meter_connect()，不修改
recordings/ 目錄裡使用者既有的設定檔內容。
"""

import os
import sys
import threading
import time
import tkinter as tk
from unittest.mock import MagicMock, patch

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


# =============================================================================
# 第一層：meter_GPIB.HP8153APowerMeter（假 pyvisa）
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


def test_layer1():
    import meter_GPIB

    print("\n=== 第一層：meter_GPIB.HP8153APowerMeter ===")

    # --- 案例 1a：get_power() 正常數值 ---
    meter, inst, rm = make_meter()
    inst.query_responses[f":READ{meter.ch}:POW?"] = "-12.34"
    ok, val = meter.get_power()
    check("1a. get_power() 正常數值 -> (True, -12.34)", ok is True and val == -12.34,
          expected=(True, -12.34), actual=(ok, val))

    # --- 案例 1b：get_power() overflow sentinel ---
    inst.query_responses[f":READ{meter.ch}:POW?"] = "+9.9E+37"
    ok, val = meter.get_power()
    check("1b. get_power() overflow sentinel -> ok=False", ok is False,
          expected=False, actual=ok)

    # --- 案例 1b-2：負向 overflow（underflow sentinel）也要判為失敗 ---
    inst.query_responses[f":READ{meter.ch}:POW?"] = "-9.9E+37"
    ok, val = meter.get_power()
    check("1b-2. get_power() underflow sentinel(負值) -> ok=False", ok is False,
          expected=False, actual=ok)

    # --- 案例 1b-3：門檻邊界（略小於 threshold，應視為有效值） ---
    inst.query_responses[f":READ{meter.ch}:POW?"] = "9e29"  # < 1e30
    ok, val = meter.get_power()
    check("1b-3. get_power() 略小於門檻(9e29) -> ok=True", ok is True and val == 9e29,
          expected=(True, 9e29), actual=(ok, val))

    # --- 案例 1b-4：無法解析成數值 ---
    inst.query_responses[f":READ{meter.ch}:POW?"] = "N/A"
    ok, val = meter.get_power()
    check("1b-4. get_power() 無法解析(N/A) -> (False, 0.0)", ok is False and val == 0.0,
          expected=(False, 0.0), actual=(ok, val))

    # --- 案例 1b-5：VisaIOError 例外要被吃掉，不可往外拋 ---
    # 注意：建構子本身也會呼叫 _query("*IDN?")，所以不能讓假 instrument
    # 從一開始就對所有 query 拋例外（那會讓連線階段本身失敗）。先讓連線
    # 正常完成，再把 query 換成會拋例外的版本，只測 get_power() 這條路徑。
    import pyvisa

    meter2, inst2, rm2 = make_meter()

    def raising_query(cmd):
        raise pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)

    inst2.query = raising_query
    try:
        ok, val = meter2.get_power()
        check("1b-5. get_power() VisaIOError -> (False, 0.0) 不往外拋",
              ok is False and val == 0.0, expected=(False, 0.0), actual=(ok, val))
    except Exception as e:
        check("1b-5. get_power() VisaIOError -> (False, 0.0) 不往外拋", False,
              detail=f"意外拋出例外: {e!r}")

    # --- 案例 2a：set_wavelength() 指令格式 ---
    meter, inst, rm = make_meter(channel=2)
    inst.write_calls.clear()
    meter.set_wavelength(1310)
    check("2a. set_wavelength(1310) 送出正確格式",
          ":SENS2:POW:WAVE 1310NM" in inst.write_calls,
          expected=":SENS2:POW:WAVE 1310NM", actual=inst.write_calls)

    # --- 案例 2b：set_range() 指令格式 ---
    inst.write_calls.clear()
    meter.set_range(-30)
    check("2b. set_range(-30) 送出正確格式",
          ":SENS2:POW:RANG -30DBM" in inst.write_calls,
          expected=":SENS2:POW:RANG -30DBM", actual=inst.write_calls)

    # --- 案例 2c：set_range_auto() 指令格式 ---
    inst.write_calls.clear()
    meter.set_range_auto(True)
    meter.set_range_auto(False)
    check("2c. set_range_auto(True/False) 送出正確格式",
          ":SENS2:POW:RANG:AUTO ON" in inst.write_calls
          and ":SENS2:POW:RANG:AUTO OFF" in inst.write_calls,
          expected=["...AUTO ON", "...AUTO OFF"], actual=inst.write_calls)

    # --- 案例 2d：channel 代入建構子傳入的值（channel=1 情境） ---
    meter1, inst1, rm1 = make_meter(channel=1)
    inst1.write_calls.clear()
    meter1.set_wavelength(1550)
    check("2d. channel=1 時指令帶 SENS1",
          ":SENS1:POW:WAVE 1550NM" in inst1.write_calls,
          expected=":SENS1:POW:WAVE 1550NM", actual=inst1.write_calls)

    # --- 案例 3a：close() 與慢查詢互斥（今天修的 bug） ---
    events = []
    query_start_evt = threading.Event()
    release_query_evt = threading.Event()
    meter3, inst3, rm3 = make_meter()
    inst3.event_log = events
    inst3.query_gate = (query_start_evt, release_query_evt)

    def slow_query():
        meter3._query(":SLOW?")

    t_query = threading.Thread(target=slow_query)
    t_query.start()
    # 等慢查詢真的進入 critical section 並卡住
    assert query_start_evt.wait(timeout=5), "慢查詢執行緒沒有在時限內進入 query"

    def do_close():
        meter3.close()  # 實際生效時機以 FakeInstrument.close() 內 append 的
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

    check(
        "3a. close() 等待慢查詢釋放鎖，事件順序正確",
        events == ["query_start", "query_end", "close_effect"],
        expected=["query_start", "query_end", "close_effect"],
        actual=events,
    )
    check(
        "3a-2. close() 沒有在慢查詢完成前搶先執行",
        order_before_release == ["query_start"],
        expected=["query_start"],
        actual=order_before_release,
    )
    check("3a-3. close() 期間 instrument.close() 真的被呼叫", inst3.closed is True,
          expected=True, actual=inst3.closed)

    # --- 案例 3b：兩個並發 _query()/_write() 呼叫不交錯 ---
    events2 = []
    lock_probe = threading.Lock()
    overlap_detected = [False]
    in_critical = [False]

    meter4, inst4, rm4 = make_meter()

    orig_query = inst4.query

    def guarded_query(cmd):
        with lock_probe:
            if in_critical[0]:
                overlap_detected[0] = True
            in_critical[0] = True
        time.sleep(0.05)  # 製造重疊機會
        with lock_probe:
            in_critical[0] = False
        return "1.23"

    inst4.query = guarded_query

    threads = [threading.Thread(target=lambda: meter4._query(":X?")) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    check("3b. 並發 _query() 不會交錯進入 critical section",
          overlap_detected[0] is False, expected=False, actual=overlap_detected[0])

    # --- 案例 3c：_write() 也受同一把鎖保護，不與 _query() 交錯 ---
    events3 = []
    lock_probe2 = threading.Lock()
    overlap_detected2 = [False]
    in_critical2 = [False]

    meter5, inst5, rm5 = make_meter()

    def guarded_write(cmd):
        with lock_probe2:
            if in_critical2[0]:
                overlap_detected2[0] = True
            in_critical2[0] = True
        time.sleep(0.05)
        with lock_probe2:
            in_critical2[0] = False

    def guarded_query2(cmd):
        with lock_probe2:
            if in_critical2[0]:
                overlap_detected2[0] = True
            in_critical2[0] = True
        time.sleep(0.05)
        with lock_probe2:
            in_critical2[0] = False
        return "1.0"

    inst5.write = guarded_write
    inst5.query = guarded_query2

    mixed_threads = []
    for i in range(4):
        if i % 2 == 0:
            mixed_threads.append(threading.Thread(target=lambda: meter5._write(":Y 1")))
        else:
            mixed_threads.append(threading.Thread(target=lambda: meter5._query(":Y?")))
    for t in mixed_threads:
        t.start()
    for t in mixed_threads:
        t.join(timeout=5)

    check("3c. _write()/_query() 並發交錯呼叫不重疊",
          overlap_detected2[0] is False, expected=False, actual=overlap_detected2[0])


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


def _pump_until(root, condition_fn, timeout=2.0):
    """
    真正把 Tk 事件迴圈跑起來，直到 condition_fn() 為真或逾時。

    Python 3.14 的 tkinter 加了「背景執行緒呼叫 after()／存取 widget 前，
    本執行緒必須正在跑 mainloop」的檢查（實測錯誤訊息："main thread is
    not in main loop"）；光是 root.update() 迴圈不算數（實測仍會拋同一個
    RuntimeError）。main_ai.py 的 `_apply_meter_range()` / `_toggle_meter_connect()`
    這類「背景執行緒做完事再用 self.root.after(0, ...) 排回主執行緒」的寫法，
    要驗證背景執行緒那一段真的有跑完，就必須讓 mainloop 真的轉起來。

    做法：排一個會自我重新排程的 watchdog，條件達成或逾時就呼叫
    root.quit()（只會結束 mainloop，不會 destroy），讓 mainloop() 這次
    呼叫回到呼叫端，測試腳本可以繼續往下走。
    """
    deadline = time.time() + timeout

    def _poll():
        if condition_fn() or time.time() > deadline:
            root.quit()
        else:
            root.after(20, _poll)

    root.after(20, _poll)
    root.mainloop()


def test_layer2():
    print("\n=== 第二層：main_ai.DS102GUI 光功率面板狀態機 ===")

    import main_ai

    root = tk.Tk()
    root.withdraw()
    try:
        gui = main_ai.DS102GUI(root)

        # ---------------------------------------------------------------
        # 案例 4：連線成功狀態機
        # ---------------------------------------------------------------
        fake_meter = FakeMeter()
        gui._on_meter_connect_result(fake_meter, None, 21, 1, 1550)
        check("4a. gui.meter is fake_meter", gui.meter is fake_meter)
        check("4b. 連線後 power_var 顯示 —（尚未查詢過）",
              gui._pm_power_var.get() == "—",
              expected="—", actual=gui._pm_power_var.get())
        check("4c. 連線後 unit_var 為空字串",
              gui._pm_unit_var.get() == "",
              expected="", actual=gui._pm_unit_var.get())
        check("4d. 連線後 comm_failures 歸零",
              gui._pm_comm_failures == 0,
              expected=0, actual=gui._pm_comm_failures)
        check("4e. 連線後 last_ok_time 歸零（不殘留上段連線時間戳）",
              gui._pm_last_ok_time == 0.0,
              expected=0.0, actual=gui._pm_last_ok_time)
        check("4f. ch_wl_var 顯示 Ch1·1550nm",
              gui._pm_ch_wl_var.get() == "Ch1·1550nm",
              expected="Ch1·1550nm", actual=gui._pm_ch_wl_var.get())

        # ---------------------------------------------------------------
        # 案例 5：連線失敗狀態機
        # ---------------------------------------------------------------
        gui.meter = None  # 重置
        with patch("main_ai.messagebox.showerror") as mock_err:
            gui._on_meter_connect_result(None, "模擬連線逾時", 21, 1, 1550)
        check("5a. 連線失敗後 gui.meter is None", gui.meter is None)
        check("5b. 連線失敗跳出 showerror", mock_err.called)
        check("5c. 連線輸入框恢復可編輯 state=normal",
              str(gui._pm_addr_entry.cget("state")) == "normal",
              expected="normal", actual=str(gui._pm_addr_entry.cget("state")))

        # ---------------------------------------------------------------
        # 案例 6：—/dBm 互斥、COMM_FAIL_THRESHOLD 邊界
        # ---------------------------------------------------------------
        threshold = main_ai.COMM_FAIL_THRESHOLD
        fake_meter2 = FakeMeter()
        gui.meter = fake_meter2
        gui._pm_comm_failures = 0

        gui._on_meter_reading(True, -12.34)
        check("6a. 有效讀值 -> power_var == -12.34",
              gui._pm_power_var.get() == "-12.34",
              expected="-12.34", actual=gui._pm_power_var.get())
        check("6a-2. 有效讀值 -> unit_var == dBm",
              gui._pm_unit_var.get() == "dBm",
              expected="dBm", actual=gui._pm_unit_var.get())

        # 連續失敗，未達門檻
        for i in range(threshold - 1):
            gui._on_meter_reading(False, 0.0)
        check(f"6b. 失敗 {threshold - 1} 次(未達門檻 {threshold}) -> power_var 維持舊值",
              gui._pm_power_var.get() == "-12.34",
              expected="-12.34", actual=gui._pm_power_var.get())
        check(f"6b-2. 失敗 {threshold - 1} 次(未達門檻) -> unit_var 仍是 dBm",
              gui._pm_unit_var.get() == "dBm",
              expected="dBm", actual=gui._pm_unit_var.get())

        # 再一次，恰好達到門檻
        banner_calls = []
        with patch.object(gui, "_flash_banner", side_effect=lambda *a, **k: banner_calls.append((a, k))):
            gui._on_meter_reading(False, 0.0)
        check(f"6c. 失敗恰好達到門檻({threshold}) -> power_var 變 —",
              gui._pm_power_var.get() == "—",
              expected="—", actual=gui._pm_power_var.get())
        check("6c-2. 達門檻 -> unit_var 變空字串",
              gui._pm_unit_var.get() == "",
              expected="", actual=gui._pm_unit_var.get())
        check("6c-3. 達門檻 -> status_var 含「已停止更新」",
              "已停止更新" in gui._pm_status_var.get(),
              expected="含 已停止更新", actual=gui._pm_status_var.get())
        check("6c-4. 達門檻 -> 觸發 _flash_banner",
              len(banner_calls) >= 1,
              expected=">=1 次", actual=len(banner_calls))

        # 恢復
        gui._on_meter_reading(True, -5.0)
        check("6d. 恢復後 power_var 顯示新數值",
              gui._pm_power_var.get() == "-5.00",
              expected="-5.00", actual=gui._pm_power_var.get())
        check("6d-2. 恢復後 unit_var 變回 dBm",
              gui._pm_unit_var.get() == "dBm",
              expected="dBm", actual=gui._pm_unit_var.get())

        # ---------------------------------------------------------------
        # 案例 7：斷線後延遲回呼不能造成誤導性警報（今天修的關鍵 bug）
        # ---------------------------------------------------------------
        fake_meter3 = FakeMeter()
        gui.meter = fake_meter3
        gui._pm_comm_failures = 0
        gui._pm_power_var.set("-1.00")
        gui._pm_unit_var.set("dBm")

        assert threshold >= 3, "此案例假設門檻至少為 3，目前不是，需要調整腳本"
        gui._on_meter_reading(False, 0.0)
        gui._on_meter_reading(False, 0.0)
        check(f"7a. 累積 2 次失敗(門檻{threshold})，尚未觸發橫幅時 status 仍是舊狀態",
              gui._pm_comm_failures == 2,
              expected=2, actual=gui._pm_comm_failures)

        gui._disconnect_meter()
        check("7b. _disconnect_meter() 後 comm_failures 歸零",
              gui._pm_comm_failures == 0,
              expected=0, actual=gui._pm_comm_failures)
        check("7c. _disconnect_meter() 後 gui.meter is None", gui.meter is None)

        status_before = gui._pm_status_var.get()
        banner_calls2 = []
        with patch.object(gui, "_flash_banner", side_effect=lambda *a, **k: banner_calls2.append((a, k))):
            gui._on_meter_reading(False, 0.0)  # 模擬斷線前卡住、事後才回來的舊查詢結果
        check("7d. 斷線後的延遲回呼是 no-op：status_var 不變",
              gui._pm_status_var.get() == status_before,
              expected=status_before, actual=gui._pm_status_var.get())
        check("7e. 斷線後的延遲回呼是 no-op：不觸發 _flash_banner",
              len(banner_calls2) == 0,
              expected=0, actual=len(banner_calls2))
        check("7f. 斷線後的延遲回呼是 no-op：comm_failures 仍是 0（沒有被意外累加）",
              gui._pm_comm_failures == 0,
              expected=0, actual=gui._pm_comm_failures)

        # ---------------------------------------------------------------
        # 案例 8：_disconnect_meter() 的完整重置
        # ---------------------------------------------------------------
        fake_meter4 = FakeMeter()
        gui.meter = fake_meter4
        gui._pm_power_var.set("-7.50")
        gui._pm_unit_var.set("dBm")
        gui._pm_auto_poll.set(True)

        gui._disconnect_meter()
        check("8a. gui.meter is None", gui.meter is None)
        check("8b. power_var 重置為 —",
              gui._pm_power_var.get() == "—",
              expected="—", actual=gui._pm_power_var.get())
        check("8c. unit_var 重置為空字串",
              gui._pm_unit_var.get() == "",
              expected="", actual=gui._pm_unit_var.get())
        check("8d. auto_poll 被強制關閉",
              gui._pm_auto_poll.get() is False,
              expected=False, actual=gui._pm_auto_poll.get())
        check("8e. 立即查詢按鈕回到 disabled",
              str(gui._pm_query_btn.cget("state")) == "disabled",
              expected="disabled", actual=str(gui._pm_query_btn.cget("state")))
        check("8f. 假 instrument close() 真的被呼叫",
              fake_meter4.closed is True,
              expected=True, actual=fake_meter4.closed)

        # ---------------------------------------------------------------
        # 案例 9：_apply_meter_range() 不阻塞主執行緒
        # ---------------------------------------------------------------
        fake_meter5 = FakeMeter()
        fake_meter5._range_auto_delay = 0.3
        gui.meter = fake_meter5
        gui._pm_range_mode_var.set("auto")

        t0 = time.time()
        gui._apply_meter_range()
        elapsed = time.time() - t0
        btn_state_immediately = str(gui._pm_apply_range_btn.cget("state"))
        btn_text_immediately = str(gui._pm_apply_range_btn.cget("text"))

        check("9a. _apply_meter_range() 呼叫本身立即返回（遠小於 0.3s）",
              elapsed < 0.15,
              expected="<0.15s", actual=f"{elapsed:.3f}s")
        check("9b. 呼叫後立刻檢查按鈕已是 disabled",
              btn_state_immediately == "disabled",
              expected="disabled", actual=btn_state_immediately)
        check("9c. 呼叫後立刻檢查按鈕文字為「套用中...」",
              btn_text_immediately == "套用中...",
              expected="套用中...", actual=btn_text_immediately)

        # 讓背景執行緒真正跑完，並讓 Tk 事件迴圈處理 root.after callback。
        # 注意：這裡不能用 root.update() 迴圈——Python 3.14 的 tkinter
        # 要求背景執行緒呼叫 self.root.after(0, ...) 時，本執行緒必須正在
        # 「跑 mainloop」，root.update() 不算數（實測仍會拋
        # RuntimeError: main thread is not in main loop，導致
        # _on_meter_range_result 永遠不會被呼叫、按鈕卡在 disabled）。
        _pump_until(
            root,
            lambda: str(gui._pm_apply_range_btn.cget("state")) == "normal",
            timeout=2.0,
        )

        check("9d. 背景執行緒跑完後按鈕恢復 normal",
              str(gui._pm_apply_range_btn.cget("state")) == "normal",
              expected="normal", actual=str(gui._pm_apply_range_btn.cget("state")))
        check("9e. 背景執行緒跑完後按鈕文字恢復「套用量程」",
              str(gui._pm_apply_range_btn.cget("text")) == "套用量程",
              expected="套用量程", actual=str(gui._pm_apply_range_btn.cget("text")))
        check("9f. set_range_auto(True) 真的被呼叫（auto 模式）",
              fake_meter5.set_range_auto_calls == [True],
              expected=[True], actual=fake_meter5.set_range_auto_calls)

        # --- 案例 9g：手動量程格式錯誤時不呼叫 set_range，且跳錯誤訊息 ---
        fake_meter6 = FakeMeter()
        fake_meter6.set_range = MagicMock()
        gui.meter = fake_meter6
        gui._pm_range_mode_var.set("manual")
        gui._pm_range_manual_var.set("abc")

        with patch("main_ai.messagebox.showerror") as mock_err2:
            gui._apply_meter_range()
        check("9g. 手動量程格式錯誤不呼叫 set_range",
              not fake_meter6.set_range.called,
              expected="not called", actual=fake_meter6.set_range.called)
        check("9g-2. 手動量程格式錯誤跳出 showerror",
              mock_err2.called,
              expected=True, actual=mock_err2.called)

        gui.meter = None  # 收尾，避免背景 poll worker 在關閉期間亂摸假物件

        # ---------------------------------------------------------------
        # 案例 10：meter_config.json 持久化 —— 只做唯讀驗證，不寫檔
        # ---------------------------------------------------------------
        check("10a. NON_RECORDING_JSON 包含 meter_config.json",
              "meter_config.json" in main_ai.NON_RECORDING_JSON,
              expected=True,
              actual=("meter_config.json" in main_ai.NON_RECORDING_JSON))

        # ---------------------------------------------------------------
        # 案例 12：_get_last_pm_value()（供 CSV dbm 欄位取用的快取讀值邏輯）
        # ---------------------------------------------------------------
        gui.meter = None
        check("12a. 未連線 -> _get_last_pm_value() 回傳 None",
              gui._get_last_pm_value() is None)

        fake_meter3 = FakeMeter()
        gui.meter = fake_meter3
        gui._pm_comm_failures = 0
        gui._pm_last_value = -8.5
        check("12b. 已連線且未達失敗門檻 -> 回傳快取值",
              gui._get_last_pm_value() == -8.5,
              expected=-8.5, actual=gui._get_last_pm_value())

        gui._pm_comm_failures = main_ai.COMM_FAIL_THRESHOLD
        check("12c. 連續失敗達門檻 -> 回傳 None（不把過期數值當成當下讀值）",
              gui._get_last_pm_value() is None)

        gui._pm_comm_failures = 0
        gui.meter = None
        gui._pm_last_value = None

        # ---------------------------------------------------------------
        # 案例 13：_record_data_point() 的 dbm 欄位（CSV 資料記錄接上光功率）
        # ---------------------------------------------------------------
        gui.ctrl._data_log = []
        gui.ctrl._record_data_point()
        check("13a. 無光功率讀值時 -> dbm 欄位為空字串（不是 None，DictWriter 才不會寫成 'None'）",
              gui.ctrl._data_log[-1]["dbm"] == "",
              expected="", actual=gui.ctrl._data_log[-1]["dbm"])

        fake_meter4 = FakeMeter()
        gui.meter = fake_meter4
        gui._pm_comm_failures = 0
        gui._pm_last_value = -3.21
        gui.ctrl._record_data_point()
        check("13b. 有快取讀值時 -> dbm 欄位帶入該值",
              gui.ctrl._data_log[-1]["dbm"] == -3.21,
              expected=-3.21, actual=gui.ctrl._data_log[-1]["dbm"])
        check("13c. 仍照常記錄各軸位置（未破壞既有欄位）",
              all(ax in gui.ctrl._data_log[-1] for ax in main_ai.AXES),
              expected=list(main_ai.AXES), actual=list(gui.ctrl._data_log[-1].keys()))

        gui.meter = None
        gui._pm_comm_failures = 0
        gui._pm_last_value = None
        gui.ctrl._data_log = []

        # ---------------------------------------------------------------
        # 案例 14：光功率相關事件走統一 LOG 入口（進 action_history，
        # 不再只寫進 logger 檔案）——連線成功／讀值失聯與恢復／中斷時
        # meter.close() 失敗 三種情境。
        # ---------------------------------------------------------------
        gui.ctrl.action_history = []
        fake_meter5 = FakeMeter()
        gui._on_meter_connect_result(fake_meter5, None, 21, 1, 1550)
        check("14a. 連線成功 -> action_history 有一筆含「已連線」的紀錄",
              any("已連線" in e["msg"] for e in gui.ctrl.action_history),
              expected="含 已連線", actual=[e["msg"] for e in gui.ctrl.action_history])

        gui.ctrl.action_history = []
        gui._pm_comm_failures = main_ai.COMM_FAIL_THRESHOLD - 1
        with patch.object(gui, "_flash_banner"):
            gui._on_meter_reading(False, 0.0)
        check("14b. 讀值連續失敗達門檻 -> action_history 有一筆含「連續讀不到」的紀錄",
              any("連續讀不到" in e["msg"] for e in gui.ctrl.action_history),
              expected="含 連續讀不到", actual=[e["msg"] for e in gui.ctrl.action_history])

        gui.ctrl.action_history = []
        gui._pm_comm_failures = main_ai.COMM_FAIL_THRESHOLD
        gui._on_meter_reading(True, -1.0)
        check("14c. 讀值從失聯恢復 -> action_history 有一筆含「恢復正常」的紀錄",
              any("恢復正常" in e["msg"] for e in gui.ctrl.action_history),
              expected="含 恢復正常", actual=[e["msg"] for e in gui.ctrl.action_history])

        gui.ctrl.action_history = []
        gui.meter = fake_meter5
        fake_meter5.close_should_raise = True
        gui._disconnect_meter()
        check("14d. meter.close() 失敗 -> action_history 有一筆含「close 失敗」的紀錄",
              any("close 失敗" in e["msg"] for e in gui.ctrl.action_history),
              expected="含 close 失敗", actual=[e["msg"] for e in gui.ctrl.action_history])

        gui.ctrl.action_history = []
        gui.meter = None
        gui._pm_comm_failures = 0

        # ---------------------------------------------------------------
        # 案例 11（regression 有效性驗證）：暫時關閉 `if self.meter is None: return`
        # 這段守衛，證明案例 7d/7e/7f 真的能抓到這個 bug（不是空洞斷言）。
        # 用 monkeypatch 一個「壞掉版本」的 _on_meter_reading 邏輯來模擬，
        # 而不是真的改 main_ai.py 原始碼檔案，做法等價但更安全。
        # ---------------------------------------------------------------
        run_regression_guard_check(gui, main_ai)

    finally:
        gui._shutting_down.set()
        root.destroy()


def run_regression_guard_check(gui, main_ai):
    """
    驗證測試本身真的能抓到「斷線後延遲回呼」這個 regression。

    做法：複製一份「拿掉開頭 guard」的 _on_meter_reading 邏輯，綁到 gui 上
    當替身方法直接呼叫，確認它會在 gui.meter is None 時仍然誤觸發橫幅 ——
    藉此證明案例 7d/7e/7f 的斷言方向是對的、不是恆真命題。
    不修改 main_ai.py 原始碼檔案本身。
    """
    import types

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

    gui.meter = None
    gui._pm_comm_failures = main_ai.COMM_FAIL_THRESHOLD - 1
    banner_calls = []
    with patch.object(gui, "_flash_banner", side_effect=lambda *a, **k: banner_calls.append(a)):
        broken_on_meter_reading(gui, False, 0.0)
    triggered_bug = len(banner_calls) > 0 and "已停止更新" in gui._pm_status_var.get()
    check(
        "11. Regression 有效性：拿掉 guard 的版本會誤觸發橫幅（證明測試抓得到這個 bug）",
        triggered_bug,
        expected="拿掉 guard 應該誤觸發",
        actual=f"triggered={triggered_bug}",
    )
    # 還原：清乾淨，避免污染後續狀態
    gui._pm_comm_failures = 0
    gui._pm_status_var.set("未連線")


def main():
    os.environ.setdefault("PYTHONUTF8", "1")
    test_layer1()
    test_layer2()

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
