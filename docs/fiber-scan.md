# 光功率／尋光分頁

> 本文件自 CLAUDE.md 拆出（2026-08-26），目的是縮小每次對話的固定載入量。
> **內容未經刪減**，動到對應功能前請完整讀過本檔。

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
- **回歸測試**：[verify_scan_tab.py](verify_scan_tab.py)（尋光分頁，57 項）、[verify_meter_panel.py](verify_meter_panel.py)（光功率分頁，82 項）、[verify_axis_calib.py](verify_axis_calib.py)（軸機械校正參數，50 項，涵蓋 `ds102_ctrl.py`／`fiber_scanner.py`／`main_ai.py` 三個層級）用假的 `ctrl` / `meter` 物件跑邏輯，不需要真實硬體或 GPIB 卡，改動對應功能後應該先跑對應的測試檔。2026-08-17 `DS102Controller` 拆到 `ds102_ctrl.py` 後前兩支仍全數通過，可作為「模組拆分沒有破壞既有行為」的既有驗證手段之一。

#### 移動期間暫停光功率背景輪詢（`motion_active`，2026-08-20）

`_start_meter_poll_worker`（獨立執行緒，每 0.5s 打一次 GPIB）原本只在 `ctrl.scanning_active`（尋光演算法執行中）為真時暫停，但使用者在**移動控制分頁**手動點動／單步／原點復歸時這顆旗標是 False，GPIB 與序列埠通訊會同時進行——不是硬體安全問題（兩條匯流排實體分離），但讀到的光功率值在馬達震動時是不可信的雜訊，且會悄悄流進 `data/*.csv` 沒有任何標記。

- **`DS102Controller.motion_active`（`ds102_ctrl.py`，唯讀 property）** 涵蓋點動/步進/原點復歸/重播/尋光，定義為 `playback_running or scanning_active or not _jog_stop.is_set() or _motion_depth > 0`。`_motion_depth`／`_motion_lock`（獨立於 `self._lock`，不擴大熱路徑鎖的責任）搭配內部 `_motion_scope()` context manager，包住 `_do_move_step()`／`move_origin()`／`origin_all()` 的核心邏輯——用計數器而非 bool 是因為 `origin_all` 可能巢狀呼叫其他也進入 `_motion_scope` 的方法，計數器能正確處理巢狀（內層先離開不會提早清空旗標）。`play_recording()` 不需要掛，它已經有 `playback_running`。
  - 🔴 **`motion_active` 只給「要不要占用其他硬體資源」的判斷用，絕對不可拿來當移動守衛**（不可放進 `move_step`/`move_continue`/`goto_point` 的守衛條件）。`scanning_active` 進 `move_step` 守衛曾導致所有收斂測試卡死（scanner 呼叫自己的移動被自己設的旗標擋住，〈`scanning_active` 與 `scan_move_step`〉），這是同一個陷阱的翻版。
  - 🔴 **`ems_active` 刻意不在判斷式內**——EMS 代表滑台已經停止（不是還在動），沒有理由連光功率讀取都跟著暫停。
  - **前置修復**：`emergency_stop()` 與 `disconnect()` 原本都不會 `set()` `_jog_stop`，點動中觸發這兩者會讓 `_jog_stop` 卡在 `clear()` 狀態，`motion_active` 永久回報 True、光功率背景輪詢從此永遠不會恢復。兩處都已補上 `self._jog_stop.set()`（`emergency_stop()` 放在設 `ems_active = True` 附近；`disconnect()` 放在關閉序列埠之前）。
- **`main_ai.py` 的 `_pm_should_poll()`** 是 `_start_meter_poll_worker` 迴圈的守衛：`meter is not None and _pm_auto_poll.get() and not ctrl.motion_active`。
- **`_pm_refresh_status_line()`** 是 `_pm_status_var` / `_pm_status_lbl` / `_pm_power_lbl` 前景色的唯一寫入者，優先序：未連線 > 通訊失敗 > 尋光中 > 移動中 > 正常。移動中呈現「⏸ 滑台移動中，暫停讀取」（`CLR_MUTED`），數值本身不清空（清成 `—` 會誤導成斷線）；不用 pack/pack_forget 切換（移動是次秒級高頻切換，會讓版面一直跳動，違反〈第四批修正〉的既有原則），改個 label 文字跟顏色即可。掛在 `_start_poller`（100ms、不做 I/O、一定會跑）而非 `_redraw_scan_plot`（沒裝 matplotlib 時整條不會執行，會讓這個機制在那種環境下失效）——`_pm_sync_scan_notice()` 原本掛在 `_redraw_scan_plot` 的呼叫也一併搬去 `_start_poller`，順便修掉它在無 matplotlib 環境下的同一種既有失效問題。
- ⚠ **`_wait_axis_stop()` 期間寫入 `data/*.csv` 的光功率值語意已改變**：改動前是「移動中的即時值（可能含震動雜訊）」，改動後背景輪詢在移動期間暫停，`_get_last_pm_value()` 回傳的會是**移動開始前最後一次背景輪詢的值**，可能已經過期（最舊可達背景輪詢間隔 `METER_POLL_INTERVAL` 那麼久）。這是刻意的取捨（過期但穩定的值優於即時但含雜訊的值），但下游若有人假設「CSV 裡這欄是移動當下量到的」，這個假設從這次改動起不再成立。
- **回歸測試**：`verify_meter_panel.py` 的 `TestMotionPausesMeterPoll`（16 項，含 `motion_active` property、`_motion_scope` 巢狀、`emergency_stop`/`disconnect` 的 `_jog_stop` 回歸鎖、GUI 層 `_pm_should_poll()` 各狀態組合）。

