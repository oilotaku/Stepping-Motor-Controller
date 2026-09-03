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

import core.fiber_scanner as fs


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

    @staticmethod
    def limit_direction(status):
        """2026-08-26 新增：`_move_relative()` 移動失敗後會呼叫
        `_note_limit_hit()`，那裡會問 ctrl 這個問題。本檔的 FakeCtrl 只有
        EMS 會讓移動失敗（`query_status` 永遠回 "Stop"），所以這裡怎麼答
        都不影響本檔斷言——但少了這個方法會直接 AttributeError。
        """
        return None

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
# calibrate_noise() 預設取 12 次樣本（2026-08-31 從 5 調高，見該方法
# docstring），這裡刻意只放 5 個值、靠取模循環湊出 12 筆——校準與後續
# 搜尋過程反覆取用的仍是「同一個母體」，range/sigma 比例才有意義（見
# scratchpad 的調參紀錄：實測 range≈0.014、threshold≈0.039，安全邊界
# 約 2.8 倍；筆數從 5 變 12 後 σ 估計值會略有變化，但仍是同一個量級）。
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


# =============================================================================
# 五、calibrate_noise() 預設 n_samples（2026-08-31：5 → 12）
# =============================================================================
class TestCalibrateNoiseDefault:
    def test_default_takes_twelve_samples(self):
        """空參數呼叫（run() 內部所有呼叫點都是空參數）要吃到新預設。"""
        calls = {"n": 0}

        def q():
            calls["n"] += 1
            return True, -50.0

        scanner, _ = make_scanner(q)
        scanner.calibrate_noise()
        assert calls["n"] == 12

    def test_explicit_n_samples_still_overrides(self):
        """呼叫端仍可覆寫，不是被寫死。"""
        calls = {"n": 0}

        def q():
            calls["n"] += 1
            return True, -50.0

        scanner, _ = make_scanner(q)
        scanner.calibrate_noise(n_samples=6)
        assert calls["n"] == 6


# =============================================================================
# 六、殘差診斷 `_diagnose_residual_curvature()`（純事後統計，不移動不量測）
# =============================================================================
def _sample(coords, power, ok=True):
    return fs.Sample(coords=dict(coords), ok=ok, power=power)


def _true_quadratic_db(x, sigma_true, peak_db=-10.0):
    """dB 域的高斯耦光曲線二次近似：P(x) = peak - (4.343/(2σ²))·x²。"""
    a = 4.343 / (2 * sigma_true ** 2)
    return peak_db - a * x * x


