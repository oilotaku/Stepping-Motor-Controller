# -*- coding: utf-8 -*-
"""
軸機械校正參數功能（axis_calibration.json，2026-08-18～19）回歸測試
（pytest，假物件/暫存目錄，不碰真實硬體、不碰真實 recordings/）。

涵蓋三層，對應 CLAUDE.md〈軸機械校正參數〉整節描述的行為：
  1. ds102_ctrl.DS102Controller：estimate_um() 的雙層驗證（set_axis_calib
     擋在輸入端、estimate_um 自己再擋一次防手動改 json）、set_axis_calib()
     的合併語意與「全部成功或全部不動」、clear_axis_calib()、
     save_point() 的 positions_um/axis_calib_snapshot 快照。
  2. fiber_scanner.FiberAlignmentScanner._measure_here()：Sample 的
     coords_um/calib_snapshot 快照與序列化。
  3. main_ai.DS102GUI：_apply_axis_calib()（GUI 端本地驗證）、
     _do_clear_axis_calib()（確認視窗）、_refresh_calib_display()。

── 安全規則（見 conftest.py 開頭，兩支既有測試檔案共同適用，這裡沿用）──
  - 不連真實硬體：一律用真的 DS102Controller() 實例（.ser 全程 None，
    從未真正 connect() 過），或直接對 FiberAlignmentScanner 注入合成
    power_query，不需要 GPIB 卡。
  - 不寫入真實 recordings/：
      🔴 這裡有一個容易踩到的陷阱，記錄下來供之後維護者參考——
      conftest.make_gui() 只 patch `main_ai.RECORDING_DIR`，但
      DS102Controller 的持久化方法（load_points / _persist_points /
      load_axis_calib / _persist_axis_calib / save_recording /
      capture_controller_config 等）全部定義在 ds102_ctrl.py，裡面
      直接引用的是 ds102_ctrl.py 自己模組層級的 `RECORDING_DIR` 全域
      名稱，跟 main_ai.py 用 `from ds102_ctrl import RECORDING_DIR`
      重新引入的那個是兩個獨立的名字綁定同一個初始物件——patch 其中
      一個不會影響另一個（已用一支獨立腳本實測驗證）。也就是說單靠
      `make_gui()` 現有的 patch，任何會呼叫到 ctrl 的存檔方法的測試，
      實際上會寫進專案真正的 recordings/ 目錄。
      本檔案因此對所有會觸發 ds102_ctrl.py 持久化路徑的測試，一律
      額外 `patch.object(ds102_ctrl, "RECORDING_DIR", new=<暫存目錄>)`
      ——控制器層級的測試直接用 pytest 內建的 tmp_path；GUI 層級的測試
      在 module-scope 的 gui fixture 建構時，把這個 patch 一併放進
      make_gui() 的 extra_patches。verify_scan_tab.py 與
      verify_meter_panel.py 目前沒有踩到這個坑，是因為它們沒有呼叫到
      teaching points / axis calib / profiles / controller config /
      recording 這些會走 ds102_ctrl.py 存檔路徑的方法，純屬僥倖，不是
      這個防護真的有效——這個落差已回報給使用者，是否要回頭補強
      conftest.py 本身由使用者決定，這裡不動 conftest.py。
  - 全程不使用 winfo_ismapped()（root.withdraw() 之後恆為 False）。
  - 軸機械校正參數的 GUI handler（_apply_axis_calib / _do_clear_axis_calib /
    _refresh_calib_display）全部同步執行、不開背景執行緒，因此這裡的
    GUI 測試不需要 pump_until()，直接呼叫並立即斷言即可。

執行方式（VS Code Test Explorer 或指令列皆可）：
    venv/Scripts/python.exe -m pytest verify_axis_calib.py -v
    venv/Scripts/python.exe -m pytest verify_axis_calib.py::TestEstimateUm -v
"""

import json
import math
from unittest.mock import patch

import pytest

import ds102_ctrl
import main_ai
from fiber_scanner import FiberAlignmentScanner

