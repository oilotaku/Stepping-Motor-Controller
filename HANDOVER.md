# HANDOVER.md

給接手這份程式碼的下一個開發者。這份文件回答「我現在該從哪裡開始、現在卡在哪裡」，不重複 CLAUDE.md／README.md 已經寫過的細節——那兩份文件分別是「AI 助理的完整操作指引」與「開發者快速上手」，這份只做兩件事：**指路**、**交代現況與未解事項**。

編製日期：**2026-09-09**（`reporter` 代理補齊 2026-09-01～2026-09-09 的落差，基底是 2026-09-01 版全文重寫）。前一版編製於 2026-09-01（彙整 2026-08-20～08-31）；中間 2026-09-01～09-09 的分資料夾遷移、兩項安全修正、一項獨立模擬工具修正，這次一次補齊。**這次是逐段補丁，不是重寫**——沒提到的段落內容與 2026-09-01 版相同，只有下面列出的段落有更動。

## 先讀這些，依這個順序

1. **[README.md](README.md)** — 5 分鐘搞懂這是什麼、怎麼跑起來。
2. **[CLAUDE.md](CLAUDE.md)** — 索引檔，逐次改動同步更新，是目前唯一跟得上程式碼進度的文件。開頭有〈紅線速查〉，動手前先掃過。
3. **[docs/](docs/)** — CLAUDE.md 2026-08-26 拆出的完整技術紀錄（執行緒、限位/安全、尋光、設定檔、通訊協定、軸校正、硬體、UI、打包、模組化、測試，各一份）。CLAUDE.md 有一張「動到什麼先讀哪份」的對照表，照表操作，不要略過。
4. **`ds102 (2).pdf`**（根目錄，未進版控，需另外取得）— DS102/DS112 官方操作手冊，指令格式的權威來源。查指令前先翻它，不要用猜的。

## 現在的狀態

- **分支拓樸（2026-09-01 用 `git branch -a` / `git show-ref` 實測確認）**：這個 repo 裡**只有 `feat/fiber-search-gui` 一條分支**，本地與 `origin` 皆是，`origin/HEAD` 也指向它，沒有任何 `main` 分支存在（本地、遠端、tag 都查過，一個都沒有）。
  **舊版 HANDOVER 寫的「尚未合併回 `main`，領先 43 個 commit」這個框架已經不成立**——沒有一個叫 `main` 的分支可以拿來比較領先幾個 commit。這不是「已經合併了」，而是**這個 repo 本身根本沒有 `main`**：可能是專案一開始就只用這一條分支在做、也可能 `main` 存在於使用者本機的另一個 clone 或尚未推上來的地方。**這件事我無法從 repo 內部判斷，需要使用者說明目前的分支/發布策略**——`feat/fiber-search-gui` 這個名字本身聽起來像是一條 feature 分支，但它就是目前唯一可用的分支。
- **當前分支 `feat/fiber-search-gui`**：把光功率計（HP 8153A）與尋光演算法（`FiberAlignmentScanner` / Powell 共軛方向法）整合進 GUI，外加 2026-08-17 起陸續做的架構調整（`DS102Controller` 拆檔、UI 色票外部化、`LongOperation`/`PowerReading` 具名狀態）、軸機械校正參數、原點復歸重現性量測、尋光的盲搜與撞限位修正等大量安全性與功能工作。細節見下方〈這次做了什麼〉。
- `main_ai.py` **7114 行**（2026-08-31 實測，見下方模組化複審記錄）。CLAUDE.md 已明確記載「行數對『該不該拆』沒有預測力」，不要單看這個數字下判斷。**2026-09-03 分五步把根目錄檔案搬進 `core/`／`legacy/`／`tools/`／`sim/`／`tests/` 五個子資料夾**（`main_ai.py` 架構上固定留在根目錄不搬），細節見下方新增的〈分資料夾遷移〉一節，行數本身未受影響。
- 回歸測試（假物件，不需硬體）**436 項全數通過，11 支 `tests/verify_*.py`**（2026-09-09 用 `venv/Scripts/python.exe -m pytest -q` 完整重跑確認：分資料夾遷移後 436 項全數通過，非只是可收集，44.46 秒；7 則 `PytestUnhandledThreadExceptionWarning`，是背景 worker 執行緒在 Tk root 已於測試 teardown 銷毀後才嘗試讀 `tk.StringVar` 觸發的 `RuntimeError: main thread is not in main loop`，屬 teardown 期間的既有時序 warning、非 failure，與本次遷移或安全修正無關，見〈交接注意事項〉指令）。

## 已知未解決事項

### 硬體層級

- **DATA1 驅動器分度值已確認生效（2026-08-21 解決，見 [docs/protocol.md](docs/protocol.md) 第 23 行）**。舊版 HANDOVER 把這個列為頭號「真的卡住」問題，**現在已經不是**：使用者用 DATA1 兩極端值（Full-step vs 1/10）重新對比測試，固定 pulse 數移動同一軸，實際移動距離確實等比例縮短，與公式預期一致，取代了 2026-08-18 當時「感覺沒有變少」的疑慮。`axis_calib.division` 的 μm 換算公式因此可信。第二顆「division changing-over switch（R1/R2）」的實際位置仍未確認（手冊那頁是圖片，文字擷取工具讀不到），但不影響這個結論。

