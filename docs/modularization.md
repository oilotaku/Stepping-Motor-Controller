# 模組化現況與下一步門檻

> 本文件自 CLAUDE.md 拆出（2026-08-26），目的是縮小每次對話的固定載入量。
> **內容未經刪減**，動到對應功能前請完整讀過本檔。

### 模組化現況與下一步門檻（2026-08-18 重新評估）

方向1b（拆 `DS102GUI`）**目前仍不建議做**。1a 完成後 main_ai.py 從 4839 行漲到 5106 行（+5.3%，安全常數橫幅＋軸校正參數卡片），architect 判斷這不是「肥大到出事」的訊號——判準不該用行數，而是「新功能有沒有被迫在多個分頁間來回耦合寫入」，這次沒有：七個 `_build_tab_*` 邊界依然清楚，新功能都乾淨落在既有分頁或 Core 重繪迴圈裡。

**若之後真的要拆，切分方案已經先備好**（依風險由低到高）：`LogTabMixin`（~170 行，無反向依賴，適合當 mixin 模式的試點）→ `PointsTabMixin`（~230 行，只讀 `ctrl.teaching_points`/`ctrl.goto_point`/`ctrl.estimate_um`）→ `RecordingTabMixin`（~500 行，內部四張卡片只互相呼叫）。三塊合計約 900 行可搬出。**儀表板、移動控制、光功率＋尋光暫不適合拆**：儀表板的 `_update_stat_ui`/`_redraw_positions` 要橫跨讀取其餘六個分頁狀態，硬拆會變成互相 import；移動控制承載 EMS/STOP/原點復歸等全域行為，且軸校正資料被 Core／Teaching 分頁共用，是七個分頁裡耦合面最廣的；光功率與尋光透過 `_scanner_power_query()`/`_pm_sync_scan_notice()` 直接呼叫對方私有方法（第五階段刻意協調），要拆得兩個一起處理並先定義正式介面，工作量不小。

**具體觸發門檻**（下次評估直接對照，不必重新從頭判斷），任一項成立即觸發重新評估：

1. `_update_stat_ui`（目前讀 4 個跨分頁旗標：`playback_running`/`_homing`/`_scanning`/`ems_active`）或 `_redraw_positions`（目前讀約 6 項）內的跨分頁狀態判斷再增加 2 項以上——代表 Core 本身已是事實上的上帝方法，這時候先拆 Core 自己的職責（例如抽出獨立的 `_busy_reasons()`），不是急著拆分頁。
2. 出現第二對「兩個分頁互相呼叫對方私有方法」的關係（目前唯一一對是 Scan↔Power）。
3. 新功能被迫寫進兩個以上分頁、且是**雙向**耦合（A 改 B 的 widget 狀態，不只是共用同一份 controller 資料）——軸校正參數雖跨了 Control/Teaching/Core 三處，但都是單向讀取 `estimate_um()`，不算數。
4. main_ai.py 總行數達到約 **5800～6000 行**，且新增內容分散在多個分頁而非集中在 1～2 個（分頁邊界依然清楚、只是又加一張獨立卡片的情況不算）。

**測試基礎建設對未來 mixin 化是有利的**：`conftest.py`／兩份 pytest 測試檔全部透過 `gui.ctrl`/`gui.meter`/`monkeypatch.setattr(gui, ...)` 存取單一 `gui` 物件屬性，沒有假設方法定義在哪個檔案——只要最終類別仍叫 `DS102GUI`、由 `main_ai.DS102GUI(root)` 建構，mixin 化後現有 123 項測試不用改一行。唯一地雷：`conftest.py` 是用 `patch.object(main_ai, "RECORDING_DIR", ...)` 動態改模組層級名字，如果將來某個 mixin 檔案在模組層級快取了 `RECORDING_DIR` 的值而非透過 `self`／動態 import 存取，這個 monkeypatch 會失效、測試會意外寫進真實 `recordings/`——現在還沒發生，真拆檔案時要提醒實作者。

⚠ **`__init__`（main_ai.py:475-916 附近）存在只靠註解提醒、沒有機制強制的初始化順序相依**：例如 `ctrl.load_axis_calib()` 必須在 `_build_notebook()` 之前執行，卡片建構時才讀得到值；`_scanner_cfg_pending` 要等 `_build_tab_scan` 才會被消費。將來若真的把 `__init__` 拆成各 mixin 自己的 `_init_xxx_state()`，這些順序相依必須顯式化（例如一份有序的 init 步驟清單），否則會在特定操作路徑上悄悄讀到空值、且不會立刻炸出來。

✅ **技術債已解決（2026-08-18）：四份設定檔讀取骨架抽成共用函式。** `_load_app_settings`／`_load_safety_settings`（ds102_ctrl.py）與 `_load_meter_config`／`_load_scanner_config`（main_ai.py）原本逐字同一套骨架，現在統一呼叫 `ds102_ctrl._load_json_settings(path, label, log=None, error_level="INFO", not_found_msg=None, fail_msg=..., invalid_type_msg=..., success_msg=None)`，用參數精確重現原本兩種行為模式：app/safety 是「詳細」模式（不傳 `log`、走模組 `logger`、INFO 等級、「不存在」與「成功」都記錄）；meter/scanner 是「安靜」模式（可選 `log` callback、ERROR 等級、「不存在」與「成功」都不記錄）。四個原函式對外的名稱／參數／回傳型別完全沒變，純內部實作重構。連帶讓 main_ai.py 的 `import json` 變成真正未使用，已移除。**新增任何第五份設定檔讀取函式，一律呼叫這個共用函式，不要再複製骨架。**

至於「`_app_settings`/`_app_setting_num`/`CLR_*` 獨立成第三個檔案」這項更早的技術債——**維持先前結論仍不急**，觸發時機該跟方向1b綁在一起（真的做 mixin 化時勢必要重新梳理 import 邊界，那時候順手做是同一批工），現在單獨做只是提前付 import 調整成本卻拿不到額外好處。
