# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

本專案的程式註解與文件皆為繁體中文，請沿用同樣語言撰寫新的註解與文件。

## 專案性質

Windows 桌面應用，用 Python + tkinter 控制 **駿河精機 SURUGA SEIKI DS102 / DS112 步進馬達控制箱**（RS-232C / USB 虛擬 COM 埠），用於光纖對準與光學自動化量測。長期目標（見 [step-motor.txt](step-motor.txt)）是把滑台與 **HP 8153A 光波萬用表**（GPIB）串起來做自動掃描尋光。

沒有 CI、沒有套件化結構——全部是可直接執行的頂層腳本。有三支回歸測試腳本（`verify_scan_tab.py` 57 項、`verify_meter_panel.py` 66 項、`verify_axis_calib.py` 50 項，共 173 項，見下方〈常用指令〉），用假物件跑 GUI 邏輯。2026-08-18 起改寫成 **pytest 測試檔**（檔名不變，透過 `pytest.ini` 的 `python_files` 設定讓 pytest 認得 `verify_*.py` 這個既有命名），VS Code 的 Testing 面板可以個別發現、個別重跑每一項；`conftest.py` 放共用的 fixture（建 GUI、跑 Tk mainloop、monkeypatch `RECORDING_DIR`）。

🔴 **`conftest.py` 的 `make_gui()` 必須同時 `patch.object(main_ai, "RECORDING_DIR", ...)` 與 `patch.object(ds102_ctrl, "RECORDING_DIR", ...)`，只 patch 一邊等於沒防護（2026-08-19 實測踩到）。** `main_ai.py` 是用 `from ds102_ctrl import RECORDING_DIR` 重新引入，這只是另一個獨立綁定同一初始物件的名字；`DS102Controller` 的持久化方法（`save_point`／`set_axis_calib`／`save_recording`／`capture_controller_config` 等）全部定義在 `ds102_ctrl.py`，引用的是該模組**自己的**模組層級綁定，只 patch `main_ai.RECORDING_DIR` 完全攔不到這些方法，會直接寫進專案真正的 `recordings/`。`verify_scan_tab.py`／`verify_meter_panel.py` 之前沒踩到純粹是因為沒呼叫到這些方法，不代表這層防護真的有效——跟本檔記載「測試腳本清空過兩次 teaching points」是同一類風險。已在 `make_gui()` 修好，**任何新增的測試檔案都不需要（也不應該）再自己額外 patch 一次**，但改動 `conftest.py` 本身時要記得這兩邊要同步。

## 常用指令

一律使用專案內的 venv 直譯器（全域 Python 沒有 pyserial / PyVISA）：

```bash
venv/Scripts/python.exe main_ai.py                       # 執行主程式（GUI）
venv/Scripts/python.exe probe_ds102.py                   # 診斷：列出序列埠並輪詢 *IDN?
venv/Scripts/python.exe probe_ds102.py --list            # 只列埠，不送任何指令（不會動到硬體）
venv/Scripts/python.exe -m serial.tools.list_ports -v    # 原始序列埠清單
venv/Scripts/python.exe -m pip install -r requirements.txt
venv/Scripts/python.exe -m ruff check .                  # ruff 未列於 requirements.txt，需另行安裝
venv/Scripts/python.exe -m pytest verify_scan_tab.py verify_meter_panel.py verify_axis_calib.py -v  # 三支合計 173 項，不需硬體
venv/Scripts/python.exe -m pytest verify_scan_tab.py::TestUserStop -v          # 只跑某個 class／單一測試（VS Code Test Explorer 用同一套機制）
```

VS Code 已設定對應的 tasks（預設 build task = 執行 GUI）與 launch 設定，見 [.vscode/](.vscode/)。

