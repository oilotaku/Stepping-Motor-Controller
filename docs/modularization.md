# 模組化現況與下一步門檻

> 本文件自 CLAUDE.md 拆出（2026-08-26），目的是縮小每次對話的固定載入量。
> **內容未經刪減**，動到對應功能前請完整讀過本檔。

### 模組化現況與下一步門檻（2026-08-18 重新評估）

方向1b（拆 `DS102GUI`）**目前仍不建議做**。1a 完成後 main_ai.py 從 4839 行漲到 5106 行（+5.3%，安全常數橫幅＋軸校正參數卡片），architect 判斷這不是「肥大到出事」的訊號——判準不該用行數，而是「新功能有沒有被迫在多個分頁間來回耦合寫入」，這次沒有：七個 `_build_tab_*` 邊界依然清楚，新功能都乾淨落在既有分頁或 Core 重繪迴圈裡。

**若之後真的要拆，切分方案已經先備好**（依風險由低到高）：`LogTabMixin`（~170 行，無反向依賴，適合當 mixin 模式的試點）→ `PointsTabMixin`（~230 行，只讀 `ctrl.teaching_points`/`ctrl.goto_point`/`ctrl.estimate_um`）→ `RecordingTabMixin`（~500 行，內部四張卡片只互相呼叫）。三塊合計約 900 行可搬出。**儀表板、移動控制、光功率＋尋光暫不適合拆**：儀表板的 `_update_stat_ui`/`_redraw_positions` 要橫跨讀取其餘六個分頁狀態，硬拆會變成互相 import；移動控制承載 EMS/STOP/原點復歸等全域行為，且軸校正資料被 Core／Teaching 分頁共用，是七個分頁裡耦合面最廣的；光功率與尋光透過 `_scanner_power_query()`/`_pm_sync_scan_notice()` 直接呼叫對方私有方法（第五階段刻意協調），要拆得兩個一起處理並先定義正式介面，工作量不小。

**具體觸發門檻**（下次評估直接對照，不必重新從頭判斷），任一項成立即觸發重新評估：

1. `_update_stat_ui`（目前讀 4 個跨分頁旗標：`playback_running`/`_homing`/`_scanning`/`ems_active`）或 `_redraw_positions`（目前讀約 6 項）內的跨分頁狀態判斷再增加 2 項以上——代表 Core 本身已是事實上的上帝方法，這時候先拆 Core 自己的職責（例如抽出獨立的 `_busy_reasons()`），不是急著拆分頁。
2. 出現第二對「兩個分頁互相呼叫對方私有方法」的關係（目前唯一一對是 Scan↔Power）。
3. 新功能被迫寫進兩個以上分頁、且是**雙向**耦合（A 改 B 的 widget 狀態，不只是共用同一份 controller 資料）——軸校正參數雖跨了 Control/Teaching/Core 三處，但都是單向讀取 `estimate_um()`，不算數。
4. main_ai.py 總行數達到約 **5800～6000 行**，且新增內容分散在多個分頁而非集中在 1～2 個（分頁邊界依然清楚、只是又加一張獨立卡片的情況不算）。

**測試基礎建設對未來 mixin 化是有利的**：`conftest.py`／兩份 pytest 測試檔全部透過 `gui.ctrl`/`gui.meter`/`monkeypatch.setattr(gui, ...)` 存取單一 `gui` 物件屬性，沒有假設方法定義在哪個檔案——只要最終類別仍叫 `DS102GUI`、由 `main_ai.DS102GUI(root)` 建構，mixin 化後現有 123 項測試不用改一行。唯一地雷：`conftest.py` 是用 `patch.object(main_ai, "RECORDING_DIR", ...)` 動態改模組層級名字，如果將來某個 mixin 檔案在模組層級快取了 `RECORDING_DIR` 的值而非透過 `self`／動態 import 存取，這個 monkeypatch 會失效、測試會意外寫進真實 `recordings/`——現在還沒發生，真拆檔案時要提醒實作者。

**`__init__`（main_ai.py:475-916 附近）存在只靠註解提醒、沒有機制強制的初始化順序相依**：例如 `ctrl.load_axis_calib()` 必須在 `_build_notebook()` 之前執行，卡片建構時才讀得到值；`_scanner_cfg_pending` 要等 `_build_tab_scan` 才會被消費。將來若真的把 `__init__` 拆成各 mixin 自己的 `_init_xxx_state()`，這些順序相依必須顯式化（例如一份有序的 init 步驟清單），否則會在特定操作路徑上悄悄讀到空值、且不會立刻炸出來。

**技術債已解決（2026-08-18）：四份設定檔讀取骨架抽成共用函式。** `_load_app_settings`／`_load_safety_settings`（ds102_ctrl.py）與 `_load_meter_config`／`_load_scanner_config`（main_ai.py）原本逐字同一套骨架，現在統一呼叫 `ds102_ctrl._load_json_settings(path, label, log=None, error_level="INFO", not_found_msg=None, fail_msg=..., invalid_type_msg=..., success_msg=None)`，用參數精確重現原本兩種行為模式：app/safety 是「詳細」模式（不傳 `log`、走模組 `logger`、INFO 等級、「不存在」與「成功」都記錄）；meter/scanner 是「安靜」模式（可選 `log` callback、ERROR 等級、「不存在」與「成功」都不記錄）。四個原函式對外的名稱／參數／回傳型別完全沒變，純內部實作重構。連帶讓 main_ai.py 的 `import json` 變成真正未使用，已移除。**新增任何第五份設定檔讀取函式，一律呼叫這個共用函式，不要再複製骨架。**

至於「`_app_settings`/`_app_setting_num`/`CLR_*` 獨立成第三個檔案」這項更早的技術債——**維持先前結論仍不急**，觸發時機該跟方向1b綁在一起（真的做 mixin 化時勢必要重新梳理 import 邊界，那時候順手做是同一批工），現在單獨做只是提前付 import 調整成本卻拿不到額外好處。

---

