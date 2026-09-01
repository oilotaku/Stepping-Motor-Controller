# 原點復歸與重現性量測

> 本文件自 CLAUDE.md 拆出（2026-08-26），目的是縮小每次對話的固定載入量。
> **內容未經刪減**，動到對應功能前請完整讀過本檔。

### 原點復歸（`origin_all` / `_wait_origin_done`）

「全軸原點復歸」不是單純對每軸送 `GO ORG`，中間有三個必要條件，改動時別拆掉：

- **等待要用 `_wait_origin_done()`，不能沿用 `_wait_axis_stop()`**。復歸樣式 5/6 本來就靠偵測限位感測器邊緣定位，途中壓到限位是正常流程；`_wait_axis_stop` 會把 limit 當失敗回傳。逾時也不同：復歸可能橫跨整個行程，給 180s。
- **復歸期間必須暫時停用控制器軟體限位**，結束後（`finally`）還原。原點通常落在行程末端（POS≈0），在軟限位之外，不關掉會被自己設的保護擋住。
- **`GO ORG` 完成後 POS 不會停在 0——「歸位後 0 點不固定」的實測數據。**
  2026-08-05 於 COM2 量測：各軸離開原點 800 pulse 後單獨送 `GO ORG`，重複三輪，殘差為

  | 軸 | 三輪殘差 (pulse) | 特性 |
  |---|---|---|
  | X | 1, 0, 1 | ≈0～1 |
  | Y | 7, 8, 7 | 系統性偏 **+7～8** |
  | Z | −7, −6, −8 | 系統性偏 **−6～−8** |

  也就是**每軸有各自固定的偏移量，再疊加 ±1～2 pulse 的機械重現性**。`MEMSW7?` 讀回是 `0` 也一樣會發生，所以別把 MEMSW7 當成歸零的保證。

  因此 `origin_all` 與 `move_origin` 都在復歸後檢查，非 0 就**強制送 `POS 0`**（摘要標記 `(POS=0 強制)`）。這讓軟體座標原點每次一致，殘留的不確定性降到機械重現性本身的 ±1～2 pulse——那是硬體下限，改程式消不掉。

**「非 Driving」不等於「復歸完成」——`_wait_origin_done()` 曾在復歸還沒開始時就回報成功（2026-08-21 實機指令追蹤證實）。**

  實測 Driving 位元的 assert 延遲（三種起始條件幾乎一致，所以是韌體處理 `GO` 指令的固定成本，跟「是不是從限位上出發」無關）：

  | 指令 | assert 延遲 |
  |---|---:|
  | `GO ORG`（壓在限位上出發） | 96 ms |
  | `GO CW`（一般步進） | 96 ms |
  | `GO ORG`（離開限位後出發） | 80 ms |

  決定會不會踩到競態的是**呼叫端第一次查詢有多快**：`_do_origin()` 送出後只打一次 `SB1?`（約 56ms）→ 穩定落在 96ms 之前 → **必然**踩中；`_wait_axis_stop()` 第一次走 `query_status()` 要 `SB3?`+`SB1?` 兩次往返（約 112ms）→ 剛好越過 → 大多數時候僥倖避開。實測序列：

  ```
  TX='AXI3:...:GO ORG'
  TX='AXI3:SB1?' RX='10'   ← bit6 未 set → 舊版立刻 return True
  TX='AXI3:POS 0'          ← 於是把座標系原點寫在滑台正要起飛的那一刻
  TX='AXI3:SB1?' RX='66'   ← 0x42，Driving 這時才 assert
  ```

  修法是 `_wait_origin_done_ex()` 的**三重證據**：`saw_driving OR pos_changed`，外加 `ORIGIN_START_GRACE`(2.0s) 寬限期與 `ORIGIN_START_POLL`(0.1s) 快輪詢。只有「Driving 從沒 assert **且** POS 完全沒動」才判 `not_executed`——物理上就是什麼都沒發生。兩個證據必須 OR：只看 Driving 會被 0.5s 輪詢節奏漏掉極短的復歸，只看 POS 會把「本來就在原點、復歸原地不動」誤判成失敗。`_wait_origin_done()` 退化成薄 bool wrapper，既有呼叫點不必改簽章。

  **這次刻意修共用函式本身而不是隔離，跟 `_wait_axis_stop` 那次相反。判準是「這個改動對既有呼叫端是增加保護還是減少保護」，不是「是不是共用函式」**：

  | | `_wait_axis_stop` | `_wait_origin_done` |
  |---|---|---|
  | 量測需要的例外 | **放寬**（容忍出發側限位） | **收緊**（要求移動證據） |
  | 對既有呼叫端 | 放寬會讓一般移動撞限位變成靜默成功 → 必須隔離 | 收緊會讓假成功變成明確失敗 → 應該共用 |

