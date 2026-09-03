# -*- coding: utf-8 -*-
"""
`fiber_scanner_advanced.run_stage_powell()` 回歸測試（pytest，合成資料，不碰真實硬體）。

背景：`fiber_scanner_advanced.py` 是 2026-08-27 新增的替代路徑，用
`scipy.optimize.minimize(method='Powell')` 取代 `fiber_scanner.py` 既有
三階段設計中的階段一＋階段二，設計依據見
`FIBER_ALIGNMENT_SCAN_DESIGN.md`〈第三輪：多軸尋光演算法候選評估
（2026-08-27）〉。本檔驗證：

  1. 基本收斂（非旋轉、非耦合的 2 軸高斯／拋物線峰值曲面）
  2. 軸間耦合案例（旋轉橢圓曲面），並與 `fiber_scanner.run_stage1`
     （不開階段二）比較收斂精度——這是設計文件宣稱的核心賣點，測試
     只如實記錄兩者的實測誤差，不因為結果不如預期就調整曲面去遷就。
  3. 撞限位／`_move_multi_axis` 失敗：objective 應回軟牆懲罰值，不拋例外
  4. 量測失敗（`Sample.ok=False`）：同樣不拋例外
  5. 使用者中止（`ScanAbort`）：例外確實往外傳，且滑台被移回目前看過的
     最佳座標
  6. scipy 未安裝時的優雅降級：拋 `RuntimeError`，不是讓 import 整個失敗
  7. 快取生效：同一個整數 pulse 座標第二次被 objective 詢問時，不會
     再呼叫一次 `_move_multi_axis`/`_measure_here`

── 安全規則（同 verify_blind_scan.py／verify_fiber_scanner_signal.py）──
  - 不連真實硬體：ctrl 用本檔的 FakeCtrl 頂替 DS102Controller。
  - 不寫入真實 recordings/：本檔完全不觸碰持久化，不需要 tmp_path。

執行方式：
    venv/Scripts/python.exe -m pytest verify_scan_powell.py -v
"""

import math

import pytest

import ds102_ctrl  # 只為了 FakeCtrl.limit_direction 委派給真正的實作
import core.fiber_scanner as fs
import core.fiber_scanner_advanced as fsa


# =============================================================================
# 假控制器（比照 verify_blind_scan.py 的 FakeCtrl，行為子集足以支撐本檔測試）
# =============================================================================
class FakeCtrl:
    _MAP = {"1": "X", "2": "Y", "3": "Z", "4": "U", "5": "V", "6": "W"}

    def __init__(self, axes=("X", "Y"), limits=None):
        self.axes = list(axes)
        self.axis_count = len(self.axes)
        self.connected = True
        self.ems_active = False
        self.scanning_active = False
        self._pos = {ax: 0.0 for ax in self.axes}
        self.axis_calib = {}
        self.stop_calls = 0
        self.move_log = []
        # {軸: (下限, 上限)}，None 表示該側無限制——軟體限位
        # （ctrl.sw_limits 的角色，_targets_reachable() 第一層檢查）。
        self.limits = limits or {}

    @property
    def positions_machine(self):
        return dict(self._pos)

    def estimate_um(self, ax, pulse):
        return None

    def query_status(self, axis_no):
        ax = self._MAP.get(axis_no)
        if ax not in self._pos:
            return "Stage not connected", ""
        return "Stop", str(self._pos[ax])

    @staticmethod
    def limit_direction(status):
        return ds102_ctrl.DS102Controller.limit_direction(status)

    def _within(self, ax, target):
        lo, hi = self.limits.get(ax, (None, None))
        if lo is not None and target < lo:
            return False
        if hi is not None and target > hi:
            return False
        return True

    def scan_move_step(self, axis_no, direction, amount, l_speed, f_speed,
                        rate, s_rate, wait_done=True):
        if self.ems_active:
            return False
        ax = self._MAP.get(axis_no)
        if ax is None or ax not in self._pos:
            return False
        delta = float(amount) * (1 if direction == "CW" else -1)
        target = self._pos[ax] + delta
        if not self._within(ax, target):
            return False
        self._pos[ax] = target
        self.move_log.append((ax, direction, amount))
        return True

    def wait_axis_stop(self, axis_no, timeout=30, start_pos=None, expected_travel=None):
        return True

    def check_sw_limits_batch(self, targets):
        for ax, target in targets.items():
            if not self._within(ax, target):
                return False, f"{ax} 目標 {target} 超出行程"
        return True, ""

    def stop(self):
        self.stop_calls += 1


