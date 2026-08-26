# -*- coding: utf-8 -*-
"""
`_wait_axis_stop()` 起步競態（CLAUDE.md 稱的「孿生競態」）的假物件回歸測試。

背景：`_wait_origin_done()` 於 2026-08-21 修掉「非 Driving 不等於復歸完成」
的競態之後，CLAUDE.md 明載 `_wait_axis_stop()` 存在同一個競態的孿生體且
尚未修——它的 `status == "Stop"` 直接 `return True`，而實測 `GO CW` 的
Driving 位元 assert 延遲是 96ms、第一次 `query_status()` 要 SB3?+SB1? 兩次
往返約 112ms，**餘裕只有約 16ms**。落在那個窗口裡就會把「還沒起步」讀成
「已經停好」，`move_step(wait_done=True)` 在軸飛行中回報成功。

本檔驗證 2026-08-26 的修法：寬限期（`MOVE_START_GRACE`）內要求「看過
Driving／走完預期行程／POS 變化過」三者之一，寬限期外回到舊語意。

安全規則（同 conftest.py 的既有規範）：
  - 不連真實硬體：用真的 `DS102Controller()` 實例但 `ser` 全程維持 None，
    逐一 monkeypatch 個別方法（`query_status` / `_serial_write`），
    不整個替換掉 ctrl 物件。
  - 不寫入真實 `recordings/` 與 `data/`：透過 `patch.object` 把
    `ds102_ctrl.RECORDING_DIR` / `DATA_DIR` 導向 tmp_path。本檔沒有呼叫
    任何持久化方法，但這層防護照補——CLAUDE.md 記載測試腳本清空過兩次
    teaching points，成本低就先擋住。
  - 測試不依賴真實時間長度：`MOVE_START_GRACE` / `MOVE_START_POLL` /
    `WAIT_INTERVAL` 一律 monkeypatch 成很小的值，讓「寬限期過後」這類
    案例不必真的等 1 秒。
"""

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import ds102_ctrl  # noqa: E402
import fiber_scanner  # noqa: E402


