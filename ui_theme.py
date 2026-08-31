"""
UI 色票（主題）單一來源。

2026-08-31 從 main_ai.py 抽出（模組化前置工作 3，見 docs/modularization.md）。

## 為什麼要獨立成一個檔案

`CLR_*` 是**唯一真正橫跨全部七個分頁**的 UI 常數：445 處引用散在
main_ai.py 各個 `_build_tab_*`、`StatusBar`、以及所有狀態色的判斷裡。
只要它還定義在 main_ai.py，任何想從 main_ai.py 搬出去的 GUI 程式碼
（`StatusBar`、將來的各個 `*TabMixin`）就必須反過來
`from main_ai import CLR_*` —— 那正是循環相依本身。抽到這裡之後，
被搬出去的 UI 模組一律改成 `from ui_theme import ...`，
main_ai.py 不再是任何人的上游。

## 🔴 依賴方向（不可反轉）

    ds102_ctrl.py  ──►  ui_theme.py  ──►  main_ai.py（及將來的 UI 模組）

`ui_theme.py` **只**允許 import `ds102_ctrl`，而且只取 `_app_settings`
這一份已經載入好的字典。**絕對不可以 import main_ai.py 或任何 GUI
模組**，否則上面那條鏈就成環，抽出這個檔案的意義整個消失。

## 🔴 為什麼 `_app_settings` / `_app_setting_num` / `_load_app_settings` 沒有跟著搬過來

複審筆記（docs/modularization.md〈前置 3〉）原本寫的是「`_app_settings`
與 `CLR_*` 一起抽出」，但那是依 2026-08-18 之前的檔案配置寫的。實際盤點
後發現這三個名字**早在 2026-08-17 拆 ds102_ctrl.py 時就已經不在
main_ai.py**——它們現在定義在 ds102_ctrl.py，理由記在該檔第 203-212 行：
`HISTORY_MAX`（`DS102Controller` 用的常數）需要靠 `_app_setting_num()`
覆寫。

把它們搬到這裡會製造出**新的循環相依**：

    ds102_ctrl.HISTORY_MAX 需要 ui_theme._app_setting_num
    ui_theme._load_app_settings 需要 ds102_ctrl._load_json_settings + RECORDING_DIR

所以維持現狀：設定檔的**載入**留在最底層的 ds102_ctrl.py，這裡只負責把
已載入的字典**解讀成色票**。這也比較符合職責劃分——`app_settings.json`
裡不只有色票，還有 `history_max` 這種跟 UI 無關的欄位。

## 🔴 覆寫必須在 import 時完成，不可改成延遲求值

下面十個常數在模組載入當下就把 `app_settings.json` 的覆寫值算完並固定。
**不要**為了「支援執行期換主題」改成函式或 property：`DS102GUI.__init__`
期間的各個 `_build_*` 直接把這些值餵給 tkinter 元件的 `bg=` / `fg=`，
改成延遲求值只會讓求值時機分散、卻換不到任何實際的換主題能力
（tkinter 元件建好之後不會自己重讀）。
"""

# 只取一份已經載入好的設定字典。見上方〈依賴方向〉——這是本檔唯一的 import。
from ds102_ctrl import _app_settings

# =============================================================================
# 顏色主題
#
# 可由 recordings/app_settings.json 的 clr_* 欄位個別覆寫（字串型別，
# 不做色碼格式驗證——格式錯的後果跟直接改這裡打錯字一樣，會在 tkinter
# 建立元件時才報錯，與硬編碼時期的風險相同）。
#
# 刻意用 `.get(key, 預設)` 而非 ds102_ctrl._app_setting_num()：那支是給
# 數值欄位做型別轉換用的，色票是字串、沒有可轉換的目標型別。
# =============================================================================
CLR_BG = _app_settings.get("clr_bg", "#F4F3F0")
CLR_CARD = _app_settings.get("clr_card", "#FFFFFF")
CLR_BORDER = _app_settings.get("clr_border", "#DEDBD3")
CLR_ACCENT = _app_settings.get("clr_accent", "#1D9E75")
CLR_DANGER = _app_settings.get("clr_danger", "#D93025")
CLR_INFO = _app_settings.get("clr_info", "#1A73E8")
CLR_WARN = _app_settings.get("clr_warn", "#F9AB00")
CLR_TEXT = _app_settings.get("clr_text", "#1F1F1E")
CLR_MUTED = _app_settings.get("clr_muted", "#80807A")
CLR_LOG_BG = _app_settings.get("clr_log_bg", "#1B1B1B")

__all__ = [
    "CLR_BG",
    "CLR_CARD",
    "CLR_BORDER",
    "CLR_ACCENT",
    "CLR_DANGER",
    "CLR_INFO",
    "CLR_WARN",
    "CLR_TEXT",
    "CLR_MUTED",
    "CLR_LOG_BG",
]
