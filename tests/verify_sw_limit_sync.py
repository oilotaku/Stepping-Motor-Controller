# -*- coding: utf-8 -*-
"""
`DS102Controller.sync_sw_limits_from_controller()` 的假物件回歸測試。

背景：長按點動的 Python 端限位保護（`self.sw_limits`，`_check_sw_limit()`
用它比對移動目標）連線時預設不會從控制器同步，只有使用者手動在〈移動
控制〉分頁的〈軟體行程限制〉卡片輸入並套用才會有值。這支方法把韌體軟體
限位（CWSLP?/CCWSLP?）同步進 `self.sw_limits`，但**治不好**「預設沒保護」
的病根——韌體軟體限位出廠／斷電後本來就是停用的，此時查回來的
`CWSLP?`/`CCWSLP?` 是哨兵值 `SW_LIMIT_SENTINEL`（±99999999），不是真邊界。
這裡做的是「兩層限位彼此同步」＋「兩層都沒保護時主動告知」，把不可見的
無保護狀態變成可見的。

**最重要的不變量（回歸鎖）**：合併規則只收緊、永不放寬、永不清空。
使用者手動設定的值代表明確意圖，不能被「韌體這次查不到／查到停用」這種
訊號覆寫成 None——見 TestRegressionLock。

安全規則（同 conftest.py／verify_wait_axis_stop.py 的既有規範）：
  - 不連真實硬體：用真的 `DS102Controller()` 實例但 `ser` 全程維持 None，
    monkeypatch `_serial_write_read` 這一個底層方法即可——`query_status()`
    內部也只透過它問 SB3?/SB1?/SB2?，同一個 monkeypatch 就夠（見〈十〉
    未接滑台軸排除測試，這是 architect 收尾審查後才加的分支，方法本體
    不再是「完全不碰 query_status」，只在會被列進 unprotected 前才查）。
  - 不寫入真實 `recordings/`／`data/`：透過 `patch.object` 把
    `ds102_ctrl.RECORDING_DIR`／`DATA_DIR` 導向 tmp_path（本檔沒有呼叫
    任何持久化方法，但這層防護照補，比照既有測試檔慣例）。

關於測試案例 8 的數字：原始規格描述「使用者已設 CCW=100.0，韌體查到
CCW=50.0（更嚴格）→ 應採用 50.0」，但這與同一份規格明載的合併演算法
（「CCW 側取較大值」，因為 `_check_sw_limit()` 把 CCW 值當下界比對
`target_pulse < ccw_lim`，數值越大代表下界卡得越高、允許範圍越窄，才是
真正的「更嚴格」）互相矛盾——50 < 100，照演算法反而是「較不嚴格」，
合併後應維持 100 不變。本檔依規格明載的演算法與 `_check_sw_limit()` 的
既有語意（未改動、也不可改動）實作，測試案例 8 改用 CCW=150.0（數值
大於既有的 100.0，才是名副其實「更嚴格」的韌體值）驗證「韌體更嚴格時
會被採用」，同時案例 8b 額外驗證「韌體較寬鬆時維持既有值不變」，兩案
合起來完整覆蓋合併演算法的兩個分支。
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import core.ds102_ctrl as ds102_ctrl  # noqa: E402


# ---------------------------------------------------------------------------
# 共用假物件
# ---------------------------------------------------------------------------
def make_responder(mapping: dict):
    """依 cmd 精確比對回傳預先排好的回應；查不到的 cmd 回傳空字串。"""

    def _resp(cmd: str, timeout: float = 2.0) -> str:
        return mapping.get(cmd, "")

    return _resp


def collect_logs(ctrl):
    """攔下 _log()，回傳 (level, msg) 清單（同 verify_wait_axis_stop.py 寫法）。"""
    got = []
    ctrl._log = lambda level, msg, **kw: got.append((level, msg))
    return got


@pytest.fixture
def ctrl(tmp_path):
    """
    乾淨的 DS102Controller，`connected=True`／`axis_count=1` 但 `ser` 仍是
    None——只測 X 軸（AXIS_NO["X"]=="1"），避免其餘五軸的預設查詢
    （回空字串）汙染 log 斷言。
    """
    patchers = [
        patch.object(ds102_ctrl, "RECORDING_DIR", new=Path(tmp_path)),
        patch.object(ds102_ctrl, "DATA_DIR", new=Path(tmp_path) / "data"),
    ]
    for p in patchers:
        p.start()
    try:
        c = ds102_ctrl.DS102Controller()
        assert c.ser is None, "測試絕不可連上真實序列埠"
        c.connected = True
        c.axis_count = 1  # 只有軸 "1"（=X）會被巡查
        yield c
    finally:
        for p in patchers:
            p.stop()


# ===========================================================================
# 一、未連線直接短路
# ===========================================================================
def test_not_connected_returns_empty(ctrl):
    ctrl.connected = False
    ctrl._serial_write_read = make_responder({})
    assert ctrl.sync_sw_limits_from_controller() == []
    assert ctrl.sw_limits["X"] == (None, None)


# ===========================================================================
# 二、兩側皆停用 → 維持無保護，記入 unprotected
# ===========================================================================
def test_both_sides_disabled_stays_unprotected(ctrl):
    ctrl._serial_write_read = make_responder(
        {"AXI1:CCWSLE?": "0", "AXI1:CWSLE?": "0"}
    )
    result = ctrl.sync_sw_limits_from_controller()
    assert ctrl.sw_limits["X"] == (None, None)
    assert "X" in ctrl.sw_limits_unprotected
    assert result == []  # 沒有任何實際同步到的項目


# ===========================================================================
# 三、哨兵值回歸鎖：SLE=1 但 SLP 是哨兵，不可當真邊界
# ===========================================================================
def test_sentinel_value_is_not_treated_as_real_boundary(ctrl):
    ctrl._serial_write_read = make_responder(
        {
            "AXI1:CCWSLE?": "1",
            "AXI1:CCWSLP?": "99999999",
            "AXI1:CWSLE?": "0",
        }
    )
    ctrl.sync_sw_limits_from_controller()
    assert ctrl.sw_limits["X"] == (None, None)
    assert "X" in ctrl.sw_limits_unprotected


def test_sentinel_value_negative_sign_also_rejected(ctrl):
    """哨兵值判斷用 abs()，負向的 -99999999 一樣要被當成哨兵。"""
    ctrl._serial_write_read = make_responder(
        {
            "AXI1:CCWSLE?": "1",
            "AXI1:CCWSLP?": "-99999999",
            "AXI1:CWSLE?": "0",
        }
    )
    ctrl.sync_sw_limits_from_controller()
    assert ctrl.sw_limits["X"] == (None, None)


# ===========================================================================
# 四、合理數值 → 採用
# ===========================================================================
def test_reasonable_value_is_adopted(ctrl):
    ctrl._serial_write_read = make_responder(
        {
            "AXI1:CCWSLE?": "1",
            "AXI1:CCWSLP?": "5000",
            "AXI1:CWSLE?": "0",
        }
    )
    result = ctrl.sync_sw_limits_from_controller()
    assert ctrl.sw_limits["X"] == (5000.0, None)
    assert "X" not in ctrl.sw_limits_unprotected
    assert any("X" in s and "5000" in s for s in result)


# ===========================================================================
# 五、只有一側啟用 → 正確的 (值, None) / (None, 值) 組合
# ===========================================================================
def test_only_ccw_side_enabled(ctrl):
    ctrl._serial_write_read = make_responder(
        {
            "AXI1:CCWSLE?": "1",
            "AXI1:CCWSLP?": "-2000",
            "AXI1:CWSLE?": "0",
        }
    )
    ctrl.sync_sw_limits_from_controller()
    assert ctrl.sw_limits["X"] == (-2000.0, None)


def test_only_cw_side_enabled(ctrl):
    ctrl._serial_write_read = make_responder(
        {
            "AXI1:CCWSLE?": "0",
            "AXI1:CWSLE?": "1",
            "AXI1:CWSLP?": "8000",
        }
    )
    ctrl.sync_sw_limits_from_controller()
    assert ctrl.sw_limits["X"] == (None, 8000.0)


# ===========================================================================
# 六、SLE? 回應非預期 → 無有效邊界 + 記 ERROR
# ===========================================================================
def test_sle_unexpected_response_is_error_and_no_boundary(ctrl):
    logs = collect_logs(ctrl)
    ctrl._serial_write_read = make_responder(
        {
            "AXI1:CCWSLE?": "2",   # 不是 "0" 也不是 "1"
            "AXI1:CWSLE?": "",    # 空字串
        }
    )
    ctrl.sync_sw_limits_from_controller()
    assert ctrl.sw_limits["X"] == (None, None)
    errors = [msg for lv, msg in logs if lv == "ERROR"]
    assert len(errors) == 2, f"CCW／CW 兩側都該各記一筆 ERROR，實際: {errors}"
    assert any("CCWSLE?" in m for m in errors)
    assert any("CWSLE?" in m for m in errors)


# ===========================================================================
# 七、SLP? 回應非數字 → fail-safe 成 None + 記 ERROR
# ===========================================================================
def test_slp_non_numeric_response_is_error_and_fail_safe(ctrl):
    logs = collect_logs(ctrl)
    ctrl._serial_write_read = make_responder(
        {
            "AXI1:CCWSLE?": "1",
            "AXI1:CCWSLP?": "not_a_number",
            "AXI1:CWSLE?": "0",
        }
    )
    ctrl.sync_sw_limits_from_controller()
    assert ctrl.sw_limits["X"] == (None, None)
    errors = [msg for lv, msg in logs if lv == "ERROR"]
    assert any("CCWSLP?" in m for m in errors)


# ===========================================================================
# 八、回歸鎖（最重要）：使用者已手動設定，韌體查不到有效邊界時絕不可清空
# ===========================================================================
class TestRegressionLock:
    def test_user_value_survives_firmware_disabled(self, ctrl):
        """
        連線前 CCW 已被使用者手動設成 100.0；這次連線韌體回報 CCW 停用
        （SLE?=0）。合併後 CCW 必須仍是 100.0，不可以被清成 None——這是
        本次修法最重要的安全不變量：「只收緊，永不放寬、永不清空」。
        """
        ctrl.sw_limits["X"] = (100.0, None)
        ctrl._serial_write_read = make_responder(
            {"AXI1:CCWSLE?": "0", "AXI1:CWSLE?": "0"}
        )
        ctrl.sync_sw_limits_from_controller()
        assert ctrl.sw_limits["X"] == (100.0, None), "使用者設定的值被意外清空"
        # CCW 側仍有保護，不該被算進「完全無保護」
        assert "X" not in ctrl.sw_limits_unprotected

    def test_user_value_survives_firmware_query_error(self, ctrl):
        """同上，但這次是韌體回應解析失敗（而非單純停用），一樣不可清空。"""
        ctrl.sw_limits["X"] = (100.0, None)
        ctrl._serial_write_read = make_responder(
            {
                "AXI1:CCWSLE?": "1",
                "AXI1:CCWSLP?": "garbage",
                "AXI1:CWSLE?": "0",
            }
        )
        ctrl.sync_sw_limits_from_controller()
        assert ctrl.sw_limits["X"] == (100.0, None)


# ===========================================================================
# 九、合併：兩側都有值時取較嚴格者
# ===========================================================================
class TestMergeStricter:
    def test_firmware_stricter_ccw_is_adopted(self, ctrl):
        """
        使用者已設 CCW=100.0，韌體查到 CCW=150.0。CCW 是下界比對
        （`target_pulse < ccw_lim` 才擋），數值越大下界卡得越高、允許
        範圍越窄，150.0 才是真正「更嚴格」的一側，合併後應採用 150.0。
        """
        ctrl.sw_limits["X"] = (100.0, None)
        ctrl._serial_write_read = make_responder(
            {
                "AXI1:CCWSLE?": "1",
                "AXI1:CCWSLP?": "150",
                "AXI1:CWSLE?": "0",
            }
        )
        result = ctrl.sync_sw_limits_from_controller()
        assert ctrl.sw_limits["X"] == (150.0, None)
        assert any("X" in s and "150" in s for s in result)

    def test_firmware_looser_ccw_keeps_user_value(self, ctrl):
        """
        反過來：韌體值比使用者已設的值寬鬆（100 → 50），必須維持 100 不變。

        architect 收尾審查抓到：舊版實作在這個分支仍會把 "X CCW=..." 塞進
        回傳的 result 清單，讓 main_ai.py 的橫幅謊報「已同步」——實際上
        韌體值被拒絕採用，什麼都沒同步。這裡補上 `result == []` 的斷言
        鎖住修正（判斷條件從 `fw_val != current` 改成 `final != current`）。
        """
        ctrl.sw_limits["X"] = (100.0, None)
        ctrl._serial_write_read = make_responder(
            {
                "AXI1:CCWSLE?": "1",
                "AXI1:CCWSLP?": "50",
                "AXI1:CWSLE?": "0",
            }
        )
        result = ctrl.sync_sw_limits_from_controller()
        assert ctrl.sw_limits["X"] == (100.0, None), "不可被較寬鬆的韌體值放寬"
        assert result == [], "韌體值未被實際採用，不該回報成「已同步」"

    def test_firmware_stricter_cw_is_adopted(self, ctrl):
        """
        CW 是上界比對（`target_pulse > cw_lim` 才擋），數值越小上界卡得
        越低、允許範圍越窄。使用者已設 CW=8000，韌體查到 5000（更嚴格）
        → 應採用 5000。
        """
        ctrl.sw_limits["X"] = (None, 8000.0)
        ctrl._serial_write_read = make_responder(
            {
                "AXI1:CCWSLE?": "0",
                "AXI1:CWSLE?": "1",
                "AXI1:CWSLP?": "5000",
            }
        )
        ctrl.sync_sw_limits_from_controller()
        assert ctrl.sw_limits["X"] == (None, 5000.0)


# ===========================================================================
# 十、未接滑台的軸不應被算進「沒有任何行程保護」
# ===========================================================================
def test_stage_not_connected_axis_excluded_from_unprotected(ctrl):
    """
    architect 收尾審查抓到：未接滑台的軸（如實機的 U 軸）本來就不會被
    驅動，不需要行程保護；`restore_controller_config()`／
    `check_homing_config()` 都有「未接滑台一律跳過」的既有慣例，這裡若
    沒有比照，會讓警示橫幅列出一個永遠不會動的軸，稀釋真正警示的可信度。

    SB3?=1（可選取）、SB1?=6（bit1+bit2，不含 Driving/Detect origin）、
    SB2?=3（bit0+bit1 皆設）→ query_status() 判定為 "Stage not connected"
    （對照 ds102_ctrl.py query_status() 的判斷邏輯）。
    """
    ctrl._serial_write_read = make_responder(
        {
            "AXI1:CCWSLE?": "0",
            "AXI1:CWSLE?": "0",
            "AXI1:SB3?": "1",
            "AXI1:SB1?": "6",
            "AXI1:SB2?": "3",
        }
    )
    ctrl.sync_sw_limits_from_controller()
    assert ctrl.sw_limits["X"] == (None, None)
    assert "X" not in ctrl.sw_limits_unprotected, "未接滑台的軸不該觸發無保護警示"
