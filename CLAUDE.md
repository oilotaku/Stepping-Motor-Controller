# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

本專案的程式註解與文件皆為繁體中文，請沿用同樣語言撰寫新的註解與文件。

## 專案性質

Windows 桌面應用，用 Python + tkinter 控制 **駿河精機 SURUGA SEIKI DS102 / DS112 步進馬達控制箱**（RS-232C / USB 虛擬 COM 埠），用於光纖對準與光學自動化量測。長期目標（見 [step-motor.txt](step-motor.txt)）是把滑台與 **HP 8153A 光波萬用表**（GPIB）串起來做自動掃描尋光。

沒有測試套件、沒有 CI、沒有套件化結構——全部是可直接執行的頂層腳本。

## 常用指令

一律使用專案內的 venv 直譯器（全域 Python 沒有 pyserial / PyVISA）：

```bash
venv/Scripts/python.exe main_ai.py                       # 執行主程式（GUI）
venv/Scripts/python.exe probe_ds102.py                   # 診斷：列出序列埠並輪詢 *IDN?
venv/Scripts/python.exe probe_ds102.py --list            # 只列埠，不送任何指令（不會動到硬體）
venv/Scripts/python.exe -m serial.tools.list_ports -v    # 原始序列埠清單
venv/Scripts/python.exe -m pip install -r requirements.txt
venv/Scripts/python.exe -m ruff check .                  # ruff 未列於 requirements.txt，需另行安裝
```

VS Code 已設定對應的 tasks（預設 build task = 執行 GUI）與 launch 設定，見 [.vscode/](.vscode/)。