#### 階段零：盲搜粗掃 + 光功率計量程（2026-08-26，實機事故修正）

**事故**：使用者在無光位置按下尋光，**滑台一步都沒動**，82 個樣本全部無效，15 秒後才由階段二丟出一則指向錯誤位置的訊息（「階段二起點量測失敗」）。根因有兩層，兩層都修了。

**第一層（根因）：`meter_GPIB.__init__` 把量程無條件鎖死在 -20dBm。**

```python
self._write(f":SENS{self.ch}:POW:RANG:AUTO OFF")
self._write(f":SENS{self.ch}:POW:RANG -20DBM")
```

而它上面的註解自己就寫著相反的話——「若對光初期完全沒光(雜訊階段)，可先保留 ON」。輸入功率低於該檔位下限時 HP 8153A 回傳 IEEE-488.2 的 underflow sentinel `+9.9E+37`，`get_power()` 依 `abs(value) > 1e30` 判為失敗，**而且那個分支不記任何 log**（VisaIOError 與解析失敗都有 `logger.error`，唯獨最常見的這條靜默），事後只能靠比對 `viRead` 的 byte 數反推。

修正：
- **預設 `range_auto=True`**（`__init__` 新增 `range_auto` / `range_dbm` 參數）。尋光的**起點本來就常常是無光的**——那正是要尋光的原因，所以正確性優先於「鎖定量程比較快」這個最佳化。
- **`get_power()` 的手動量程自動退回**：讀到 sentinel 且目前是手動量程 → 切回自動量程、重讀一次。鎖定量程從此是**樂觀最佳化**：尋光從底噪爬到耦合峰值可能跨數十 dB，鎖在剛偵測到微弱訊號時的檔位必然會 overrange，沒有這道退回就不該鎖。退回後不自動鎖回去，避免在檔位邊界反覆切換。
- **`_log_sentinel()` 節流記錄**（`SENTINEL_LOG_INTERVAL_SEC` 5s）。無光時每次讀值都走這條，逐筆記會灌爆 log；但完全不記，下一次同樣的事故一樣查不出來。
- GUI 端：`_on_scan_signal_found()` 在確認訊號後鎖定量程，`_restore_meter_auto_range()` 在 `_on_scan_done()`（所有結束路徑的唯一匯流點）還原。🔴 **少了還原這一步，bug 會以更隱蔽的形式復發**：上一輪鎖定的量程留到下一輪，而下一輪的起點又常常是無光的。

**第二層：演算法在讀不到值時靜默空轉。**

`_search_axis_once()` 起點量不到就 `return True, None`（判定「此步長已收斂」）而完全不移動；`_check_signal_detectable()` 因有效讀值為 0 走「樣本太少，不誤殺」的放行分支。整條路徑無聲。修正：

- `calibrate_noise()` 新增 `self._noise_baseline`，一個有效讀值都拿不到時留 `None`，`run()` 立刻 `NoSignalAbort` 並直指光功率計與量程，不再空轉整個階段一。
- 🔴 `_check_signal_detectable()` 把「有效讀值為 0 但量測次數 ≥4」與「樣本太少」**分成兩條路**——前者是明確失敗（儀器讀不到數字），後者才是不誤殺的放行。兩者共用同一條 `len(powers) < 4: return` 正是事故裡讓 82 個全無效樣本溜過去的缺口。

**階段零盲搜（`run_stage0_blind`）**：座標下降與爬坡都需要**梯度**，完全無光時所有方向都貼在同一底噪水準，演算法在原地無從選方向。盲搜不需要梯度，只需要「掃到就算數」。

