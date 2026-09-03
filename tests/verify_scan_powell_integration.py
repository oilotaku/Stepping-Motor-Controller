# -*- coding: utf-8 -*-
"""
Powell 尋光「接入 run() 與 GUI」整合回歸測試（pytest，假物件，不碰真實硬體）。

背景：`fiber_scanner_advanced.run_stage_powell()` 本身已有 `verify_scan_powell.py`
的 7 組測試覆蓋（收斂性、撞限位、量測失敗、使用者中止、scipy 降級、快取）。
但那些測試全部直接呼叫 `run_stage_powell()`，不經過
`FiberAlignmentScanner.run(algorithm="powell", ...)` 這一層——2026-08-28
把 Powell 接進 `run()`（新增 `_run_primary_algorithm()` 分流、
`_check_signal_detectable()` 補檢查、`persist_samples()` 的 JSON metadata、
main_ai.py 的演算法下拉選單）之後，這一層整合邏輯完全沒有測試覆蓋。

本檔驗證範圍（對應任務書的 7 個項目）：
  1. `run(algorithm="powell", ...)` 端到端：正常收斂、`scanning_active`
     歸零、`persist_samples()` 的 JSON 含 `algorithm`/`powell_params`。
  2. `enable_stage2=True` + `algorithm="powell"`：不拋例外、`run_stage2`
     真的沒被呼叫、有留下警告 log。
  3. Powell 路徑下的「無訊號」情境：`abort_if_no_signal=True` 時真的會
     中止（`_check_signal_detectable()` 涵蓋 Powell 路徑）；
     `blind_mode="auto"` 時會觸發回頭補盲搜。
  4. `_run_primary_algorithm` 的 `swallow_no_signal=False` 分支：補盲搜
     之後的第二次呼叫若仍無訊號，例外要真的往外傳、中止整輪，
     不能被靜默吞掉繼續跑去 `run_stage3()`——這是 coder 在
     `_run_primary_algorithm()` docstring 裡的關鍵行為聲明，用假物件
     實測而非只看程式碼判斷是否成立。
  5. `algorithm` 參數傳入不合法字串時的白名單防禦（退回
     "coordinate_descent"，不呼叫 `fiber_scanner_advanced.run_stage_powell`）。
  6. main_ai.py「尋光」分頁：演算法 Combobox 切到 Powell 時，「啟用階段二
     局部精修」checkbox 與「起始步長」Entry 是否正確變成 disabled；
     切回座標下降時是否正確恢復。
  7. `scanner_config.json` 存了 `"algorithm": "powell"` 但環境沒裝 scipy
     （monkeypatch `fiber_scanner_advanced._SCIPY_AVAILABLE = False`）時，
     GUI 初始化與 `_do_start_scan()` 是否都正確降級成座標下降。

── 安全規則（同 verify_scan_powell.py／verify_blind_scan.py／conftest.py）──
  - 不連真實硬體：fiber_scanner.py 層級的測試用本檔自己的 FakeCtrl 頂替
    DS102Controller；main_ai.py 層級的測試透過 conftest.make_gui() 建立
    真的 DS102Controller 實例，但 self.ser 全程維持 None，逐一
    monkeypatch 個別方法。
  - 不寫入真實 recordings/：任何直接呼叫 `scanner.run(...)` 的測試都
    明確傳入 `scan_dir=tmp_path`（`persist_samples()` 預設輸出目錄若不
    覆寫會落在 `fiber_scanner.py` 所在目錄的 `recordings/scans/`，那是
    專案真正的目錄）；main_ai.py 層級的測試透過 make_gui() 把
    `RECORDING_DIR` 導向暫存目錄。

執行方式：
    venv/Scripts/python.exe -m pytest verify_scan_powell_integration.py -v
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import core.ds102_ctrl as ds102_ctrl
import core.fiber_scanner as fs
import core.fiber_scanner_advanced as fsa
import main_ai

from conftest import close_gui, make_gui, pump_until


# =============================================================================
# 假控制器（比照 verify_scan_powell.py 的 FakeCtrl；本檔案獨立一份，見
# docs/testing.md「各檔 FakeCtrl 彼此不通用」——不同測試檔的 FakeCtrl
# 即使外觀相似，也不共用同一份定義，避免其中一邊的修改悄悄影響另一邊）。
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
        self.move_log = []
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
        pass


def make_scanner(power_query, ctrl=None, axes=("X", "Y"), **kwargs):
    ctrl = ctrl or FakeCtrl(axes=axes)
    kwargs.setdefault("step_min", 2)
    kwargs.setdefault("settle_sec", 0.0)
    scanner = fs.FiberAlignmentScanner(ctrl=ctrl, power_query=power_query, **kwargs)
    return scanner, ctrl


# 小幅、有界、確定性的「雜訊」——比照 verify_scan_powell.py 的 _FLAT_NOISE，
# 避免 calibrate_noise() 量到 σ=0。
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


def make_flat_query(base=-45.0):
    """
    全程回傳同一水準（±小雜訊）的合成量測——模擬「完全沒耦光，只有電子
    雜訊」的情境：不管滑台移到哪裡，讀值的『範圍』都貼在雜訊水準，
    _check_signal_detectable() 應該要判定「沒有偵測到訊號」。

    這跟「讀不到值」（sentinel／meter_ok=False）是兩回事——這裡刻意讓
    每次量測都 ok=True、都是有效數字，只是完全沒有空間結構，用來驗證
    _check_signal_detectable() 的「range 太小」判準，而不是「读值全部
    無效」判準（那個已經有 verify_fiber_scanner_signal.py 覆蓋）。
    """
    idx = {"i": -1}

    def q():
        idx["i"] += 1
        return True, base + _noise(idx["i"])

    return q


# 提供給 run_stage_powell 假替身使用。
# 🔴 2026-08-28 更新：_powell_param_snapshot() 已改成直接讀
# fiber_scanner_advanced 的模組常數（DEFAULT_XTOL_PULSE 等），不再用
# inspect.signature 反射「目前掛在 run_stage_powell 這個名字上的物件」的
# 簽章——即使假替身的參數預設值跟真正的 run_stage_powell 不同，也不會
# 讓 persist_samples() 炸掉。但假替身仍必須接受 `early_signal_check`
# 關鍵字參數（即使不透過它做任何事）：`_run_primary_algorithm()` 一律
# 會傳這個關鍵字，簽章沒有它會直接 TypeError。這裡進一步在每次假量測
# 之後呼叫它一次，模擬真正 run_stage_powell() 的 objective() 每次完成
# 測量就觸發 early_signal_check 的行為，讓依賴這個假替身的測試（見下方
# TestSecondNoSignalPropagates）能繼續驗證到「無訊號」判定真的有觸發。
def _fake_run_stage_powell_factory(n_measure=6):
    calls = {"n": 0}

    def fake_run_stage_powell(
        scanner, xtol_pulse=5.0, ftol_sigma_mult=3.0, max_iterations=200,
        penalty_lambda=None, early_signal_check=None,
    ):
        calls["n"] += 1
        for _ in range(n_measure):
            scanner._measure_here()
            if early_signal_check is not None:
                early_signal_check()
        return dict(scanner.ctrl.positions_machine)

    return fake_run_stage_powell, calls


# =============================================================================
# 一、run(algorithm="powell") 端到端
# =============================================================================
class TestRunEndToEndPowell:
    def test_completes_and_resets_scanning_active(self, tmp_path):
        axes = ("X", "Y")
        peak = {"X": 40.0, "Y": -20.0}
        k = {"X": 0.01, "Y": 0.01}
        ctrl = FakeCtrl(axes=axes)
        scanner, ctrl = make_scanner(
            make_paraboloid_query(ctrl, axes, peak, k), ctrl=ctrl, axes=axes,
            blind_mode="off",
        )

        result = scanner.run(
            initial_step={ax: 20 for ax in axes},
            algorithm="powell",
            powell_max_iterations=150,
            scan_dir=tmp_path,
        )

        assert scanner.last_abort_reason is None, (
            f"預期正常收斂完成，卻中止：{scanner.last_abort_reason}"
        )
        assert ctrl.scanning_active is False
        for ax in axes:
            assert abs(result[ax] - peak[ax]) <= 25.0

    def test_persist_samples_json_has_algorithm_and_powell_params(self, tmp_path):
        axes = ("X", "Y")
        peak = {"X": 10.0, "Y": 10.0}
        k = {"X": 0.01, "Y": 0.01}
        ctrl = FakeCtrl(axes=axes)
        scanner, ctrl = make_scanner(
            make_paraboloid_query(ctrl, axes, peak, k), ctrl=ctrl, axes=axes,
            blind_mode="off",
        )

        scanner.run(
            initial_step={ax: 20 for ax in axes},
            algorithm="powell",
            powell_max_iterations=100,
            scan_dir=tmp_path,
        )

        files = list(Path(tmp_path).glob("scan_*.json"))
        assert len(files) == 1, f"預期恰好一份樣本檔，實際 {len(files)} 份"
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["algorithm"] == "powell"
        assert "powell_params" in data
        assert set(data["powell_params"]) == {
            "xtol_pulse", "ftol_sigma_mult", "penalty_lambda", "max_iterations",
        }
        assert data["powell_params"]["max_iterations"] == 100

    def test_coordinate_descent_json_omits_powell_params(self, tmp_path):
        """對照組：座標下降不該寫 powell_params 鍵（見 persist_samples() 註解）。"""
        axes = ("X", "Y")
        peak = {"X": 10.0, "Y": 10.0}
        k = {"X": 0.01, "Y": 0.01}
        ctrl = FakeCtrl(axes=axes)
        scanner, ctrl = make_scanner(
            make_paraboloid_query(ctrl, axes, peak, k), ctrl=ctrl, axes=axes,
            blind_mode="off",
        )

        scanner.run(
            initial_step={ax: 20 for ax in axes},
            algorithm="coordinate_descent",
            scan_dir=tmp_path,
        )

        files = list(Path(tmp_path).glob("scan_*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["algorithm"] == "coordinate_descent"
        assert "powell_params" not in data


# =============================================================================
# 二、enable_stage2=True + algorithm="powell" → 階段二被忽略，不拋例外
# =============================================================================
class TestStage2IgnoredWithPowell:
    def test_stage2_not_called_and_warning_logged(self, tmp_path, monkeypatch):
        axes = ("X", "Y")
        peak = {"X": 30.0, "Y": 10.0}
        k = {"X": 0.01, "Y": 0.01}
        ctrl = FakeCtrl(axes=axes)
        logs = []
        scanner, ctrl = make_scanner(
            make_paraboloid_query(ctrl, axes, peak, k), ctrl=ctrl, axes=axes,
            blind_mode="off", progress_cb=logs.append,
        )

        stage2_calls = {"n": 0}

        def spy_stage2(*a, **kw):
            stage2_calls["n"] += 1
            return {}

        monkeypatch.setattr(scanner, "run_stage2", spy_stage2)

        scanner.run(
            initial_step={ax: 20 for ax in axes},
            enable_stage2=True,
            algorithm="powell",
            powell_max_iterations=100,
            scan_dir=tmp_path,
        )

        assert stage2_calls["n"] == 0, (
            "enable_stage2=True 對 Powell 路徑應被忽略，run_stage2 不該被呼叫"
        )
        assert any("忽略「啟用階段二局部精修」" in m for m in logs), (
            "應該留下警告 log，讓使用者知道這個勾選對 Powell 沒有效果"
        )
        assert scanner.last_abort_reason is None
        assert scanner.enable_stage2 is True  # 旗標本身仍如實記錄使用者的選擇


# =============================================================================
# 三、Powell 路徑「無訊號」情境
# =============================================================================
class TestNoSignalPowell:
    def test_abort_if_no_signal_true_aborts_with_blind_off(self, tmp_path):
        """
        blind_mode="off"：全程雜訊貼底、沒有真正訊號峰值，abort_if_no_signal
        預設 True，Powell 路徑也必須被 _check_signal_detectable() 攔下來，
        不可以像改動前那樣完全繞過這道檢查。
        """
        axes = ("X", "Y")
        ctrl = FakeCtrl(axes=axes)
        scanner, ctrl = make_scanner(
            make_flat_query(), ctrl=ctrl, axes=axes, blind_mode="off",
        )

        scanner.run(
            initial_step={ax: 20 for ax in axes},
            algorithm="powell",
            powell_max_iterations=30,
            scan_dir=tmp_path,
        )

        assert scanner.last_abort_kind == "no_signal", (
            f"預期無訊號中止，實際 last_abort_kind={scanner.last_abort_kind}，"
            f"reason={scanner.last_abort_reason}"
        )
        assert ctrl.scanning_active is False
        # 驗證第二項修法（Powell 路徑的無訊號偵測提早生效）：blind_mode="off"
        # 時全部樣本都來自 Powell 的 objective()，早偵測 hook 讓中止發生在
        # 遠少於 max_iterations（本測試傳入 30）次量測內——改動前要等
        # minimize() 整個跑完才檢查，樣本數會貼著 30 附近；改動後
        # _check_signal_detectable() 在有效樣本一達到 4 筆就會判定，這裡
        # 用 20 當寬鬆上界，只要遠低於 30 就代表提早偵測真的生效了。
        assert len(scanner.samples) < 20, (
            f"預期無訊號偵測遠早於 max_iterations 次量測內觸發，"
            f"實際量測了 {len(scanner.samples)} 次——提早偵測可能沒有生效"
        )

    def test_blind_mode_auto_triggers_blind_retry(self, tmp_path, monkeypatch):
        """
        blind_mode="auto"：Powell 第一次判定無訊號後，run() 應該回到起點
        補跑一次階段零盲搜（本例盲搜範圍內同樣沒有訊號，最終仍中止，但
        重點是「有沒有真的觸發盲搜」）。
        """
        axes = ("X", "Y")
        ctrl = FakeCtrl(axes=axes)
        logs = []
        scanner, ctrl = make_scanner(
            make_flat_query(), ctrl=ctrl, axes=axes, blind_mode="auto",
            blind_step=50, blind_max_radius=100, progress_cb=logs.append,
        )

        # 用 spy 包住真正的 run_stage_powell，只記錄「第一次呼叫」耗用了
        # 幾次量測——盲搜格點本身也會產生大量樣本，不能直接拿
        # len(scanner.samples) 的總數當提早偵測是否生效的證據，必須把
        # Powell 這一段單獨量出來。
        orig_run_stage_powell = fsa.run_stage_powell
        first_call_sample_count = {"n": None}
        call_seq = {"n": 0}

        def spy(scanner_arg, *a, **kw):
            before = len(scanner_arg.samples)
            try:
                return orig_run_stage_powell(scanner_arg, *a, **kw)
            finally:
                if call_seq["n"] == 0:
                    first_call_sample_count["n"] = len(scanner_arg.samples) - before
                call_seq["n"] += 1

        monkeypatch.setattr(fsa, "run_stage_powell", spy)

        scanner.run(
            initial_step={ax: 20 for ax in axes},
            algorithm="powell",
            powell_max_iterations=30,
            scan_dir=tmp_path,
        )

        assert first_call_sample_count["n"] is not None, "run_stage_powell 應該至少被呼叫一次"
        assert first_call_sample_count["n"] < 20, (
            "第一次 Powell 呼叫應該在遠少於 max_iterations 次量測內就被提早偵測中止，"
            f"實際該次呼叫量測了 {first_call_sample_count['n']} 次"
        )

        assert any("回到起點改跑階段零盲搜" in m for m in logs), (
            "blind_mode=auto 且 Powell 判定無訊號時，應該有觸發回頭補盲搜的 log"
        )
        assert any("階段零（盲搜）開始" in m for m in logs), (
            "應該觀察到 run_stage0_blind() 真的被呼叫（不是只記了一行 log 就跳過）"
        )
        assert scanner.last_abort_kind == "no_signal"


# =============================================================================
# 四、swallow_no_signal=False：補盲搜後第二次仍無訊號要真的中止整輪
# =============================================================================
class TestSecondNoSignalPropagates:
    def test_second_call_after_blind_success_still_raises_and_skips_stage3(
        self, tmp_path, monkeypatch
    ):
        """
        coder 在 `_run_primary_algorithm()` docstring 裡的聲明：補盲搜之後
        的第二次呼叫（swallow_no_signal=False）若仍然無訊號，NoSignalAbort
        必須真的往外傳、把整輪中止，不能被靜默吞掉繼續跑到 run_stage3()。

        用假的 run_stage_powell（只做量測、不做真正的最佳化）＋假的
        run_stage0_blind（直接呼叫 _confirm_signal() 模擬「盲搜找到訊號」，
        不需要真的跑方形螺旋——那部分 verify_blind_scan.py 已覆蓋）把測試
        焦點收斂在 _run_primary_algorithm() 這一層的分流邏輯本身：
          1. 第一次呼叫（swallow_no_signal=True）：量到全平的雜訊 → 判定
             無訊號 → 因為 blind_mode=="auto" 而被吞掉，回傳 True。
          2. run() 接著呼叫（我們的假）run_stage0_blind()，它直接確認
             訊號、返回 True。
          3. run() 用 swallow_no_signal=False 再呼叫一次主演算法 → 假
             run_stage_powell 同樣只量到平的雜訊 → 判定無訊號 → 這次
             必須真的往外傳。
        """
        axes = ("X", "Y")
        ctrl = FakeCtrl(axes=axes)
        scanner, ctrl = make_scanner(
            make_flat_query(), ctrl=ctrl, axes=axes, blind_mode="auto",
        )

        fake_run_stage_powell, powell_calls = _fake_run_stage_powell_factory(n_measure=6)
        monkeypatch.setattr(fsa, "run_stage_powell", fake_run_stage_powell)

        blind_calls = {"n": 0}

        def fake_blind(center=None):
            blind_calls["n"] += 1
            scanner._confirm_signal(-10.0)
            return True

        monkeypatch.setattr(scanner, "run_stage0_blind", fake_blind)

        stage3_calls = {"n": 0}

        def fake_stage3():
            stage3_calls["n"] += 1
            return dict(scanner.ctrl.positions_machine)

        monkeypatch.setattr(scanner, "run_stage3", fake_stage3)

        scanner.run(
            initial_step={ax: 20 for ax in axes},
            algorithm="powell",
            powell_max_iterations=50,
            scan_dir=tmp_path,
        )

        assert powell_calls["n"] == 2, (
            "應該呼叫兩次主演算法：第一次無訊號被吞掉補盲搜，"
            f"補盲搜後第二次仍無訊號要中止整輪；實際呼叫 {powell_calls['n']} 次"
        )
        assert blind_calls["n"] == 1, f"應該只補盲搜一次，實際 {blind_calls['n']} 次"
        assert stage3_calls["n"] == 0, (
            "🔴 與 coder 聲明比對：第二次仍無訊號時 run_stage3() 完全不該被呼叫"
            "——若這個斷言失敗，代表第二次的 NoSignalAbort 被靜默吞掉、"
            "整輪繼續跑去收尾了，與 docstring 的聲明不符"
        )
        assert scanner.last_abort_kind == "no_signal"
        assert ctrl.scanning_active is False


# =============================================================================
# 五、algorithm 白名單防禦
# =============================================================================
class TestAlgorithmWhitelist:
    def test_unknown_algorithm_falls_back_to_coordinate_descent(self, tmp_path, monkeypatch):
        axes = ("X", "Y")
        peak = {"X": 10.0, "Y": 10.0}
        k = {"X": 0.01, "Y": 0.01}
        ctrl = FakeCtrl(axes=axes)
        scanner, ctrl = make_scanner(
            make_paraboloid_query(ctrl, axes, peak, k), ctrl=ctrl, axes=axes,
            blind_mode="off",
        )

        powell_calls = {"n": 0}
        orig = fsa.run_stage_powell

        def spy(*a, **kw):
            powell_calls["n"] += 1
            return orig(*a, **kw)

        monkeypatch.setattr(fsa, "run_stage_powell", spy)

        scanner.run(
            initial_step={ax: 20 for ax in axes},
            algorithm="not_a_real_algorithm",
            scan_dir=tmp_path,
        )

        assert scanner.algorithm == "coordinate_descent", (
            f"不合法的 algorithm 字串應退回座標下降，實際變成 {scanner.algorithm!r}"
        )
        assert powell_calls["n"] == 0, "不應該呼叫到 run_stage_powell"
        assert scanner.last_abort_reason is None


# =============================================================================
# 六、main_ai.py「尋光」分頁：演算法 Combobox 切換時的元件啟用/停用
# =============================================================================
@pytest.fixture(scope="module")
def gui(tmp_path_factory):
    recording_dir = tmp_path_factory.mktemp("scan_powell_gui_rec")
    scan_dir = tmp_path_factory.mktemp("scan_powell_gui_scan")
    root, g, patchers = make_gui(
        recording_dir,
        extra_patches=[patch("core.fiber_scanner._default_scan_dir", return_value=scan_dir)],
    )
    yield root, g
    close_gui(root, g, patchers)


class TestAlgoComboTogglesEntryStates:
    """
    這一整組測試共用模組層級的 `gui` fixture（跟 verify_scan_tab.py 同一個
    理由：連續建構過多 tk.Tk() 會偶發 TclError），每個測試結束前都把
    Combobox 切回座標下降，避免污染下一個測試的初始狀態。
    """

    def test_switch_to_powell_disables_step_entry(self, gui):
        root, g = gui
        g._scan_axis_selected_vars["X"].set(True)
        try:
            g._scan_algo_var.set(main_ai.ALGO_LABELS["powell"])
            g._on_scan_algo_changed()
            entries = g._scan_axis_step_entries.get("X", [])
            assert entries, "X 軸應該至少有一個起始步長 Entry"
            for w in entries:
                assert str(w.cget("state")) == "disabled"
        finally:
            g._scan_algo_var.set(main_ai.ALGO_LABELS["coordinate_descent"])
            g._on_scan_algo_changed()

    def test_switch_to_powell_disables_stage2_checkbox(self, gui):
        root, g = gui
        try:
            g._scan_algo_var.set(main_ai.ALGO_LABELS["powell"])
            g._on_scan_algo_changed()
            assert str(g._scan_stage2_cb.cget("state")) == "disabled"
        finally:
            g._scan_algo_var.set(main_ai.ALGO_LABELS["coordinate_descent"])
            g._on_scan_algo_changed()

    def test_switch_to_powell_shows_warning_label(self, gui):
        root, g = gui
        try:
            g._scan_algo_var.set(main_ai.ALGO_LABELS["powell"])
            g._on_scan_algo_changed()
            assert g._scan_algo_warn_lbl.winfo_manager() != ""
        finally:
            g._scan_algo_var.set(main_ai.ALGO_LABELS["coordinate_descent"])
            g._on_scan_algo_changed()

    def test_switch_back_restores_step_entry(self, gui):
        root, g = gui
        g._scan_axis_selected_vars["X"].set(True)
        g._scan_algo_var.set(main_ai.ALGO_LABELS["powell"])
        g._on_scan_algo_changed()
        g._scan_algo_var.set(main_ai.ALGO_LABELS["coordinate_descent"])
        g._on_scan_algo_changed()
        for w in g._scan_axis_step_entries.get("X", []):
            assert str(w.cget("state")) == "normal"

    def test_switch_back_restores_stage2_checkbox(self, gui):
        root, g = gui
        g._scan_algo_var.set(main_ai.ALGO_LABELS["powell"])
        g._on_scan_algo_changed()
        g._scan_algo_var.set(main_ai.ALGO_LABELS["coordinate_descent"])
        g._on_scan_algo_changed()
        assert str(g._scan_stage2_cb.cget("state")) == "normal"

    def test_switch_back_hides_warning_label(self, gui):
        root, g = gui
        g._scan_algo_var.set(main_ai.ALGO_LABELS["powell"])
        g._on_scan_algo_changed()
        g._scan_algo_var.set(main_ai.ALGO_LABELS["coordinate_descent"])
        g._on_scan_algo_changed()
        assert g._scan_algo_warn_lbl.winfo_manager() == ""


# =============================================================================
# 七、scipy 未安裝時的降級（GUI 初始化 + _do_start_scan()）
# =============================================================================
class TestScipyUnavailableDegradation:
    """
    每個 fixture 都各自建構 GUI（不沿用共用的 `gui`）：這裡驗證的正是
    「用某種特定的 scanner_config.json ＋ 特定的 scipy 可用性開機建構
    DS102GUI()」這個行為本身，天生就需要獨立、全新的 GUI 執行個體，
    跟 verify_scan_tab.py 的 TestScannerConfigPersistence 同一個理由。
    """

    @pytest.fixture(scope="class")
    @classmethod
    def gui_no_scipy(cls, tmp_path_factory):
        recording_dir = tmp_path_factory.mktemp("scan_noscipy_rec")
        scan_dir = tmp_path_factory.mktemp("scan_noscipy_scan")
        (Path(recording_dir) / "scanner_config.json").write_text(
            json.dumps({"algorithm": "powell"}, ensure_ascii=False), encoding="utf-8"
        )
        root, g, patchers = make_gui(
            recording_dir,
            extra_patches=[
                patch("core.fiber_scanner._default_scan_dir", return_value=scan_dir),
                patch.object(fsa, "_SCIPY_AVAILABLE", False),
            ],
        )
        yield root, g
        close_gui(root, g, patchers)

    def test_construction_falls_back_to_coordinate_descent(self, gui_no_scipy):
        """存檔是 powell、但這個環境沒裝 scipy → 開機後 Combobox 應顯示座標下降。"""
        _, g = gui_no_scipy
        assert g._scan_algo_var.get() == main_ai.ALGO_LABELS["coordinate_descent"]
        assert g._scan_algo_label_algos.get(g._scan_algo_var.get()) == "coordinate_descent"

    def test_combo_values_exclude_powell(self, gui_no_scipy):
        """Combobox 的可選清單不該包含 Powell 選項（不讓使用者選到裝不了的東西）。"""
        _, g = gui_no_scipy
        values = list(g._scan_algo_combo["values"])
        assert main_ai.ALGO_LABELS["powell"] not in values

    def test_do_start_scan_defends_against_stale_powell_selection(self, gui_no_scipy):
        """
        即使有人（舊設定殘留、或未來 UI 邏輯漏洞）硬把 Combobox 塞回 Powell
        標籤，_do_start_scan() 的最後一道防線也要擋下並提示缺 scipy，
        不能悄悄改用座標下降開始、也不能讓例外一路炸進 _run() 背景執行緒。
        """
        root, g = gui_no_scipy
        g.ctrl.connected = True
        g.ctrl.scanning_active = False

        class _FakeMeter:
            def get_power(self):
                return True, -10.0

        g.meter = _FakeMeter()
        # 強行塞入不在 combobox values 裡的字串，模擬「設定檔存過 powell、
        # UI 邏輯理論上不該讓使用者走到這裡，但防線仍要生效」的情境。
        g._scan_algo_var.set(main_ai.ALGO_LABELS["powell"])
        try:
            with patch("main_ai.messagebox.askyesno", return_value=True), \
                 patch("main_ai.messagebox.showerror") as mock_err:
                g._do_start_scan()
            assert mock_err.called, "應該跳出「缺少 scipy」的錯誤訊息"
            assert not g._scanning.is_set(), "不該真的啟動背景搜尋執行緒"
        finally:
            g._scan_algo_var.set(main_ai.ALGO_LABELS["coordinate_descent"])
            if g._scanning.is_set():
                g._do_stop_scan()
                pump_until(root, lambda: not g._scanning.is_set(), timeout=15.0)