class TestResidualDiagnosticsWindowFiltering:
    """
    驗證取樣窗口正確濾掉遠處的盲搜級樣本，不讓它們污染二次擬合。
    直接構造 scanner.samples，不需要真的跑一輪搜尋——擬合本身是純函式，
    這樣測試才能對擬合結果做精確的數值斷言（比照 _mk_samples 的既有寫法）。
    """

    def test_pollution_far_outside_window_has_zero_effect(self):
        """混入盲搜級大範圍樣本（座標可達數千 pulse），擬合結果必須跟
        完全不加這些樣本時一模一樣——不是「差不多」，是真的被濾掉。"""
        ctrl = FakeCtrl(axes=("X",))
        scanner, _ = make_scanner(lambda: (True, 0.0), ctrl=ctrl)
        scanner.active_axes = ["X"]

        sigma_true = 30.0
        xs_clean = [-15, -12, -8, -5, -2, 0, 2, 5, 8, 12, 15]
        clean_samples = [_sample({"X": x}, _true_quadratic_db(x, sigma_true)) for x in xs_clean]
        pollution = [_sample({"X": x}, 999.0) for x in (-5000, -1000, 1000, 5000)]

        scanner.samples = clean_samples + pollution
        scanner._diagnose_residual_curvature()
        with_pollution = scanner.residual_diagnostics.get("X")

        scanner2, _ = make_scanner(lambda: (True, 0.0), ctrl=ctrl)
        scanner2.active_axes = ["X"]
        scanner2.samples = list(clean_samples)
        scanner2._diagnose_residual_curvature()
        without_pollution = scanner2.residual_diagnostics.get("X")

        assert with_pollution is not None
        assert without_pollution is not None
        assert with_pollution["sigma_pulse"] == pytest.approx(without_pollution["sigma_pulse"], abs=1e-9)
        assert with_pollution["sigma_pulse"] == pytest.approx(sigma_true, rel=1e-6)

    def test_single_axis_selection_self_window_is_still_enforced(self):
        """只選 1 軸搜尋時 other_axes 是空清單——那層窗口檢查形同虛設，
        被擬合軸自己的窗口限制才是唯一防線。用刻意設計成「若沒有這層
        自身窗口限制、擬合會被拉走」的污染樣本驗證它真的有作用。"""
        ctrl = FakeCtrl(axes=("X",))
        scanner, _ = make_scanner(lambda: (True, 0.0), ctrl=ctrl)
        scanner.active_axes = ["X"]  # 只選 1 軸

        sigma_true = 30.0
        xs_clean = [-15, -12, -8, -5, -2, 0, 2, 5, 8, 12, 15]
        clean_samples = [_sample({"X": x}, _true_quadratic_db(x, sigma_true)) for x in xs_clean]
        # 座標遠超過窗口（tol = step_min(2) x REOPEN_STEP_MULT(8) = 16），
        # 功率刻意設得比乾淨樣本都高，若自身窗口沒發揮作用，這幾點會
        # 主導最小平方擬合，讓 sigma 估計完全跑掉。
        pollution = [_sample({"X": x}, -1.0) for x in (200, -200, 400, -400)]
        scanner.samples = clean_samples + pollution

        scanner._diagnose_residual_curvature()
        diag = scanner.residual_diagnostics.get("X")
        assert diag is not None
        assert diag["sigma_pulse"] == pytest.approx(sigma_true, rel=1e-6)

    def test_other_axis_outside_window_is_excluded(self):
        """多軸情境：即使被擬合軸自己的座標在窗口內，只要另一軸偏離最終
        收斂位置超過 tol，這筆樣本也要被排除。"""
        ctrl = FakeCtrl(axes=("X", "Y"))
        scanner, _ = make_scanner(lambda: (True, 0.0), ctrl=ctrl)
        scanner.active_axes = ["X", "Y"]

        sigma_true = 30.0
        xs_clean = [-15, -12, -8, -5, -2, 0, 2, 5, 8, 12, 15]
        clean_samples = [
            _sample({"X": x, "Y": 0.0}, _true_quadratic_db(x, sigma_true)) for x in xs_clean
        ]
        # X 座標在窗口內，但 Y 偏移遠超過 tol——應該被排除，不能拿來擬合 X。
        contaminated = [_sample({"X": x, "Y": 500.0}, -1.0) for x in (-10, -3, 3, 10)]
        scanner.samples = clean_samples + contaminated

        scanner._diagnose_residual_curvature()
        diag = scanner.residual_diagnostics.get("X")
        assert diag is not None
        assert diag["sigma_pulse"] == pytest.approx(sigma_true, rel=1e-6)
        assert diag["n_samples"] == len(xs_clean)  # 污染樣本確實一筆都沒進來


class TestResidualDiagnosticsEdgeCases:
    def test_insufficient_samples_skips_axis_without_exception(self):
        ctrl = FakeCtrl(axes=("X",))
        scanner, _ = make_scanner(lambda: (True, 0.0), ctrl=ctrl)
        scanner.active_axes = ["X"]
        scanner.samples = [_sample({"X": x}, _true_quadratic_db(x, 30.0)) for x in (-5, 0, 5)]
        scanner._diagnose_residual_curvature()  # 不應拋例外
        assert "X" not in scanner.residual_diagnostics

    def test_one_sided_samples_skips_axis(self):
        """窗口內只有單側樣本（min(x) 與 max(x) 沒有跨過 0）時不可靠外插。"""
        ctrl = FakeCtrl(axes=("X",))
        scanner, _ = make_scanner(lambda: (True, 0.0), ctrl=ctrl)
        scanner.active_axes = ["X"]
        scanner.samples = [_sample({"X": x}, _true_quadratic_db(x, 30.0)) for x in (2, 5, 8, 12)]
        scanner._diagnose_residual_curvature()
        assert "X" not in scanner.residual_diagnostics

    def test_upward_opening_fit_skips_axis(self):
        """擬合開口非向下（a>=0，物理上不合理，例如雜訊主導）要跳過。"""
        ctrl = FakeCtrl(axes=("X",))
        scanner, _ = make_scanner(lambda: (True, 0.0), ctrl=ctrl)
        scanner.active_axes = ["X"]
        xs = [-15, -12, -8, -5, -2, 0, 2, 5, 8, 12, 15]
        scanner.samples = [_sample({"X": x}, 0.001 * x * x) for x in xs]  # 開口向上
        scanner._diagnose_residual_curvature()
        assert "X" not in scanner.residual_diagnostics

    def test_failure_never_raises_and_is_logged(self):
        """run() 把整段包在 try/except 裡是防禦措施，但方法本身遇到擬合
        失敗時也應該乾淨地跳過並記 log，不是靠外層 try/except 兜底。"""
        ctrl = FakeCtrl(axes=("X",))
        logs = []
        scanner, _ = make_scanner(lambda: (True, 0.0), ctrl=ctrl, progress_cb=lambda m: logs.append(m))
        scanner.active_axes = ["X"]
        scanner.samples = [_sample({"X": x}, _true_quadratic_db(x, 30.0)) for x in (2, 5, 8, 12)]
        scanner._diagnose_residual_curvature()
        assert any("略過" in m for m in logs)