### 程式層級（2026-09-01 用 `grep` 逐項重新核對；第 3 項已於 2026-09-03 修正，第 4 項為 2026-09-03 新發現並已修正的問題，剩下第 1、2 項仍然存在）

1. ~~長按點動的 Python 端限位保護預設不生效~~——**2026-09-02 已部分修正**。`core/ds102_ctrl.py` 的 `DS102Controller.sync_sw_limits_from_controller()`（architect 評估、`coder` 落地、architect 收尾審查）在 `connect()` 的 `restore_controller_config()` 之後，逐軸逐側查 `CWSLE?`/`CCWSLE?`/`CWSLP?`/`CCWSLP?`，把韌體端限位鏡射進 `self.sw_limits`——合併規則「只收緊、永不放寬、永不清空」：使用者手動輸入並套用過的值不會被覆寫，兩邊都有值時取較嚴格的一側；`main_ai.py` 連線成功時據此跳「已同步」／「⚠ 沒有任何行程保護」兩則橫幅。**這治不好病根本身**——韌體軟體限位出廠／斷電後就是停用的，此時查回來的 `CWSLP?`/`CCWSLP?` 是哨兵值（±99999999），同步邏輯視為無有效邊界，`sw_limits` 依然是 `None`；出廠狀態下長按點動依然全程只靠韌體端限位保護（此時韌體端本身也已停用），跟修正前的實際保護效果相同。這次修法做到的是「兩層彼此同步、無保護時明講」，不是「預設就有保護」——後者需要使用者主動設定並持久化韌體限位。新增 `tests/verify_sw_limit_sync.py`（15 項），**假物件驗證，未真機驗證**：出廠狀態連線後無保護橫幅是否正確跳出、手動設定韌體限位後「目前生效」欄數值是否相符、尋光在 `sw_limits` 有值時 `_targets_reachable()` 是否會誤擋起步，皆待確認。
2. **`play_recording` 仍繞過 `_check_sw_limit` 與 `PULS` 整數正規化**——`grep -n "_check_sw_limit\b" core/ds102_ctrl.py` 只在 `move_step`／`goto_point` 相關路徑出現，`play_recording()`（3561～3720 行左右）本體完全不呼叫它；錄製時每步 delay 仍是寫死 800ms（非實際按住時間）。重播無法還原點動的實際行程長度，不能當安全功能用。
3. ~~`_toggle_connect` 的中斷分支仍在 Tk 主執行緒做同步序列 I/O~~——**2026-09-03 已修正（architect 評估、僅假物件驗證）**。`ctrl.stop()` + `ctrl.disconnect()` 搬進背景執行緒（比照連線分支既有的 `_do()` 寫法），UI 更新抽成 `_on_disconnect_result()` 透過 `root.after()` 回主執行緒；中斷期間立即 disable `_conn_btn` 避免連點疊出第二條中斷執行緒；`root.after` 呼叫前檢查 `_shutting_down`（連線分支既有的同一個缺陷這次一併補上）。`core/ds102_ctrl.py` 的 `DS102Controller.disconnect()` 同時把 `self.connected = False` 移到 `ser.close()` 之前，消除「其他執行緒看得到 connected=True 但 port 已關」的窗口。**architect 明確定調這個修法不會根治 `ser.close()` 真的卡死的情況**（Win32 `CloseHandle` 理論上仍可能不返回），只是把症狀從「UI 凍結」降級成「按鈕卡在『中斷中...』」——`ser.close()` 沒有可中止機制。**拔線/斷線情境下是否真的會卡住仍待實機驗證**，本次未做。詳見 [docs/safety-fixes.md](docs/safety-fixes.md)〈仍然存在〉第 3 項。
4. ~~全軸原點復歸中 CW/CCW 按鈕仍可點動~~——**2026-09-03 已修正，使用者回報並實機驗證通過**（commit `87b9712`）。根因不是缺守衛，是 `state="disabled"` 對 CW/CCW 這兩顆按鈕根本沒生效過：它們用 `.bind("<ButtonPress-1>", ...)` 掛事件而非 `command=`，tkinter 的 `state="disabled"` 只擋 `command=` callback，不擋 `.bind()` 掛的低階事件——`_set_drive_buttons_state("disabled")` 過去只讓按鈕視覺變灰，實際點擊仍會觸發 `_on_cw_press`/`_on_ccw_press`。危險之處：`origin_all()` 對正在復歸的那一軸會暫時關閉軟體限位（原點通常落在行程末端 POS≈0，會被軟限位擋住），此時若還能發出點動指令，等於在唯一的安全網關閉時移動該軸。修法：`_on_cw_press`/`_on_ccw_press` 的守衛加上 `self._any_long_op_running()`，一次涵蓋全軸復歸／尋光／原點復歸重現性量測／重播四項。既有 436 項回歸測試全數通過（沒有測試直接覆蓋這個缺陷，本來就要人工按按鈕才會發現）。**2026-09-03 實機驗證通過**：全軸原點復歸執行中按住 CW/CCW 完全無反應。詳見 [docs/safety-fixes.md](docs/safety-fixes.md)〈全軸原點復歸中 CW/CCW 仍可點動〉。