終端機執行時務必帶 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`（Windows 主控台預設 cp950，否則中文 log 會亂碼）。VS Code 側已涵蓋：`settings.json` 的 `terminal.integrated.env.windows` 對整合終端機全域生效、`launch.json` 四個 configuration 全部自帶 `env`；`tasks.json` 只有「執行 DS102 GUI」自帶 `env`，其餘三個 task 靠 settings 的全域設定拿到（都是 `type: shell`，所以有效）。

**沒有硬體時**：GUI 頂端有「模擬模式」按鈕，`DS102Controller.connect_sim()` 會走 `_sim_parse` / `_sim_query` 假造回應，可完整測試 UI 流程與錄製重播。

## 檔案定位（哪個才是主程式）

根目錄有三份高度相似的 DS102 程式，改錯檔案是最常見的失誤：

| 檔案 | 定位 |
|---|---|
| [main_ai.py](main_ai.py) | **唯一的主程式（v3.0）**，功能與修正都加在這裡 |
| [ds102_controller.py](ds102_controller.py) | main_ai.py 的前一版快照（約 1970 行）。已進版控，可作為對照，但**不要在此新增功能** |
| [main.py](main.py) | 廠商 SURUGA SEIKI 官方範例（模組層級全域變數風格），是**指令格式的權威來源**。main_ai.py 的每個指令組法都對應此檔某段程式。修改指令時先回頭比對 |
| [test.py](test.py) | 無 GUI 的連線 / 狀態查詢腳本（含 `find_ds_port()` 自動搜埠）。名稱誤導——不是單元測試 |
| [probe_ds102.py](probe_ds102.py) | 序列埠診斷工具，硬體接不上時的第一站 |
| [meter_GPIB.py](meter_GPIB.py) | HP 8153A 光功率計封裝（PyVISA），目前尚未與馬達程式整合 |
| [Gtest.py](Gtest.py) | 外部第三方範例（NTT-Mabuchi），`import control` 的模組不存在於本 repo，**無法執行**，僅作參考 |
| [step-motor.txt](step-motor.txt) | 三層架構藍圖與 GPIB 側注意事項。⚠ 但其中的 **DS112 通訊細節全部是錯的**（宣稱結束符 `\r\n`、鮑率 9600、用 `!:` 輪詢 B/R 狀態）——實機是 `\r`、38400、查 `SB1?`。此檔只採信 HP 8153A 與「馬達動則不讀光」那幾段 |

## main_ai.py 架構

單檔約 3100 行，嚴格分成三塊：

1. **`DS102Controller`** — 所有序列通訊集中於此，完全不碰 tkinter。對外只暴露 `connect()` / `move_step()` / `query_status()` / `goto_point()` 等高階方法。
2. **`StatusBar`** — 各分頁共用的座標 / 連線狀態列（同時存在多個實例，統一收在 `self._status_bars`）。
3. **`DS102GUI`** — 五個分頁（分頁標題字串為「儀表板 / 移動控制 / Teaching / 行程錄製 / LOG」，grep 時用這些字），只呼叫 controller 的公開方法。

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

#### 四條輪詢迴圈（各司其職，別互相取代）

| 迴圈 | 在哪 | 節奏 | 做什麼 |
|---|---|---|---|
| `_start_poller()` | Tk 主執行緒 `root.after` | `UI_REDRAW_INTERVAL` 100ms | **只重繪快取的座標**，完全不碰序列埠 |
| `_start_position_worker()` | 背景執行緒 | `POSITION_POLL_INTERVAL` 0.5s | 呼叫 `refresh_positions()`，對每個已啟用軸送一筆 `POS?` 寫回 `_positions_pulse` |
| `_poll_status()` | 背景執行緒 `while` + `time.sleep(0.1)` | 100ms | 移動中追蹤選取軸的狀態，`status != "Driving"` 就收工 |
| `_watch_jog_limit()` | 背景執行緒 | `JOG_WATCH_INTERVAL` 0.06s | 長按點動時監看軟體限位 |

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

### 單位與座標（容易改錯的地方）

- **單位一律 pulse，沒有 um / mm 切換**（2026-08-05 移除）。連線時送 `AXI{n}:UNIT 0` 把控制器也固定在 pulse，所以 `POS?` 回傳值即 pulse，不需要任何換算函式。之所以拿掉：控制器裡的 `SD`（每 pulse 距離）並未配置實際尺度（`RESOLUT?` = 1），換算成 um/mm 等於拿未經驗證的假設去乘除。
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
- **恢復「模擬模式」按鈕**（一度被註解掉，但 CLAUDE.md 與 README 都寫它存在，且沒硬體時整個 UI 無法操作）。
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

⚠ **這兩層不對稱**：`_points_loaded` 的拒寫保護**只有 teaching points 有**。`_persist_profiles()` 是裸呼叫 `_write_json_with_backup`，沒有 `_profiles_loaded` 這個東西（全檔 grep 無此名）。所以「沒 load 就 save 會清空」這個實際發生過兩次的事故，在 `speed_profiles.json` 上**至今仍可重現**，只有 `.bak` 可救。

⚠ `_write_json_with_backup()` 的 `write_text` / `replace` **沒有 try/except**。磁碟滿或檔案被防毒鎖住時例外會往上拋進 Tk callback 變成 traceback，`.tmp` 殘留在 `recordings/`。它防的是「寫到一半壞檔」，不是「萬無一失」。

新增任何設定檔一律走 `_write_json_with_backup()`，別自己 `json.dump` 到目標路徑。

## DS102 通訊協定重點

**權威參考:`ds102 (2).pdf`**(170 頁,DS102/DS112 Operation Manual Ver 2.00,倉庫根目錄)。查指令前先翻它,不要靠猜——實測發現韌體對不存在的指令會**回傳看似合理的值**(例如 `LIMIT?` 回 `2`、`SOFTLIMIT?` 回 `0`),但這些都不在手冊的 Inquiry Command 表裡。第 4.3.4 節之後是完整指令表,查詢指令集中在 `＜Inquiry Command＞`(約 p.132)。

常用查詢(手冊縮寫,大寫字母不可省):

| 指令 | 用途 |
|---|---|
| `AXI{n}:CWSLP?` / `CCWSLP?` | 軟體限位**座標** |
| `AXI{n}:CWSLE?` / `CCWSLE?` | 軟體限位啟用(0=停用) |
| `AXI{n}:RESOLUT?` | 1 pulse 的距離 = `STANDARD?` ÷ 分割數 |
| `AXI{n}:DRDIV?` | 驅動器分割(0=full step…15=1/250) |
| `AXI{n}:PULSA?` / `HOMEP?` | 絕對驅動座標 / Home 座標 |
| `TCH00?`～`TCH63?` | 控制器**內建 64 組 teaching point** |

**機械限位(實體開關)的座標無法查詢**,手冊沒有這種指令;只能開到限位再讀 `POS?`。而且 `POS` 是相對暫存器,原點復歸會重設,所以穩定的量是兩端之差(行程)而非絕對值。

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

## 執行期產出（皆已 gitignore）

- `logs/ds102_YYYYMMDD_HHMMSS.log` — 每次啟動一個檔（DEBUG 進檔案，INFO 以上進終端機）；關閉時另存 `*_history.txt`
- `recordings/*.json` — 錄製的行程；同目錄的 `teaching_points.json`、`speed_profiles.json`、`controller_config.json` 是設定檔，載入錄製清單時由 `NON_RECORDING_JSON` 明確排除（另有自動產生的 `*.json.bak`）
- `data/data_*.csv` — 實驗數據（時間戳 + 各軸位置）
- 根目錄殘留的 `ds102_log_YYYYMMDD.log` 來自舊版 main.py / test.py 的 logging 設定
- ⚠ 根目錄的 `output/`（PyInstaller / auto-py-to-exe 的產出目錄，venv 內裝了這兩個工具）**不在 `.gitignore` 裡**。目前是空的，但一旦打包就會有大量二進位檔進版控——打包前先補上這條規則。

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

## 子代理分工（常設規則，不需逐次指派）

`.claude/agents/` 底下有四個代理：`architect`（設計與審查）、`coder`（實作）、`tester`（測試）、`ui-designer`（介面與操作體驗）。
**以下情況直接派工，不必等使用者開口**：

| 時機 | 派給 |
|---|---|
| 動到 `main_ai.py` 的執行緒、序列通訊、限位／安全邏輯、持久化 | 先 `architect` 評估，再實作 |
| 新增／調整 GUI 元件、分頁版面、對話框、狀態呈現方式 | 先 `ui-designer` 提案，再實作 |
| 改動完成後 | `architect` 審查；有測試價值的邏輯再交 `tester` |
| 使用者只給規格、要求產出實作 | `coder` |

例外——**這些情況自己做，不要派工**：一兩行的修正、純文件更新、使用者已明確指定做法的改動、以及任何會實際驅動硬體的操作（那必須先取得使用者授權，不可轉手給代理）。

## 編輯注意事項

- **每次 Write / Edit 之後會自動跑語法檢查**：`.claude/settings.json` 掛了 PostToolUse hook `.claude/hooks/check_py_syntax.py`，對 `.py` 檔跑 `ast.parse()`，語法錯就 `decision: block`。看到它擋下來就是真的打錯了，當場修掉。該 hook 的輸出**一律 `ensure_ascii=True`**——這台機器的 stdout 是 cp950，直接印中文會 `UnicodeEncodeError`，改動它時別把這點拿掉。
- `.vscode/settings.json` 刻意把 pyserial / PyVISA / tkinter 相關的 Pylance 診斷降為 `information`（型別描述檔與實際行為有落差），但保留 `reportCallIssue` / `reportUndefinedVariable` / `reportMissingImports` 為 error。看到這幾類紅字要當真。
- 該檔同時關閉了 `editor.formatOnSave`，避免 ruff 大範圍重排 main_ai.py。請勿對整檔套用格式化——只改動到的區塊。
- 廠商驅動資料夾 `DS102-CDMv21228/`、`DS102_USB_Driver_V2.08.28/` 不進版控也不需分析。
