# 光纖對準尋光演算法 — 設計文件

最初於 2026-08-07 彙整 `mathematician` 代理兩輪演算法設計、`architect` 代理的落地評估，以及依此完成的無硬體實作（[fiber_scanner.py](fiber_scanner.py)）。**2026-08-19 更新**：補上 2026-08-12 的光功率整合（訊號有效性判準）、2026-08-17 該判準的兩個 bug 修正、2026-08-17 的 GUI 整合（`main_ai.py`「尋光」分頁，六階段＋architect 完整審查）、2026-08-19 的尋光彈性選軸（1～6 軸）。**2026-09-02 更新**：補上 2026-08-26～2026-08-31 累積的內容——階段零盲搜與光功率計量程 bug 修正、撞限位反覆撞與階段一收斂修正、樣本 Excel 報表匯出、尋光分頁速度預設值調整、`gaussian_vector_sim.py` 數學模擬工具、殘差診斷與收尾曲率擬合微調，見下方新增的〈2026-08-26～2026-08-31 後續更新〉一節。

**仍未接上真實 HP 8153A。** `fiber_scanner.py` 的核心演算法（座標下降＋K 近鄰＋收尾微擾）已在真滑台用合成功率函式驗證過，但那是假光源；GUI 整合（main_ai.py「尋光」分頁）只做過假物件驗證，**沒有真機端到端跑過一次「按下開始尋光→完整跑完一輪」**（見下方〈GUI 整合〉與 HANDOVER.md）。跟真實光學響應有關的參數（`GPIB_THROTTLE_SEC`、`min_valid_power_dbm` 等）都還待實測校準——本文件會明確標出哪些是「已驗證的邏輯」、哪些是「起跳用的假設值」。

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

### 實作時發現並修正的 bug：`scanning_active` 自我阻擋

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

**對 mathematician 原始虛擬碼的修正**：原虛擬碼裡 `step[axis]` 只在迴圈外初始化一次，一旦某軸在第 1 輪收斂到 `step_min` 以下，後續所有輪次對該軸的 while 迴圈都不會再執行——這樣「多輪 cycle 修正耦合」的設計目的實際上達不到。實作改成每輪開頭重新給一個較小的起始步長（`step_min × REOPEN_STEP_MULT`），讓每個軸在每一輪都有機會被重新優化，同時不必付出「每輪都從最大步長開始」的全額成本。

### 階段二：K 近鄰局部精修（按需啟用）

初始星形設計取樣（每軸 ±1 臂點）→ 用目前位置附近 K 個最近樣本做加權線性迴歸估計梯度（距離依 `axis_scale` 正規化、高斯核加權）→ 沿梯度方向移動（trust-region 式步長調整）→ 驗證是否真的更好，沒有就退回並縮步。

梯度估計用手刻加權最小平方（[fiber_scanner.py](fiber_scanner.py) 的 `_weighted_least_squares` / `_solve_linear_system`），**不依賴 numpy**——維持零額外相依套件，矩陣最大 7×7（截距+6軸）不需要優化的線性代數函式庫。

多軸同時移動走 `_move_multi_axis()`：全部送出 GO（`wait_done=False`）→ 依序等每一軸到位 → 任一軸失敗就整批 `stop()`。

### 實作時發現並修正的 bug：階段二在有軸間耦合時完全失效（2026-08-07）

用可分離（軸間獨立）的合成高斯測試階段二時一直運作正常，直到改用**有意耦合**的測試函式（旋轉橢圓高斯，long axis 沿對角線、σ_major/σ_minor=3.3x）做「啟用 K 近鄰對比計時」的對照實驗，才發現階段二完全沒有幫助——兩輪跑出一模一樣的結果。追下去是**兩個獨立的 bug 疊在一起**：

1. **鄰居池共線**（`_estimate_gradient`）：一開始以為是同座標重複點把星形取樣點擠出 K 近鄰（加了去重），但去重後仍然失敗——階段一收斂末期在目前位置附近留下的樣本會集中在「同一座標、只有最後處理的那個軸在變化」（座標下降每輪只沿單一軸移動的必然結果，例如最近 6 個鄰居的 X 座標完全相同），這種鄰居集合對那個沒有變化的軸而言，設計矩陣的截距欄與該軸欄位線性相依，迴歸矩陣依然奇異——不是重複點造成的，是鄰居集合本身缺乏該軸方向的變異。真正的修法是**只用階段二自己收集的樣本做迴歸**（`_stage2_sample_start` 記錄階段二開始時的樣本索引，`_estimate_gradient` 只從這之後取鄰居），完全不碰階段一的歷史樣本——星形設計本來就刻意讓每軸都有 ±r 的取樣點，具備多軸變化，排除階段一歷史後鄰居池自然只剩這些有效點。
2. **星形設計找到更好的點卻沒有走過去**：星形設計對每個臂點量完就撤回中心（`_move_relative(ax, -sign*r)`），只把數值記進 `best_power`，工作位置卻沒有跟著移動。修掉 bug 1 之後梯度下降迴圈能正常估計方向了，但它算出的方向剛好指向星形設計已經探過的那個臂點——踩到同一個座標、量到同一個功率值，跟 `best_power` **打平**（不是更好），嚴格 `>` 判斷成「沒有改善」而撤回，`step_length` 跟著減半。星形設計已經找到的改善就這樣白白浪費，20 輪後 `step_length` 縮到 `step_min` 以下收工，位置停在原地。修法：星形設計結束後，若某個臂確實比中心好就**真的走過去**（`best_arm` 記錄該臂，收尾時 `_move_relative` 過去），讓梯度下降迴圈從那個已知更好的點接著往下找，而不是從一個「數字比較好但沒人站上去」的中心繼續。

兩個 bug 都是用假 serial + 合成函式的模擬抓到的，抓到才拿去跑真實硬體——這是這個 session 一路沿用的作法：先在模擬裡把邏輯錯誤榨乾，硬體時間只花在驗證，不花在除錯。修正後同一組合成函式（耦合強度 3.3x）下，純座標下降（階段一+三）誤差 208.4 pulse，加上階段二後誤差降到 26.4 pulse，多花 25 次量測（+18%）。48 項無硬體測試修正後全過。

### 階段三：收尾微擾

對每軸做 ±`step_min` 微擾，抓階段二因座標取整可能錯過的鄰近格點最大值。

### 收斂判準

兩層，缺一不可：

1. **步長下限** `step_min`：需 ≥ 機械重現性下限 ±1~2 pulse（CLAUDE.md〈原點復歸〉的實測數據）。
2. **功率雜訊底限**：`calibrate_noise()` 在目前位置重複量測估計標準差，`noise_sigma_mult × σ` 作為「真的更好」的門檻——低於這個差值視為雜訊，不算改善。

### 動態調速（2026-08-07 新增）：位移量越小、驅動速度越低

`_dynamic_speed(delta_pulse)`：位移量在 `[speed_scale_pulses]` 範圍內線性內插，兩端夾在 `f_speed_min` / `f_speed_max`。所有移動（`_move_relative` 單軸、`_move_multi_axis` 多軸批次，每軸各自依自己的位移量計算）都改用這個動態值，不再用建構子固定傳入的速度。

**理由**：DS102 的 `R0`（加減速時間）是固定的**時間**、不是固定的**距離**。位移量小時若仍用大位移的高速 `F0`，相對加速度會更劇烈——超過馬達可用扭矩就有失步風險。DS102 是**開迴路控制，沒有編碼器回授**，失步不會有任何訊號，`_positions_pulse` 會悄悄跟真實座標脫節，之後所有收斂判斷都建立在錯誤的座標基準上。降速是用時間換可靠度，成本低、方向正確，但**這只是降低失步機率，不是偵測失步**——偵測本身是另一個獨立的題目，尚未著手。

這是保守的線性內插起跳值，不是實測校準過的失步安全邊界。DS102 實際的扭矩-轉速曲線需要真實硬體才量得出來（見下方待實測參數）。

