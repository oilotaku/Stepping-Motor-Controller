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

import ds102_ctrl  # 只為了 FakeCtrl.limit_direction 委派給真正的實作
import fiber_scanner as fs


# =============================================================================
# 假控制器
# =============================================================================
class FakeCtrl:
    """
    假的 DS102Controller。

    與 verify_fiber_scanner_signal.py 的 FakeCtrl 有三點刻意不同：
      - `wait_axis_stop()` 接受 `start_pos`／`expected_travel` 兩個關鍵字
        參數。盲搜走的是 `_move_multi_axis()`（多軸同時出發），那條路徑會
        傳這兩個位移提示（見 CLAUDE.md〈孿生競態〉），少了它們會 TypeError。
      - 支援軟體限位（`limits`），用來驗證盲搜對超出行程的格點是「跳過並
        繼續」而不是整個中止。
      - 支援**實體**限位（`hard_limits`，2026-08-26 新增）。這與 `limits`
        是兩件完全不同的事，混在一起看會誤解整個修正的重點：

          * `limits`（軟體限位）＝ `ctrl.sw_limits`，在**送指令前**就把
            目標擋掉，滑台一步都不會動。它的實際狀態是「預設六軸全是
            `(None, None)`、GUI 不填就是全部放行」。
          * `hard_limits`（實體限位開關）擋不住任何指令。軸會真的走到
            端點停住，`SB1?` 從此回報 `Detect CW/CCW limit`，而且會一直
            這樣回報到軸離開開關為止。這是實機唯一存在的那一層保護。

        `_note_limit_hit()` 要鎖的就是「只有第二層存在時不要反覆去撞它」，
        所以測試必須有辦法模擬第二層。
    """

    _MAP = {"1": "X", "2": "Y", "3": "Z", "4": "U", "5": "V", "6": "W"}

    def __init__(self, axes=("X", "Y"), limits=None, hard_limits=None):
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
        # {軸: (下限, 上限)}——實體限位開關，見上方 docstring
        self.hard_limits = hard_limits or {}
        self._limit_side = {}      # {軸: "CW"/"CCW"}，目前壓在哪一側
        self._pending_fail = set()  # wait_done=False 那條路徑要讓哪些軸的等待失敗
        self.limit_hits = []       # [(軸, 側)]，每次真的撞上記一筆，供斷言用

    @property
    def positions_machine(self):
        return dict(self._pos)

    def estimate_um(self, ax, pulse):
        return None

    def query_status(self, axis_no):
        ax = self._MAP.get(axis_no)
        if ax not in self._pos:
            return "Stage not connected", ""
        side = self._limit_side.get(ax)
        if side:
            # 壓在限位上時 SB1? 一直都是這個狀態（準位而非邊緣），
            # 這正是 _note_limit_hit() 在移動失敗後補查一次的前提。
            return f"Detect {side} limit", str(self._pos[ax])
        return "Stop", str(self._pos[ax])

    @staticmethod
    def limit_direction(status):
        """刻意委派給真正的實作，不自己再寫一份。

        CLAUDE.md 記著「比對方向字串必須先判斷 CCW」（`"CCW"` 本身含有
        `"CW"`）——測試裡複製一份等於讓那條規則有兩個來源，真正的實作
        改壞了測試也照樣綠燈。
        """
        return ds102_ctrl.DS102Controller.limit_direction(status)

    def _hard_clamp(self, ax, target):
        """回傳 (實際會停在哪, 撞到哪一側或 None)。"""
        lo, hi = self.hard_limits.get(ax, (None, None))
        if lo is not None and target < lo:
            return lo, "CCW"
        if hi is not None and target > hi:
            return hi, "CW"
        return target, None

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
        final, side = self._hard_clamp(ax, target)
        self._pos[ax] = final
        self.move_log.append((ax, direction, amount))
        if side is None:
            self._limit_side.pop(ax, None)  # 走離開關就不再壓著了
            return True
        self._limit_side[ax] = side
        self.limit_hits.append((ax, side))
        if wait_done:
            return False  # 同實機：_wait_axis_stop() 讀到 Limit 判失敗
        # wait_done=False：GO 確實送出去了（回 True），失敗要由後續的
        # wait_axis_stop() 回報——_move_multi_axis() 走的正是這條路徑。
        self._pending_fail.add(ax)
        return True

    def wait_axis_stop(self, axis_no, timeout=30, start_pos=None, expected_travel=None):
        ax = self._MAP.get(axis_no)
        if ax in self._pending_fail:
            self._pending_fail.discard(ax)
            return False
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


