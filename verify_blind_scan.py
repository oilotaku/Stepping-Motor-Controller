# -*- coding: utf-8 -*-
"""
FiberAlignmentScanner 階段零「盲搜粗掃」回歸測試（pytest，合成資料，不碰真實硬體）。

2026-08-26 新增，起因是實機事故：使用者在**無光位置**按下尋光，滑台一步
都沒動、82 個樣本全部無效、15 秒後才由階段二丟出一則指向錯誤位置的訊息
（「階段二起點量測失敗」）。根因有兩層，本檔鎖住的是第二層：

  1. 儀器層：`meter_GPIB` 初始化把量程無條件鎖在 -20dBm，無光時必定
     underrange，HP 8153A 回傳 IEEE-488.2 sentinel `+9.9E+37`，
     `get_power()` 判為失敗。（該層的修正在 meter_GPIB.py，不在本檔範圍。）
  2. 演算法層：起點量不到值時 `_search_axis_once()` 直接判定「此步長已
     收斂」而完全不移動，且 `_check_signal_detectable()` 因為有效讀值為 0
     而走「樣本太少，不誤殺」的放行分支——整條路徑靜默。

驗證對象：
  1. `_rect_spiral_offsets()` 的幾何正確性（覆蓋完整、無重複、由內而外）
  2. `calibrate_noise()` 對「一個有效讀值都拿不到」的偵測（baseline=None）
  3. `run()` 在讀不到值時**立刻**中止並指向光功率計，而不是空轉整個階段一
  4. `_check_signal_detectable()` 對「量了很多次但零有效讀值」的明確報錯
  5. `run_stage0_blind()` 找得到偏離起點的峰值、掃完找不到時的中止訊息
  6. 🔴 blind_mode="auto" 時，使用者中止／EMS **絕對不可**觸發盲搜
  7. `on_signal_found` 回呼（量程鎖定）只觸發一次
  8. 函式庫預設 blind_mode="off"（向下相容，fail-safe）

── 安全規則（同 verify_fiber_scanner_signal.py）──
  - 不連真實硬體：ctrl 用本檔的 FakeCtrl 頂替 DS102Controller。
  - 不寫入真實 recordings/：樣本落地路徑一律用 pytest 的 tmp_path。

執行方式：
    venv/Scripts/python.exe -m pytest verify_blind_scan.py -v
"""

import pytest

import fiber_scanner as fs


# =============================================================================
# 假控制器
# =============================================================================
class FakeCtrl:
    """
    假的 DS102Controller。

    與 verify_fiber_scanner_signal.py 的 FakeCtrl 有兩點刻意不同：
      - `wait_axis_stop()` 接受 `start_pos`／`expected_travel` 兩個關鍵字
        參數。盲搜走的是 `_move_multi_axis()`（多軸同時出發），那條路徑會
        傳這兩個位移提示（見 CLAUDE.md〈孿生競態〉），少了它們會 TypeError。
      - 支援軟體限位（`limits`），用來驗證盲搜對超出行程的格點是「跳過並
        繼續」而不是整個中止。
    """

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
        # {軸: (下限, 上限)}，None 表示該側無限制
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


def make_scanner(power_query, ctrl=None, **kwargs):
    ctrl = ctrl or FakeCtrl()
    kwargs.setdefault("step_min", 2)
    kwargs.setdefault("settle_sec", 0.0)
    scanner = fs.FiberAlignmentScanner(ctrl=ctrl, power_query=power_query, **kwargs)
    return scanner, ctrl


def make_dead_meter_query():
    """實機事故的重現：光功率計每次都回傳失敗（underrange sentinel 被判為失敗）。"""
    calls = {"n": 0}

    def q():
        calls["n"] += 1
        return False, 0.0

    q.calls = calls  # type: ignore[attr-defined]
    return q


_FLAT_NOISE = [0.008, -0.006, 0.007, -0.004, 0.005]


def make_flat_query(base=-75.0):
    """完全沒耦光：讀值有效，但與座標無關、只有微小有界擾動。"""
    idx = {"i": -1}

    def q():
        idx["i"] += 1
        return True, base + _FLAT_NOISE[idx["i"] % len(_FLAT_NOISE)]

    return q


