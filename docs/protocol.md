# DS102 通訊協定重點

> 本文件自 CLAUDE.md 拆出（2026-08-26），目的是縮小每次對話的固定載入量。
> **內容未經刪減**，動到對應功能前請完整讀過本檔。

## DS102 通訊協定重點

**權威參考:`ds102 (2).pdf`**(170 頁,DS102/DS112 Operation Manual Ver 2.00,倉庫根目錄)。查指令前先翻它,不要靠猜——實測發現韌體對不存在的指令會**回傳看似合理的值**(例如 `LIMIT?` 回 `2`、`SOFTLIMIT?` 回 `0`),但這些都不在手冊的 Inquiry Command 表裡。第 4.3.4 節之後是完整指令表,查詢指令集中在 `＜Inquiry Command＞`(約 p.132)。

常用查詢(手冊縮寫,大寫字母不可省):

| 指令 | 用途 |
|---|---|
| `AXI{n}:CWSLP?` / `CCWSLP?` | 軟體限位**座標** |
| `AXI{n}:CWSLE?` / `CCWSLE?` | 軟體限位啟用(0=停用) |
| `AXI{n}:RESOLUT?` | 1 pulse 的距離 = `STANDARD?` ÷ 分割數。手冊第 94 頁〈Unit Set〉證實 `RESOLUT?`＝`1` 的原因：真正代表機械行程的是 `SD`（馬達整步時的機械位移量，螺桿導程相關），要透過 DT100 手持終端機或控制軟體手動輸入，這台機器從未設定過——不是控制器的 bug，是沒人填過這個值 |
| `AXI{n}:DRDIV?` | 驅動器分割(0=full step…15=1/250)。官方指令名 `:DRiverDIVision?`/`:DRDIV?`，查證來源 `ds102 (2).pdf` 第 131 頁〈Inquiry Command〉表。**對這台滑台裝的 AMS（微步進）型驅動器沒有意義，`:DRDIV` 指令對它完全不生效**——手冊第 73-75 頁〈3.5 Driver division number setting〉明講：Normal 型驅動器才能用手持終端機／軟體／通訊指令切換 FULL/Half；**Micro step 型驅動器要打開外殼、用螺絲起子調驅動器上的實體旋轉開關（DATA1）**，控制器沒有電路能讀回這顆開關的實際位置。2026-08-18 實機驗證：使用者把實體開關轉到 6，`DRDIV?` 依然回 `0`——因為查詢到的只是控制器內部一個獨立的軟體暫存器（預設 `0`），跟實體開關完全沒有連動，這是驅動器硬體設計本身如此，不是查詢邏輯錯誤。實體開關（DATA1）與軟體 `DRDIV?`／`:DRDIV` 是**同一套 0～F(15) 編號、對照表完全一致**（第 75 頁表格逐列以「步進角 = 0.72°÷分割數」驗算過，例如 `6=1/10`：0.72÷10=0.072° 吻合）：`0=1/1(Full) 1=1/2 2=1/2.5 3=1/4 4=1/5 5=1/8 6=1/10 7=1/20 8=1/25 9=1/40 A=1/50 B=1/80 C=1/100 D=1/125 E=1/200 F=1/250`（這張表第一版用 `pdftotext -layout` 擷取時欄位對錯位，誤植成「差一位」，後來改用 `pdftotext -table` 重新擷取並逐列驗算才發現，查 PDF 表格前**兩種擷取模式都跑一次交叉比對比較保險**）。`connect()` 會查一次存進 `ctrl.axis_drdiv: Dict[str, str]`（GUI 頂部與 LOG 顯示），**這顆值目前對這台機器而言只是「軟體暫存器內容」，不代表實際細分設定**，pulse→um 換算不能拿它當依據——真的要換算請用〈軸機械校正參數〉（見 [axis-calibration.md](axis-calibration.md)）那組使用者手動輸入的 `axis_calib.division`，〈單位與座標〉（見 [CLAUDE.md](../CLAUDE.md)）一節 |
| `AXI{n}:PULSA?` / `HOMEP?` | 絕對驅動座標 / Home 座標 |
| `TCH00?`～`TCH63?` | 控制器**內建 64 組 teaching point** |

**機械限位(實體開關)的座標無法查詢**,手冊沒有這種指令;只能開到限位再讀 `POS?`。而且 `POS` 是相對暫存器,原點復歸會重設,所以穩定的量是兩端之差(行程)而非絕對值。

**DATA1 微步距已驗證生效（2026-08-21）**：使用者重新實測，將 DATA1 從 Full-step 調整為 1/10 並重開機，固定 pulse 數移動同一軸，實際移動距離確實等比例縮短——與公式預期（division 加大、同樣 pulse 數走的距離等比例變短，〈軸機械校正參數〉（見 [axis-calibration.md](axis-calibration.md)））一致，取代 2026-08-18 當時「感覺沒有變少」的疑慮。先前那次異常判讀的原因未明（可能是觀察誤差或當時的對比不夠極端），未進一步追查，也不影響這次結論。第二顆「division changing-over switch」（R1/R2）的實際位置仍未確認（PDF 裡是圖片、文字擷取工具讀不到），但已不影響判斷——DATA1 本身確定有生效，`axis_calib.division` 換算公式可信。

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
- **移動後不可立刻讀值**：`_wait_axis_stop()` 以 `WAIT_INTERVAL`(0.5s) 輪詢，逾時 `WAIT_TIMEOUT`(30s)；原點復歸改用 `_wait_origin_done()`（只看 Driving 位元，逾時 180s）。注意 `_wait_axis_stop` 的續輪條件只有 `Driving`，其他狀態（Limit／通訊錯誤／軸無法選取）一律 `return False`——2026-08-05 之前搭配「沒人看回傳值」，實際語意是「撞限位＝立刻放棄等待且不通知任何人」。2026-08-26 修〈孿生競態〉（見 [homing.md](homing.md)）後多了一個例外：**送出 GO 之後的 `MOVE_START_GRACE`(1.0s) 寬限期內、且完全沒有位移證據時，非 Driving 狀態（含限位）會續輪而不是立刻下結論**——那一刻無法分辨「GO 尚未生效」與「真的停好／真的撞上」，〈原點復歸〉（見 [homing.md](homing.md)）末尾那段。寬限期之後的判定與改動前逐字相同。若之後要接光功率量測，停穩後還需再等約 30ms 讓機構震動衰減。
- `limit_direction()` 從狀態字串判斷壓在哪一側限位時，**必須先判斷 `"CCW"`**——`"CCW"` 字串本身就含有 `"CW"`，順序反了會把 CCW 限位全部誤判成 CW。任何新增的方向字串比對都有同一個陷阱。
- HP 8153A 側（[meter_GPIB.py](meter_GPIB.py)）：SCPI 指令結尾 `\n`（由 pyvisa `write_termination` 預設附加，不是手寫的）。連續通訊之間需 `time.sleep(0.03~0.05)` 否則 GPIB 緩衝區溢位會出現 Query INTERRUPTED——但**目前 `meter_GPIB.py` 全檔沒有任何 `time.sleep`**（`import time` 是未使用的 import）。這是「整合時必須補上」的待辦，不是既有實作，別去該檔找對應程式碼。
