# =============================================================================
# 尋光樣本 Excel 匯出的回歸測試（pytest，不需硬體）
#
# 測的是 fiber_scanner.export_samples_xlsx() 與 persist_samples() 順帶寫出
# 的 .xlsx。**刻意真的把檔案寫出來再讀回來驗證**，而不是 mock 掉
# xlsxwriter——這個功能唯一的價值就是「產生的檔案 Excel 打得開、欄位對得
# 上」，把寫檔那一段換成假物件等於什麼都沒測到。
#
# 讀回驗證用 zipfile + 解 xlsx 內部的 XML：xlsx 就是一個 zip，不必為了讀
# 回自己的輸出再多裝一個 openpyxl（本專案 venv 也沒有）。
#
# 🔴 各 verify_*.py 的 FakeCtrl 彼此不通用（見 docs/testing.md）。本檔完全
# 不需要 ctrl——export_samples_xlsx() 是純函式，只吃 Sample list，這也是當初
# 把它拆成模組層級函式而不是 FiberAlignmentScanner 方法的原因。
# =============================================================================

import json
import re
import zipfile
from pathlib import Path

import pytest

import fiber_scanner
from fiber_scanner import Sample, export_samples_xlsx

pytestmark = pytest.mark.skipif(
    not fiber_scanner._XLSXWRITER_AVAILABLE,
    reason="未安裝 xlsxwriter，Excel 匯出功能本身就是優雅降級的",
)


# ---------------------------------------------------------------------------
# 讀回工具：把 xlsx 解成 {工作表名: [[儲存格字串, ...], ...]}
# ---------------------------------------------------------------------------
def _shared_strings(z: zipfile.ZipFile):
    try:
        xml = z.read("xl/sharedStrings.xml").decode("utf-8")
    except KeyError:
        return []
    # <si> 底下可能被拆成多個 <t>（rich text），全部串起來才是完整字串
    return [
        "".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S))
        for si in re.findall(r"<si>(.*?)</si>", xml, re.S)
    ]


def _sheet_cells(z: zipfile.ZipFile, sheet_path: str, strings):
    xml = z.read(sheet_path).decode("utf-8")
    rows = []
    for row_xml in re.findall(r"<row[^>]*>(.*?)</row>", xml, re.S):
        cells = []
        for c in re.findall(r"<c\s([^>]*?)(?:/>|>(.*?)</c>)", row_xml, re.S):
            attrs, body = c
            m = re.search(r'r="([A-Z]+)\d+"', attrs)
            col = m.group(1) if m else ""
            v = re.search(r"<v>(.*?)</v>", body or "", re.S)
            raw = v.group(1) if v else ""
            if 't="s"' in attrs and raw != "":
                raw = strings[int(raw)]
            cells.append((col, raw))
        rows.append(cells)
    return rows


def _read_xlsx(path: Path):
    """回傳 {工作表名: [[(欄字母, 值字串), ...], ...]}。"""
    with zipfile.ZipFile(path) as z:
        strings = _shared_strings(z)
        wb = z.read("xl/workbook.xml").decode("utf-8")
        names = re.findall(r'<sheet name="([^"]+)"', wb)
        out = {}
        for i, name in enumerate(names, start=1):
            out[name] = _sheet_cells(z, f"xl/worksheets/sheet{i}.xml", strings)
        return out


def _flat(rows):
    """把一張表壓成純值的二維 list，方便比對。"""
    return [[v for _, v in row] for row in rows]


# ---------------------------------------------------------------------------
# 假樣本
# ---------------------------------------------------------------------------
def _samples():
    return [
        Sample(
            coords={"X": 1000.0, "Y": -2000.0},
            ok=True,
            power=-35.5,
            note="起點",
            ts="2026-08-26T10:00:00",
            coords_um={"X": 12.5, "Y": -25.0},
            calib_snapshot={
                "X": {"lead_pitch_mm": 1.0, "step_angle_deg": 0.9, "division": 80},
            },
        ),
        # 通訊失敗：power=None，ok=False
        Sample(
            coords={"X": 1100.0, "Y": -2000.0},
            ok=False,
            power=None,
            note="讀值失敗",
            ts="2026-08-26T10:00:01",
        ),
        # 低於絕對下限：ok=False 但 power 有實際數值（Sample docstring 的第二種）
        Sample(
            coords={"X": 1200.0, "Y": -2000.0},
            ok=False,
            power=-70.0,
            note="低於下限",
            ts="2026-08-26T10:00:02",
        ),
        Sample(
            coords={"X": 1300.0, "Y": -2000.0},
            ok=True,
            power=-12.25,
            note="最佳",
            ts="2026-08-26T10:00:03",
            coords_um={"X": 16.25, "Y": -25.0},
        ),
    ]


