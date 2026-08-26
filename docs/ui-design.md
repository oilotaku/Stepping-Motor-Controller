# 視覺設計原則

> 本文件自 CLAUDE.md 拆出（2026-08-26），目的是縮小每次對話的固定載入量。
> **內容未經刪減**，動到對應功能前請完整讀過本檔。

## 視覺設計原則

### tkinter 桌面介面（main_ai.py，ui-designer 職責範圍）

現有色彩／字型系統已成形，**新增或調整介面時延用既有 token，不要另立一套**：

- 色彩定義在 `CLR_BG` / `CLR_CARD` / `CLR_BORDER` / `CLR_ACCENT` / `CLR_DANGER` / `CLR_INFO` / `CLR_WARN` / `CLR_TEXT` / `CLR_MUTED` / `CLR_LOG_BG`（main_ai.py:257-266）。這些不是隨意選的色票，而是**語意化**的：`CLR_DANGER` 綁定撞限位／警報、`CLR_WARN` 綁定通訊失聯／待確認、`CLR_ACCENT` 綁定「目前選取軸」（見上方〈第四批修正〉）。新增任何狀態顯示前先檢查有沒有對應的語意色，不要因為好看而混用。
- 按鈕走 `ttk.Style` 的語意化角色（`Accent` / `Danger` / `Info` / `Warn` / `Flat`，main_ai.py:2256-2260），而不是逐一設色。新增按鈕先判斷屬於哪個語意角色，用既有的 `.TButton` style name，不要手動 `configure(bg=...)`。
- 字型統一 `("Segoe UI", 10)`（main_ai.py:2245）——這是 Windows 系統預設字型，**刻意**不是什麼「有特色」的排版選擇，而是為了跟作業系統其餘 UI 元素視覺一致、且不需要額外綁定字型檔。這支程式是驅動實體滑台的工業控制面板，**易讀性與跨機器一致性優先於視覺獨特性**：不要為了風格新增自訂字型或加大字重層級，除非能確認目標機器都有安裝。
- 這套系統本身就是多輪安全修正（2026-08-05～08-06）逐步收斂出來的——像「未連線一律顯示 `—` 不顯示 `0`」「橫幅常駐 pack 避免版面跳動」都是介面決策同時也是安全機制，改視覺樣式時連帶會動到這些行為，**先讀上方〈第三批／第四批修正〉再動手**。
- **開機視窗改為預設最大化**（2026-08-19，`_build_window()`：`self.root.state("zoomed")`，Windows 專用、非真正全螢幕，保留標題列/工作列）。原本沒有設 `geometry()`、只有 `minsize(1020, 720)`，Tk 會照 widget 最小需求尺寸開窗；這幾輪陸續加了軸校正參數卡片、安全常數橫幅、尋光控制列與更寬的圖表後，預設開窗尺寸擠壓內容、常需要捲動。改用 `state("zoomed")` 而非寫死固定像素（例如 `geometry("1600x1000")`），是為了不綁定特定螢幕解析度——使用者仍可自行拖曳還原成任意大小，只是開機當下改成先吃滿目前螢幕可用空間。已確認在 `root.withdraw()` 之後呼叫（測試環境的既有模式）不會出錯。
- 🔴 **`ttk.Checkbutton` / `ttk.Radiobutton` 是「先翻轉 `variable`，再呼叫 `command`」**——`command` 內部必須讀 `variable` 的**新值**來決定要做開還是關，不能無條件當成「開」。2026-08-26 使用者回報「光功率浮動視窗失效」就是這個：`_toggle_pm_float_window()` 一律走「開」的分支，取消勾選時只 `lift()` 不關閉，勾選框顯示未勾、視窗卻還在，之後每次點擊都只是把它拉到最上層，**視窗再也關不掉**。移動控制與光功率兩個分頁共用同一個 `BooleanVar`，任一邊 desync 之後另一邊也一起失效。回歸測試在 `verify_meter_panel.py::TestPmFloatWindowToggle`（5 項，照 Tk 的真實時序「先 `set()` 再呼叫 command」驅動）。
- 新增／調整 GUI 元件一律先派 `ui-designer` 提案（〈子代理分工〉（見 [CLAUDE.md](../CLAUDE.md)）），不要自行決定版面。