**危險寫入要自己設閘門，不能只信上游的等待函式（`_confirm_stopped()`）。** 這是本專案第三次在同一個模式上出事（`_wait_axis_stop` 誤判撞限位、`_wait_origin_done` 誤判完成、`POS 0` 寫在飛行中），所以升格成通則而不是第三則個案：**傷害發生在 `set_position(axis_no, "0")` 這道指令上，不是在等待函式裡**。等待函式的判定再嚴格都只是「相信上游」，任何新呼叫路徑或未來改動都可能繞過。`_confirm_stopped(axis_no)` 連續數次確認「狀態非 Driving 且 POS 完全沒變」，四個歸零呼叫點（`move_origin`／`origin_all`／量測基準復歸／`_measure_one_combo` 的 `finally`）全部先過它，確認不了一律不寫並記 ERROR。用 POS 連續不變而非只看 Driving，理由同上——Driving 有 96ms 的 assert 延遲，POS 是實際位移的直接證據。

**孿生競態已修（2026-08-26）：`_wait_axis_stop()` 的起步窗口。** 它的 `status == "Stop"` 原本直接 `return True`，而第一次 `query_status()` 約 112ms、Driving assert 延遲約 96ms——**餘裕只有約 16ms**。落在那個窗口就會把「還沒起步」讀成「已經停好」，`move_step(wait_done=True)` 在軸飛行中回報成功，下游 `goto_point()` 提前送出下一軸、`fiber_scanner._measure_here()` 在移動中量光功率。修法沿用 `_wait_origin_done_ex()` 已實機驗證過的三重證據配方（`MOVE_START_GRACE`(1.0s)／`MOVE_START_POLL`(0.05s)／`MOVE_POS_EPS`(1)／`MOVE_MOTION_EPS`(1)，與 `ORIGIN_*` 那組同樣刻意不外部化到 `safety_settings.json`）：

