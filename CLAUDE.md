# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

本專案的程式註解與文件皆為繁體中文，請沿用同樣語言撰寫新的註解與文件。

## 專案性質

Windows 桌面應用，用 Python + tkinter 控制 **駿河精機 SURUGA SEIKI DS102 / DS112 步進馬達控制箱**（RS-232C / USB 虛擬 COM 埠），用於光纖對準與光學自動化量測。長期目標（見 [step-motor.txt](step-motor.txt)）是把滑台與 **HP 8153A 光波萬用表**（GPIB）串起來做自動掃描尋光。

沒有 CI、沒有套件化結構——全部是可直接執行的頂層腳本。有七支回歸測試腳本（`verify_scan_tab.py` 57、`verify_meter_panel.py` 87、`verify_axis_calib.py` 50、`verify_blind_scan.py` 44、`verify_wait_axis_stop.py` 29、`verify_fiber_scanner_signal.py` 14、`verify_ctrl_pos_sync.py` 13，共 294 項，見下方〈常用指令〉。⚠ 這串數字每次加測試都會過期，以 `pytest --collect-only -q` 的實際輸出為準），用假物件跑 GUI 邏輯。2026-08-18 起改寫成 **pytest 測試檔**（檔名不變，透過 `pytest.ini` 的 `python_files` 設定讓 pytest 認得 `verify_*.py` 這個既有命名），VS Code 的 Testing 面板可以個別發現、個別重跑每一項；`conftest.py` 放共用的 fixture（建 GUI、跑 Tk mainloop、monkeypatch `RECORDING_DIR`）。

🔴 **`conftest.py` 的 `make_gui()` 必須同時 `patch.object(main_ai, "RECORDING_DIR", ...)` 與 `patch.object(ds102_ctrl, "RECORDING_DIR", ...)`，只 patch 一邊等於沒防護（2026-08-19 實測踩到）。** `main_ai.py` 是用 `from ds102_ctrl import RECORDING_DIR` 重新引入，這只是另一個獨立綁定同一初始物件的名字；`DS102Controller` 的持久化方法（`save_point`／`set_axis_calib`／`save_recording`／`capture_controller_config` 等）全部定義在 `ds102_ctrl.py`，引用的是該模組**自己的**模組層級綁定，只 patch `main_ai.RECORDING_DIR` 完全攔不到這些方法，會直接寫進專案真正的 `recordings/`。`verify_scan_tab.py`／`verify_meter_panel.py` 之前沒踩到純粹是因為沒呼叫到這些方法，不代表這層防護真的有效——跟本檔記載「測試腳本清空過兩次 teaching points」是同一類風險。已在 `make_gui()` 修好，**任何新增的測試檔案都不需要（也不應該）再自己額外 patch 一次**，但改動 `conftest.py` 本身時要記得這兩邊要同步。**2026-08-21 起 `DATA_DIR` 也比照辦理**（`patch.object` main_ai 與 ds102_ctrl 兩邊，導到 `recording_dir/data`）：新增的 `save_homing_repeat_result()` 寫的是 `DATA_DIR` 而非 `RECORDING_DIR`，理由與上述完全相同——少 patch 一邊，測試就會寫進專案真正的 `data/`。

## 常用指令

一律使用專案內的 venv 直譯器（全域 Python 沒有 pyserial / PyVISA）：