# ---------------------------------------------------------------------------
# 共用假物件
# ---------------------------------------------------------------------------
class ScriptedStatus:
    """
    依序回傳預先排好的 (status, pos)，用完最後一筆就一直重複它。

    重複最後一筆而不是拋 StopIteration，是為了讓「一直停在原地不動」
    （GO 未生效）與「一直卡在 Driving」（逾時）這兩類案例寫起來自然。
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, axis_no):
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        status, pos = self.script[idx]
        return status, ("" if pos is None else str(pos))


@pytest.fixture
def ctrl(tmp_path, monkeypatch):
    """乾淨的 DS102Controller（未連線），時序常數縮到毫秒級。"""
    patchers = [
        patch.object(ds102_ctrl, "RECORDING_DIR", new=Path(tmp_path)),
        patch.object(ds102_ctrl, "DATA_DIR", new=Path(tmp_path) / "data"),
    ]
    for p in patchers:
        p.start()
    try:
        c = ds102_ctrl.DS102Controller()
        assert c.ser is None, "測試絕不可連上真實序列埠"
        monkeypatch.setattr(ds102_ctrl, "MOVE_START_GRACE", 0.20)
        monkeypatch.setattr(ds102_ctrl, "MOVE_START_POLL", 0.01)
        monkeypatch.setattr(ds102_ctrl, "WAIT_INTERVAL", 0.01)
        yield c
    finally:
        for p in patchers:
            p.stop()


def collect_alarms(ctrl):
    """掛一個記錄用的 alarm callback，回傳它收到的 (title, detail) 清單。"""
    got = []
    ctrl._alarm_cb = lambda title, detail: got.append((title, detail))
    return got


def collect_logs(ctrl):
    """攔下 _log()，回傳 (level, msg) 清單。"""
    got = []
    ctrl._log = lambda level, msg, **kw: got.append((level, msg))
    return got


# ===========================================================================
# 一、競態本身（這次修正的核心）
# ===========================================================================
class TestStartupRace:
    def test_stop_before_driving_asserts_is_not_treated_as_done(self, ctrl):
        """
        第一次取樣落在 Driving 尚未 assert 的窗口內（回 Stop、POS 未動），
        接著才轉 Driving 並真的走完——必須等到真正停穩才回 True。

        這正是舊寫法會誤判成功的序列：舊碼在第一筆 Stop 就 return True。
        """
        q = ScriptedStatus([
            ("Stop", 0),        # GO 已送出但 bit6 還沒 assert
            ("Driving", 120),
            ("Driving", 380),
            ("Stop", 500),
        ])
        ctrl.query_status = q
        ok = ctrl._wait_axis_stop("1", timeout=5.0, start_pos=0.0, expected_travel=500.0)
        assert ok is True
        assert q.calls == 4, "不可在第一筆 Stop 就回報到位（舊競態的簽名）"

    def test_race_window_without_travel_hint_still_waits(self, ctrl):
        """呼叫端沒傳位移提示時，同一個序列一樣不可提早回報成功。"""
        q = ScriptedStatus([("Stop", 0), ("Driving", 50), ("Stop", 200)])
        ctrl.query_status = q
        assert ctrl._wait_axis_stop("1", timeout=5.0) is True
        assert q.calls == 3

    def test_normal_move_unchanged(self, ctrl):
        """一般移動（第一次取樣就是 Driving）行為與改動前完全相同。"""
        q = ScriptedStatus([("Driving", 10), ("Driving", 400), ("Stop", 500)])
        ctrl.query_status = q
        assert ctrl._wait_axis_stop("1", timeout=5.0, start_pos=0.0, expected_travel=500.0) is True
        assert q.calls == 3


# ===========================================================================
# 二、短移動：在第一次取樣之前就跑完
# ===========================================================================
class TestShortMove:
    def test_completed_before_first_sample_via_expected_travel(self, ctrl):
        """
        移動短到第一次取樣時已經結束（從沒看到 Driving）。位移證據
        （走完 expected_travel）必須讓它立刻回 True，不能空等寬限期
        之後判成「GO 未生效」。
        """
        q = ScriptedStatus([("Stop", 500)])
        ctrl.query_status = q
        t0 = time.time()
        ok = ctrl._wait_axis_stop("1", timeout=5.0, start_pos=0.0, expected_travel=500.0)
        assert ok is True
        assert q.calls == 1
        assert time.time() - t0 < 0.15, "有位移證據就該立刻回，不必等寬限期"

    def test_tolerance_one_pulse(self, ctrl):
        """容差 1 pulse（與量測路徑 offset-1 同一慣例）：走 499/500 算到位。"""
        ctrl.query_status = ScriptedStatus([("Stop", 499)])
        assert ctrl._wait_axis_stop("1", timeout=5.0, start_pos=0.0, expected_travel=500.0) is True

    def test_zero_pulse_move_is_trivially_done(self, ctrl):
        """PULS 0（位移 0）不可被判成「GO 未生效」——0 >= 0-1 天然成立。"""
        ctrl.query_status = ScriptedStatus([("Stop", 0)])
        assert ctrl._wait_axis_stop("1", timeout=5.0, start_pos=0.0, expected_travel=0.0) is True

    def test_pos_change_is_fallback_evidence(self, ctrl):
        """沒有 expected_travel 時，POS 相對第一次取樣值變化過即算證據。"""
        q = ScriptedStatus([("Stop", 100), ("Stop", 103)])
        ctrl.query_status = q
        assert ctrl._wait_axis_stop("1", timeout=5.0) is True
        assert q.calls == 2


# ===========================================================================
# 三、GO 未生效：寬限期過後仍毫無動作證據
# ===========================================================================
class TestCommandNotExecuted:
    def test_reports_failure_after_grace(self, ctrl):
        alarms = collect_alarms(ctrl)
        logs = collect_logs(ctrl)
        ctrl.query_status = ScriptedStatus([("Stop", 0)])
        ok = ctrl._wait_axis_stop("1", timeout=5.0, start_pos=0.0, expected_travel=500.0)
        assert ok is False
        assert any(lv == "ERROR" and "移動未生效" in msg for lv, msg in logs)
        assert alarms and "未生效" in alarms[0][0]

    def test_waits_at_least_the_grace_period(self, ctrl):
        """不可在寬限期內就下「未生效」的結論（那正是競態的另一面）。"""
        ctrl.query_status = ScriptedStatus([("Stop", 0)])
        t0 = time.time()
        assert ctrl._wait_axis_stop("1", timeout=5.0, start_pos=0.0, expected_travel=500.0) is False
        assert time.time() - t0 >= ds102_ctrl.MOVE_START_GRACE

    def test_unreadable_pos_does_not_fake_success(self, ctrl):
        """POS 讀不到（通訊失敗）時證據不成立，不可誤報成功。"""
        ctrl.query_status = ScriptedStatus([("Stop", None)])
        assert ctrl._wait_axis_stop("1", timeout=5.0, start_pos=0.0, expected_travel=500.0) is False


# ===========================================================================
# 四、限位／異常：既有的「撞限位不再靜默」不可退步
# ===========================================================================
class TestLimitHandling:
    def test_limit_after_driving_still_fails_loudly(self, ctrl):
        """看過 Driving 之後撞限位——2026-08-05 的結論，必須立刻報失敗。"""
        alarms = collect_alarms(ctrl)
        ctrl.query_status = ScriptedStatus([("Driving", 100), ("Detect CW limit", 552)])
        assert ctrl._wait_axis_stop("1", timeout=5.0, start_pos=0.0, expected_travel=5000.0) is False
        assert alarms and alarms[0][1] == "Detect CW limit"

    def test_limit_with_full_travel_is_not_swallowed_by_evidence(self, ctrl):
        """
        🔴 走完了預期行程、但停在限位上——位移證據成立也**不可**當成功。
        這是修改分支結構時最容易踩到的坑：把 moved 當成 return True 的
        通用捷徑，就會讓撞限位重新變回靜默。
        """
        alarms = collect_alarms(ctrl)
        ctrl.query_status = ScriptedStatus([("Detect CW limit", 500)])
        assert ctrl._wait_axis_stop("1", timeout=5.0, start_pos=0.0, expected_travel=500.0) is False
        assert alarms, "撞限位必須觸發警報"

    def test_departing_from_a_pressed_limit_is_not_a_false_failure(self, ctrl):
        """
        從限位上往反方向出發：GO 尚未生效時讀到的是「出發前就壓著的那顆
        限位」。舊寫法會在這裡直接判失敗；現在寬限期內續輪，等它轉
        Driving 就正常完成。
        """
        alarms = collect_alarms(ctrl)
        q = ScriptedStatus([
            ("Detect CCW limit", 0),   # 還沒起步，讀到出發側限位
            ("Driving", 60),
            ("Stop", 300),
        ])
        ctrl.query_status = q
        assert ctrl._wait_axis_stop("1", timeout=5.0, start_pos=0.0, expected_travel=300.0) is True
        assert alarms == [], "這不是撞限位，不該發警報"

    def test_stuck_on_limit_through_grace_still_fails(self, ctrl):
        """真的走不掉（整段寬限期都壓在限位上、POS 不動）照樣要報失敗。"""
        alarms = collect_alarms(ctrl)
        ctrl.query_status = ScriptedStatus([("Detect CCW limit", 0)])
        assert ctrl._wait_axis_stop("1", timeout=5.0, start_pos=0.0, expected_travel=300.0) is False
        assert alarms and alarms[0][1] == "Detect CCW limit"


# ===========================================================================
# 五、既有行為：EMS、逾時、以及使用者中途按停止
# ===========================================================================
class TestExistingBehaviour:
    def test_ems_aborts_immediately(self, ctrl):
        ctrl.ems_active = True
        ctrl.query_status = ScriptedStatus([("Driving", 0)])
        assert ctrl._wait_axis_stop("1", timeout=5.0) is False

    def test_timeout_returns_false(self, ctrl):
        ctrl.query_status = ScriptedStatus([("Driving", 10)])
        assert ctrl._wait_axis_stop("1", timeout=0.15) is False

    def test_user_stop_midmove_returns_promptly(self, ctrl):
        """
        🔴 這一案是「寬限期外刻意不要求位移證據」的理由。使用者中途按
        停止時軸會提前停下，`travelled` 永遠達不到 expected；若無條件
        要求 `travelled >= expected - 1`（CLAUDE.md 原本記載的修法），
        這裡會退化成空等到 WAIT_TIMEOUT(30s)。
        """
        ctrl.query_status = ScriptedStatus([("Driving", 40), ("Stop", 120)])
        t0 = time.time()
        assert ctrl._wait_axis_stop("1", timeout=5.0, start_pos=0.0, expected_travel=5000.0) is True
        assert time.time() - t0 < 0.5


# ===========================================================================
# 六、_move_evidence（純計算）
# ===========================================================================
class TestMoveEvidence:
    ev = staticmethod(ds102_ctrl.DS102Controller._move_evidence)

    def test_none_pos_is_never_evidence(self):
        assert self.ev(None, 0.0, 0.0, 500.0) is False

    def test_full_travel(self):
        assert self.ev(500.0, 0.0, 0.0, 500.0) is True

    def test_short_travel_without_pos_change(self):
        assert self.ev(300.0, 300.0, 0.0, 500.0) is False

    def test_negative_direction_uses_absolute_travel(self):
        assert self.ev(-500.0, 0.0, 0.0, 500.0) is True

    def test_pos_change_threshold(self):
        assert self.ev(101.0, 100.0, None, None) is False   # 1 pulse 不算（需 > EPS）
        assert self.ev(102.0, 100.0, None, None) is True

    def test_missing_hints_fall_back_to_first_pos(self):
        assert self.ev(150.0, 100.0, None, None) is True
        assert self.ev(100.0, 100.0, None, None) is False


# ===========================================================================
# 七、呼叫端接線：位移提示有沒有真的傳下去
# ===========================================================================
class TestCallSitesPassHints:
    def test_do_move_step_passes_start_and_expected(self, ctrl):
        seen = {}

        def fake_wait(axis_no, timeout=None, start_pos=None, expected_travel=None):
            seen.update(axis_no=axis_no, start_pos=start_pos, expected_travel=expected_travel)
            return True

        ctrl._serial_write = lambda *a, **k: None
        ctrl._wait_axis_stop = fake_wait
        with ctrl._lock:
            ctrl._positions_pulse["X"] = 1234.0
        assert ctrl._do_move_step("1", "CW", "500", "100", "2000", "300", "100", True) is True
        assert seen == {"axis_no": "1", "start_pos": 1234.0, "expected_travel": 500.0}

    def test_public_wrapper_forwards_hints(self, ctrl):
        seen = {}

        def fake_wait(*args):
            seen["args"] = args
            return True

        ctrl._wait_axis_stop = fake_wait
        ctrl.wait_axis_stop("2", 9.0, 10.0, 20.0)
        assert seen["args"] == ("2", 9.0, 10.0, 20.0)

    def test_replay_hint_parses_step_command(self, ctrl):
        with ctrl._lock:
            ctrl._positions_pulse["Y"] = 77.0
        tx = "AXI2:L0 100:R0 2000:S0 300:F0 5000:PULS 250:GO CCW"
        assert ctrl._replay_move_hint("2", tx) == (77.0, 250.0)

    def test_replay_hint_ignores_jog_and_origin(self, ctrl):
        assert ctrl._replay_move_hint("1", "AXI1:F0 5000:GO CWJ") == (None, None)
        assert ctrl._replay_move_hint("1", "AXI1:F0 5000:GO ORG") == (None, None)
        assert ctrl._replay_move_hint("1", "AXI1:PULS 250:GOABS") == (None, None)

    def test_replay_hint_handles_cw_and_ccw(self, ctrl):
        with ctrl._lock:
            ctrl._positions_pulse["X"] = 5.0
        assert ctrl._replay_move_hint("1", "AXI1:PULS 30:GO CW") == (5.0, 30.0)
        assert ctrl._replay_move_hint("1", "AXI1:PULS 30:GO CCW") == (5.0, 30.0)


class TestScannerBatchMovePassesHints:
    """fiber_scanner._move_multi_axis 的多軸批次等待也要帶位移提示。"""

    def test_hints_match_each_axis_delta(self):
        waits = []

        class FakeCtrl:
            positions_machine = {"X": 100.0, "Y": -50.0, "Z": 0.0}

            def scan_move_step(self, *a, **k):
                return True

            def wait_axis_stop(self, axis_no, timeout=None, start_pos=None, expected_travel=None):
                waits.append((axis_no, start_pos, expected_travel))
                return True

            def stop(self):
                pass

        class FakeScanner:
            ctrl = FakeCtrl()

            def _check_abort(self):
                pass

            def _dynamic_speed(self, delta):
                return ("100", "5000", "2000", "300")

        fiber_scanner.FiberAlignmentScanner._move_multi_axis(
            FakeScanner(), {"X": 40, "Y": -25, "Z": 0}
        )
        assert sorted(waits) == sorted([
            (fiber_scanner.AXIS_NO["X"], 100.0, 40),
            (fiber_scanner.AXIS_NO["Y"], -50.0, 25),
        ]), "Z 的 delta=0 應被略過，X/Y 要帶各自的起點與預期位移"