終端機執行時務必帶 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`（Windows 主控台預設 cp950，否則中文 log 會亂碼）。VS Code 側已涵蓋：`settings.json` 的 `terminal.integrated.env.windows` 對整合終端機全域生效、`launch.json` 四個 configuration 全部自帶 `env`；`tasks.json` 只有「執行 DS102 GUI」自帶 `env`，其餘三個 task 靠 settings 的全域設定拿到（都是 `type: shell`，所以有效）。

🔴 **沒有模擬模式。** 2026-08-06 依使用者要求整個移除（連同 `connect_sim` / `_sim_parse` / `_sim_query` / `sim_mode` 的全部判斷）。**不要再加回來**——這支程式驅動的是真實滑台，假造的回應會讓使用者以為自己連上了硬體。

沒有硬體時要測 UI／控制邏輯，用**假的 serial 物件**取代 `ctrl.ser`：注意 `query_status()` 是三段式，`SB3?` 必須回 bit0=1（`"1"`）該軸才會被視為可選取，否則 `_wait_axis_stop()` 會立刻回 False。scratchpad 的 `verify_*.py` 都有現成寫法。

## 檔案定位（哪個才是主程式）

根目錄有三份高度相似的 DS102 程式，改錯檔案是最常見的失誤：

| 檔案 | 定位 |
|---|---|
| [main_ai.py](main_ai.py) | **唯一的主程式（v3.0）**，功能與修正都加在這裡 |
| [ds102_ctrl.py](ds102_ctrl.py) | 🔴 **不要跟下面的 `ds102_controller.py` 搞混**——這是 2026-08-17 從 main_ai.py 拆出來的 `DS102Controller` 本體（現役程式碼，約 1945 行），main_ai.py 用 `from ds102_ctrl import DS102Controller, ...` 引入。細節見下方〈main_ai.py 架構〉 |
| [ds102_controller.py](ds102_controller.py) | main_ai.py 的前一版快照（約 1970 行，跟上面的 `ds102_ctrl.py` 是完全不同的兩個檔案）。已進版控，可作為對照，但**不要在此新增功能** |
| [main.py](main.py) | 廠商 SURUGA SEIKI 官方範例（模組層級全域變數風格），是**指令格式的權威來源**。main_ai.py 的每個指令組法都對應此檔某段程式。修改指令時先回頭比對 |
| [test.py](test.py) | 無 GUI 的連線 / 狀態查詢腳本（含 `find_ds_port()` 自動搜埠）。名稱誤導——不是單元測試 |
| [probe_ds102.py](probe_ds102.py) | 序列埠診斷工具，硬體接不上時的第一站 |
| [meter_GPIB.py](meter_GPIB.py) | HP 8153A 光功率計封裝（PyVISA）。**已於 2026-08-12 整合進 GUI**（main_ai.py 直接 `from meter_GPIB import HP8153APowerMeter`），供「光功率」與「尋光」分頁使用；仍**未接上真實儀器驗證過**，本機沒有 GPIB 卡可測 |
| [fiber_scanner.py](fiber_scanner.py) | `FiberAlignmentScanner`：光纖對準尋光演算法（座標下降＋K近鄰精修＋收尾微擾）。**已於 2026-08-13～17 分六階段接上 GUI**（main_ai.py 的「尋光」分頁），並補上 57 項假物件回歸測試（見下方〈光功率／尋光分頁〉）。本檔自己**仍刻意不 import main_ai.py**（避免循環相依），軸命名自成一份，main_ai.py 改軸命名時要同步 |
| [verify_scan_tab.py](verify_scan_tab.py) / [verify_meter_panel.py](verify_meter_panel.py) / [verify_axis_calib.py](verify_axis_calib.py) | 「尋光」／「光功率」分頁／軸機械校正參數的假物件回歸測試（合計 173 項 pytest 測試函式，`python -m pytest verify_scan_tab.py verify_meter_panel.py verify_axis_calib.py -v` 執行，VS Code Testing 面板也認得）。用假的 `ctrl` / `meter` 物件驅動邏輯，不需要真實硬體 |
| [conftest.py](conftest.py) / [pytest.ini](pytest.ini) | pytest 共用設定：`conftest.py` 放建 GUI／跑 Tk mainloop／monkeypatch `RECORDING_DIR` 這類共用 fixture；`pytest.ini` 把 `python_files` 放寬成同時認得 `verify_*.py` 與標準 `test_*.py`，並排除 `venv`／驅動資料夾等不相關目錄 |
| [Gtest.py](Gtest.py) | 外部第三方範例（NTT-Mabuchi），`import control` 的模組不存在於本 repo，**無法執行**，僅作參考 |
| [step-motor.txt](step-motor.txt) | 三層架構藍圖與 GPIB 側注意事項。⚠ 但其中的 **DS112 通訊細節全部是錯的**（宣稱結束符 `\r\n`、鮑率 9600、用 `!:` 輪詢 B/R 狀態）——實機是 `\r`、38400、查 `SB1?`。此檔只採信 HP 8153A 與「馬達動則不讀光」那幾段 |
| [FIBER_ALIGNMENT_SCAN_DESIGN.md](FIBER_ALIGNMENT_SCAN_DESIGN.md) | 尋光演算法的完整設計文件：mathematician 兩輪演算法討論、architect 落地評估、無硬體驗證方式、待實測參數清單 |

## main_ai.py 架構

main_ai.py 約 5106 行（2026-08-06 時約 3100 行，2026-08-12～17 加入光功率／尋光兩分頁後一度衝到 6601 行，2026-08-17 把 `DS102Controller` 拆出去後降到 4839 行，之後陸續加入 B 類安全常數橫幅與軸機械校正參數卡片，漲回目前規模），邏輯上仍是三塊，但**`DS102Controller` 現在實際定義在 [ds102_ctrl.py](ds102_ctrl.py)**：

1. **`DS102Controller`**（[ds102_ctrl.py](ds102_ctrl.py)，約 2221 行）— 所有序列通訊集中於此，完全不碰 tkinter。對外只暴露 `connect()` / `move_step()` / `query_status()` / `goto_point()` 等高階方法。main_ai.py 開頭用 `from ds102_ctrl import DS102Controller, AXES, AXIS_NO, NO_AXIS, MODE_CONTINUE, MODE_STEP, MODE_ORIGIN, COMM_FAIL_THRESHOLD, _BASE_DIR, LOG_DIR, RECORDING_DIR, DATA_DIR, NON_RECORDING_JSON, logger, _write_json_with_backup, _app_settings, _app_setting_num` 整批重新引入——這份清單就是 `DS102Controller` 的完整依賴閉包，改動任一邊的模組層級常數前先確認它有沒有在這份清單裡。
2. **`StatusBar`**（仍在 main_ai.py）— 各分頁共用的座標 / 連線狀態列（同時存在多個實例，統一收在 `self._status_bars`）。**刻意沒有跟著搬去 `ds102_ctrl.py`**：它用到的 `CLR_*` 色票（含 `app_settings.json` 覆寫邏輯）留在 main_ai.py，若把 `StatusBar` 也搬走，`ds102_ctrl.py` 會反過來需要 import main_ai.py 的色票，形成循環相依；`StatusBar` 本身只有約 120 行、且與 `DS102GUI` 的 `self._status_bars` 集中管理耦合更緊，留給下次拆 `DS102GUI` 時一併考慮較合適。
3. **`DS102GUI`**（main_ai.py）— 七個分頁（分頁標題字串為「儀表板 / 移動控制 / Teaching / 行程錄製 / 光功率 / 尋光 / LOG」，grep 時用這些字），只呼叫 controller 的公開方法。「光功率」封裝 `HP8153APowerMeter`（[meter_GPIB.py](meter_GPIB.py)）、「尋光」封裝 `FiberAlignmentScanner`（[fiber_scanner.py](fiber_scanner.py)），細節見下方〈光功率／尋光分頁〉。

⚠ `fiber_scanner.py` 的 `TYPE_CHECKING` 型別提示已同步改成 `from ds102_ctrl import DS102Controller`（原本指向 main_ai.py，`DS102Controller` 搬家後這裡也要跟著改，否則型別提示會指向錯誤的定義位置——雖然不影響執行期，但下次有人依賴它做型別檢查會查錯地方）。`fiber_scanner.py` 本身仍然不 import 任何一個 main_ai 系列模組，「避免循環相依」的方向沒變。

🔴 **main_ai.py 開頭那個 `from ds102_ctrl import (...)` 區塊裡的 `NON_RECORDING_JSON`，IDE／靜態分析會標成「unused import」，但不能因此移除。** [verify_meter_panel.py](verify_meter_panel.py) 直接讀 `main_ai.NON_RECORDING_JSON` 這個模組屬性做斷言，main_ai.py 程式邏輯本身確實沒用到它，但拿掉這個 import 會讓 `main_ai` 模組上不再有這個屬性，那支回歸測試整支炸掉（`AttributeError`）。2026-08-17 拆分 `ds102_ctrl.py` 時實際發生過兩次：coder 第一次搬移時就發現這個依賴、刻意加回重新引入清單；後續一輪「清理架構審查發現的死 import」又想拿掉，靠重跑 `verify_meter_panel.py` 才抓到。**靜態分析工具看不到外部測試腳本這種跨模組屬性依賴**——main_ai.py:275-280 附近有對應註解，改動這個 import 區塊前務必先跑 `verify_meter_panel.py`，不要只憑 grep 或 IDE 診斷判斷「看起來沒用到」。

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

### 執行緒規則（違反會凍結 UI 或炸掉 tkinter）

- 任何**會阻塞的序列操作**（`connect`、`wait_done=True` 的 `move_step` / `move_origin`、`play_recording`）必須在 `threading.Thread(daemon=True)` 執行。
- 背景執行緒**絕不可直接碰 tkinter widget**，一律 `self.root.after(0, lambda: ...)` 回主執行緒。
- 反過來也成立：**別用 `root.after` 排下一輪輪詢**。`after` 的callback 跑在 Tk 主執行緒，會把阻塞式序列查詢搬回 UI 執行緒導致凍結。輪詢迴圈要留在 worker 內用 `time.sleep`（見 `_poll_status`）。
- 序列埠交易受 `_serial_lock` 保護，一次 TX→RX 不可分割。移動執行緒的 `_wait_axis_stop` 與 UI 輪詢會同時查詢，沒有這把鎖時回應會被對方讀走——症狀是位置錯亂與**莫名其妙的 limit 警報**（把 `POS?` 的數值當成 `SB1?` 狀態位元解讀）。`emergency_stop()` 刻意不取這把鎖，避免等鎖延遲停止。
- `_poll_busy` 事件確保同時只有一條狀態輪詢執行緒（連按驅動鍵時不會疊出多條搶序列埠）。
- `_positions_pulse` / `_offsets` 由 `self._lock` 保護，`action_history` 由 `self._history_lock` 保護。
- 睡眠寫法的規則是「**有對應的收工 Event 就用 `event.wait(interval)`，沒有就 `time.sleep`**」，不是一律用哪一種：
  - `_start_position_worker` 用 `self._shutting_down.wait(...)`、`_watch_jog_limit` 用 `self._jog_stop.wait(...)`
  - `_poll_status` / `_wait_axis_stop` / `_wait_origin_done` 用 `time.sleep`——它們**沒有**檢查任何收工旗標
- ⚠ `_shutting_down` 定義在 **`DS102GUI.__init__`**，不是 controller 的屬性。在 `DS102Controller` 內寫 `self._shutting_down` 會直接 `AttributeError`。
- ⚠ 因此「關窗不會卡住」**只對 position worker 成立**。`_on_close()` set 完旗標就直接 `disconnect()` + `root.destroy()`，此刻若有 `_wait_origin_done` 在跑（最長 180s），它會繼續對已關閉的 port 打 `SB1?`。
- 依 [step-motor.txt](step-motor.txt) 的硬體限制：**不要**用多執行緒同時對 GPIB 與序列埠通訊，量測流程全程單執行緒依序執行。

#### 輪詢迴圈（各司其職，別互相取代）

原本四條，2026-08-12 光功率整合後加了第五條（獨立於 DS102 序列通訊，走 GPIB）、2026-08-13 尋光整合後加了第六條（純重繪，不做任何 I/O）：

| 迴圈 | 在哪 | 節奏 | 做什麼 |
|---|---|---|---|
| `_start_poller()` | Tk 主執行緒 `root.after` | `UI_REDRAW_INTERVAL` 100ms | **只重繪快取的座標**，完全不碰序列埠 |
| `_start_position_worker()` | 背景執行緒 | `POSITION_POLL_INTERVAL` 0.5s | 呼叫 `refresh_positions()`，對每個已啟用軸送一筆 `POS?` 寫回 `_positions_pulse` |
| `_poll_status()` | 背景執行緒 `while` + `time.sleep(0.1)` | 100ms | 移動中追蹤選取軸的狀態，`status != "Driving"` 就收工 |
| `_watch_jog_limit()` | 背景執行緒 | `JOG_WATCH_INTERVAL` 0.06s | 長按點動時監看軟體限位 |
| `_start_meter_poll_worker()` | 背景執行緒 | `METER_POLL_INTERVAL` 0.5s | 光功率背景輪詢（GPIB，與上述四條的序列埠通訊完全無關，不受 `_serial_lock` 影響） |
| `_redraw_scan_plot()`（經 `root.after`） | Tk 主執行緒 | `SCAN_PLOT_REDRAW_INTERVAL` 250ms | 只消化 `_scan_plot_pending` 佇列重繪 matplotlib 圖表，不觸發任何量測或移動 |

前兩者的分工是關鍵：`_start_poller` 之所以能跑 10ms 是因為它不做 I/O；`query_status()` 一次**只更新它被傳入的那一軸**，沒有 position worker 的話未選取的軸會永遠停在舊值。要加「畫面上某個數字沒在更新」的修正時，先確認該值是靠哪一條迴圈供應。

（原本是 10ms，2026-08-05 放寬到 100ms：資料源只有 2 Hz，10ms 等於 49/50 次在重繪同一個數字，代價是每秒約 600 次 `_lock` 取放與數千次 `Label.config()` 全堆在 Tk 主執行緒上。這是「介面越跑越鈍」的成因之一。）

#### LOG 量的控制（別把它改回去）

`_serial_write_read` 對 `_POLL_QUERIES`（`POS?` / `SB1?` / `SB2?` / `SB3?`）**不寫 DEBUG TX/RX**。這些是每秒十幾筆的例行輪詢，逐筆記錄會同時灌爆 LOG 檔、`action_history`、GUI 的 Text widget，而且每一筆都要 `root.after` 回主執行緒重繪五個 StatusBar——實測是「LOG 過多」與介面卡頓的直接成因。要追通訊細節時把 `DS102Controller.verbose_poll_log` 設 `True` 即可恢復。

WARN／ERROR 一律照記，安靜的只有成功路徑。另有兩道上限：`action_history` 最多 `HISTORY_MAX`(5000) 筆、GUI LOG 文字框最多 `LOG_TEXT_MAX_LINES`(2000) 行，都是無上限成長會拖垮程式的地方。

#### 長按點動的限位保護（`move_continue` / `_watch_jog_limit`）

🔴 **先講最重要的：這兩層保護預設一層都不會生效。** `sw_limits` 初始值全是 `(None, None)`，GUI 的限制輸入框開機是空字串，而且**沒有任何程式會從控制器把 `CWSLP`/`CCWSLP` 讀回來填進去**。除非使用者手動打數字並按「套用限制設定」，`lim` 永遠是 `None` → 第一層不執行、第二層執行緒根本不啟動。出廠狀態下長按點動**完全依賴韌體端限位**。別把下面這兩層當成常態保護。

點動沒有目標座標，無法像 `move_step` 那樣事先攔截，所以（在 `sw_limits` 有值時）是兩層：

1. **出發前**——已經壓在該方向的軟體限位上就直接拒絕啟動。
2. **移動中**——`_watch_jog_limit` 背景執行緒輪詢 `POS?`，越界立刻 `stop()`。

第二層是「事後偵測」，從發現到停穩還會滑一段，提前量 `lookahead = f_speed × period + f_speed × rate / 2000`。`period` **必須用每輪實測值**（`time.time()` 差）而非常數：實測用常數 60ms 時真正的週期是 116ms，結果滑出限位 36 pulse。改動這裡前先讀該函式的 docstring，兩次超限的數據都記在裡面。

`self._jog_stop` 事件負責讓監看執行緒收工——`stop()` 會 set 它，所以任何新增的停止路徑都要記得 set，否則執行緒會活到程式結束。

#### `scanning_active` 與 `scan_move_step`（尋光演算法用，2026-08-07）

`FiberAlignmentScanner`（[fiber_scanner.py](fiber_scanner.py)，完整設計見 [FIBER_ALIGNMENT_SCAN_DESIGN.md](FIBER_ALIGNMENT_SCAN_DESIGN.md)）跑的時候會把 `ctrl.scanning_active` 設為 `True`，`move_step` / `move_continue` / `move_origin` / `origin_all` / `goto_point` 的守衛都比照 `playback_running` 加上這個判斷，`_start_position_worker` 也排除它，避免背景輪詢跟演算法搶 `_serial_lock`。

🔴 **`move_step` 因此不能被 scanner 自己呼叫**——它的守衛會把演算法自己的移動也一併擋下（這是實作時真的踩到的 bug：第一版直接讓 `scanning_active` 進 `move_step`，結果所有收斂測試都卡住不動，因為演算法呼叫自己的移動時被自己設的旗標擋住）。移動邏輯拆成 `_do_move_step()`（實作，無守衛）+ `move_step()`（GUI 用，含 `scanning_active` 守衛）+ `scan_move_step()`（scanner 專用，只受 `ems_active` 攔截）三層。**新增任何呼叫移動的程式碼前，先想清楚是 GUI 操作還是 scanner 內部邏輯，選對入口。**

`check_sw_limits_batch()` 與 `wait_axis_stop()`（`_wait_axis_stop` 的公開版本）是專門給 scanner 多軸批次移動流程用的公開方法。`positions_machine` 屬性回傳機械座標，供 scanner 全程在同一個座標系下運作（不透過 offset）。

#### 尋光彈性選軸（1～6 軸，2026-08-19）

`FiberAlignmentScanner` 原本就是動態軸數架構（`_active_axes()` 用 `ctrl.axis_count`＋`query_status()` 即時偵測硬體可用性，座標下降／K 近鄰精修的迴圈與矩陣維度全部用 `len(axes)` 動態決定，不是寫死 3 或 6），這次新增的是**使用者主動排除某軸**的能力，不是「自動跟著接了幾軸走」——例如實機接了 X/Y/Z 三軸，使用者這次可以只勾選 X/Z 搜尋。

- **`FiberAlignmentScanner.__init__` 新增 `selected_axes: Optional[List[str]] = None`**（關鍵字傳遞），存成 `self._selected_axes`。`_active_axes()` 在既有的硬體可用性偵測**之後**，再與這份清單取交集；`None` 時完全不過濾，向下相容改動前的行為。`run_stage1`/`run_stage2`/`run_stage3` 完全不用改——它們一律呼叫 `self._active_axes()`，交集邏輯對它們透明，這是這次改動範圍能維持小的關鍵。
- **`run()` 開頭**先算一次 `self.active_axes = self._active_axes()`（供 GUI 讀「這次實際搜尋範圍」）：交集後為空就 `raise ScanAbort`，訊息依 `self._selected_axes is not None` 分兩種文案（「選定的軸目前皆不可動」vs 舊行為的「沒有可動的軸」）；非空但有勾選軸被交集排除（使用者勾了、但這軸現在被偵測為不可動），用 `self._log()` 回報一則警告，不靜默吞掉這個落差。
- **main_ai.py 的軸勾選 UI 刻意不「連線後重建分頁」**——沿用專案既有的「固定建六組、連線後依 `axis_count` 動態 enable/disable」模式（跟軸驅動按鈕群組 `_all_axis_btn_groups` 同一招），`_build_tab_scan` 用 `AXES`（固定六軸）建立，`_on_connect_result` 才依實際軸數切 `_scan_axis_checkbuttons` 的 state，停用的軸連帶把 `BooleanVar` 強制設回 `False`。「連線後重建分頁」這個做法被 architect 明確否決過——matplotlib canvas、自我重新排程的 `_redraw_scan_plot`、`_scanner_cfg_pending` 這些既有機制沒有為「分頁被銷毀重建」設計過，硬做風險遠高於沿用已驗證的既有模式。
- 🔴 **「該軸是否勾選搜尋」與「階段二總開關」是兩個獨立條件，必須用 AND 合成，不能讓其中一個 handler 覆寫另一個**——`_refresh_scan_entry_states()` 是唯一改 Entry state 的地方，`_on_scan_stage2_toggle()` 與六個軸勾選框的 command 都只呼叫這個函式，不再各自直接 `.config(state=...)`。這是照抄 `_update_stat_ui` 曾經「不認得復歸中被覆寫回正常」那次教訓的預防措施，兩層狀態疊加時最容易犯的錯就是後呼叫的 handler 無條件覆寫前一個已經算好的結果。
- **0 軸要擋在打開確認對話框之前**：`_do_start_scan()` 主執行緒收集 `selected_axes` 後立刻檢查，空清單就 `_flash_banner` 並 `return`，不讓使用者看到一個注定沒有意義的確認流程。確認對話框文案加了「搜尋軸：X、Z」這行，擺在起始步長摘要之前。
- **`scanner_config.json` 新增 `selected_axes` 欄位**，只在使用者確認開始尋光後才存（不是每次點勾選框就寫檔）。這跟 `initial_step`／`stage2_local_radius`／`axis_scale` 那些刻意不存檔的 per-axis 數值不同類——選軸是跟裝置物理配置綁定的操作習慣，比較像 `l_speed`／`step_min` 這類「跨次搜尋穩定的參數」。缺這個欄位（舊格式設定檔）時六軸預設全勾選，向下相容。
- **即時軌跡圖只做最小防呆**：`_redraw_scan_plot()` 畫 XY／XZ 子圖前檢查 `self._active_scanner.active_axes` 有沒有涵蓋對應軸，沒有就顯示文字提示（例如「本次搜尋未包含 X/Y 軸」）取代空白圖表。**任意 1～6 軸組合的完整視覺化留給下一輪**（要嘛軸對選擇器、要嘛多子圖矩陣，屬於版面設計問題不是這次選軸功能的架構問題）——樣本資料本身（`Sample.coords`）不受影響，CSV／JSON 落地永遠完整記錄實際搜尋到的軸，只有即時圖表這個呈現層有這個已知限制。
- ✅ **上面「即時軌跡圖只做最小防呆」提到的下一輪，已於 2026-08-19 完成**：左上子圖（原本寫死 XY 投影）改成**可切換軸對的 2D 投影**——`_scan_pair_combo`（Combobox，存字串 `"X-Y"` 這種格式、不存 index，理由跟 `ORG_MODES` 一樣避免差一位）依這次搜尋軸數決定顯示模式：≥3 軸顯示可切換的 Combobox、剛好 2 軸顯示靜態文字（只有一種配對不需要選）、剛好 1 軸完全不顯示（無配對可言，子圖改顯示提示文字）。右上子圖（原本寫死 XZ 投影）改成**多軸 1D 相對位移趨勢線**（`self._scan_trend_lines: Dict[str, Line2D]`，固定六條，y 軸是 `coord[i]-coord[0]` 而非絕對 pulse 值——不同軸行程差很多，共用 y 軸畫絕對值會讓小行程的軸看起來像壓平的線），天生支援任意 1～6 軸、不需要配對，是解決「只選 U/V 兩軸時兩張圖都變文字」的關鍵那一塊。🔴 **六條趨勢線刻意不用 `CLR_ACCENT`／`CLR_DANGER`／`CLR_WARN`／`CLR_INFO` 上色**——這幾色在本專案是「目前選取軸」「警報」等全域語意，這裡的「軸」是搜尋範圍的概念，混用會誤導；改用 `CLR_TEXT`（X/Y/Z）／`CLR_MUTED`（U/V/W）分群，同群組內再用線型（實線/虛線/點線）分軸。gridspec 骨架與既有的 250ms 重繪節奏（`SCAN_PLOT_REDRAW_INTERVAL`／`_redraw_scan_plot`→`_scan_plot_extend`→`_scan_redraw_figure`）完全不變，這次只是子圖內部畫什麼的替換，沒有新增執行緒風險。`_scan_plot_reset()` 簽章改為接受 `selected_axes` 參數（`None` 時退回讀 GUI 勾選狀態，供舊呼叫端沿用）。
- **已知的可選小問題（architect 審查發現，判定不影響安全性/正確性，不急著修）**：
  1. 軸數從少變多時（例如控制器從 3 軸換成 6 軸），先前因為 disable 被強制清空的軸不會自動恢復勾選，需要使用者手動重新勾——這是「排除軸」方向的非對稱，符合專案一貫的 fail-safe 原則（寧可少搜不多動），不是缺陷。
  2. `_do_start_scan()` 啟動背景執行緒到 `run()` 真正把 `scanner.active_axes` 寫入之間有一段極短視窗期，若剛好被 250ms 的 `_redraw_scan_plot` 排程撞上，圖表會短暫誤判成「未包含 X/Y 軸」再自動修正，純視覺閃爍，不影響任何資料或判斷邏輯。
  3. `cfg.get("selected_axes")` 讀檔時沒有型別驗證，手動改壞 `scanner_config.json`（例如存成非 list）會讓 `_build_tab_scan` 炸掉——這跟專案裡其他設定欄位（如 `enable_stage2`）同一種「相信自己寫出來的檔案格式正確」的既有慣例一致，不是這次改動特有的退步。

#### 光功率／尋光分頁（2026-08-12～17，分六階段＋一輪 architect 審查落地）

「光功率」分頁（`_build_tab_power`）與「尋光」分頁（`_build_tab_scan`）是兩個獨立但互相協調的分頁：

- **matplotlib 是尋光分頁專屬的選用相依**（`requirements.txt` 已列 `matplotlib`/`numpy`），main_ai.py 頂部用 `try/except ImportError` 判斷，裝不到就設 `_MATPLOTLIB_AVAILABLE = False`，「尋光」分頁改顯示安裝提示文字並停用，**不影響其餘六個分頁**。新增任何 matplotlib 呼叫前先確認在這個 guard 之內。
- **兩個分頁共用同一份 GPIB 連線與讀值**，由「尋光」進行中時接手：`ctrl.scanning_active` 為真時，「光功率」分頁的自動輪詢會暫停、改顯示「尋光中（讀值由尋光分頁提供）」，讀值改由尋光分頁的 sample callback 回寫。這個協調狀態**沒有獨立輪詢**，搭 `_redraw_scan_plot()` 既有的 250ms 節奏一併同步（實測用 `winfo_ismapped()` 當守衛會漏更新——使用者尋光結束時人不在「光功率」分頁，畫面會卡在「尋光中」直到下次尋光開始又結束且剛好切在該分頁——已改為不依賴分頁是否可見）。
- **`sample_cb` 跑在 scanner 的背景執行緒**，🔴 只能做資料寫入（丟進 `_scan_plot_pending` 佇列），不能碰 matplotlib 或 tkinter widget——實際重繪固定在 Tk 主執行緒的 `_redraw_scan_plot()` 做。
- **設定持久化**：`meter_config.json`（GPIB 位址／channel／波長）與 `scanner_config.json`（速度、安全判準、是否啟用階段二 K 近鄰精修等跨次搜尋穩定的參數）都在 `RECORDING_DIR`，走 `_load_meter_config()` / `_save_meter_config()` / `_load_scanner_config()` / `_save_scanner_config()`。兩者都是純量欄位的整份覆寫，**不需要**比照 teaching points 的 `_points_loaded` 拒寫保護（沒有「累積型集合被空狀態蓋掉」的風險）。兩個檔名都已加進 `NON_RECORDING_JSON`。
- ⚠ **階段二相關的 `tk.BooleanVar`（`_scan_stage2_var`）不能用寫死的初始值建立**——它要在讀到 `scanner_config.json` 的 `enable_stage2` 欄位後才建立變數，順序反了會讓存檔值永遠讀不回來（2026-08-17 由假物件回歸測試抓到並修正）。
- **回歸測試**：[verify_scan_tab.py](verify_scan_tab.py)（尋光分頁，57 項）、[verify_meter_panel.py](verify_meter_panel.py)（光功率分頁，66 項）、[verify_axis_calib.py](verify_axis_calib.py)（軸機械校正參數，50 項，涵蓋 `ds102_ctrl.py`／`fiber_scanner.py`／`main_ai.py` 三個層級）用假的 `ctrl` / `meter` 物件跑邏輯，不需要真實硬體或 GPIB 卡，改動對應功能後應該先跑對應的測試檔。2026-08-17 `DS102Controller` 拆到 `ds102_ctrl.py` 後前兩支仍全數通過，可作為「模組拆分沒有破壞既有行為」的既有驗證手段之一。

### 單位與座標（容易改錯的地方）

- **單位一律 pulse，沒有 um / mm 切換**（2026-08-05 移除）。連線時送 `AXI{n}:UNIT 0` 把控制器也固定在 pulse，所以 `POS?` 回傳值即 pulse，不需要任何換算函式。之所以拿掉：控制器裡的 `SD`（每 pulse 距離）並未配置實際尺度（`RESOLUT?` = 1），換算成 um/mm 等於拿未經驗證的假設去乘除。**這個決定仍然成立**——下面的〈軸機械校正參數〉是額外疊加的估算顯示，不是恢復這個切換。

#### 軸機械校正參數（`axis_calibration.json`，2026-08-18，純估算顯示）

使用者提供了實際滑台的官網規格（駿河精機 KHE06008-C，導程 1mm 滾珠螺桿＋0.72°/step 五相馬達），驗算出 `um/pulse = 導程(um) ÷ (360/步進角 × 分度值)` 這條公式跟官網標示的「Full-step 2μm/Pulse、Half-step 1μm/Pulse」完全吻合，因此新增這個功能——但**刻意只做附加估算顯示，不是恢復 um/mm 單位切換**：`move_step`／`goto_point`／限位比對／教點座標比對永遠只認 pulse，um 純粹是額外算出來、擺在座標旁邊給人參考的數字。

- **資料模型**：`ctrl.axis_calib: Dict[str, dict]`（`ds102_ctrl.py`），只有**參數填齊的軸才會是這個字典的 key**（跟 `sw_limits` 六軸都預先擺 `(None, None)` 不同——這裡沒有「安全預設值」的需求）。每軸存 `{lead_pitch_mm, step_angle_deg, division, ts}`。`division` 是**倍數本身**（Full=1、Half=2、1/10=10...），不是 `AXI{n}:DRDIV?` 那種韌體查詢索引——見下方為什麼這兩者刻意不合併。
- **持久化**：`recordings/axis_calibration.json`，走 `_write_json_with_backup()`，套用跟 `teaching_points.json` 同一套 `_axis_calib_loaded` 拒寫保護（沒 load 過就存會拒寫並記 ERROR）。`set_axis_calib(calib)` 是**合併更新**（只更新傳入的軸，其餘軸既有資料不受影響，仿照 `capture_controller_config()` 的合併邏輯），不是整份覆蓋。已加進 `NON_RECORDING_JSON`。
- **驗證是「全部成功或全部不動」**：`set_axis_calib()` 逐軸檢查導程/步進角為正數、分度值為正整數，任一軸不合法就整批不寫入、回傳錯誤字串清單——不會出現「六軸裡兩軸套用成功、一軸被拒絕」這種混合結果，避免使用者誤以為都套用了。GUI 端（`_apply_axis_calib()`）在呼叫 controller 之前還有一層本地檢查：**三個欄位必須一起填、一起留空**，只填一兩欄會被當成本地格式錯誤直接擋下，不會送到 controller。
- **`estimate_um(ax, pulse) -> Optional[float]`** 是純計算（無 I/O），軸沒有校正參數或參數不合法一律回傳 `None`，呼叫端據此決定「不顯示」而不是顯示 0 或猜測值。掛在三個既有重繪點（`_redraw_positions()`／`_refresh_points()`／教點列表），都是純格式化附加文字，**沒有新增任何輪詢迴圈**——`_start_poller` 100ms 節奏跑的是乘法，不是查詢，不違反「絕不碰序列埠」的規則。`StatusBar.update_coords()` 刻意**沒有**附加 μm（Label 固定 `width=10`，空間放不下）。
- 🔴 **`axis_calib.division`（使用者手動輸入）跟 `axis_drdiv`（連線時查詢、對 AMS 驅動器沒意義的軟體暫存器）刻意不合併、不自動代入、不互相驗證**——一個是使用者斷言的事實，一個是已知不可信的查詢結果，且兩者的數值定義不同（`division` 是倍數，`axis_drdiv` 的原始回應是查表索引）。GUI 上把兩欄並列顯示純粹讓使用者參考，**不要**為了「省事」讓 division 欄位預設抓 `axis_drdiv` 的值，那會把索引當倍數用，算出錯誤的 μm。
- 輸入卡片（`_build_card_axis_calib()`）放在**移動控制分頁**、緊接「控制器設定」卡片之後，六軸一視同仁列出（不弱化 U/V/W 這類未接滑台的軸，跟既有 `_build_card_sw_limits` 的寫法一致）。套用按鈕用 `Accent.TButton`，**不是** `Warn.TButton`、也**沒有**確認視窗——這不是清除保護、沒有安全含意，跟 `_apply_sw_limits` 清空限制時的情境不同，照抄那套確認流程反而是把安全性修正的既定模式錯誤地移植到非安全情境。
- 🔴 **`_apply_axis_calib()` 把「三欄全空」當成「本次不動這軸」，不是「清除」**——這是刻意的（避免使用者不小心清掉一欄就整軸資料消失），但代價是**原本完全沒有路徑能移除已存的校正參數**，2026-08-19 使用者實際回報這個缺口。修法：每一列加一顆獨立的「清除」按鈕（`Flat.TButton`，不是 `Danger`/`Warn`——這不是危險操作），呼叫 `ctrl.clear_axis_calib(ax)`（`ds102_ctrl.py`，`pop` 該軸的 key＋`_persist_axis_calib()`，沿用既有拒寫保護）。這次**比照** Teaching Point／Profile 刪除的既有規格：按下去先跳確認視窗列出目前數值（`icon="warning"`／`default="no"`），該軸本來就沒設定時完全不彈視窗。與「套用」按鈕的差異：套用是新增/更新資料沒有安全含意不需要確認，清除是移除使用者已經輸入過的資料，跟刪教點/刪行程同一類「使用者花時間輸入的東西，删掉要有一道防線」，兩者的確認需求判斷依據不同，不要因為都是 axis_calib 卡片上的按鈕就套同一套規則。
- **驗證過的手算範例**：KHE06008-C 規格（導程 1mm、步進角 0.72、division=1）代入 `estimate_um("X", 500)` = `500 × (1×1000)/((360/0.72)×1)` = `500 × 2.0` = `1000.0` μm，與官網「Full-step 2μm/Pulse」吻合。
- **存檔時的 μm 快照（2026-08-19，只做兩處，architect 評估後刻意限縮範圍）**：`ctrl.save_point()`（Teaching Point）與 `fiber_scanner.py` 的 `_measure_here()`（尋光樣本）在存檔當下，**若某軸有校正參數，額外記錄一份 μm 估算值＋當下的校正參數快照**（`positions_um`/`axis_calib_snapshot`，尋光樣本對應 `Sample.coords_um`/`Sample.calib_snapshot`）——理由：校正參數之後可能被使用者改掉或用 `clear_axis_calib()` 清除，沒有快照的話舊紀錄的 μm 數字之後無法驗證是怎麼算出來的。存的是**完整校正參數**（含 `lead_pitch_mm`/`step_angle_deg`/`division`），不是只存算出來的 μm，這樣公式或參數輸入錯誤時還能回頭重新驗算。
  - **只有「至少一軸有校正參數」時才加這些新欄位**——完全沒用這個功能的使用者，存檔內容跟改動前逐位元組相同，不會憑空多出空欄位。`Sample` 的兩個新欄位沒有校正資料時是 `None`（不是空字典 `{}`），跟 `estimate_um()` 回傳 `None` 的既有慣例一致。
  - **純附加、不影響任何既有讀取邏輯**：`goto_point()` 只讀 `positions_pulse`，尋光演算法只讀 `Sample.coords`（pulse），新欄位不會被任何判斷路徑碰到。舊格式檔案（沒有這兩個新欄位）可以正常讀取，`load_points()` 是整份 `json.load()`，缺欄位不會出錯。
  - **明確排除的地方**：行程錄製（`recordings/*.json`）——內容是原始序列指令字串不是結構化座標，且 CLAUDE.md 已記載「重播不是安全功能，連 pulse 本身都不保證精確」，附加 μm 只會讓一個已知不精確的數字看起來更可信。實驗數據 CSV 與 `controller_config.json` 也不在這次範圍——前者優先度低（沒有證據顯示目前有人在用），後者性質是「目前韌體狀態」不是「歷史紀錄」，加快照沒有對應的使用情境。
- `positions` property 回傳的是**工作座標 = 機械位置 − offset**。要機械座標請直接讀 `_positions_pulse`（記得取鎖）。
- 座標系分工：**Teaching Point 存工作座標**（`goto_point` 會自行加回 offset），而 `_positions_pulse` 與 `sw_limits` 是**機械座標**。跨這條界線比較數值前先確認在同一個座標系。
- ⚠ 但這只是約定、沒有強制：`save_point()` **完全不減 offset**，它照收 GUI 傳進來的數字。只有走「填入目前座標」按鈕（`_do_fill_current`，用的是 `ctrl.positions`）才保證存進去的是工作座標。設過 offset 之後手打一組數字存檔，goto 時會被**多加一次 offset**。
- 軟體限位 `sw_limits` 以 pulse 比對，於 `move_step` / `goto_point` 送出指令**之前**攔截。
- ⚠ GUI 的「套用限制設定」（`_apply_sw_limits`）**只寫 Python 端的 `self.ctrl.sw_limits` 字典，不會送 `CWSLP`/`CCWSLP` 給控制器**。韌體端的限位是另一套、要另外設（見下方〈硬體連不上時〉）。兩者互不同步，排查限位行為時先確認在講哪一層。
- ⚠ 而且 `_apply_sw_limits` **會靜默清空限位**：它每次遍歷全部六軸，輸入框留空或格式錯（例如打成 `10,613` 帶逗號）一律 `except ValueError: → None`＝無限制，然後照樣跳出「軟體行程限制已套用」的成功對話框。輸入框開機是空的，所以「只填 X 軸就按套用」會把 Y/Z 既有的限制一併歸零。
- ⚠ 軟體限位比對用的是**快取座標**：`move_step` / `goto_point` 取的 `_positions_pulse` 只由 position worker 每 0.5s 刷新一次。剛做完長按點動就按步進，比對基準可能落後半秒的行程。

### 原點復歸（`origin_all` / `_wait_origin_done`）

「全軸原點復歸」不是單純對每軸送 `GO ORG`，中間有三個必要條件，改動時別拆掉：

- **等待要用 `_wait_origin_done()`，不能沿用 `_wait_axis_stop()`**。復歸樣式 5/6 本來就靠偵測限位感測器邊緣定位，途中壓到限位是正常流程；`_wait_axis_stop` 會把 limit 當失敗回傳。逾時也不同：復歸可能橫跨整個行程，給 180s。
- **復歸期間必須暫時停用控制器軟體限位**，結束後（`finally`）還原。原點通常落在行程末端（POS≈0），在軟限位之外，不關掉會被自己設的保護擋住。
- 🔴 **`GO ORG` 完成後 POS 不會停在 0——「歸位後 0 點不固定」的實測數據。**
  2026-08-05 於 COM2 量測：各軸離開原點 800 pulse 後單獨送 `GO ORG`，重複三輪，殘差為

  | 軸 | 三輪殘差 (pulse) | 特性 |
  |---|---|---|
  | X | 1, 0, 1 | ≈0～1 |
  | Y | 7, 8, 7 | 系統性偏 **+7～8** |
  | Z | −7, −6, −8 | 系統性偏 **−6～−8** |

  也就是**每軸有各自固定的偏移量，再疊加 ±1～2 pulse 的機械重現性**。`MEMSW7?` 讀回是 `0` 也一樣會發生，所以別把 MEMSW7 當成歸零的保證。

  因此 `origin_all` 與 `move_origin` 都在復歸後檢查，非 0 就**強制送 `POS 0`**（摘要標記 `(POS=0 強制)`）。這讓軟體座標原點每次一致，殘留的不確定性降到機械重現性本身的 ±1～2 pulse——那是硬體下限，改程式消不掉。

`MEMSW0?` 回 `0`（樣式 Type0＝不執行）與 `Stage not connected` 的軸會被略過；單軸失敗不中止整批。

🔴 **`MEMSW` 是 RAM-only，控制器斷電後整組（MEMSW0～7、全軸）回到 0**——與韌體軟體限位同一個性質，2026-08-05 實機踩到。而 `MEMSW0=0` 的語意是「復歸樣式 Type0＝不執行」，所以**斷電後按「全軸原點復歸」會把每一軸都合法略過、幾秒跑完**。舊版在這情境下回傳 `True`，使用者看到成功訊息但滑台根本沒動——現在改成：一軸都沒真的復歸就回傳 `False` 並說明原因。

**解法是 `recordings/controller_config.json`**（見下方〈控制器設定持久化〉）：連線時自動把存檔的 MEMSW0 補回控制器。補不齊的部分再由 `check_homing_config()` 用 LOG 與橫幅提醒（未接滑台的軸會排除）。

各軸的復歸樣式：**X=2、Y=1、Z=2、U=0**（U 未接滑台）。

⚠ 但「未接滑台會被略過」**不是無條件成立**：它靠 `query_status` 回傳 `"Stage not connected"`，而該字串只在 `SB1 & 0x06`（有限位位元）成立時才有機會產生。未接滑台的軸當下若沒觸發限位位元，`query_status` 會回 `"Stop"`，該軸不會被略過，而是照送 `GO ORG` 然後等到 180s 逾時。

### 🔴 安全性相關的既有行為（2026-08-05 大修，改動前先讀）

這一區的每一條都是實際查證過的，不是推測。**已修的部分請勿改回原樣**——原寫法的共同症狀是「出事時畫面與 LOG 都不會有任何警告」。

#### 已修（2026-08-05，模擬 16/16 + **實機 COM2 驗證通過**）

實機驗證方式：用**韌體軟體限位**觸發「撞限位」情境（電子式停止、無機械撞擊），走的是與機械限位完全相同的程式路徑。實測 X 從 252 走到軟限位 552 被擋停 → `move_step` 回 `False` → `goto_point` 中止 → **Z 軸完全沒動**，警報正常發出。重播點動實測 **4.6 秒**（修正前會是 30 秒逾時）。

- **撞限位不再靜默。** `_wait_axis_stop()` 的警報與 WARN log 已恢復（原本整段被註解掉）；`move_step()` 現在**回傳 bool**（False = 被攔截／逾時／撞限位），`goto_point()` 會據此**中止後續軸**。以前 teaching point 超出行程會演變成三軸接連撞端點且零警告。新增呼叫 `move_step` 的程式碼**務必檢查回傳值**。
- **`origin_all` 的 `finally` 不再 fail-unsafe。** 舊寫法 `CWSLE {cw or '0'}` 在查詢逾時（`_serial_write_read` 回 `""`）時會把韌體限位**停用**。現在讀不到原值一律還原成 `1`（啟用）並記 ERROR。保護的失效方向必須是「寧可多擋」。
- **重播不再把點動開到撞限位。** 舊判斷式 `"GO" in tx` 會把 `GO CWJ`（連續點動，沒有終點）也納入等待，導致 `_wait_axis_stop` 阻塞最長 30 秒、軸一路跑到硬體限位才停，`STOP 0` 遲遲送不出去。現在改用 `_is_finite_move()`，只有真正有終點的指令才等；且任一步未正常到位就中止重播。
- **「中斷連線」按鈕會先送 STOP** 再 `disconnect()`（`_on_close()` 本來就有做，中斷按鈕漏了）。
- **`_apply_sw_limits` 不再靜默清空。** 格式錯（例如 `10,613` 帶逗號）會跳錯誤視窗而非默默當成「無限制」還顯示成功；把既有限制清空時會明確警告。
- **`speed_profiles.json` 補上拒寫保護**，與 teaching points 一致（`_profiles_loaded`）。
- `_watch_jog_limit` 解析失敗時會照睡一輪，不再忙迴圈灌爆序列埠。

#### 第二批修正（2026-08-05，使用者回報的 7 項，**實機 COM2 驗證通過**）

- 🔴 **`stop()` 不再無限等 `_serial_lock`。** 這是「長按點動放開會卡死」的根因：position worker 卡在 `read_until` 逾時裡時（最壞 2s × 3 retry ≈ 6s），舊寫法會傻等整段時間，UI 凍結而滑台持續前進。現在只等 `STOP_LOCK_TIMEOUT`(0.15s)，取不到鎖就**插隊直接寫入**——STOP 唯寫不讀，最壞只打斷別人一次查詢，對方本來就有重送機制。實測在背景刷新競爭下反應時間 0.3～33ms。**別把它改回 `_serial_write()`。**
- **限位提醒改為非強制橫幅**（`_flash_banner`）。`_show_alarm` 不再呼叫 `ctrl.stop()`（`STOP 0` 是停**全部**軸，但觸發限位的只有一軸，沒有理由連坐；該軸早已被控制器擋停），也不再用 modal `messagebox` 逼使用者按確認。
- **錄製步驟的 delay 修改會寫回 json**（`save_recording()`）。以前只改記憶體，重開程式就變回原值。
- **重播的「每輪之間間隔」可設定**（`play_recording(cycle_delay_ms=...)`，GUI 預設 3000ms）。⚠ 這是**跑完一次完整行程後、下一輪開始前**的等待，**不是**步與步之間的延遲（那是各步驟自己的 `delay_ms`，在「已錄製步驟」雙擊修改並存檔）。以前這個間隔是寫死的 `time.sleep(3)`，而且連第一輪之前都會等、畫面上毫無提示，看起來像沒反應；現在只在輪與輪之間等、可中止、並回報進度。
- **復歸後一律確認並強制歸零。** 實測 `GO ORG` 會留下每軸各自的殘差（X≈0～1、Y≈+7～8、Z≈−6～−8 pulse，見上方〈原點復歸〉的量測表），`MEMSW7=0` 也一樣。所以 `origin_all` 與 `move_origin` 都改成復歸後檢查、非 0 就強制寫 `POS 0`。這是「歸位後 0 點不固定」的真正解法。
- **`move_origin`（單軸）改用 `_wait_origin_done`**，不再誤用 `_wait_axis_stop`（後者把復歸途中壓限位當失敗，逾時 30s 對全行程復歸也不夠），並補上回傳值。
- **`origin_all` 一軸都沒復歸時回傳 `False`**，不再在「全部被略過」時回報成功。
- **連線時 `check_homing_config()`** 檢查 MEMSW0 並警告（不自動寫入）。
- `_start_poller` 從 10ms 放寬到 100ms，`action_history` 與 GUI LOG 文字框都加了上限（見上方〈LOG 量的控制〉）。這幾項合起來是「偶發當機」的主要來源。

#### 第三批修正（2026-08-06，ui-designer 檢討出的介面安全缺陷，**實機驗證通過**）

- 🔴 **停止鍵不可放進 `_drive_buttons`。** 那串會被 `_set_drive_buttons_state("disabled")` 整批關掉，時機包含**重播中與全軸復歸中**——正是滑台在動、最需要停止的時候。現在停止鍵由 `_sync_stop_button()` 單獨管理，只在未連線時 disable。
- 🔴 **「停止重播」會真的送出 STOP。** 舊寫法只 `self._stop_playback.set()`，而 `play_recording` 收到旗標直接 `return` 不送停止指令；若中斷的那步是 `GO CWJ`（無終點），收尾的 `STOP 0` 就永遠送不出去，該軸一路跑到硬體限位。現在 GUI 端先送 STOP，`play_recording` 的 `finally` 也會在**未正常跑完**時補送（用 `completed` 旗標區分）。
- 🔴 **`_update_stat_ui` 每輪都會重設按鈕狀態**，以前不認得「復歸中」，導致 `_do_home_all` 剛鎖上的按鈕在 100ms 後全部復活（含那顆文字停在「復歸中…」的按鈕，再按就疊出第二條執行緒）。現在多一個 `self._homing` 事件。**任何新增的「作業進行中」狀態都必須同步加進這個判斷。**
- 🔴 **`ORG_MODES` 從 `ORG 1` 開始，不再提供 `ORG 0`。** `move_origin()` 第一道指令就是 `MEMSW0 {type}`，選 ORG 0 等於把該軸復歸樣式寫成 Type0＝不執行——空等 180s 逾時、樣式被永久覆蓋、還會把 `controller_config.json` 剛還原的設定當場毀掉。而舊的預設值正是 `ORG 0`。
  - ⚠ 連帶：**不可用 `ORG_MODES.index(...)` 換算樣式編號**，清單從 1 開始會差一位。用 `_selected_org_type()` 解析字串。
  - 下拉預設值改為連線／切軸時由 `_sync_org_mode()` 從控制器讀 `MEMSW0?` 填入；讀不到就留空，`_do_origin_move()` 會拒絕執行而不是猜一個值送出去。

#### 第四批修正（2026-08-06，ui-designer 檢討的 S2/S3/S4 共 18 項）

- **通訊失聯會反映在畫面。** `refresh_positions()` 維護 `comm_failures` / `last_position_ok`，連續 `COMM_FAIL_THRESHOLD`(3) 輪讀不到就 `comm_stale=True`：座標轉紅、指示燈轉紅、StatusBar 標「已停止更新」、跳橫幅。以前 USB 被拔掉時 `connected` 仍是 True、指示燈仍是綠的、座標**停在最後一次成功的值不動**。
- **未連線一律顯示 `—`，絕不顯示 0。** 0 幾乎就落在限位開關上，顯示 0 等於畫一組「所有軸都壓在端點」的假座標。
- **當前軸在五個分頁都看得到**（StatusBar 的該軸格子改 `CLR_ACCENT` 底色）。軸選擇器只存在於兩個分頁，而選錯軸就是驅動錯的滑台。驅動卡也改成顯示軸名（`X`）而非軸號（`1`）。
- **橫幅常駐 pack、高度固定**，閒置時只是變回背景色。以前有訊息才 pack、逾時 `pack_forget`，整個 notebook 會上下跳 30px——而觸發橫幅最頻繁的情境正是「長按點動撞限位」，按鈕在手指下方位移。
- **橫幅訊息不再互相覆蓋**：`BANNER_COALESCE_SEC`(1s) 內湧入的排隊依序顯示（連線時「已還原」與「樣式未設定」兩則就是這個情境），超過就直接換掉（那多半是使用者剛按按鈕的回饋，不該等）。
- **`_apply_global_delay` 先檢查再動畫面。** 舊寫法先把 tree 每列的延遲改掉才發現沒選行程 → 警告 return，結果「檔案沒寫、記憶體沒改，但螢幕滿屏新值」。
- **`_apply_sw_limits` 改為事前確認**，並新增「目前生效」欄顯示 `ctrl.sw_limits` 實際內容（輸入框留白有「沒設限制」與「有設但沒顯示」兩種無法分辨的含意）。格式錯時**整批不套用**。
- **Position 顯示與輸入拆開。** 以前共用 `_ctrl_pos_var`，輪詢每 100ms 覆寫它，使用者打到一半的數字會被清掉；改寫座標現在要確認（它會讓所有限位比對與教點基準跟著偏移）。
- **刪除確認規格統一**：Teaching Point 與 Profile 比照「刪除行程」——列出內容、`icon="warning"`、`default="no"`。
- **重播加確認視窗**（列出步數／輪數／預估時間／⚠ 不檢查軟體限位），改 `Warn.TButton`，重播中 disable 自己並啟用停止鍵。以前可連按疊出第二條執行緒。
- **`_rec_tree` 改用 iid（行程名稱）**，不再用 `.index()` 假設顯示順序與記憶體順序一致。
- ~~恢復「模擬模式」按鈕~~ → **後續依使用者要求整個移除模擬模式**（見上方〈常用指令〉的紅字）。
- **`Esc` = 停止所有軸**（`bind_all`，任何分頁都有效）。緊急停止**刻意不綁鍵盤**——誤觸後要走解除流程並確認各軸位置。
- **LOG 加層級篩選**（全部／WARN 以上／只看 ERROR）。
- **儀表板每軸狀態小字接上資料**（`_dash_status_vars` 以前建立後全檔沒人更新，永遠是「—」）。
- **版面重分配**：速度設定從儀表板搬到**移動控制、緊接驅動按鈕下方**（調速是反覆試出來的，以前每次都要來回切兩次分頁）；軟體限位與控制器設定也搬過去、用分隔線隔在底部。行程錄製分頁改依操作動線排序：錄製 → 已儲存行程 → 步驟明細 → 重播設定（以前「步驟明細」排在它的資料來源上方）。
- `_append_log_ui` 不再每筆都呼叫 `_update_stat_ui()`（`_start_poller` 已經在做）。

#### 仍然存在（尚未修，動到時要知道）

1. **長按點動的 Python 端保護預設不生效**——見上方〈長按點動的限位保護〉。`sw_limits` 初始全 `None`，沒有任何程式會從控制器讀 `CWSLP`/`CCWSLP` 回填。修法是連線時讀回來填進去，但需要實機驗證。
2. **`play_recording` 仍繞過 `_check_sw_limit` 與 `PULS` 取整數正規化**（它直接重送原始 `tx`）。而且錄製時每步 delay 是**寫死 800ms**、不是實際按住的時間，所以重播**無法還原點動的行程長度**（實測：錄製走 277 pulse，重播走 1046 pulse）。別把錄製重播當成安全功能。
3. **`_toggle_connect` 仍在 Tk 主執行緒做阻塞式序列 I/O**（`stop()` 現在有界了，但 `disconnect()` 與連線流程沒有）。

### 控制器設定持久化（`controller_config.json`）

DS102 的 **MEMSW（復歸樣式）與韌體軟體限位都是 RAM-only**，控制器一斷電就整組回到出廠值。`recordings/controller_config.json` 存的就是這些值，讓它們能在下次連線時自動補回：

| 方法 | 作用 |
|---|---|
| `capture_controller_config()` | 讀出控制器現況並存檔（GUI：「儲存控制器設定」） |
| `load_controller_config()` | 載入設定檔 |
| `restore_controller_config()` | 把設定檔的值補回控制器，回傳實際還原了哪些 |

`connect()` 會依序做 load → restore → `check_homing_config()`，所以正常使用不必手動操作；GUI 另有「立即還原」按鈕。

還原邏輯刻意**只補「看起來被清空」的項目，不覆蓋使用者刻意改過的值**：

- MEMSW0：設定檔有非 0 值、而控制器現在是 `0` → 才寫回
- 軟體限位：設定檔記錄為啟用(`1`)、而控制器現在是停用(`0`) → 才寫座標並啟用
- 未接滑台的軸一律跳過（所以 U 軸不會被寫入）

⚠ `capture_controller_config()` 是**與磁碟合併**而非整份覆蓋——若某次連線只認到部分軸（通訊異常、或換了另一台控制器），直接覆蓋會清掉其餘軸的設定，與 teaching points 踩過的坑同一類。

⚠ 新增任何放在 `RECORDING_DIR` 的設定檔，**務必同步加進 `NON_RECORDING_JSON`**，否則 `load_recordings_from_disk()` 會把它當成「沒有 steps 的行程」載進清單。

### 行程的儲存與刪除

- `save_recording(rec)` — 把單一行程寫回自己的 json（走 `_write_json_with_backup`）。任何修改 `rec` 內容的地方都要呼叫它，否則改動只存在記憶體，重開就沒了。
- `delete_recording(name)` — 從記憶體清單移除，並把 json **改名成 `.bak`** 而非真的刪除。這個專案已因「整份覆蓋」弄丟過兩次 teaching points，行程是使用者花時間錄出來的，留一份可救回的副本成本很低。`.bak` 不符合 `*.json` 的 glob，不會再被載入清單。錄製中／重播中會拒絕刪除。

### 設定檔持久化（改這裡前務必先讀）

`teaching_points.json` / `speed_profiles.json` 是**把整個記憶體字典整份寫回**，所以任何沒先 `load_*()` 就呼叫 `save`/`delete` 的路徑都會清空既有內容——實際發生過兩次，都是測試腳本直接 `DS102Controller()` 就存檔。現有兩層防護：

- `_write_json_with_backup()`：寫入前留 `.bak`（且不拿空內容蓋掉有內容的備份），再「先寫 `.tmp` 後 `replace`」避免寫到一半壞檔。
- `_points_loaded` 旗標：沒 load 過就要寫回時，比對磁碟上是否有記憶體裡沒有的鍵，有就**拒絕寫入並記 ERROR**。

**這兩層原本不對稱**（`_points_loaded` 曾經只有 teaching points 有），2026-08-05 已補上 `_profiles_loaded` 讓 `_persist_profiles()` 比照同一套邏輯（見下方〈已修〉）。`meter_config.json` / `scanner_config.json` 刻意**不**走這層保護——那兩份是純量欄位，整份覆寫本來就是正確行為，不是「累積型集合被空狀態蓋掉」的風險場景（見上方〈光功率／尋光分頁〉）。

⚠ `_write_json_with_backup()` 的 `write_text` / `replace` **沒有 try/except**。磁碟滿或檔案被防毒鎖住時例外會往上拋進 Tk callback 變成 traceback，`.tmp` 殘留在 `recordings/`。它防的是「寫到一半壞檔」，不是「萬無一失」。

新增任何設定檔一律走 `_write_json_with_backup()`，別自己 `json.dump` 到目標路徑。

### UI 節奏／色票外部化（`app_settings.json`，2026-08-17）

跟前面幾份設定檔性質不同：`recordings/app_settings.json` 是**給維護人員手動編輯的靜態設定，程式只讀不寫**，沒有對應的 `_save_app_settings()`——沒有任何執行路徑會把值寫回檔案，所以不需要 `_points_loaded` 那類拒寫保護。

⚠ **2026-08-17 拆分 `ds102_ctrl.py` 後，`_load_app_settings()`／`_app_setting_num()`／模組層級的 `_app_settings` 實際定義都搬到了 `ds102_ctrl.py`**（原因：`HISTORY_MAX` 是 `DS102Controller` 用到的常數，依賴這三者才能算出值，為了不讓 `ds102_ctrl.py` 反過來 import main_ai.py，整組一起搬）。main_ai.py 用 `from ds102_ctrl import _app_settings, _app_setting_num` 重新引入，所以下面提到的 `CLR_*`／`POSITION_POLL_INTERVAL`／`UI_REDRAW_INTERVAL` 等 A 類常數在 main_ai.py 端呼叫 `_app_setting_num(...)`/`_app_settings.get(...)` 時行為不變，只是這兩個函式本體不在同一個檔案裡了。

- 涵蓋範圍**只有 A 類（UI 節奏／顯示上限／色票）**：`POSITION_POLL_INTERVAL`／`UI_REDRAW_INTERVAL`／`LOG_TEXT_MAX_LINES`／`HISTORY_MAX`／`SCAN_PLOT_REDRAW_INTERVAL`／`METER_POLL_INTERVAL`／`BANNER_COALESCE_SEC`，以及十個 `CLR_*` 色票。這些改壞最多是介面變慢/變醜，不會讓滑台做出危險動作。
- 🔴 **`WAIT_TIMEOUT`／`STOP_LOCK_TIMEOUT`／`COMM_FAIL_THRESHOLD`／`JOG_WATCH_INTERVAL`／`MAX_RETRY`／`WAIT_INTERVAL` 這類有安全含意的時序常數，以及 `AXES`／`AXIS_NO`／`ORG_MODES`／`_POLL_QUERIES` 這類綁定韌體指令協定的常數，刻意不外部化**，仍然寫死在程式碼裡。前者要改（未來的規劃）必須先做範圍夾限＋不合法退回內建預設值；後者改壞的後果是指令送到錯的軸，不該讓維護人員能繞過「改指令要回頭比對 main.py」這道審查關卡。
- `_load_app_settings()` 在**模組載入時**（任何 class 定義之前）就執行，此時 `logger` 還沒被 `init_runtime()` 掛上 `FileHandler`（那要等 `main()` 呼叫 `init_runtime()`），所以載入這一刻的 log 不一定落地到 `logs/*.log`；但它只做 `p.exists()`/`read_text()`，不建立任何目錄或檔案，不違反「import main_ai 不會建立任何目錄或 log 檔」這條既有保證。
- 數值欄位透過 `_app_setting_num(settings, key, default, cast)` 讀取：型別不對（例如維護人員把數字打成字串）就個別退回預設值並記 INFO，不影響其餘欄位、不中止載入。色票欄位是字串，直接 `.get(key, 預設色碼)`，不做色碼格式驗證——格式錯的後果跟硬編碼時期手誤打錯字一樣，會在 tkinter 建元件時才報錯。
- 已加進 `NON_RECORDING_JSON`，不會被 `load_recordings_from_disk()` 誤當成行程檔。

### B 類安全常數外部化（`safety_settings.json`，2026-08-18）

跟 A 類**刻意分成獨立檔案、獨立機制**，不共用 `app_settings.json`／`_load_app_settings`／`_app_setting_num`。理由：`recordings/` 整個目錄不進版控、沒有 PR review 這道關卡，把安全常數跟色票放同一份檔案會讓人誤以為兩者風險等級相同——這六顆常數牽涉「撞限位要多久才停下來」這類安全行為，不是「介面變慢變醜」等級。

- **涵蓋範圍**：`MAX_RETRY`（1–5）／`WAIT_TIMEOUT`（10.0–120.0s）／`WAIT_INTERVAL`（0.1–2.0s）／`JOG_WATCH_INTERVAL`（0.03–1.0s，範圍刻意收得比其他顆窄——它雖然有 `_watch_jog_limit()` 的自我修正機制，但那**只保護前瞻量計算**，不保護首輪猜測與通訊負載，見 CLAUDE.md〈長按點動的限位保護〉）／`STOP_LOCK_TIMEOUT`（0.0–0.5s，負值自然落在範圍外被拒絕，不需要特判——**不能** clamp 到 0，因為 `RLock.acquire(timeout=負值)` 語意是無限等待，clamp 會把一個危險輸入悄悄轉成看似合理的值）／`COMM_FAIL_THRESHOLD`（1–10）。全部定義在 [ds102_ctrl.py](ds102_ctrl.py)。
- **驗證比 A 類嚴格**：型別對了還要落在合法範圍內，`_safety_setting_num(settings, key, default, cast, min_val, max_val)`。**兩種拒絕情況（型別錯誤／範圍超出）一律退回內建預設值，不做 clamp 到邊界**——貼著邊界的值本身也未必是維護人員的本意。記 **`logger.warning(...)`**（比 A 類的 INFO 高一級），並把可讀訊息 append 進模組層級 `_safety_setting_rejections: list[str]`（型別錯誤與範圍超出的訊息文字刻意不同，方便分辨）。欄位缺漏是正常情況，靜默使用預設值，不記錄。
- 🔴 **`_safety_setting_rejections` 裡的訊息只進了 Python 內建 `logging`，不會進到 GUI 的 LOG 分頁**——驗證發生在模組載入時，比任何 `DS102Controller` 實例、比 `init_runtime()` 的 `FileHandler` 都早，跟 GUI LOG 分頁靠的 `ctrl._log()`/`action_history` 是兩條獨立路徑。`DS102GUI.__init__` 在 `self.ctrl = DS102Controller()` 之後、任何 widget 建立之前，會逐則呼叫 `self.ctrl._log("WARN", f"[安全設定] {msg}")` 補寫一次，讓「LOG 篩 WARN 能看到明細」這條路徑成立。**新增任何依賴 `_safety_setting_rejections` 的功能，記得它預設不在 GUI LOG 裡，需要類似的橋接。**
- **GUI 呈現（2026-08-18，ui-designer 設計）**：
  - 橫幅：程式啟動、視窗建好後立刻顯示一次，**不等連線**（這是本地設定檔驗證結果，跟有沒有連硬體無關；`check_homing_config()` 那類既有橫幅綁在連線流程是因為內容必須跟控制器對話才查得到，這裡不是同一種情境）。0 則不顯示；1 則直接顯示完整訊息；≥2 則彙整成一則摘要（`⚠ 安全設定檔有 N 項欄位超出合法範圍...`），不逐則排隊——避免六顆全錯時開機瞬間連續跳出六則橫幅。
  - 儀表板「系統狀態」列新增第 6 格「安全設定」：`內建預設` 或 `⚠ N 項已回退`。**這是靜態值，建立當下讀一次就好，刻意不掛進 `_update_stat_ui` 的 100ms 週期性重繪**（`_safety_setting_rejections` 程式生命週期內不會再變，掛進輪詢是純浪費）。
- **實測驗證**（三種情境，皆用假 `ctrl.ser=None` 建完整 GUI）：無設定檔（橫幅不顯示／儀表板「內建預設」／LOG 0 筆）、一項被拒絕（單則橫幅／「⚠ 1 項已回退」／LOG 1 筆）、三項被拒絕（彙整橫幅／「⚠ 3 項已回退」／LOG 3 筆且明細齊全）——三種情境橫幅文字、儀表板文字、LOG 筆數皆與規格逐字相符。
- 已加進 `NON_RECORDING_JSON`。`verify_scan_tab.py`／`verify_meter_panel.py` 兩支回歸測試在這輪改動後重跑仍全數通過。

## DS102 通訊協定重點

**權威參考:`ds102 (2).pdf`**(170 頁,DS102/DS112 Operation Manual Ver 2.00,倉庫根目錄)。查指令前先翻它,不要靠猜——實測發現韌體對不存在的指令會**回傳看似合理的值**(例如 `LIMIT?` 回 `2`、`SOFTLIMIT?` 回 `0`),但這些都不在手冊的 Inquiry Command 表裡。第 4.3.4 節之後是完整指令表,查詢指令集中在 `＜Inquiry Command＞`(約 p.132)。

常用查詢(手冊縮寫,大寫字母不可省):

| 指令 | 用途 |
|---|---|
| `AXI{n}:CWSLP?` / `CCWSLP?` | 軟體限位**座標** |
| `AXI{n}:CWSLE?` / `CCWSLE?` | 軟體限位啟用(0=停用) |
| `AXI{n}:RESOLUT?` | 1 pulse 的距離 = `STANDARD?` ÷ 分割數。手冊第 94 頁〈Unit Set〉證實 `RESOLUT?`＝`1` 的原因：真正代表機械行程的是 `SD`（馬達整步時的機械位移量，螺桿導程相關），要透過 DT100 手持終端機或控制軟體手動輸入，這台機器從未設定過——不是控制器的 bug，是沒人填過這個值 |
| `AXI{n}:DRDIV?` | 驅動器分割(0=full step…15=1/250)。官方指令名 `:DRiverDIVision?`/`:DRDIV?`，查證來源 `ds102 (2).pdf` 第 131 頁〈Inquiry Command〉表。🔴 **對這台滑台裝的 AMS（微步進）型驅動器沒有意義，`:DRDIV` 指令對它完全不生效**——手冊第 73-75 頁〈3.5 Driver division number setting〉明講：Normal 型驅動器才能用手持終端機／軟體／通訊指令切換 FULL/Half；**Micro step 型驅動器要打開外殼、用螺絲起子調驅動器上的實體旋轉開關（DATA1）**，控制器沒有電路能讀回這顆開關的實際位置。2026-08-18 實機驗證：使用者把實體開關轉到 6，`DRDIV?` 依然回 `0`——因為查詢到的只是控制器內部一個獨立的軟體暫存器（預設 `0`），跟實體開關完全沒有連動，這是驅動器硬體設計本身如此，不是查詢邏輯錯誤。實體開關（DATA1）與軟體 `DRDIV?`／`:DRDIV` 是**同一套 0～F(15) 編號、對照表完全一致**（第 75 頁表格逐列以「步進角 = 0.72°÷分割數」驗算過，例如 `6=1/10`：0.72÷10=0.072° 吻合）：`0=1/1(Full) 1=1/2 2=1/2.5 3=1/4 4=1/5 5=1/8 6=1/10 7=1/20 8=1/25 9=1/40 A=1/50 B=1/80 C=1/100 D=1/125 E=1/200 F=1/250`（⚠ 這張表第一版用 `pdftotext -layout` 擷取時欄位對錯位，誤植成「差一位」，後來改用 `pdftotext -table` 重新擷取並逐列驗算才發現，查 PDF 表格前**兩種擷取模式都跑一次交叉比對比較保險**）。`connect()` 會查一次存進 `ctrl.axis_drdiv: Dict[str, str]`（GUI 頂部與 LOG 顯示），**這顆值目前對這台機器而言只是「軟體暫存器內容」，不代表實際細分設定**，pulse→um 換算不能拿它當依據——真的要換算請用〈軸機械校正參數〉那組使用者手動輸入的 `axis_calib.division`，見下方〈單位與座標〉一節 |
| `AXI{n}:PULSA?` / `HOMEP?` | 絕對驅動座標 / Home 座標 |
| `TCH00?`～`TCH63?` | 控制器**內建 64 組 teaching point** |

**機械限位(實體開關)的座標無法查詢**,手冊沒有這種指令;只能開到限位再讀 `POS?`。而且 `POS` 是相對暫存器,原點復歸會重設,所以穩定的量是兩端之差(行程)而非絕對值。

🟡 **待查事項（2026-08-18，尚未有結論）**：使用者實測「固定 pulse 數移動同一軸，把驅動器 DATA1 從 Full-step 轉到 1/10 並重開機，實際移動距離感覺沒有變少」——照公式與手冊資料，division 加大應該讓同樣 pulse 數走的距離等比例變短（見上方〈軸機械校正參數〉）。已排除「開關沒生效」（有重開機）。手冊 3.5.2 節提到 DATA1 是否生效還要看另一顆「division changing-over switch」是否撥在 R1（預設值，撥 R2 則改聽 DATA2），但使用者拆殼後**只看到 DATA1，沒看到第二顆開關**——這顆開關的圖示在 PDF 裡是圖片，文字擷取工具讀不到，無法進一步比對。目前請使用者改測更極端的對比（DATA1: 0 vs F，步進角相差 250 倍）以確認 DATA1 本身到底有沒有在生效，結果尚未回報。**有結論後補寫在這裡，取代這整段待查事項。**

- ASCII 指令，**結尾必須是 `\r`**（`_serial_write` 統一補上）。鮑率預設 38400，`probe_ds102.py` / `test.py` 依序試 38400 → 19200 → 9600 → 4800。
- 送出前 `reset_input_buffer()` 清殘留，失敗最多重送 `MAX_RETRY`(3) 次。
- **不要照抄 main.py 在 write 與 read 之間的 `time.sleep(0.1)`**。實測（2026-07-31，40 次 `POS?`）現行寫法 40/40 一次就成功、平均 56 ms/次；加上該延遲後同樣 40/40，但變成 101 ms/次。那個延遲在此機器上純屬浪費。
- **本程式實際送出的指令只有這四種**（與 main.py 逐字一致，經比對確認）：
  ```
  連續點動  AXI{n}:L0 {l}:R0 {r}:S0 {s}:F0 {f}:GO CWJ|CCWJ   → 放開按鈕送 STOP 0
  步進      AXI{n}:L0 {l}:R0 {r}:S0 {s}:F0 {f}:PULS {p}:GO CW|CCW
  原點復歸  AXI{n}:MEMSW0 {type} 之後 AXI{n}:...:GO ORG
  停止      STOP 0
  ```
  注意參數順序：欄位序是 `L0 R0 S0 F0`，但 Python 函式簽章是 `(l_speed, f_speed, rate, s_rate)`——兩者不同序，f-string 內是交叉對應的，接錯不會報錯只會跑錯速度。
- **沒有用絕對移動**。`goto_point()` 是算出 `target − current` 的 delta 後拆成一連串**相對**的 `move_step()` CW/CCW 呼叫，不是送 `GO ABS`。`GO ABS` / `GOABS` / `GO HOME` / `GOTCH` / `HOMEP` 全都**不存在於 main.py 與 main_ai.py**（僅 `_sim_parse` 留了一段 `GO ABS` 的 regex，實際上永遠不會被觸發）。要改用絕對移動是可行的方向，但那是新增功能而非照抄現有寫法。
- 手冊上的完整能力（目前未使用，要用時先翻 PDF 確認）：`GO` 參數為 `0/CW`、`1/CCW`、`2/ORG`、`3/HOME`、`4/ABS`、`5/CWJ`、`6/CCWJ`。**`GO ORG` 與 `GO HOME` 是兩回事**：ORG 用感測器找機械原點(依 `MEMSW0` 的 13 種樣式)；HOME 只是走到 `HOMEP` 這個座標值。另有 `GOTCH {0-63}` 可驅動到控制器內建的 teaching point。
- **`PULS` 不接受帶小數點的值,而且失敗時完全靜默**。實測(2026-07-31)`PULS 500.0000` 位移 0 且不回報任何錯誤,`PULS 500` 正常走 500。程式會誤以為指令送出成功。`move_step` 已統一在送出前正規化(pulse 模式取整數、um/mm 去尾隨零)——**任何新增的指令組法都要照做**,否則會出現「按了沒反應但 log 顯示成功」。
- 狀態查詢採三段式（`query_status()`，對應 main.py `update_status()`）：
  - `AXI{n}:SB3?` bit0 = 該軸是否可選取，否則視為 Stop
  - `AXI{n}:SB1?` bit6 = Driving，bit4 = 原點偵測，bit1/bit2 = 觸發 limit
  - 觸發 limit 時再查 `AXI{n}:SB2?` 分辨 CW/CCW 硬體限位、CW/CCW 軟體限位、滑台未接
  - `AXI{n}:POS?` 取得目前位置
- **移動後不可立刻讀值**：`_wait_axis_stop()` 以 `WAIT_INTERVAL`(0.5s) 輪詢，逾時 `WAIT_TIMEOUT`(30s)；原點復歸改用 `_wait_origin_done()`（只看 Driving 位元，逾時 180s）。⚠ 注意 `_wait_axis_stop` **只有 `Driving` 會續輪**，任何其他狀態（Limit／通訊錯誤／軸無法選取）第一輪就 `return False`——搭配上面第 1 點（沒人看回傳值），實際語意是「撞限位＝立刻放棄等待且不通知任何人」。若之後要接光功率量測，停穩後還需再等約 30ms 讓機構震動衰減。
- `limit_direction()` 從狀態字串判斷壓在哪一側限位時，**必須先判斷 `"CCW"`**——`"CCW"` 字串本身就含有 `"CW"`，順序反了會把 CCW 限位全部誤判成 CW。任何新增的方向字串比對都有同一個陷阱。
- HP 8153A 側（[meter_GPIB.py](meter_GPIB.py)）：SCPI 指令結尾 `\n`（由 pyvisa `write_termination` 預設附加，不是手寫的）。⚠ 連續通訊之間需 `time.sleep(0.03~0.05)` 否則 GPIB 緩衝區溢位會出現 Query INTERRUPTED——但**目前 `meter_GPIB.py` 全檔沒有任何 `time.sleep`**（`import time` 是未使用的 import）。這是「整合時必須補上」的待辦，不是既有實作，別去該檔找對應程式碼。

## 執行期目錄與啟動流程（打包相關，改動前先讀）

- **三個資料目錄以「程式所在位置」為基準，不是 CWD。** `_app_dir()` 在打包後（`sys.frozen`）用 `sys.executable` 的目錄，否則用 `__file__` 的目錄。以前是 `Path("logs")` 這種相對路徑，直接跑 .py 看不出問題，但打包成 exe 後從開始功能表啟動（CWD 可能是 `C:\Windows\System32`）就會把教點與行程存到那裡。
- **`init_runtime()` 由 `main()` 呼叫，不在 import 時執行。** 建目錄與 `logging.FileHandler` 以前寫在模組層級，也就是在任何 GUI 之前；目錄不可寫時例外會在「還沒有視窗可以顯示錯誤」的階段拋出——windowed exe 的症狀就是**雙擊之後什麼都沒發生**。現在改為回傳 `(ok, err)`，`main()` 先開一個 withdrawn 的 root，失敗就用 `messagebox` 說明。
- ⚠ 因此 **import `main_ai` 不會建立任何目錄或 log 檔**。測試腳本要用 `RECORDING_DIR` 時自己指到暫存目錄即可，不必擔心污染。
- **`StreamHandler` 只在 `sys.stderr is not None` 時才加。** PyInstaller `--windowed` 會把 stdout/stderr 設成 `None`，而 `StreamHandler()` 預設綁 stderr——少了這道檢查，每一筆 log 的 `emit()` 都會踩 `AttributeError` 再被 logging 內部吞掉。
- `log_filename` 在 `init_runtime()` 失敗或未呼叫時是 `None`，`_on_close()` 會據此跳過歷程匯出。

🔴 **2026-08-17 已實際打包驗證過一次**（`venv/Scripts/pyinstaller.exe --onedir --windowed --name DS102 main_ai.py`）：build 乾淨完成（含 matplotlib TkAgg backend 自動偵測），產出約 152MB；雙擊產生的 `DS102.exe` 能正常啟動、存活、於自己目錄下建出 `logs/`/`recordings/`/`data/`（驗證了 `_app_dir()` 的 `sys.frozen` 分支），matplotlib 中文字型（Microsoft JhengHei）在打包環境下也能正確解析。**沒有實測連硬體**（GPIB／序列埠），那部分仍待驗證。

打包時另外要注意：
- 用 `--onedir` 而非 `--onefile`。這台機器的 SentinelOne 有前科（見 [DRIVER_ISSUE_REPORT.md](DRIVER_ISSUE_REPORT.md)），而未簽章的 onefile exe 自解壓縮到 temp 的行為特徵跟 packer 一樣，是典型的誤判目標；onedir 也省掉每次啟動的解壓時間。
- 別把 `ds102 (2).pdf`（4.4MB）與兩個驅動資料夾（6MB）`--add-data` 進去，執行期完全用不到。
- 全檔沒有動態 import（無 `importlib` / `__import__` / `exec`），hidden-import 風險低——若之後把 `DS102Controller` 拆成獨立模組（見上方〈main_ai.py 架構〉），只要新模組也維持靜態 `from x import y`（同目錄 sibling import，跟現有 `fiber_scanner.py`／`meter_GPIB.py` 的匯入方式一樣），PyInstaller 的預設分析會自動收進去，不需要額外宣告 hidden-import；真正該留意的是「有沒有新增動態 import」，不是模組數量變多本身。
- 沒有單一實例保護：兩個 exe 同時跑會搶同一個 COM 埠。
- ⚠ **打包會把 DEBUG 等級的 log 全寫進檔案**（含 matplotlib 首次建圖時的 `findfont` 字型掃描，單次啟動就能灌出數十萬行、逾 700KB），不是打包引入的問題（開發模式跑 `.py` 也一樣），但在只看 `logs/` 目錄大小時容易誤判成「這支程式在跑迴圈」。

## 執行期產出（皆已 gitignore）

- `logs/ds102_YYYYMMDD_HHMMSS.log` — 每次啟動一個檔（DEBUG 進檔案，INFO 以上進終端機）；關閉時另存 `*_history.txt`
- `recordings/*.json` — 錄製的行程；同目錄的 `teaching_points.json`、`speed_profiles.json`、`controller_config.json`、`meter_config.json`、`scanner_config.json` 是設定檔，載入錄製清單時由 `NON_RECORDING_JSON` 明確排除（另有自動產生的 `*.json.bak`）
- `data/data_*.csv` — 實驗數據（時間戳 + 各軸位置）
- 根目錄殘留的 `ds102_log_YYYYMMDD.log` 來自舊版 main.py / test.py 的 logging 設定
- 根目錄的 `output/`（auto-py-to-exe 的產出目錄）與 PyInstaller 的 `build/`/`dist/`/`*.spec` 皆已列入 `.gitignore`。

## 硬體連不上時

實機連線狀態（**2026-08-05 實測**）：**COM2 @ 38400**（7/31 時是 COM4，**埠號會變，別寫死**——靠 VID/PID `0DFD:0002` 認才可靠），`DS102VER?` 回應 `DS102 4.00`，`CONTA?` = **4 軸（X/Y/Z/U）**，但 **U 軸未接滑台**（`SB2 & 0x03 == 0x03`，且 `MEMSW0?` = 0），實際可動的是 X/Y/Z 三軸。注意韌體自報為 **DS102** 而非 DS112。

各軸復歸樣式（`MEMSW0?`）：X=2、Y=1、Z=2、U=0。全軸 `UNIT?`=0（pulse）、`RESOLUT?`=1。

### 實測行程（2026-07-31，連續點動到兩端限位）

| 軸 | CCW 端 | CW 端 | 行程 | 中點 |
|---|---:|---:|---:|---:|
| X | 2 | 10,615 | **10,613** | 5,308 |
| Y | −4,145 | 1 | **4,146** | −2,072 |
| Z | −5 | 10,573 | **10,578** | 5,284 |

單位 pulse。**這些是原點復歸後那一次座標系內的值**——`POS` 是相對暫存器，再次復歸會重設，屆時端點數值會變，但行程（差值）不變。

🔴 **2026-08-05 複測：韌體軟體限位目前全部停用、已回到出廠預設**（`CWSLE?`/`CCWSLE?` = `0`，`CWSLP?` = `99999999`、`CCWSLP?` = `-99999999`）。7/31 設定時沒送 `WRITE`，只存在 RAM，控制器斷電後就沒了——**這證實了下面那段的預測，也代表現在韌體端完全沒有保護**。要保護就得重設一次（`AXI{n}:CCWSLP/CWSLP` + `CCWSLE/CWSLE 1`），並且知道下次斷電還會再失效。這層保護比 `main_ai.py` 的 Python 端 `sw_limits` 可靠（Python 端只在送指令前用快取座標算一次，韌體端是實時的）。

`CWSLE?` / `CCWSLE?` 的回應是**純 `"0"` / `"1"`**（無前綴、無空白），`origin_all` 還原時的格式守衛據此判斷。

長按點動的 Python 端保護後來由 `_watch_jog_limit()` 補上（見上方架構段），但它是事後偵測、會滑一小段，仍以韌體端的限位為準。

⚠ **座標 0 幾乎就落在限位開關上**（X/Z 的 CCW 端、Y 的 CW 端都在 0 附近）。所以任何「移動到 0」的操作——`HOMEP` 預設值 0、座標全填 0 的 teaching point——實際語意都是「把該軸推去撞端點」。設計預設值時務必避開 0。

曾經卡了很久的 **SentinelOne Device Control 封鎖已於 2026-07-31 解除**（見 [DRIVER_ISSUE_REPORT.md](DRIVER_ISSUE_REPORT.md)）。若同樣症狀再現——裝置管理員 Problem Code 10 / `STATUS_DRIVER_BLOCKED`，看起來像簽章問題但不是——該文件已逐條排除驅動、簽章、WDAC、HVCI、Secure Boot，**不要重複排查這些**，直接請 IT 在 Device Control 政策核可 `VID_0DFD&PID_0002`。USB-RS232 轉接線會被同一政策攔下，不是可行替代方案。

搜埠邏輯（`probe_ds102.py` / `test.py`）：USB 直連時靠 VID/PID `0x0DFD/0x0002` 認；走 RS-232C 轉接線時 VID/PID 屬於轉接線，只能靠 `*IDN?` 回應 `SURUGA,DS1` 判定。主機板的 Intel AMT SOL 與 Bluetooth 虛擬埠會被 `SKIP_KEYWORDS` 跳過——它們開得起來但永遠不回應。

## 視覺設計原則

### tkinter 桌面介面（main_ai.py，ui-designer 職責範圍）

現有色彩／字型系統已成形，**新增或調整介面時延用既有 token，不要另立一套**：

- 色彩定義在 `CLR_BG` / `CLR_CARD` / `CLR_BORDER` / `CLR_ACCENT` / `CLR_DANGER` / `CLR_INFO` / `CLR_WARN` / `CLR_TEXT` / `CLR_MUTED` / `CLR_LOG_BG`（main_ai.py:257-266）。這些不是隨意選的色票，而是**語意化**的：`CLR_DANGER` 綁定撞限位／警報、`CLR_WARN` 綁定通訊失聯／待確認、`CLR_ACCENT` 綁定「目前選取軸」（見上方〈第四批修正〉）。新增任何狀態顯示前先檢查有沒有對應的語意色，不要因為好看而混用。
- 按鈕走 `ttk.Style` 的語意化角色（`Accent` / `Danger` / `Info` / `Warn` / `Flat`，main_ai.py:2256-2260），而不是逐一設色。新增按鈕先判斷屬於哪個語意角色，用既有的 `.TButton` style name，不要手動 `configure(bg=...)`。
- 字型統一 `("Segoe UI", 10)`（main_ai.py:2245）——這是 Windows 系統預設字型，**刻意**不是什麼「有特色」的排版選擇，而是為了跟作業系統其餘 UI 元素視覺一致、且不需要額外綁定字型檔。這支程式是驅動實體滑台的工業控制面板，**易讀性與跨機器一致性優先於視覺獨特性**：不要為了風格新增自訂字型或加大字重層級，除非能確認目標機器都有安裝。
- 這套系統本身就是多輪安全修正（2026-08-05～08-06）逐步收斂出來的——像「未連線一律顯示 `—` 不顯示 `0`」「橫幅常駐 pack 避免版面跳動」都是介面決策同時也是安全機制，改視覺樣式時連帶會動到這些行為，**先讀上方〈第三批／第四批修正〉再動手**。
- **開機視窗改為預設最大化**（2026-08-19，`_build_window()`：`self.root.state("zoomed")`，Windows 專用、非真正全螢幕，保留標題列/工作列）。原本沒有設 `geometry()`、只有 `minsize(1020, 720)`，Tk 會照 widget 最小需求尺寸開窗；這幾輪陸續加了軸校正參數卡片、安全常數橫幅、尋光控制列與更寬的圖表後，預設開窗尺寸擠壓內容、常需要捲動。改用 `state("zoomed")` 而非寫死固定像素（例如 `geometry("1600x1000")`），是為了不綁定特定螢幕解析度——使用者仍可自行拖曳還原成任意大小，只是開機當下改成先吃滿目前螢幕可用空間。已確認在 `root.withdraw()` 之後呼叫（測試環境的既有模式）不會出錯。
- 新增／調整 GUI 元件一律先派 `ui-designer` 提案（見下方〈子代理分工〉），不要自行決定版面。

### HTML / Artifact 報告（fiber_scan_charts.html 這類產出）

這類報告目前是 scratchpad 產出（不進版控），但當作對外可分享的正式交付物看待，**視覺水準要對得起裡面的真機驗證數據**：

- **避免「AI slop」美學**：不要預設用 Inter / Roboto / Arial / system-ui 這類無特色字型，不要落入「白底紫色漸層」這種樣板配色，版面不要是無差異的置中卡片堆疊。挑選字型與配色時要對應內容特性——這批報告是精密量測數據，可以往「儀器儀表／科學圖表」的方向找識別度（例如等寬字型呈現數字、細線條分隔、資料本身作為視覺焦點），而不是為了花俏而花俏。
- **色彩要有主從**：延續 `fiber_scan_charts.html` 已建立的做法——完整的淺色 token 定義在 `:root`，深色模式在 `@media (prefers-color-scheme: dark)` 與 `:root[data-theme="dark"]` 兩處同步覆寫（見 Artifact 發布規範）。不要每次重新發明一套 token 命名，沿用既有的 `--series-*`、`--accent-*` 系列。
- **動態效果要服務理解，不是裝飾**：`fiber_scan_charts.html` 的 `buildReplayDemo()` 逐筆重播是先例——用動畫呈現「演算法怎麼一步步收斂」這種本來要盯著一堆數字才能理解的過程，是值得投入的地方；不要在不需要的地方加微互動。
- **自包含限制不可違反**：不能連外部字型 CDN（Google Fonts 等）——嚴格 CSP 會擋。要用有特色的字型，選擇系統常見的 serif/mono 字型堆疊，或把字型檔案內嵌成 data URI（注意檔案大小，Artifact 上限 16MB）。
- 這類報告的資料**必須先查證再寫入**（見〈子代理分工〉裡 `mathematician` 與 `reporter` 的分工），視覺設計服務的是「把已經查證過的數據講清楚」，不能為了美觀而簡化或誤導數據本身的意義。

## 子代理分工（常設規則，不需逐次指派）

`.claude/agents/` 底下有六個代理：`architect`（設計與審查）、`coder`（實作）、`tester`（測試）、`ui-designer`（介面與操作體驗）、`mathematician`（數值方法與量測數據分析）、`reporter`（彙整跨代理討論與測試紀錄成正式文件）。
**以下情況直接派工，不必等使用者開口**：

| 時機 | 派給 |
|---|---|
| 動到 `main_ai.py` 的執行緒、序列通訊、限位／安全邏輯、持久化 | 先 `architect` 評估，再實作 |
| 新增／調整 GUI 元件、分頁版面、對話框、狀態呈現方式 | 先 `ui-designer` 提案，再實作 |
| 設計掃描尋光演算法、擬合光功率曲線、座標系換算或誤差分析 | 先 `mathematician` 設計，`coder` 落地 |
| 改動完成後 | `architect` 審查；有測試價值的邏輯再交 `tester` |
| 使用者只給規格、要求產出實作 | `coder` |
| 多個代理已分別提出結論、或一輪開發＋測試＋bug 修正告一段落，要收斂成文件 | `reporter` 彙整既有討論與實測數據，不重新做設計判斷 |

例外——**這些情況自己做，不要派工**：一兩行的修正、純文件更新、使用者已明確指定做法的改動、以及任何會實際驅動硬體的操作（那必須先取得使用者授權，不可轉手給代理）。

## 編輯注意事項

- **每次 Write / Edit 之後會自動跑語法檢查**：`.claude/settings.json` 掛了 PostToolUse hook `.claude/hooks/check_py_syntax.py`，對 `.py` 檔跑 `ast.parse()`，語法錯就 `decision: block`。看到它擋下來就是真的打錯了，當場修掉。該 hook 的輸出**一律 `ensure_ascii=True`**——這台機器的 stdout 是 cp950，直接印中文會 `UnicodeEncodeError`，改動它時別把這點拿掉。
- `.vscode/settings.json` 刻意把 pyserial / PyVISA / tkinter 相關的 Pylance 診斷降為 `information`（型別描述檔與實際行為有落差），但保留 `reportCallIssue` / `reportUndefinedVariable` / `reportMissingImports` 為 error。看到這幾類紅字要當真。
- 該檔同時關閉了 `editor.formatOnSave`，避免 ruff 大範圍重排 main_ai.py。請勿對整檔套用格式化——只改動到的區塊。
- 廠商驅動資料夾 `DS102-CDMv21228/`、`DS102_USB_Driver_V2.08.28/` 不進版控也不需分析。