def make_offset_peak_query(ctrl, peak, base=-75.0, amp=40.0, width=300.0):
    """
    只有走到 `peak` 附近才量得到訊號，其餘位置貼在底噪上。

    這是盲搜要解決的真實情境：起點完全在訊號區之外，梯度為零，爬坡類
    演算法在原地無從決定方向。
    """
    idx = {"i": -1}

    def q():
        idx["i"] += 1
        pos = ctrl.positions_machine
        d2 = sum((pos.get(ax, 0.0) - v) ** 2 for ax, v in peak.items())
        noise = _FLAT_NOISE[idx["i"] % len(_FLAT_NOISE)]
        if d2 > width ** 2:
            return True, base + noise
        return True, base + amp * (1.0 - (d2 ** 0.5) / width) + noise

    return q


# =============================================================================
# 一、方形螺旋的幾何正確性
# =============================================================================
class TestSpiralGeometry:
    @pytest.mark.parametrize("step,radius", [(1, 1), (1, 2), (1, 5), (200, 4000), (7, 70)])
    def test_covers_full_square_without_duplicates(self, step, radius):
        pts = list(fs._rect_spiral_offsets(step, radius))
        n = radius // step
        assert len(pts) == (2 * n + 1) ** 2
        assert len(set(pts)) == len(pts)

    def test_starts_at_center(self):
        assert list(fs._rect_spiral_offsets(10, 50))[0] == (0, 0)

    def test_expands_outward_monotonically(self):
        """由內而外：第一次抵達半徑 k 的順序必須隨 k 遞增。"""
        pts = list(fs._rect_spiral_offsets(1, 4))
        first = {}
        for i, (a, b) in enumerate(pts):
            first.setdefault(max(abs(a), abs(b)), i)
        indices = [first[k] for k in sorted(first)]
        assert indices == sorted(indices)

    def test_radius_smaller_than_step_yields_center_only(self):
        assert list(fs._rect_spiral_offsets(500, 100)) == [(0, 0)]

    def test_all_points_within_radius(self):
        for a, b in fs._rect_spiral_offsets(50, 500):
            assert max(abs(a), abs(b)) <= 500


# =============================================================================
# 二、calibrate_noise 對「零有效讀值」的偵測
# =============================================================================
class TestCalibrateNoise:
    def test_all_invalid_leaves_baseline_none(self):
        scanner, _ = make_scanner(make_dead_meter_query())
        sigma = scanner.calibrate_noise()
        assert sigma == 0.0
        assert scanner._noise_baseline is None

    def test_valid_readings_record_baseline(self):
        scanner, _ = make_scanner(make_flat_query(base=-70.0))
        scanner.calibrate_noise()
        assert scanner._noise_baseline is not None
        assert -70.1 < scanner._noise_baseline < -69.9

    def test_single_valid_reading_records_baseline_with_zero_sigma(self):
        seq = [(True, -50.0)] + [(False, 0.0)] * 10
        it = iter(seq)
        scanner, _ = make_scanner(lambda: next(it))
        scanner.calibrate_noise()
        assert scanner._noise_baseline == -50.0
        assert scanner._noise_sigma == 0.0


# =============================================================================
# 三、實機事故的直接回歸鎖：讀不到值要立刻中止，不可空轉
# =============================================================================
class TestDeadMeterAbortsImmediately:
    def test_run_aborts_with_meter_pointing_message(self, tmp_path):
        q = make_dead_meter_query()
        scanner, ctrl = make_scanner(q, ctrl=FakeCtrl(axes=("X", "Y", "Z")))
        scanner.run(initial_step={"X": 100, "Y": 100, "Z": 100}, scan_dir=tmp_path)

        assert scanner.last_abort_reason is not None
        assert "讀不到有效值" in scanner.last_abort_reason
        assert scanner.last_abort_kind == "no_signal"

    def test_run_does_not_spin_through_stage1(self, tmp_path):
        """
        事故當下量了 82 次、跑完 5 輪 cycle 才中止。修正後必須在雜訊校準
        之後就收手——量測次數不該超過 calibrate_noise 自己的取樣數。
        """
        q = make_dead_meter_query()
        scanner, ctrl = make_scanner(q, ctrl=FakeCtrl(axes=("X", "Y", "Z")))
        scanner.run(initial_step={"X": 100, "Y": 100, "Z": 100}, scan_dir=tmp_path)

        assert q.calls["n"] <= 6  # calibrate_noise 預設 5 次，容一次餘裕
        assert ctrl.move_log == []  # 一步都不該動

    def test_blind_search_also_refuses_without_baseline(self):
        scanner, _ = make_scanner(make_dead_meter_query(), blind_mode="always")
        scanner.calibrate_noise()
        with pytest.raises(fs.NoSignalAbort) as ei:
            scanner.run_stage0_blind()
        assert "沒有可用的底噪基準" in str(ei.value)


