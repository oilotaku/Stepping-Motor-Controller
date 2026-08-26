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
- **`NoSignalAbort(ScanAbort)` 子類別**：`blind_mode="auto"` 時 `run()` 用 `except NoSignalAbort` 精確攔截。🔴 **絕對不可寫成 `except ScanAbort`**——使用者主動停止與 EMS 觸發也是 ScanAbort，那會讓「按下停止」立刻換來一輪掃過上千格點的盲搜，結果完全相反。`verify_blind_scan.py::TestBlindModeIntegration::test_user_stop_never_triggers_blind_search` 鎖這一點。
- **auto 模式會先把滑台移回起點再盲搜**（`_return_to_machine`）：階段一在純雜訊上會亂爬，漂走後那裡不該當圓心——半徑是使用者相對**起點**設定的。
- **`last_abort_kind`（"no_signal"／"other"）**取代呼叫端對訊息字串的子字串比對。新增四種無訊號中止訊息時，舊的字面量比對會讓它們全部靜默掉進「其他錯誤」分支。

**預設值與已知限制**：

- 🔴 **函式庫預設 `blind_mode="off"`，GUI 預設 `"auto"`**（`GUI_DEFAULT_BLIND_MODE`）。盲搜是本專案單次自動運動量最大的操作，沒有明確要求就自動跑起來違反「寧可少搜不多動」；GUI 是使用者看得到、可取消、且確認對話框會明列掃描規模的情境，兩者不衝突。
- 預設 `blind_step=200` / `blind_max_radius=1000` ＝ 121 點（粗估半分鐘），刻意讓「預設按下去」規模溫和。早期版本半徑 4000 ＝ 1681 點、七分鐘以上的無人看管運動，不適合當預設。
- 🔴 **格距必須小於耦合光斑的尺度，否則螺旋會直接跨過訊號區而漏掉。** 單模纖芯約 9μm（以 2μm/pulse 換算約 5 pulse）、多模 50/62.5μm 也不過 25~30 pulse——**200 pulse（約 400μm）對這兩種都太粗**，只適合「光斑很大／只是要先確認大方向」。真要靠盲搜找到單模耦合，格距得往個位數 pulse 設，而那會讓同樣半徑的點數暴增，必須同時縮小半徑。這個取捨沒有通用解，GUI 的即時格點數估算（`_update_blind_estimate`）就是為了讓它看得見。
- ⚠ **本輪改動全部以假物件測試為準，尚未實機驗證**：自動量程在實機上能不能讀到無光底噪、`+9.9E+37` 是否確實是無光時的回應（目前由 log 的 17-byte 回應長度推得）、量程自動退回的實際行為，都要等下次接上 GPIB 才能確認。

**回歸測試**：[verify_blind_scan.py](../verify_blind_scan.py)（35 項）——螺旋幾何、雜訊校準的零有效讀值偵測、事故的直接回歸鎖（不空轉、不移動）、盲搜找峰/掃完/限位跳過/幾何不漂移、三種模式串接、使用者中止與 EMS 的安全鎖、量程鎖定回呼。