## 2026-08-31 重新評估（門檻觸發後的第一次正式複審）

> 觸發原因：main_ai.py 已達 **6820 行**（08-18 評估時為 5083 行，+1737／+34%），門檻 4 的行數條件成立。
> 本節是 architect 對照四條門檻逐條複審的結論。**結論：方向1b（拆分頁 mixin）現在仍不該做，但理由跟 08-18 完全不同——不是「還不夠痛」，而是「拆分頁解決不了現在真正在痛的東西，而且會讓它更難修」。**

### 一、四條門檻的逐條判定

| 門檻 | 判定 | 依據 |
|---|---|---|
| 1. Core 跨分頁狀態 +2 以上 | **實質成立**（字面上每個方法各 +1） | 見下方〈門檻 1〉 |
| 2. 出現第二對「互相呼叫對方私有方法」 | **不成立**（但既有那一對變粗了很多） | 見下方〈門檻 2〉 |
| 3. 新功能被迫寫進 2+ 分頁且雙向 | **明確成立** | 見下方〈門檻 3〉 |
| 4. 行數 5800～6000 且新增內容分散 | **行數成立，分散性不成立** | 見下方〈門檻 4〉 |

#### 門檻 1：Core 重繪迴圈已是事實上的上帝迴圈

08-18 的門檻只點名 `_update_stat_ui` 與 `_redraw_positions`，但這兩個方法跟 `_start_poller._poll()` 是同一條 100ms 迴圈，應該整條一起算。與 `076fb0a`（08-18 當時版本）逐行比對：

- `_update_stat_ui`：`busy` 從 3 個旗標（`playback_running`/`_homing`/`_scanning`）變成 4 個（新增 `_org_repeat_running`，main_ai.py:6607-6612）。**+1**
- `_redraw_positions`：新增尾段直接寫「移動控制」分頁的 `_ctrl_pos_var`（main_ai.py:6686-6692，來自 `d73d5f7`）。這是新的跨分頁 widget 寫入。**+1**
- `_start_poller._poll()`：新增 3 個跨分頁動作——`_org_repeat_elapsed_var`（儀表板卡片）、`_pm_refresh_status_line()`、`_pm_sync_scan_notice()`（光功率分頁，main_ai.py:6715-6722）。**+3**

整條迴圈現在每 100ms 直接觸碰 **儀表板／移動控制／尋光／光功率 四個分頁的 widget＋全部 StatusBar**。字面判準（單一方法 +2）沒到，但門檻 1 想抓的現象（「Core 本身已是事實上的上帝方法」）**已經成立**。

門檻 1 的原文已經預先寫好了處方：「**這時候先拆 Core 自己的職責（例如抽出獨立的 `_busy_reasons()`），不是急著拆分頁**」。這次複審的結論跟那句話一致。

#### 門檻 2：沒有第二對，但 Scan↔Power 這一對從 2 個橋接點長成 6 個

08-18 記錄的橋接是 `_scanner_power_query()` / `_pm_sync_scan_notice()` 兩個。現在是：

| 方向 | 橋接點 | 位置 |
|---|---|---|
| Scan → Power | `_scanner_power_query()` | 5065 呼叫 / 5981 定義 |
| Scan → Power | `_restore_meter_auto_range()` | 5169 呼叫 / 5998 定義 |
| Scan → Power | `_on_scan_signal_found()`（背景執行緒改儀器量程） | 5005 註冊 / 6019 定義 |
| Scan → Power | `_pm_sync_scan_notice()` | 5213 呼叫 |
| Core → Power | `_pm_sync_scan_notice()` / `_pm_refresh_status_line()` | 6721-6722 |
| **Scan → Power（直接寫欄位，不經方法）** | `_scan_plot_extend()` 直接寫 `_pm_last_ok_time` / `_pm_last_value` / `_pm_status_var` / `_pm_power_lbl` / `_pm_status_lbl` | **4608-4616** |

最後一列是這次複審最關鍵的發現：**尋光分頁的繪圖函式直接改寫光功率分頁的「最近一次讀值」快取與三個 widget**。這已經不是「呼叫對方的私有方法」，是**兩個分頁共用同一份未命名的可變狀態**。

有一項是往好的方向動的：`_pm_sync_scan_notice()` 的呼叫時機 2026-08-20 從 `_redraw_scan_plot` 搬到 `_start_poller`，改成跟著 `ctrl.scanning_active` 的狀態驅動，Scan 不再需要主動通知 Power。這是正確的解耦方向，值得當作剩下五個橋接點的樣板。

**結論：門檻 2 不成立（仍只有一對），但這一對的耦合深度已經從「方法呼叫」下沉到「共用可變狀態」，`PowerTabMixin` 與 `ScanTabMixin` 在現況下不可能分開拆。**

#### 門檻 3：成立——「原點復歸重現性量測」是散彈式接線的第二個實例

`dbff961`（+651 行）把量測卡片放在**儀表板**分頁，但要讓它正確運作，必須手動接到分頁以外的 **5 個位置**：

| 接線點 | 位置 | 方向 |
|---|---|---|
| `_update_stat_ui` 的 `busy` | 6611 | Core 讀卡片旗標 |
| `_start_poller` 的經過時間 | 6715-6717 | Core 寫卡片 widget |
| `_do_stop`（移動控制的「■ Stop」） | 2498-2499 | **移動控制寫卡片的私有 Event** |
| `_on_escape`（全域 Esc） | 2508-2509 | 同上 |
| `_toggle_connect`（頂列中斷連線） | 5499-5500 | 同上 |
| `_on_close` | 6770 | 同上 |
| 反向：卡片鎖／解鎖移動控制的驅動鍵 | 2019、2126 呼叫 `_set_drive_buttons_state()` | **卡片寫移動控制的 widget 狀態** |

雙向、跨分頁、且是 widget 狀態層級，**完全命中門檻 3 的定義**。

#### 門檻 4：行數成立，但成長是集中的不是分散的

逐提交量測（`git show <sha>:main_ai.py | wc -l`）：

