# -*- coding: utf-8 -*-
"""
FiberAlignmentScanner「訊號有效性判準」回歸測試（pytest，合成資料，不碰真實硬體）。

2026-08-19 從 scratchpad 補進版控、轉為 pytest（原本是獨立可執行、用自製
record()/check() helper 的腳本，比照 verify_axis_calib.py 轉換時的做法：
class 分組、plain assert、tmp_path fixture）。CLAUDE.md〈訊號有效性判準〉
與 FIBER_ALIGNMENT_SCAN_DESIGN.md〈驗證方式〉均已記載這支測試存在此檔。

驗證對象：fiber_scanner.py 的兩層訊號有效性判準
  1. `_measure_here()` 的絕對下限分支（`min_valid_power_dbm`）
  2. `_check_signal_detectable()`（階段一第 1 輪收斂時判斷「真的站在
     峰值附近」還是「整個範圍根本沒訊號」）
  3. 兩者串接進 `run_stage1()` / `run()` 的整合情境
  4. 回歸驗證有效性：證明案例組 3 的斷言方向沒有寫反（關掉開關同一情境
     不再觸發，證明中止不是恆真結果）

── 安全規則 ──
  - 不連真實硬體：`ctrl` 用本檔自製的 FakeCtrl 頂替 DS102Controller——只
    實作 FiberAlignmentScanner 實際會呼叫到的公開介面（見 fiber_scanner.py
    內 `self.ctrl.` 開頭的呼叫點：connected/scanning_active/ems_active/
    axis_count/positions_machine/query_status/scan_move_step/
    wait_axis_stop/check_sw_limits_batch/stop），完全不開真實 COM 埠。
  - 不寫入真實 recordings/：`fiber_scanner.py` 刻意不 import main_ai.py／
    ds102_ctrl.py（避免循環相依），本檔也因此不需要 conftest.py 的
    RECORDING_DIR patch——樣本持久化路徑（`scan_dir` 參數）由每個測試
    透過 pytest 內建的 `tmp_path` fixture 明確指定，不會落在專案的
    recordings/ 底下，也沒有 main_ai/ds102_ctrl 那組「兩個獨立模組層級
    綁定」的坑（那是 verify_axis_calib.py 才會踩到的問題）。

執行方式（VS Code Test Explorer 或指令列皆可）：
    venv/Scripts/python.exe -m pytest verify_fiber_scanner_signal.py -v
    venv/Scripts/python.exe -m pytest verify_fiber_scanner_signal.py::TestAbsoluteFloor -v
"""

import json
from pathlib import Path

import pytest

import fiber_scanner as fs


# =============================================================================
# 假控制器：只實作 FiberAlignmentScanner 用到的公開介面，無限行程、
# 同步立即完成（不做任何序列 I/O）。
# =============================================================================
class FakeCtrl:
    """
    假的 DS102Controller。座標無限範圍（本次驗證不觸及軟體限位邏輯），
    scan_move_step 同步完成、永遠成功（除非 ems_active）。

    只暴露 fiber_scanner.py 全檔 `self.ctrl.` 呼叫點用到的介面：
    connected / scanning_active / ems_active / axis_count /
    positions_machine / query_status / scan_move_step / wait_axis_stop /
    check_sw_limits_batch / stop。
    """

    # 與 fiber_scanner.AXIS_NO 一致（該檔刻意不 import main_ai，見檔頭說明）
    _MAP = {"1": "X", "2": "Y", "3": "Z", "4": "U", "5": "V", "6": "W"}

    def __init__(self, axes=("X",)):
        self.axes = list(axes)
        self.axis_count = len(self.axes)  # 決定 _active_axes() 掃描幾軸
        self.connected = True
        self.ems_active = False
        self.scanning_active = False
        self._pos = {ax: 0.0 for ax in self.axes}
        self.axis_calib = {}  # 本檔不測 μm 快照本身，一律回傳 None（見 estimate_um）
        self.stop_calls = 0
        self.move_log = []  # [(axis, direction, amount_str), ...]

    @property
    def positions_machine(self):
        return dict(self._pos)

    def estimate_um(self, ax, pulse):
        """比照 ds102_ctrl.DS102Controller.estimate_um：沒有校正參數一律 None。

        2026-08-19 `_measure_here()` 新增的 μm 快照邏輯會呼叫這個方法，
        本檔測的是訊號有效性判準、不是 μm 快照，回傳 None 讓該邏輯直接
        跳過（等同「這軸沒有校正參數」），不影響本檔任何斷言。
        """
        return None

    def query_status(self, axis_no):
        ax = self._MAP.get(axis_no)
        if ax not in self._pos:
            return "Stage not connected", ""
        return "Stop", str(self._pos[ax])

    def scan_move_step(self, axis_no, direction, amount, l_speed, f_speed, rate, s_rate, wait_done=True):
        if self.ems_active:
            return False
        ax = self._MAP.get(axis_no)
        if ax is None or ax not in self._pos:
            return False
        delta = float(amount) * (1 if direction == "CW" else -1)
        self._pos[ax] += delta
        self.move_log.append((ax, direction, amount))
        return True

    def wait_axis_stop(self, axis_no, timeout=30):
        return True  # scan_move_step 已同步完成，視為立即到位

    def check_sw_limits_batch(self, targets):
        return True, ""  # 本次驗證不觸及限位邏輯，全部放行

    def stop(self):
        self.stop_calls += 1