- **證據三選一**：①看過 Driving assert（正常移動的主要路徑）②走完預期行程 `|POS − start_pos| >= expected_travel − 1`（涵蓋「短到在第一次取樣前就跑完」的移動，單看 Driving 會誤判成從未起步）③POS 相對**第一次取樣值**變化過（呼叫端沒傳提示時的保底）。三者皆不成立且寬限期已過，才判 `GO` 未生效、回 `False` 並記 ERROR＋發警報。
- **證據要求只在寬限期內生效，寬限期一過就回到舊語意——這是與本檔原本記載的修法（無條件要求 `travelled >= expected − 1`）唯一的差異，且這個差異是必要的。** 無條件版會在使用者中途按「停止」時退化成空等到 `WAIT_TIMEOUT`(30s)：`STOP 0` 讓軸提前停下，`travelled` 永遠達不到 expected。競態純粹是「起步窗口」現象，把要求限縮在寬限期內就足以堵住，且寬限期之後的行為與改動前逐字相同。
- **`moved` 成立不可當成 `return True` 的通用捷徑。** 走完了預期行程但停在限位上，是貨真價實的撞限位，必須照〈第一批修正〉「撞限位不再靜默」的結論大聲報出來。分支結構因此是「先分 `status == "Stop"` 與其他，再各自考慮證據」，不是「有 `moved` 就成功」——`verify_wait_axis_stop.py::TestLimitHandling::test_limit_with_full_travel_is_not_swallowed_by_evidence` 就是鎖這一點。
- **順帶修掉一個既有的同源誤判**：從限位上往反方向出發時，`GO` 尚未生效的那一刻讀到的是「出發前就壓著的那顆限位」，舊寫法會直接判失敗並發警報。現在寬限期內、且沒有位移證據時對限位狀態也續輪，等 Driving assert 即可分辨；真的走不掉則寬限期一過照樣報錯，代價只是這種必定失敗的情境晚 1 秒才報。這與 `_wait_axis_stop_leaving_limit()` **不是**同一件事——後者仍然只給量測路徑用，本體沒有放寬「行進方向那一側限位＝失敗」的判定。
- **四個呼叫端都補上了位移提示**（沒傳只是少一條證據，不會誤報成功，但短移動會被誤判成「未生效」，所以有值就該傳）：`_do_move_step()` 傳快取的機械座標＋`pulse_amt`（刻意不另打一筆 `POS?`——熱路徑上多一次往返約 56ms，而快取在每次移動結束時都被 `query_status()` 寫成當下實測值）；公開的 `wait_axis_stop()` 加了兩個選用參數並轉交；`fiber_scanner._move_multi_axis()` 出發前抓一份 `positions_machine` 快照、依各軸 delta 分別傳入（搜尋的單步移動量常常小到在第一次取樣前就跑完，這裡最需要證據②）；`play_recording()` 用新的 `_replay_move_hint()` 從原始指令字串反解 `PULS n` ＋ `GO CW/CCW`（`GO ABS`／`HOME`／`GOTCH` 的行程與 `PULS` 無關，一律回 `(None, None)` 退回保底證據）。
- **回歸測試**：`verify_wait_axis_stop.py`（29 項），涵蓋競態序列、短移動、`GO` 未生效、限位四類判定、EMS／逾時／使用者中途停止，以及四個呼叫端有沒有真的把提示傳下去。**尚未實機驗證**——本次改動全部以假物件測試為準，`MOVE_START_GRACE` 對真實韌體的餘裕是否足夠、以及「移動未生效」會不會在實機上誤報，都要等下次接上 COM2 時確認。

`MEMSW0?` 回 `0`（樣式 Type0＝不執行）與 `Stage not connected` 的軸會被略過；單軸失敗不中止整批。

**`MEMSW` 是 RAM-only，控制器斷電後整組（MEMSW0～7、全軸）回到 0**——與韌體軟體限位同一個性質，2026-08-05 實機踩到。而 `MEMSW0=0` 的語意是「復歸樣式 Type0＝不執行」，所以**斷電後按「全軸原點復歸」會把每一軸都合法略過、幾秒跑完**。舊版在這情境下回傳 `True`，使用者看到成功訊息但滑台根本沒動——現在改成：一軸都沒真的復歸就回傳 `False` 並說明原因。

**解法是 `recordings/controller_config.json`**（〈控制器設定持久化〉（見 [settings-files.md](settings-files.md)））：連線時自動把存檔的 MEMSW0 補回控制器。補不齊的部分再由 `check_homing_config()` 用 LOG 與橫幅提醒（未接滑台的軸會排除）。

各軸的復歸樣式：**X=2、Y=1、Z=2、U=0**（U 未接滑台）。

但「未接滑台會被略過」**不是無條件成立**：它靠 `query_status` 回傳 `"Stage not connected"`，而該字串只在 `SB1 & 0x06`（有限位位元）成立時才有機會產生。未接滑台的軸當下若沒觸發限位位元，`query_status` 會回 `"Stop"`，該軸不會被略過，而是照送 `GO ORG` 然後等到 180s 逾時。

#### 原點復歸重現性量測（`measure_homing_repeatability`，2026-08-21）