def make_peaked_query(ctrl, axis="X", center=30.0, amp=100.0, k=0.05):
    """
    有明顯高斯峰值（用二次曲線近似）情境：峰值在 center。

    ⚠ 刻意疊上 `_FLAT_NOISE` 的微小擾動。完全無雜訊時 `calibrate_noise()`
    會量到 σ=0，而 `_check_signal_detectable()` 在 `σ<=0` 時直接 return
    （無法計算期望全距），連「有訊號」的判定也一併跳過——那會讓本檔測
    `_signal_confirmed` 的案例失去意義。真實儀器不可能零雜訊。
    """
    idx = {"i": -1}

    def q():
        idx["i"] += 1
        noise = _FLAT_NOISE[idx["i"] % len(_FLAT_NOISE)]
        return True, amp - k * (ctrl.positions_machine[axis] - center) ** 2 + noise

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

    def test_off_mode_message_points_at_range_setting(self, tmp_path):
        """blind_mode="off" 時維持「立刻中止並直指量程」的行為。"""
        scanner, _ = make_scanner(
            make_dead_meter_query(), ctrl=FakeCtrl(axes=("X", "Y")), blind_mode="off",
        )
        scanner.run(initial_step={"X": 100, "Y": 100}, scan_dir=tmp_path)
        assert "量程" in (scanner.last_abort_reason or "")