# =============================================================================
# 七、收尾曲率擬合微調 `run_stage_curvature_refine()`
# =============================================================================
def make_gaussian_query(ctrl, axis="X", center=0.0, sigma_true=50.0, peak_db=-10.0,
                         noise_pattern=None):
    """dB 域高斯耦光曲線的合成功率函式，疊上決定性、有界的擾動樣式
    （比照 make_flat_power_query 的既有寫法，避免真隨機造成測試偶發性）。
    """
    pattern = noise_pattern if noise_pattern is not None else _FLAT_NOISE_PATTERN
    idx = {"i": -1}

    def q():
        idx["i"] += 1
        x = ctrl.positions_machine[axis]
        p = _true_quadratic_db(x - center, sigma_true, peak_db)
        return True, p + pattern[idx["i"] % len(pattern)]

    return q


class TestCurvatureRefineConvergence:
    def test_reduces_residual_compared_to_stage1_and_3_alone(self):
        """合成高斯場下，開啟曲率微調要比只跑階段一＋三的殘差更小。

        用固定種子的偽亂數（非本檔慣用的決定性擾動樣式）疊加雜訊，取多組
        種子的平均殘差比較——單一種子偶爾會被階段一的三點內插直接命中
        （見 scratchpad 的原型腳本），平均才能穩定看出曲率微調的效果，
        避免測試對單一種子的巧合命中敏感。
        """
        import random

        def run_once(enable_curvature, seed, center, sigma_true):
            ctrl = FakeCtrl(axes=("X",))
            rng = random.Random(seed)

            def q():
                x = ctrl.positions_machine["X"]
                p = _true_quadratic_db(x - center, sigma_true)
                return True, p + rng.gauss(0, 0.05)

            scanner, _ = make_scanner(q, ctrl=ctrl, selected_axes=["X"])
            scanner.run(initial_step={"X": 40}, enable_curvature_fit=enable_curvature, scan_dir=None)
            return abs(ctrl.positions_machine["X"] - center)

        center, sigma_true = 23.0, 60.0
        seeds = range(12)
        residuals_off = [run_once(False, s, center, sigma_true) for s in seeds]
        residuals_on = [run_once(True, s, center, sigma_true) for s in seeds]

        mean_off = sum(residuals_off) / len(residuals_off)
        mean_on = sum(residuals_on) / len(residuals_on)
        # 實測（見 scratchpad 原型腳本）：約 2.05 → 0.5 pulse，取一半當保守門檻。
        assert mean_on < mean_off * 0.7, (
            f"曲率微調沒有明顯改善平均殘差：off={mean_off:.3f}, on={mean_on:.3f}"
        )


class TestCurvatureRefinePureNoise:
    def test_pure_noise_field_moves_nothing(self):
        """最重要的一條：完全沒有真實訊號峰值時，曲率微調一步都不該移動
        （訊噪比門檻必須擋下所有由雜訊算出來的「修正量」）。"""
        ctrl = FakeCtrl(axes=("X",))
        logs = []
        scanner, _ = make_scanner(
            make_flat_power_query(), ctrl=ctrl, selected_axes=["X"],
            progress_cb=lambda m: logs.append(m),
        )
        scanner.active_axes = ["X"]
        scanner.calibrate_noise()
        assert scanner._noise_sigma is not None and scanner._noise_sigma > 0

        before = dict(ctrl.positions_machine)
        scanner.run_stage_curvature_refine({"X": 20})
        after = dict(ctrl.positions_machine)
        assert after == before  # 淨位移為 0：探測用的往返移動即使發生也必須完全退回
        # 確認真的跑到探測／門檻判斷這一步，而不是在更早期就因為某個
        # 無關原因（例如 active_axes 是空的）而整個方法直接 no-op。
        assert any("曲率微調" in m for m in logs)


class TestCurvatureRefineLowValidReadings:
    def test_mostly_invalid_readings_skip_axis_without_applying_correction(self):
        """每組只有 1~2 筆有效讀值時，不可以假裝有 m=4 筆去算門檻——
        必須整軸放棄，不套用任何修正。"""
        ctrl = FakeCtrl(axes=("X",))
        calls = {"n": 0}

        def q():
            calls["n"] += 1
            x = ctrl.positions_machine["X"]
            if calls["n"] % 5 == 0:  # 每 5 次只有 1 次讀得到
                return True, _true_quadratic_db(x, 30.0, peak_db=-10.0)
            return False, 0.0

        scanner, _ = make_scanner(q, ctrl=ctrl, selected_axes=["X"])
        scanner.active_axes = ["X"]
        scanner.calibrate_noise()

        before = dict(ctrl.positions_machine)
        scanner.run_stage_curvature_refine({"X": 20})
        after = dict(ctrl.positions_machine)
        assert after == before


