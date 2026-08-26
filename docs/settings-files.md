# 設定檔與持久化

> 本文件自 CLAUDE.md 拆出（2026-08-26），目的是縮小每次對話的固定載入量。
> **內容未經刪減**，動到對應功能前請完整讀過本檔。

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

**這兩層原本不對稱**（`_points_loaded` 曾經只有 teaching points 有），2026-08-05 已補上 `_profiles_loaded` 讓 `_persist_profiles()` 比照同一套邏輯（〈已修〉（見 [safety-fixes.md](safety-fixes.md)））。`meter_config.json` / `scanner_config.json` 刻意**不**走這層保護——那兩份是純量欄位，整份覆寫本來就是正確行為，不是「累積型集合被空狀態蓋掉」的風險場景（見上方〈光功率／尋光分頁〉）。

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