| 提交 | 行數 | 落點 |
|---|---|---|
| `076fb0a`（08-18 基準） | 5083 | — |
| `dbff961` 原點復歸重現性 | 6117 | 儀表板（+651） |
| `8e0b0f6`～`e4382fd` 尋光系列 | 6810 | 尋光＋光功率（約 +900） |
| `2365bd3`（現在） | 6820 | — |

成長集中在 **2 個分頁**，門檻 4 的第二個連接條件（「分散在多個分頁」）**不成立**。七個 `_build_tab_*` 的邊界本身沒有被打破。

現在各區塊的實際大小（以方法定義位置切分）：

| 區塊 | 行數 | 對外洩漏的名字 |
|---|---|---|
| 尋光 | **1714** | 13／107 |
| 儀表板（含原點復歸卡片 541 行） | 1111 | 4／32 |
| 光功率（3341-3580 建構 ＋ 5595-6177 方法，被尋光分頁夾在中間） | 823 | **22／22 建構區全洩漏** |
| 行程錄製 | 500 | **2／34** |
| 移動控制 | 415 | — |
| Teaching | 230 | **2／12** |
| LOG | 168 | **3／10** |

### 二、結論：現在不做方向1b，先做三件前置工作

#### 為什麼不做（兩個決定性理由）

**理由 1：拆完還是超標，成本收不回來。** 文件已備好的三塊 `LogTabMixin`(168) ＋ `PointsTabMixin`(230) ＋ `RecordingTabMixin`(500) 合計 **898 行**。6820 − 898 = **5922 行**，**仍然落在門檻 4 的 5800～6000 區間內**。付了 mixin 化的全部成本（初始化順序相依顯式化、`RECORDING_DIR` 的 monkeypatch 地雷、七個分頁的 import 邊界重畫），換來的結果是「下次評估仍然會被同一條門檻觸發」。真正的體積在尋光(1714)＋光功率(823)＝2537 行，而那正是最不能拆的一塊。

**理由 2：觸發門檻的那個功能，恰恰是 mixin 化不能解決、還會被弄得更糟的類型。** 門檻 3 命中的原因不是分頁邊界糊掉，而是專案裡有一個**沒有被命名的概念**：「長時間背景作業」。目前有三個實例（重播、原點復歸重現性量測、尋光），每一個都要作者記得手動接進 `_update_stat_ui` 的 busy、`_start_poller` 的計時、`_do_stop`、`_on_escape`、`_toggle_connect`、`_on_close` 這 6 個位置。**現在這張表已經漏了**——見下方〈三〉。把分頁拆成 6 個檔案之後，這 6 個接線點會散落在 6 個檔案裡，漏接只會更難查。

#### 前置工作（建議依序執行，都不搬動分頁）

**前置 0（必須，且與模組化無關）：補上〈三〉列出的漏接。** 這是正確性缺陷，不該等模組化排程。
**2026-08-31 已完成**（見〈六〉）。

**前置 1：把「長時間背景作業」變成第一類概念。**
**2026-08-31 已完成，實際落地的設計見〈七〉。** 原始構想如下（保留以對照）：建立一份註冊表，每個長時間作業註冊 `(名稱, 進行中旗標, 請求停止的 callable, 經過時間 StringVar)`，然後：

- `_update_stat_ui` 的 `busy` 改成 `any(op.running for op in self._long_ops)`；門檻 1 原文提到的 `_busy_reasons()` 就在這裡實現（順帶讓橫幅可以說出「正在忙什麼」）。
- `_start_poller` 的兩段經過時間計算合併成一個迴圈。
- `_do_stop` / `_on_escape` / `_toggle_connect` / `_on_close` 一律改成 `for op in self._long_ops: op.request_stop()`——**漏接從此在結構上不可能發生**，這才是門檻 3 真正要的東西。
- 這一步**不可以**順手把 `motion_active` 或 `scanning_active` 併進來當移動守衛，那是 [fiber-scan.md](fiber-scan.md) 明列的紅線。註冊表只服務「UI 忙碌顯示」與「停止請求分派」兩件事。

**前置 2：把光功率的「最近一次讀值」抽成具名的共用狀態。** 現況是 `_pm_last_ok_time` / `_pm_last_value` / `_pm_status_var` 三個欄位由「光功率」與「尋光」兩邊各自直接寫（4608-4616 vs 5944-5945）。抽成一個小物件（例如 `PowerReading`，帶 `value` / `ok_time` / `source`），寫入只走它的方法，讀取只走 `_pm_refresh_status_line()`。做完之後 Scan→Power 的六個橋接點會剩下真正需要協調的兩三個，且都是有名字的介面。

**前置 3：`_app_settings` / `_app_setting_num` / `CLR_*` 抽成第三個檔案——這次應該做，而且應該最先做。**

**先前「跟方向1b綁在一起、現在不急」的結論，這次推翻。** 新理由：

1. 它是本次盤點裡**唯一零風險、無條件正向**的一步（純模組層級常數搬家，沒有狀態、沒有執行緒、沒有初始化順序問題）。
2. 現況第 2 節已經寫明：`StatusBar` 留在 main_ai.py 的**唯一理由**就是它需要 `CLR_*` 色票，搬去 `ds102_ctrl.py` 會造成循環相依。抽出 `ui_theme.py` 之後這個阻礙立刻消失，`StatusBar`（約 120 行）就有了獨立搬遷的選項。
3. 如果將來真的做 mixin 化，每個 mixin 檔案都會需要色票；不先抽，就會變成每個 mixin 都 `from main_ai import CLR_*` ——那正是循環相依本身。這件事**必須**在 mixin 化之前完成，不是「跟它一起做」。
4. 搬遷時注意：`app_settings.json` 的覆寫邏輯要一起搬，且 `CLR_*` 必須在 import 時就完成覆寫（現有行為），不可改成延遲求值——`_build_*` 在 `__init__` 期間就讀它們了。

### 三、複審過程中發現的正確性缺陷（與模組化無關，應優先修）