## 樣本持久化

`persist_samples()` 在 `run()` 的 `finally` 一次性寫出（不逐筆即時寫檔——hot loop 裡做磁碟 I/O 會拖慢緊繃的量測預算）。**中止／例外時也要寫**，這是唯一能回答「跑到哪裡出問題」的資料來源。寫入方式仿照 `_write_json_with_backup()`：先寫 `.tmp` 再 `replace`。預設輸出到 `recordings/scans/`（以程式所在位置為基準，比照 `main_ai.py` 的 `_app_dir()` 邏輯，見 [CLAUDE.md](CLAUDE.md)〈執行期目錄與啟動流程〉）。

## 驗證方式（無硬體）

`FiberAlignmentScanner` 的 `power_query` 完全依賴注入，測試用假 serial 物件（追蹤各軸座標、回應 DS102 協定的 SB3?/SB1?/POS?）+ 合成高斯功率曲線取代真實 HP 8153A。初版（`51ad813`）40 項測試，修正階段二兩個 bug 時（`6fba07d`，見上方〈實作時發現並修正的 bug〉）補上迴歸測試，現為 **48 項**，涵蓋：

- 單軸／三軸座標下降精確收斂到合成峰值
- `_active_axes()` 正確排除未接滑台的軸
- 多軸批次移動：全部通過時同時到位；一軸超限時整批不出發且送出 STOP
- `scanning_active` 期間 GUI 操作全部拒絕，但 `scan_move_step` 仍可動作
- EMS／使用者中止都正確標記 `completed=False` 並保留樣本
- `_weighted_least_squares` 精確還原已知線性關係
- 階段一+階段二混合仍能收斂

訊號有效性判準（`min_valid_power_dbm` 絕對下限、`_check_signal_detectable`、門檻公式修正，見上方〈訊號有效性判準〉）另外用**合成資料**驗證，涵蓋絕對下限、`_check_signal_detectable` 單元測試、`run_stage1` 整合測試、開關回歸驗證。**已進版控（2026-08-19）**：`verify_fiber_scanner_signal.py` 原本只存在於某次 session 的 scratchpad（獨立可執行腳本、33 項個別斷言），已比照 `verify_scan_tab.py`／`verify_meter_panel.py`／`verify_axis_calib.py` 的既有慣例補進 repo 並轉為 pytest（4 個 class、14 個測試函式，同一情境的相關斷言合併進同一函式，覆蓋範圍不變）。轉換時發現原腳本的 `FakeCtrl` 缺 `estimate_um()`（寫於 2026-08-19 μm 快照功能加入之前，`_measure_here()` 現在無條件呼叫它），已補上回傳 `None` 的樁修正。

**這一節測的是 `fiber_scanner.py` 演算法本身**（假 serial + 合成功率曲線）。GUI 整合層另有獨立的 57 項假物件回歸測試（`verify_scan_tab.py`，已進版控，見下方〈GUI 整合〉），測的是「`main_ai.py` 怎麼呼叫 scanner」，兩層測試互不重疊也互不取代。

## 實作路線圖

| 步驟 | 狀態 | 是否需要硬體 |
|---|---|---|
| 補 `meter_GPIB.py` 地基（節流、例外處理、sentinel 契約） | 已完成 | 不需要 |
| `DS102Controller` 的 `scanning_active` 安全整合 | 已完成 | 不需要 |
| `FiberAlignmentScanner` 三階段演算法 | 已完成 | 不需要 |
| 假 serial + 合成功率曲線驗證收斂性與安全邏輯 | 已完成（48 項測試） | 不需要 |
| 假 GPIB ＋真滑台，驗證執行緒與鎖的實際延遲 | 已驗證（2026-08-07） | 已完成 |
| 真滑台小範圍驗證多軸批次收尾（比照 `origin_all` 用韌體軟體限位模擬撞限位） | 已驗證（2026-08-07） | 已完成 |
| 訊號有效性判準（絕對下限＋全域無訊號偵測） | 已完成，合成資料驗證（14 項，已進版控，見上方） | 不需要 |
| HP 8153A 整合進 GUI（光功率／尋光分頁）＋ 尋光六階段 GUI 整合 | 已完成，假物件驗證（57 項，已進版控） | 不需要 |
| 尋光彈性選軸（1～6 軸）＋ 即時軌跡圖支援任意軸組合 | 已完成，假物件驗證（併入 123 項既有回歸測試） | 不需要 |
| 真機端到端跑一次「GUI 按下開始尋光→完整跑完一輪」 | ⬜ 待執行 | **需要滑台＋光功率計** |
| 接真實 HP 8153A，校準下方所有待實測參數 | ⬜ 待執行 | **需要光功率計** |

### 實機驗證紀錄（2026-08-07）

用真滑台 + 合成 2D 高斯功率函式（假光源，峰值設在 X=400、Y=−400，程式不告訴演算法答案）跑完整的階段一＋階段三：

- **收斂結果**：找到 X=399、Y=−399，誤差各 1 pulse——正好落在機械重現性下限（±1~2 pulse），即物理極限內最準的答案。
- **共 85 次量測、86.6 秒**（X/Y 各約 900 pulse 的搜尋窗）。
- **軟體限位正確攔截了一次危險探測**：演算法方向探測階段嘗試 Y 軸 +方向（`GO CW`），該方向正好是 Y 當時壓著的限位——`check_sw_limits_batch`/`_check_sw_limit` 在送出任何序列指令前就攔下，滑台完全沒有真的往那個方向動，演算法自動改往唯一可行的方向搜尋。
- **第 2 輪的「重開步長」機制正確運作**：兩軸都已收斂時，本輪總改善量為 0，提前跳出 cycle 迴圈，沒有浪費時間重複已經確認過的區域。

這次驗證是在真滑台上進行、沒有真實光學訊號，所以**沒有驗證到的部分**是：真實 GPIB 通訊延遲與節流、真實功率讀值的雜訊特性、真實響應曲線是否符合高斯假設——這些仍待接上 HP 8153A 才能校準。

### 三軸延伸驗證（2026-08-07）

同一天延伸到 X/Y/Z 三軸（U 仍未接滑台），峰值改設在 X=700、Y=−750、Z=350（刻意遠離上次兩軸演示結束時的位置，確保演算法真的要走一段才能找到，不是「一測就到」）：

- **收斂結果**：X=699（誤差 1 pulse）、Y=−750（精確命中）、Z=350（精確命中）。
- **共 141 次量測、138.0 秒**。
- **軸數與成本的線性關係得到實測印證**：兩軸 85 次/86.6s → 三軸 141 次/138.0s，比值約 1.6~1.7 倍，與「量測數線性於軸數」的設計目標吻合（座標下降逐軸輪替，不是全維網格搜尋，見上方〈為什麼是這個組合〉）。
- 軟體限位再次正確攔截超限探測（Y 軸方向探測嘗試 −911 pulse，超出 −900 的軟體限位），滑台沒有真的往那個方向動。

### 階段二對比驗證（2026-08-07）：軸間耦合下 K 近鄰的實際效益

前兩次驗證用的合成功率函式軸間獨立（可分離），階段二在那種情境下幫不上忙是預期中事——換成**刻意有軸間耦合**的合成函式（旋轉橢圓高斯：稜脊角度 −30°、沿稜脊寬度 σ=400、垂直稜脊寬度 σ=120，耦合強度 3.3x）才是誠實的對比場景，也正是在準備這個對比時抓到並修掉上面〈實作時發現並修正的 bug〉那兩個階段二的邏輯錯誤。

同一峰值（X=200、Y=−200）、同一起點（X=850、Y=−575）跑兩輪：

| | 量測次數 | 耗時 | X 誤差 | Y 誤差 |
|---|---:|---:|---:|---:|
| A：純座標下降（階段一＋階段三） | 136 | 134.7s | 185.0 | −96.0 |
| B：座標下降＋K 近鄰（階段一＋階段二＋階段三） | 161 | 167.1s | 24.0 | −11.0 |