自動化原本要人工用碼表做的量測：讓軸離開原點固定 pulse 數 → 送 `GO ORG` → **在強制歸零之前**讀 POS 殘差，重複 N 輪並掃描多個離開距離，統計殘差離散程度。目的是回答「軟體座標原點能不能當光纖對準的可信基準」——2026-08-05 那張手動量測表（X≈0～1、Y≈+7～8、Z≈−6～−8）只有三輪、且無法分辨「固定偏移」與「每輪累積漂移」，這個功能就是為了補上這個缺口。GUI 落點是**移動控制分頁**的一張獨立卡片（`_build_card_origin_repeatability`，緊接速度設定卡片之後），不是新分頁——依〈模組化現況與下一步門檻〉的門檻 4，「分頁邊界依然清楚、只是又加一張獨立卡片」不觸發拆分。

**`_do_origin()` 是專案裡第三個「無守衛層」**（前兩個是 `_do_move_step()` 與 scanner 用的 `scan_move_step()`）。它從 `move_origin()` 抽出「設 MEMSW0 → 送 `GO ORG` → `_wait_origin_done()`」的核心，不讀 POS、不歸零、**不含任何 `scanning_active`／`measuring_active` 守衛**——量測方法必須能呼叫它，否則會被自己設的旗標擋住（`scanning_active` 進 `move_step` 守衛導致所有收斂測試卡死，是本檔記載過的既有教訓）。`move_origin()` 改成呼叫它之後外部行為逐字不變（仍然讀 POS、非 0 就強制歸零），`origin_all()` 完全沒動。**`_do_origin()` 刻意不抽 MEMSW7**：`move_origin()` 原本就沒設 MEMSW7，只有 `origin_all()` 有，抽進去會改變 `move_origin()` 的既有行為。