這三項是「散彈式接線」論點的實證，但它們本身就是缺陷，不該等模組化。

**必須修正 1：尋光執行中按 Esc 或「■ Stop」，滑台會停一下然後自己繼續走。**
`_on_escape`（main_ai.py:2501-2510）與 `_do_stop`（2493-2499）都只送 `ctrl.stop()`，並設 `_stop_playback`／`_org_repeat_stop_event`，但**都沒有呼叫 `self._active_scanner.request_stop()`**。`fiber_scanner._check_abort()`（fiber_scanner.py:2518-2523）只檢查 `_stop_event` 與 `ctrl.ems_active`，兩者皆未被設。
失效情境：尋光進行中按 Esc（`bind_all`，在任何分頁都有效）→ `STOP 0` 讓軸停下 → `_wait_axis_stop()` 看到軸已停、回報 True → 演算法把這個被強制停住的位置當成該步的落點，量測後送出下一組 `PULS`/`GO` → **滑台在使用者按下停止後約 1 秒內重新開始移動**。目前唯一能真正停住尋光的入口是尋光分頁自己的停止鍵（`_do_stop_scan`，5149-5155，它有呼叫 `request_stop()`）。注意 `_sync_stop_button()`（6248）明文「其餘一律可按」，所以停止鍵在尋光期間確實是 enabled 的。

**必須修正 2：尋光／重播執行中按頂列「中斷」，背景執行緒會繼續對已關閉的序列埠送指令。**
`_toggle_connect`（5491-5510）的中斷分支設了 `_org_repeat_stop_event`，但沒有 `_active_scanner.request_stop()`，也沒有 `_stop_playback.set()`，接著就 `ctrl.stop()` + `ctrl.disconnect()`。
這與 `_on_close`（6764-6778）已經修過的問題**是同一個**——那裡的註解寫得很清楚「沒有這行，`_run()` 仍會繼續跑 `scanner.run()`，下一步對已經 `disconnect()` 的序列埠操作大機率拋例外」。修在 `_on_close`，沒有修在 `_toggle_connect`。這正是散彈式接線的典型症狀：同一個 bug 要在 N 個地方各修一次，修了 N−1 個。

**建議改善 3：`_do_stop` 與 `_on_escape` 對重播的行為不一致。**
`_on_escape` 會 `self._stop_playback.set()`，`_do_stop` 不會。`ds102_ctrl.play_recording()`（3592-3606）只在 `stop_event` 或 `ems_active` 時收工，所以重播中按「■ Stop」是「停這一步，然後繼續下一步」。若這是刻意設計（重播有自己的停止鍵），至少要在 `_do_stop` 補註解說明；但 `_on_escape` 的存在說明原意是「全域停止應中止重播」。前置 1 的註冊表會一次消滅這類不一致。

### 四、下次評估的觀察指標（取代原本的四條門檻）

行數判準這次已經證明**沒有預測力**（6820 行的檔案，邊界其實還算清楚；而拆掉 898 行也回不到門檻以下）。改用下列三項：

1. ~~**長時間背景作業的接線點數量**：目前 6 個接線點 × 3 個作業。前置 1 完成後應降為「1 個註冊呼叫」。若前置 1 沒做而作業數增加到 4 個（例如未來的自動化量測流程），視為紅線。~~
   **2026-08-31 前置 1 完成後改為**：接線點已降為「`_register_long_ops()` 裡的 1 筆註冊」，這項指標本身不再是觸發條件。**改成盯這個**：`grep -n "_stop_playback\.set\|_org_repeat_stop_event\.set\|_active_scanner\.request_stop" main_ai.py` 的結果，除了 `_register_long_ops()` 與各分頁自己的專屬停止鍵（`_do_stop_playback` / `_do_stop_org_repeat` / `_do_stop_scan`）之外，**不應該再有第四類呼叫點**。出現了就代表有人繞過註冊表又接了一份手動接線，那是「兩份會走鐘的真相來源」，該回頭改成註冊。
2. **跨分頁的「共用可變狀態」數量**：目前 1 組（光功率讀值快取）。**出現第二組即觸發**——這比「互相呼叫私有方法」更嚴重，因為它連呼叫點都 grep 不到。
3. **單一分頁區塊超過 2000 行**：尋光目前 1714 行，是最接近的。若尋光突破 2000 行，就該把「尋光＋光功率」當成**一個** `AlignmentMixin`（約 2500 行）整塊搬出，而不是拆成兩個——前置 2 是這一步的必要前提。

**不建議**再用 main_ai.py 總行數當觸發條件。

### 五、修訂後的切分順序（若將來真的執行方向1b）

前置 1～3 完成後，順序改為：

1. `ui_theme.py`（`CLR_*` ＋ `_app_settings`）— 前置 3，先做，零風險
2. `LogTabMixin`（168 行，對外只洩漏 3 個名字且全是 `__init__`／`_build_notebook` 的正常註冊）— mixin 模式試點
3. `PointsTabMixin`（230 行，洩漏 2 個）
4. `RecordingTabMixin`（500 行，洩漏 2／34，是所有分頁裡封裝最好的一塊）
5. `AlignmentMixin`（尋光＋光功率合併約 2537 行）— **必須先完成前置 2**，且兩者一起搬，不可分開

08-18 記錄的兩個地雷依然有效，實作前務必重讀本檔第 19、21 行：mixin 檔案**不可在模組層級快取 `RECORDING_DIR`**（`conftest.py` 的 `patch.object` 會失效，測試會寫進真實 `recordings/`）；`__init__` 的初始化順序相依必須顯式化。

📌 本次複審**未搬動任何程式碼**，也未執行測試（本機為 Linux，專案的 `venv/Scripts/python.exe` 是 Windows 直譯器）。上述行號依 `2365bd3` 版本的 main_ai.py（6820 行）。

---

## 六、前置 0 落地紀錄（2026-08-31，安全缺陷修正）

