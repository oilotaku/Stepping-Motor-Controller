# DS102 / DS112 步進馬達控制器

Windows 桌面應用，用 Python + tkinter 控制 **駿河精機 SURUGA SEIKI DS102 / DS112 步進馬達控制箱**（RS-232C / USB 虛擬 COM 埠），用於光纖對準與光學自動化量測。

滑台已與 **HP 8153A 光波萬用表**（GPIB，封裝於 [meter_GPIB.py](meter_GPIB.py)）整合，GUI 內建「光功率」與「尋光」兩個分頁，可執行自動掃描尋光（[fiber_scanner.py](fiber_scanner.py) 的 `FiberAlignmentScanner`）。光功率計尚未接上真實儀器驗證過，本機沒有 GPIB 卡可測。

---

## 快速開始

```bash
# 安裝相依套件（一定要用專案內的 venv，全域 Python 沒有 pyserial / PyVISA）
venv/Scripts/python.exe -m pip install -r requirements.txt

# 執行主程式
venv/Scripts/python.exe main_ai.py
```

VS Code 使用者：預設 build task（`Ctrl+Shift+B`）就是執行 GUI，另有除錯設定，見 [.vscode/](.vscode/)。

> **終端機執行務必帶 `PYTHONUTF8=1` 與 `PYTHONIOENCODING=utf-8`。**
> Windows 主控台預設 cp950，否則中文 log 會亂碼。VS Code 側已由
> `settings.json` 的 `terminal.integrated.env.windows` 全域設定。

**本程式沒有模擬模式**——它驅動的是真實滑台，假造的回應會讓人誤以為已連上硬體。沒有硬體時要測控制邏輯，請用假的 serial 物件取代 `ctrl.ser`。

```bash
# 跑回歸測試（假物件，不需硬體，187 項）
venv/Scripts/python.exe -m pytest verify_scan_tab.py verify_meter_panel.py verify_axis_calib.py verify_fiber_scanner_signal.py -v
```

VS Code 的 Testing 面板也能個別發現、個別重跑每一項（`.vscode/settings.json` 已設定 `python.testing.pytestEnabled`）。

---

## 環境

| 項目 | 版本 / 值 |
|---|---|
| Python | 3.14.4（venv 內） |
| pyserial | 3.5 |
| PyVISA | 1.16.2 |
| matplotlib | 3.11.1（尋光分頁即時軌跡圖用，選用相依，裝不到就停用該分頁，不影響其餘功能） |
| numpy | 2.5.2（matplotlib 的必要相依） |
| tkinter | Python 內建，無需安裝 |
| 控制器連線 | COM 埠 @ 38400 baud（**埠號會變**，靠 VID/PID `0DFD:0002` 認才可靠） |

實機組態（2026-08-06 實測）：韌體 `DS102 4.00`、4 軸（X/Y/Z/U），其中 **U 軸未接滑台**，實際可動的是 X/Y/Z 三軸。

---

## 功能

- **手動驅動** — 長按連續點動、定量步進、單軸原點復歸
- **全軸原點復歸** — 依各軸自己的 `MEMSW0` 樣式依序執行，並強制歸零座標
- **Teaching Point** — 儲存／載入／移動至指定座標組，支援工作座標偏置
- **行程錄製與重播** — 錄下驅動指令序列，可調整每步延遲或指定重播用的固定延遲
- **速度 Profile** — 命名儲存四參數（L0/F0/R0/S0）快速切換
- **軟體行程限制** — Python 端限位攔截（與控制器韌體端限位是兩套，見下方注意事項）
- **控制器設定持久化** — MEMSW 與韌體軟體限位是 RAM-only，斷電即失；存檔後連線時自動補回
- **軸機械校正參數** — 輸入每軸導程／步進角／分度值，把 pulse 座標額外估算成 μm 顯示（純參考，不影響任何移動/限位/教點判斷，內部永遠只認 pulse）
- **光功率監看** — HP 8153A GPIB 讀值，獨立分頁與浮動視窗
- **自動尋光** — 座標下降＋K 近鄰局部精修的對準演算法，可彈性勾選 1～6 軸參與搜尋，內建即時軌跡圖（可切換軸對 2D 投影＋多軸相對位移趨勢線）
- **實驗數據記錄** — 時間戳 + 各軸位置（含光功率 dBm）匯出 CSV
- **LOG** — 分級記錄與匯出

---

## 檔案定位

根目錄有多份相似的 DS102 程式，**改錯檔案是最常見的失誤**：