- **`measuring_active`（`ds102_ctrl.py`）** 加在 `move_step`／`move_continue`／`move_origin`／`origin_all`／`goto_point` 五處公開守衛（跟 `scanning_active`／`playback_running` 並列的 OR 條件），並排除於 `_start_position_worker`。**絕對不可放進 `_do_move_step()`／`_do_origin()`**，同上。
- **`at_origin` / `origin_lost` 不變量：只有剛成功完成一次 `_do_origin()`，滑台才真的在原點，這時候才可以寫 `POS 0`。** `_measure_one_combo()` 的 `finally` 是**條件式**歸零，不是無條件——`_do_move_step` 撞限位、`_do_origin` 逾時、或 EMS 中止時滑台停在行程中的任意點，此時寫 `POS 0` 等於把座標系原點偷偷改到滑台當下位置（之後 goto 教點與限位比對全部跟著偏移，且零警告）。這比不歸零危險得多，是 architect 審查抓到的 M3。不歸零時記 ERROR、回傳 dict 帶 `origin_lost=True`，GUI 端用 `CLR_DANGER` 橫幅示警（跟撞限位同等級）。
- **`origin_lost=True` 必須中止該軸剩餘的 offset**（offsets 迴圈裡 `combo_done_cb` 之後檢查，成立就記進 `skipped_axes` 並 `break`，只中止該軸、其他軸各自有基準復歸不受影響）。這是第二輪審查的 N1：`_measure_one_combo` 的 `at_origin` 初始值是 `True`（沿用「進場時在原點」的前提），前一個組合失準後若不收手，下一個組合會在座標系已失準的框架裡量出誤報的「累積漂移」；更糟的是它若在第一輪之前就被中止，`at_origin` 還是初始的 `True`，`finally` 就會把 `POS 0` 寫在撞限位停下的錯誤位置——M3 的失效模式從組合內部搬到組合之間。
- **每軸開始前先做一次基準復歸**（`_do_origin()` + `set_position(axis_no, "0")`，在停用韌體限位之後、offsets 迴圈之前）。沒有這一步的話「進場時 POS≈0」只是隱含假設：使用者若剛點動完停在 POS=3000 又手動指定方向，第一組 offset 必定作廢且失敗原因會被誤報成「累積漂移」（architect 審查的 M4）。
- **`_homing_repeat_abort(stop_event)`** 統一 `stop_event.is_set() or ems_active` 判斷，**只給外層 axes／offsets 迴圈用**；`_measure_one_combo()` 內部刻意手動分開檢查，因為它要據此寫出不同的 note 文案（「EMS 觸發，中止量測」／「移動失敗/撞限位」／「原點復歸逾時」／「使用者中止（復歸中）」）——把一次緊急停止標成「撞限位」會讓事後判讀資料的人往完全錯誤的方向查。**不要為了「統一」把 combo 內的檢查換成這個 helper**，那會把 note 的區分能力弄丟。
- **量測期間會暫停該軸的韌體軟體限位**（比照 `origin_all` 既有邏輯，`finally` 還原，**讀不到原值一律還原成 `1`（啟用）**，不可 fail-unsafe），所以量測進行中唯一的越界保護是機械限位開關與 Python 端的距離防呆。確認對話框有對應警語。
- **歸零策略是「整批不歸零、每個 (軸, offset) 組合結束才歸零一次」**（mathematician 定案）：量到的是相對單一基準的絕對序列，可事後差分還原成逐輪增量，反之不行。這是能分辨「固定偏移」與「累積漂移」的唯一做法，刻意**不**提供每輪歸零的 GUI 選項。統計上：無漂移時 headline 是 peak-to-peak `range`（對準容差是硬邊界，σ 會低估最壞情況），`median` 是系統性偏移**不是**重現性；判定為漂移時 `range`／`σ(p)` 隨 N 成長無意義，改報 `drift_rate`／`σ(diff)`。`n<2` 時所有統計欄位是 `None`。
- **資料落地在 `data/homing_repeat_YYYYMMDD_HHMMSS.{csv,json}`**（實驗數據，**不走** `recordings/` 那套 `_write_json_with_backup`／`_points_loaded` 拒寫保護——那是為「累積型集合被空狀態蓋掉」設計的，這裡每次都是全新檔案）。CSV 長格式，欄位含 `origin_lost`／`memsw7`，**零樣本的組合也會輸出一列**（`origin_lost` 那些最重要的失敗案例往往正是零樣本，只寫 `for s in samples` 會讓它們在 CSV 裡完全消失）。撞名時遞增後綴 + `open(..., "x")` 雙保險，不靜默覆蓋。
- **離開原點的移動不能用 `_wait_axis_stop()`**：量測的起點必然在限位開關上（CLAUDE.md 既有記載「座標 0 幾乎就落在限位開關上」），而**開關有實體作用寬度**——2026-08-21 實機量測 X 軸：POS=100 時 `SB2=2` 仍被壓著，POS=150 才解除。離開 offset 小於這個寬度時軸仍壓在出發側限位上，`_wait_axis_stop()` 會依既有語意（只有 Driving 續輪、其他狀態一律 return False）判成「移動失敗/撞限位」。改用 `_wait_axis_stop_leaving_limit(axis_no, leaving_side, start_pos, min_travel, ...)`：只容忍**出發那一側**的限位，行進方向那一側仍是真失敗，且**必須同時滿足位移判準**（`travelled >= offset - 1`）。位移判準不是可選的——`query_status()` 只要回報 limit 就代表 Driving 已清除，所以「壓在出發側限位 + 非 Driving」有兩種成因：走完了只是沒脫離開關、或 `GO` 才剛送出 bit6 尚未 assert（軸一步都沒動）。只判狀態會把後者判成到位，接著量出一筆殘差≈0 的**假資料**，比大聲失敗危險得多。這個函式只給量測用，`_wait_axis_stop()` 本體一個字都沒改。
- **未脫離開關的組合會被標記**：`left_switch`（逐筆）／`offset_below_switch`（組合層級）進 CSV 與 note，GUI 該列用 `CLR_WARN`。這類數據有效但**與其他 offset 不可直接比較**（軸從未離開開關作用區，`GO ORG` 沒有從外側重新掃過感測器邊緣，量的不是同一個量），而三個預設 offset 全部預勾時，使用者拿到的 CSV 外觀完全看不出這個差別。同理 `on_sensor`（逐筆，復歸後是否停在原點/限位感測器上）／`homed_off_sensor`（組合層級）。`on_sensor` **只在「殘差已超出失控門檻」這個已知異常的分支上**拿來決定要不要歸零，不可當成一般路徑的歸零閘門——有些 ORG 樣式會在找到感測器後退出作用區停下，那時 `on_sensor` 是 False 但復歸完全正常。
- **失控保護門檻不是漂移判定**：`runaway_threshold = max(0.25 * offset, 30.0)`（變數名刻意叫 runaway 不叫 drift，避免下一個人從變數名推回錯誤結論）。真正的漂移判定在事後統計（`drift_rate`／`σ(diff)`）。殘差 ≈ offset 時另給一段文案——那是「軸根本沒回來」的簽名，真正的累積漂移是小量逐輪累加，不會一次就落在 offset 附近。
- **2026-08-21 實機驗證結果**（COM2，三軸 × offset 200/1000/3000 × 10 輪 = 90 次復歸，全數成功、無漂移、`on_sensor` 全 True）：

  | 軸 \ offset | 200 | 1000 | 3000 |
  |---|---|---|---|
  | X | range 2, median 2 | range 1, median −1 | range 3, median 4 |
  | Y | range 2, median −1 | range 1, median 1 | range 1, median −1 |
  | Z | range 2, median 1 | range 2, median −1 | range 1, median −0.5 |

  **結論：重現性（peak-to-peak）1～3 pulse，無累積漂移，且不隨離開距離變化。** 這是 2026-08-05 那組三輪手動量測答不出來的部分（三輪無法分辨「固定偏移」與「每輪累積漂移」）。這組數值與上方 2026-08-05 手動量測表（X≈0～1、Y≈+7～8、Z≈−6～−8）**不可直接比較**——中間 DATA1 微步距改過，pulse 的物理尺度已經不同。

  修正競態前的量測（Y 出現 median=83）是**框架產物**：基準復歸提前返回、`POS 0` 寫在飛行途中所致，修正後同一軸收斂到 −2。**這正好示範了 median 這一欄對基準復歸正確性的敏感度**——range／σ／`drift_rate` 都是同框架內的差分量，框架偏移會整體抵消，只有 median 會被污染。CLAUDE.md 既有的統計設計（headline 用 peak-to-peak `range`、明載「median 是系統性偏移不是重現性」）因此是對的。
