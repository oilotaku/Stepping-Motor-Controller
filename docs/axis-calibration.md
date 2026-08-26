# 軸機械校正參數（`axis_calibration.json`）

> 本文件自 CLAUDE.md 拆出（2026-08-26），目的是縮小每次對話的固定載入量。
> **內容未經刪減**，動到對應功能前請完整讀過本檔。

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