- **啟用階段二：量測次數 +25（+18%）、耗時 +32.4s（+24%），誤差從 ≈208 pulse 降到 ≈26 pulse。** 階段一被 `max_cycles=3` 提前截斷、還沒收斂到 `step_min` 就被迫停止（稜脊形狀讓座標下降需要反覆之字形逼近，這正是階段二存在的理由），階段二的星形設計＋梯度精修在階段一卡住的地方接著往下找，一步就大幅逼近答案。
- 真機結果與無硬體模擬（同一組合成函式與參數）**完全一致**（收斂座標、量測次數在誤差範圍內相同），確認了先在模擬裡榨乾邏輯錯誤、真機只用來驗證的方法論在這次也成立。
- **這個對比同時證實了「按需啟用」的設計取捨是對的**：階段一收斂良好（可分離座標系）時，多跑階段二只是純開銷；只有像這次耦合強、階段一卡住的情況，階段二才值回額外的 18% 量測成本。是否啟用交給呼叫端依「階段一改善量是否持續走緩」判斷，這裡不自動開關。

### 多軸批次收尾驗證（2026-08-07）

比照 `origin_all` 的驗證方法：用韌體軟體限位（`CCWSLP`/`CCWSLE`）電子式觸發撞限位，不靠真的撞機械開關。Y 軸（起始 −211）暫時把 CCW 軟體限位設在 −230（19 pulse 外），直接呼叫 `scanner._move_multi_axis({"X": 300, "Y": -50})`——這是 `run_stage2` 內部實際在用的方法，不是另外寫模擬：

- **`_move_multi_axis` 正確回傳 `False`**，且 Y 軸最終停在 −230（誤差 0 pulse），確認韌體限位攔截與 Python 端偵測都正常運作。
- **X 軸跑完了完整 300 pulse**（224→524，在已驗證過的安全範圍內），沒有被腰斬。

這個結果同時揭露一個值得記錄的行為特性（不是安全缺陷，但改動 `_move_multi_axis` 前要知道）：`sent` 清單裡的等待是**依軸的送出順序依序 `wait_axis_stop`**，不是「哪一軸先失敗就先發現」。這次 `deltas` 是 `{"X":300, "Y":-50}`（X 在前），所以流程是：兩軸的 GO 都送出後，先阻塞等 X 走完整個 300 pulse（DS102 沒有告訴 X「兄弟軸失敗了」的機制，X 會自主走到自己的合法終點），X 完成後才輪到檢查 Y——這時 Y 早就已經被韌體攔停在限位上了。也就是說 `ctrl.stop()` 實際上是在**其餘軸大多已經自然到位或失敗之後**才被呼叫，是「收尾保險」而非「發現失敗立刻中斷其他軸」。

這不構成安全問題：沒有任何一軸超出「自己合法設定的目標」，`run_stage2` 收到 `False` 後的退回邏輯（對整批 delta 取負號復原）在這個案例下依然正確，因為 X 確實走完了完整的位移量。真正的代價是**時間**——如果排在前面的軸剛好目標很遠或接近逾時（`WAIT_TIMEOUT` 30s），已經失敗的候選點會在真正回報失敗前多等這段時間。目前沒有把它列為待修的 bug：DS102 本身沒有「一軸失敗就中斷其他軸」的韌體機制，要做到真正即時中斷需要把循序 `wait_axis_stop` 改成並行輪詢所有軸的狀態，屬於架構層級的改動，效益（省下最多幾秒到 30 秒）目前沒有到值得優先做的程度，先記錄在這裡。

## 訊號有效性判準（2026-08-12，`d99d177`；門檻公式修正 2026-08-17，`18d59c3`）

同一天 HP 8153A 也整合進 GUI（新增「光功率」分頁與浮動視窗，`64adade`／`b55b17a`），但那一部分是獨立的 GUI 呈現層工作，細節見 CLAUDE.md〈光功率／尋光分頁〉，這裡不重複。這一節只記錄同一天 `fiber_scanner.py` 演算法本身新增的兩層判準，用來區分「量測雜訊」與「訊號小到不具參考價值」：舊邏輯只靠 `meter_GPIB` 的 `ok`（通訊成功與否）與 `_noise_floor()`（同點重複量測的相對差異門檻），若整個掃描範圍根本沒耦光，所有讀值都貼在儀器本底附近，會被當成有意義的梯度去追。

- **`min_valid_power_dbm`（絕對下限，預設 `None`＝停用）**：低於此值的讀值視為無效，`Sample.ok=False` 但保留原始 `power` 供除錯，`note` 記錄原因。這個值**必須來自真機「刻意不耦光」的暗電流基準量測才能定案**，目前程式無法自己假設，仍是 `None`。
- **`_check_signal_detectable()`**：階段一第 1 輪座標下降跑完就無條件檢查一次（原因見下方 bug 1），比較「本輪樣本的功率變化範圍（range）」與「純雜訊情境下該樣本數的期望全距」，range 太小就判定整個探測範圍沒有偵測到訊號並主動中止（`abort_if_no_signal` 可關閉），不需要額外校準值即可運作。
- **`Sample` 新增 `sample_cb`（`SampleCallback`）**：每筆「移動＋量測」完成都會呼叫一次，供外部即時視覺化使用（GUI 整合時接上即時軌跡圖，見下一節）。

**`_check_signal_detectable` 判斷方向的假設仍待真機驗證**：它假設 HP 8153A 在固定量程、無光耦合時的讀值是「穩定貼底」而非「因對數壓縮而劇烈跳動」。如果真機量出來的行為相反（無光時讀值反而在 dBm 尺度上劇烈跳動，因為線性功率趨近零時對數會放大雜訊），這個判準的方向需要重新設計，不能沿用「range 小＝無訊號」。這件事必須用真機在「刻意不對準」的位置實測才能確認，截至 2026-08-19 仍未做。

### 實作時發現並修正的 bug：無訊號判準的觸發時機與門檻公式（2026-08-17）

合成資料實測抓到兩個獨立的 bug，都是 2026-08-12 剛加的判準本身的問題，兩次都經過合成資料 3 次獨立隨機執行驗證：

1. **觸發時機**：`_check_signal_detectable()` 原本只在「本輪改善量（`total_improvement`）低於雜訊底限」時才呼叫。但 `total_improvement`（各軸 `max(0, 改善)` 相加、負值夾成 0）在純雜訊情境下有結構性正偏誤——方向探測比較 `p_plus > p0` 沒有雜訊門檻，等同挑雜訊讀值中較大者的選擇偏誤，多軸加總後這個偏誤會意外讓 `total_improvement` 大於雜訊底限，導致判準完全沒被觸發。實測跑到離合成峰值 1000+ pulse 遠的地方才「收斂」。修法：改成第一輪跑完就無條件檢查一次，不再依賴這個有偏誤的中介指標。
2. **門檻公式**：修好觸發時機後，門檻本身還是卡在邊緣沒攔下來（range 只比固定的 `no_signal_range_mult × 3σ` 門檻高 1.5%）。純雜訊下 range 的期望值本來就隨樣本數 n 增加（統計製程管制文獻的 d2(n) 係數），固定門檻隱含假設了 n 不會太大，但單輪座標下降實測會累積到 n≈206（3 軸情境）。修法：新增 `_norm_ppf()`（標準常態反累積分布函數的有理近似 + 一次 Newton 修正，只用標準庫 `math`，非 scipy）與 `_expected_noise_range_factor(n)`（Blom (1958) 近似：`E[R_n]/σ ≈ 2·Φ⁻¹((n−0.375)/(n+0.25))`），把門檻改成 `no_signal_range_mult × d2(n) × σ`，不再是固定值。已與已發表 d2 表核對，n=5/10/25/50/100 誤差皆 <2%。

