# HANDOVER.md

給接手這份程式碼的下一個開發者（人或 AI）。這份文件回答「我現在該從哪裡開始、現在卡在哪裡」，不重複 CLAUDE.md／README.md 已經寫過的細節——那兩份文件分別是「AI 助理的完整操作指引」與「開發者快速上手」，這份只做兩件事：**指路**、**交代現況與未解事項**。

編製日期：2026-08-18，基於分支 `feat/fiber-search-gui`（尚未合併回 `main`，領先 30 個 commit）。

## 先讀這些，依這個順序

1. **[README.md](README.md)** — 5 分鐘搞懂這是什麼、怎麼跑起來。
2. **[CLAUDE.md](CLAUDE.md)** — 完整開發指引，包含大量「已經踩過、別再踩一次」的坑與實測數據。這份文件很長，但**不是選讀**——裡面記載的很多行為是安全性修正的結果，看起來像可以簡化的地方，通常是有理由的。改動前用 grep 找對應章節，別憑直覺猜。
3. **`ds102 (2).pdf`**（根目錄，未進版控，需另外取得）— DS102/DS112 官方操作手冊，指令格式的權威來源。CLAUDE.md 反覆提醒「查指令前先翻手冊，不要用猜的」是真的吃過虧才寫下的規則。

## 現在的狀態