| 檔案 | 定位 |
|---|---|
| [main_ai.py](main_ai.py) | **唯一的主程式（v3.0）**，約 5408 行。GUI 與各分頁邏輯都加在這裡 |
| [ds102_ctrl.py](ds102_ctrl.py) | `DS102Controller` 本體（2026-08-17 從 main_ai.py 拆出的獨立模組，約 2326 行，完全不碰 tkinter）。不要跟下面的 `ds102_controller.py` 搞混 |
| [ds102_controller.py](ds102_controller.py) | main_ai.py 的前一版快照（跟上面的 `ds102_ctrl.py` 是完全不同的兩個檔案）。可作對照，**不要在此新增功能** |
| [main.py](main.py) | 廠商官方範例，是**指令格式的權威來源**。修改指令前先回頭比對 |
| [test.py](test.py) | 無 GUI 的連線／狀態查詢腳本。名稱誤導——不是單元測試 |
| [probe_ds102.py](probe_ds102.py) | 序列埠診斷工具，硬體接不上時的第一站 |
| [meter_GPIB.py](meter_GPIB.py) | HP 8153A 光功率計封裝，已整合進「光功率」／「尋光」分頁，尚未接上真實儀器驗證 |
| [fiber_scanner.py](fiber_scanner.py) | `FiberAlignmentScanner`，光纖對準尋光演算法，已接上「尋光」分頁 |
| [verify_scan_tab.py](verify_scan_tab.py) / [verify_meter_panel.py](verify_meter_panel.py) / [verify_axis_calib.py](verify_axis_calib.py) / [verify_fiber_scanner_signal.py](verify_fiber_scanner_signal.py) | 「尋光」／「光功率」分頁／軸機械校正參數／`fiber_scanner.py` 訊號有效性判準的假物件回歸測試（57／66／50／14 項，共 187 項，pytest 測試檔，`python -m pytest verify_scan_tab.py verify_meter_panel.py verify_axis_calib.py verify_fiber_scanner_signal.py -v` 執行，或用 VS Code Testing 面板，不需硬體） |
| [Gtest.py](Gtest.py) | 外部第三方範例，`import control` 的模組不存在於本 repo，**無法執行** |
| [step-motor.txt](step-motor.txt) | 三層架構藍圖。其中 DS112 通訊細節（`\r\n`、9600、`!:` 輪詢）**全部是錯的** |

---

## main_ai.py 架構

邏輯上仍是三塊，但 `DS102Controller` 現在實際定義在獨立檔案 [ds102_ctrl.py](ds102_ctrl.py)：

1. **`DS102Controller`**（`ds102_ctrl.py`）— 所有序列通訊集中於此，完全不碰 tkinter，main_ai.py 用 `from ds102_ctrl import DS102Controller, ...` 引入
2. **`StatusBar`**（main_ai.py）— 各分頁共用的座標／連線狀態列
3. **`DS102GUI`**（main_ai.py）— 七個分頁（儀表板 / 移動控制 / Teaching / 行程錄製 / 光功率 / 尋光 / LOG），只呼叫 controller 的公開方法

六條輪詢迴圈各司其職：

| 迴圈 | 執行緒 | 節奏 | 職責 |
|---|---|---|---|
| `_start_poller()` | Tk 主執行緒 | 100ms | 只重繪快取座標，不碰序列埠 |
| `_start_position_worker()` | 背景 | 0.5s | 對各軸送 `POS?` 回寫位置 |
| `_poll_status()` | 背景 | 100ms | 移動中追蹤選取軸的狀態 |
| `_watch_jog_limit()` | 背景 | 0.06s | 長按點動時監看軟體限位 |
| `_start_meter_poll_worker()` | 背景 | 0.5s | 光功率背景輪詢（GPIB，與序列埠通訊無關） |
| `_redraw_scan_plot()` | Tk 主執行緒 | 250ms | 只重繪尋光即時軌跡圖，不觸發量測或移動 |

---

## 開發注意事項（踩過的坑）

完整說明見 [CLAUDE.md](CLAUDE.md)，以下是最容易出事的幾條：

### 硬體

- **座標 0 幾乎就落在限位開關上。** 任何「移動到 0」的操作實際語意是「把該軸推去撞端點」。設計預設值務必避開 0。
- **`MEMSW` 與韌體軟體限位都是 RAM-only**，控制器斷電後整組回到出廠值。`MEMSW0=0` 的語意是「復歸樣式 Type0＝不執行」，所以斷電後按「全軸原點復歸」會把每一軸都略過。用「控制器設定」的存檔／還原功能因應。
- **`GO ORG` 完成後 POS 不會停在 0。** 實測各軸有固定殘差（X≈0～1、Y≈+7～8、Z≈−6～−8 pulse）再疊加 ±1～2 pulse 的機械重現性。程式會在復歸後強制寫 `POS 0`。
- **`PULS` 不接受帶小數點的值，而且失敗時完全靜默**（`PULS 500.0000` 位移 0 且不報錯）。送出前一律取整數。

### 程式