from conftest import close_gui, make_gui


# =============================================================================
# 一、ds102_ctrl.DS102Controller 層級（不需要 GUI）
# =============================================================================
class TestEstimateUm:
    """
    estimate_um()：純計算，無 I/O。這裡直接寫 ctrl.axis_calib 字典
    （繞過 set_axis_calib 的輸入驗證），驗證 estimate_um 自己的「第二道
    防線」——CLAUDE.md 明講這道防線防的是「json 檔被手動編輯繞過 GUI
    輸入驗證」，不是防使用者手滑，所以測試手法就是直接改字典。
    """

    def test_khe06008c_full_step_matches_official_spec(self):
        """驗證過的手算範例：導程1mm/步進角0.72/division=1，500 pulse = 1000.0 μm。"""
        ctrl = ds102_ctrl.DS102Controller()
        ctrl.axis_calib["X"] = {
            "lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1
        }
        assert ctrl.estimate_um("X", 500) == pytest.approx(1000.0)

    def test_axis_without_calib_returns_none(self):
        ctrl = ds102_ctrl.DS102Controller()
        assert ctrl.estimate_um("X", 500) is None

    def test_zero_pulse_returns_zero_not_none(self):
        """邊界：pulse=0 是合法輸入，應回傳 0.0（有校正參數時），不是 None。"""
        ctrl = ds102_ctrl.DS102Controller()
        ctrl.axis_calib["X"] = {
            "lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1
        }
        v = ctrl.estimate_um("X", 0)
        assert v == pytest.approx(0.0)
        assert v is not None

    def test_negative_pulse_scales_symmetrically(self):
        """負 pulse（機械座標常見，例如 Y 軸行程多在負值側）應等比例算出負 μm。"""
        ctrl = ds102_ctrl.DS102Controller()
        ctrl.axis_calib["X"] = {
            "lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1
        }
        assert ctrl.estimate_um("X", -500) == pytest.approx(-1000.0)

    def test_zero_division_returns_none(self):
        ctrl = ds102_ctrl.DS102Controller()
        ctrl.axis_calib["X"] = {
            "lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 0
        }
        assert ctrl.estimate_um("X", 500) is None

    def test_negative_division_returns_none(self):
        ctrl = ds102_ctrl.DS102Controller()
        ctrl.axis_calib["X"] = {
            "lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": -1
        }
        assert ctrl.estimate_um("X", 500) is None

    def test_zero_lead_pitch_returns_none(self):
        ctrl = ds102_ctrl.DS102Controller()
        ctrl.axis_calib["X"] = {
            "lead_pitch_mm": 0.0, "step_angle_deg": 0.72, "division": 1
        }
        assert ctrl.estimate_um("X", 500) is None

    def test_negative_lead_pitch_returns_none(self):
        ctrl = ds102_ctrl.DS102Controller()
        ctrl.axis_calib["X"] = {
            "lead_pitch_mm": -1.0, "step_angle_deg": 0.72, "division": 1
        }
        assert ctrl.estimate_um("X", 500) is None

    def test_zero_step_angle_returns_none(self):
        ctrl = ds102_ctrl.DS102Controller()
        ctrl.axis_calib["X"] = {
            "lead_pitch_mm": 1.0, "step_angle_deg": 0.0, "division": 1
        }
        assert ctrl.estimate_um("X", 500) is None

    def test_negative_step_angle_returns_none(self):
        ctrl = ds102_ctrl.DS102Controller()
        ctrl.axis_calib["X"] = {
            "lead_pitch_mm": 1.0, "step_angle_deg": -0.72, "division": 1
        }
        assert ctrl.estimate_um("X", 500) is None

    def test_inf_lead_pitch_returns_none(self):
        """inf 滿足 x>0，若沒有 math.isfinite() 檢查會被誤放行，算出 inf μm。"""
        ctrl = ds102_ctrl.DS102Controller()
        ctrl.axis_calib["X"] = {
            "lead_pitch_mm": float("inf"), "step_angle_deg": 0.72, "division": 1
        }
        assert ctrl.estimate_um("X", 500) is None

    def test_nan_step_angle_returns_none(self):
        """nan 滿足 x<=0 為 False，同樣需要 isfinite() 才能擋下。"""
        ctrl = ds102_ctrl.DS102Controller()
        ctrl.axis_calib["X"] = {
            "lead_pitch_mm": 1.0, "step_angle_deg": float("nan"), "division": 1
        }
        assert ctrl.estimate_um("X", 500) is None

    def test_division_as_float_returns_none(self):
        """型別陷阱：division 存成 2.0（浮點）而非 2（整數），isinstance(x, int) 為 False。"""
        ctrl = ds102_ctrl.DS102Controller()
        ctrl.axis_calib["X"] = {
            "lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 2.0
        }
        assert ctrl.estimate_um("X", 500) is None

    def test_division_as_bool_returns_none(self):
        """型別陷阱：bool 是 int 的子類別，division=True 會被 isinstance(x, int) 誤判為合法整數 1。"""
        ctrl = ds102_ctrl.DS102Controller()
        ctrl.axis_calib["X"] = {
            "lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": True
        }
        assert ctrl.estimate_um("X", 500) is None

    def test_missing_division_key_returns_none(self):
        """格式不完整：字典缺 division 這個 key（不是型別錯，是根本沒有），.get() 回 None。"""
        ctrl = ds102_ctrl.DS102Controller()
        ctrl.axis_calib["X"] = {"lead_pitch_mm": 1.0, "step_angle_deg": 0.72}
        assert ctrl.estimate_um("X", 500) is None