# =============================================================================
# 四、_check_signal_detectable：量很多次但零有效讀值
# =============================================================================
class TestZeroValidSamples:
    def test_many_samples_zero_valid_raises(self):
        scanner, ctrl = make_scanner(make_flat_query())
        samples = [fs.Sample(coords={"X": 0.0}, ok=False, power=None) for _ in range(20)]
        scanner._noise_sigma = 0.01
        with pytest.raises(fs.NoSignalAbort) as ei:
            scanner._check_signal_detectable(samples)
        msg = str(ei.value)
        assert "沒有任何一次讀到有效功率值" in msg
        assert "量程" in msg  # 訊息要指向真正的原因，不是含糊的「沒有訊號」

    def test_few_samples_still_passes_silently(self):
        """樣本太少仍是「不誤殺」放行，這條既有行為不可被新分支吃掉。"""
        scanner, _ = make_scanner(make_flat_query())
        scanner._noise_sigma = 0.01
        scanner._check_signal_detectable(
            [fs.Sample(coords={"X": 0.0}, ok=False, power=None) for _ in range(3)]
        )  # 不應拋出


# =============================================================================
# 五、盲搜本身
# =============================================================================
class TestBlindSearch:
    def test_finds_peak_away_from_start(self):
        ctrl = FakeCtrl(axes=("X", "Y"))
        scanner, _ = make_scanner(
            make_offset_peak_query(ctrl, peak={"X": 1000.0, "Y": -600.0}),
            ctrl=ctrl, blind_step=200, blind_max_radius=2000,
        )
        scanner.calibrate_noise()
        assert scanner.run_stage0_blind() is True
        pos = ctrl.positions_machine
        # 停在峰值的作用半徑內（width=300）
        assert (pos["X"] - 1000.0) ** 2 + (pos["Y"] + 600.0) ** 2 <= 300.0 ** 2

    def test_exhausted_sweep_reports_scan_scale(self):
        ctrl = FakeCtrl(axes=("X", "Y"))
        scanner, _ = make_scanner(
            make_flat_query(), ctrl=ctrl, blind_step=100, blind_max_radius=300,
        )
        scanner.calibrate_noise()
        with pytest.raises(fs.NoSignalAbort) as ei:
            scanner.run_stage0_blind()
        msg = str(ei.value)
        assert "掃完" in msg and "格點" in msg
        assert "100" in msg and "300" in msg  # 格距與半徑都要出現在訊息裡

    def test_skips_points_outside_limits_and_keeps_going(self):
        """超出行程的格點要跳過並繼續掃，不是整批中止。"""
        ctrl = FakeCtrl(axes=("X", "Y"), limits={"X": (-100.0, 100.0)})
        scanner, _ = make_scanner(
            make_flat_query(), ctrl=ctrl, blind_step=100, blind_max_radius=300,
        )
        scanner.calibrate_noise()
        with pytest.raises(fs.NoSignalAbort) as ei:
            scanner.run_stage0_blind()
        assert "略過" in str(ei.value)
        assert -100.0 <= ctrl.positions_machine["X"] <= 100.0

    def test_geometry_does_not_drift_after_blocked_points(self):
        """
        被限位擋掉的格點不可讓後續整個螺旋跟著平移——每點都用「絕對目標 −
        目前座標」重算 delta 就是為了這件事。掃過的座標必須全部落在 ±半徑內。
        """
        ctrl = FakeCtrl(axes=("X", "Y"), limits={"X": (-100.0, 100.0)})
        visited = []
        scanner, _ = make_scanner(
            make_flat_query(), ctrl=ctrl, blind_step=100, blind_max_radius=300,
            sample_cb=lambda s: visited.append((s.coords["X"], s.coords["Y"])),
        )
        scanner.calibrate_noise()
        with pytest.raises(fs.NoSignalAbort):
            scanner.run_stage0_blind()
        assert visited
        assert all(abs(x) <= 300 and abs(y) <= 300 for x, y in visited)

    def test_requires_two_axes(self):
        ctrl = FakeCtrl(axes=("X",))
        scanner, _ = make_scanner(make_flat_query(), ctrl=ctrl)
        scanner.calibrate_noise()
        with pytest.raises(fs.ScanAbort) as ei:
            scanner.run_stage0_blind()
        assert "兩個軸" in str(ei.value)

    def test_explicit_axes_outside_search_range_fall_back(self):
        ctrl = FakeCtrl(axes=("X", "Y"))
        logs = []
        scanner, _ = make_scanner(
            make_flat_query(), ctrl=ctrl, blind_step=100, blind_max_radius=100,
            blind_axes=("Z", "W"), progress_cb=lambda m: logs.append(m),
        )
        scanner.calibrate_noise()
        with pytest.raises(fs.NoSignalAbort):
            scanner.run_stage0_blind()
        assert any("不在本次搜尋範圍" in m for m in logs)

    def test_min_delta_floor_prevents_noise_false_positive(self):
        """
        σ→0 時純靠 σ 倍數會讓門檻塌成 0，任何雜訊尖峰都算「找到訊號」。
        絕對下限必須擋住這件事。
        """
        ctrl = FakeCtrl(axes=("X", "Y"))
        scanner, _ = make_scanner(
            make_flat_query(), ctrl=ctrl, blind_step=100, blind_max_radius=200,
            blind_signal_sigma_mult=0.0, blind_signal_min_delta_db=0.5,
        )
        scanner.calibrate_noise()
        with pytest.raises(fs.NoSignalAbort):
            scanner.run_stage0_blind()  # 擾動只有 ±0.008，遠低於 0.5 dB


