# 光纖對準尋光演算法 — 設計文件

2026-08-07。彙整 `mathematician` 代理兩輪演算法設計、`architect` 代理的落地評估，以及依此完成的無硬體實作（[fiber_scanner.py](fiber_scanner.py)）。尚未接上 HP 8153A，所有跟真實光學響應有關的參數都待實測校準——本文件會明確標出哪些是「已驗證的邏輯」、哪些是「起跳用的假設值」。

## 目標與背景

長期目標（見 [step-motor.txt](step-motor.txt)）：把本程式控制的 DS102 滑台與 HP 8153A 光功率計（[meter_GPIB.py](meter_GPIB.py)）串起來，做自動掃描尋光，用於光纖對準。要求**軸數從 1 到 6 都能用同一套演算法**——目前實機只有 X/Y/Z 三軸可動，U 軸未接滑台，但程式的六軸命名（`AXES = ["X","Y","Z","U","V","W"]`）已預留擴充空間。

## 核心演算法：三階段混合

| 階段 | 方法 | 何時用 |
|---|---|---|
| 一 | 座標下降式模式搜尋 | 一律執行，全域粗定位 |
| 二 | K 近鄰局部加權迴歸精修 | **按需啟用**，處理軸間耦合 |
| 三 | ±最小步長收尾微擾 | 一律執行，抓格點誤差 |

### 為什麼是這個組合（mathematician 兩輪討論的結論）

第一輪設計座標下降：對「實際可動的軸清單」逐一輪替，每軸做「方向探測 → 定步長爬坡 → 三點拋物線內插精修 → 步長減半」。量測次數 O(軸數 × log(行程/步長下限))——**線性**於軸數。

第二輪使用者提出「改用 K 近鄰擴散」作為核心策略。mathematician 給出**不利但誠實**的結論：純 K 近鄰擴散**不建議**作為全域搜尋的主要方法，尤其 4～6 軸時。理由是維度詛咒——要讓 K 近鄰的「鄰居」真的算「近」，即使只要求每軸切 5 段的粗解析度，樣本量是 O(5^軸數)：3 軸 125 點起，**6 軸 15,625 點起**，以每次移動+量測 0.5~1s 估算，6 軸光初始化就要 2~4.3 小時。稀疏取樣版本雖然點數少，但「鄰居」在高維度下其實一點都不近，梯度估計不可信。

**結論**：兩者沒有相互替代關係，是互補的兩階段。座標下降負責「先大致找到範圍」（便宜、線性），K 近鄰負責「小盒子內處理殘餘耦合」（局部性假設在小範圍內才站得住腳）。這正是最終採用的三階段設計。

## architect 的落地評估與已解決的缺口

architect 讀過 `main_ai.py` 的實際程式碼後，指出三個**必須在寫程式碼前定案**的缺口，全部已在實作中處理：

| 缺口 | 落地方案 |
|---|---|
| 現有背景輪詢（`_start_position_worker`）會跟演算法搶 `_serial_lock` | 新增 `scanning_active` 旗標，比照 `playback_running` 的既有模式排除 |
| DS102 沒有單軸停止指令，階段二多軸同時出發時一軸失敗如何收尾 | `_move_multi_axis()`：批次預檢全過才送出，任一軸失敗就整批 `stop()`——這是候選點失敗，不是緊急事件 |
| `meter_GPIB.py` 零例外處理、零節流，撐不住自動化迴圈 | 補上 `VisaIOError` 處理、GPIB 節流、`(ok, value)` 二元組取代容易混淆的 sentinel 值 |

## 這個新功能長在哪裡

獨立類別 `FiberAlignmentScanner`（[fiber_scanner.py](fiber_scanner.py)），不掛在 `DS102Controller` 也不掛在 GUI：

- `DS102Controller` 的定位是「只管序列通訊」，混入 GPIB 依賴會破壞分層。
- `DS102GUI` 的規則是「只呼叫 controller 公開方法」，把跑幾分鐘、含決策邏輯的演算法焊進 Tk 事件處理會鎖死主執行緒。
- `power_query` 透過依賴注入解耦 GPIB，測試時傳入合成功率函式即可，不需要真實硬體。
- `fiber_scanner.py` **刻意不 import main_ai.py**：main_ai.py 未來若要接 GUI 會 import 這個檔案，若這裡也 import main_ai 會構成循環相依。軸名/軸號對應（`AXES`/`AXIS_NO`/`NO_AXIS`）因此在本檔自成一份，須與 main_ai.py 保持同步。