### 測試涵蓋缺口

- HP 8153A **channel A 從未驗證過**（本次 grep 相關 commit 與 `core/meter_GPIB.py` 未見新進展），只確認 channel B 可正常回應；channel A 據稱需要外接光學頭，仍是轉述資訊，沒有第二來源佐證。
- **尋光 GUI 端到端的真機驗證仍不完整，但比 2026-08-19 時進了一步**：
  - Powell 共軛方向法路徑（2026-08-27～28 新增並接進 GUI）**全部只做過假物件驗證**，GUI 上會顯示紅字警示「僅通過假物件測試，尚未真機驗證」——`xtol_pulse`/`ftol_sigma_mult`/`penalty_lambda` 仍是待校準的起跳值。
  - 座標下降路徑的〈撞限位不再反覆撞〉修正**已於 2026-08-27 做過一次實機驗證**（COM2，Y 軸，見 [docs/fiber-scan.md](docs/fiber-scan.md)〈撞限位不再反覆撞〉），但驗證範圍**只涵蓋 `_move_relative()`（單軸路徑）**——`_move_multi_axis()`（盲搜／階段二走的多軸同時出發路徑）在實機上的撞限位行為、`_travel_bounds` 跨 `run()` 重置的實機效果、以及方向探測雜訊門檻／`STAGE1_MAX_PASSES_PER_STEP` 保險絲在真實雜訊統計特性下是否真的收斂，**都還沒驗證**。
  - 階段零盲搜與光功率計自動量程退回機制（2026-08-26）**只有 log 觀察佐證、未完整實機驗證**：實機 log 證實自動量程確實會讀到值，但讀值是間歇性的；量程自動退回的實際行為要等下次接 GPIB 才能確認。
- **新增**：`sim/gaussian_vector_sim.py` / `sim/gaussian_vector_sim_gui.py`（2026-08-28～31 新增，2026-09-09 修正一項 GUI 端 bug，見下方新增小節）是純數學模擬工具，**完全不連硬體、不 import 任何 DS102 相關模組**，只是輔助觀察尋光演算法梯度場行為與調參的工具，不能算作任何形式的真機驗證，也不應被誤認為填補了上述缺口。

### 文件同步缺口

- **`FIBER_ALIGNMENT_SCAN_DESIGN.md`**：**2026-09-03 已由 `reporter` 代理補齊**——新增〈第四輪：2026-08-26～31 實機事故修正與模組化〉，涵蓋階段零盲搜／光功率量程修正／撞限位反覆撞修正／Excel 報表匯出／殘差診斷與收尾曲率擬合微調／`LongOperation`/`PowerReading` 模組化複審／`sim/gaussian_vector_sim.py`，每項皆標註驗證狀態（假物件測試／部分實機驗證／無驗證）。檔頭「最後更新」已改為 2026-09-03。這份落差目前已補齊，不再是待辦。
- **Notion〈DS102/DS112 步進馬達控制器專案現況總覽〉**（HANDOVER 舊版誤稱〈尋光系統工程報告〉，實際標題如上，連結見下）：**2026-09-03 查證發現已由他人／其他 session 在 2026-09-02 補上〈十一、2026-09-02 補記〉一節**，內容已涵蓋上述同一批七項落差（含模組化複審），並附「Claude Code session 事後補記、非原作者查證」標註——**先於本次 `reporter` 任務就已經補齊，代理查證後未重複寫入**。頁面連結：https://app.notion.com/p/3bf070110f3481ae8e38d43781bd1b97 。
  - **附帶發現，非本次任務範圍，僅供使用者參考**：這個 Notion 空間底下有一個關聯子頁〈🎥 視覺輔助粗對準（影像辨識）規劃與 A0 架構決策〉，本次沒有展開查證其內容，也不確定這是不是一條正在進行、卻完全沒反映在本 repo 的程式碼或文件裡的另一條工作線。若確有其事，建議使用者自行確認狀態並決定要不要在 CLAUDE.md／HANDOVER.md 記一筆。
  - 兩份文件的落差**截至 2026-09-03 皆已補齊**，但兩者仍是「補記」性質而非逐次同步——後續開發持續累積新進度後，落差還會再出現，下次需要全面複審時建議直接請 `reporter` 代理重新彙整，而不是逐段補丁。

## 這次（2026-08-20～2026-08-31）做了什麼

以下依主題分組，不逐條照抄 commit message；每項都標了對應的 docs/ 章節，細節請直接讀那份文件，這裡只整理重點結論。

### 光功率背景輪詢移動中自動暫停（2026-08-20）