# =============================================================================
# 六、模式串接與安全邊界
# =============================================================================
class TestBlindModeIntegration:
    def test_library_default_is_off(self):
        scanner, _ = make_scanner(make_flat_query())
        assert scanner.blind_mode == "off"
        assert fs.DEFAULT_BLIND_MODE == "off"

    def test_invalid_mode_falls_back_to_default(self):
        scanner, _ = make_scanner(make_flat_query(), blind_mode="nonsense")
        assert scanner.blind_mode == fs.DEFAULT_BLIND_MODE

    def test_off_mode_preserves_old_no_signal_abort(self, tmp_path):
        ctrl = FakeCtrl(axes=("X", "Y"))
        scanner, _ = make_scanner(make_flat_query(), ctrl=ctrl, blind_mode="off")
        scanner.run(initial_step={"X": 20, "Y": 20}, scan_dir=tmp_path)
        assert "沒有偵測到高於雜訊的訊號" in (scanner.last_abort_reason or "")

    def test_auto_mode_falls_back_to_blind_and_finds_signal(self, tmp_path):
        ctrl = FakeCtrl(axes=("X", "Y"))
        scanner, _ = make_scanner(
            make_offset_peak_query(ctrl, peak={"X": 800.0, "Y": 400.0}),
            ctrl=ctrl, blind_mode="auto", blind_step=200, blind_max_radius=1600,
        )
        scanner.run(initial_step={"X": 20, "Y": 20}, scan_dir=tmp_path)
        assert scanner.last_abort_reason is None  # 盲搜救回來了，整輪算完成
        pos = ctrl.positions_machine
        assert (pos["X"] - 800.0) ** 2 + (pos["Y"] - 400.0) ** 2 <= 400.0 ** 2

    def test_auto_mode_reports_blind_failure_when_nothing_found(self, tmp_path):
        ctrl = FakeCtrl(axes=("X", "Y"))
        scanner, _ = make_scanner(
            make_flat_query(), ctrl=ctrl, blind_mode="auto",
            blind_step=100, blind_max_radius=200,
        )
        scanner.run(initial_step={"X": 20, "Y": 20}, scan_dir=tmp_path)
        assert "掃完" in (scanner.last_abort_reason or "")
        assert scanner.last_abort_kind == "no_signal"

    def test_user_stop_never_triggers_blind_search(self, tmp_path):
        """
        🔴 最重要的安全鎖：使用者按停止也是 ScanAbort，若 run() 用
        `except ScanAbort` 攔截而不是 `except NoSignalAbort`，按下停止會
        立刻換來一輪掃過上千格點的盲搜——完全相反的結果。
        """
        ctrl = FakeCtrl(axes=("X", "Y"))
        scanner, _ = make_scanner(
            make_flat_query(), ctrl=ctrl, blind_mode="auto",
            blind_step=100, blind_max_radius=1000,
        )
        scanner.request_stop()
        scanner.run(initial_step={"X": 20, "Y": 20}, scan_dir=tmp_path)
        assert scanner.last_abort_reason == "使用者中止搜尋"
        assert scanner.last_abort_kind == "other"
        assert ctrl.move_log == []

    def test_ems_never_triggers_blind_search(self, tmp_path):
        ctrl = FakeCtrl(axes=("X", "Y"))
        ctrl.ems_active = True
        scanner, _ = make_scanner(
            make_flat_query(), ctrl=ctrl, blind_mode="auto",
            blind_step=100, blind_max_radius=1000,
        )
        scanner.run(initial_step={"X": 20, "Y": 20}, scan_dir=tmp_path)
        assert scanner.last_abort_reason == "EMS 觸發，搜尋中止"
        assert ctrl.move_log == []