〈三〉列出的三項全部修正，位置與寫法一律比照 `_on_close()` 既有模式（`if self._active_scanner is not None: self._active_scanner.request_stop()`）：

| 缺陷 | 修在哪 | 補的測試 |
|---|---|---|
| 必須修正 1（Esc／■ Stop 攔不住尋光） | `_do_stop()`、`_on_escape()` | `verify_scan_tab.py` 案例 27a／27b |
| 必須修正 2（中斷連線攔不住尋光） | `_toggle_connect()` 中斷分支 | 案例 27c |
| 建議改善 3（`_do_stop` 對重播不一致） | `_do_stop()` 補 `_stop_playback.set()` | 由前置 1 的案例 28i 涵蓋 |

回歸測試 404 項全數通過（Linux 端以 `xvfb-run -a venv/bin/python -m pytest ...` 執行）。

這批修正**本身就是散彈式接線的最後一次示範**：同一個概念改了 4 個地方、加了 3 個測試，而下一個長時間作業還是會要求作者記得同樣的 4 個地方。前置 1 就是為了讓這件事不再發生。

---

## 七、前置 1 落地紀錄（2026-08-31，長時間背景作業註冊表）

### 7.1 資料結構

`main_ai.py` 模組層級新增 `@dataclass(frozen=True) class LongOperation`（定義在 `StatusBar` 之後、`DS102GUI` 之前）：

| 欄位 | 型別 | 語意 |
|---|---|---|
| `key` | `str` | 內部識別（log／測試用） |
| `label` | `str` | 給人看的名稱，`_busy_reasons()` 用它組訊息 |
| `is_running` | `Callable[[], bool]` | 唯一的 busy 判準 |
| `request_stop` | `Optional[Callable[[], None]]` | 請求收工；**`None` 代表這項作業沒有軟體停止機制** |
| `started_at` | `Optional[Callable[[], float]]` | 本輪起算的 `time.time()` 基準 |
| `show_elapsed` | `Optional[Callable[[str], None]]` | 把 `"MM:SS"` 寫進對應的 StringVar |

**除了 `key` / `label`，每個欄位都是 callable，而且註冊時一律包成 lambda，不可寫成 `self._stop_playback.set` 這種綁定方法。** 兩個理由，都是本檔第 21 行那個地雷的直接對策：

1. **消滅初始化順序相依。** `_org_repeat_elapsed_var` 要等 `_build_card_origin_repeatability()` 才建立，而 `_register_long_ops()` 跑在 `_build_notebook()` 之前。全部走 callable 表示每個欄位都在「被呼叫的當下」才解析 `self` 的屬性，註冊表因此可以放在 `__init__` 的任何位置（實際擺在四個旗標定義之後，純為可讀性）。
2. **避免靜默盯著舊物件。** 綁定方法會在註冊當下把 Event／StringVar 實例抓進閉包；日後若有人重新指派 `self._scanning = threading.Event()`，註冊表會**不報錯地**繼續盯著已被丟棄的舊物件。

### 7.2 註冊的四項作業

`DS102GUI._register_long_ops()`，與改用註冊表之前 `_update_stat_ui` 的 busy 判斷逐項對應，沒有新增也沒有移除：

| key | label | `is_running` | `request_stop` | 計時 |
|---|---|---|---|---|
| `playback` | 行程重播 | `ctrl.playback_running` | `_stop_playback.set()` | 無（有自己的步數進度） |
| `homing` | 全軸原點復歸 | `_homing.is_set()` | **`None`** | 無 |
| `scan` | 尋光 | `_scanning.is_set()` | `_request_stop_scanner()` | `_scan_start_time` → `_scan_elapsed_var` |
| `org_repeat` | 原點復歸重現性量測 | `_org_repeat_running.is_set()` | `_org_repeat_stop_event.set()` | `_org_repeat_start_time` → `_org_repeat_elapsed_var` |

兩點要注意：

- **`homing` 的 `request_stop=None` 不是漏寫。** `ds102_ctrl.origin_all()` 不收 `stop_event`，沒有任何軟體旗標能讓它提前收工；唯一能縮短它的是呼叫端本來就會送的 `ctrl.stop()`（讓當下那一軸的 `_wait_origin_done()` 提早結束，整批仍會依序跑完其餘軸）。要真的能中止必須改 `origin_all()` 的簽章，是另一件事。**註冊表刻意把「沒有停止機制」表達出來，而不是假裝有。**
- **尋光的 `_active_scanner` 是 `Optional` 且會動態變 None**，所以 None 判斷收斂在 `_request_stop_scanner()` 這一個方法裡（`_do_stop_scan()` 也改成呼叫它）。註冊表對外的約定是「`request_stop` 不為 `None` 時，任何時候呼叫都必須安全且冪等」。

### 7.3 四個新方法

| 方法 | 用途 |
|---|---|
| `_register_long_ops()` | 建表，`__init__` 呼叫一次 |
| `_any_long_op_running()` | `_update_stat_ui` 的 busy 判準 |
| `_busy_reasons() -> List[str]` | 進行中作業的名稱清單（門檻 1 原文點名要的東西） |
| `_request_stop_long_ops()` | 停止請求分派 |
| `_update_long_op_elapsed()` | `_start_poller` 每輪呼叫，更新所有經過時間顯示 |

### 7.4 `_request_stop_long_ops()` 的兩個設計決定

**（a）刻意不先判斷 `is_running()`，無條件分派。** 理由：

1. 三個 `request_stop` 都是冪等的旗標設定，而且各自的啟動流程（`_do_play_rec` 的 `_stop_playback.clear()`、`_do_start_org_repeat` 的 `_org_repeat_stop_event.clear()`）都會在下一輪開始前把旗標清掉，殘留的 set 不會誤殺下一輪。
2. 「進行中旗標」與「停止目標」不是同一個物件，兩者的 set/clear 有極短的交錯窗口（`_scanning.set()` 早於 `_active_scanner = scanner`）。拿 `is_running()` 當閘門等於把這個窗口變成漏接窗口——而漏接正是這張表要消滅的東西。