# =============================================================================
# 三之二、底噪本身不可量測時的退化判準
#
# 🔴 2026-08-26 第一版把「起點讀不到值」當成不能盲搜的理由而直接中止，
# 那是判斷錯誤：sentinel（+9.9E+37）的語意是「低於可量測下限」，是明確
# 資訊而非未知——在連底噪都測不到的環境裡，任何一個讀得到的有效值本身
# 就已經高於底噪。實機 log 13:57／13:59 兩次全 sentinel，使用者選了 auto
# 模式卻直接看到「未偵測訊號」，盲搜一次都沒跑到。
# =============================================================================
class TestUnreadableBaseline:
    def test_blind_runs_and_sweeps_without_baseline(self):
        ctrl = FakeCtrl(axes=("X", "Y"))
        scanner, _ = make_scanner(
            make_dead_meter_query(), ctrl=ctrl,
            blind_mode="always", blind_step=100, blind_max_radius=200,
        )
        scanner.calibrate_noise()
        assert scanner._noise_baseline is None
        with pytest.raises(fs.NoSignalAbort) as ei:
            scanner.run_stage0_blind()
        # 真的掃了（不是拒絕執行），而且訊息說明是「全程低於可量測下限」
        assert "掃完" in str(ei.value)
        assert "低於儀器可量測下限" in str(ei.value)
        assert ctrl.move_log  # 滑台確實動過

    def test_any_readable_point_counts_as_signal(self):
        """底噪不可量測時，掃到第一個讀得到的點就算找到訊號。"""
        ctrl = FakeCtrl(axes=("X", "Y"))
        state = {"n": 0}

        def q():
            state["n"] += 1
            # 前 8 次（含雜訊校準的 5 次）全部讀不到，之後開始讀得到
            if state["n"] <= 8:
                return False, 0.0
            return True, -55.0

        scanner, _ = make_scanner(
            q, ctrl=ctrl, blind_mode="always", blind_step=100, blind_max_radius=400,
        )
        scanner.calibrate_noise()
        assert scanner._noise_baseline is None
        assert scanner.run_stage0_blind() is True

    def test_auto_mode_reaches_blind_when_baseline_unreadable(self, tmp_path):
        """實機 log 13:57／13:59 的回歸鎖：auto 模式必須真的跑到盲搜。"""
        ctrl = FakeCtrl(axes=("X", "Y"))
        logs = []
        scanner, _ = make_scanner(
            make_dead_meter_query(), ctrl=ctrl, blind_mode="auto",
            blind_step=100, blind_max_radius=200,
            progress_cb=lambda m: logs.append(m),
        )
        scanner.run(initial_step={"X": 100, "Y": 100}, scan_dir=tmp_path)
        assert any("直接進入階段零盲搜" in m for m in logs)
        assert any("階段零（盲搜）開始" in m for m in logs)
        assert ctrl.move_log  # 不再是「一步都不動」


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
# =============================================================================
# 實體限位：撞過一次就記住，不再往同一側反覆撞（2026-08-26）
# =============================================================================
class TestTravelBounds:
    """
    起因（實機 `logs/ds102_20260826_150901.log` 15:10 起）：一輪盲搜實撞了
    **50 次**限位，全部是 `Detect CCW limit`。

    成因是三件事疊在一起：

      1. `ctrl.sw_limits` 預設六軸都是 `(None, None)`，GUI 不填就沒有；
         控制器韌體的 `CWSLE`/`CCWSLE` 出廠停用、而且是 RAM-only。所以
         `check_sw_limits_batch()` 對所有目標一律放行——盲搜裡那句「先做
         批次限位檢查再送指令」實際上是 no-op。
      2. 盲搜半徑（實機設 10000 pulse）大於實際行程（Y 軸全程只有 4146
         pulse，見 docs/hardware.md），螺旋有很大一片在行程外。
      3. 每個格點都用「絕對目標 − 目前座標」重算 delta（這是刻意的，見
         `run_stage0_blind`），而卡在限位上的軸座標不會變，下一個格點算出
         的 delta 幾乎一樣 → 原地反覆撞同一顆開關，每次還連帶送一次
         `STOP 0`（停掉正在正常移動的另一軸）並彈一則限位警報橫幅。

    本 class 鎖的是修正後的行為：**同一軸的同一側，一輪最多實撞一次**。
    """

    def _peak_far_outside(self, ctrl):
        """峰值放在行程外——強迫盲搜掃完整個半徑，把每個格點都試過。"""
        return make_flat_query()

    def test_each_axis_side_is_hit_at_most_once(self):
        ctrl = FakeCtrl(
            axes=("X", "Y"),
            hard_limits={"X": (-100.0, 100.0), "Y": (-100.0, 100.0)},
        )
        scanner, _ = make_scanner(
            self._peak_far_outside(ctrl), ctrl=ctrl,
            blind_step=100, blind_max_radius=400,
        )
        scanner.calibrate_noise()
        with pytest.raises(fs.NoSignalAbort):
            scanner.run_stage0_blind()

        counts = {}
        for ax, side in ctrl.limit_hits:
            counts[(ax, side)] = counts.get((ax, side), 0) + 1
        assert counts, "測試設計錯誤：這個組態本來就該撞到限位"
        assert all(n == 1 for n in counts.values()), (
            f"同一側撞了不只一次：{counts}（修正前實機是 50 次）"
        )

    def test_bound_records_the_position_it_stopped_at(self):
        ctrl = FakeCtrl(axes=("X", "Y"), hard_limits={"X": (-100.0, 100.0)})
        scanner, _ = make_scanner(make_flat_query(), ctrl=ctrl, step_min=10)
        assert scanner._move_relative("X", 500) is False  # 撞 CW 端
        assert scanner._travel_bounds["X"][1] == 100.0
        assert scanner._travel_bounds["X"][0] is None     # CCW 側還沒撞過

    def test_bounded_direction_sends_no_further_command(self):
        """記住邊界之後，同方向的移動要在送指令前就被擋下，不再打到硬體。"""
        ctrl = FakeCtrl(axes=("X", "Y"), hard_limits={"X": (-100.0, 100.0)})
        scanner, _ = make_scanner(make_flat_query(), ctrl=ctrl, step_min=10)
        scanner._move_relative("X", 500)
        n_cmds = len(ctrl.move_log)
        n_hits = len(ctrl.limit_hits)

        assert scanner._move_relative("X", 500) is False
        assert scanner._move_relative("X", 10) is False
        assert len(ctrl.move_log) == n_cmds, "不該再送出任何指令"
        assert len(ctrl.limit_hits) == n_hits, "不該再撞一次"

    def test_opposite_direction_still_allowed(self):
        """只擋撞到的那一側。反方向是唯一走得掉的方向，擋掉它等於整軸鎖死。"""
        ctrl = FakeCtrl(axes=("X", "Y"), hard_limits={"X": (-100.0, 100.0)})
        scanner, _ = make_scanner(make_flat_query(), ctrl=ctrl, step_min=10)
        scanner._move_relative("X", 500)
        assert scanner._move_relative("X", -50) is True
        assert ctrl.positions_machine["X"] == 50.0

    def test_timeout_failure_does_not_record_a_bound(self):
        """
        🔴 移動失敗不等於撞限位。逾時／通訊失聯若被記成「行程末端」，
        那一側一整片區域會在這一輪被靜默排除，比多撞幾次嚴重得多。
        """
        class TimeoutCtrl(FakeCtrl):
            def scan_move_step(self, *a, **kw):
                return False  # 失敗，但 query_status 仍回 "Stop"（沒壓在限位上）

        ctrl = TimeoutCtrl(axes=("X", "Y"))
        scanner, _ = make_scanner(make_flat_query(), ctrl=ctrl, step_min=10)
        assert scanner._move_relative("X", 500) is False
        assert scanner._travel_bounds.get("X") in (None, [None, None])

    def test_repeat_hit_keeps_the_inner_value(self):
        """
        限位開關有實體作用寬度，同一顆開關每次停下的座標會差幾個 pulse。
        重複撞到時取靠內側的（CCW 取大、CW 取小）才是保守方向。
        """
        ctrl = FakeCtrl(axes=("X", "Y"))
        scanner, _ = make_scanner(make_flat_query(), ctrl=ctrl, step_min=10)
        ctrl._limit_side["X"] = "CCW"
        ctrl._pos["X"] = -100.0
        scanner._note_limit_hit("X")
        ctrl._pos["X"] = -98.0
        scanner._note_limit_hit("X")
        assert scanner._travel_bounds["X"][0] == -98.0
        ctrl._pos["X"] = -105.0
        scanner._note_limit_hit("X")
        assert scanner._travel_bounds["X"][0] == -98.0

    def test_bounds_reset_between_runs(self, tmp_path):
        """
        兩輪之間可能做過原點復歸，POS 是相對暫存器、復歸後座標整組改變，
        沿用舊邊界等於用錯誤的座標把一片區域靜默排除。

        用一組「陳舊到不可能成立」的邊界（把 X 夾在 10~20，而滑台在 0）
        當探針：只要 run() 有重置，這一輪沒撞過限位的軸就不會再有邊界；
        沒重置的話它會把 X 整條軸鎖死。
        """
        ctrl = FakeCtrl(axes=("X", "Y"))
        scanner, _ = make_scanner(
            make_flat_query(), ctrl=ctrl, blind_mode="auto",
            blind_step=100, blind_max_radius=200,
        )
        scanner._travel_bounds = {"X": [10.0, 20.0]}

        scanner.run(initial_step={"X": 20, "Y": 20}, scan_dir=tmp_path)
        assert "X" not in scanner._travel_bounds, (
            f"上一輪的陳舊邊界沒有被清掉：{scanner._travel_bounds}"
        )

    def test_user_sw_limits_still_apply(self):
        """新增的實測邊界是**疊加**在 ctrl.sw_limits 上，不是取代它。"""
        ctrl = FakeCtrl(axes=("X", "Y"), limits={"X": (-50.0, 50.0)})
        scanner, _ = make_scanner(make_flat_query(), ctrl=ctrl, step_min=10)
        ok, reason = scanner._targets_reachable({"X": 200.0})
        assert ok is False and "X" in reason

    def test_multi_axis_precheck_sends_nothing_when_out_of_bounds(self):
        """
        多軸路徑同樣要在送出前擋下——`_move_multi_axis()` 的失敗代價比單軸
        高：任一軸失敗就整批 `STOP 0`，會連坐停掉本來正常移動的另一軸。
        """
        ctrl = FakeCtrl(axes=("X", "Y"), hard_limits={"X": (-100.0, 100.0)})
        scanner, _ = make_scanner(make_flat_query(), ctrl=ctrl, step_min=10)
        scanner._move_multi_axis({"X": 500, "Y": 50})   # X 撞 CW 端
        n_cmds, n_stops = len(ctrl.move_log), ctrl.stop_calls

        assert scanner._move_multi_axis({"X": 500, "Y": 50}) is False
        assert len(ctrl.move_log) == n_cmds, "一根軸都不該送"
        assert ctrl.stop_calls == n_stops, "沒送出去就不需要 STOP"

    def test_blind_scan_still_covers_the_reachable_region(self):
        """擋掉走不到的格點之後，走得到的那些仍然要照掃，而且幾何不漂移。"""
        ctrl = FakeCtrl(axes=("X", "Y"), hard_limits={"X": (-100.0, 100.0)})
        visited = []
        scanner, _ = make_scanner(
            make_flat_query(), ctrl=ctrl,
            blind_step=100, blind_max_radius=300,
            sample_cb=lambda s: visited.append((s.coords["X"], s.coords["Y"])),
        )
        scanner.calibrate_noise()
        with pytest.raises(fs.NoSignalAbort):
            scanner.run_stage0_blind()
        assert visited
        for x, y in visited:
            assert -100.0 <= x <= 100.0        # 沒有一點停在行程外
            assert -300.0 <= y <= 300.0        # 也沒有漂出使用者設定的半徑
        # 走得到的那一段（X∈{-100,0,100}×Y∈{-300..300}）要真的掃到
        assert len({(x, y) for x, y in visited}) >= 15