`_start_meter_poll_worker` 原本只在尋光執行中暫停，使用者在〈移動控制〉分頁手動點動/單步/原點復歸時 GPIB 輪詢仍在跑，讀到的是馬達震動期間的雜訊值且會悄悄流進 CSV。新增 `DS102Controller.motion_active` 唯讀 property（涵蓋點動/步進/原點復歸/重播/尋光），光功率背景輪詢與狀態列文字依此暫停/恢復。`motion_active` 只服務「要不要占用其他硬體資源」的判斷，明確不可當移動守衛（同一個陷阱之前已在 `scanning_active` 上踩過一次）。詳見 [docs/fiber-scan.md](docs/fiber-scan.md)〈移動期間暫停光功率背景輪詢〉。

### 原點復歸：孿生競態修正 + 重現性量測功能（2026-08-21、2026-08-26）

- **`_wait_axis_stop()` 起步窗口競態**（2026-08-26 修）：第一次查詢約 112ms、Driving assert 延遲約 96ms，餘裕只有約 16ms，落在窗口內會把「還沒起步」誤判成「已經停好」。沿用 `_wait_origin_done_ex()` 已驗證過的三重證據配方修正，29 項回歸測試新增。仍**未實機驗證**——寬限期對真實韌體是否足夠、移動未生效會不會誤報，都要等下次接 COM2 確認。詳見 [docs/homing.md](docs/homing.md)。
- **原點復歸重現性量測**（`measure_homing_repeatability`，2026-08-21 新增）：自動化原本要人工用碼表做的量測，回答「軟體座標原點能不能當光纖對準的可信基準」。**2026-08-21 已實機驗證**（COM2，X/Y/Z 三軸 × offset 200/1000/3000 × 10 輪 = 90 次復歸，全數成功、無漂移）：重現性（peak-to-peak）**1～3 pulse，無累積漂移，且不隨離開距離變化**——這是 2026-08-05 那組三輪手動量測（因樣本太少）答不出來的結論。同一次查證也發現 **實機 MEMSW0 曾經是錯的**（Z 軸誤設為 1 導致 `GO ORG` 完全無作用，改回 2 才正常）且會被 `restore_controller_config()` 靜默還原回錯誤值（因為 MEMSW 是 RAM-only、設定檔存的正是那組疑似錯誤的值）——這是實測中真正踩到的坑，不是臆測，詳見 [docs/homing.md](docs/homing.md) 該節。
- **移動控制分頁座標統一由重繪迴圈供應**（2026-08-26）：修正 `POS` 顯示在 `GO ORG` 期間與 `StatusBar` 不同步的問題，已標記為 2026-08-26 實機驗證通過。

### CLAUDE.md 拆分為索引＋docs/（2026-08-26）

原本 614 行的單一 CLAUDE.md 拆成索引檔＋ `docs/` 下 11 份主題文件（threading／homing／safety-fixes／fiber-scan／settings-files／protocol／axis-calibration／hardware／ui-design／packaging／modularization／testing），內容一字未刪，只是改成按需讀取，降低每次對話的固定載入量。本次重寫 HANDOVER.md 的資訊來源大多來自這批拆出的文件。

### 尋光：無光位置補階段零盲搜 + 光功率計量程 bug（2026-08-26，實機事故修正）

**事故**：使用者在無光位置按下尋光，滑台一步都沒動，82 個樣本全部無效，15 秒後才丟出誤導性訊息。根因兩層都修了：
1. `meter_GPIB.__init__` 把量程無條件鎖死在 -20dBm，無光時讀到 sentinel `+9.9E+37` 且完全不記 log。改成預設自動量程＋讀到 sentinel 時自動退回。
2. 演算法在讀不到值時靜默空轉（`_search_axis_once()` 起點量不到就當成「已收斂」而不移動）。新增階段零盲搜（`run_stage0_blind()`，方形螺旋掃描）在完全無梯度可循時派上用場。

**修正過程本身有兩次判斷失誤，值得記住**：第一版盲搜的觸發判準用「階段一有沒有拋例外」而非「有沒有確認到訊號」，實機打臉（撞限位導致樣本不足但沒拋例外，盲搜一次都沒觸發）；第一版也曾把「底噪讀不到值」當成拒絕盲搜的理由，但那正是盲搜最該派上用場的情境。兩者都已修正並有回歸測試鎖住（`tests/verify_blind_scan.py`，44 項）。本輪改動除了幾筆 log 觀察外，**仍以假物件測試為準，尚未完整實機驗證**。詳見 [docs/fiber-scan.md](docs/fiber-scan.md)〈階段零：盲搜粗掃〉。

### 尋光：撞限位反覆撞 + 階段一收斂修正（2026-08-26～27，實機事故修正，部分已實機驗證）