**（b）每一項各自 `try/except` 並 `logger.exception`。** 這是「使用者按下停止」的路徑，任何一項 `request_stop` 拋例外都不可以吃掉排在它後面的作業的停止請求。失效情境：尋光的 `request_stop` 拋例外 → 沒有逐項保護時，排在後面的原點復歸重現性量測永遠等不到停止旗標，使用者按下停止後量測執行緒繼續對硬體送下一輪指令。（案例 28f 鎖住這個行為。）

### 7.5 六個接線點的變化

| 位置 | 之前 | 之後 |
|---|---|---|
| `_update_stat_ui` | 4 個旗標 or 起來的 `busy` | `if self._any_long_op_running():` |
| `_start_poller._poll()` | 兩段各自 `if 旗標: 算 elapsed; var.set(...)` | `self._update_long_op_elapsed()` |
| `_do_stop` | 3 個 `if ...: ....set()` | `self._request_stop_long_ops()` |
| `_on_escape` | 同上 | 同上 |
| `_toggle_connect` 中斷分支 | 2 個 `if`（且缺 `_stop_playback`） | 同上（順帶補齊重播那一項） |
| `_on_close` | 2 個無條件 set ＋ 1 個 `if` | 同上 |

另外 `_do_start_org_repeat` 被擋下來時的橫幅改用 `_busy_reasons()` 說出實際在忙什麼。**它的擋人條件刻意沒有改成 `_any_long_op_running()`**：那段條件額外含有 `ctrl.measuring_active` / `ctrl.scanning_active` 兩個控制器層級旗標，是註冊表（只服務 UI 忙碌顯示與停止分派）不該涵蓋的；`_busy_reasons()` 為空時退回原本的通稱字串。

**本次沒有把 `motion_active` 或 `scanning_active` 併進註冊表，也沒有讓註冊表參與任何移動守衛判斷。** 那是 [fiber-scan.md](fiber-scan.md) 明列的紅線。

### 7.6 驗證

`verify_scan_tab.py` 新增案例 28（9 項，class `TestLongOperationRegistry`），鎖住：註冊表涵蓋四項（28a）、`homing` 的 `request_stop` 是 `None`（28b）、四個旗標各自驅動 busy（28c）、`_busy_reasons()`（28d）、無條件分派到三個目標（28e）、單項失敗不影響其餘（28f）、經過時間只在進行中更新且收工後保留最後值（28g，`_on_scan_done` 的完成橫幅要讀它）、`show_elapsed` 確實延後求值（28h）、`_do_stop`／`_on_escape` 同時涵蓋重播與量測（28i）。

全套回歸測試 **413 項通過**（404 + 9），指令：

```bash
xvfb-run -a venv/bin/python -m pytest verify_scan_tab.py verify_meter_panel.py verify_axis_calib.py \
  verify_fiber_scanner_signal.py verify_wait_axis_stop.py verify_ctrl_pos_sync.py verify_blind_scan.py \
  verify_scan_export.py verify_scan_powell.py verify_scan_powell_integration.py -q
```

### 7.7 副作用：行數不減反增

main_ai.py 從 6820 → **7000 行**（+180）。程式碼本身其實變少（六個接線點合計 −50 行、註冊表機制 +60 行），增加的幾乎全是把原本散在六處的理由收攏成一份完整說明。**這正好再次印證〈四〉的判斷：行數對「該不該拆」沒有預測力**，不要因為這次 +180 就把它讀成惡化訊號。

### 7.8 對將來 mixin 化的意義

前置 1 讓「長時間背景作業」的接線收斂成 `DS102GUI` 上的一個方法。將來若真的做方向1b，`_register_long_ops()` 是**必須留在核心類別、不可下放到任何 mixin** 的東西——它天生要橫跨所有分頁，正是核心的職責。反過來說，這也是本次複審主張「先拆 Core 自己的職責、不是急著拆分頁」的具體成果之一。

---

## 八、前置 3 落地紀錄（2026-08-31，`ui_theme.py`）

### 8.1 實際做了什麼

新增 [ui_theme.py](../ui_theme.py)（86 行），內容是**十個 `CLR_*` 色票**加上完整的
設計理由。main_ai.py 改成 `from ui_theme import (CLR_BG, ...)` 逐一列名重新引入
（不用 `import *`——底下有 445 處引用，`import *` 會讓靜態分析完全查不到來源）。

依賴鏈：`ds102_ctrl.py → ui_theme.py → main_ai.py`，無環。`ui_theme.py` 只
import 一個名字（`ds102_ctrl._app_settings`），**不 import main_ai.py 或任何
GUI 模組**。

### 8.2 與原計畫的重大差異：`_app_settings` 沒有搬

〈前置 3〉原文寫的是「`_app_settings` / `_app_setting_num` / `CLR_*` 抽成第三個
檔案」。**這一半做不到，而且不該做。** 動手前重新 grep 才發現：這三個名字早在
2026-08-17 拆 `ds102_ctrl.py` 時就**已經不在 main_ai.py** 了——它們定義在
`ds102_ctrl.py:203-259`，理由記在該檔的區塊註解：`HISTORY_MAX`（`DS102Controller`
用的常數）需要靠 `_app_setting_num()` 覆寫。

把它們搬進 `ui_theme.py` 會製造**新的循環相依**：

```
ds102_ctrl.HISTORY_MAX      需要  ui_theme._app_setting_num
ui_theme._load_app_settings 需要  ds102_ctrl._load_json_settings + RECORDING_DIR
```

**這件事本身值得記下來**：複審筆記寫於 2026-08-31，但〈前置 3〉那一段的內容是
從 2026-08-18 之前的舊結論繼承下來的，沒有重新驗證檔案配置。**複審筆記裡「繼承自
更早結論」的段落，行號與檔案歸屬都要當成過期資訊重新查證。**