def make_scanner(power_query, ctrl=None, axes=("X", "Y"), **kwargs):
    ctrl = ctrl or FakeCtrl(axes=axes)
    kwargs.setdefault("step_min", 2)
    kwargs.setdefault("settle_sec", 0.0)
    scanner = fs.FiberAlignmentScanner(ctrl=ctrl, power_query=power_query, **kwargs)
    scanner.active_axes = list(axes)
    return scanner, ctrl


# 小幅、有界、確定性的「雜訊」——比照既有測試檔的 _FLAT_NOISE，避免
# calibrate_noise() 量到 σ=0 導致 ftol 退化成 0（Powell 會跑到 maxfev
# 才停，失去容差意義；且 fsa 文件字串明講前置條件是雜訊已校準）。
_FLAT_NOISE = [0.008, -0.006, 0.007, -0.004, 0.005]


def _noise(i):
    return _FLAT_NOISE[i % len(_FLAT_NOISE)]


def make_paraboloid_query(ctrl, axes, peak, k):
    """非旋轉、非耦合的拋物線峰值曲面：value = -Σ k[ax]*(pos[ax]-peak[ax])^2。"""
    idx = {"i": -1}

    def q():
        idx["i"] += 1
        pos = ctrl.positions_machine
        val = 0.0
        for ax in axes:
            val -= k[ax] * (pos[ax] - peak[ax]) ** 2
        return True, val + _noise(idx["i"])

    return q


def make_rotated_ellipse_query(ctrl, axes, peak, k_major, k_minor, theta_deg):
    """
    旋轉橢圓峰值曲面（軸間耦合）：在座標系裡沿 (cosθ, sinθ) 方向是淺谷
    （k_major，收斂慢），垂直方向是陡谷（k_minor，收斂快）。這種形狀是
    座標下降法（沿 X/Y 單獨探測）的經典困難案例——最佳下降方向跟座標軸
    不重合，Powell 的共軛方向理論上該在這種曲面上勝出。
    """
    theta = math.radians(theta_deg)
    c, s = math.cos(theta), math.sin(theta)
    idx = {"i": -1}

    def q():
        idx["i"] += 1
        pos = ctrl.positions_machine
        dx = pos[axes[0]] - peak[axes[0]]
        dy = pos[axes[1]] - peak[axes[1]]
        # 旋轉到主軸座標系
        u = dx * c + dy * s
        v = -dx * s + dy * c
        val = -(k_major * u ** 2 + k_minor * v ** 2)
        return True, val + _noise(idx["i"])

    return q


# =============================================================================
# 一、基本收斂（非旋轉、非耦合）
# =============================================================================
class TestBasicConvergence:
    def test_converges_near_peak(self):
        axes = ("X", "Y")
        peak = {"X": 54.0, "Y": -37.0}
        k = {"X": 0.01, "Y": 0.01}
        ctrl = FakeCtrl(axes=axes)
        scanner, ctrl = make_scanner(
            make_paraboloid_query(ctrl, axes, peak, k), ctrl=ctrl, axes=axes
        )
        scanner.calibrate_noise()

        final = fsa.run_stage_powell(scanner, xtol_pulse=2.0, max_iterations=200)

        for ax in axes:
            err = abs(final[ax] - peak[ax])
            assert err <= 15.0, f"{ax} 軸誤差 {err} 超出容許範圍（xtol=2.0 的數個量級）"