```bash
venv/Scripts/python.exe main_ai.py                       # 執行主程式（GUI）
venv/Scripts/python.exe probe_ds102.py                   # 診斷：列出序列埠並輪詢 *IDN?
venv/Scripts/python.exe probe_ds102.py --list            # 只列埠，不送任何指令（不會動到硬體）
venv/Scripts/python.exe -m serial.tools.list_ports -v    # 原始序列埠清單
venv/Scripts/python.exe -m pip install -r requirements.txt
venv/Scripts/python.exe -m ruff check .                  # ruff 未列於 requirements.txt，需另行安裝
venv/Scripts/python.exe -m pytest verify_scan_tab.py verify_meter_panel.py verify_axis_calib.py verify_fiber_scanner_signal.py verify_wait_axis_stop.py verify_ctrl_pos_sync.py verify_blind_scan.py -v  # 七支合計 294 項（2026-08-26 實測），不需硬體
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
| [ds102_ctrl.py](ds102_ctrl.py) | 🔴 **不要跟下面的 `ds102_controller.py` 搞混**——這是 2026-08-17 從 main_ai.py 拆出來的 `DS102Controller` 本體（現役程式碼，約 3763 行），main_ai.py 用 `from ds102_ctrl import DS102Controller, ...` 引入。細節見下方〈main_ai.py 架構〉 |
| [ds102_controller.py](ds102_controller.py) | main_ai.py 的前一版快照（約 1970 行，跟上面的 `ds102_ctrl.py` 是完全不同的兩個檔案）。已進版控，可作為對照，但**不要在此新增功能** |
| [main.py](main.py) | 廠商 SURUGA SEIKI 官方範例（模組層級全域變數風格），是**指令格式的權威來源**。main_ai.py 的每個指令組法都對應此檔某段程式。修改指令時先回頭比對 |
| [test.py](test.py) | 無 GUI 的連線 / 狀態查詢腳本（含 `find_ds_port()` 自動搜埠）。名稱誤導——不是單元測試 |
| [probe_ds102.py](probe_ds102.py) | 序列埠診斷工具，硬體接不上時的第一站 |
| [meter_GPIB.py](meter_GPIB.py) | HP 8153A 光功率計封裝（PyVISA）。**已於 2026-08-12 整合進 GUI**（main_ai.py 直接 `from meter_GPIB import HP8153APowerMeter`），供「光功率」與「尋光」分頁使用。**2026-08-26 已實機連線**（`HEWLETT-PACKARD,8153A,0,2.1`，GPIB21／Ch2／1310nm），同日修掉「初始化無條件鎖死 -20dBm 量程」導致無光時必定 underrange 的問題，改為預設自動量程＋讀到 sentinel 時自動退回，見 [docs/fiber-scan.md](docs/fiber-scan.md)〈階段零：盲搜粗掃〉。⚠ 自動量程在實機能否讀到無光底噪**尚未驗證** |
| [fiber_scanner.py](fiber_scanner.py) | `FiberAlignmentScanner`：光纖對準尋光演算法（座標下降＋K近鄰精修＋收尾微擾）。**已於 2026-08-13～17 分六階段接上 GUI**（main_ai.py 的「尋光」分頁），並補上 57 項假物件回歸測試（見 [docs/fiber-scan.md](docs/fiber-scan.md)）。本檔自己**仍刻意不 import main_ai.py**（避免循環相依），軸命名自成一份，main_ai.py 改軸命名時要同步 |
| [verify_scan_tab.py](verify_scan_tab.py) / [verify_meter_panel.py](verify_meter_panel.py) / [verify_axis_calib.py](verify_axis_calib.py) / [verify_fiber_scanner_signal.py](verify_fiber_scanner_signal.py) / [verify_wait_axis_stop.py](verify_wait_axis_stop.py) / [verify_ctrl_pos_sync.py](verify_ctrl_pos_sync.py) / [verify_blind_scan.py](verify_blind_scan.py) | 「尋光」／「光功率」分頁／軸機械校正參數／`fiber_scanner.py` 訊號有效性判準／`_wait_axis_stop()` 起步競態／移動控制分頁座標供應鏈的假物件回歸測試（合計 294 項 pytest 測試函式，`python -m pytest verify_scan_tab.py verify_meter_panel.py verify_axis_calib.py verify_fiber_scanner_signal.py verify_wait_axis_stop.py verify_ctrl_pos_sync.py verify_blind_scan.py -v` 執行，VS Code Testing 面板也認得）。用假的 `ctrl` / `meter` 物件驅動邏輯，不需要真實硬體。`verify_fiber_scanner_signal.py` 是 2026-08-19 從某次 session 的 scratchpad 補進版控並轉成 pytest（原本是獨立可執行腳本），轉換時發現它的 `FakeCtrl` 沒有 `estimate_um()`，因為原腳本寫於 μm 快照功能（見 [docs/axis-calibration.md](docs/axis-calibration.md)）加入之前——`_measure_here()` 現在無條件呼叫這個方法，補上回傳 `None` 的樁即可，不影響任何既有斷言。`verify_wait_axis_stop.py` 是 2026-08-26 修〈孿生競態〉時新增的（29 項），它是唯一一支不建 GUI、直接對 `DS102Controller` 實例逐一 monkeypatch `query_status` 的測試檔，所以沒有用 `conftest.py` 的 `make_gui()`，而是自己 `patch.object` `ds102_ctrl.RECORDING_DIR`／`DATA_DIR`——新增同類測試檔時照抄它的 `ctrl` fixture 即可。`verify_ctrl_pos_sync.py` 是 2026-08-26 修〈移動控制分頁「Position:」的座標供應鏈統一〉時新增的（13 項），用 module-scope 的 `gui` fixture＋autouse 的狀態重置，直接同步呼叫 `_redraw_positions()` 斷言畫面文字。`verify_blind_scan.py` 是 2026-08-26 修〈無光位置尋光無動作〉時新增的（44 項，見 [docs/fiber-scan.md](docs/fiber-scan.md)〈階段零：盲搜粗掃〉），它的 `FakeCtrl` 與 `verify_fiber_scanner_signal.py` 那份**不通用**：盲搜走 `_move_multi_axis()`，該路徑會傳 `start_pos`／`expected_travel` 兩個位移提示，舊的 `wait_axis_stop()` 樁沒有這兩個參數會直接 TypeError；本檔的版本另外支援軟體限位，用來驗證盲搜對超出行程的格點是「跳過並繼續」而不是整批中止 |
| [conftest.py](conftest.py) / [pytest.ini](pytest.ini) | pytest 共用設定：`conftest.py` 放建 GUI／跑 Tk mainloop／monkeypatch `RECORDING_DIR` 這類共用 fixture；`pytest.ini` 把 `python_files` 放寬成同時認得 `verify_*.py` 與標準 `test_*.py`，並排除 `venv`／驅動資料夾等不相關目錄 |
| [Gtest.py](Gtest.py) | 外部第三方範例（NTT-Mabuchi），`import control` 的模組不存在於本 repo，**無法執行**，僅作參考 |
| [step-motor.txt](step-motor.txt) | 三層架構藍圖與 GPIB 側注意事項。⚠ 但其中的 **DS112 通訊細節全部是錯的**（宣稱結束符 `\r\n`、鮑率 9600、用 `!:` 輪詢 B/R 狀態）——實機是 `\r`、38400、查 `SB1?`。此檔只採信 HP 8153A 與「馬達動則不讀光」那幾段 |
| [FIBER_ALIGNMENT_SCAN_DESIGN.md](FIBER_ALIGNMENT_SCAN_DESIGN.md) | 尋光演算法的完整設計文件：mathematician 兩輪演算法討論、architect 落地評估、無硬體驗證方式、待實測參數清單 |

## 🔴 這份檔案是索引，不是全文

專案的完整技術紀錄在 [docs/](docs/)，2026-08-26 從本檔拆出（原本 614 行／約 36k token，每次對話都要全額載入）。**內容一字未刪，只是改成按需讀取。**

**不要因為 CLAUDE.md 沒寫，就以為沒有規定。** 下面每一條紅線背後都有實際踩過的案例，經過與數據記在對應文件裡——動到該區域前先把那份讀完，這比省 token 重要得多。

| 動到什麼 | 先讀 |
|---|---|
| 執行緒、輪詢迴圈、長按點動限位 | [docs/threading.md](docs/threading.md) |
| 原點復歸、`GO ORG`、`POS 0` 歸零、重現性量測 | [docs/homing.md](docs/homing.md) |
| 限位、警報、按鈕鎖定、重播、任何安全行為 | [docs/safety-fixes.md](docs/safety-fixes.md) |
| 尋光演算法、光功率分頁、`scanning_active` / `motion_active` | [docs/fiber-scan.md](docs/fiber-scan.md) |
| 任何 json 設定檔的讀寫 | [docs/settings-files.md](docs/settings-files.md) |
| 送出／修改任何 DS102 指令 | [docs/protocol.md](docs/protocol.md) |
| pulse→μm 換算、`axis_calib` | [docs/axis-calibration.md](docs/axis-calibration.md) |
| 硬體連不上、找不到埠、實測行程數值 | [docs/hardware.md](docs/hardware.md) |
| 新增／調整 GUI 元件、色票、按鈕樣式 | [docs/ui-design.md](docs/ui-design.md) |
| 打包成 exe、執行期目錄 | [docs/packaging.md](docs/packaging.md) |
| 想把 main_ai.py 拆檔 | [docs/modularization.md](docs/modularization.md) |

## 🔴 紅線速查（每條的完整經過在括號中的文件）

- **沒有模擬模式，不要加回來。** 這支程式驅動真實滑台，假回應會讓人以為連上了硬體。
- **`move_step()` 回傳 bool，新增呼叫一律檢查回傳值**；False = 被攔截／逾時／撞限位。（safety-fixes）
- **選對移動入口**：GUI 操作走 `move_step()`／`move_origin()`（含守衛）；scanner 內部走 `scan_move_step()`；量測走 `_do_move_step()`／`_do_origin()`（無守衛）。把 `scanning_active` 放進 `move_step` 守衛曾讓所有收斂測試卡死。（fiber-scan）
- **`motion_active` 只判斷「要不要占用其他硬體資源」，絕不可當移動守衛。**（fiber-scan）
- **光功率計預設用自動量程，不可為了速度無條件鎖死檔位。** 鎖在 -20dBm 曾讓無光起點必定 underrange（HP 8153A 回 `+9.9E+37`），尋光整段靜默空轉、滑台一步未動。要鎖必須搭配 `get_power()` 的自動退回。（fiber-scan）
- **盲搜的 `except` 只能攔 `NoSignalAbort`，不可攔 `ScanAbort`。** 使用者中止與 EMS 也是 ScanAbort，攔錯會讓「按下停止」換來一輪掃過上千格點的盲搜。（fiber-scan）
- **`POS 0` 只能在 `_confirm_stopped()` 通過後才寫。** 寫在飛行中等於把座標系原點偷偷改掉，且零警告。（homing）
- **「非 Driving」不等於「已停好」**：`GO` 有約 96ms 的 Driving assert 延遲，等待函式一律要有位移證據。（homing）
- **任何新增的「作業進行中」狀態，必須同步加進 `_update_stat_ui` 的按鈕鎖定判斷**，否則 100ms 後按鈕自己復活。（safety-fixes）
- **停止／中止類按鈕不可放進 `_drive_buttons`**，那串會在重播中與復歸中被整批 disable——正是最需要停止的時候。（safety-fixes）
- **新增放在 `RECORDING_DIR` 的設定檔，務必同步加進 `NON_RECORDING_JSON`**，且一律走 `_write_json_with_backup()`。（settings-files）
- **`ORG_MODES` 從 `ORG 1` 開始，不可用 `.index()` 換算樣式編號**（差一位）。（safety-fixes）
- **`limit_direction()` 比對方向字串必須先判斷 `"CCW"`**——`"CCW"` 本身含有 `"CW"`。（protocol）
- **`PULS` 不接受小數且失敗完全靜默**，送出前一律取整數。（protocol）
- **MEMSW 與韌體軟體限位都是 RAM-only**，控制器斷電整組歸零。（homing / settings-files）
- **座標 0 幾乎就落在限位開關上**，任何「移動到 0」的預設值等於推去撞端點。（hardware）
- **`axis_calib.division`（使用者輸入的倍數）不可用 `axis_drdiv`（軟體暫存器查表索引）代入**，那會把索引當倍數算出錯誤的 μm。（axis-calibration）
- **`ttk.Checkbutton` 的 `command` 必須依 `variable` 的新值分流**（Tk 先翻轉變數再呼叫 command）。無條件當成「開」會讓浮動視窗永遠關不掉。（ui-design）
- **打包用 `--onedir` 不用 `--onefile`**（SentinelOne 誤判前科）。（packaging）

## main_ai.py 架構

main_ai.py 約 6430 行（2026-08-26 實測；⚠ 這已越過[docs/modularization.md](docs/modularization.md)的門檻 4「5800～6000 行」，下次動到分頁結構前應先重新評估方向1b，本檔先前記載的 5408 行是過時數字）（2026-08-06 時約 3100 行，2026-08-12～17 加入光功率／尋光兩分頁後一度衝到 6601 行，2026-08-17 把 `DS102Controller` 拆出去後降到 4839 行，之後陸續加入 B 類安全常數橫幅、軸機械校正參數卡片、尋光彈性選軸與軌跡圖重繪，漲回目前規模——2026-08-19 architect 重新評估模組化時的量測點是 5106 行，之後又加了約 300 行，仍未到[docs/modularization.md](docs/modularization.md)定義的 5800～6000 行觸發線），邏輯上仍是三塊，但**`DS102Controller` 現在實際定義在 [ds102_ctrl.py](ds102_ctrl.py)**：

1. **`DS102Controller`**（[ds102_ctrl.py](ds102_ctrl.py) 全檔約 3763 行，`class DS102Controller` 本身約 3170 行）— 所有序列通訊集中於此，完全不碰 tkinter。對外只暴露 `connect()` / `move_step()` / `query_status()` / `goto_point()` 等高階方法。main_ai.py 開頭用 `from ds102_ctrl import DS102Controller, AXES, AXIS_NO, NO_AXIS, MODE_CONTINUE, MODE_STEP, MODE_ORIGIN, COMM_FAIL_THRESHOLD, _BASE_DIR, LOG_DIR, RECORDING_DIR, DATA_DIR, NON_RECORDING_JSON, logger, _write_json_with_backup, _load_json_settings, _app_settings, _app_setting_num, _safety_setting_rejections` 整批重新引入——這份清單就是 `DS102Controller` 的完整依賴閉包，改動任一邊的模組層級常數前先確認它有沒有在這份清單裡（`_load_json_settings`／`_safety_setting_rejections` 是後來加的，見 [docs/settings-files.md](docs/settings-files.md) 與 [docs/modularization.md](docs/modularization.md)，這份清單本身就是活的，隨改動同步更新）。
2. **`StatusBar`**（仍在 main_ai.py）— 各分頁共用的座標 / 連線狀態列（同時存在多個實例，統一收在 `self._status_bars`）。**刻意沒有跟著搬去 `ds102_ctrl.py`**：它用到的 `CLR_*` 色票（含 `app_settings.json` 覆寫邏輯）留在 main_ai.py，若把 `StatusBar` 也搬走，`ds102_ctrl.py` 會反過來需要 import main_ai.py 的色票，形成循環相依；`StatusBar` 本身只有約 120 行、且與 `DS102GUI` 的 `self._status_bars` 集中管理耦合更緊，留給下次拆 `DS102GUI` 時一併考慮較合適。
3. **`DS102GUI`**（main_ai.py）— 七個分頁（分頁標題字串為「儀表板 / 移動控制 / Teaching / 行程錄製 / 光功率 / 尋光 / LOG」，grep 時用這些字），只呼叫 controller 的公開方法。「光功率」封裝 `HP8153APowerMeter`（[meter_GPIB.py](meter_GPIB.py)）、「尋光」封裝 `FiberAlignmentScanner`（[fiber_scanner.py](fiber_scanner.py)），細節見 [docs/fiber-scan.md](docs/fiber-scan.md)。

⚠ `fiber_scanner.py` 的 `TYPE_CHECKING` 型別提示已同步改成 `from ds102_ctrl import DS102Controller`（原本指向 main_ai.py，`DS102Controller` 搬家後這裡也要跟著改，否則型別提示會指向錯誤的定義位置——雖然不影響執行期，但下次有人依賴它做型別檢查會查錯地方）。`fiber_scanner.py` 本身仍然不 import 任何一個 main_ai 系列模組，「避免循環相依」的方向沒變。

🔴 **main_ai.py 開頭那個 `from ds102_ctrl import (...)` 區塊裡的 `NON_RECORDING_JSON`，IDE／靜態分析會標成「unused import」，但不能因此移除。** [verify_meter_panel.py](verify_meter_panel.py) 直接讀 `main_ai.NON_RECORDING_JSON` 這個模組屬性做斷言，main_ai.py 程式邏輯本身確實沒用到它，但拿掉這個 import 會讓 `main_ai` 模組上不再有這個屬性，那支回歸測試整支炸掉（`AttributeError`）。2026-08-17 拆分 `ds102_ctrl.py` 時實際發生過兩次：coder 第一次搬移時就發現這個依賴、刻意加回重新引入清單；後續一輪「清理架構審查發現的死 import」又想拿掉，靠重跑 `verify_meter_panel.py` 才抓到。**靜態分析工具看不到外部測試腳本這種跨模組屬性依賴**——main_ai.py:275-280 附近有對應註解，改動這個 import 區塊前務必先跑 `verify_meter_panel.py`，不要只憑 grep 或 IDE 診斷判斷「看起來沒用到」。

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

### 單位與座標（容易改錯的地方）

- **單位一律 pulse，沒有 um / mm 切換**（2026-08-05 移除）。連線時送 `AXI{n}:UNIT 0` 把控制器也固定在 pulse，所以 `POS?` 回傳值即 pulse，不需要任何換算函式。之所以拿掉：控制器裡的 `SD`（每 pulse 距離）並未配置實際尺度（`RESOLUT?` = 1），換算成 um/mm 等於拿未經驗證的假設去乘除。**這個決定仍然成立**——[docs/axis-calibration.md](docs/axis-calibration.md) 的軸機械校正參數是額外疊加的估算顯示，不是恢復這個切換。

細節（`axis_calibration.json`、`estimate_um()`、存檔時的 μm 快照）見 [docs/axis-calibration.md](docs/axis-calibration.md)。

## 執行期產出（皆已 gitignore）

- `logs/ds102_YYYYMMDD_HHMMSS.log` — 每次啟動一個檔（DEBUG 進檔案，INFO 以上進終端機）；關閉時另存 `*_history.txt`
- `recordings/*.json` — 錄製的行程；同目錄的 `teaching_points.json`、`speed_profiles.json`、`controller_config.json`、`meter_config.json`、`scanner_config.json` 是設定檔，載入錄製清單時由 `NON_RECORDING_JSON` 明確排除（另有自動產生的 `*.json.bak`）
- `data/data_*.csv` — 實驗數據（時間戳 + 各軸位置）
- `data/homing_repeat_*.csv` / `data/homing_repeat_*.json` — 原點復歸重現性量測結果（CSV 長格式逐輪明細，JSON 是 metadata + 統計摘要），見 [docs/homing.md](docs/homing.md)
- 根目錄殘留的 `ds102_log_YYYYMMDD.log` 來自舊版 main.py / test.py 的 logging 設定
- 根目錄的 `output/`（auto-py-to-exe 的產出目錄）與 PyInstaller 的 `build/`/`dist/`/`*.spec` 皆已列入 `.gitignore`。

## 子代理分工（常設規則，不需逐次指派）

`.claude/agents/` 底下有八個代理：`architect`（設計與審查）、`coder`（實作）、`tester`（測試）、`ui-designer`（介面與操作體驗）、`mathematician`（數值方法與量測數據分析）、`reporter`（彙整跨代理討論與測試紀錄成正式文件）、`questioner`（針對報告提出釐清與批判性問題，不改寫文件本身）、`data-scientist`（把已查證數據轉成圖表與統計摘要供報告使用，不蒐集新數據、不做演算法設計）。
**以下情況直接派工，不必等使用者開口**：

| 時機 | 派給 |
|---|---|
| 動到 `main_ai.py` 的執行緒、序列通訊、限位／安全邏輯、持久化 | 先 `architect` 評估，再實作 |
| 新增／調整 GUI 元件、分頁版面、對話框、狀態呈現方式 | 先 `ui-designer` 提案，再實作 |
| 設計掃描尋光演算法、擬合光功率曲線、座標系換算或誤差分析 | 先 `mathematician` 設計，`coder` 落地 |
| 改動完成後 | `architect` 審查；有測試價值的邏輯再交 `tester` |
| 使用者只給規格、要求產出實作 | `coder` |
| 多個代理已分別提出結論、或一輪開發＋測試＋bug 修正告一段落，要收斂成文件 | `reporter` 彙整既有討論與實測數據，不重新做設計判斷 |
| `reporter` 產出或更新的報告要定稿交付前 | `questioner` 挑缺口、找沒查證的宣稱，問題丟回 `reporter`／使用者，`questioner` 本身不改寫報告 |
| 報告需要新增數據圖表、趨勢線、統計摘要 | `data-scientist` 只用已查證數據產圖表與摘要，交回 `reporter` 嵌入報告行文 |

例外——**這些情況自己做，不要派工**：一兩行的修正、純文件更新、使用者已明確指定做法的改動、以及任何會實際驅動硬體的操作（那必須先取得使用者授權，不可轉手給代理）。

## 編輯注意事項

- **每次 Write / Edit 之後會自動跑語法檢查**：`.claude/settings.json` 掛了 PostToolUse hook `.claude/hooks/check_py_syntax.py`，對 `.py` 檔跑 `ast.parse()`，語法錯就 `decision: block`。看到它擋下來就是真的打錯了，當場修掉。該 hook 的輸出**一律 `ensure_ascii=True`**——這台機器的 stdout 是 cp950，直接印中文會 `UnicodeEncodeError`，改動它時別把這點拿掉。
- `.vscode/settings.json` 刻意把 pyserial / PyVISA / tkinter 相關的 Pylance 診斷降為 `information`（型別描述檔與實際行為有落差），但保留 `reportCallIssue` / `reportUndefinedVariable` / `reportMissingImports` 為 error。看到這幾類紅字要當真。
- 該檔同時關閉了 `editor.formatOnSave`，避免 ruff 大範圍重排 main_ai.py。請勿對整檔套用格式化——只改動到的區塊。
- 廠商驅動資料夾 `DS102-CDMv21228/`、`DS102_USB_Driver_V2.08.28/` 不進版控也不需分析。