- 單位**一律 pulse**，沒有 um / mm 切換——但可以在「移動控制」分頁輸入每軸機械參數，額外附加估算的 μm 顯示（見上方〈功能〉，純參考不影響內部判斷）。
- 這台滑台的驅動器是 **AMS（微步進）型**，細分設定要打開外殼用實體旋轉開關調，`AXI{n}:DRDIV?` 對它沒有意義（查回來的只是控制器內部一個沒人寫過的軟體暫存器，跟實體開關無關）。
- 任何**會阻塞的序列操作**必須在背景執行緒；背景執行緒**絕不可直接碰 tkinter widget**，一律 `root.after(0, ...)` 回主執行緒。
- 序列埠交易受 `_serial_lock` 保護，一次 TX→RX 不可分割。`stop()` 與 `emergency_stop()` 刻意不受此限，避免等鎖延遲停止。
- `limit_direction()` 比對方向字串時**必須先判斷 `"CCW"`**——`"CCW"` 本身就含有 `"CW"`。
- 設定檔是整份寫回，沒先 `load_*()` 就 `save` 會清空既有內容（已發生過兩次，現已修正）。一律走 `_write_json_with_backup()`。
- `app_settings.json`（UI 節奏／色票）與 `safety_settings.json`（`WAIT_TIMEOUT`／`STOP_LOCK_TIMEOUT` 等安全相關時序常數）是另一類設定檔：維護人員手動編輯、程式只讀不寫，不重編就能調參數。兩者刻意分開機制——後者驗證更嚴格（型別+範圍雙重檢查），改壞一律拒絕退回內建預設值，不做 clamp。

---

## 執行期產出（皆已 gitignore）

這三個目錄建在**程式所在位置**（不是目前工作目錄），所以從哪裡啟動都指向同一份資料：

| 路徑 | 內容 |
|---|---|
| `logs/` | 每次啟動一個檔；關閉時另存 `*_history.txt` |
| `recordings/` | 錄製的行程；同目錄的 `teaching_points.json`、`speed_profiles.json`、`controller_config.json`、`meter_config.json`、`scanner_config.json`、`app_settings.json`、`safety_settings.json`、`axis_calibration.json` 是設定檔 |
| `data/` | 實驗數據 CSV |

---

## 打包成 exe

venv 內已裝 `pyinstaller` 與 `auto-py-to-exe`。

```bash
venv/Scripts/pyinstaller.exe --onedir --windowed --name DS102 main_ai.py
```

**2026-08-17 已實際打包驗證過一次**：build 乾淨完成（matplotlib TkAgg backend 自動偵測），產出約 152MB，`DS102.exe` 能正常啟動、存活，在自己目錄下建出 `logs/`/`recordings/`/`data/`。**沒有實測連硬體**（GPIB／序列埠），那部分仍待驗證。

- **用 `--onedir` 而非 `--onefile`** — 未簽章的 onefile exe 會自解壓縮到 temp，行為特徵與 packer 相同，是防毒誤判的典型目標；這台機器的 SentinelOne 有前科（見 [DRIVER_ISSUE_REPORT.md](DRIVER_ISSUE_REPORT.md)）。onedir 也省掉每次啟動的解壓時間。
- **程式必須放在有寫入權限的位置**（桌面、`D:\` 等），不要放 `Program Files`——它需要在自己的目錄下建 `logs/` `recordings/` `data/`。權限不足時會跳錯誤視窗說明，不會無聲關閉。
- 不需要把 `ds102 (2).pdf` 或驅動資料夾打包進去，執行期用不到。
- 沒有單一實例保護：兩個 exe 同時執行會搶同一個 COM 埠。
- `DS102Controller` 拆到獨立檔案 `ds102_ctrl.py` 後仍是靜態 `from ds102_ctrl import ...`（同目錄 sibling import，跟 `fiber_scanner.py`／`meter_GPIB.py` 的匯入方式一樣），PyInstaller 能自動收進去，不需要額外的 hidden-import 宣告。

---

## 硬體連不上時

1. `venv/Scripts/python.exe probe_ds102.py --list` — 只列序列埠，不送任何指令（不會動到硬體）
2. `venv/Scripts/python.exe probe_ds102.py` — 逐埠輪詢 `*IDN?`，依序試 38400 → 19200 → 9600 → 4800
3. 若裝置管理員出現 **Problem Code 10 / `STATUS_DRIVER_BLOCKED`**，看起來像簽章問題但不是——見 [DRIVER_ISSUE_REPORT.md](DRIVER_ISSUE_REPORT.md)。該文件已逐條排除驅動、簽章、WDAC、HVCI、Secure Boot，**不要重複排查這些**，直接請 IT 在 SentinelOne Device Control 政策核可 `VID_0DFD&PID_0002`。

---

## 參考文件

- `ds102 (2).pdf` — DS102/DS112 Operation Manual Ver 2.00（170 頁），**指令表的權威來源**，未進版控
- [CLAUDE.md](CLAUDE.md) — 給 AI 助理的專案指引，同時是最完整的開發筆記
- [DRIVER_ISSUE_REPORT.md](DRIVER_ISSUE_REPORT.md) — USB 驅動被封鎖的排查紀錄