# =============================================================================
# 階段一一定要停得下來（2026-08-26）
# =============================================================================
class TestStage1Termination:
    """
    與 TestTravelBounds 同一次修正，但這是**另一個**根因，兩個疊在一起才
    是實機「尋光一直跳 Detect limit」的完整解釋：

      - `_search_axis_once()` 只要「有移動」就回報未收斂，而外層
        `while s >= step_min` 只在收斂時才縮步。
      - 方向探測用的是赤裸的 `p_plus > p0`，沒有雜訊門檻（同一個函式裡的
        爬坡迴圈一直都用 `> p_curr + noise_floor`，這個不對稱既有註解自己
        就寫著是「選擇偏誤」）。

    純雜訊環境下兩側輪流「看起來比較好」→ 每次呼叫都移動 → 永遠不縮步 →
    永遠不結束。假物件重現（起點壓在限位上、讀值只有雜訊）：**舊版跑到
    180 萬次移動仍在原地兩點之間擺盪，其中 60 萬次是真的撞在限位開關上**。
    """

    def test_pure_noise_from_a_limit_terminates(self, tmp_path):
        budget = 3000

        class BudgetCtrl(FakeCtrl):
            """超過預算就炸——測不出來的話會變成整個測試套件掛住。"""

            def scan_move_step(self, *a, **kw):
                if len(self.move_log) >= budget:
                    raise RuntimeError(
                        f"階段一未收斂：移動次數超過 {budget}（舊版是 180 萬次）"
                    )
                return super().scan_move_step(*a, **kw)

        ctrl = BudgetCtrl(axes=("X", "Y"), hard_limits={"X": (-100.0, 100.0)})
        scanner, _ = make_scanner(make_flat_query(), ctrl=ctrl, step_min=10)
        scanner._move_relative("X", 500)          # 先把 X 推到端點壓住
        scanner.run({"X": 10, "Y": 10}, scan_dir=tmp_path)

        assert len(ctrl.move_log) < budget
        # 撞限位的次數才是使用者實際看到的東西（每次都是一則警報橫幅）
        assert len(ctrl.limit_hits) <= 4, f"撞太多次：{ctrl.limit_hits}"

    def test_direction_probe_requires_noise_floor(self):
        """
        方向探測的門檻是 `p0 + 雜訊底限`。低於底限的「改善」不是資訊，
        拿它決定方向等於讓雜訊駕駛滑台——這正是上面那個無窮擺盪的來源。
        """
        ctrl = FakeCtrl(axes=("X", "Y"))
        readings = iter([
            -50.0,   # p0（起點）
            -49.99,  # +step：比 p0 大，但遠小於雜訊底限 → 不可採信
            -49.99,  # -step：同上
        ])

        def q():
            try:
                return True, next(readings)
            except StopIteration:
                return True, -50.0

        scanner, _ = make_scanner(q, ctrl=ctrl, step_min=10)
        scanner._noise_sigma = 0.5        # 底限 = 3σ = 1.5 dB
        scanner._noise_baseline = -50.0

        converged, _ = scanner._search_axis_once("X", 10)
        assert converged is True, "雜訊等級的差異不該被當成找到方向"
        assert ctrl.positions_machine["X"] == 0.0, "兩側都試過後必須回到原點"

    def test_real_gradient_is_still_followed(self):
        """保險絲與雜訊門檻都不可以擋掉真正的梯度——這才是尋光本體。"""
        ctrl = FakeCtrl(axes=("X", "Y"))
        scanner, _ = make_scanner(
            make_peaked_query(ctrl, axis="X", center=100.0, amp=100.0, k=0.05),
            ctrl=ctrl, step_min=10,
        )
        scanner.calibrate_noise()
        scanner.run_stage1({"X": 40, "Y": 40})
        assert abs(ctrl.positions_machine["X"] - 100.0) <= 20.0


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