def make_scanner(power_query, ctrl=None, **kwargs):
    ctrl = ctrl or FakeCtrl()
    kwargs.setdefault("step_min", 2)
    kwargs.setdefault("settle_sec", 0.0)  # 不需要真的等震動衰減，加速測試
    scanner = fs.FiberAlignmentScanner(ctrl=ctrl, power_query=power_query, **kwargs)
    return scanner, ctrl


# 決定性、有界的「無訊號」擾動樣式（避免用真隨機造成測試偶發性）。
# calibrate_noise() 預設取 5 次樣本，這裡故意也只放 5 個值，讓校準用的
# 統計量與後續搜尋過程反覆取用的是「同一個母體」，range/sigma 比例才有
# 意義（見 scratchpad 的調參紀錄：實測 range≈0.014、threshold≈0.039，
# 安全邊界約 2.8 倍）。
_FLAT_NOISE_PATTERN = [0.008, -0.006, 0.007, -0.004, 0.005]


def make_flat_power_query():
    """完全沒耦光情境：功率與座標無關，只有微小、有界、決定性的擾動。"""
    idx = {"i": -1}

    def q():
        idx["i"] += 1
        return True, -75.0 + _FLAT_NOISE_PATTERN[idx["i"] % len(_FLAT_NOISE_PATTERN)]

    return q


def make_peaked_power_query(ctrl, axis="X", center=30.0, amp=100.0, k=0.05):
    """有明顯高斯峰值（用二次曲線近似）情境：峰值在 center。"""

    def q():
        pos = ctrl.positions_machine[axis]
        return True, amp - k * (pos - center) ** 2

    return q


# =============================================================================
# 一、`_measure_here()` 的絕對下限判定
# =============================================================================
class TestAbsoluteFloor:
    def test_below_floor_marks_not_ok_but_keeps_raw_power(self):
        holder = {"val": -60.0}
        scanner, _ = make_scanner(lambda: (True, holder["val"]), min_valid_power_dbm=-50.0)
        s = scanner._measure_here()
        assert s.ok is False
        assert s.power == -60.0  # 保留原始讀值供除錯，不是 None
        assert "low_signal" in s.note

    def test_above_floor_marks_ok_with_empty_note(self):
        holder = {"val": -30.0}
        scanner, _ = make_scanner(lambda: (True, holder["val"]), min_valid_power_dbm=-50.0)
        s = scanner._measure_here()
        assert s.ok is True
        assert s.power == -30.0
        assert s.note == ""

    def test_disabled_by_default_preserves_old_behavior(self):
        """min_valid_power_dbm=None（預設停用）-> 向下相容，極低值仍 ok=True。"""
        scanner, _ = make_scanner(lambda: (True, -99.0))  # 不傳 min_valid_power_dbm
        assert scanner.min_valid_power_dbm is None
        s = scanner._measure_here()
        assert s.ok is True
        assert s.power == -99.0
        assert s.note == ""

    def test_boundary_value_equal_to_floor_is_ok(self):
        """恰好等於下限不算「低於」，判定條件用嚴格 <。"""
        scanner, _ = make_scanner(lambda: (True, -50.0), min_valid_power_dbm=-50.0)
        s = scanner._measure_here()
        assert s.ok is True

    def test_comm_failure_ignores_floor_entirely(self):
        """通訊失敗（meter_ok=False）時完全不看 min_valid_power_dbm，
        power 仍是 None，不會誤入 low_signal 分支。"""
        scanner, _ = make_scanner(lambda: (False, 0.0), min_valid_power_dbm=-50.0)
        s = scanner._measure_here()
        assert s.ok is False
        assert s.power is None
        assert "low_signal" not in s.note