# =============================================================================
# 七、on_signal_found（量程鎖定回呼）
# =============================================================================
class TestSignalFoundCallback:
    def test_called_once_when_blind_finds_signal(self):
        ctrl = FakeCtrl(axes=("X", "Y"))
        calls = []
        scanner, _ = make_scanner(
            make_offset_peak_query(ctrl, peak={"X": 600.0, "Y": 0.0}),
            ctrl=ctrl, blind_step=200, blind_max_radius=1200,
            on_signal_found=lambda p: calls.append(p),
        )
        scanner.calibrate_noise()
        scanner.run_stage0_blind()
        assert len(calls) == 1

    def test_not_called_when_no_signal(self):
        ctrl = FakeCtrl(axes=("X", "Y"))
        calls = []
        scanner, _ = make_scanner(
            make_flat_query(), ctrl=ctrl, blind_step=100, blind_max_radius=200,
            on_signal_found=lambda p: calls.append(p),
        )
        scanner.calibrate_noise()
        with pytest.raises(fs.NoSignalAbort):
            scanner.run_stage0_blind()
        assert calls == []

    def test_callback_exception_does_not_break_scan(self):
        ctrl = FakeCtrl(axes=("X", "Y"))

        def boom(p):
            raise RuntimeError("量程鎖定失敗")

        scanner, _ = make_scanner(
            make_offset_peak_query(ctrl, peak={"X": 600.0, "Y": 0.0}),
            ctrl=ctrl, blind_step=200, blind_max_radius=1200,
            on_signal_found=boom,
        )
        scanner.calibrate_noise()
        assert scanner.run_stage0_blind() is True  # 回呼爆掉不影響搜尋結果

    def test_reset_between_runs(self, tmp_path):
        """同一個 scanner 實例重複 run()，旗標要重置，否則第二輪不會再鎖量程。"""
        ctrl = FakeCtrl(axes=("X", "Y"))
        scanner, _ = make_scanner(make_flat_query(), ctrl=ctrl, blind_mode="off")
        scanner._signal_confirmed = True
        scanner.run(initial_step={"X": 20, "Y": 20}, scan_dir=tmp_path)
        # run() 開頭重置後，這一輪是純雜訊、沒確認到訊號，所以仍是 False
        assert scanner._signal_confirmed is False
