"""
UI 色票（主題）單一來源。2026-08-31 從 main_ai.py 抽出（模組化前置工作 3，
見 docs/modularization.md）。

## 為什麼要獨立成一個檔案

`CLR_*` 橫跨全部七個分頁，445 處引用散在 main_ai.py 各 `_build_tab_*`／
`StatusBar`／狀態色判斷裡。若仍定義在 main_ai.py，任何想搬出去的 GUI
程式碼都得反過來 `from main_ai import CLR_*`，形成循環相依。抽出後
一律改 `from ui_theme import ...`，main_ai.py 不再是任何人的上游。

## 🔴 依賴方向（不可反轉）

    ds102_ctrl.py  ──►  ui_theme.py  ──►  main_ai.py（及將來的 UI 模組）

本檔**只**允許 import `ds102_ctrl` 的 `_app_settings`（已載入好的字典），
**不可 import main_ai.py 或任何 GUI 模組**，否則上面這條鏈就成環。

## 🔴 `_app_settings` / `_app_setting_num` / `_load_app_settings` 為何沒搬過來

原規劃要跟 `CLR_*` 一起抽出，但盤點後發現這三者早在 2026-08-17 拆
ds102_ctrl.py 時就已定義在該檔（`HISTORY_MAX` 靠 `_app_setting_num()`
覆寫，見該檔 203-212 行）。搬過來會製造新的循環相依
（`ds102_ctrl.HISTORY_MAX` 需要 `ui_theme._app_setting_num`，反之
`_load_app_settings` 又需要 ds102_ctrl 的載入函式）。故設定檔**載入**
留在 ds102_ctrl.py，本檔只負責把已載入的字典**解讀成色票**——也符合
職責劃分，因為 `app_settings.json` 裡還有 `history_max` 等非 UI 欄位。

## 🔴 覆寫必須在 import 時完成，不可延遲求值

下面十個常數在模組載入當下就算完並固定。**不要**為了「支援執行期換
主題」改成函式或 property：`_build_*` 直接把這些值餵給 tkinter 的
`bg=`/`fg=`，元件建好後不會自己重讀，延遲求值換不到任何實際效果。
"""

# 本檔唯一的 import，見上方〈依賴方向〉。
from core.ds102_ctrl import _app_settings

# =============================================================================
# 顏色主題：可由 recordings/app_settings.json 的 clr_* 欄位個別覆寫（字串，
# 不做色碼格式驗證——錯了會在 tkinter 建元件時才報錯，風險同硬編碼時期）。
# 用 .get() 而非 _app_setting_num()：後者是數值型別轉換用的，色票是字串。
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