- **當前分支 `feat/fiber-search-gui`**：把光功率計（HP 8153A）與尋光演算法（`FiberAlignmentScanner`）整合進 GUI，外加這次 session 做的架構調整（`DS102Controller` 拆成獨立檔案 `ds102_ctrl.py`、UI 常數與安全常數外部化、軸機械校正參數）。
- **尚未合併回 `main`**。較早一輪 architect 對整個分支的審查最初結論是「不建議合併」（1 critical + 3 high/medium 問題），問題已修正，但**沒有找到後續「建議合併」的正式結論紀錄**——下一步要嘛重新請 architect 對整個分支做一次完整複審，要嘛人工確認可以合併。
- 詳細的開發歷程、測試涵蓋率、逐項風險清單，已經整理成 Notion 頁面〈[尋光系統工程報告](https://app.notion.com/p/3bf070110f3481ae8e38d43781bd1b97)〉（2026-08-17 編製，2026-08-18 補了一條待查事項）。**那份報告涵蓋到光功率／尋光整合為止，沒有涵蓋這次 session 做的模組拆分與軸校正功能**——這份 HANDOVER.md 的〈這次 session 做了什麼〉一節補上這段落差。

## 已知未解決事項

### 硬體層級（真的卡住、需要有人拆機器才能繼續）

🔴 **DATA1 驅動器分度值疑似沒有生效，原因未定**（2026-08-18，最新、最急）。使用者實測：固定 pulse 數移動同一軸，把驅動器分度值旋轉開關 DATA1 從 Full-step 轉到 1/10 並重新通電，實際移動距離感覺沒有變化——跟公式預期（distance 應等比例縮短為 1/10）不符。已排除「開關沒生效」（有重新通電）。手冊提到另有一顆「division changing-over switch（R1/R2）」決定 DATA1 是否真的被驅動器採用，但使用者拆殼後**只看到 DATA1，沒看到第二顆開關**——手冊那張配圖是圖片，AI 助理的 PDF 文字擷取工具讀不到圖片內容，無法進一步比對實際長相與位置。**下一步**：使用者要用 DATA1 兩個極端值（0 vs F，步進角相差 250 倍）重新對比測試，確認 DATA1 本身到底有沒有在生效；結果出來後要回頭更新 CLAUDE.md〈DS102 通訊協定重點〉裡 `DRDIV?` 那條、以及 Notion 報告 6.2 節的對應項目。

### 程式層級（CLAUDE.md 已經記載，抄錄重點方便快速掃過，細節見 CLAUDE.md 原文）

1. **長按點動的 Python 端限位保護預設不生效**——`sw_limits` 初始值全 `None`，沒有程式會在連線時從控制器讀 `CWSLP`/`CCWSLP` 回填，出廠狀態下長按點動完全依賴韌體端限位。
2. **`play_recording` 仍繞過 `_check_sw_limit` 與 `PULS` 整數正規化**，且錄製時每步 delay 是寫死 800ms（非實際按住時間）——重播無法還原點動的實際行程長度，別把錄製重播當安全功能。
3. **`_toggle_connect` 仍在 Tk 主執行緒做阻塞式序列 I/O**（`stop()` 已經有界，但 `disconnect()` 與連線流程沒有）。

### 測試涵蓋缺口

- `verify_fiber_scanner_signal.py`（33 項，訊號有效性判準測試）目前只存在於某次 session 的 scratchpad，**沒有進版控**——比照 `verify_scan_tab.py`／`verify_meter_panel.py` 的既有慣例應該補進 repo。
- HP 8153A **channel A 從未驗證過**，只確認 channel B 可正常回應；channel A 據稱需要外接光學頭，這點是轉述資訊，沒有找到第二來源佐證。
- `FIBER_ALIGNMENT_SCAN_DESIGN.md` 的內容停在 2026-08-07，沒有更新光功率整合（08-12）、訊號判準修正（08-17）、GUI 整合（08-17）的內容——文件與程式碼進度之間有落差。
- 尋光演算法核心邏輯與尋光「分頁」各自都驗證過，但「使用者在 GUI 按下開始尋光、完整跑完一輪」這條端到端路徑**沒有真機驗證紀錄**。

## 這次 session 做了什麼（2026-08-17～18，這份 HANDOVER.md 之前沒人寫過的部分）

使用者要求「變更架構方便打包後修改維護」，architect 規劃了五個階段，完成了四個：

1. **打包基線驗證**——用 PyInstaller `--onedir` 實際打包過一次，確認能正常啟動、在自己目錄建執行期資料夾。之前完全沒打包測試過。
2. **A 類 UI 常數外部化**——UI 節奏／色票搬進 `recordings/app_settings.json`（唯讀，維護人員手動編輯）。
3. **`DS102Controller` 拆成獨立檔案 `ds102_ctrl.py`**——main_ai.py 從 6601 行降到約 4850 行。**新檔案務必別跟 `ds102_controller.py`（舊版快照，凍結不用）搞混**，兩個檔名很像但完全是不同東西。
4. **B 類安全常數外部化**——`WAIT_TIMEOUT`／`STOP_LOCK_TIMEOUT` 等六顆常數搬進獨立的 `recordings/safety_settings.json`，有範圍驗證（超出範圍一律拒絕退回內建預設值，不做 clamp），並補上 GUI 橫幅／儀表板提示。
5. **拆 `DS102GUI` 本體**——architect 建議暫緩，先觀察前四步有沒有緩解「難以定位程式碼」的實際痛點，不急著做。

另外新增了一個獨立功能：**軸機械校正參數**（`axis_calibration.json`）——使用者輸入六軸各自的螺桿導程／馬達步進角／實體分度值開關轉到的分度值，程式據此把 pulse 座標**額外**估算成 μm 顯示。這是純顯示功能，不影響任何移動/限位/教點的內部判斷，那些永遠只認 pulse。就是在驗證這個功能、實際去查驅動器規格時，才發現上面那條 DATA1 疑似沒生效的問題。

以上每一步都經過至少一輪 architect 審查、獨立驗證（不只信任實作代理的自我報告）、兩支既有回歸測試（`verify_scan_tab.py` 57 項、`verify_meter_panel.py` 66 項）全程保持通過。詳細設計理由與逐項驗證數據都寫進 CLAUDE.md 對應章節，這裡不重複。

## 交接注意事項

- 這個專案高度仰賴子代理分工（`architect`／`coder`／`tester`／`ui-designer`／`mathematician`／`reporter`），規則寫在 CLAUDE.md〈子代理分工〉一節，**動到執行緒／序列通訊／持久化的改動要先過 architect，新增 GUI 元件要先過 ui-designer**，不是隨意的建議，是這個專案吃過虧之後定下的流程。
- 沒有 CI。`verify_scan_tab.py`／`verify_meter_panel.py`／`verify_axis_calib.py` 是目前僅有的永久回歸測試（假物件，不需硬體，2026-08-18 已轉成 pytest 測試檔，共 173 項），改動相關功能後應該先跑 `venv/Scripts/python.exe -m pytest verify_scan_tab.py verify_meter_panel.py verify_axis_calib.py -v`，或直接用 VS Code 的 Testing 面板逐一重跑。**改動 `conftest.py` 的 `make_gui()` 時要記得它同時 patch `main_ai.RECORDING_DIR` 與 `ds102_ctrl.RECORDING_DIR`（兩個獨立的模組層級綁定，缺一邊等於沒防護），見 CLAUDE.md 開頭〈常用指令〉那條紅字說明。**
- git commit 習慣寫得比較長，說明「為什麼」不只「做了什麼」，且都會附驗證結果（回歸測試通過與否、手算驗證數字等）——看 `git log` 找同類型改動的前例，照同樣的詳細程度寫，別只寫一行摘要。
- **沒有模擬模式，這是刻意的、不要加回來**——這支程式驅動真實滑台，任何會影響移動/限位/安全邏輯的改動，最終都要有人在真機上驗證過才算數，光靠假物件測試通過不夠。