- **實機的 MEMSW0 曾經是錯的，而且會被設定檔靜默還原回去。** 2026-08-21 實測：Z 軸 `MEMSW0=1` 時 `GO ORG` **完全沒有作用**（從 POS=−1000 送復歸，軸一步都沒動，狀態回 `Stop`）；改成 CLAUDE.md 記載的 `2` 之後正常復歸（移動 41266 pulse 到 CCW 端並停在硬體限位上）。當時控制器上的值是 X=2、**Y=2、Z=1**，與本檔記載的 X=2、Y=1、Z=2 相比 Y/Z 對調。**`recordings/controller_config.json` 存的就是那組疑似錯誤的值**，而 MEMSW 是 RAM-only、斷電後全歸 0——依 `restore_controller_config()` 的規則（設定檔有非 0 值、控制器現在是 0 → 寫回），**控制器每次斷電重連，程式都會主動把錯的樣式寫回去**。在控制器端改好 MEMSW0 之後，必須按 GUI 的「儲存控制器設定」重新 capture，否則會被靜默還原。這比 MEMSW0 本身錯更難察覺（實測中就發生過一次：改好 Z=2，控制器斷電重開後又變回 1）。
- **GUI 旗標串接**：`_org_repeat_running`（Event）已加進 `_update_stat_ui` 的按鈕鎖定判斷（本檔〈第三批修正〉要求「任何新增的『作業進行中』狀態都必須同步加進這個判斷」）；`_org_repeat_stop_btn` **不在 `_drive_buttons` 裡**（比照 `_scan_stop_btn`，避免作業進行中最需要停止時被整批 disable 鎖死）；`_do_stop()`／`_on_escape()`／`_toggle_connect()` 斷線分支／`_on_close()` 四處都會 set `_org_repeat_stop_event`。