最終的職責劃分反而更乾淨，也符合實情：設定檔的**載入**留在最底層的
`ds102_ctrl.py`（`app_settings.json` 裡不只有色票，還有 `history_max` 這種跟 UI
完全無關的欄位），`ui_theme.py` 只負責把已載入的字典**解讀成色票**。

### 8.3 刻意沒有一起搬的東西

**UI 節奏常數**（`POSITION_POLL_INTERVAL` / `LOG_TEXT_MAX_LINES` /
`UI_REDRAW_INTERVAL` / `BANNER_COALESCE_SEC` / `METER_POLL_INTERVAL` /
`SCAN_PLOT_REDRAW_INTERVAL` / `PM_FLOAT_W`／`PM_FLOAT_H`）留在 main_ai.py。

理由是它們與 `CLR_*` 有**本質差異**：`CLR_*` 真正橫跨全部七個分頁，是共用的；而
每個節奏常數各自只有**一個**擁有者分頁（`SCAN_PLOT_REDRAW_INTERVAL` → 尋光、
`METER_POLL_INTERVAL`／`PM_FLOAT_*` → 光功率、`LOG_TEXT_MAX_LINES` → LOG，其餘三個
→ Core）。將來真的 mixin 化時，它們應該**跟著各自的 mixin 走**，塞進一個叫
`ui_theme` 的檔案反而是把單一擁有者的東西假裝成共用資源。

**`StatusBar` 也沒有搬。** 前置 3 確實已經解除了它的阻礙（見 8.4），但搬它是另一
件事：它有 tkinter 元件、要 `DS102Controller` 型別、且與 `DS102GUI._status_bars`
的集中管理耦合。現在把它單獨搬進一個新檔案，只是把 120 行從 A 檔移到 B 檔而沒有
解決任何實際問題；等真的做 mixin 化、需要決定「共用 UI 元件放哪」時一併處理，才
會落在正確的位置。**差別在於：現在這是一個「可以做但沒必要」的選項，而不是像以前
那樣「想做也做不到」。**

### 8.4 對將來 mixin 化的意義

原文列的三個理由全部成立且已兌現：

1. 零風險（純模組層級常數搬家，無狀態、無執行緒、無初始化順序問題）——實測 413 項
   回歸測試零修改、零失敗。
2. `StatusBar` 的搬遷阻礙解除。
3. 將來每個 `*TabMixin` 都改 `from ui_theme import ...`，不會有任何一個 mixin
   需要 `from main_ai import CLR_*`（那正是循環相依本身）。**這一步必須在
   mixin 化之前完成，不是跟它一起做。**

覆寫時機不變：`CLR_*` 在 `ui_theme.py` **模組載入當下**就把
`app_settings.json` 的覆寫值算完並固定，**不可改成延遲求值**——`_build_*` 在
`DS102GUI.__init__` 期間就把這些值餵給 tkinter 元件的 `bg=`／`fg=` 了。

### 8.5 行數

main_ai.py 7000 → **7017**（+17）。移除 13 行色票定義，換來 16 行 import 與 14 行
理由註解。**又一次印證〈四〉的判斷：行數對「該不該拆」沒有預測力**，不要把 +17
讀成惡化訊號。

---

## 九、前置 2 落地紀錄（2026-08-31，`PowerReading`）

### 9.1 決策：實作，不是只做設計

派工時要求先盤點、判斷風險是否超出「重構」範疇。盤點結果是**沒有超出**，理由是
執行緒歸屬全部查得清楚且一面倒：

| `self._pm_reading` 的存取點 | 執行緒 |
|---|---|
| `_on_meter_reading()`（背景輪詢／立即查詢的結果） | 主執行緒（兩條路徑都走 `root.after`） |
| `_scan_plot_extend()`（尋光轉貼） | 主執行緒（掛在 `_redraw_scan_plot` 的 `root.after` 鏈） |
| `_on_meter_connect_result()` / `_disconnect_meter()` | 主執行緒 |
| `_pm_update_age_label()` | 主執行緒 |
| **`_get_last_pm_value()`** | **移動執行緒**（`ds102_ctrl._record_data_point()` 在 `_wait_axis_stop()` 的等待迴圈裡呼叫） |

也就是「單一寫入執行緒 ＋ 單一跨執行緒讀取者」。這個形狀**不需要鎖**，而且改成
frozen dataclass 之後跨執行緒讀取的一致性**變好了**（見 9.3）。所以沒有走「只做
設計、記錄風險」那條路。

### 9.2 資料結構

main_ai.py 模組層級新增 `@dataclass(frozen=True) class PowerReading`（緊接在
`LongOperation` 之後）：

| 欄位 | 型別 | 語意 |
|---|---|---|
| `value` | `Optional[float]` | 最近一次成功讀值（dBm）；`None`＝從未讀到可信讀值 |
| `ok_time` | `float` | 該次讀值當下的 `time.time()`；`0.0`＝從未成功讀到（沿用舊 `_pm_last_ok_time` 的 `<= 0` 判斷語意） |
| `source` | `str` | `"meter"`／`"scan"`。**純診斷，沒有任何邏輯依它分流** |

取代原本的 `_pm_last_value` / `_pm_last_ok_time` 兩個裸欄位（已從程式碼完全消失）。

### 9.3 frozen 是唯一真正的技術理由，而不是「看起來比較整齊」

原本 value 與 ok_time 是**兩行各自指派**，移動執行緒有機會讀到「新的 value 配舊的
ok_time」這種撕裂組合。換成 frozen dataclass 後，更新是**單一次屬性重新指派**
（GIL 下不可分割），跨執行緒讀取端拿到的必定是同一次讀值的完整快照。

**但這只是讓原本就存在的跨執行緒讀取變得一致，沒有、也不宣稱新增任何鎖保護。**
`PowerReading` 的 docstring 明文寫了這句——不要因為它現在有名字就假設它是執行緒
安全的容器（派工時特別點名要避免的「看起來安全其實還是沒鎖」的假象）。真正的保證
只有「單一寫入執行緒 ＋ 不可變快照」這一條。

### 9.4 唯二的寫入者