兩次修正後既有回歸測試（`verify_fiber_scanner_signal.py`，見上方〈驗證方式〉）全數通過。

## GUI 整合（2026-08-17，`8dbc24e`～`32b4c66`）

`FiberAlignmentScanner` 接進 `main_ai.py` 的「尋光」分頁，同一天（依 git log 時間戳 08:46～09:57）分六階段完成，外加一輪 architect 對整個 `feat/fiber-search-gui` 分支的完整審查：

| 階段 | commit | 內容 |
|---|---|---|
| 一 | `8dbc24e` | 分頁骨架與背景執行緒基礎：matplotlib 優雅降級（裝不到就停用分頁）、`scanner_config.json` 持久化、`_scanning` Event（比照 `_homing`）、`_do_start_scan`/`_do_stop_scan`/`_on_scan_done` 背景執行緒骨架 |
| 三 | `6e72bdb` | 嵌入式 matplotlib 即時軌跡圖：`sample_cb`（scanner 背景執行緒）只做 `list.append`，主執行緒用 `root.after` 固定 250ms 節奏重繪 |
| 四 | `f06ab25` | 完整版面與設定 UI：三張設定卡片（掃描設定／訊號有效性判準／進階設定），照 ui-designer 提案落地 |
| 五 | `1cc5ee5` | 光功率分頁協調：尋光中光功率分頁改顯示提示、暫停自動輪詢、讀值改由 sample callback 回寫 |
| 六 | `b6f2ec8` | 結果呈現分級（正常完成／使用者中止／無訊號中止／未預期例外四種）+ 修正常駐提示的分頁選取陷阱 |
| — | `2648861` | architect 對整個分支的審查結果修正（見下方） |
| — | `32b4c66` | `verify_scan_tab.py` 假物件回歸測試，57 項 |

commit 訊息本身標的是「第一／第三／第四／第五／第六階段」，沒有獨立的「第二階段」commit——如實記錄這個落差，未在 git 歷史中找到對應原因，不代為推測。

**跨執行緒安全**：即時圖表採資料寫入／重繪分離，`sample_cb` 只做 `list.append`，主執行緒的 `_redraw_scan_plot()` 固定 250ms 節奏重繪——`FigureCanvasTkAgg.draw_idle()` 從背景執行緒直接呼叫會丟 `RuntimeError: main thread is not in main loop`（Python 3.14 tkinter 的強制檢查），這是實測過的限制，不是預防性假設。

### 實作過程中自行發現並修正的三個 bug

1. **`enable_stage2` 違反「背景執行緒不可直接碰 tkinter widget」的鐵律（第一階段，`8dbc24e`）**：原本在背景執行緒的 `_run()` 裡才呼叫 `self._scan_stage2_var.get()`，Python 3.14 的 tkinter 會直接丟 `RuntimeError`。改成在主執行緒先讀出來存進區域變數再傳進背景執行緒。
2. **常駐提示的分頁選取陷阱（第五／六階段，`b6f2ec8`）**：`_pm_sync_scan_notice()` 等三個函式用 `winfo_ismapped()` 當「這個提示列目前是否顯示」的判斷依據，但該函式反映的是「目前實際畫在螢幕上」——尋光執行中使用者十之八九停在「尋光」分頁盯著圖表，不會停在「光功率」分頁，未選取分頁底下所有元件的 `winfo_ismapped()` 永遠回傳 `False`，即使早就 `pack()` 過。這會讓 `pack_forget()` 分支被誤判成「本來就沒顯示」而整段跳過（含按鈕還原、狀態文字還原），提示殘留到使用者手動切走再切回。改用 `winfo_manager()`（只反映 `pack()`/`pack_forget()` 呼叫過沒有，不受分頁選取影響）修正三處，並用 tkinter 最小重現腳本先確認機制本身，再驗證修正後行為正確。
3. **`enable_stage2` 設定回填缺口（測試階段，`32b4c66`）**：`_scan_stage2_var` 在 `__init__` 就建立成寫死的 `BooleanVar(value=False)`，比 `_scanner_cfg_pending`（`scanner_config.json` 的內容）還早建立，導致開始尋光時確實會把這個欄位存進設定檔，但下次開程式讀不回來，勾選框永遠回到未勾選且沒有任何提示。改成跟其餘會被設定檔覆寫的欄位同一個模式：搬進 `_build_tab_scan()` 才用 `cfg.get("enable_stage2", False)` 建立。

### architect 對整個分支的審查（`2648861`）

architect 對整個 `feat/fiber-search-gui` 分支做完整審查，**第一版結論是「不建議合併」**，抓到 1 個 critical + 3 個 high/medium 問題：

- **CRITICAL：`ScanAbort` 被 `fiber_scanner.run()` 吞掉，結果分級功能整組失效。** `run()` 內部把使用者中止／EMS 觸發／無訊號判定全部用 `ScanAbort` 自己接住、正常 `return`——`main_ai.py` 精心比對三種訊息字面量來分類結果的程式碼，實際上永遠不會被執行到（`run()` 唯一真的會拋出例外的路徑在啟動背景執行緒前就被 `_do_start_scan` 擋掉了）。實際影響：**使用者按停止、甚至 EMS 緊急停止觸發，畫面都會顯示「✔ 尋光完成」**——安全訊息層級的問題，操作者可能誤以為對準流程正常結束。修法：`fiber_scanner.py` 新增 `self.last_abort_reason`（`None`＝真的收斂完成，否則是中止原因字串），`run()` 在 `except ScanAbort` 分支設定它；`main_ai.py` 改成讀這個屬性分類，不再只依賴例外傳遞。用真的跑滿 `scanner.run()` 背景執行緒（不是直接呼叫 `_on_scan_done`）驗證中途停止／EMS 觸發／純雜訊無訊號中止三種情境，確認都不會再被誤報成「完成」。
- 三項高/中風險：`scanner_config.json`／`meter_config.json` 讀到「合法 JSON 但頂層不是物件」會讓整個 `DS102GUI.__init__` 崩潰（已加 `isinstance(dict)` 檢查）；`_redraw_scan_plot()` 只接 `tk.TclError`，其他型別例外會讓 250ms 重繪迴圈永久斷掉且無任何提示（已加 `except Exception` 但不 `return`）；`_on_close()` 沒有通知進行中的尋光背景執行緒視窗正在關閉（已比照 `_stop_playback.set()` 補上）。

全部修正後 `fiber_scanner.py` 既有合成資料測試全數維持通過，`main_ai.py` 端補上 57 項假物件回歸測試（`verify_scan_tab.py`，`32b4c66`）。

**本文件與 HANDOVER.md 一致地如實記錄：沒有找到 architect 對整個分支重新審查、並給出「建議合併」結論的紀錄。** 之後（2026-08-19）的尋光彈性選軸與即時軌跡圖重繪都各自經過針對該子功能的 architect 審查（見下方），但那是「對單一子功能」的審查，不等於「對整個分支」的總覽式複審——分支目前仍未合併回 `main`。

## 尋光彈性選軸（1～6 軸，2026-08-19，`8a03321`）

`FiberAlignmentScanner` 原本就是動態軸數架構——`_active_axes()` 用 `ctrl.axis_count` ＋ `query_status()` 即時偵測硬體可用性，座標下降／K 近鄰精修的迴圈與矩陣維度全部用 `len(axes)` 動態決定，不是寫死 3 或 6。這次新增的是**使用者主動排除某軸**的能力，跟「自動跟著實際接了幾軸走」是兩件事——例如實機接了 X/Y/Z 三軸，使用者這次可以只勾選 X/Z 搜尋。