**症狀**：使用者回報「尋光常常 Detect limit」，實機 log 顯示一輪盲搜實撞了 50 次限位。根因兩層：
1. `ctrl.check_sw_limits_batch()` 在實機上是 no-op（`sw_limits` 預設全空），加上每個候選格點都用「絕對目標 − 目前座標」重算 delta，卡在限位上時下一格點算出的 delta 幾乎相同 → 原地反覆撞同一顆開關。修正：新增 `_note_limit_hit()` / `_targets_reachable()`，撞過一次後記住該側邊界，之後同側超界的候選點送指令前就被擋掉。
2. 階段一在純雜訊下不會結束——方向探測用赤裸的 `>` 比較、沒有雜訊門檻，純雜訊環境下兩側輪流「看起來比較好」。假物件重現：**修正前 180 萬次移動仍在擺盪（其中 60 萬次真的撞限位）；修正後 8 次移動、2 次撞限位、正常結束**。

**2026-08-27 部分實機驗證通過**（COM2，Y 軸，scratchpad 一次性診斷腳本）：確認 `_move_relative()` 撞限位後 `_note_limit_hit()` 正確記錄邊界、同方向再送候選點時完全不再送出任何 GO 指令。驗證範圍**只涵蓋單軸路徑**，多軸同時出發（`_move_multi_axis()`）與跨輪重置未驗證，見上方〈測試涵蓋缺口〉。詳見 [docs/fiber-scan.md](docs/fiber-scan.md)〈撞限位不再反覆撞〉。

### 尋光：樣本 Excel 報表匯出（2026-08-26～27）

每輪尋光結束時在 JSON 樣本檔旁邊多寫一份同名 `.xlsx`（〈摘要〉+〈樣本〉兩張表）。兩個設計要點：xlsx **不取代** JSON（JSON 才是完整無損、給程式讀的原始紀錄）；xlsx 的任何寫入失敗**只記 log、絕不往外拋**（最常見失敗是 Windows 上檔案正被 Excel 開著）。回歸測試（`tests/verify_scan_export.py`，24 項）刻意真的把檔案寫出來再用 `zipfile` 解開驗證，沒有 mock 掉 `xlsxwriter`。詳見 [docs/fiber-scan.md](docs/fiber-scan.md)〈尋光樣本的 Excel 報表〉。

### 尋光：速度預設值調成 10 倍（2026-08-27，配合分度變更）

驅動器分度（division）調整為原本的 1/10 後，同樣的 pulse 數對應的物理位移縮小為 1/10，「尋光」分頁的四個速度預設值同步調成 10 倍以維持實際移動速度不變。這只動了「尋光」分頁自己的一組 `tk.StringVar`，跟〈移動控制〉分頁的速度設定卡是完全獨立的另一組，**沒有跟著改**，兩邊不要假設已同步。詳見 [docs/fiber-scan.md](docs/fiber-scan.md)〈尋光分頁的預設速度調成 10 倍〉。

### 原點復歸例外訊息 NameError 修正 + 測試文件拆到 docs/testing.md（2026-08-27）

一則例外訊息在 lambda 閉包內撞 `NameError`，已修正。測試相關內容從 CLAUDE.md 拆到獨立的 [docs/testing.md](docs/testing.md)。

### Powell 共軛方向法尋光路徑（2026-08-27～28，僅假物件驗證）

新增 `core/fiber_scanner_advanced.py` 的 `run_stage_powell()`，用 `scipy.optimize.minimize(method='Powell')` 取代座標下降的階段一＋階段二。合成旋轉橢圓耦合曲面測試中收斂誤差約為座標下降的 1/10（6 pulse vs 63 pulse，**合成資料，非真機數據**）。2026-08-28 接進「尋光」GUI 分頁的演算法下拉選單，`xtol_pulse`/`ftol_sigma_mult`/`penalty_lambda` 三個容差參數刻意不開放 GUI 調整（architect 審查結論：調錯是靜默失效，操作員在無真機數據前也無從判斷該往哪調）。**GUI 上會顯示紅字警示「僅通過假物件測試，尚未真機驗證」**，這是刻意保留的提醒，不要移除。新增 `tests/verify_scan_powell.py`（8 項）＋ `tests/verify_scan_powell_integration.py`（17 項）。詳見 [docs/fiber-scan.md](docs/fiber-scan.md)〈Powell 尋光路徑接進 GUI〉與 `FIBER_ALIGNMENT_SCAN_DESIGN.md`〈第三輪〉。

### 多軸高斯向量圖模擬工具（2026-08-28～31，完全獨立、不連硬體）

新增 `sim/gaussian_vector_sim.py`（CLI）與 `sim/gaussian_vector_sim_gui.py`（獨立 tkinter GUI），逐行對照 `core/fiber_scanner.py` 的搜尋邏輯移植，用來在沒有硬體時觀察梯度場長相、驗證尋光演算法行為並調參。**搜尋邏輯必須忠實重現 `core/fiber_scanner.py`，不可另外設計通用演算法**——早期版本用 steepest descent + Rprop，與真實座標下降邏輯不符，已重寫；真實梯度只用來畫背景向量圖，搜尋本身跟真機一樣只靠「移動＋量測」的有限差分。完全不 import 任何 DS102 相關模組，不連接硬體，因此**不能算真機驗證的替代品**（見上方〈測試涵蓋缺口〉）。**2026-09-09 另修正一項 GUI 端 bug，見下方新增的〈2026-09-01～2026-09-09 補充〉一節。**