- **方形螺旋，不是阿基米德螺旋**：DS102 只有「單軸相對步進」一種移動原語、沒有插補（見 [protocol.md](protocol.md)），斜線畫不出來。方形螺旋每步只動一軸、位移固定＝`step`。邊長序列 1,1,2,2,3,3,…；⚠ **最外環要走到邊長 `2×n_rings + 1` 才補得完最後四個角**（取 `2×n_rings` 時 step=1／radius=2 只吐 21 點而非 25）。
- **固定 2 軸**。第三軸用同樣密度掃會讓格點數變成立方，以光纖對準需要的格距估算根本跑不完。
- 🔴 **每個格點都用「絕對目標 − 目前座標」重算 delta**，不是沿螺旋累加相對位移。撞限位跳過的格點會讓實際位置偏離螺旋路徑，累加式會讓後續每一點跟著整體平移，掃出來的區域跟使用者設定的半徑不再對應。
- **判定門檻 = baseline + max(σ倍數×σ, 絕對下限dB)**。兩者取大：底噪穩定時 σ→0，純靠倍數會退化成「比基準大一點點就算找到」，一個雜訊尖峰就能讓滑台停在沒有光的地方並**回報成功**。
- 🔴 **觸發判準是「跑完階段一有沒有確認到訊號」（`_signal_confirmed`），不是「階段一有沒有拋 `NoSignalAbort`」。** 第一版只攔例外，實機立刻打臉——log 13:08：X 軸撞限位使第一輪只收到 11 筆樣本、有效的不足 4 個，`_check_signal_detectable()` 走「樣本太少，不誤殺」的放行分支，接著 `total_improvement < noise_floor` 讓外層迴圈 `break`，`run_stage1()` 就這樣**正常 return**——沒有例外，所以沒有盲搜，最後由階段二丟出「起點量測失敗」。用旗標判斷同時涵蓋「拋例外」與「安靜收斂」兩條路徑。
- 🔴 **底噪讀不到值不是拒絕盲搜的理由——第一版在這裡判斷錯了。** sentinel（`+9.9E+37`）的語意是「功率低於目前可量測下限」，那是**明確資訊而非未知**：在連底噪都測不到的環境裡，任何一個讀得到的有效值本身就已經高於底噪。第一版讓 `run()` 在 `_noise_baseline is None` 時直接中止，等於把盲搜最該派上用場的情境擋掉（實機 log 13:57／13:59 兩次全 sentinel，使用者選了 auto 卻直接看到「未偵測訊號」，盲搜一次都沒跑到）。現在改為：`blind_mode="off"` 才維持立刻中止；否則進盲搜並套用**退化判準——掃到任何一個讀得到的有效值就算找到訊號**。
- **`_check_signal_detectable()` 不再被 `abort_if_no_signal` 整個 gate 掉**，改成只有「判定沒訊號時要不要拋例外」受那個開關控制。它同時負責在**有**訊號時設起 `_signal_confirmed`，關掉開關等於連「有訊號」的判定也一併跳過，`run()` 會誤以為從未偵測到訊號而多跑一輪盲搜。
- **`_power_clearly_above_baseline()`**：階段一結束、`_signal_confirmed` 仍為 False 且不是因為拋例外時，量一次當下功率跟基準比。用途是分辨「真的沒訊號，該去盲搜」與「其實已站在訊號上，只是有效樣本不足 4 個而無從判定」——沒有這道檢查，後者會白跑一輪上百格點的盲搜。門檻與盲搜完全相同，避免兩處判準不一致造成來回擺盪；回傳實際讀值而非 bool，那個值會進 log 與量程鎖定訊息。
- **`NoSignalAbort(ScanAbort)` 子類別**：`blind_mode="auto"` 時 `run()` 用 `except NoSignalAbort` 精確攔截。🔴 **絕對不可寫成 `except ScanAbort`**——使用者主動停止與 EMS 觸發也是 ScanAbort，那會讓「按下停止」立刻換來一輪掃過上千格點的盲搜，結果完全相反。`verify_blind_scan.py::TestBlindModeIntegration::test_user_stop_never_triggers_blind_search` 鎖這一點。
- **auto 模式會先把滑台移回起點再盲搜**（`_return_to_machine`）：階段一在純雜訊上會亂爬，漂走後那裡不該當圓心——半徑是使用者相對**起點**設定的。
- **`last_abort_kind`（"no_signal"／"other"）**取代呼叫端對訊息字串的子字串比對。新增四種無訊號中止訊息時，舊的字面量比對會讓它們全部靜默掉進「其他錯誤」分支。

**預設值與已知限制**：

- 🔴 **函式庫預設 `blind_mode="off"`，GUI 預設 `"auto"`**（`GUI_DEFAULT_BLIND_MODE`）。盲搜是本專案單次自動運動量最大的操作，沒有明確要求就自動跑起來違反「寧可少搜不多動」；GUI 是使用者看得到、可取消、且確認對話框會明列掃描規模的情境，兩者不衝突。
- 預設 `blind_step=200` / `blind_max_radius=1000` ＝ 121 點（粗估半分鐘），刻意讓「預設按下去」規模溫和。早期版本半徑 4000 ＝ 1681 點、七分鐘以上的無人看管運動，不適合當預設。
- 🔴 **格距必須小於耦合光斑的尺度，否則螺旋會直接跨過訊號區而漏掉。** 單模纖芯約 9μm（以 2μm/pulse 換算約 5 pulse）、多模 50/62.5μm 也不過 25~30 pulse——**200 pulse（約 400μm）對這兩種都太粗**，只適合「光斑很大／只是要先確認大方向」。真要靠盲搜找到單模耦合，格距得往個位數 pulse 設，而那會讓同樣半徑的點數暴增，必須同時縮小半徑。這個取捨沒有通用解，GUI 的即時格點數估算（`_update_blind_estimate`）就是為了讓它看得見。
- ⚠ **實機 log（2026-08-26 13:07～13:59）證實自動量程確實會讀到值**（`雜訊校準完成：σ=0.46271，基準功率 -57.2439 dBm`），但**讀值是間歇性的**——同一段時間內有時讀到 -57 dBm、有時仍回 sentinel（錯誤碼 `-231 Data questionable`），功率就卡在儀器可測邊緣。這是盲搜必須能在「多數格點讀不到、偶爾讀到」的情況下運作的直接理由，也是退化判準存在的原因。
- ⚠ **除上述 log 觀察外，本輪改動仍以假物件測試為準，尚未完整實機驗證**：自動量程在實機上能不能讀到無光底噪、`+9.9E+37` 是否確實是無光時的回應（目前由 log 的 17-byte 回應長度推得）、量程自動退回的實際行為，都要等下次接上 GPIB 才能確認。