- **`__init__` 新增 `selected_axes: Optional[List[str]] = None`**（關鍵字傳遞），存成 `self._selected_axes`。`_active_axes()` 在既有的硬體可用性偵測**之後**，再與這份清單取交集；`None` 時完全不過濾，向下相容改動前的行為。
- **`run()` 開頭**先算一次 `self.active_axes = self._active_axes()`（供 GUI 讀「這次實際搜尋範圍」）：交集後為空就 `raise ScanAbort`，訊息依 `self._selected_axes is not None` 分兩種文案（「選定的軸目前皆不可動」vs 舊行為的「沒有可動的軸」）；非空但有勾選軸被交集排除（使用者勾了、但這軸現在被偵測為不可動），`_log()` 回報一則警告，不靜默吞掉這個落差。
- **`run_stage1`/`run_stage2`/`run_stage3` 完全不用改**——它們一律呼叫 `self._active_axes()`，交集邏輯對它們透明，這是這次改動範圍能維持小的關鍵。
- `main_ai.py` GUI 端沿用專案既有的「固定建六組、連線後動態 enable/disable」模式，刻意不重建分頁——matplotlib canvas、自我重新排程的 `_redraw_scan_plot` 這些既有機制沒有為分頁重建設計過。0 軸選取會在打開確認對話框之前擋下。`scanner_config.json` 新增 `selected_axes` 欄位持久化，缺欄位時六軸預設全勾選（向下相容）。

**獨立驗證**（不只信任實作代理的自我報告）：`selected_axes=["X","Z"]` 交集正確、`selected_axes=None` 向下相容、交集為空兩種文案分開驗證、AND 合成邏輯（軸勾選 × 階段二總開關）兩種情境驗證、0 軸情境用 mock 確認未開啟確認對話框、連線後依 `axis_count` 正確 enable/disable 並清空超出範圍軸的 `BooleanVar`。`verify_scan_tab.py`／`verify_meter_panel.py` 123 項全數通過（假物件驗證，未真機驗證）。

architect 收尾審查（`286aaea`）額外記錄三個**判定不影響安全性/正確性、不急著修**的可選小問題：軸數變多時先前被強制清空的軸不會自動恢復勾選（fail-safe 方向的非對稱，非缺陷）、`_do_start_scan()` 啟動背景執行緒到 `run()` 寫入 `active_axes` 之間有極短視窗期可能讓圖表短暫誤判（純視覺閃爍）、`cfg.get("selected_axes")` 讀檔無型別驗證（跟其他設定欄位同一種既有慣例）。詳細文字見 CLAUDE.md〈尋光彈性選軸〉。

**即時軌跡圖支援任意 1～6 軸組合**（2026-08-19，`41eb5a4`）是這次選軸功能的後續收尾，但屬於 GUI 呈現層而非演算法設計本身（左上子圖改成可切換軸對的 2D 投影、右上子圖改成多軸 1D 相對位移趨勢線），細節記在 CLAUDE.md〈尋光彈性選軸〉一節，不在此重複。

## 待實測參數（現在給不了絕對數值，都是起跳用的保守預設）

| 參數 | 目前預設 | 待實測項目 |
|---|---|---|
| `step_min`（最小步長） | 2 pulse | 需高於機械重現性下限，也要看功率讀值在這個尺度下是否還能分辨訊號 |
| `settle_sec`（震動衰減等待） | 0.03s | CLAUDE.md 記載約 30ms，待微調 |
| GPIB 節流間隔 | 0.03s | `meter_GPIB.py` 的 `GPIB_THROTTLE_SEC`，2026-08-19 仍是 `step-motor.txt` 記載的建議值起跳，尚未接上真實儀器校準（`meter_GPIB.py` 原始碼註解本身明確標示） |
| `noise_sigma_mult` | 3.0 | 功率雜訊標準差的倍數門檻 |
| `min_valid_power_dbm`（訊號絕對下限） | `None`（停用） | 需真機「刻意不耦光」的暗電流基準量測才能定案，見上方〈訊號有效性判準〉 |
| `no_signal_range_mult`（全域無訊號偵測倍數） | 2.0 | 門檻公式本身（Blom d2(n) 近似）已用合成資料驗證兩輪（見〈實作時發現並修正的 bug〉），但「range 小＝無訊號」的判斷方向仍是待真機驗證的假設 |
| 初始步長 `initial_step` | 呼叫端提供，無內建假設 | 需要先掃過全行程量出真實峰寬（FWHM）才能訂 |
| 軸間耦合強度 | 未知 | 決定是否需要啟用階段二的唯一依據 |
| `axis_scale`（階段二距離正規化） | 未提供則不正規化 | 真正的物理尺度需要階段一的副產品才能反推 |
| `f_speed_min`（動態調速下限） | `max(50, f_speed / 5)` | DS102 的扭矩-轉速曲線與實際可靠加減速範圍，需要真實硬體才量得出來 |
| `speed_scale_pulses`（動態調速的位移量範圍） | `(step_min, step_min × 32)` | 「多小的位移算小位移」本身也是待校準的門檻，現在只是跟 `step_min` 掛勾的粗略比例 |
| `MAX_CYCLES` / 階段二 `max_iterations` | 5 / 20 | 依實測收斂速度調整 |
| 是否存在旁瓣（sinc 式次峰） | 未知 | 需要先掃一次全行程功率曲線才能判斷 |

## 尚未做的事（有意識的範圍界定）

**GUI 整合**（開始/停止按鈕、進度顯示）——2026-08-07 版本原記載「這輪刻意不做，依子代理分工應先過 `ui-designer`」，已於 2026-08-17 分六階段完成（見上方〈GUI 整合〉），過程確實先過了 `ui-designer` 提案與 `architect` 審查。以下改列**目前仍未做的事**：

- **`meter_GPIB.py` 的實際連線驗證，仍不完整**——2026-08-12 已用真實 HP 8153A 查證修正兩個 SCPI 指令 bug（`WAV`→`WAVE`、`FETC`→`READ`），且原始碼註解記載 **channel B 實機驗證過可正常回應**，但 **channel A 從未驗證過**（據稱需要外接光學頭，這點是轉述資訊，未找到第二來源佐證）；`GPIB_THROTTLE_SEC` 等節流參數仍是起跳值，未接上真實儀器校準；`VisaIOError` 處理路徑本身也還沒有真實儀器可以觸發驗證。
- **軟體限位與韌體限位在掃描期間的交互**——階段二的批次移動走 `check_sw_limits_batch`（Python 端），韌體端的限位（`CWSLE`）依 CLAUDE.md〈硬體連不上時〉2026-08-05 複測記載目前維持出廠預設（未啟用），這件事在正式跑真實掃描前需要重新確認，截至 2026-08-19 未見狀態變更的紀錄。
- **GUI 整合層的端到端真機驗證**——`fiber_scanner.py` 演算法核心與「尋光」分頁各自都驗證過（前者真機、後者假物件），但「使用者在 GUI 按下開始尋光、完整跑完一輪」這條路徑**沒有真機驗證紀錄**，2026-08-19 新增的彈性選軸／即時軌跡圖重繪同樣只有假物件驗證。這是目前最大的驗證缺口。
- **architect 對整個 `feat/fiber-search-gui` 分支的總覽式複審**——目前只有對整個分支的「第一輪」審查（`2648861`，最初「不建議合併」，修正後未見重新複審結論）與之後各子功能各自的審查，沒有「這個分支現在可以合併」的正式結論，見上方〈GUI 整合〉。

## 第三輪：多軸尋光演算法候選評估（2026-08-27，純規劃階段，尚未落地）

**本節是規劃／文獻回顧的紀錄，不是已完成的功能。** 沒有任何程式碼因本節而改動，`fiber_scanner.py` 現況仍是上方〈核心演算法：三階段混合〉描述的三階段設計。記錄目的是留下決策脈絡，供之後真的要落地時直接接續。

**觸發動機**：現有三階段設計本質仍是「逐軸座標下降為主力」（見〈核心演算法〉），軸間耦合（旋轉橢圓型耦合誤差）要靠階段二額外量測才能從 208 pulse 誤差壓到 26 pulse（多花 18% 量測）。本輪評估其他候選演算法家族，作為替代或補強方向。