### HTML / Artifact 報告（fiber_scan_charts.html 這類產出）

這類報告目前是 scratchpad 產出（不進版控），但當作對外可分享的正式交付物看待，**視覺水準要對得起裡面的真機驗證數據**：

- **避免「AI slop」美學**：不要預設用 Inter / Roboto / Arial / system-ui 這類無特色字型，不要落入「白底紫色漸層」這種樣板配色，版面不要是無差異的置中卡片堆疊。挑選字型與配色時要對應內容特性——這批報告是精密量測數據，可以往「儀器儀表／科學圖表」的方向找識別度（例如等寬字型呈現數字、細線條分隔、資料本身作為視覺焦點），而不是為了花俏而花俏。
- **色彩要有主從**：延續 `fiber_scan_charts.html` 已建立的做法——完整的淺色 token 定義在 `:root`，深色模式在 `@media (prefers-color-scheme: dark)` 與 `:root[data-theme="dark"]` 兩處同步覆寫（見 Artifact 發布規範）。不要每次重新發明一套 token 命名，沿用既有的 `--series-*`、`--accent-*` 系列。
- **動態效果要服務理解，不是裝飾**：`fiber_scan_charts.html` 的 `buildReplayDemo()` 逐筆重播是先例——用動畫呈現「演算法怎麼一步步收斂」這種本來要盯著一堆數字才能理解的過程，是值得投入的地方；不要在不需要的地方加微互動。
- **自包含限制不可違反**：不能連外部字型 CDN（Google Fonts 等）——嚴格 CSP 會擋。要用有特色的字型，選擇系統常見的 serif/mono 字型堆疊，或把字型檔案內嵌成 data URI（注意檔案大小，Artifact 上限 16MB）。
- 這類報告的資料**必須先查證再寫入**（〈子代理分工〉（見 [CLAUDE.md](../CLAUDE.md)）裡 `mathematician` 與 `reporter` 的分工），視覺設計服務的是「把已經查證過的數據講清楚」，不能為了美觀而簡化或誤導數據本身的意義。

### md-document／design-system skill（本機 HTML 報告，另一條產出路徑）

`.claude/skills/` 底下的 `md-document` 與 `design-system` 是**第三方通用 markdown→HTML 外掛**（`author: Alireza Rezvani`），不是為本專案寫的程式碼，跟上面「HTML / Artifact 報告」那節的手工視覺規範是兩套獨立機制，不要混為一談：

- `md-document` 把長篇 markdown（規格、報告、說明文件）轉成單檔 HTML，附側邊 TOC／搜尋／程式碼複製鈕；`design-system` 是它的品牌設定來源，10 題 onboarding wizard 決定主色／字型／版面風格，兩者共用 `config_loader.py` 讀寫。
- **輸出目錄是 `reports/`，已加進 `.gitignore`**（跟 `logs/`／`recordings/`／`data/` 同一類「本機產出、不進版控」）。
- `design-system` 的 onboarding 設定檔存在**專案外的全域路徑** `~/.config/markdown-html/design-system.json`（本機已於 2026-08-19 完成過一次），不是 repo 內的專案設定——同一台機器上其他專案用這套外掛也會沿用同一份品牌設定，除非另外跑 `--scope project` 覆寫。
- 目前唯一實際引用它的是 `data-scientist` 代理（〈子代理分工〉（見 [CLAUDE.md](../CLAUDE.md)））：圖表若要嵌進這種本機 HTML 報告就用 `matplotlib` 轉 base64 內嵌；若目標是可分享的 Artifact 連結，則走前一節的手工 SVG／CSP 限制那套規範。`reporter` 本身目前仍以 markdown／CLAUDE.md 更新為主要產出形式，尚未串接這個 skill。