**回歸測試**：[verify_blind_scan.py](../verify_blind_scan.py)（44 項）——螺旋幾何、雜訊校準的零有效讀值偵測、事故的直接回歸鎖（不空轉、不移動）、盲搜找峰/掃完/限位跳過/幾何不漂移、三種模式串接、使用者中止與 EMS 的安全鎖、量程鎖定回呼。

#### 撞限位不再反覆撞 + 階段一一定停得下來（2026-08-26，實機事故修正）

> 本節由 AI 協助整理（This document was AI-assisted）。

**症狀**：使用者回報「尋光常常 Detect limit」。實機 log `logs/ds102_20260826_150901.log` 15:10 起的**一輪盲搜實撞了 50 次限位**，全部是 `Detect CCW limit`，畫面上就是限位警報橫幅一直跳。根因有兩層，兩層都修了，兩層都有回歸測試鎖。

**第一層：送出前的限位檢查在實機上是 no-op。**

`run_stage0_blind()` / `run_stage2()` 送指令前都會呼叫 `ctrl.check_sw_limits_batch()`，註解也明明白白寫著「超出行程的格點不必真的送一次 GO 才發現走不了」。但它比對的 `ctrl.sw_limits` **預設六軸全是 `(None, None)`**（GUI 的〈軟體行程限制〉不填就是全部放行，而且不進設定檔、每次啟動都是空的），控制器韌體的 `CWSLE`/`CCWSLE` 出廠也是停用、還是 RAM-only（見 [hardware.md](hardware.md) 2026-08-05 複測）。**兩層保護實際上都不存在**，候選點超出行程時只能靠真的撞上限位開關才知道走不了。

再加上兩件事就湊成 50 次：

- 盲搜半徑設 10000 pulse，而 **Y 軸全行程只有 4146 pulse**（X 10613、Z 10578，見 [hardware.md](hardware.md)）——螺旋有一大片在行程外。
- 🔴 每個格點都用「絕對目標 − 目前座標」重算 delta（這是刻意的，見上一節），而**卡在限位上的軸座標不會變**，下一個格點算出來的 delta 幾乎一模一樣 → 原地反覆撞同一顆開關。每撞一次還連帶送一次 `STOP 0`，把本來正常移動的另一軸也連坐停掉。

修正：`FiberAlignmentScanner._note_limit_hit()` + `_targets_reachable()`。

- 移動失敗後補查一次 `query_status()`，用 `ctrl.limit_direction()` 判斷壓在哪一側，把當下機械座標記進 `self._travel_bounds[軸][側]`。之後同側超界的候選點在**送指令前**就被擋掉——**一輪最多各撞一次**。
- 🔴 **判不出限位方向時一律不記邊界。** 移動失敗還有逾時、通訊失聯等成因，把那些誤記成「行程末端」會讓該方向一整片區域在這一輪被靜默排除，比多撞幾次嚴重得多。
- 同側重複撞到時取靠內側的值（CCW 取大、CW 取小）：限位開關有實體作用寬度，每次停下的座標會差幾個 pulse。
- 🔴 **邊界不跨輪沿用**（每次 `run()` 重置）。兩輪之間可能做過原點復歸，`POS` 是相對暫存器、復歸後同一個機械位置的座標值整組改變，沿用舊邊界等於用錯誤的座標靜默排除一片區域。代價只是每輪各方向要重新實撞一次。
- `_targets_reachable()` 是**兩層疊加**不是取代：`ctrl.sw_limits`（事先知道、但預設是空的）＋本輪實測邊界（一定準、但要先撞過一次）。
- `_move_relative()` 被實測邊界擋下時**刻意不記 log**——階段一每一輪的方向探測都會走到這條路徑，逐筆記會灌爆 log；真正該大聲講的那一次（實際撞上）已經由 `_note_limit_hit()` 記了。
- `_move_multi_axis()` 記邊界排在 `stop()` **之後**：`_note_limit_hit()` 會多送一次 `SB3?`/`SB1?`（約 112ms），插在 `stop()` 前面等於讓其餘還在動的軸多跑那段時間。限位狀態是準位不是邊緣，停下來之後照樣讀得到。

