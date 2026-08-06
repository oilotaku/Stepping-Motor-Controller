# DS102 / DS112 步進馬達控制器

Windows 桌面應用，用 Python + tkinter 控制 **駿河精機 SURUGA SEIKI DS102 / DS112 步進馬達控制箱**（RS-232C / USB 虛擬 COM 埠），用於光纖對準與光學自動化量測。

長期目標是把滑台與 **HP 8153A 光波萬用表**（GPIB）串起來做自動掃描尋光，目前光功率計的封裝（[meter_GPIB.py](meter_GPIB.py)）尚未與馬達程式整合。

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

**沒有硬體時**：`DS102Controller.connect_sim()` 提供模擬模式，會用假造回應走完整個 UI 流程與錄製重播。

---

## 環境

| 項目 | 版本 / 值 |
|---|---|
| Python | 3.14.4（venv 內） |
| pyserial | 3.5 |
| PyVISA | 1.16.2 |
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
- **實驗數據記錄** — 時間戳 + 各軸位置匯出 CSV
- **LOG** — 分級記錄與匯出

---

## 檔案定位

根目錄有多份相似的 DS102 程式，**改錯檔案是最常見的失誤**：

| 檔案 | 定位 |
|---|---|
| [main_ai.py](main_ai.py) | **唯一的主程式（v3.0）**，約 4000 行。功能與修正都加在這裡 |
| [ds102_controller.py](ds102_controller.py) | main_ai.py 的前一版快照。可作對照，**不要在此新增功能** |
| [main.py](main.py) | 廠商官方範例，是**指令格式的權威來源**。修改指令前先回頭比對 |
| [test.py](test.py) | 無 GUI 的連線／狀態查詢腳本。名稱誤導——不是單元測試 |
| [probe_ds102.py](probe_ds102.py) | 序列埠診斷工具，硬體接不上時的第一站 |
| [meter_GPIB.py](meter_GPIB.py) | HP 8153A 光功率計封裝，尚未整合 |
| [Gtest.py](Gtest.py) | 外部第三方範例，`import control` 的模組不存在於本 repo，**無法執行** |
| [step-motor.txt](step-motor.txt) | 三層架構藍圖。⚠ 其中 DS112 通訊細節（`\r\n`、9600、`!:` 輪詢）**全部是錯的** |

---

## main_ai.py 架構

單檔，嚴格分成三塊：

1. **`DS102Controller`** — 所有序列通訊集中於此，完全不碰 tkinter
2. **`StatusBar`** — 各分頁共用的座標／連線狀態列
3. **`DS102GUI`** — 五個分頁（儀表板 / 移動控制 / Teaching / 行程錄製 / LOG），只呼叫 controller 的公開方法

四條輪詢迴圈各司其職：

| 迴圈 | 執行緒 | 節奏 | 職責 |
|---|---|---|---|
| `_start_poller()` | Tk 主執行緒 | 100ms | 只重繪快取座標，不碰序列埠 |
| `_start_position_worker()` | 背景 | 0.5s | 對各軸送 `POS?` 回寫位置 |
| `_poll_status()` | 背景 | 100ms | 移動中追蹤選取軸的狀態 |
| `_watch_jog_limit()` | 背景 | 0.06s | 長按點動時監看軟體限位 |

---

## 開發注意事項（踩過的坑）

完整說明見 [CLAUDE.md](CLAUDE.md)，以下是最容易出事的幾條：

### 硬體

- **座標 0 幾乎就落在限位開關上。** 任何「移動到 0」的操作實際語意是「把該軸推去撞端點」。設計預設值務必避開 0。
- **`MEMSW` 與韌體軟體限位都是 RAM-only**，控制器斷電後整組回到出廠值。`MEMSW0=0` 的語意是「復歸樣式 Type0＝不執行」，所以斷電後按「全軸原點復歸」會把每一軸都略過。用「控制器設定」的存檔／還原功能因應。
- **`GO ORG` 完成後 POS 不會停在 0。** 實測各軸有固定殘差（X≈0～1、Y≈+7～8、Z≈−6～−8 pulse）再疊加 ±1～2 pulse 的機械重現性。程式會在復歸後強制寫 `POS 0`。
- **`PULS` 不接受帶小數點的值，而且失敗時完全靜默**（`PULS 500.0000` 位移 0 且不報錯）。送出前一律取整數。

### 程式

- 單位**一律 pulse**，沒有 um / mm 切換。
- 任何**會阻塞的序列操作**必須在背景執行緒；背景執行緒**絕不可直接碰 tkinter widget**，一律 `root.after(0, ...)` 回主執行緒。
- 序列埠交易受 `_serial_lock` 保護，一次 TX→RX 不可分割。`stop()` 與 `emergency_stop()` 刻意不受此限，避免等鎖延遲停止。
- `limit_direction()` 比對方向字串時**必須先判斷 `"CCW"`**——`"CCW"` 本身就含有 `"CW"`。
- 設定檔是整份寫回，沒先 `load_*()` 就 `save` 會清空既有內容（已發生過兩次）。一律走 `_write_json_with_backup()`。

---

## 執行期產出（皆已 gitignore）

| 路徑 | 內容 |
|---|---|
| `logs/` | 每次啟動一個檔；關閉時另存 `*_history.txt` |
| `recordings/` | 錄製的行程；同目錄的 `teaching_points.json`、`speed_profiles.json`、`controller_config.json` 是設定檔 |
| `data/` | 實驗數據 CSV |

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