## `DS102Controller` 的安全整合（[main_ai.py](main_ai.py)）

- `scanning_active: bool` — 掃描期間鎖定其他移動來源。
- `move_step` / `move_continue` / `move_origin` / `origin_all` / `goto_point` 的守衛加上 `or self.scanning_active`。
- `_start_position_worker` 排除條件加上 `and not self.ctrl.scanning_active`。
- `check_sw_limits_batch(targets)` — 一次性檢查多軸目標座標，全過才能送出任何一軸的 GO。
- `wait_axis_stop()` — `_wait_axis_stop()` 的公開版本，供 scanner 在多軸批次流程中使用。
- `positions_machine` — 公開的機械座標存取器（`positions` 屬性回傳的是扣除 offset 的工作座標，限位比對用的是機械座標，兩者不可混用）。

### 🔴 實作時發現並修正的 bug：`scanning_active` 自我阻擋

第一版把 `scanning_active` 直接加進 `move_step` 的守衛，結果**演算法呼叫自己的移動也被自己設的旗標擋住**——無硬體驗證的第一輪測試裡，所有收斂測試都卡在座標 0 不動，`_search_axis_once` 每次移動都回傳 False。

`scanning_active` 的用途是擋「其他來源」（GUI 手動操作、背景輪詢），不該連 scanner 自己也擋。修法：把 `move_step` 拆成三段——

- `_do_move_step()`：實際邏輯（限位檢查、PULS 格式化、序列寫入、等待到位），不含守衛判斷。
- `move_step()`：GUI／使用者操作用，守衛含 `scanning_active`。
- `scan_move_step()`：`FiberAlignmentScanner` 專用，只受 `ems_active` 攔截。

這個 bug 與修法本身就是「無硬體驗證能提前抓到什麼」的具體例子——如果沒有先跑合成資料測試，這個問題會一路留到接上真實硬體才被發現。

## 演算法細節

### 階段一：座標下降

```
for cycle in 1..max_cycles:
    step = 第 1 輪用 initial_step；第 2 輪起用 step_min × REOPEN_STEP_MULT
    for axis in active_axes:
        while step[axis] >= step_min:
            converged, power = search_axis_once(axis, step[axis])
            if converged: step[axis] //= 2
    if 本輪總改善 < 雜訊底限: break
```

`search_axis_once` 單軸尋峰子程序：方向探測（兩側各試一次）→ 定向爬坡（固定步長走到不再上升）→ 三點拋物線內插精修（四捨五入回整數 pulse，內插沒有實際更好就退回格點最大值）。

⚠ **對 mathematician 原始虛擬碼的修正**：原虛擬碼裡 `step[axis]` 只在迴圈外初始化一次，一旦某軸在第 1 輪收斂到 `step_min` 以下，後續所有輪次對該軸的 while 迴圈都不會再執行——這樣「多輪 cycle 修正耦合」的設計目的實際上達不到。實作改成每輪開頭重新給一個較小的起始步長（`step_min × REOPEN_STEP_MULT`），讓每個軸在每一輪都有機會被重新優化，同時不必付出「每輪都從最大步長開始」的全額成本。

### 階段二：K 近鄰局部精修（按需啟用）

初始星形設計取樣（每軸 ±1 臂點）→ 用目前位置附近 K 個最近樣本做加權線性迴歸估計梯度（距離依 `axis_scale` 正規化、高斯核加權）→ 沿梯度方向移動（trust-region 式步長調整）→ 驗證是否真的更好，沒有就退回並縮步。

梯度估計用手刻加權最小平方（[fiber_scanner.py](fiber_scanner.py) 的 `_weighted_least_squares` / `_solve_linear_system`），**不依賴 numpy**——維持零額外相依套件，矩陣最大 7×7（截距+6軸）不需要優化的線性代數函式庫。