**第二層：階段一在純雜訊下不會結束。**

用假物件重現同一個情境（起點壓在限位上、讀值只有雜訊）：**舊版跑到 180 萬次移動仍在原地兩點之間擺盪，其中 60 萬次是真的撞在限位開關上**。也就是說第一層的 50 次還只是使用者提早按停止的結果。

- `_search_axis_once()` 只要「有移動」就回報未收斂，而外層 `while s >= step_min` 只在收斂時才縮步。
- 方向探測用的是赤裸的 `p_plus > p0`，**沒有雜訊門檻**——同一個函式裡的爬坡迴圈一直都是 `> p_curr + noise_floor`，這個不對稱既有註解自己就寫著是「等同挑雜訊讀值中較大者的選擇偏誤」。純雜訊下兩側輪流「看起來比較好」，於是每次呼叫都移動、永遠不縮步。

修正：

- **方向探測補上雜訊門檻**（`> p0 + self._noise_floor()`），與爬坡迴圈一致。低於底限的「改善」本來就不是資訊，拿它決定方向等於讓雜訊駕駛滑台。
- **`STAGE1_MAX_PASSES_PER_STEP = 200` 保險絲**：同一步長連續 200 次未收斂就強制縮步並記一則警告。🔴 這是防無窮迴圈的保險絲**不是調校參數**；熔斷時刻意縮步繼續而不是中止整輪（換更小的步長還有機會），但**一定要記 log**——正常情況下不該走到這條路徑，靜默熔斷會讓「為什麼結果怪怪的」永遠查不出來。200 對真的在跟訊號的搜尋非常寬鬆：爬坡是在單次呼叫內部連續走完的，這裡數的是「換方向的次數」。

**修正前後（同一個假物件情境）**：180 萬次移動／60 萬次撞限位／不會結束 → **8 次移動／2 次撞限位／正常結束**。

**回歸測試**：[verify_blind_scan.py](../verify_blind_scan.py) 新增 13 項（`TestTravelBounds` 10 項、`TestStage1Termination` 3 項）。`FakeCtrl` 新增 `hard_limits` 參數——🔴 **它與既有的 `limits` 是兩件完全不同的事，混在一起看會誤解整個修正的重點**：`limits` 是軟體限位，在送指令前擋掉、滑台一步都不會動；`hard_limits` 是實體限位開關，**擋不住任何指令**，軸會真的走到端點停住、`SB1?` 從此回報 `Detect CW/CCW limit` 直到離開開關。實機唯一存在的就是後者，`_note_limit_hit()` 要鎖的正是「只有後者存在時不要反覆去撞它」。`FakeCtrl.limit_direction()` 刻意委派給真正的 `DS102Controller.limit_direction`，不自己複製一份（CLAUDE.md 的「必須先判斷 CCW」只能有一個來源）。三項新測試都已驗證**在修正前的程式碼上會失敗**。

**✅ 2026-08-27 實機驗證通過**（COM2，Y 軸，`verify_hw_limit_probe.py`，scratchpad 一次性診斷腳本、未進版控）：

1. 相對移動 -6000 pulse（遠超 Y 全行程 4146 pulse，保證撞底）→ `_move_relative()` 回傳 `False`。
2. 補查 `ctrl.query_status()` 原始回應：`status='Detect CCW limit'`——**`STOP 0` 送出後限位位元沒有被清掉**，`_note_limit_hit()` 補查的那一次讀得到。`limit_direction()` 正確判讀成 `"CCW"`。
3. `scanner._travel_bounds["Y"]` 正確記下撞停座標。
4. 同方向再送一次候選點：用 `ctrl.scan_move_step` 的呼叫次數當證據，**呼叫次數完全沒有增加**——真的沒有再送出任何一筆 GO 指令，不是「送了但被控制器拒絕」。
5. 反方向（離開限位）移動 500 pulse 正常成功，沒有被誤擋。

驗證範圍：只涵蓋 `_move_relative()`（階段一／階段三走的單軸路徑）與單軸撞限位。**尚未涵蓋**：`_move_multi_axis()`（盲搜／階段二走的多軸同時出發路徑）在實機上的撞限位行為、`_travel_bounds` 跨 `run()` 重置在實機上的效果、以及方向探測雜訊門檻／`STAGE1_MAX_PASSES_PER_STEP` 保險絲在真實雜訊統計特性下是否真的收斂（那部分仍以假物件的合成雜訊為準）。

⚠ 附帶觀察：測試當下 Y 軸連線後座標即為 `0.0`，而 -6000 的移動指令送出後座標**完全沒有變化**就回報撞限位——代表這次連線時 Y 已經停在 CCW 端附近，與 [hardware.md](hardware.md) 2026-07-31 記載「Y 的 CW 端在 0 附近」的座標系不是同一個。這正是文件早就寫明的「`POS` 是相對暫存器，再次復歸會重設，端點的座標值會變」，不代表量測有誤，只是提醒兩份記錄的座標系不能互相比對。