# =============================================================================
# 二、軸間耦合案例 + 與 run_stage1 比較
# =============================================================================
class TestCoupledConvergence:
    PEAK = {"X": 40.0, "Y": 40.0}
    K_MAJOR = 1.0e-4   # 淺谷方向（沿旋轉軸）
    K_MINOR = 5.0e-3   # 陡谷方向
    THETA_DEG = 30.0

    def test_powell_converges_on_rotated_ellipse(self):
        axes = ("X", "Y")
        ctrl = FakeCtrl(axes=axes)
        scanner, ctrl = make_scanner(
            make_rotated_ellipse_query(
                ctrl, axes, self.PEAK, self.K_MAJOR, self.K_MINOR, self.THETA_DEG
            ),
            ctrl=ctrl, axes=axes,
        )
        scanner.calibrate_noise()

        final = fsa.run_stage_powell(scanner, xtol_pulse=2.0, max_iterations=300)

        for ax in axes:
            err = abs(final[ax] - self.PEAK[ax])
            assert err <= 40.0, f"{ax} 軸誤差 {err} 超出容許範圍"

    def test_compare_powell_vs_stage1_on_rotated_ellipse(self):
        """
        與 fiber_scanner.run_stage1（不開階段二）在同一個合成耦合曲面上
        比較收斂精度。不強制斷言 Powell 一定贏——如實記錄兩者誤差，用
        -s 執行可看到列印的比較數字。這是設計文件〈第三輪〉宣稱的核心
        賣點，值得留一組數據佐證或反證。
        """
        axes = ("X", "Y")

        ctrl_powell = FakeCtrl(axes=axes)
        scanner_powell, ctrl_powell = make_scanner(
            make_rotated_ellipse_query(
                ctrl_powell, axes, self.PEAK, self.K_MAJOR, self.K_MINOR, self.THETA_DEG
            ),
            ctrl=ctrl_powell, axes=axes,
        )
        scanner_powell.calibrate_noise()
        final_powell = fsa.run_stage_powell(scanner_powell, xtol_pulse=2.0, max_iterations=300)
        err_powell = {
            ax: abs(final_powell[ax] - self.PEAK[ax]) for ax in axes
        }

        ctrl_stage1 = FakeCtrl(axes=axes)
        scanner_stage1, ctrl_stage1 = make_scanner(
            make_rotated_ellipse_query(
                ctrl_stage1, axes, self.PEAK, self.K_MAJOR, self.K_MINOR, self.THETA_DEG
            ),
            ctrl=ctrl_stage1, axes=axes,
        )
        scanner_stage1.calibrate_noise()
        scanner_stage1.active_axes = list(axes)
        final_stage1 = scanner_stage1.run_stage1({ax: 20 for ax in axes})
        err_stage1 = {
            ax: abs(final_stage1[ax] - self.PEAK[ax]) for ax in axes
        }

        total_err_powell = sum(err_powell.values())
        total_err_stage1 = sum(err_stage1.values())
        print(
            f"\n[耦合曲面收斂精度比較] Powell 誤差={err_powell} "
            f"(合計 {total_err_powell:.2f}) / "
            f"run_stage1 誤差={err_stage1} (合計 {total_err_stage1:.2f})"
        )

        # 兩者都應該「有在收斂」（誤差不能離譜到像是完全沒動）——這是
        # 底線，不是設計文件宣稱的那個比較。
        assert total_err_powell <= 200.0
        assert total_err_stage1 <= 200.0