class TestExportBasics:
    def test_檔案產生且有兩張工作表(self, tmp_path):
        out = export_samples_xlsx(_samples(), tmp_path / "s.xlsx", completed=True)
        assert out.exists()
        assert zipfile.is_zipfile(out)
        assert list(_read_xlsx(out).keys()) == ["摘要", "樣本"]

    def test_不留下tmp暫存檔(self, tmp_path):
        export_samples_xlsx(_samples(), tmp_path / "s.xlsx", completed=True)
        assert list(tmp_path.glob("*.tmp")) == []

    def test_沒有樣本要拋ValueError(self, tmp_path):
        with pytest.raises(ValueError):
            export_samples_xlsx([], tmp_path / "s.xlsx")

    def test_目錄不存在時自動建立(self, tmp_path):
        target = tmp_path / "深" / "一層" / "s.xlsx"
        export_samples_xlsx(_samples(), target, completed=True)
        assert target.exists()


class TestSampleSheet:
    def _sheet(self, tmp_path):
        out = export_samples_xlsx(_samples(), tmp_path / "s.xlsx", completed=True)
        return _flat(_read_xlsx(out)["樣本"])

    def test_表頭欄位與順序(self, tmp_path):
        head = self._sheet(tmp_path)[0]
        assert head == [
            "#", "時間",
            "X (pulse)", "Y (pulse)",
            "X (µm 估算)", "Y (µm 估算)",
            "功率 (dBm)", "有效", "備註",
        ]

    def test_每筆樣本一列(self, tmp_path):
        rows = self._sheet(tmp_path)
        assert len(rows) == 1 + 4  # 表頭 + 4 筆

    def test_pulse座標寫成數值(self, tmp_path):
        rows = self._sheet(tmp_path)
        assert float(rows[1][2]) == 1000.0
        assert float(rows[1][3]) == -2000.0

    def test_ok為False但有讀值時功率照樣寫出(self, tmp_path):
        # 🔴 這是刻意的行為：低於絕對下限的讀值是事後判斷「門檻設太高」的
        # 唯一依據，不可以因為 ok=False 就抹掉數值（有效欄已能分辨）。
        rows = self._sheet(tmp_path)
        assert float(rows[3][6]) == -70.0
        assert rows[3][7] == "否"

    def test_通訊失敗那筆功率留空(self, tmp_path):
        rows = self._sheet(tmp_path)
        assert rows[2][6] == ""
        assert rows[2][7] == "否"

    def test_沒有um的樣本um欄留空而非填0(self, tmp_path):
        rows = self._sheet(tmp_path)
        assert rows[2][4] == "" and rows[2][5] == ""

    def test_備註原樣寫出(self, tmp_path):
        rows = self._sheet(tmp_path)
        assert [r[8] for r in rows[1:]] == ["起點", "讀值失敗", "低於下限", "最佳"]