多軸同時移動走 `_move_multi_axis()`：全部送出 GO（`wait_done=False`）→ 依序等每一軸到位 → 任一軸失敗就整批 `stop()`。

### 階段三：收尾微擾

對每軸做 ±`step_min` 微擾，抓階段二因座標取整可能錯過的鄰近格點最大值。

### 收斂判準

兩層，缺一不可：

1. **步長下限** `step_min`：需 ≥ 機械重現性下限 ±1~2 pulse（CLAUDE.md〈原點復歸〉的實測數據）。
2. **功率雜訊底限**：`calibrate_noise()` 在目前位置重複量測估計標準差，`noise_sigma_mult × σ` 作為「真的更好」的門檻——低於這個差值視為雜訊，不算改善。

### 動態調速（2026-08-07 新增）：位移量越小、驅動速度越低

`_dynamic_speed(delta_pulse)`：位移量在 `[speed_scale_pulses]` 範圍內線性內插，兩端夾在 `f_speed_min` / `f_speed_max`。所有移動（`_move_relative` 單軸、`_move_multi_axis` 多軸批次，每軸各自依自己的位移量計算）都改用這個動態值，不再用建構子固定傳入的速度。

**理由**：DS102 的 `R0`（加減速時間）是固定的**時間**、不是固定的**距離**。位移量小時若仍用大位移的高速 `F0`，相對加速度會更劇烈——超過馬達可用扭矩就有失步風險。DS102 是**開迴路控制，沒有編碼器回授**，失步不會有任何訊號，`_positions_pulse` 會悄悄跟真實座標脫節，之後所有收斂判斷都建立在錯誤的座標基準上。降速是用時間換可靠度，成本低、方向正確，但**這只是降低失步機率，不是偵測失步**——偵測本身是另一個獨立的題目，尚未著手。

⚠ 這是保守的線性內插起跳值，不是實測校準過的失步安全邊界。DS102 實際的扭矩-轉速曲線需要真實硬體才量得出來（見下方待實測參數）。

## 樣本持久化

`persist_samples()` 在 `run()` 的 `finally` 一次性寫出（不逐筆即時寫檔——hot loop 裡做磁碟 I/O 會拖慢緊繃的量測預算）。**中止／例外時也要寫**，這是唯一能回答「跑到哪裡出問題」的資料來源。寫入方式仿照 `_write_json_with_backup()`：先寫 `.tmp` 再 `replace`。預設輸出到 `recordings/scans/`（以程式所在位置為基準，比照 `main_ai.py` 的 `_app_dir()` 邏輯，見 [CLAUDE.md](CLAUDE.md)〈執行期目錄與啟動流程〉）。

## 驗證方式（無硬體）

`FiberAlignmentScanner` 的 `power_query` 完全依賴注入，測試用假 serial 物件（追蹤各軸座標、回應 DS102 協定的 SB3?/SB1?/POS?）+ 合成高斯功率曲線取代真實 HP 8153A。共 40 項測試，涵蓋：

- 單軸／三軸座標下降精確收斂到合成峰值
- `_active_axes()` 正確排除未接滑台的軸
- 多軸批次移動：全部通過時同時到位；一軸超限時整批不出發且送出 STOP
- `scanning_active` 期間 GUI 操作全部拒絕，但 `scan_move_step` 仍可動作
- EMS／使用者中止都正確標記 `completed=False` 並保留樣本
- `_weighted_least_squares` 精確還原已知線性關係
- 階段一+階段二混合仍能收斂

## 實作路線圖

| 步驟 | 狀態 | 是否需要硬體 |
|---|---|---|
| 補 `meter_GPIB.py` 地基（節流、例外處理、sentinel 契約） | ✅ 已完成 | 不需要 |
| `DS102Controller` 的 `scanning_active` 安全整合 | ✅ 已完成 | 不需要 |
| `FiberAlignmentScanner` 三階段演算法 | ✅ 已完成 | 不需要 |
| 假 serial + 合成功率曲線驗證收斂性與安全邏輯 | ✅ 已完成（40 項測試） | 不需要 |
| 假 GPIB ＋真滑台，驗證執行緒與鎖的實際延遲 | ✅ 已驗證（2026-08-07，COM2） | 已完成 |
| 真滑台小範圍驗證多軸批次收尾（比照 `origin_all` 用韌體軟體限位模擬撞限位） | ⬜ 待執行 | **需要滑台** |
| 接真實 HP 8153A，校準下方所有待實測參數 | ⬜ 待執行 | **需要光功率計** |