**⚠ 使用者側的設定同樣值得調整**：`recordings/scanner_config.json` 目前是 `blind_max_radius=10000`／`blind_step=2000`。半徑 10000 對 Y 軸（全行程 4146 pulse）本來就有一大半掃不到，這個修正只是讓掃不到的部分**不再用撞的**去發現。要讓盲搜真的有效率，半徑應該依各軸實際行程設定，或先在 GUI 的〈軟體行程限制〉填上實測端點。

#### 尋光樣本的 Excel 報表（2026-08-26）

本節由 AI 協助撰寫（This document was AI-assisted）。

**產出什麼**：每輪尋光結束時（完成、中止、例外都算），`persist_samples()` 在既有的 `recordings/scans/scan_YYYYMMDD_HHMMSS.json` **旁邊**再寫一份同檔名的 `.xlsx`。另外「尋光」分頁工具列新增「⤓ 匯出 Excel」按鈕，讓使用者自選存檔位置——兩條路徑共用同一個產生器 `fiber_scanner.export_samples_xlsx()`，報表內容一致。

**🔴 xlsx 不取代 JSON，兩份都要。** JSON 是完整、無損、給程式讀的原始紀錄（事後重繪軌跡圖、回溯除錯都靠它，`Sample` 的每個欄位原樣保存）；xlsx 是給人看的報表，欄位攤平成表格、多了 μm 估算欄與統計摘要，但 `calib_snapshot` 只留最後一筆、note 超過 32000 字元會截斷。為了「省一個檔」把 JSON 換成 xlsx 等於把唯一能回答「跑到哪裡出問題」的資料來源降級。

**🔴 xlsx 的任何失敗都只記 log、不往外拋（`_persist_samples_xlsx()`）。** 這個函式跑在 `run()` 的 `finally` 裡、緊接在 JSON 寫完之後。實務上最常見的失敗是 **Windows 上目標檔正被 Excel 開著造成的 `PermissionError`**（使用者上一輪的報表還開著就會遇到），其次是沒裝 `xlsxwriter`。讓例外炸穿 `finally` 會蓋掉 `run()` 原本要回傳的最終座標，換來的只是一個報表格式的問題。`verify_scan_export.py::TestPersistSamplesIntegration::test_xlsx寫失敗不影響json` 鎖這一點。

**相依套件優雅降級**：`xlsxwriter` 比照 main_ai.py 對 matplotlib 的處理——`fiber_scanner` 頂端 try/except import，缺席時 `_XLSXWRITER_AVAILABLE=False`，自動存檔靜默跳過（只記一行 log），GUI 的匯出鍵在建分頁時就 disable 並把原因寫進按鈕文字（「⤓ 匯出 Excel（缺 xlsxwriter）」），而不是讓使用者按下去才看到例外。已列入 `requirements.txt`。

**報表內容**：

- 〈摘要〉：匯出時間、結束狀態（完成／中止＋中止原因）、樣本總數與有效樣本數、搜尋軸、起訖時間、最佳功率與其座標與序號、最終座標、各軸校正參數快照。有 μm 欄時附一行警語——μm 是依機械校正參數換算的**估算顯示值、不是實測位移**（見 [axis-calibration.md](axis-calibration.md)），報表被單獨傳出去時這句是唯一能阻止讀者當實測值引用的東西。
- 〈樣本〉：每筆一列，`#`／時間／各軸 pulse／各軸 μm 估算／功率 dBm／有效／備註。凍結窗格 + 自動篩選。

**兩個容易改錯的地方**：

- 🔴 **`ok=False` 但 `power` 有數值時，功率欄照樣寫出，不可抹掉。** 那是 `Sample` docstring 講的第二種情況（讀值低於絕對下限，`min_valid_power_dbm`），該數值是事後判斷「門檻是不是設太高」的唯一依據；「有效」欄已經分辨得出來。只有通訊失敗（`power=None`）才留空。
- **軸欄依 `AXES` 的固定順序排，不是依 `coords` 的鍵出現順序**（`_sample_axes()`）——否則同一輪不同樣本的欄序可能不一致。不在 `AXES` 裡的鍵附在最後而不是丟掉：報表少一整欄比多一欄難察覺得多。

**寫檔方式**：比照 `_write_json_with_backup()`，先寫 `.tmp` 再 `replace()`；中途失敗會清掉 `.tmp`。半殘的 xlsx 用 Excel 開起來會直接報毀損，比沒有檔案更難診斷。

**回歸測試**：[verify_scan_export.py](../verify_scan_export.py)（24 項）——**真的把檔案寫出來再用 `zipfile` 解開 xlsx 內部 XML 讀回驗證**，沒有 mock 掉 `xlsxwriter`。這個功能唯一的價值就是「產生的檔案 Excel 打得開、欄位對得上」，把寫檔那段換成假物件等於什麼都沒測到。用 `zipfile` 而非 openpyxl 是為了不再多一個測試專用相依（venv 也沒有 openpyxl）。

