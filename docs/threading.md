# 輪詢迴圈與點動限位保護

> 本文件自 CLAUDE.md 拆出（2026-08-26），目的是縮小每次對話的固定載入量。
> **內容未經刪減**，動到對應功能前請完整讀過本檔。

#### 輪詢迴圈（各司其職，別互相取代）

原本四條，2026-08-12 光功率整合後加了第五條（獨立於 DS102 序列通訊，走 GPIB）、2026-08-13 尋光整合後加了第六條（純重繪，不做任何 I/O）：

| 迴圈 | 在哪 | 節奏 | 做什麼 |
|---|---|---|---|
| `_start_poller()` | Tk 主執行緒 `root.after` | `UI_REDRAW_INTERVAL` 100ms | **只重繪快取的座標**，完全不碰序列埠 |
| `_start_position_worker()` | 背景執行緒 | `POSITION_POLL_INTERVAL` 0.5s | 呼叫 `refresh_positions()`，對每個已啟用軸送一筆 `POS?` 寫回 `_positions_pulse` |
| `_poll_status()` | 背景執行緒 `while` + `time.sleep(0.1)` | 100ms | 移動中追蹤選取軸的狀態，`status != "Driving"` 就收工 |
| `_watch_jog_limit()` | 背景執行緒 | `JOG_WATCH_INTERVAL` 0.06s | 長按點動時監看軟體限位 |
| `_start_meter_poll_worker()` | 背景執行緒 | `METER_POLL_INTERVAL` 0.5s | 光功率背景輪詢（GPIB，與上述四條的序列埠通訊完全無關，不受 `_serial_lock` 影響） |
| `_redraw_scan_plot()`（經 `root.after`） | Tk 主執行緒 | `SCAN_PLOT_REDRAW_INTERVAL` 250ms | 只消化 `_scan_plot_pending` 佇列重繪 matplotlib 圖表，不觸發任何量測或移動 |

前兩者的分工是關鍵：`_start_poller` 之所以能跑 10ms 是因為它不做 I/O；`query_status()` 一次**只更新它被傳入的那一軸**，沒有 position worker 的話未選取的軸會永遠停在舊值。要加「畫面上某個數字沒在更新」的修正時，先確認該值是靠哪一條迴圈供應。

（原本是 10ms，2026-08-05 放寬到 100ms：資料源只有 2 Hz，10ms 等於 49/50 次在重繪同一個數字，代價是每秒約 600 次 `_lock` 取放與數千次 `Label.config()` 全堆在 Tk 主執行緒上。這是「介面越跑越鈍」的成因之一。）

#### LOG 量的控制（別把它改回去）

`_serial_write_read` 對 `_POLL_QUERIES`（`POS?` / `SB1?` / `SB2?` / `SB3?`）**不寫 DEBUG TX/RX**。這些是每秒十幾筆的例行輪詢，逐筆記錄會同時灌爆 LOG 檔、`action_history`、GUI 的 Text widget，而且每一筆都要 `root.after` 回主執行緒重繪五個 StatusBar——實測是「LOG 過多」與介面卡頓的直接成因。要追通訊細節時把 `DS102Controller.verbose_poll_log` 設 `True` 即可恢復。

WARN／ERROR 一律照記，安靜的只有成功路徑。另有兩道上限：`action_history` 最多 `HISTORY_MAX`(5000) 筆、GUI LOG 文字框最多 `LOG_TEXT_MAX_LINES`(2000) 行，都是無上限成長會拖垮程式的地方。

#### 長按點動的限位保護（`move_continue` / `_watch_jog_limit`）

**2026-09-02 起，連線時會自動同步，但這治不好「出廠狀態下沒有保護」這件事本身。** `ds102_ctrl.DS102Controller.sync_sw_limits_from_controller()` 在 `connect()`（`restore_controller_config()` 之後）逐軸逐側查 `CWSLE?`/`CCWSLE?`/`CWSLP?`/`CCWSLP?`，把韌體端限位鏡射進 `self.sw_limits`——合併規則是「只收緊、永不放寬、永不清空」：使用者手動打數字並套用過的值不會被這次同步覆寫，兩邊都有值時取較嚴格的一側。**但韌體軟體限位本身出廠／斷電後就是停用的**（`docs/hardware.md` 實測），此時查回來的 `CWSLP?`/`CCWSLP?` 是哨兵值（±99999999），同步邏輯會把它視為「無有效邊界」，`sw_limits` 該側維持 `None`——所以出廠狀態連線後 `lim` 依然是 `None`，第一層不執行、第二層執行緒依然不會啟動，跟同步前的行為一致。這支同步做到的是「兩層彼此不再各說各話」＋連線成功時跳橫幅明講「軸 X 沒有任何行程保護」，把不可見的無保護狀態變成可見，**不是**讓長按點動預設就有保護。別把下面這兩層當成常態保護，除非使用者已經手動設定過韌體限位（透過〈儲存控制器設定〉持久化）或本次連線後手動套用過 `sw_limits`。

點動沒有目標座標，無法像 `move_step` 那樣事先攔截，所以（在 `sw_limits` 有值時）是兩層：

1. **出發前**——已經壓在該方向的軟體限位上就直接拒絕啟動。
2. **移動中**——`_watch_jog_limit` 背景執行緒輪詢 `POS?`，越界立刻 `stop()`。

第二層是「事後偵測」，從發現到停穩還會滑一段，提前量 `lookahead = f_speed × period + f_speed × rate / 2000`。`period` **必須用每輪實測值**（`time.time()` 差）而非常數：實測用常數 60ms 時真正的週期是 116ms，結果滑出限位 36 pulse。改動這裡前先讀該函式的 docstring，兩次超限的數據都記在裡面。

`self._jog_stop` 事件負責讓監看執行緒收工——`stop()` 會 set 它，所以任何新增的停止路徑都要記得 set，否則執行緒會活到程式結束。**2026-08-20 補齊了兩個漏掉的路徑：`emergency_stop()` 與 `disconnect()` 原本都不會 set `_jog_stop`**——點動中觸發這兩者會讓監看執行緒收工旗標卡在 `clear()` 狀態。這原本只是「執行緒活到程式結束」的既有已知代價，但後來新增的 `motion_active` property（見下方〈移動期間暫停光功率背景輪詢〉）直接讀 `_jog_stop.is_set()`，卡住的旗標會讓 `motion_active` 永久回報 `True`。兩處都已補上 `self._jog_stop.set()`。