class TestSummarySheet:
    def _text(self, tmp_path, **kw):
        out = export_samples_xlsx(_samples(), tmp_path / "s.xlsx", **kw)
        return "\n".join(
            "\t".join(row) for row in _flat(_read_xlsx(out)["摘要"])
        )

    def test_完成狀態(self, tmp_path):
        assert "完成" in self._text(tmp_path, completed=True)

    def test_中止時寫出中止原因(self, tmp_path):
        text = self._text(tmp_path, completed=False, abort_reason="使用者中止")
        assert "中止／未完成" in text
        assert "使用者中止" in text

    def test_樣本數與有效樣本數(self, tmp_path):
        text = self._text(tmp_path, completed=True)
        assert "樣本總數\t4" in text
        assert "有效樣本數\t2" in text

    def test_最佳功率取ok為True中的最大值(self, tmp_path):
        # -70.0 雖然不是最小值也不參與比較（ok=False），最佳應是 -12.25
        text = self._text(tmp_path, completed=True)
        assert "-12.25" in text

    def test_有um欄時附上估算值警語(self, tmp_path):
        # μm 是依校正參數換算的估算顯示值，報表被單獨傳出去時這句是唯一
        # 能阻止讀者當成實測位移引用的東西（見 docs/axis-calibration.md）
        assert "估算值，非實測位移" in self._text(tmp_path, completed=True)

    def test_寫出校正參數快照(self, tmp_path):
        text = self._text(tmp_path, completed=True)
        assert "X 軸校正參數" in text
        assert "0.9" in text

    def test_extra_meta會被寫進摘要(self, tmp_path):
        out = export_samples_xlsx(
            _samples(), tmp_path / "s.xlsx", completed=True,
            extra_meta={"匯出方式": "使用者手動匯出（尋光分頁）"},
        )
        text = "\n".join("\t".join(r) for r in _flat(_read_xlsx(out)["摘要"]))
        assert "使用者手動匯出（尋光分頁）" in text

    def test_整輪無有效讀值不會炸(self, tmp_path):
        dead = [
            Sample(coords={"X": 0.0}, ok=False, power=None, ts="2026-08-26T10:00:00"),
            Sample(coords={"X": 10.0}, ok=False, power=None, ts="2026-08-26T10:00:01"),
        ]
        out = export_samples_xlsx(dead, tmp_path / "d.xlsx", completed=False)
        text = "\n".join("\t".join(r) for r in _flat(_read_xlsx(out)["摘要"]))
        assert "整輪沒有任何有效讀值" in text


class TestAxisOrdering:
    def test_軸依AXES順序而非出現順序(self, tmp_path):
        s = [Sample(coords={"Z": 1.0, "X": 2.0, "Y": 3.0}, ok=True, power=-1.0)]
        out = export_samples_xlsx(s, tmp_path / "s.xlsx", completed=True)
        head = _flat(_read_xlsx(out)["樣本"])[0]
        assert head[2:5] == ["X (pulse)", "Y (pulse)", "Z (pulse)"]

    def test_未知軸名附在後面而不是被丟掉(self, tmp_path):
        # 報表少一整欄比多一欄難察覺得多——寧可多印
        s = [Sample(coords={"X": 1.0, "Q": 9.0}, ok=True, power=-1.0)]
        out = export_samples_xlsx(s, tmp_path / "s.xlsx", completed=True)
        head = _flat(_read_xlsx(out)["樣本"])[0]
        assert "Q (pulse)" in head