### 尋光演算法殘差診斷與收尾曲率擬合微調（2026-08-31）

補上殘差診斷與收尾曲率擬合的微調，細節見 commit `08e0bcd` 與對應程式碼；本次彙整未進一步展開，若要動這塊建議直接讀 diff。

### `state("zoomed")` 跨平台安全化（2026-08-31）

主視窗預設最大化的呼叫方式改成跨平台安全呼叫（`state("zoomed")` 是 Windows 專屬 API，非 Windows 平台呼叫會拋例外），詳見 [docs/ui-design.md](docs/ui-design.md)。

### main_ai.py 模組化正式複審 + 前置 0～3 全部完成（2026-08-31）

`architect` 對照四條既有門檻逐條複審（行數已達 6820 行觸發門檻 4），**結論：仍不拆分頁 mixin**——理由不是「還不夠痛」，而是「拆分頁解決不了現在真正在痛的東西」。複審過程中額外抓到 **三項散彈式接線安全缺陷**：尋光執行中按 Esc 或「■ Stop」滑台會停一下又自己繼續走、尋光/重播執行中按頂列「中斷」背景執行緒會繼續對已關閉的序列埠送指令、`_do_stop` 對重播的行為與 `_on_escape` 不一致。三項全部修正（前置 0）。另完成三項模組化前置工作：

- **前置 1（`LongOperation` 註冊表）**：把「長時間背景作業」（重播/原點復歸/尋光/原點復歸重現性量測）的 UI 忙碌顯示與停止請求分派收斂成單一註冊表，取代之前要作者手動記得接進 6 個位置（`_update_stat_ui`／`_start_poller`／`_do_stop`／`_on_escape`／`_toggle_connect`／`_on_close`）的舊模式——那正是漏接三項安全缺陷的根本原因。註冊表只服務 UI 忙碌顯示與停止分派，**明確不可**把 `motion_active`/`scanning_active` 併進來當移動守衛。
- **前置 2（`PowerReading` 具名狀態）**：光功率「最近一次讀值」原本由「光功率」與「尋光」兩個分頁各自直接寫入三個裸欄位，收斂成 frozen dataclass `PowerReading`，`_scan_plot_extend()` 的 7 個分散寫入點收斂成 1 個呼叫。frozen 是唯一真正的技術理由——讓移動執行緒的跨執行緒讀取變成單一次屬性重新指派、避免撕裂讀取，**不宣稱新增任何鎖保護**。
- **前置 3（`core/ui_theme.py`）**：十個 `CLR_*` 色票抽成獨立檔案，依賴鏈 `core/ds102_ctrl.py → core/ui_theme.py → main_ai.py` 無環。原計畫要一併搬的 `_app_settings`/`_app_setting_num` **沒有搬**——複審當下才發現這兩者早在 2026-08-17 就已經在 `core/ds102_ctrl.py`（`HISTORY_MAX` 需要），搬進 `core/ui_theme.py` 反而會製造新的循環相依。

三項前置工作全數完成、413 → 421 項回歸測試零修改零破壞（`ui_theme.py` 那步）。main_ai.py 行數從 6820 漲到 7114（+294，幾乎全是機制與說明註解，非邏輯膨脹），**再次印證複審自己下的結論：行數對「該不該拆」沒有預測力**，不要單看行數變化下判斷。完整記錄見 [docs/modularization.md](docs/modularization.md)〈2026-08-31 重新評估〉起（含〈六〉～〈九〉四節）。

### 長按點動限位保護：連線時同步韌體限位（2026-09-02，僅假物件驗證）

修〈已知未解決事項〉程式層級第 1 項。`architect` 先評估（結論：韌體軟體限位出廠／斷電即停用，此時查回來的 `CWSLP?`/`CCWSLP?` 是哨兵值，這個修法治不好「出廠狀態下沒有保護」本身，只能做到「兩層彼此同步＋無保護時明講」）→ `coder` 落地新增 `core/ds102_ctrl.py` 的 `DS102Controller.sync_sw_limits_from_controller()`（`connect()` 於 `restore_controller_config()` 之後呼叫）→ `architect` 收尾審查抓到三個非阻塞問題並全部修正：「已同步」橫幅在韌體值較寬鬆被拒絕採用時仍誤報、「沒有任何行程保護」警示排在四則橫幅最後要等 30 秒以上才出現、未接滑台的軸（如實機 U 軸）被誤報成無保護。合併規則「只收緊、永不放寬、永不清空」是本次最重要的安全不變量，靠 `tests/verify_sw_limit_sync.py`（15 項）鎖住。刻意排除 `sw_limits` 本身的持久化（跨連線記住使用者手動輸入）——`POS` 是相對暫存器，跨斷電/跨原點復歸保存一組舊座標邊界有獨立風險，值得另開一次評估。

   **2026-09-02 同日實機驗證通過**（COM，X 軸，CW 限位 8000 pulse）：連續長按點動 CW 多次，`_watch_jog_limit()` 全部正確在越過限位之前送出 `STOP`，未曾滑出限位——這是這段程式碼第一次真的在真機上被觸發（過去 `sw_limits` 全空，這段路徑從未真的跑過）。同時觀察到一個純物理現象、非程式錯誤：目前點動速度參數（`F0=5000, R0=1000`）換算出的剎車距離達 2500 pulse，讓監看執行緒在離限位還有 2400～3000 pulse 時就先停，佔 8000 pulse 限位範圍的 3 成以上。這是 `_watch_jog_limit()`「寧可提早停也不要越界」設計的本質，速度參數要不要調整是獨立的後續事項，本次未動，細節見 [docs/safety-fixes.md](docs/safety-fixes.md)〈仍然存在〉第 1 項。**仍待實機驗證**：出廠狀態連線後無保護橫幅是否正確跳出、尋光在 `sw_limits` 有值時 `_targets_reachable()` 是否會誤擋起步（「手動設定韌體限位後『目前生效』欄數值是否相符」這項已隨上述驗證一併確認——`_watch_jog_limit()` 收到的 `lim=8000` 與韌體設定一致）。