| 方法 | 用途 | 呼叫者 |
|---|---|---|
| `_pm_note_reading(value, source)` | 記錄一筆成功讀值；同時更新 `_pm_power_var`／`_pm_unit_var`／狀態燈，然後呼叫 `_pm_refresh_status_line()` ＋ `_pm_update_age_label()` | `_on_meter_reading()` 成功分支、`_scan_plot_extend()` |
| `_pm_clear_reading()` | 回到「沒有任何可信讀值」 | `_on_meter_connect_result()`、`_disconnect_meter()` |

`_pm_note_reading()` **刻意不碰 `_pm_comm_failures`**——那是背景輪詢自己的失聯
計數，尋光期間它本來就沒在跑，被轉貼路徑累加或歸零都會污染尋光結束後的失聯判斷。
這是搬進來之前 `_scan_plot_extend` 就已經寫明的既有約定，不是新規則（案例 29f 鎖住）。

### 9.5 關鍵成果：`_scan_plot_extend()` 的 7 個寫入點收斂成 1 個呼叫

這是〈門檻 2〉表格最後一列點名的、複審裡最嚴重的那個橋接點。改動前它直接寫：

```
_pm_last_ok_time / _pm_last_value / _pm_power_var / _pm_unit_var /
_pm_power_lbl.config(fg=) / _pm_status_var / _pm_status_lbl.config(fg=) /
_pm_set_status_dot() / _pm_update_age_label()
```

改動後：

```python
if valid_samples := [s for s in samples if s.ok and s.power is not None]:
    self._pm_note_reading(valid_samples[-1].power, source="scan")
```

其中「尋光中（讀值由尋光分頁提供）」那兩行**是刪掉而不是搬走的**：
`_pm_refresh_status_line()` 的 `scanning_active` 分支本來就會設同一句話，兩邊是
**兩份會走鐘的真相來源**。刪掉之後狀態文字完全由 `_pm_refresh_status_line()`
決定，它 docstring 自稱的「唯一寫入者」這次才真的成立。

失效情境（若有人把那兩行加回去）：尋光結束後最後一批樣本才進到繪圖函式，此時
`scanning_active` 已是 False，畫面卻會被改回「尋光中」，而 `_pm_sync_scan_notice()`
的還原分支這一輪已經跑過，要等下一輪 100ms 才校正——使用者會看到狀態文字閃一下。
案例 20d 鎖住這個行為。

### 9.6 順手修掉的一處不對稱（行為變更，範圍極小）

`_on_meter_connect_result()` 原本只把 `_pm_last_ok_time` 歸零、**沒有清
`_pm_last_value`**；`_disconnect_meter()` 反過來只清 value、沒有歸零 ok_time。
前者的後果是「連上新的光功率計、還沒讀到第一筆值之前，`_get_last_pm_value()` 會
回傳上一個 session 的舊數值」，那個值會直接寫進 `data/*.csv` 的 dbm 欄位而沒有
任何標記。

實務上很難踩到（UI 上連線鍵是 toggle，連線前必定經過 `_disconnect_meter()`），
所以風險評估為極低；但既然兩條路徑本來就該表達同一件事，沒有理由讓它們各清一半。
兩處統一改呼叫 `_pm_clear_reading()`（案例 29d／29e 鎖住）。

### 9.7 沒有動的東西

**自動量程退回邏輯完全沒碰。** `_on_scan_signal_found()`（鎖定量程）與
`_restore_meter_auto_range()`（尋光結束還原）一行未改——它們操作的是儀器狀態，
不是「讀值怎麼存」。見 CLAUDE.md〈光功率計預設用自動量程〉那條紅線。

Scan→Power 剩下的橋接點：`_scanner_power_query()`、`_restore_meter_auto_range()`、
`_on_scan_signal_found()`、`_pm_sync_scan_notice()`、`_pm_note_reading()`。全部
都是**有名字的方法呼叫**，沒有任何一個是直接指派對方的欄位或 widget。

### 9.8 驗證

- `verify_meter_panel.py` 新增 class `TestPowerReading`（案例 29a~29f，6 項）
- `verify_scan_tab.py` 新增案例 20c（`source` 標成 `"scan"`）、20d（不再自己寫狀態文字）
- 既有測試中直接指派舊裸欄位的 8 處改用新的寫入方法（12b／12c／13a／13b 的
  `g._pm_last_value = ...` → `g._pm_note_reading(...)`／`g._pm_clear_reading()`；
  4e 的 `_pm_last_ok_time` → `_pm_reading.ok_time`；20b 的 `_pm_last_value` →
  `_pm_reading.value`）

全套回歸測試 **421 項通過**（413 + 8）：

```bash
xvfb-run -a venv/bin/python -m pytest verify_scan_tab.py verify_meter_panel.py verify_axis_calib.py \
  verify_fiber_scanner_signal.py verify_wait_axis_stop.py verify_ctrl_pos_sync.py verify_blind_scan.py \
  verify_scan_export.py verify_scan_powell.py verify_scan_powell_integration.py -q
```

main_ai.py 7017 → **7114**（+97，幾乎全是 `PowerReading` 與兩個寫入者的說明文字；
程式碼本身 `_scan_plot_extend` 少了 8 行）。

### 9.9 對〈四〉觀察指標的影響

指標 2（「跨分頁的共用可變狀態數量，目前 1 組，出現第二組即觸發」）——**這 1 組
已經消滅**。指標改成盯：

> `grep -n "_pm_reading" main_ai.py` 的結果，除了 `PowerReading` 定義、`__init__`
> 的初始化、`_pm_note_reading()`／`_pm_clear_reading()` 兩個寫入者、以及
> `_get_last_pm_value()`／`_pm_update_age_label()` 兩個讀取者之外，**不應該再有
> 第七類存取點**。特別是「尋光」分頁側**一次都不該出現**。

指標 3（尋光突破 2000 行就把「尋光＋光功率」當一個 `AlignmentMixin` 整塊搬出）的
前提條件——前置 2——現在已滿足。