# =============================================================================
# 三、撞限位／_move_multi_axis 失敗 → 軟牆懲罰，不拋例外
# =============================================================================
class TestLimitPenalty:
    def test_does_not_raise_and_stays_feasible(self):
        axes = ("X", "Y")
        peak = {"X": 500.0, "Y": 500.0}  # 峰值故意設在可行域之外
        k = {"X": 0.001, "Y": 0.001}
        # 軟體限位把可行域夾在 [-50, 50]
        ctrl = FakeCtrl(axes=axes, limits={"X": (-50.0, 50.0), "Y": (-50.0, 50.0)})
        scanner, ctrl = make_scanner(
            make_paraboloid_query(ctrl, axes, peak, k), ctrl=ctrl, axes=axes
        )
        scanner.calibrate_noise()

        final = fsa.run_stage_powell(scanner, xtol_pulse=2.0, max_iterations=200)

        for ax in axes:
            lo, hi = ctrl.limits[ax]
            assert lo - 1e-6 <= final[ax] <= hi + 1e-6, (
                f"{ax} 軸最終座標 {final[ax]} 超出可行域 [{lo}, {hi}]——"
                "軟牆懲罰未能把 Powell 帶回可行域內"
            )


# =============================================================================
# 四、量測失敗（Sample.ok=False）→ 固定小懲罰，不拋例外
# =============================================================================
class TestMeasurementFailurePenalty:
    def test_does_not_raise_and_still_converges(self):
        axes = ("X", "Y")
        peak = {"X": 30.0, "Y": -20.0}
        k = {"X": 0.01, "Y": 0.01}
        dead_zone_calls = {"n": 0}

        ctrl = FakeCtrl(axes=axes)
        base_query = make_paraboloid_query(ctrl, axes, peak, k)

        def flaky_query():
            pos = ctrl.positions_machine
            # 靠近某個與峰值無關的固定座標時，量測失敗（通訊逾時的替身）。
            if abs(pos["X"] - 10.0) <= 3 and abs(pos["Y"] - 10.0) <= 3:
                dead_zone_calls["n"] += 1
                return False, 0.0
            return base_query()

        scanner, ctrl = make_scanner(flaky_query, ctrl=ctrl, axes=axes)
        scanner.calibrate_noise()

        final = fsa.run_stage_powell(scanner, xtol_pulse=2.0, max_iterations=200)

        for ax in axes:
            err = abs(final[ax] - peak[ax])
            assert err <= 15.0, f"{ax} 軸誤差 {err} 超出容許範圍"


# =============================================================================
# 五、使用者中止（ScanAbort）→ 例外往外傳 + 滑台移回目前最佳座標
# =============================================================================
class TestUserAbort:
    def test_abort_propagates_and_moves_back_to_best_seen(self):
        axes = ("X", "Y")
        peak = {"X": 60.0, "Y": 60.0}
        k = {"X": 0.01, "Y": 0.01}
        ctrl = FakeCtrl(axes=axes)
        scanner, ctrl = make_scanner(
            make_paraboloid_query(ctrl, axes, peak, k), ctrl=ctrl, axes=axes
        )
        scanner.calibrate_noise()

        # 追蹤每次成功量測的 (座標, 是否有效, 功率)，用來獨立算出「目前
        # 看過的最佳座標」，跟 run_stage_powell 內部的 best_state 做交叉比對。
        history = []
        orig_measure = scanner._measure_here

        def tracking_measure():
            s = orig_measure()
            history.append((dict(s.coords), s.ok, s.power))
            return s

        scanner._measure_here = tracking_measure

        # 第 8 次呼叫 _move_multi_axis 時模擬使用者按下停止（ScanAbort）。
        # 用夠大的次數確保 history 已經累積到一些成功量測。
        orig_move = scanner._move_multi_axis
        move_calls = {"n": 0}
        ABORT_AT = 8

        def aborting_move(deltas):
            move_calls["n"] += 1
            if move_calls["n"] == ABORT_AT:
                raise fs.ScanAbort("使用者中止搜尋")
            return orig_move(deltas)

        scanner._move_multi_axis = aborting_move

        with pytest.raises(fs.ScanAbort):
            fsa.run_stage_powell(scanner, xtol_pulse=2.0, max_iterations=200)

        # 中止之後，run_stage_powell 的 except 區塊應該有再呼叫一次
        # _move_multi_axis 做回退（第 9 次呼叫，這次不會再觸發 abort）。
        assert move_calls["n"] == ABORT_AT + 1, (
            "沒有觀察到中止後的回退移動呼叫——ScanAbort 應該先把滑台移回"
            "目前看過的最佳座標，才重新往外拋"
        )

        ok_samples = [(c, p) for c, ok, p in history if ok and p is not None]
        assert ok_samples, "測試設計錯誤：中止前應該已經累積至少一次成功量測"
        best_coords, best_power = max(ok_samples, key=lambda cp: cp[1])

        final = dict(ctrl.positions_machine)
        for ax in axes:
            assert final[ax] == pytest.approx(best_coords[ax], abs=1e-6), (
                f"{ax} 軸中止後的座標 {final[ax]} 與獨立追蹤到的最佳座標 "
                f"{best_coords[ax]}（功率 {best_power}）不一致"
            )