### 測試套件成長軌跡

187 項（2026-08-19）→ 379 項（Powell GUI 整合後，2026-08-28）→ 413 項（前置 0/1 完成後，2026-08-31）→ 421 項（前置 2 完成後，2026-08-31）→ **436 項（新增 `tests/verify_sw_limit_sync.py`，2026-09-02）→ 436 項不變（2026-09-09 用 `pytest --collect-only -q` 重新實測，分資料夾遷移後項數維持 436、全數可收集）**，11 支 `tests/verify_*.py`。

## 2026-09-01～2026-09-09 補充

以下四項是 2026-09-01 版 HANDOVER 定稿後、`91cd894` merge 前後累積的變動，2026-09-09 由 `reporter` 代理補齊落差時新增；本檔內舊扁平路徑（`ds102_ctrl.py`／`fiber_scanner.py`／`meter_GPIB.py`／`ui_theme.py`／`fiber_scanner_advanced.py`／`verify_*.py`）已一併改成下面第一項遷移後的現況路徑（`core/`／`tests/`／`sim/` 前綴）。

### 分資料夾遷移（2026-09-03，十步 commit，純搬檔不動邏輯）

根目錄檔案分五個階段（commit `b9ddb17` → `8b7be2a`，共十筆 commit）搬進 `core/`（現役核心模組）、`legacy/`（舊版對照）、`tools/`（診斷腳本）、`sim/`（無硬體模擬）、`tests/`（pytest 測試檔）五個子資料夾：第一步先把 `_app_dir()` 改成「往上尋找含 `main_ai.py` 的目錄」定位專案根（為搬檔預留退路）、第二步搬測試檔到 `tests/`、第三步分五個子提交依序搬 `ui_theme.py`／`meter_GPIB.py`／`fiber_scanner.py`／`fiber_scanner_advanced.py`／`ds102_ctrl.py` 到 `core/`、第四步搬歷史/工具/模擬器四類檔案、第五步更新 `.vscode/launch.json` 路徑。`main_ai.py` 架構上固定留在根目錄不搬（`_app_dir()` 的定位邏輯依賴它在那裡）。動機與過程本次未見獨立說明文字（commit message 只描述「做了什麼」，沒有另外交代「為什麼現在做」），CLAUDE.md〈檔案定位〉章節已完整記錄遷移後的最終路徑對照表，這裡不重複。

隨遷移發生的文件同步分兩批：commit `445687d`（2026-09-04）補齊 CLAUDE.md／README.md／docs/testing.md 三份文件的路徑引用；**本次（2026-09-09）補的是 HANDOVER.md 自己的路徑引用**——`445687d` 當時沒有動到這份文件。

**驗證狀態**：2026-09-09 用 `venv/Scripts/python.exe -m pytest --collect-only -q` 重新實測，436 項測試全數可收集、無 import 錯誤，確認遷移後 `tests/` 底下的 `from core.ds102_ctrl import ...` 等絕對 import 路徑正常運作（`pytest.ini` 的 `pythonpath = .` 生效）。這是本次彙整唯一實際執行過的查證項目，其餘遷移細節僅來自 commit message 與 diff 比對。

### 全軸原點復歸中 CW/CCW 點動守衛（2026-09-03，commit `87b9712`，使用者回報並實機驗證通過）

已併入上方〈已知未解決事項〉程式層級第 4 項，不重複記錄——這裡只提醒它存在，因為 2026-09-01 版 HANDOVER 完全沒提到這項修正。

### 中斷連線分支改背景執行緒（2026-09-03，commit `ebb54c3`）

已併入上方〈已知未解決事項〉程式層級第 3 項——這項修正的 commit 本身就直接改了 HANDOVER.md 對應段落，日期與內容吻合，不是另一次獨立修正，這裡同樣只提醒不重複記錄。

### gaussian_vector_sim_gui.py 三軸繪圖範圍 bug（2026-09-09，commit `4da5829`，獨立於 DS102 硬體）

