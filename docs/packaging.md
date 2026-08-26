# 執行期目錄、啟動流程與打包

> 本文件自 CLAUDE.md 拆出（2026-08-26），目的是縮小每次對話的固定載入量。
> **內容未經刪減**，動到對應功能前請完整讀過本檔。

## 執行期目錄與啟動流程（打包相關，改動前先讀）

- **三個資料目錄以「程式所在位置」為基準，不是 CWD。** `_app_dir()` 在打包後（`sys.frozen`）用 `sys.executable` 的目錄，否則用 `__file__` 的目錄。以前是 `Path("logs")` 這種相對路徑，直接跑 .py 看不出問題，但打包成 exe 後從開始功能表啟動（CWD 可能是 `C:\Windows\System32`）就會把教點與行程存到那裡。
- **`init_runtime()` 由 `main()` 呼叫，不在 import 時執行。** 建目錄與 `logging.FileHandler` 以前寫在模組層級，也就是在任何 GUI 之前；目錄不可寫時例外會在「還沒有視窗可以顯示錯誤」的階段拋出——windowed exe 的症狀就是**雙擊之後什麼都沒發生**。現在改為回傳 `(ok, err)`，`main()` 先開一個 withdrawn 的 root，失敗就用 `messagebox` 說明。
- ⚠ 因此 **import `main_ai` 不會建立任何目錄或 log 檔**。測試腳本要用 `RECORDING_DIR` 時自己指到暫存目錄即可，不必擔心污染。
- **`StreamHandler` 只在 `sys.stderr is not None` 時才加。** PyInstaller `--windowed` 會把 stdout/stderr 設成 `None`，而 `StreamHandler()` 預設綁 stderr——少了這道檢查，每一筆 log 的 `emit()` 都會踩 `AttributeError` 再被 logging 內部吞掉。
- `log_filename` 在 `init_runtime()` 失敗或未呼叫時是 `None`，`_on_close()` 會據此跳過歷程匯出。

🔴 **2026-08-17 已實際打包驗證過一次**（`venv/Scripts/pyinstaller.exe --onedir --windowed --name DS102 main_ai.py`）：build 乾淨完成（含 matplotlib TkAgg backend 自動偵測），產出約 152MB；雙擊產生的 `DS102.exe` 能正常啟動、存活、於自己目錄下建出 `logs/`/`recordings/`/`data/`（驗證了 `_app_dir()` 的 `sys.frozen` 分支），matplotlib 中文字型（Microsoft JhengHei）在打包環境下也能正確解析。**沒有實測連硬體**（GPIB／序列埠），那部分仍待驗證。

打包時另外要注意：
- 用 `--onedir` 而非 `--onefile`。這台機器的 SentinelOne 有前科（見 [DRIVER_ISSUE_REPORT.md](DRIVER_ISSUE_REPORT.md)），而未簽章的 onefile exe 自解壓縮到 temp 的行為特徵跟 packer 一樣，是典型的誤判目標；onedir 也省掉每次啟動的解壓時間。
- 別把 `ds102 (2).pdf`（4.4MB）與兩個驅動資料夾（6MB）`--add-data` 進去，執行期完全用不到。
- 全檔沒有動態 import（無 `importlib` / `__import__` / `exec`），hidden-import 風險低——若之後把 `DS102Controller` 拆成獨立模組（〈main_ai.py 架構〉（見 [CLAUDE.md](../CLAUDE.md)）），只要新模組也維持靜態 `from x import y`（同目錄 sibling import，跟現有 `fiber_scanner.py`／`meter_GPIB.py` 的匯入方式一樣），PyInstaller 的預設分析會自動收進去，不需要額外宣告 hidden-import；真正該留意的是「有沒有新增動態 import」，不是模組數量變多本身。
- 沒有單一實例保護：兩個 exe 同時跑會搶同一個 COM 埠。
- ⚠ **打包會把 DEBUG 等級的 log 全寫進檔案**（含 matplotlib 首次建圖時的 `findfont` 字型掃描，單次啟動就能灌出數十萬行、逾 700KB），不是打包引入的問題（開發模式跑 `.py` 也一樣），但在只看 `logs/` 目錄大小時容易誤判成「這支程式在跑迴圈」。