class TestCurvatureRefineZeroSigma:
    def test_zero_noise_sigma_skips_entire_stage(self):
        """_noise_sigma == 0.0（校準時有效讀值太少，見 calibrate_noise）
        時要整段跳過，不能讓門檻退化成 0 而失去防護。"""
        ctrl = FakeCtrl(axes=("X",))

        def q():
            x = ctrl.positions_machine["X"]
            return True, -50.0 + 1.0 * x  # 明顯梯度、完全無雜訊

        scanner, _ = make_scanner(q, ctrl=ctrl, selected_axes=["X"])
        scanner.active_axes = ["X"]
        scanner._noise_sigma = 0.0  # 模擬校準時只拿到 <2 筆有效讀值的情況

        before = dict(ctrl.positions_machine)
        scanner.run_stage_curvature_refine({"X": 20})
        after = dict(ctrl.positions_machine)
        assert after == before


class TestCurvatureRefineLimitHit:
    def test_known_travel_bound_gives_clean_skip_without_any_move(self):
        """單側探測撞限位（模擬這一輪稍早已經撞過一次、_travel_bounds 已
        記住行程邊界）——_move_relative() 會在送指令前就被 _targets_reachable()
        擋下，一次都不會真的動，乾淨跳過該軸、不做單側外推。"""
        ctrl = FakeCtrl(axes=("X",))
        scanner, _ = make_scanner(make_peaked_power_query(ctrl, center=5.0), ctrl=ctrl,
                                   selected_axes=["X"])
        scanner.active_axes = ["X"]
        scanner.calibrate_noise()
        # 模擬「這一輪稍早已經撞過一次」留下的行程邊界：CW 側行程末端在 10。
        scanner._travel_bounds["X"] = [None, 10.0]

        before = dict(ctrl.positions_machine)
        scanner.run_stage_curvature_refine({"X": 20})
        after = dict(ctrl.positions_machine)
        assert after == before
        assert ctrl.move_log == []  # 一步都沒有真的送出去


class TestCurvatureRefineAbortPropagates:
    def test_check_abort_during_refine_raises_scan_abort(self):
        """使用者中止／EMS 觸發時要讓 ScanAbort 原樣往外傳，不可以在這個
        階段內部被吞掉——交給 run() 最外層的 except ScanAbort 收尾。"""
        ctrl = FakeCtrl(axes=("X",))
        scanner, _ = make_scanner(lambda: (True, -10.0), ctrl=ctrl, selected_axes=["X"])
        scanner.active_axes = ["X"]
        scanner.calibrate_noise()

        state = {"n": 0}

        def q():
            state["n"] += 1
            if state["n"] > 3:
                ctrl.ems_active = True
            x = ctrl.positions_machine["X"]
            return True, _true_quadratic_db(x, 30.0, peak_db=-10.0)

        scanner._power_query = q
        with pytest.raises(fs.ScanAbort, match="EMS"):
            scanner.run_stage_curvature_refine({"X": 20})


class TestCurvatureRefineMoveFailureRobustness:
    """
    掃過每一個可能的移動失敗時機（去程/回程/最終正負探測/套用修正），
    確認 `_move_relative()` 的回傳值在每一處都被檢查、失敗時乾淨放棄該軸，
    不會讓例外往外炸或讓內部狀態（座標、樣本）進入不一致的狀態。
    """

    @pytest.mark.parametrize("fail_from_call", [1, 2, 3, 4, 5, 6, 7])
    def test_move_failure_at_any_point_is_handled_gracefully(self, fail_from_call):
        class FakeCtrlFailFrom(FakeCtrl):
            """從第 fail_from_call 次 scan_move_step 呼叫開始，所有移動都
            失敗（座標不變，模擬逾時／通訊失聯，不是撞限位——query_status
            仍回報 "Stop"）。"""

            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._move_calls = 0

            def scan_move_step(self, *args, **kwargs):
                self._move_calls += 1
                if self._move_calls >= fail_from_call:
                    return False
                return super().scan_move_step(*args, **kwargs)

        ctrl = FakeCtrlFailFrom(axes=("X",))
        scanner, _ = make_scanner(make_peaked_power_query(ctrl, center=17.0, amp=-10.0, k=0.0012),
                                   ctrl=ctrl, selected_axes=["X"])
        scanner.active_axes = ["X"]
        scanner.calibrate_noise()

        # 不應該拋出任何例外（ScanAbort 以外，此情境不涉及中止）。
        scanner.run_stage_curvature_refine({"X": 20})
        pos = ctrl.positions_machine["X"]
        assert isinstance(pos, float) and pos == pos  # 沒有變成 NaN，狀態一致