### 候選演算法與文獻來源

查詢了 PI（Physik Instrumente）FMPA 產品線、光纖對準期刊論文、2026 年 *Micromachines* 探針卡對準演算法比較研究（Bejani et al.）等業界／學術資料，確認以下候選家族：

1. **SPSA（同步擾動隨機近似）**——每輪梯度估計只需 2 次量測，與軸數無關（座標下降是 O(軸數)），對「量測比移動貴、未來要撐到 6 軸」的場景理論效益最高，但是隨機演算法，收斂路徑不可解釋，`a_k`/`c_k` 步長排程需要多輪調參。
2. **修改型 Simplex／Nelder-Mead**——光纖對準文獻中驗證最久、最常被提及的方法（King's modified simplex），天然處理多軸耦合，缺點是需要維護 N+1 個頂點，對雜訊量測敏感。
3. **Powell 共軛方向法**——對現有階段一（座標下降）的直接升級：仍是一連串 1D 線搜，只是方向向量從座標軸換成任意方向（每輪結束用位移向量取代一個舊方向）。改動幅度最小。
4. **局部貝氏最佳化（GP 代理模型）**——理論上樣本效率最高，`scikit-optimize`/`bayes_opt` 可封裝掉 GP 的實作複雜度，但需要事先給搜尋邊界（見下方「結構性限制」）。
5. **有限差分最陡下降法（Fixed/Variable Gradient Ascent）**——PI FMPA 與探針卡論文的主力演算法之一，概念上是階段一的「聯合版」，但每次梯度估計要 2×軸數 次量測，量測成本仍隨軸數線性成長。
6. **強化學習／Model-free 線上調整**——文獻確實存在（如 PPO 用於光學處理器線上訓練），但需要大量互動樣本才能收斂，與「每次量測都要等真實滑台移動＋GPIB」的成本結構完全不合，**不建議**列入候選。

參考文獻：
- Bejani et al., "Comparative Evaluation of Optical Alignment Algorithms for Integrated Probe Cards in Photonic Wafer Testing," *Micromachines* 17(5):592, 2026. https://www.mdpi.com/2072-666X/17/5/592
- PI, "History and Future of Photonics Alignment Automation." https://www.pi-usa.us/en/tech-blog/history-and-future-of-photonics-alignment-automation-test-assembly-of-sip-components
- PI FMPA 產品說明. https://www.pi-usa.us/en/products/photonics-alignment-solutions/
- "A Novel Algorithm for Fiber-Optic Alignment Automation." https://www.researchgate.net/publication/3423526_A_Novel_Algorithm_for_Fiber-Optic_Alignment_Automation
- "Automation of multi-degree-of-freedom fiber-optic alignment using a modified simplex method," *Mechatronics*. https://www.sciencedirect.com/science/article/abs/pii/S0890695505000040
- "Fiber optic active alignment method based on a pattern search algorithm." https://www.researchgate.net/publication/238981793_Fiber_optic_active_alignment_method_based_on_a_pattern_search_algorithm
- "Fuzzy simplex algorithm for active fiber-laser alignment." https://www.researchgate.net/publication/296736717_Fuzzy_simplex_algorithm_for_active_fiber-laser_alignment
- Laser Focus World, "Simplex algorithm aligns quickly and simply." https://www.laserfocusworld.com/software-accessories/positioning-support-accessories/article/16556202/simplex-algorithm-aligns-quickly-and-simply

### 兩種排序不一致：效益 vs 實現/驗證難易度

按**預期效益**排序，SPSA 排第一（直接解決量測數隨軸數線性成長這個已記錄的結構性瓶頸）。但按**實現與驗證難易度**排序，SPSA 掉到中後段——它是隨機演算法，跟現有 `verify_*.py` 全走確定性斷言的測試風格不合，需要固定亂數種子或統計檢定，且步長排程調參本身需要多輪離線實驗。相對地，Powell 在兩種排序都排前段：`_search_axis_once()` 的 bracket→定向爬坡→三點拋物線骨架本來就是「沿一個向量做 1D 搜尋」，換成任意方向向量幾乎不用重寫，且能直接套用既有的旋轉橢圓耦合測試資料（208→26 pulse 那組）做對照，不必重新設計測試情境。

### mathematician 意見（兩輪）

**第一輪**（審過候選清單後）：
- 光功率讀值是 **dBm（對數量）**，耦合曲面在 dBm 域是**全域二次型**（不是線性域直覺的「近峰陡遠峰平」）——這對 Powell、SPSA 等梯度類方法都是好消息，尤其 Powell 在二次型曲面上有 **n 輪有限終止**的理論保證，直接對應 208→26 pulse 那個耦合案例。
- `calibrate_noise()` 的 σ（dB 單位）不能直接當 SPSA 的擾動幅度 `c_k`（pulse 單位），中間差一個局部斜率 |∂P/∂x|，且 `c_k` 需要一個 pulse 下限（建議 ≥5 pulse，對應機械重現性 2~3 倍），否則遞減步長會被機構殘差吃掉——這是 SPSA 一個容易被忽略的失效模式。
- Powell 方向向量退化在 2~3 軸場景下要 3~5 輪才會真的發生，週期性重置為座標軸方向即可，不必實作 Brent 接受判別式；但 `PULS` 不接小數，沿方向走小步時會被整數量化偷偷打回座標軸，需要「分量 <3 pulse 就歸零重新正規化」這條規則，否則共軛性是假的。
- SPSA 的確定性驗證其實做得到（帶種子的 `random.Random(seed)` 實例 + 可重現偽雜訊的合成 dBm 高斯曲面，斷言固定種子跑 N 輪後誤差 ≤ tol），難度沒有想像中高；真正的成本在 `a_k`/`c_k` 排程的離線調參輪數。
- 結論：先深入設計 **Powell**（理論保證明確、改動面最小、可直接用既有耦合測試資料對照）。

**第二輪**（確認放寬 numpy 限制、比較 Powell vs 貝氏最佳化的落地設計後）：
- objective 函式需要一層 adapter（絕對座標 → round 成整數 pulse → 查快取 → miss 才移動+量測 → 回傳 −dBm）；快取是必需品，Powell 線搜末期會在 <1 pulse 範圍反覆試探，沒快取就是重複移動+量測白燒預算。
- **異常處理是兩者最大分歧**：`scipy.optimize.minimize` 的 objective 一拋例外就整個中止、內部最佳點全丟——撞限位／`_targets_reachable` 否決絕不可用例外，要回「軟牆懲罰值」（`f(clip(x)) + λ·‖x−clip(x)‖₁`，不能用常數或 `inf`，否則三點拋物線內插會算出垃圾方向）。`skopt` 用 `dimensions=[Integer(lo,hi),...]` 結構性解決了整數量化與邊界，根本不會產生越界候選點——這是貝氏最佳化對本專案的真正結構優勢，比「樣本效率」更實在。
- scipy `Powell` 的預設容差（`xtol=1e-4`/`ftol=1e-4`）對本專案座標量級（1e3~1e5 pulse）完全不合用，只會靠 `maxfev` 硬停；需客製化 `xtol≈5 pulse`、`ftol≈3σ`，且要手動設 `options={'direc':...}`（預設方向集合是 1 pulse 單位長度，第一輪線搜會泡在雜訊裡）。
- **貝氏最佳化有一個結構性水土不服**：它需要事先給搜尋邊界，但本專案的行程邊界（`_travel_bounds`）恰恰要撞過一次限位才會知道（`check_sw_limits_batch()` 實機是 no-op，見上方〈架構〉紅線）。給太寬會讓 GP 在巨大空域浪費全部預算，給太窄可能框不到峰——貝氏最佳化理論上最適合取代的階段零盲搜，正是本專案最沒有先驗邊界的階段。**應排在「行程邊界可事先建表」（一次完整 homing + 四側撞邊界建 `_travel_bounds` 持久化）之後**再重新評估，不是現在的優先項。
- 整合方式建議「一段換一段」：**Powell 先取代階段一＋階段二**，階段零（盲搜起點）與階段三（收尾微擾）不動，可直接用既有旋轉橢圓耦合資料做可歸因的 A/B 對照。完全取代零+一+二會讓「起點」與「精修」同時改變，回歸無法歸因。
- 最終建議仍是 **Powell**，理由比第一輪更強：dBm 域二次型的理論保證直接對應耦合案例、scipy 實作成熟、改動面最小，而貝氏最佳化目前卡在「沒有先驗邊界」這個結構性問題上，不是排序問題可以繞過的。