# =============================================================================
# 二、`_check_signal_detectable()` 直接單元測試
# =============================================================================
def _mk_samples(powers):
    return [fs.Sample(coords={"X": float(i)}, ok=True, power=p) for i, p in enumerate(powers)]


class TestCheckSignalDetectableUnit:
    def test_noise_floor_and_threshold_baseline(self):
        """確認測試前提成立：_noise_sigma=0.1/mult 時 floor=0.1、threshold=0.2。"""
        scanner, _ = make_scanner(lambda: (True, 0.0))
        scanner._noise_sigma = 0.1 / scanner.noise_sigma_mult
        floor = scanner._noise_floor()
        assert floor == pytest.approx(0.1)
        threshold = scanner.no_signal_range_mult * floor
        assert threshold == pytest.approx(0.2)

    def test_no_signal_range_raises_scan_abort(self):
        scanner, _ = make_scanner(lambda: (True, 0.0))
        scanner._noise_sigma = 0.1 / scanner.noise_sigma_mult
        samples = _mk_samples([-75.01, -75.02, -75.03, -75.015])  # range=0.02 << 0.2
        with pytest.raises(fs.ScanAbort, match="沒有偵測到"):
            scanner._check_signal_detectable(samples)

    def test_normal_convergence_range_does_not_raise(self):
        scanner, _ = make_scanner(lambda: (True, 0.0))
        scanner._noise_sigma = 0.1 / scanner.noise_sigma_mult
        samples = _mk_samples([-80.0, -60.0, -40.0, -20.0])  # range=60 >> 0.2
        scanner._check_signal_detectable(samples)  # 不應拋例外

    def test_fewer_than_four_samples_skips_check(self):
        """樣本數 < 4 不判斷，即使 range 本身很窄。"""
        scanner, _ = make_scanner(lambda: (True, 0.0))
        scanner._noise_sigma = 0.1 / scanner.noise_sigma_mult
        samples = _mk_samples([-75.01, -75.02, -75.03])  # 只有 3 筆
        scanner._check_signal_detectable(samples)  # 不應拋例外

    def test_uncalibrated_noise_skips_check(self):
        """_noise_sigma 未校準（None）-> _noise_floor()=0 -> threshold<=0
        -> 不應該拋例外，即使樣本數足夠且 range 窄。"""
        scanner, _ = make_scanner(lambda: (True, 0.0))
        assert scanner._noise_sigma is None  # 確認測試前提：未呼叫 calibrate_noise()
        samples = _mk_samples([-75.01, -75.02, -75.03, -75.015])
        scanner._check_signal_detectable(samples)  # 不應拋例外

    def test_mixed_invalid_samples_filtered_correctly(self):
        """混入通訊失敗（power=None）與 ok=False 的樣本，確認過濾邏輯正確
        （不會被這些無效樣本拉低/干擾判斷，也不會因 None 值在 max()/min()
        裡炸掉）。"""
        scanner, _ = make_scanner(lambda: (True, 0.0))
        scanner._noise_sigma = 0.1 / scanner.noise_sigma_mult
        mixed = _mk_samples([-75.01, -75.02, -75.03, -75.015])
        mixed.append(fs.Sample(coords={"X": 99.0}, ok=False, power=None, note="comm fail"))
        mixed.append(fs.Sample(coords={"X": 100.0}, ok=False, power=-999.0, note="low_signal: ..."))
        with pytest.raises(fs.ScanAbort):
            scanner._check_signal_detectable(mixed)  # 過濾後 range 仍窄 -> 仍應中止