### 實機驗證紀錄（2026-08-07，COM2）

用真滑台 + 合成 2D 高斯功率函式（假光源，峰值設在 X=400、Y=−400，程式不告訴演算法答案）跑完整的階段一＋階段三：

- **收斂結果**：找到 X=399、Y=−399，誤差各 1 pulse——正好落在機械重現性下限（±1~2 pulse），即物理極限內最準的答案。
- **共 85 次量測、86.6 秒**（X/Y 各約 900 pulse 的搜尋窗）。
- **軟體限位正確攔截了一次危險探測**：演算法方向探測階段嘗試 Y 軸 +方向（`GO CW`），該方向正好是 Y 當時壓著的限位——`check_sw_limits_batch`/`_check_sw_limit` 在送出任何序列指令前就攔下，滑台完全沒有真的往那個方向動，演算法自動改往唯一可行的方向搜尋。
- **第 2 輪的「重開步長」機制正確運作**：兩軸都已收斂時，本輪總改善量為 0，提前跳出 cycle 迴圈，沒有浪費時間重複已經確認過的區域。

這次驗證是在真滑台上進行、沒有真實光學訊號，所以**沒有驗證到的部分**是：真實 GPIB 通訊延遲與節流、真實功率讀值的雜訊特性、真實響應曲線是否符合高斯假設——這些仍待接上 HP 8153A 才能校準。

## 待實測參數（現在給不了絕對數值，都是起跳用的保守預設）

| 參數 | 目前預設 | 待實測項目 |
|---|---|---|
| `step_min`（最小步長） | 2 pulse | 需高於機械重現性下限，也要看功率讀值在這個尺度下是否還能分辨訊號 |
| `settle_sec`（震動衰減等待） | 0.03s | CLAUDE.md 記載約 30ms，待微調 |
| GPIB 節流間隔 | 0.03s | `meter_GPIB.py` 的 `GPIB_THROTTLE_SEC`，待接上真實儀器校準 |
| `noise_sigma_mult` | 3.0 | 功率雜訊標準差的倍數門檻 |
| 初始步長 `initial_step` | 呼叫端提供，無內建假設 | 需要先掃過全行程量出真實峰寬（FWHM）才能訂 |
| 軸間耦合強度 | 未知 | 決定是否需要啟用階段二的唯一依據 |
| `axis_scale`（階段二距離正規化） | 未提供則不正規化 | 真正的物理尺度需要階段一的副產品才能反推 |
| `f_speed_min`（動態調速下限） | `max(50, f_speed / 5)` | DS102 的扭矩-轉速曲線與實際可靠加減速範圍，需要真實硬體才量得出來 |
| `speed_scale_pulses`（動態調速的位移量範圍） | `(step_min, step_min × 32)` | 「多小的位移算小位移」本身也是待校準的門檻，現在只是跟 `step_min` 掛勾的粗略比例 |
| `MAX_CYCLES` / 階段二 `max_iterations` | 5 / 20 | 依實測收斂速度調整 |
| 是否存在旁瓣（sinc 式次峰） | 未知 | 需要先掃一次全行程功率曲線才能判斷 |

## 尚未做的事（有意識的範圍界定）

- **GUI 整合**（開始/停止按鈕、進度顯示）——依 CLAUDE.md 的子代理分工，新增 GUI 元件應先過 `ui-designer`，這輪刻意不做。
- **`meter_GPIB.py` 的實際連線驗證**——目前的修正是靜態程式碼審查＋邏輯正確性，`VisaIOError` 處理路徑本身沒有真實儀器可以觸發驗證。
- **軟體限位與韌體限位在掃描期間的交互**——階段二的批次移動走 `check_sw_limits_batch`（Python 端），韌體端的限位（`CWSLE`）目前維持出廠預設（依使用者先前的選擇未啟用），這件事在正式跑真實掃描前需要重新確認。