### architect 意見（放寬「不依賴 numpy」的打包／相容性評估）

- **事實修正**：`requirements.txt` 其實**已經有 `numpy`**（matplotlib 強制相依，`main_ai.py` 也直接 import 它）。「不依賴 numpy」原本就只是 `fiber_scanner.py` 單一檔案的自我約束，不是整個專案的原則。
- 既有 onedir 打包已是 152MB（numpy+matplotlib 貢獻約 113MB）。加 scipy 估計拉高到 250~280MB；再加 scikit-optimize/bayes_opt（依賴 scikit-learn）估計到 300~380MB。體積本身在隨身碟/內網部署場景判斷不是否決理由。
- 相容性風險分兩級：numpy 與現有 pyserial/PyVISA/tkinter 已共存並通過打包驗證，風險等於零；scipy 會帶一份**重複的 OpenBLAS**（`numpy.libs` 與 `scipy.libs` 各一份塞進同一 onedir 資料夾），是 `DLL load failed` 的典型來源，必須實際試打包一次驗證。**scikit-learn 會再引入一份 OpenMP runtime**，「Windows 上同時載入多個 OpenMP runtime」是比 BLAS 重複更嚴重的已知當機/掛死模式——這是對貝氏最佳化套件最大的保留，風險比 scipy 高一個等級。
- 對既有 57 項假物件回歸測試：新增 scipy 的模組層級 import 成本可忽略（numpy 的成本現在已經在付，`conftest.py` 收集階段就 import `main_ai`）；唯一要守的規則是新模組**不可 import `matplotlib.pyplot`**（`main_ai.py` 已固定 TkAgg）。
- **依賴分層建議**：`scipy` 放進主 `requirements.txt`；`scikit-optimize`/`bayes_opt`/`scikit-learn` 另立 `requirements-optimize.txt`，定位為離線回放實驗用，不進出貨的 frozen build。
- **檔案切分建議：新開 `fiber_scanner_advanced.py`，不要改寫 `fiber_scanner.py`**。既有檔案承載的是一系列事故修正後的安全不變量（`_note_limit_hit()`、`_targets_reachable()`、只攔 `NoSignalAbort`、逐一檢查 `move_step()` 回傳值），為了一次演算法實驗去動它風險不對稱；新增依賴也需要能「裝不到就降級」（比照 matplotlib 的 try/except 模式）。**硬性條件：新檔不得複製那層安全邏輯**，移動/量測/行程邊界必須沿用既有 scanner 的 `scan_move_step()`／`_targets_reachable()`／中止語意，只把「決定下一個座標」抽成 objective adapter——安全邏輯一旦出現兩份實作，208 pulse 那類 bug 會從沒被修的那一份長回來。
- 放行順序建議：先只加 scipy 做 Powell，跑一次真實 `--onedir` 試打包確認雙 OpenBLAS 無誤，再談貝氏最佳化那組。
- `_move_multi_axis()` 在 `fiber_scanner.py` 已存在，多軸批次移動不是新建設成本（初評誤判為缺口，第二輪已更正）。

### 本輪結論

1. **首選落地方向：Powell 共軛方向法，用 `scipy.optimize.minimize(method='Powell')`**，先取代階段一＋階段二，階段零與階段三不動。
2. 貝氏最佳化因「需要事先給搜尋邊界，但本專案邊界要撞過一次限位才知道」的結構性限制，排到「行程邊界可事先建表」之後再評估。
3. SPSA、有限差分最陡下降、Simplex、強化學習暫不投入。
4. 依賴分層：`scipy` 進主 `requirements.txt`；`scikit-optimize`/`bayes_opt`/`scikit-learn` 另立 `requirements-optimize.txt`。
5. 檔案切分：新開 `fiber_scanner_advanced.py`，不改寫 `fiber_scanner.py`，且不得複製既有安全邏輯。
6. **正式落地前缺三筆實測數據**（不到手，Powell 的容差與軟牆參數都釘不死）：峰附近 dB/pulse 斜率量級、單次量測 vs 單次移動的時間拆解、`calibrate_noise()` 在實機的 σ。
7. CLAUDE.md「不依賴 numpy」的措辭需要修改：先講清楚 `requirements.txt` 其實已有 numpy 這個事實修正，再新增「允許 scipy 等成熟數值最佳化套件」的部分，並與「pulse-only、不做 um/mm 換算」那條完全不同層次的既有決策劃清界線。

**尚待實際著手時才需要的動作**（本節只到規劃層級，均未執行）：草擬 CLAUDE.md 修改文字並定案、跑一次真實 `--onedir` 試打包驗證雙 OpenBLAS 無誤、安排上述三筆實測數據的量測。完整討論過程另存於使用者的 plan 檔（`fuzzy-drifting-thompson.md`），本節是收斂後的正式紀錄。

### 落地實作（2026-08-27，假物件驗證，未真機驗證）

`coder` 依上述規格實作 `fiber_scanner_advanced.py`（`run_stage_powell()`，用 `scipy.optimize.minimize(method='Powell')`），複審時發現並修正一個 bug（best_state 初始化被 `-inf` 污染，會讓撞限位的軟牆懲罰公式又炸出 `+inf`，違反設計時刻意要避開的那個問題）。`tester` 補上 `verify_scan_powell.py`（8 項假物件測試，涵蓋基本收斂、軸間耦合對照、撞限位軟牆懲罰、量測失敗、使用者中止回退、scipy 降級、objective 快取），全數通過，既有 362 項回歸套件同步跑過無破壞。

**耦合案例對照數據**（旋轉橢圓曲面，長軸極淺 k=1e-4、短軸陡峭 k=5e-3，θ=30°）驗證了本節效益評估的核心宣稱：

| 方法 | X 誤差 | Y 誤差 | 合計 |
|---|---|---|---|
| Powell（`run_stage_powell`） | 4.0 pulse | 2.0 pulse | 6.0 pulse |
| 座標下降（`run_stage1`，不開階段二） | 40.0 pulse | 23.0 pulse | 63.0 pulse |

在這個合成耦合曲面上 Powell 收斂精度約為座標下降的 10 倍，方向與〈本輪結論〉一致。**這是合成資料驗證，不是真機數據**——`xtol_pulse`/`ftol_sigma_mult`/`penalty_lambda` 仍是待校準的起跳值（見檔頭註解）。

**2026-08-28 更新：已接進 GUI。** `main_ai.py`「尋光」分頁新增演算法下拉選單（座標下降 / Powell），`fiber_scanner.py` 的 `run()` 新增 `algorithm` 參數依此分流。落地時另外修正 architect 審查抓到的兩個問題：Powell 專屬 metadata（`_powell_param_snapshot()`）寫入失敗原本會炸穿 `persist_samples()` 的 `finally` 導致整輪 JSON 樣本檔不寫出，改成雙層防禦；無訊號偵測原本要等整輪 Powell 跑完才檢查，改成每次真實量測後即檢查。新增 `verify_scan_powell_integration.py`（17 項），連同既有套件共 379 項全數通過。完整記錄見 [docs/fiber-scan.md](docs/fiber-scan.md)〈Powell 尋光路徑接進 GUI〉。**仍只有假物件驗證，未真機驗證**——GUI 上會顯示對應警示文字。