# =============================================================================
# 三、完整流程整合測試（run_stage1 串接 _check_signal_detectable）
# =============================================================================
class TestIntegrationRunStage1:
    def test_flat_power_aborts_as_no_signal(self, tmp_path):
        ctrl = FakeCtrl()
        logs = []
        scanner, _ = make_scanner(
            make_flat_power_query(), ctrl=ctrl, progress_cb=lambda m: logs.append(m)
        )
        result = scanner.run(initial_step={"X": 20}, scan_dir=tmp_path)

        no_signal_msgs = [m for m in logs if "沒有偵測到" in m]
        assert len(no_signal_msgs) >= 1

        abort_msgs = [m for m in logs if m.startswith("搜尋中止：")]
        assert len(abort_msgs) == 1  # ScanAbort 被 run() 正常捕捉，不外洩

        # 樣本確實持久化到指定的暫存 scan_dir（不是專案 recordings/）。
        scan_files = list(tmp_path.glob("scan_*.json"))
        assert len(scan_files) == 1
        data = json.loads(scan_files[0].read_text(encoding="utf-8"))
        assert data.get("completed") is False  # 異常中止，不是正常跑完三階段
        assert data.get("sample_count", 0) > 0

        assert isinstance(result, dict)  # 中止不會讓 run() 炸例外

    def test_peaked_power_converges_without_false_abort(self, tmp_path):
        """明顯高斯峰值就在起點附近 -> 不應被誤判為無訊號。"""
        ctrl = FakeCtrl()
        logs = []
        scanner, _ = make_scanner(
            make_peaked_power_query(ctrl, center=30.0), ctrl=ctrl,
            progress_cb=lambda m: logs.append(m),
        )
        scanner.run(initial_step={"X": 20}, scan_dir=tmp_path)

        no_signal_msgs = [m for m in logs if "沒有偵測到" in m]
        assert len(no_signal_msgs) == 0

        abort_msgs = [m for m in logs if m.startswith("搜尋中止：")]
        assert len(abort_msgs) == 0  # 正常跑完，不是被中止

        scan_files = list(tmp_path.glob("scan_*.json"))
        if scan_files:
            data = json.loads(scan_files[0].read_text(encoding="utf-8"))
            assert data.get("completed") is True

        # 座標下降確實收斂到峰值附近。
        assert abs(ctrl.positions_machine["X"] - 30.0) <= scanner.step_min


# =============================================================================
# 四、回歸驗證有效性 —— 證明案例組三的斷言方向沒有寫反
# =============================================================================
class TestRegressionGuard:
    def test_disabling_abort_flag_suppresses_no_signal_abort(self, tmp_path):
        """用同一個「完全沒耦光」情境，但把 abort_if_no_signal 設為 False，
        確認這次「不會」觸發中止——藉此證明：
          (a) test_flat_power_aborts_as_no_signal 的中止不是偶然發生的恆真
              結果，關掉開關就不會發生；
          (b) 開關本身確實有控制 _check_signal_detectable 是否被呼叫的作用。
        """
        ctrl = FakeCtrl()
        logs = []
        scanner, _ = make_scanner(
            make_flat_power_query(), ctrl=ctrl, abort_if_no_signal=False,
            progress_cb=lambda m: logs.append(m),
        )
        scanner.run(initial_step={"X": 20}, scan_dir=tmp_path)

        no_signal_msgs = [m for m in logs if "沒有偵測到" in m]
        assert len(no_signal_msgs) == 0

        abort_msgs = [m for m in logs if m.startswith("搜尋中止：")]
        assert len(abort_msgs) == 0

        scan_files = list(tmp_path.glob("scan_*.json"))
        if scan_files:
            data = json.loads(scan_files[0].read_text(encoding="utf-8"))
            assert data.get("completed") is True  # 正常跑完，未被中止