# =============================================================================
# 八、階段一「正常收斂但從未偵測到訊號」也要補盲搜
#
# 實機 log 13:08：X 軸撞限位使第一輪只收到 11 筆樣本，有效的不足 4 個，
# `_check_signal_detectable()` 走「樣本太少，不誤殺」的放行分支，接著
# `total_improvement < noise_floor` 讓外層迴圈 break——run_stage1 就這樣
# **正常 return**，沒有拋 NoSignalAbort。第一版只攔例外，於是沒有盲搜，
# 最後由階段二丟出「起點量測失敗」。判準因此改成 `_signal_confirmed` 旗標。
# =============================================================================
class TestStage1ConvergesWithoutSignal:
    def test_auto_mode_falls_back_after_silent_convergence(self, tmp_path):
        ctrl = FakeCtrl(axes=("X", "Y"))
        logs = []
        # 讀值有效但完全平坦，且限制樣本數：X 軸行程極窄 -> 探測很快撞限位
        scanner, _ = make_scanner(
            make_flat_query(), ctrl=ctrl, blind_mode="auto",
            blind_step=100, blind_max_radius=200,
            abort_if_no_signal=False,  # 關掉中止 -> 階段一必定「正常收斂」
            progress_cb=lambda m: logs.append(m),
        )
        scanner.run(initial_step={"X": 20, "Y": 20}, scan_dir=tmp_path)
        assert any("階段零（盲搜）開始" in m for m in logs),             "階段一正常收斂但從未確認訊號時，auto 模式仍必須補跑盲搜"

    def test_abort_switch_off_still_evaluates_signal(self):
        """
        關掉「無訊號時中止」不可連「有訊號」的判定也一起跳過——否則
        `_signal_confirmed` 永遠是 False，run() 會誤以為從未偵測到訊號。
        """
        ctrl = FakeCtrl(axes=("X",))
        scanner, _ = make_scanner(
            make_peaked_query(ctrl), ctrl=ctrl, abort_if_no_signal=False,
        )
        scanner.calibrate_noise()
        scanner.run_stage1({"X": 64})
        assert scanner._signal_confirmed is True

    def test_no_blind_when_already_on_signal(self, tmp_path):
        """
        已經站在訊號上、只是樣本數不足以讓判準表態時，不該白跑一輪盲搜。
        `_power_clearly_above_baseline()` 就是為此存在。
        """
        ctrl = FakeCtrl(axes=("X", "Y"))
        logs = []
        scanner, _ = make_scanner(
            make_peaked_query(ctrl, axis="X"), ctrl=ctrl, blind_mode="auto",
            blind_step=100, blind_max_radius=1000,
            progress_cb=lambda m: logs.append(m),
        )
        scanner.run(initial_step={"X": 64, "Y": 64}, scan_dir=tmp_path)
        assert not any("階段零（盲搜）開始" in m for m in logs)

    def test_power_check_returns_actual_reading_not_bool(self):
        ctrl = FakeCtrl(axes=("X", "Y"))
        scanner, _ = make_scanner(lambda: (True, -20.0), ctrl=ctrl)
        scanner._noise_baseline = -70.0
        scanner._noise_sigma = 0.1
        assert scanner._power_clearly_above_baseline() == -20.0

    def test_power_check_returns_none_below_threshold(self):
        ctrl = FakeCtrl(axes=("X", "Y"))
        scanner, _ = make_scanner(lambda: (True, -69.9), ctrl=ctrl)
        scanner._noise_baseline = -70.0
        scanner._noise_sigma = 0.0
        assert scanner._power_clearly_above_baseline() is None

    def test_power_check_treats_readable_as_signal_when_baseline_unreadable(self):
        ctrl = FakeCtrl(axes=("X", "Y"))
        scanner, _ = make_scanner(lambda: (True, -80.0), ctrl=ctrl)
        scanner._noise_baseline = None
        assert scanner._power_clearly_above_baseline() == -80.0