#### 尋光分頁的預設速度調成 10 倍（2026-08-27，配合分度變更）

本節由 AI 協助整理（This document was AI-assisted）。

**改了什麼**：「尋光」分頁〈速度設定〉的四個預設值全部調成原本的 10 倍——`l_speed` 5→50 pps、`f_speed` 1000→10000 pps、`rate` 100→1000 ms、`s_rate` 5→50 %（[main_ai.py](../main_ai.py) `_build_tab_scan` 的 `tk.StringVar` 初始值）。

**為什麼**：驅動器的**分度（division）設定調整為原本的 1/10**——同一顆馬達走同樣的物理距離，現在對應的 pulse 數變成 10 倍（每個 pulse 代表的實際位移縮小為 1/10）。速度單位是 pps（pulse/sec），分度變細後，原本的 pps 數字對應的物理移動速度也跟著掉到 1/10；把尋光分頁四個速度預設值同步調成 10 倍，是為了讓實際移動速度維持跟分度調整前一致，不是單純把尋光調快。

🔴 **這是「尋光」分頁專屬的預設值，跟〈移動控制〉分頁的〈速度設定〉卡（[main_ai.py:1180-1186](../main_ai.py#L1180-L1186)）是兩組獨立的 `tk.StringVar`，這次沒有跟著改。** 若〈移動控制〉分頁之後也要因為同一個分度變更調整手動點動速度，需要另外處理，不要假設兩邊已經同步。

**已一併修掉的不一致**：`_do_start_scan()` 組 `scanner_kwargs` 時，四個速度欄位若被使用者清空會退回救援預設值（[main_ai.py](../main_ai.py) 的 `scanner_kwargs` 建構處），原本這組救援值還停在舊的 10 倍前數字（"5"/"1000"/"100"/"5"），沒有跟著 Entry 的新預設一起改，正常路徑不會踩到（Entry 一開始就帶新預設），但使用者清空欄位時會悄悄退回分度調整前的舊速度。已同步改成 "50"/"10000"/"1000"/"50"。

⚠ **這次改動未附回歸測試**——四個值都是 UI 預設字串，`verify_blind_scan.py`／`verify_scan_export.py` 等既有測試都是自建 `FiberAlignmentScanner` 時直接傳入速度參數，不經過這條 GUI 預設值路徑，沒有測試需要同步更新，也沒有新增測試涵蓋「分度變更後速度預設是否正確换算」這件事本身（那屬於實機校正判斷，不是程式邏輯可驗證的範圍）。

#### Powell 尋光路徑接進 GUI（2026-08-27～28）

本節由 AI 協助撰寫（This document was AI-assisted）。

**改了什麼**：新增第二套主搜尋演算法——[fiber_scanner_advanced.py](../fiber_scanner_advanced.py) 的 `run_stage_powell()`，用 `scipy.optimize.minimize(method='Powell')` 對已勾選的軸做聯合最佳化，取代 `fiber_scanner.py` 三階段設計的階段一＋階段二。合成旋轉橢圓耦合曲面測試中收斂誤差約為座標下降的 1/10（6 pulse vs 63 pulse），完整設計脈絡見 [FIBER_ALIGNMENT_SCAN_DESIGN.md](../FIBER_ALIGNMENT_SCAN_DESIGN.md)〈第三輪〉。2026-08-27 先落地演算法本身（純假物件驗證），2026-08-28 接進「尋光」分頁：`main_ai.py` 卡片一新增「演算法」下拉選單（座標下降 / Powell），選 Powell 時自動把「起始步長」與「啟用階段二局部精修」灰階（Powell 不吃這兩個設定，涵蓋了原本階段一＋二的範圍），並在選單下方顯示紅字警示「僅通過假物件測試，尚未真機驗證」；進階設定卡新增 Powell 專屬的 `max_iterations`（函式評估次數上限）輸入框——`xtol_pulse`／`ftol_sigma_mult`／`penalty_lambda` 三個容差參數維持不開放輸入（下一段說明原因）。

**為什麼容差參數不開放 GUI 調整**：architect 審查結論——這三個值調錯了是**靜默失效**（搜尋提早停在錯的地方，或跑到 `max_iterations` 才停，介面上看不出差別），操作員在還沒拿到真機數據前也沒有回饋依據能判斷該往哪個方向調，開放輸入只會製造「調錯了也不知道」的風險。三個值改成模組層級具名常數 `DEFAULT_XTOL_PULSE` / `DEFAULT_FTOL_SIGMA_MULT` / `DEFAULT_PENALTY_LAMBDA`（`fiber_scanner_advanced.py`），要校準時直接改這三個常數，不經過 GUI。

**scipy 缺席時的兩道防線**：`main_ai.py` 用 `fiber_scanner_advanced._SCIPY_AVAILABLE` 判斷——未安裝時演算法下拉的 `values` 直接不含 Powell 選項（使用者選不到），且啟動搜尋前 `_do_start_scan()` 另外二次攔截「`algorithm == "powell"` 但 `_SCIPY_AVAILABLE` 為 False」這個組合並跳出明確錯誤訊息。🔴 **這道二次攔截不是多餘的防禦性程式**：`scanner_config.json` 可能存過先前裝了 scipy 時選過的 `"powell"`，換到沒裝 scipy 的環境啟動時，若沒有這道檢查，錯誤只會在背景執行緒裡才炸出來，被 `_run()` 的 `except Exception` 接住，誤分類成「未預期例外」，使用者看不出真正原因是缺套件。

**無訊號偵測必須掛在真實量測之後，不能等 `minimize()` 跑完**：座標下降在 `run_stage1()` 的 cycle==1 就會做訊號確認檢查，十幾筆樣本內就能判定無訊號並中止。Powell 若沒有對應的 hook，`abort_if_no_signal` 要等整輪 `minimize()` 跑完（最多 `max_iterations` 次移動＋量測，真機是數分鐘量級）才有機會觸發，形同虛設。落地方式：`run_stage_powell()` 新增 `early_signal_check` 參數，`objective()` 每完成一次**真正的測量**（不是快取命中、也不是撞限位／量測失敗的懲罰分支）就呼叫一次；`fiber_scanner.py` 的 `_run_primary_algorithm()` 包了一個呼叫 `_check_signal_detectable()` 的 closure 傳進去。它拋出的 `NoSignalAbort`／`ScanAbort` 刻意讓例外沿 `objective() → minimize() → run_stage_powell()` 自然往外傳，不透過 scipy 的 `callback=` 參數（那條路徑依賴 scipy 內部怎麼處理 callback 拋出的例外，不保證跨版本行為一致）。實測效果：無訊號判定從「等整輪跑完」提前到第 4 次量測。

**`persist_samples()` 的雙層防禦**：Powell 這輪的 metadata（`_powell_param_snapshot()` 讀到的 xtol／ftol／penalty／max_iterations）要寫進 JSON 與 Excel 摘要。這個函式一度用 `inspect.signature()` 反射 `run_stage_powell()` 的參數預設值，理由是「怕另外硬編一份數字、跟簽章預設值不同步」——但這個呼叫點在 `persist_samples()`／`_persist_samples_xlsx()` 的 try 保護範圍**外**，日後若參數改名（`xtol_pulse` → `tol_pulse`，這正是待實測校準參數的常見下場），`sig.parameters["xtol_pulse"]` 會直接 `KeyError`，炸穿 `run()` 的 `finally`，導致整輪 JSON 樣本檔一個字都不會寫出。改成直接讀模組常數（見上）本身就不會因參數改名而 `KeyError`；呼叫端 `persist_samples()` 與 `_persist_samples_xlsx()` 各自再包一層 `try/except` 當第二道防線，metadata 寫入失敗只記 log、不影響樣本檔本體寫出——同一個理由跟〈尋光樣本的 Excel 報表〉一節的 xlsx 失敗處理原則一致。

**演算法選擇如何存/取**：跟既有的 `_scan_blind_mode_labels` 慣例一致，Combobox 存**顯示標籤字串**，換算回內部代碼一律查反向 map `self._scan_algo_label_algos`，不可用 `.index()`（`ORG_MODES` 差一位是前車之鑑，見 CLAUDE.md 紅線速查）。`algorithm` 與 `powell_max_iterations` 存進 `scanner_config.json`，跟 `blind_mode` 同類——跨次搜尋穩定的設定，該存。🔴 若設定檔存了 `"powell"` 但目前環境沒裝 scipy，UI 建立階段就會把它退回 `"coordinate_descent"`（不改動存檔本身），下次啟動 `_do_start_scan()` 存檔時才會把這次「實際選了什麼」寫回去覆蓋掉——換句話說沒有「保留使用者原本選的 Powell、等裝回 scipy 自動恢復」這回事，裝回 scipy 後需要使用者重新手動選一次。

**回歸測試**：[verify_scan_powell.py](../verify_scan_powell.py)（8 項，`run_stage_powell()` 本身：收斂行為、限位／量測失敗的懲罰分支、中止例外的傳遞）＋ [verify_scan_powell_integration.py](../verify_scan_powell_integration.py)（17 項，GUI 與 `fiber_scanner.py` 分流邏輯：演算法下拉切換時的欄位灰階、scipy 缺席時的雙重防線、`_powell_param_snapshot()` 的雙層防禦、無訊號早偵測的觸發時機），連同既有套件共 379 項全數通過。**全部是假物件驗證，尚未真機驗證**——`xtol_pulse`／`ftol_sigma_mult`／`penalty_lambda` 仍是待校準的起跳值，正式在真機上用 Powell 之前應先小範圍試跑並全程留意（GUI 上的紅字警示就是提醒這件事）。