**尚待實際著手時才需要的動作，更新為**：CLAUDE.md 修改文字定案、真實 `--onedir` 試打包驗證雙 OpenBLAS、三筆待實測參數的真機量測（現在有真實 GUI 入口可以進行）。

## 2026-08-26～2026-08-31 後續更新

本節補上第三輪〈Powell 落地〉之後、HANDOVER.md 2026-09-01 版彙整過但本文件先前未涵蓋的演算法層變動。細節與實機驗證狀態以 [docs/fiber-scan.md](docs/fiber-scan.md) 為準，這裡只記與演算法設計直接相關的部分。

### 階段零：無光位置盲搜（2026-08-26，實機事故修正）

**事故**：使用者在無光位置按下尋光，滑台一步都沒動，82 個樣本全部無效，15 秒後才丟出誤導性訊息。根因兩層：`meter_GPIB.__init__` 把量程無條件鎖死在 -20dBm，無光時讀到 sentinel `+9.9E+37` 且不記 log；`_search_axis_once()` 起點量不到值時直接當成「已收斂」而不移動，等同靜默空轉。修正：量程改回預設自動量程＋讀到 sentinel 時自動退回；新增 `run_stage0_blind()`（方形螺旋掃描），在完全無梯度可循時派上用場。

修正過程本身踩了兩次判斷失誤——第一版盲搜觸發判準用「階段一有沒有拋例外」而非「有沒有確認到訊號」，被撞限位但沒拋例外的情境打臉；也曾把「底噪讀不到值」當成拒絕盲搜的理由，但那正是盲搜最該派上用場的情境。兩者都已修正並用 `verify_blind_scan.py`（44 項）鎖住。**仍以假物件測試為準，尚未完整實機驗證**（log 觀察佐證讀值間歇性，量程自動退回行為待下次接 GPIB 確認）。

### 撞限位反覆撞 + 階段一雜訊門檻（2026-08-26～27，實機事故修正，部分已實機驗證）

**症狀**：實機 log 顯示一輪盲搜實撞了 50 次限位。根因兩層：
1. `ctrl.check_sw_limits_batch()` 在實機是 no-op（見〈架構〉），加上每個候選格點都用「絕對目標 − 目前座標」重算 delta，卡在限位上時下一格點算出的 delta 幾乎相同，導致原地反覆撞同一顆開關。修正：新增 `_note_limit_hit()` / `_targets_reachable()`，撞過一次後記住該側邊界，之後同側超界候選點送指令前就被擋掉。
2. 階段一方向探測（`_search_axis_once()`）原本用赤裸的 `> p0` 判斷「哪一側比較好」，純雜訊環境下兩側輪流「看起來比較好」，步長永遠不縮、階段一永遠不結束。**假物件重現：修正前 180 萬次移動仍在擺盪（其中 60 萬次真的撞限位）；加上雜訊門檻（`> p0 + noise_floor`）後 8 次移動、2 次撞限位、正常結束。**

**2026-08-27 部分實機驗證通過**（COM2，Y 軸）：確認 `_move_relative()` 撞限位後 `_note_limit_hit()` 正確記錄邊界、同方向再送候選點時完全不再送出 GO 指令。驗證範圍**只涵蓋單軸路徑**，`_move_multi_axis()`（盲搜／階段二走的多軸同時出發路徑）與跨 `run()` 的 `_travel_bounds` 重置尚未驗證。

### 樣本 Excel 報表匯出（2026-08-26～27）

補充〈樣本持久化〉：每輪尋光結束時在 JSON 樣本檔旁邊多寫一份同名 `.xlsx`（〈摘要〉+〈樣本〉兩張表）。xlsx **不取代** JSON——JSON 仍是完整無損、給程式讀的原始紀錄；xlsx 的任何寫入失敗**只記 log、絕不往外拋**（跑在 `run()` 的 `finally`，最常見失敗是 Windows 上檔案正被 Excel 開著）。`verify_scan_export.py`（24 項）刻意真的把檔案寫出來再用 `zipfile` 解開驗證，沒有 mock 掉 `xlsxwriter`。

### 尋光分頁速度預設值調成 10 倍（2026-08-27）

驅動器分度（division）調整為原本的 1/10 後，同樣 pulse 數對應的物理位移縮小為 1/10，「尋光」分頁自己的四個速度預設 `tk.StringVar` 同步調成 10 倍以維持實際移動速度不變。這組設定跟〈移動控制〉分頁的速度設定卡是完全獨立的另一份，**沒有跟著改**，兩邊不要假設已同步。

### 多軸高斯向量圖模擬工具（2026-08-28～31，輔助調參，非硬體驗證）

新增 `gaussian_vector_sim.py`（CLI）與 `gaussian_vector_sim_gui.py`（獨立 tkinter GUI），逐行對照 `fiber_scanner.py` 的 `run_stage0_blind()` / `run_stage1()` / `_search_axis_once()` / `_rect_spiral_offsets()` 移植，用來在沒有硬體時觀察梯度場長相、驗證尋光演算法行為並調參。**搜尋邏輯必須忠實重現 `fiber_scanner.py`，不可另外設計通用演算法**——早期版本用 steepest descent + Rprop，因與真實座標下降是兩套邏輯已重寫；真實梯度只用來畫背景向量圖，搜尋本身跟真機一樣只靠「移動＋量測」的有限差分。完全獨立於 `main_ai.py`，不 import 任何 DS102 相關模組，不連接硬體，**不能算作真機驗證的替代品**——下方〈第三輪〉之後的殘差診斷微調正是用這個工具跑出來才發現的問題，但發現之後的修正仍要走假物件回歸測試與真機驗證兩條路徑分別確認。

### 尋光演算法殘差診斷與收尾曲率擬合微調（2026-08-31，`08e0bcd`）

用 `gaussian_vector_sim.py` 忠實重現階段一座標下降後，發現探測距離跟移動步長綁在一起——梯度平緩的軸會過早判定收斂，留下換算成 dB 可能有意義的殘差。mathematician 評估後給出三項建議，architect 審查落地版本又抓到 9 個問題，一次做對：

- **`calibrate_noise()` 預設 `n_samples` 5→12**：σ 估計的變異係數從 35% 降到 21%，牽動全部階段的判準穩定度，不需要多動滑台一步。
- **新增 `_diagnose_residual_curvature()`**：`run()` 收尾時用既有樣本對每軸做 dB 域二次曲線擬合，估計 σ／殘留距離／預估耦光損失，寫進 JSON 與 xlsx 摘要。取樣窗口同時限制「其他軸」與「自身」座標在 `step_min × REOPEN_STEP_MULT` 內，避免盲搜與大步長探測樣本混進擬合算出無意義的數字；擬合前無因次化避免病態矩陣；殘留距離超出窗口一半判定不可信並跳過。**純事後統計，不移動、不量測，失敗只記 log**。
- **新增 `run_stage_curvature_refine()`（預設關閉，`enable_curvature_fit`）**：階段一收斂後的最後一步微調，對稱探測＋三點公式反推修正量，補在既有〈階段三：收尾微擾〉之後。三項接受準則：有效筆數 ≥3（防間歇性 sentinel）、`_noise_sigma > 0`（防校準失敗時門檻退化成 0）、`denom` 對應實際樣本數的訊噪比門檻（不是硬編假設滿筆數）；套用後「沒有變差」才接受，否則退回；探測失敗一律整軸放棄，不做單側外推。Powell 路徑改用 `fiber_scanner_advanced` 自己的尺度常數，不誤用對它無意義的 `initial_step`。

新增 22 項回歸測試（`verify_fiber_scanner_signal.py`），涵蓋取樣窗口過濾、純雜訊場零移動、低有效筆數放棄、`denom` 門檻、撞限位/中止等邊界情況，數值場景先用原型腳本實測驗證再寫成斷言。**這一輪同樣只有假物件驗證，`enable_curvature_fit` 預設關閉，未真機驗證前不建議開啟。**