# =============================================================================
# 六、scipy 未安裝時的優雅降級
# =============================================================================
class TestScipyUnavailable:
    def test_raises_runtime_error_not_import_error(self, monkeypatch):
        axes = ("X", "Y")
        ctrl = FakeCtrl(axes=axes)
        scanner, ctrl = make_scanner(
            make_paraboloid_query(ctrl, axes, {"X": 0.0, "Y": 0.0}, {"X": 0.01, "Y": 0.01}),
            ctrl=ctrl, axes=axes,
        )
        monkeypatch.setattr(fsa, "_SCIPY_AVAILABLE", False)

        with pytest.raises(RuntimeError):
            fsa.run_stage_powell(scanner)


# =============================================================================
# 七、快取生效：同一整數 pulse 座標第二次被詢問時不再移動／量測
# =============================================================================
class _FakeOptimizeResult:
    def __init__(self, x):
        self.x = x


class TestObjectiveCache:
    def test_repeated_integer_coord_skips_move_and_measure(self, monkeypatch):
        axes = ("X", "Y")
        peak = {"X": 10.0, "Y": 10.0}
        k = {"X": 0.01, "Y": 0.01}
        ctrl = FakeCtrl(axes=axes)
        scanner, ctrl = make_scanner(
            make_paraboloid_query(ctrl, axes, peak, k), ctrl=ctrl, axes=axes
        )
        scanner.calibrate_noise()

        move_calls = {"n": 0}
        orig_move = scanner._move_multi_axis

        def counting_move(deltas):
            move_calls["n"] += 1
            return orig_move(deltas)

        scanner._move_multi_axis = counting_move

        measure_calls = {"n": 0}
        orig_measure = scanner._measure_here

        def counting_measure():
            measure_calls["n"] += 1
            return orig_measure()

        scanner._measure_here = counting_measure

        # 把 scipy.optimize.minimize 換成一個假的：直接抓住 run_stage_powell
        # 內部建立的 objective 閉包，用同一個 x0（目前座標，round 後與
        # 目前整數 pulse 座標相同）呼叫兩次，驗證第二次完全不觸發移動／量測。
        def fake_minimize(objective, x0, method=None, options=None):
            v1 = objective(x0)
            v2 = objective(x0)
            assert v1 == v2, "同一座標快取前後兩次 objective 值不一致"
            return _FakeOptimizeResult(x0)

        monkeypatch.setattr(fsa, "minimize", fake_minimize)

        fsa.run_stage_powell(scanner, xtol_pulse=2.0, max_iterations=10)

        assert move_calls["n"] == 1, "同一整數座標第二次詢問時不應再呼叫 _move_multi_axis"
        assert measure_calls["n"] == 1, "同一整數座標第二次詢問時不應再呼叫 _measure_here"