class TestSetAxisCalib:
    """
    set_axis_calib()：驗證 + 合併更新 + 持久化。每個測試各自用
    pytest 內建的 tmp_path（函式層級、自動隔離），patch
    ds102_ctrl.RECORDING_DIR 到暫存目錄，絕不落在真實 recordings/。
    """

    def test_valid_multi_axis_all_written(self, tmp_path):
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_axis_calib()
            errors = ctrl.set_axis_calib({
                "X": {"lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1},
                "Z": {"lead_pitch_mm": 2.0, "step_angle_deg": 1.8, "division": 4},
            })
            assert errors == []
            assert set(ctrl.axis_calib) == {"X", "Z"}
            on_disk = json.loads(
                (tmp_path / "axis_calibration.json").read_text(encoding="utf-8")
            )
            assert set(on_disk) == {"X", "Z"}

    def test_merge_only_updates_passed_axes(self, tmp_path):
        """合併更新：後呼叫更新 Y 不應動到先前已寫入的 X。"""
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_axis_calib()
            ctrl.set_axis_calib({
                "X": {"lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1}
            })
            ctrl.set_axis_calib({
                "Y": {"lead_pitch_mm": 2.0, "step_angle_deg": 1.8, "division": 2}
            })
            assert set(ctrl.axis_calib) == {"X", "Y"}
            assert ctrl.axis_calib["X"]["lead_pitch_mm"] == 1.0

    def test_one_invalid_axis_blocks_entire_batch(self, tmp_path):
        """驗證是「全部成功或全部不動」：Y 不合法時，同批次裡合法的 X 也不會被寫入。"""
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_axis_calib()
            errors = ctrl.set_axis_calib({
                "X": {"lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1},
                "Y": {"lead_pitch_mm": -1.0, "step_angle_deg": 0.72, "division": 1},
            })
            assert errors  # 非空
            assert ctrl.axis_calib == {}  # X 也沒有被寫入
            assert not (tmp_path / "axis_calibration.json").exists()

    def test_inf_lead_rejected(self, tmp_path):
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_axis_calib()
            errors = ctrl.set_axis_calib({
                "X": {"lead_pitch_mm": float("inf"), "step_angle_deg": 0.72, "division": 1}
            })
            assert errors
            assert "X" not in ctrl.axis_calib

    def test_nan_angle_rejected(self, tmp_path):
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_axis_calib()
            errors = ctrl.set_axis_calib({
                "X": {"lead_pitch_mm": 1.0, "step_angle_deg": float("nan"), "division": 1}
            })
            assert errors
            assert "X" not in ctrl.axis_calib

    def test_division_as_string_rejected(self, tmp_path):
        """型別錯誤：division 傳字串（例如從沒做過型別轉換的呼叫端傳入）應被拒絕。"""
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_axis_calib()
            errors = ctrl.set_axis_calib({
                "X": {"lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": "1"}
            })
            assert errors
            assert any("分度值" in e for e in errors)
            assert "X" not in ctrl.axis_calib

    def test_division_as_float_rejected(self, tmp_path):
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_axis_calib()
            errors = ctrl.set_axis_calib({
                "X": {"lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1.0}
            })
            assert errors
            assert "X" not in ctrl.axis_calib

    def test_division_as_bool_rejected(self, tmp_path):
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_axis_calib()
            errors = ctrl.set_axis_calib({
                "X": {"lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": True}
            })
            assert errors
            assert "X" not in ctrl.axis_calib

    def test_zero_division_rejected(self, tmp_path):
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_axis_calib()
            errors = ctrl.set_axis_calib({
                "X": {"lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 0}
            })
            assert errors
            assert "X" not in ctrl.axis_calib

    def test_negative_lead_rejected(self, tmp_path):
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_axis_calib()
            errors = ctrl.set_axis_calib({
                "X": {"lead_pitch_mm": -1.0, "step_angle_deg": 0.72, "division": 1}
            })
            assert errors
            assert "X" not in ctrl.axis_calib

    def test_reject_write_without_load_when_disk_has_other_axes(self, tmp_path):
        """
        拒寫保護：比照 _persist_points 的既有邏輯。一個「已 load」的 controller
        先在磁碟留下 X 軸資料；另一個全新、沒呼叫過 load_axis_calib() 的
        controller 直接 set_axis_calib(Y)——_persist_axis_calib() 應該偵測到
        磁碟有它記憶體裡沒有的既有軸（X），拒絕寫入，磁碟內容維持只有 X。

        🔴 注意 set_axis_calib() 的回傳值 errors 在這個情境下仍是空 list
        （驗證通過＝數值本身合法），拒寫是在 _persist_axis_calib() 內部
        默默發生、只留一則 ERROR log，呼叫端從回傳值完全看不出來——這跟
        CLAUDE.md 描述的 _persist_points 拒寫保護是同一種「回傳值無法反映
        持久化是否真的成功」的既有行為，不是這次校正參數功能特有的缺陷。
        """
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl1 = ds102_ctrl.DS102Controller()
            ctrl1.load_axis_calib()
            errors1 = ctrl1.set_axis_calib({
                "X": {"lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1}
            })
            assert errors1 == []

            ctrl2 = ds102_ctrl.DS102Controller()
            assert ctrl2._axis_calib_loaded is False
            errors2 = ctrl2.set_axis_calib({
                "Y": {"lead_pitch_mm": 2.0, "step_angle_deg": 1.8, "division": 2}
            })
            assert errors2 == []  # 驗證本身沒問題，回傳值看不出拒寫
            # 記憶體裡 ctrl2 已經合併了 Y（拒寫發生在 update() 之後）——
            # 這是既有設計，記錄下來供之後維護者比對，不是這裡要驗證的重點。
            assert ctrl2.axis_calib == {
                "Y": {"lead_pitch_mm": 2.0, "step_angle_deg": 1.8, "division": 2,
                      "ts": ctrl2.axis_calib["Y"]["ts"]}
            }

            on_disk = json.loads(
                (tmp_path / "axis_calibration.json").read_text(encoding="utf-8")
            )
            assert "X" in on_disk
            assert "Y" not in on_disk  # 真正的重點：磁碟沒有被 ctrl2 寫壞


class TestClearAxisCalib:
    def test_clear_existing_axis_returns_true(self, tmp_path):
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_axis_calib()
            ctrl.set_axis_calib({
                "X": {"lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1}
            })
            assert ctrl.clear_axis_calib("X") is True
            assert "X" not in ctrl.axis_calib

    def test_clear_nonexistent_axis_returns_false(self, tmp_path):
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_axis_calib()
            assert ctrl.clear_axis_calib("Z") is False

    def test_clear_then_estimate_returns_none(self, tmp_path):
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_axis_calib()
            ctrl.set_axis_calib({
                "Y": {"lead_pitch_mm": 2.0, "step_angle_deg": 1.8, "division": 2}
            })
            assert ctrl.estimate_um("Y", 100) is not None
            ctrl.clear_axis_calib("Y")
            assert ctrl.estimate_um("Y", 100) is None

    def test_clear_removes_from_disk_not_just_memory(self, tmp_path):
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_axis_calib()
            ctrl.set_axis_calib({
                "X": {"lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1}
            })
            ctrl.clear_axis_calib("X")
            on_disk = json.loads(
                (tmp_path / "axis_calibration.json").read_text(encoding="utf-8")
            )
            assert "X" not in on_disk


class TestSavePointUmSnapshot:
    """save_point() 的 positions_um / axis_calib_snapshot 快照（只加不改既有欄位）。"""

    def test_mixed_axes_only_calibrated_in_positions_um(self, tmp_path):
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_points()
            ctrl.load_axis_calib()
            ctrl.set_axis_calib({
                "X": {"lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1}
            })
            ctrl.save_point("p1", {"X": 500, "Y": 300})
            entry = ctrl.saved_points["p1"]
            assert entry["positions_um"] == {"X": pytest.approx(1000.0)}
            assert "Y" not in entry["positions_um"]

    def test_no_calibration_omits_um_keys_entirely(self, tmp_path):
        """完全沒用這個功能時，存檔內容不應憑空多出空的 positions_um/axis_calib_snapshot。"""
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_points()
            ctrl.load_axis_calib()
            ctrl.save_point("p2", {"X": 100})
            entry = ctrl.saved_points["p2"]
            assert "positions_um" not in entry
            assert "axis_calib_snapshot" not in entry

    def test_snapshot_contains_full_calib_params(self, tmp_path):
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_points()
            ctrl.load_axis_calib()
            ctrl.set_axis_calib({
                "Z": {"lead_pitch_mm": 2.0, "step_angle_deg": 1.8, "division": 4}
            })
            ctrl.save_point("p3", {"Z": 200})
            snap = ctrl.saved_points["p3"]["axis_calib_snapshot"]["Z"]
            assert snap["lead_pitch_mm"] == 2.0
            assert snap["step_angle_deg"] == 1.8
            assert snap["division"] == 4

    def test_goto_point_ignores_um_snapshot_reads_only_positions_pulse(self, tmp_path):
        """
        goto_point() 只讀 positions_pulse——用假的 move_step/query_status
        （不碰序列埠）驗證：存了 positions_um 快照的教點，goto 依然只憑
        positions_pulse 的數字移動，行為不受新欄位存在與否影響。
        """
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_points()
            ctrl.load_axis_calib()
            ctrl.set_axis_calib({
                "X": {"lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1}
            })
            ctrl.axis_count = 1
            ctrl.save_point("pt", {"X": 500})
            assert "positions_um" in ctrl.saved_points["pt"]  # 前提：確實有快照

            calls = []

            def fake_move_step(axis_no, direction, amount, l_speed, f_speed, rate,
                                s_rate, wait_done=True):
                calls.append((axis_no, direction, amount))
                delta = float(amount) if direction == "CW" else -float(amount)
                ax = ds102_ctrl.NO_AXIS[axis_no]
                with ctrl._lock:
                    ctrl._positions_pulse[ax] += delta
                return True

            ctrl.move_step = fake_move_step
            ctrl.query_status = lambda axis_no: ("Stop", "")

            ok = ctrl.goto_point("pt", "5", "1000", "100", "5")
            assert ok is True
            assert calls == [("1", "CW", "500")]
            assert ctrl.positions_machine["X"] == pytest.approx(500.0)

    def test_legacy_file_without_new_fields_loads_fine(self, tmp_path):
        """舊格式檔案（沒有 positions_um/axis_calib_snapshot）能正常 load_points()。"""
        p = tmp_path / "teaching_points.json"
        p.write_text(
            json.dumps({
                "old_pt": {
                    "positions_pulse": {"X": 100.0},
                    "ts": "2020-01-01T00:00:00",
                }
            }),
            encoding="utf-8",
        )
        with patch.object(ds102_ctrl, "RECORDING_DIR", new=tmp_path):
            ctrl = ds102_ctrl.DS102Controller()
            ctrl.load_points()
            assert ctrl.saved_points["old_pt"]["positions_pulse"]["X"] == 100.0
            assert "positions_um" not in ctrl.saved_points["old_pt"]


# =============================================================================
# 二、fiber_scanner.FiberAlignmentScanner 層級（不需要 GUI）
# =============================================================================
class TestMeasureHereCalibSnapshot:
    """
    _measure_here() 建立 Sample 時的 coords_um/calib_snapshot 快照。
    用一個真的 DS102Controller() 實例當假 ctrl（.ser 全程 None，從未
    connect()），直接寫 _positions_pulse / axis_calib 兩個記憶體字典，
    完全不碰任何持久化路徑，不需要 patch RECORDING_DIR。
    """

    @staticmethod
    def _build_scanner(calib=None, power=(True, -10.0)):
        ctrl = ds102_ctrl.DS102Controller()
        ctrl.ems_active = False
        if calib:
            for ax, params in calib.items():
                ctrl.axis_calib[ax] = params
        scanner = FiberAlignmentScanner(
            ctrl=ctrl, power_query=lambda: power, settle_sec=0.0
        )
        return ctrl, scanner

    def test_only_calibrated_axes_in_coords_um(self):
        ctrl, scanner = self._build_scanner(calib={
            "X": {"lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1}
        })
        ctrl._positions_pulse["X"] = 500.0
        ctrl._positions_pulse["Y"] = 300.0
        sample = scanner._measure_here()
        assert sample.coords_um == {"X": pytest.approx(1000.0)}
        assert "Y" not in sample.coords_um

    def test_no_calibration_gives_none_not_empty_dict(self):
        """完全無校正時，coords_um/calib_snapshot 應為 None，不是空字典 {}。"""
        ctrl, scanner = self._build_scanner()
        sample = scanner._measure_here()
        assert sample.coords_um is None
        assert sample.calib_snapshot is None

    def test_calib_snapshot_matches_axis_calib_content_but_is_a_copy(self):
        params = {"lead_pitch_mm": 2.0, "step_angle_deg": 1.8, "division": 4, "ts": "2020"}
        ctrl, scanner = self._build_scanner(calib={"Z": params})
        ctrl._positions_pulse["Z"] = 200.0
        sample = scanner._measure_here()
        assert sample.calib_snapshot["Z"] == params
        # 快照必須是複本，不是同一個字典參照——之後 clear/修改 axis_calib
        # 不應該回頭動到已經存進樣本裡的歷史快照。
        assert sample.calib_snapshot["Z"] is not ctrl.axis_calib["Z"]

    def test_to_dict_serializes_none_as_none(self):
        ctrl, scanner = self._build_scanner()
        sample = scanner._measure_here()
        d = sample.to_dict()
        assert d["coords_um"] is None
        assert d["calib_snapshot"] is None
        assert json.loads(json.dumps(d))["coords_um"] is None  # None -> null 不會爆

    def test_to_dict_serializes_nested_calib_values(self):
        ctrl, scanner = self._build_scanner(calib={
            "X": {"lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1}
        })
        ctrl._positions_pulse["X"] = 100.0
        sample = scanner._measure_here()
        d = sample.to_dict()
        assert d["coords_um"]["X"] == pytest.approx(200.0)
        assert isinstance(d["calib_snapshot"]["X"], dict)
        assert d["calib_snapshot"]["X"]["division"] == 1


# =============================================================================
# 三、main_ai.DS102GUI 層級
# =============================================================================
def _reset_calib_state(g):
    """
    把共用 gui 的軸校正參數狀態重置為乾淨狀態，讓每個測試互不影響
    （比照 verify_scan_tab.py 部分案例「收尾，避免影響後續測試」的做法，
    這裡改成 autouse fixture 統一在每個測試前做，不必每個測試各自收尾）。
    """
    g.ctrl.axis_calib = {}
    for ax in main_ai.AXES:
        g._calib_vars[ax]["lead"].set("")
        g._calib_vars[ax]["angle"].set("")
        g._calib_vars[ax]["div"].set("")
    g._refresh_calib_display()


@pytest.fixture(scope="module")
def gui(tmp_path_factory):
    """
    整份檔案第三節（GUI 層級）共用的單一 (root, gui, recording_dir)。
    額外把 ds102_ctrl.RECORDING_DIR 也 patch 到同一個暫存目錄——見本檔
    開頭 docstring 的安全規則說明，單靠 make_gui() 既有的
    main_ai.RECORDING_DIR patch 保護不到 axis_calibration.json。
    """
    recording_dir = tmp_path_factory.mktemp("axis_calib_gui_rec")
    root, g, patchers = make_gui(
        recording_dir,
        extra_patches=[patch.object(ds102_ctrl, "RECORDING_DIR", new=recording_dir)],
    )
    yield root, g, recording_dir
    close_gui(root, g, patchers)


class TestApplyAxisCalib:
    """_apply_axis_calib()：GUI 端本地驗證 + 呼叫 controller。"""

    @pytest.fixture(autouse=True)
    def _arrange(self, gui):
        _root, g, _recording_dir = gui
        _reset_calib_state(g)

    def test_valid_input_applies_and_persists(self, gui):
        root, g, recording_dir = gui
        g._calib_vars["X"]["lead"].set("1.0")
        g._calib_vars["X"]["angle"].set("0.72")
        g._calib_vars["X"]["div"].set("1")
        g._apply_axis_calib()
        assert g.ctrl.axis_calib["X"]["lead_pitch_mm"] == 1.0
        on_disk = json.loads(
            (recording_dir / "axis_calibration.json").read_text(encoding="utf-8")
        )
        assert on_disk["X"]["division"] == 1

    def test_partial_fields_treated_as_local_format_error(self, gui):
        """只填兩欄（division 留空）：本地格式錯誤，不應該送到 controller。"""
        root, g, recording_dir = gui
        g._calib_vars["Y"]["lead"].set("1.0")
        g._calib_vars["Y"]["angle"].set("0.72")
        g._calib_vars["Y"]["div"].set("")
        with patch("main_ai.messagebox.showerror") as mock_err, \
             patch.object(g.ctrl, "set_axis_calib") as mock_set:
            g._apply_axis_calib()
        assert mock_err.called
        assert not mock_set.called

    def test_all_blank_is_skipped_existing_data_untouched(self, gui):
        """三欄全空：本次不動這軸，既有資料（若有）保持原樣。"""
        root, g, recording_dir = gui
        g.ctrl.set_axis_calib({
            "Z": {"lead_pitch_mm": 3.0, "step_angle_deg": 1.8, "division": 2}
        })
        # 其餘軸（含 Z）的輸入欄位維持 reset 後的空白狀態，直接套用。
        g._apply_axis_calib()
        assert g.ctrl.axis_calib["Z"]["lead_pitch_mm"] == 3.0

    def test_updating_one_axis_leaves_others_intact(self, gui):
        """只更新部分軸時，其餘軸既有資料不受影響（跟上面案例合起來驗證同一個機制的兩個角度）。"""
        root, g, recording_dir = gui
        g.ctrl.set_axis_calib({
            "Z": {"lead_pitch_mm": 3.0, "step_angle_deg": 1.8, "division": 2}
        })
        g._calib_vars["W"]["lead"].set("1.0")
        g._calib_vars["W"]["angle"].set("0.72")
        g._calib_vars["W"]["div"].set("1")
        g._apply_axis_calib()
        assert g.ctrl.axis_calib["W"]["lead_pitch_mm"] == 1.0
        assert g.ctrl.axis_calib["Z"]["lead_pitch_mm"] == 3.0

    def test_division_with_decimal_point_rejected_locally(self, gui):
        """
        型別轉換陷阱：使用者在分度值欄位打「2.0」（看起來像合法數字），
        但 GUI 端用 int(div_s) 解析，int("2.0") 會丟 ValueError，
        應該被當成本地格式錯誤擋下，不是靜默接受或算出錯誤結果。
        """
        root, g, recording_dir = gui
        g._calib_vars["U"]["lead"].set("1.0")
        g._calib_vars["U"]["angle"].set("0.72")
        g._calib_vars["U"]["div"].set("2.0")
        with patch("main_ai.messagebox.showerror") as mock_err:
            g._apply_axis_calib()
        assert mock_err.called
        assert "U" not in g.ctrl.axis_calib


class TestDoClearAxisCalib:
    """_do_clear_axis_calib()：確認視窗 + 清除。"""

    @pytest.fixture(autouse=True)
    def _arrange(self, gui):
        _root, g, _recording_dir = gui
        _reset_calib_state(g)

    def test_confirm_yes_clears_data_and_disk(self, gui):
        root, g, recording_dir = gui
        g.ctrl.set_axis_calib({
            "X": {"lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1}
        })
        with patch("main_ai.messagebox.askyesno", return_value=True) as mock_ask:
            g._do_clear_axis_calib("X")
        assert mock_ask.called
        assert "X" not in g.ctrl.axis_calib
        assert g._calib_vars["X"]["lead"].get() == ""
        on_disk = json.loads(
            (recording_dir / "axis_calibration.json").read_text(encoding="utf-8")
        )
        assert "X" not in on_disk

    def test_confirm_no_keeps_data(self, gui):
        root, g, recording_dir = gui
        g.ctrl.set_axis_calib({
            "Y": {"lead_pitch_mm": 2.0, "step_angle_deg": 1.8, "division": 2}
        })
        with patch("main_ai.messagebox.askyesno", return_value=False):
            g._do_clear_axis_calib("Y")
        assert "Y" in g.ctrl.axis_calib
        on_disk = json.loads(
            (recording_dir / "axis_calibration.json").read_text(encoding="utf-8")
        )
        assert "Y" in on_disk

    def test_unset_axis_shows_no_dialog(self, gui):
        """該軸本來就沒設定時完全不彈確認視窗。"""
        root, g, recording_dir = gui
        with patch("main_ai.messagebox.askyesno") as mock_ask:
            g._do_clear_axis_calib("Z")
        assert not mock_ask.called


class TestRefreshCalibDisplay:
    """_refresh_calib_display()：「目前生效」欄的文字格式。"""

    @pytest.fixture(autouse=True)
    def _arrange(self, gui):
        _root, g, _recording_dir = gui
        _reset_calib_state(g)

    def test_calibrated_axis_shows_um_per_pulse(self, gui):
        root, g, recording_dir = gui
        g.ctrl.set_axis_calib({
            "X": {"lead_pitch_mm": 1.0, "step_angle_deg": 0.72, "division": 1}
        })
        g._refresh_calib_display()
        # estimate_um("X", 1) = 1*(1000)/((360/0.72)*1) = 2.0
        assert g._calib_cur_vars["X"].get() == "≈ 2.00000 μm/pulse"

    def test_uncalibrated_axis_shows_placeholder(self, gui):
        root, g, recording_dir = gui
        g._refresh_calib_display()
        assert g._calib_cur_vars["V"].get() == "未設定"