class TestPersistSamplesIntegration:
    """persist_samples() 寫 JSON 之外，要在旁邊順帶產出同名 .xlsx。"""

    def _scanner(self):
        # 只測持久化，不跑搜尋——直接建實例後塞 samples 進去。
        # __init__ 需要 ctrl 與 power_query，給最小可用的假物件即可。
        class _Ctrl:
            connected = True
            scanning_active = False
            axis_calib = {}
            positions_machine = {}

        sc = fiber_scanner.FiberAlignmentScanner(_Ctrl(), lambda: (True, -30.0))
        sc.samples = _samples()
        return sc

    def test_同時產出json與xlsx(self, tmp_path):
        sc = self._scanner()
        json_path = sc.persist_samples(completed=True, scan_dir=tmp_path)
        assert json_path is not None and json_path.suffix == ".json"
        xlsx = json_path.with_suffix(".xlsx")
        assert xlsx.exists()
        assert sc.last_xlsx_path == xlsx
        # 正常完成、沒有中止過的情況：兩欄都應該是 None
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["abort_reason"] is None
        assert data["abort_kind"] is None

    def test_json保存中止原因與類型(self, tmp_path):
        # 🔴 迴歸測試：JSON 是「跑到哪裡出問題」的無損原始紀錄，中止原因
        # 與型別化分類（no_signal／other／exception）只寫進 Excel 是不夠
        # 的——Excel 匯出失敗、或報表沒有一併交付時，JSON 必須自己就能
        # 區分使用者停止、EMS、無訊號、未預期例外，不能只留樣本本身。
        sc = self._scanner()
        sc.last_abort_reason = "偵測不到訊號：已掃完搜尋範圍"
        sc.last_abort_kind = "no_signal"

        json_path = sc.persist_samples(completed=False, scan_dir=tmp_path)
        assert json_path is not None

        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["completed"] is False
        assert data["abort_reason"] == sc.last_abort_reason
        assert data["abort_kind"] == sc.last_abort_kind == "no_signal"

    def test_xlsx寫失敗不影響json(self, tmp_path, monkeypatch):
        # 🔴 這是這個功能最重要的一條：JSON 是「跑到哪裡出問題」的權威
        # 紀錄，絕不能被報表格式的失敗拖下水（實務上最常見的是目標檔正被
        # Excel 開著造成的 PermissionError）。
        sc = self._scanner()

        def _boom(*a, **kw):
            raise PermissionError("檔案正被其他程式使用")

        monkeypatch.setattr(fiber_scanner, "export_samples_xlsx", _boom)
        json_path = sc.persist_samples(completed=True, scan_dir=tmp_path)
        assert json_path is not None and json_path.exists()
        assert sc.last_xlsx_path is None

    def test_沒有樣本時兩份都不寫(self, tmp_path):
        sc = self._scanner()
        sc.samples = []
        assert sc.persist_samples(completed=True, scan_dir=tmp_path) is None
        assert list(tmp_path.glob("*")) == []

    def test_同一秒內連續兩輪保存不覆蓋(self, tmp_path, monkeypatch):
        # 🔴 迴歸測試：檔名曾經只有秒級精度（%Y%m%d_%H%M%S），同一秒內
        # 連續兩次 persist_samples() 會算出同一個檔名，tmp.replace() 不
        # 報錯、第一輪的原始樣本會被靜默覆蓋消失。改成微秒精度
        # （%_f）後，同一秒仍要能產生兩組互不覆蓋的 json／xlsx。
        import fiber_scanner as fs
        from datetime import datetime as _real_datetime

        class _FakeDateTime:
            _us = 0

            @classmethod
            def now(cls):
                cls._us += 1
                # 刻意固定同一秒，只讓微秒遞增，模擬「同一秒內連續保存」
                return _real_datetime(2026, 1, 1, 12, 0, 0, cls._us)

        monkeypatch.setattr(fs, "datetime", _FakeDateTime)

        sc = self._scanner()
        path1 = sc.persist_samples(completed=True, scan_dir=tmp_path)
        sc2 = self._scanner()
        path2 = sc2.persist_samples(completed=True, scan_dir=tmp_path)

        assert path1 is not None and path2 is not None
        assert path1 != path2
        assert path1.exists() and path2.exists()
        assert path1.with_suffix(".xlsx").exists()
        assert path2.with_suffix(".xlsx").exists()
        # 兩份 json 各自的樣本內容都要還在，不是其中一份被蓋成空的
        assert json.loads(path1.read_text(encoding="utf-8"))["sample_count"] == len(_samples())
        assert json.loads(path2.read_text(encoding="utf-8"))["sample_count"] == len(_samples())

    def test_mkdir失敗時persist_samples回傳None不外拋(self, tmp_path, monkeypatch):
        # 🔴 迴歸測試：out_dir.mkdir() 曾經寫在 try 區塊外，PermissionError
        # 會直接炸穿 persist_samples()，進而炸穿 run() 的 finally——掃描本身
        # 正常結束也會被 GUI 顯示成「尋光異常結束」。mkdir 必須跟 JSON 寫入
        # 共用同一個 except OSError，之後重構絕不能把它移回 try 外面。
        sc = self._scanner()
        missing_dir = tmp_path / "no_such_subdir"

        def _boom_mkdir(self, *a, **kw):
            raise PermissionError("拒絕存取")

        monkeypatch.setattr(Path, "mkdir", _boom_mkdir)
        result = sc.persist_samples(completed=True, scan_dir=missing_dir)
        assert result is None
        assert sc.last_xlsx_path is None
        assert not missing_dir.exists()