**症狀**：使用者回報「3 軸模擬時 X-Y 讀不到數值」。**根因**：`sim/gaussian_vector_sim_gui.py` 原本只有一組全軸共用的「繪圖範圍下限／上限」，多軸模擬時各軸中心與 σ 尺度差很大（例如 Z 軸對焦行程遠比 X/Y 橫向對準行程長）——用文件 docstring 裡單模光纖三軸實測校準場景的參數（σ 22.5/22.5/250、中心點分屬完全不同尺度）重現後確認，共用同一組範圍會讓格點解析度完全罩不住尺度較小的軸，**不是只有 X-Y 讀不到，三個軸對子圖全部 0/625 可讀**——比使用者原始回報的範圍更廣。**修法**：比照 `sim/gaussian_vector_sim.py` 本身 CLI 端 `--grid-range` 早就支援的每軸各自一組 `(min, max)`，「模擬軸」表格每列新增「範圍下限／上限」兩個輸入框，移除原本共用的 `_grid_min`/`_grid_max` 欄位。**驗證**（2026-09-09，xvfb 下）：重現舊行為（共用範圍時三軸每個子圖 0/625 可讀）、套用 per-axis 範圍後 X-Y 可讀 137/625，並跑過 `_on_run_clicked` 到 `_on_run_done` 的完整同步路徑確認不會炸；既有 436 項回歸測試全數通過，`sim/gaussian_vector_sim_gui.py` 完全獨立於 `main_ai.py`，跟這批測試無交集。**這是純數學模擬工具的修正，不是任何形式的真機驗證，也不影響尋光演算法本身在硬體上的驗證狀態**（定調比照上方〈測試涵蓋缺口〉對 `sim/` 工具的既有立場，保持一致）。

## 交接注意事項

- 這個專案高度仰賴子代理分工（`architect`／`coder`／`tester`／`ui-designer`／`mathematician`／`reporter`／`questioner`／`data-scientist`），規則寫在 CLAUDE.md〈子代理分工〉一節，**動到執行緒／序列通訊／持久化的改動要先過 architect，新增 GUI 元件要先過 ui-designer，動到尋光演算法要先過 mathematician**，不是隨意的建議，是這個專案吃過虧之後定下的流程。
- 沒有 CI。目前僅有的永久回歸測試是 11 支 `tests/verify_*.py`（假物件，不需硬體），**436 項全數可收集（2026-09-09 用 `pytest --collect-only -q` 重新實測確認，分資料夾遷移後 import 路徑正常）**：

  ```bash
  venv/Scripts/python.exe -m pytest -v    # pytest.ini 的 testpaths=tests 自動收集全部 tests/verify_*.py
  venv/Scripts/python.exe -m pytest --collect-only -q    # 只確認項數與是否全部可收集，不需硬體、不需真的跑
  ```

  Linux 端要跑 GUI 相關測試需要虛擬顯示：`xvfb-run -a venv/bin/python -m pytest ... -q`（本機是 Linux，`venv/Scripts/python.exe` 是 Windows 直譯器，兩邊直譯器路徑不能混用）。**實際測試項數請以 `pytest --collect-only -q` 為準，不要相信任何文件裡寫死的數字**——436 這個數字是 2026-09-02（新增 `tests/verify_sw_limit_sync.py`，15 項）實測跑出來的，2026-09-09 分資料夾遷移後重新用 `--collect-only` 確認項數不變、全數可收集；**本次僅執行了 `--collect-only`，未逐項重新跑過 436 項是否全數通過（pass），這點需要使用者或下次任務自行確認**。
- `tests/conftest.py` 的 `make_gui()` 要記得它同時 patch `main_ai.RECORDING_DIR`／`core.ds102_ctrl.RECORDING_DIR`／`main_ai.DATA_DIR`／`core.ds102_ctrl.DATA_DIR` 四個模組層級綁定，缺一邊等於沒防護，新增測試檔不需要（也不應該）再自己額外 patch。細節見 [docs/testing.md](docs/testing.md)。
- git commit 習慣寫得比較長，說明「為什麼」不只「做了什麼」，且都會附驗證結果（回歸測試通過與否、實機量測數字等）——看 `git log` 找同類型改動的前例，照同樣的詳細程度寫，別只寫一行摘要。
- **沒有模擬模式，這是刻意的、不要加回來**——這支程式驅動真實滑台，任何會影響移動/限位/安全邏輯的改動，最終都要有人在真機上驗證過才算數，光靠假物件測試通過不夠。本次彙整再次確認：尋光的 Powell 路徑、階段零盲搜的自動量程退回、多軸撞限位路徑，都還停在「假物件驗證/部分實機驗證」，不要在文件或對話中把它們講成「已驗證」。
- 動到分頁結構、或新增任何「長時間背景作業」之前，先讀 [docs/modularization.md](docs/modularization.md) 2026-08-31 那一節（含〈六〉～〈九〉），不要重做評估——`_register_long_ops()` 是目前正確的接線方式，不要繞過它手動接六個位置。
