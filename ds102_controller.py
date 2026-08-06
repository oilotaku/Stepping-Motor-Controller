# =============================================================================
# DS102/DS112 步進馬達控制器 — 圖形化版本
# 依據 main.py 指令格式完整整合
#
# 依賴套件：pyserial  (pip install pyserial)
# GUI 框架：tkinter（Python 3 內建，無需額外安裝）
#
# 功能清單：
#   1. 多軸控制（X/Y/Z/U/V/W，依實際連線軸數自動啟用）
#   2. 移動單位切換（pulse / um / mm）
#   3. 座標輸入控制（絕對 / 相對 / 連續點動 / 原點返回）
#   4. 多組 Teaching Point（支援大範圍數值與小數輸入）
#   5. 行程錄製 / 重播（每步可自訂延遲）
#   6. 緊急停止
#   7. 詳細 LOG（所有頁面常駐顯示）
#   8. 連線驗證（*IDN? 確認為 DS102/DS112）
# =============================================================================

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import serial
import serial.tools.list_ports
import threading
import time
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

# =============================================================================
# 目錄建立與 LOG 系統初始化
# =============================================================================
LOG_DIR       = Path("logs")
RECORDING_DIR = Path("recordings")
LOG_DIR.mkdir(exist_ok=True)
RECORDING_DIR.mkdir(exist_ok=True)

# 檔案 LOG（詳細 DEBUG 等級），Terminal 只顯示 INFO 以上
log_filename = LOG_DIR / f"ds102_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
_file_handler   = logging.FileHandler(log_filename, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_stream_handler = logging.StreamHandler()
_stream_handler.setLevel(logging.INFO)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[_file_handler, _stream_handler],
)
logger = logging.getLogger("DS102")

# =============================================================================
# 常數定義
# =============================================================================
AXES     = ["X", "Y", "Z", "U", "V", "W"]
AXIS_NO  = {"X": "1", "Y": "2", "Z": "3", "U": "4", "V": "5", "W": "6"}

# 驅動模式編號（對應 main.py 的 mode 變數）
MODE_CONTINUE = 0   # 連續點動
MODE_STEP     = 1   # 步進
MODE_ORIGIN   = 2   # 原點返回

# 移動單位代碼（對應 DS102 UNIT 指令：0=pulse, 1=um, 2=mm）
UNIT_CODE = {"pulse": "0", "um": "1", "mm": "2"}

# 原點模式清單（對應 MEMSW0 設定）
ORG_MODES = [f"ORG {i}" for i in range(13)]

# 顏色主題
CLR_BG      = "#F4F3F0"
CLR_CARD    = "#FFFFFF"
CLR_BORDER  = "#DEDBD3"
CLR_ACCENT  = "#1D9E75"
CLR_DANGER  = "#D93025"
CLR_INFO    = "#1A73E8"
CLR_WARN    = "#F9AB00"
CLR_TEXT    = "#1F1F1E"
CLR_MUTED   = "#80807A"
CLR_LOG_BG  = "#1B1B1B"

# =============================================================================
# 後端控制器（負責所有串列通訊，與 GUI 完全分離）
# =============================================================================
class DS102Controller:
    """
    DS102/DS112 控制器核心類別。
    所有對馬達的指令均透過此類別發送，GUI 只需呼叫公開方法。
    指令格式完全依照 main.py 範本。
    """

    def __init__(self):
        self.ser: Optional[serial.Serial] = None
        self.port      = ""
        self.baudrate  = 38400
        self.connected = False      # 是否成功連線且驗證為 DS102/DS112
        self.sim_mode  = False      # 模擬模式（無實體硬體）
        self.ems_active = False     # 緊急停止狀態

        # 當前選取軸號（字串 "1"~"6"，對應 main.py 的 axisNo）
        self.axis_no   = "1"
        # 當前驅動模式（0=連續, 1=步進, 2=原點）
        self.drive_mode = MODE_CONTINUE
        # 各軸最後已知位置（模擬模式下自行維護）
        self.positions: Dict[str, float] = {ax: 0.0 for ax in AXES}
        # 韌體版本字串
        self.firmware  = ""
        # 可用軸數（連線後由 CONTA? 查詢）
        self.axis_count = 0

        # Teaching Points 儲存（名稱 → {positions, ts}）
        self.saved_points: Dict[str, dict] = {}
        # 動作歷史（供 LOG 顯示與行程錄製使用）
        self.action_history: List[dict]    = []
        # 行程錄製緩衝
        self.recording         = False
        self.recorded_steps: List[dict]    = []
        self._recording_name   = ""
        # 已儲存行程列表（in-memory）
        self.recordings: List[dict]        = []

        # GUI LOG 回調（由 GUI 設定，每次有新 LOG 時呼叫）
        self._log_cb = None

    # -------------------------------------------------------------------------
    # LOG 系統
    # -------------------------------------------------------------------------
    def set_log_callback(self, cb):
        """設定 GUI LOG 回調函式"""
        self._log_cb = cb

    def _log(self, level: str, msg: str, tx: str = "", rx: str = ""):
        """
        統一 LOG 記錄入口。
        level: INFO / DEBUG / WARN / ERROR / CMD
        tx: 發送的原始指令（字串）
        rx: 收到的回應（字串）
        """
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        entry = {"ts": ts, "level": level, "msg": msg, "tx": tx, "rx": rx}
        self.action_history.append(entry)

        # 行程錄製中，同步記錄步驟（僅記錄實際發送指令的動作）
        if self.recording and tx:
            self.recorded_steps.append(entry)

        # 呼叫 GUI 回調（在 UI thread 更新顯示）
        if self._log_cb:
            self._log_cb(entry)

        # 寫入檔案 LOG
        log_line = f"[{tx}] → [{rx}] {msg}" if tx else msg
        if level == "ERROR":
            logger.error(log_line)
        elif level == "WARN":
            logger.warning(log_line)
        elif level == "DEBUG":
            logger.debug(log_line)
        else:
            logger.info(log_line)

    # -------------------------------------------------------------------------
    # 串列通訊底層
    # -------------------------------------------------------------------------
    def _serial_write(self, cmd: str):
        """
        只發送，不等待回應。
        對應 main.py 的 serial_write()。
        指令結尾自動加上 \r。
        """
        raw = (cmd + "\r").encode("utf-8")
        if self.sim_mode:
            # 模擬模式：解析指令更新內部狀態
            self._sim_parse(cmd)
            self._log("CMD", f"[模擬] {cmd}", tx=cmd, rx="(sim)")
            return
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(raw)
                self._log("CMD", cmd, tx=cmd)
            except Exception as e:
                self._log("ERROR", f"寫入失敗: {e}", tx=cmd)

    def _serial_write_read(self, cmd: str) -> str:
        """
        發送指令並等待回應，回傳解碼後的字串。
        對應 main.py 的 serial_write_read()。
        """
        raw = (cmd + "\r").encode("utf-8")
        if self.sim_mode:
            resp = self._sim_query(cmd)
            self._log("CMD", f"[模擬] {cmd} → {resp}", tx=cmd, rx=resp)
            return resp
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(raw)
                self._log("DEBUG", f"TX: {cmd}", tx=cmd)
                time.sleep(0.1)
                data = self.ser.read_until(b"\r")
                resp = data.decode("utf-8", errors="ignore").strip()
                self._log("DEBUG", f"RX: {resp}", rx=resp)
                return resp
            except Exception as e:
                self._log("ERROR", f"讀寫失敗: {e}", tx=cmd)
                return ""
        return ""

    # -------------------------------------------------------------------------
    # 模擬模式內部解析（無硬體時測試用）
    # -------------------------------------------------------------------------
    def _sim_parse(self, cmd: str):
        """解析發送指令，更新模擬位置（僅供模擬模式使用）"""
        import re
        # 絕對移動：AXI1:PULS 1000:GO ABS → 更新目前軸位置
        m = re.search(r":PULS\s+([\d.\-]+):GO\s+ABS", cmd)
        if m:
            ax = self._axis_no_to_name(self.axis_no)
            if ax:
                self.positions[ax] = float(m.group(1))
        # 相對移動（步進 CW/CCW）
        m = re.search(r":PULS\s+([\d.]+):GO\s+(CW|CCW)\b", cmd)
        if m and "ABS" not in cmd:
            ax = self._axis_no_to_name(self.axis_no)
            if ax:
                d = float(m.group(1))
                self.positions[ax] += d if m.group(2) == "CW" else -d
        # POS 設定
        m = re.search(r":POS\s+([\d.\-]+)", cmd)
        if m:
            ax = self._axis_no_to_name(self.axis_no)
            if ax:
                self.positions[ax] = float(m.group(1))

    def _sim_query(self, cmd: str) -> str:
        """模擬查詢回應"""
        if "*IDN?"    in cmd: return "SURUGA,DS102,1.0"
        if "DS102VER?" in cmd: return "Ver.1.0.0 (Sim)"
        if "CONTA?"   in cmd: return "2"
        if ":SB3?"    in cmd: return "1"       # 軸可選取
        if ":SB1?"    in cmd: return "0"       # 停止中
        if ":POS?"    in cmd:
            ax = self._axis_no_to_name(self.axis_no)
            return str(int(self.positions.get(ax, 0))) if ax else "0"
        return "0"

    def _axis_no_to_name(self, no: str) -> Optional[str]:
        """將軸號碼字串("1"~"6")轉換為軸名稱("X"~"W")"""
        mapping = {"1":"X","2":"Y","3":"Z","4":"U","5":"V","6":"W"}
        return mapping.get(no)

    # -------------------------------------------------------------------------
    # 連線管理
    # -------------------------------------------------------------------------
    def connect(self, port: str, baudrate: int = 38400) -> tuple[bool, str]:
        """
        開啟 COM port 並驗證連線。
        回傳 (成功?, 訊息字串)。
        驗證流程與 main.py comm_port_open() 完全一致：
          1. 開啟 Serial
          2. 發送 *IDN?，確認回應包含 "SURUGA,DS1"
          3. 查詢韌體版本 DS102VER?
          4. 查詢軸數 CONTA? 並設定各軸 UNIT/SELSP
        """
        # 若已開啟則先關閉
        if self.ser and self.ser.is_open:
            self.ser.close()

        try:
            self.ser = serial.Serial(port, baudrate, timeout=2)
        except serial.SerialException as e:
            msg = f"COM port 開啟失敗: {e}"
            self._log("ERROR", msg)
            return False, msg

        # --- 驗證是否為 DS102/DS112 ---
        r = self._serial_write_read("*IDN?")
        if "SURUGA,DS1" not in str(r):
            # 回應不符合，關閉連線並回報錯誤
            self.ser.close()
            msg = f"{port} 收到非預期回應：{r!r}\n請確認連接的是 DS102/DS112 控制器。"
            self._log("ERROR", msg)
            return False, msg

        # --- 查詢韌體版本 ---
        self.firmware = self._serial_write_read("DS102VER?")
        self._log("INFO", f"韌體版本: {self.firmware}")

        # --- 查詢軸數並初始化 UNIT / SELSP ---
        conta = self._serial_write_read("CONTA?")
        try:
            self.axis_count = int(conta)
        except ValueError:
            self.axis_count = 2
            self._log("WARN", f"CONTA? 回應異常 ({conta!r})，預設 2 軸")

        # 依照 main.py：為每個軸設定 UNIT 0（pulse）與 SELSP 0（速度表 0）
        for ax_no in range(self.axis_count):
            self._serial_write(f"AXI{ax_no + 1}:UNIT {UNIT_CODE['pulse']}:SELSP 0")
            time.sleep(0.1)

        self.port      = port
        self.baudrate  = baudrate
        self.connected = True
        self.sim_mode  = False
        msg = f"成功連線至 {port}（{self.axis_count} 軸，韌體 {self.firmware}）"
        self._log("INFO", msg)
        return True, msg

    def connect_sim(self):
        """進入模擬模式（無實體硬體）"""
        self.connected  = True
        self.sim_mode   = True
        self.firmware   = "Simulator"
        self.axis_count = 6
        self._log("INFO", "模擬模式啟動，所有指令將在本機模擬執行")

    def disconnect(self):
        """中斷連線"""
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.connected = False
        self.sim_mode  = False
        self._log("INFO", "已中斷連線")

    # -------------------------------------------------------------------------
    # 單位換算
    # -------------------------------------------------------------------------
    def set_axis_unit(self, axis_no: str, unit: str):
        """
        切換指定軸的移動單位。
        對應指令：AXI{n}:UNIT {code}
        unit: "pulse" / "um" / "mm"
        """
        code = UNIT_CODE.get(unit, "0")
        self._serial_write(f"AXI{axis_no}:UNIT {code}")
        self._log("INFO", f"軸 {axis_no} 單位切換為 {unit}")

    # -------------------------------------------------------------------------
    # 驅動指令（完全依照 main.py move_stage() 格式）
    # -------------------------------------------------------------------------
    def move_continue(self, axis_no: str, direction: str,
                      l_speed: str, f_speed: str, rate: str, s_rate: str):
        """
        連續點動（長按不放）。
        指令格式：AXI{n}:L0 {l}:R0 {r}:S0 {s}:F0 {f}:GO CWJ  （或 CCWJ）
        對應 main.py mode==0 的 CW/CCW 分支。
        """
        dir_str = "CWJ" if direction == "CW" else "CCWJ"
        cmd = (f"AXI{axis_no}:L0 {l_speed}:R0 {rate}:S0 {s_rate}"
               f":F0 {f_speed}:GO {dir_str}")
        self._serial_write(cmd)
        self._log("INFO", f"連續點動 軸{axis_no} {direction}", tx=cmd)

    def move_step(self, axis_no: str, direction: str, pulses: str,
                  l_speed: str, f_speed: str, rate: str, s_rate: str):
        """
        步進移動。
        指令格式：AXI{n}:L0 {l}:R0 {r}:S0 {s}:F0 {f}:PULS {p}:GO CW  （或 CCW）
        對應 main.py mode==1。
        """
        cmd = (f"AXI{axis_no}:L0 {l_speed}:R0 {rate}:S0 {s_rate}"
               f":F0 {f_speed}:PULS {pulses}:GO {direction}")
        self._serial_write(cmd)
        self._log("INFO", f"步進 軸{axis_no} {direction} {pulses}", tx=cmd)

    def move_origin(self, axis_no: str, org_type: int,
                    l_speed: str, f_speed: str, rate: str, s_rate: str):
        """
        原點返回。
        指令格式：
          AXI{n}:MEMSW0 {type}  （設定原點種類）
          AXI{n}:L0 {l}:R0 {r}:S0 {s}:F0 {f}:GO ORG
        對應 main.py mode==2。
        """
        self._serial_write(f"AXI{axis_no}:MEMSW0 {org_type}")
        time.sleep(0.1)
        cmd = (f"AXI{axis_no}:L0 {l_speed}:R0 {rate}:S0 {s_rate}"
               f":F0 {f_speed}:GO ORG")
        self._serial_write(cmd)
        self._log("INFO", f"原點返回 軸{axis_no} ORG{org_type}", tx=cmd)

    def stop(self):
        """
        停止所有軸。
        指令格式：STOP 0
        對應 main.py stop_button_click()。
        """
        cmd = "STOP 0"
        self._serial_write(cmd)
        self._log("INFO", "停止所有軸", tx=cmd)

    def emergency_stop(self):
        """緊急停止：直接對串列埠寫入 STOP 0，不走正常發送流程"""
        self.ems_active = True
        raw = ("STOP 0\r").encode("utf-8")
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(raw)
            except Exception:
                pass
        # 模擬模式同樣記錄
        self._log("ERROR", "🚨 緊急停止！", tx="STOP 0")

    def release_ems(self):
        """解除緊急停止狀態"""
        self.ems_active = False
        self._log("INFO", "緊急停止已解除")

    # -------------------------------------------------------------------------
    # 狀態查詢（對應 main.py update_status()）
    # -------------------------------------------------------------------------
    def query_status(self, axis_no: str) -> tuple[str, str]:
        """
        查詢指定軸的狀態與當前位置。
        回傳 (狀態字串, 位置字串)。
        使用 SB3? / SB1? / SB2? / POS? 指令，與 main.py 完全一致。
        """
        # 先確認軸可選取（SB3 bit0）
        sb3 = self._serial_write_read(f"AXI{axis_no}:SB3?")
        try:
            sb3_val = int(sb3)
        except (ValueError, TypeError):
            return "通訊錯誤", ""

        if not (sb3_val & 0x01):
            return "軸無法選取", ""

        # 查詢運動狀態（SB1）
        sb1 = self._serial_write_read(f"AXI{axis_no}:SB1?")
        try:
            sb1_val = int(sb1)
        except (ValueError, TypeError):
            return "通訊錯誤", ""

        if sb1_val & 0x40:
            status = "Driving"
        elif sb1_val & 0x10:
            status = "Detect origin"
        elif sb1_val & 0x06:
            # 偵測到 Limit，進一步查 SB2
            sb2 = self._serial_write_read(f"AXI{axis_no}:SB2?")
            try:
                sb2_val = int(sb2)
            except (ValueError, TypeError):
                sb2_val = 0
            if sb2_val & 0x03 == 0x03:
                status = "Stage not connected"
            elif sb2_val & 0x01:
                status = "Detect CW limit"
            elif sb2_val & 0x02:
                status = "Detect CCW limit"
            elif sb2_val & 0x04:
                status = "Detect CW SW limit"
            elif sb2_val & 0x08:
                status = "Detect CCW SW limit"
            else:
                status = "Limit"
        else:
            status = "Stop"

        # 查詢當前位置
        pos = self._serial_write_read(f"AXI{axis_no}:POS?")
        ax_name = self._axis_no_to_name(axis_no)
        if ax_name and pos:
            try:
                self.positions[ax_name] = float(pos)
            except ValueError:
                pass

        return status, pos

    def set_position(self, axis_no: str, value: str):
        """
        設定當前位置（座標重設）。
        指令格式：AXI{n}:POS {value}
        對應 main.py position_button_click()。
        """
        cmd = f"AXI{axis_no}:POS {value}"
        self._serial_write(cmd)
        self._log("INFO", f"軸{axis_no} 位置設為 {value}", tx=cmd)

    # -------------------------------------------------------------------------
    # Teaching Points
    # -------------------------------------------------------------------------
    def save_point(self, name: str, positions: Dict[str, float]):
        """儲存一組 Teaching Point"""
        self.saved_points[name] = {
            "positions": positions,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        self._log("INFO", f"Teaching Point [{name}] 已儲存: {positions}")
        self._persist_points()

    def delete_point(self, name: str):
        """刪除 Teaching Point"""
        self.saved_points.pop(name, None)
        self._log("INFO", f"Teaching Point [{name}] 已刪除")
        self._persist_points()

    def _persist_points(self):
        """將 Teaching Points 寫入 JSON 檔案"""
        p = RECORDING_DIR / "teaching_points.json"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(self.saved_points, f, ensure_ascii=False, indent=2)

    def load_points(self):
        """從 JSON 檔案載入 Teaching Points"""
        p = RECORDING_DIR / "teaching_points.json"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                self.saved_points = json.load(f)
            self._log("INFO", f"載入 {len(self.saved_points)} 個 Teaching Points")

    # -------------------------------------------------------------------------
    # 行程錄製
    # -------------------------------------------------------------------------
    def start_recording(self, name: str = ""):
        """開始錄製行程"""
        self.recording       = True
        self.recorded_steps  = []
        self._recording_name = name or f"rec_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self._log("INFO", f"開始錄製行程: {self._recording_name}")

    def stop_recording(self) -> dict:
        """停止錄製並儲存行程"""
        self.recording = False
        rec = {
            "name":    self._recording_name,
            "created": datetime.now().isoformat(timespec="seconds"),
            "steps":   list(self.recorded_steps),
            "count":   len(self.recorded_steps),
        }
        self.recordings.append(rec)
        path = RECORDING_DIR / f"{self._recording_name}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        self._log("INFO", f"行程 [{rec['name']}] 已儲存，{rec['count']} 步")
        return rec

    def play_recording(self, rec: dict, repeat: int = 1,
                       stop_event: Optional[threading.Event] = None,
                       progress_cb=None):
        """
        重播行程。
        rec: 行程字典（含 steps 清單，每步含 delay_ms）
        repeat: 重複次數
        stop_event: 停止信號
        progress_cb: 進度回調 (current_step, total_steps)
        """
        steps = rec.get("steps", [])
        total = len(steps) * repeat
        done  = 0
        self._log("INFO", f"開始重播 [{rec['name']}] × {repeat}，共 {total} 步")

        for _ in range(repeat):
            for step in steps:
                if stop_event and stop_event.is_set():
                    self._log("WARN", "重播已停止")
                    return
                # 發送指令
                tx = step.get("tx", "")
                if tx:
                    self._serial_write(tx)
                # 等待每步自訂延遲
                delay_ms = step.get("delay_ms", 200)
                time.sleep(delay_ms / 1000.0)
                done += 1
                if progress_cb:
                    progress_cb(done, total)

        self._log("INFO", f"行程 [{rec['name']}] 重播完成")

    def load_recordings_from_disk(self):
        """從磁碟載入所有已儲存的行程"""
        for p in sorted(RECORDING_DIR.glob("*.json")):
            if p.name == "teaching_points.json":
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    rec = json.load(f)
                # 避免重複載入
                if not any(r.get("name") == rec.get("name") for r in self.recordings):
                    self.recordings.append(rec)
            except Exception as e:
                self._log("WARN", f"無法載入行程 {p.name}: {e}")

    def export_log(self, path: str):
        """將動作歷史匯出為純文字 LOG 檔案"""
        with open(path, "w", encoding="utf-8") as f:
            for h in self.action_history:
                tx_part = f" TX=[{h['tx']}]" if h.get("tx") else ""
                rx_part = f" RX=[{h['rx']}]" if h.get("rx") else ""
                f.write(f"[{h['ts']}] [{h['level']}]{tx_part}{rx_part} {h['msg']}\n")
        self._log("INFO", f"LOG 已匯出至: {path}")


# =============================================================================
# 常駐狀態列元件（顯示當前座標與移動單位，嵌入每個頁面底部）
# =============================================================================
class StatusBar(tk.Frame):
    """
    每個分頁底部的常駐狀態列。
    顯示：選取軸、當前座標（全軸）、移動單位、最新 LOG 訊息。
    """

    def __init__(self, parent, ctrl: DS102Controller, **kwargs):
        super().__init__(parent, bg=CLR_BORDER, **kwargs)
        self.ctrl  = ctrl
        self._unit = tk.StringVar(value="pulse")  # 此 StatusBar 的單位顯示

        # 一列顯示各軸座標
        coord_frame = tk.Frame(self, bg=CLR_CARD)
        coord_frame.pack(fill="x", padx=1, pady=(1, 0))

        self._coord_labels: Dict[str, tk.Label] = {}
        for ax in AXES:
            cell = tk.Frame(coord_frame, bg=CLR_CARD)
            cell.pack(side="left", padx=6, pady=2)
            tk.Label(cell, text=f"{ax}:", bg=CLR_CARD, fg=CLR_MUTED,
                     font=("Segoe UI", 8, "bold")).pack(side="left")
            lbl = tk.Label(cell, text="0", bg=CLR_CARD, fg=CLR_TEXT,
                           font=("Consolas", 10, "bold"), width=9, anchor="e")
            lbl.pack(side="left")
            self._coord_labels[ax] = lbl

        # 單位顯示
        unit_frame = tk.Frame(coord_frame, bg=CLR_CARD)
        unit_frame.pack(side="right", padx=6)
        tk.Label(unit_frame, text="單位:", bg=CLR_CARD, fg=CLR_MUTED,
                 font=("Segoe UI", 8)).pack(side="left")
        self._unit_lbl = tk.Label(unit_frame, textvariable=self._unit,
                                   bg=CLR_CARD, fg=CLR_ACCENT,
                                   font=("Segoe UI", 9, "bold"), width=6)
        self._unit_lbl.pack(side="left")

        # 最新 LOG 訊息列
        log_frame = tk.Frame(self, bg="#E8E7E2")
        log_frame.pack(fill="x", padx=1, pady=(0, 1))
        self._log_var = tk.StringVar(value="就緒")
        tk.Label(log_frame, textvariable=self._log_var, bg="#E8E7E2",
                 fg=CLR_MUTED, font=("Segoe UI", 8), anchor="w").pack(
            fill="x", padx=6, pady=1)

    def update_coords(self):
        """刷新所有軸座標顯示（由外部定期呼叫）"""
        for ax, lbl in self._coord_labels.items():
            pos = self.ctrl.positions.get(ax, 0.0)
            lbl.config(text=f"{pos:,.2f}")

    def update_unit(self, unit: str):
        """更新單位顯示"""
        self._unit.set(unit)

    def update_log(self, msg: str):
        """更新最新 LOG 訊息"""
        # 截斷過長的訊息
        self._log_var.set(msg[:100] if len(msg) > 100 else msg)


# =============================================================================
# 主 GUI 應用程式
# =============================================================================
class DS102GUI:

    def __init__(self, root: tk.Tk):
        self.root  = root
        self.ctrl  = DS102Controller()
        self.ctrl.set_log_callback(self._on_log_entry)

        # 全域移動單位（pulse / um / mm）
        self._unit_var    = tk.StringVar(value="pulse")
        # 全域選取軸（字串 "1"~"6"）
        self._axis_no_var = tk.StringVar(value="1")
        # 停止重播信號
        self._stop_playback = threading.Event()
        # 點動狀態
        self._jog_active    = False
        # 所有 StatusBar 實例（每個分頁一個）
        self._status_bars: List[StatusBar] = []
        # LOG 分頁中的文字框參考
        self._log_text: Optional[tk.Text] = None
        # 所有分頁的軸選取按鈕組清單（每次 _build_axis_selector 呼叫後 append）
        # 使用清單存多組，確保每個分頁的按鈕都能同步更新顏色
        self._all_axis_btn_groups: List[Dict[str, tk.Button]] = []
        self._all_unit_btn_groups: List[Dict[str, tk.Button]] = []

        self._build_window()
        self._build_top_bar()
        self._build_notebook()

        # 載入已儲存的資料
        self.ctrl.load_points()
        self.ctrl.load_recordings_from_disk()
        self._refresh_points()
        self._refresh_recordings()

        # 啟動定時器：每 200ms 刷新座標顯示
        self._start_poller()

    # =========================================================================
    # 視窗骨架
    # =========================================================================
    def _build_window(self):
        """設定主視窗屬性與 ttk 樣式"""
        self.root.title("DS102 / DS112 步進馬達控制器 v2.0")
        self.root.configure(bg=CLR_BG)
        self.root.minsize(980, 700)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        s = ttk.Style()
        s.theme_use("clam")
        # 通用樣式
        s.configure(".",          background=CLR_BG, foreground=CLR_TEXT,
                     font=("Segoe UI", 10))
        s.configure("TFrame",     background=CLR_BG)
        s.configure("TLabel",     background=CLR_BG, foreground=CLR_TEXT)
        s.configure("TEntry",     fieldbackground=CLR_CARD, foreground=CLR_TEXT,
                     borderwidth=1, relief="solid")
        s.configure("TCombobox",  fieldbackground=CLR_CARD, foreground=CLR_TEXT)
        # Notebook
        s.configure("TNotebook",      background=CLR_BG, borderwidth=0)
        s.configure("TNotebook.Tab",  padding=[14, 7], font=("Segoe UI", 10))
        s.map("TNotebook.Tab",
              background=[("selected", CLR_CARD), ("!selected", CLR_BG)])
        # 按鈕樣式
        for name, bg, fg, abg in [
            ("Accent", CLR_ACCENT,  "white", "#138A5F"),
            ("Danger", CLR_DANGER,  "white", "#B52C22"),
            ("Info",   CLR_INFO,    "white", "#1557B0"),
            ("Warn",   CLR_WARN,    "white", "#C88000"),
            ("Flat",   CLR_BORDER,  CLR_TEXT, "#CCCAC3"),
        ]:
            s.configure(f"{name}.TButton", background=bg, foreground=fg,
                         font=("Segoe UI", 10, "bold"), padding=[10, 5])
            s.map(f"{name}.TButton", background=[("active", abg)])
        # Treeview
        s.configure("Treeview", background=CLR_CARD,
                     fieldbackground=CLR_CARD, foreground=CLR_TEXT, rowheight=26)
        s.configure("Treeview.Heading", background=CLR_BG, foreground=CLR_MUTED,
                     font=("Segoe UI", 9))

    def _build_top_bar(self):
        """建立頂部工具列（標題、連線設定、EMS 按鈕）"""
        top = tk.Frame(self.root, bg=CLR_CARD,
                       highlightbackground=CLR_BORDER, highlightthickness=1)
        top.pack(fill="x")

        # ── 左側：標題 ──
        left = tk.Frame(top, bg=CLR_CARD)
        left.pack(side="left", padx=16, pady=8)
        tk.Label(left, text="DS102 / DS112  馬達控制器",
                 bg=CLR_CARD, fg=CLR_TEXT,
                 font=("Segoe UI", 13, "bold")).pack(anchor="w")
        self._fw_var = tk.StringVar(value="（未連線）")
        tk.Label(left, textvariable=self._fw_var, bg=CLR_CARD,
                 fg=CLR_MUTED, font=("Segoe UI", 9)).pack(anchor="w")

        # ── 右側：EMS + 連線狀態 ──
        right = tk.Frame(top, bg=CLR_CARD)
        right.pack(side="right", padx=16, pady=8)

        self._ems_btn = tk.Button(
            right, text="⛔  緊急停止", bg=CLR_DANGER, fg="white",
            font=("Segoe UI", 11, "bold"), relief="flat",
            padx=14, pady=6, cursor="hand2", command=self._toggle_ems)
        self._ems_btn.pack(side="right", padx=(10, 0))

        conn_f = tk.Frame(right, bg=CLR_CARD)
        conn_f.pack(side="right")
        self._conn_dot = tk.Canvas(conn_f, width=10, height=10,
                                   bg=CLR_CARD, highlightthickness=0)
        self._conn_dot.pack(side="left", padx=(0, 4))
        self._conn_dot_id = self._conn_dot.create_oval(1, 1, 9, 9,
                                                        fill=CLR_DANGER, outline="")
        self._conn_lbl = tk.Label(conn_f, text="未連線", bg=CLR_CARD,
                                   fg=CLR_MUTED, font=("Segoe UI", 10))
        self._conn_lbl.pack(side="left")

        # ── 中間：連線設定 ──
        conn_row = tk.Frame(top, bg=CLR_CARD)
        conn_row.pack(side="left", padx=20, pady=8)

        # COM Port 下拉（自動掃描可用 Port）
        tk.Label(conn_row, text="Port:", bg=CLR_CARD, fg=CLR_MUTED,
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="e", padx=(0, 4))
        self._port_var = tk.StringVar()
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self._port_cb = ttk.Combobox(conn_row, textvariable=self._port_var,
                                      values=ports, width=12, state="readonly")
        if ports:
            self._port_var.set(ports[0])
        self._port_cb.grid(row=0, column=1, padx=(0, 6))

        # Baud Rate 下拉
        tk.Label(conn_row, text="Baud:", bg=CLR_CARD, fg=CLR_MUTED,
                 font=("Segoe UI", 9)).grid(row=0, column=2, sticky="e", padx=(0, 4))
        self._baud_var = tk.StringVar(value="38400")
        ttk.Combobox(conn_row, textvariable=self._baud_var,
                     values=["38400", "19200", "9600", "4800"],
                     width=8, state="readonly").grid(row=0, column=3, padx=(0, 8))

        # 重新掃描按鈕
        tk.Button(conn_row, text="↻", bg=CLR_CARD, fg=CLR_MUTED,
                  relief="flat", font=("Segoe UI", 11), cursor="hand2",
                  command=self._scan_ports).grid(row=0, column=4, padx=(0, 4))

        # 連線 / 中斷按鈕
        self._conn_btn = tk.Button(
            conn_row, text="連線", bg=CLR_ACCENT, fg="white",
            font=("Segoe UI", 10, "bold"), relief="flat",
            padx=10, pady=4, cursor="hand2", command=self._toggle_connect)
        self._conn_btn.grid(row=0, column=5, padx=(0, 4))

        # 模擬模式按鈕
        tk.Button(conn_row, text="模擬模式", bg=CLR_INFO, fg="white",
                  font=("Segoe UI", 10, "bold"), relief="flat",
                  padx=10, pady=4, cursor="hand2",
                  command=self._start_sim).grid(row=0, column=6)

    # =========================================================================
    # Notebook 分頁
    # =========================================================================
    def _build_notebook(self):
        """建立分頁容器"""
        self._nb = ttk.Notebook(self.root)
        self._nb.pack(fill="both", expand=True)

        tabs = [
            ("📊 儀表板",   self._build_tab_dashboard),
            ("🎮 移動控制", self._build_tab_control),
            ("📍 Teaching", self._build_tab_points),
            ("🔴 行程錄製", self._build_tab_recording),
            ("📋 LOG",      self._build_tab_log),
        ]
        for name, builder in tabs:
            frame = ttk.Frame(self._nb)
            self._nb.add(frame, text=f"  {name}  ")
            builder(frame)

    # -------------------------------------------------------------------------
    # 輔助：帶滾動條的容器
    # -------------------------------------------------------------------------
    def _scrollable(self, parent) -> tk.Frame:
        """建立可垂直捲動的 Frame 容器"""
        canvas = tk.Canvas(parent, bg=CLR_BG, highlightthickness=0)
        scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        frame  = tk.Frame(canvas, bg=CLR_BG)
        frame.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=frame, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # 滑鼠滾輪：綁定到 canvas 本身（避免影響其他元件）
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        return frame

    def _card_frame(self, parent, title: str = "",
                    pady=(6, 6)) -> tk.Frame:
        """建立帶標題的卡片 Frame"""
        outer = tk.Frame(parent, bg=CLR_BG)
        outer.pack(fill="x", padx=12, pady=pady)
        inner = tk.Frame(outer, bg=CLR_CARD,
                         highlightbackground=CLR_BORDER, highlightthickness=1)
        inner.pack(fill="x")
        if title:
            tk.Label(inner, text=title, bg=CLR_CARD, fg=CLR_MUTED,
                     font=("Segoe UI", 8, "bold")).pack(
                anchor="w", padx=12, pady=(6, 0))
        return inner

    def _add_status_bar(self, parent) -> StatusBar:
        """在指定父容器底部加入常駐狀態列"""
        sb = StatusBar(parent, self.ctrl)
        sb.pack(side="bottom", fill="x")
        self._status_bars.append(sb)
        return sb

    # =========================================================================
    # 軸選取工具列（各移動控制分頁頂部共用）
    # =========================================================================
    def _build_axis_selector(self, parent) -> None:
        """
        建立軸選取按鈕列與移動單位切換。
        每個分頁各自建立一組按鈕，並統一加入
        _all_axis_btn_groups / _all_unit_btn_groups 清單，
        確保 _select_axis / _select_unit 呼叫時可同步更新所有分頁的按鈕顏色。
        """
        row = tk.Frame(parent, bg=CLR_CARD,
                       highlightbackground=CLR_BORDER, highlightthickness=1)
        row.pack(fill="x", padx=0, pady=0)

        # 軸選取標籤
        tk.Label(row, text="軸選取:", bg=CLR_CARD, fg=CLR_MUTED,
                 font=("Segoe UI", 9)).pack(side="left", padx=(12, 6), pady=6)

        # 取得當前選取軸名稱，用於初始化高亮顏色
        current_ax_name = next(
            (a for a, n in AXIS_NO.items() if n == self.ctrl.axis_no), "X")

        # 此分頁專屬的軸按鈕字典
        axis_btns: Dict[str, tk.Button] = {}
        for ax in AXES:
            no = AXIS_NO[ax]
            b  = tk.Button(
                row, text=ax, width=3, relief="flat",
                font=("Segoe UI", 10, "bold"), cursor="hand2",
                bg=CLR_ACCENT if ax == current_ax_name else CLR_BORDER,
                fg="white"    if ax == current_ax_name else CLR_TEXT,
                command=lambda a=ax, n=no: self._select_axis(a, n))
            b.pack(side="left", padx=2, pady=4)
            axis_btns[ax] = b
        # 加入全域清單，讓 _select_axis 可一次更新所有分頁的按鈕顏色
        self._all_axis_btn_groups.append(axis_btns)

        # 分隔線
        ttk.Separator(row, orient="vertical").pack(side="left", fill="y",
                                                    padx=10, pady=4)

        # 移動單位標籤
        tk.Label(row, text="移動單位:", bg=CLR_CARD, fg=CLR_MUTED,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 6))

        # 此分頁專屬的單位按鈕字典
        current_unit = self._unit_var.get()
        unit_btns: Dict[str, tk.Button] = {}
        for unit in ["pulse", "um", "mm"]:
            b = tk.Button(
                row, text=unit, width=6, relief="flat",
                font=("Segoe UI", 9), cursor="hand2",
                bg=CLR_ACCENT if unit == current_unit else CLR_BORDER,
                fg="white"    if unit == current_unit else CLR_TEXT,
                command=lambda u=unit: self._select_unit(u))
            b.pack(side="left", padx=2, pady=4)
            unit_btns[unit] = b
        # 加入全域清單，讓 _select_unit 可一次更新所有分頁的按鈕顏色
        self._all_unit_btn_groups.append(unit_btns)

    def _select_axis(self, ax_name: str, ax_no: str):
        """
        切換選取軸，更新 controller 狀態，並同步更新
        所有分頁（_all_axis_btn_groups）的軸按鈕高亮顏色。
        """
        self.ctrl.axis_no = ax_no
        self._axis_no_var.set(ax_no)
        # 遍歷所有分頁的軸按鈕組，一次更新顏色
        for btn_group in self._all_axis_btn_groups:
            for a, b in btn_group.items():
                b.config(bg=CLR_ACCENT if a == ax_name else CLR_BORDER,
                         fg="white"    if a == ax_name else CLR_TEXT)
        self.ctrl._log("INFO", f"選取軸 {ax_name} (軸號 {ax_no})")
        self._query_and_refresh()

    def _select_unit(self, unit: str):
        """
        切換移動單位，傳送 UNIT 指令，並同步更新
        所有分頁（_all_unit_btn_groups）的單位按鈕高亮顏色。
        """
        self._unit_var.set(unit)
        # 對所有已啟用軸傳送 UNIT 指令
        if self.ctrl.connected:
            for i in range(self.ctrl.axis_count):
                self.ctrl.set_axis_unit(str(i + 1), unit)
        # 遍歷所有分頁的單位按鈕組，一次更新顏色
        for btn_group in self._all_unit_btn_groups:
            for u, b in btn_group.items():
                b.config(bg=CLR_ACCENT if u == unit else CLR_BORDER,
                         fg="white"    if u == unit else CLR_TEXT)
        for sb in self._status_bars:
            sb.update_unit(unit)
        self.ctrl._log("INFO", f"移動單位切換為 {unit}")

    # =========================================================================
    # TAB：儀表板
    # =========================================================================
    def _build_tab_dashboard(self, parent):
        """儀表板：顯示各軸位置、速度設定、系統狀態"""
        # 常駐狀態列（底部）
        sb = self._add_status_bar(parent)
        self._dash_status = sb

        scr = self._scrollable(parent)

        # ── 系統狀態卡 ──
        stat_card = self._card_frame(scr, "系統狀態")
        stat_row  = tk.Frame(stat_card, bg=CLR_CARD)
        stat_row.pack(fill="x", padx=12, pady=8)

        self._stat_vars: Dict[str, tk.StringVar] = {}
        for i, (key, lbl, default) in enumerate([
            ("conn",  "連線狀態", "未連線"),
            ("mode",  "驅動模式", "—"),
            ("ems",   "EMS 狀態", "正常"),
            ("axes",  "可用軸數", "—"),
        ]):
            cell = tk.Frame(stat_row, bg="#F0EEE8")
            cell.grid(row=0, column=i, padx=6, sticky="ew")
            stat_row.columnconfigure(i, weight=1)
            tk.Label(cell, text=lbl, bg="#F0EEE8", fg=CLR_MUTED,
                     font=("Segoe UI", 8)).pack(pady=(6, 0))
            v = tk.StringVar(value=default)
            self._stat_vars[key] = v
            tk.Label(cell, textvariable=v, bg="#F0EEE8", fg=CLR_TEXT,
                     font=("Segoe UI", 13, "bold")).pack(pady=(0, 6))

        # ── 各軸位置卡 ──
        pos_card = self._card_frame(scr, "各軸即時位置")
        pos_grid = tk.Frame(pos_card, bg=CLR_CARD)
        pos_grid.pack(fill="x", padx=12, pady=8)

        self._dash_pos_vars: Dict[str, tk.StringVar] = {}
        self._dash_status_vars: Dict[str, tk.StringVar] = {}

        for i, ax in enumerate(AXES):
            col = i % 3
            row = i // 3
            cell = tk.Frame(pos_grid, bg="#F0EEE8")
            cell.grid(row=row, column=col, padx=6, pady=4, sticky="ew")
            pos_grid.columnconfigure(col, weight=1)

            tk.Label(cell, text=f"{ax} 軸", bg="#F0EEE8", fg=CLR_MUTED,
                     font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8, pady=(6, 0))

            pv = tk.StringVar(value="0")
            self._dash_pos_vars[ax] = pv
            tk.Label(cell, textvariable=pv, bg="#F0EEE8", fg=CLR_TEXT,
                     font=("Consolas", 18, "bold")).pack(anchor="w", padx=8)

            sv = tk.StringVar(value="—")
            self._dash_status_vars[ax] = sv
            tk.Label(cell, textvariable=sv, bg="#F0EEE8", fg=CLR_MUTED,
                     font=("Segoe UI", 8)).pack(anchor="w", padx=8, pady=(0, 6))

        # ── 速度設定卡 ──
        spd_card = self._card_frame(scr, "速度設定（全局）")
        spd_f    = tk.Frame(spd_card, bg=CLR_CARD)
        spd_f.pack(fill="x", padx=12, pady=8)

        # 速度欄位：對應 main.py 的 txtLSpeed/txtRate/txtSRate/txtSpeed
        self._spd_entries: Dict[str, tk.StringVar] = {}
        speed_fields = [
            ("l_speed", "Start-up Speed (L)",    "100",  "pps"),
            ("rate",    "Accel/Decel Rate (R)",   "100",  "ms"),
            ("s_rate",  "S-curve Rate (S)",        "100",  "%"),
            ("f_speed", "Driving Speed (F)",       "1000", "pps"),
        ]
        for r, (key, label, default, unit) in enumerate(speed_fields):
            tk.Label(spd_f, text=label, bg=CLR_CARD, fg=CLR_MUTED,
                     font=("Segoe UI", 9), width=22, anchor="w"
                     ).grid(row=r, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=default)
            self._spd_entries[key] = v
            ttk.Entry(spd_f, textvariable=v, width=12).grid(
                row=r, column=1, sticky="w", padx=8, pady=3)
            tk.Label(spd_f, text=unit, bg=CLR_CARD, fg=CLR_MUTED,
                     font=("Segoe UI", 9)).grid(row=r, column=2, sticky="w")

    # =========================================================================
    # TAB：移動控制（單軸點動 + 步進 + 原點）
    # =========================================================================
    def _build_tab_control(self, parent):
        """移動控制分頁：含軸選取、驅動模式、點動/步進/原點返回"""
        # 常駐狀態列
        sb = self._add_status_bar(parent)

        # 軸選取 + 單位切換工具列
        self._build_axis_selector(parent)

        scr = self._scrollable(parent)

        # ── 驅動模式選擇 ──
        mode_card = self._card_frame(scr, "驅動模式")
        mode_f    = tk.Frame(mode_card, bg=CLR_CARD)
        mode_f.pack(fill="x", padx=12, pady=8)

        self._drive_mode_var = tk.IntVar(value=MODE_CONTINUE)
        modes = [
            (MODE_CONTINUE, "連續點動 (Continue)", None),
            (MODE_STEP,     "步進 (Step)",         "step_dist"),
            (MODE_ORIGIN,   "原點返回 (Origin)",   "org_mode"),
        ]

        # 步進距離輸入框（Mode==Step 時顯示）
        self._step_dist_var = tk.StringVar(value="1000")
        # 原點模式下拉（Mode==Origin 時顯示）
        self._org_mode_var  = tk.StringVar(value="ORG 0")

        for mode_val, mode_lbl, extra_key in modes:
            row_f = tk.Frame(mode_f, bg=CLR_CARD)
            row_f.pack(fill="x", pady=2)
            tk.Radiobutton(
                row_f, text=mode_lbl, variable=self._drive_mode_var,
                value=mode_val, bg=CLR_CARD, fg=CLR_TEXT,
                font=("Segoe UI", 10), activebackground=CLR_CARD,
                command=self._on_mode_change, width=20, anchor="w"
            ).pack(side="left")

            if mode_val == MODE_STEP:
                # 步進距離輸入
                ttk.Entry(row_f, textvariable=self._step_dist_var,
                          width=12).pack(side="left", padx=4)
                tk.Label(row_f, text="（單位依右上角選擇）",
                         bg=CLR_CARD, fg=CLR_MUTED,
                         font=("Segoe UI", 8)).pack(side="left")
            elif mode_val == MODE_ORIGIN:
                # 原點模式下拉
                ttk.Combobox(row_f, textvariable=self._org_mode_var,
                             values=ORG_MODES, width=10,
                             state="readonly").pack(side="left", padx=4)

        # ── 點動控制（只有兩個按鈕：+ CW / - CCW）──
        jog_card = self._card_frame(scr, "驅動按鈕（長按=連續，點擊=步進/原點）")
        jog_f    = tk.Frame(jog_card, bg=CLR_CARD)
        jog_f.pack(pady=12)

        # 顯示當前選取軸
        ax_disp = tk.Frame(jog_f, bg=CLR_CARD)
        ax_disp.pack(pady=(0, 10))
        tk.Label(ax_disp, text="當前軸:", bg=CLR_CARD,
                 fg=CLR_MUTED, font=("Segoe UI", 9)).pack(side="left")
        tk.Label(ax_disp, textvariable=self._axis_no_var,
                 bg=CLR_CARD, fg=CLR_ACCENT,
                 font=("Segoe UI", 14, "bold")).pack(side="left", padx=4)

        # 狀態顯示
        self._ctrl_status_var = tk.StringVar(value="Stop")
        tk.Label(jog_f, textvariable=self._ctrl_status_var,
                 bg=CLR_CARD, fg=CLR_TEXT,
                 font=("Segoe UI", 10)).pack(pady=(0, 8))

        # 位置顯示
        pos_row = tk.Frame(jog_f, bg=CLR_CARD)
        pos_row.pack(pady=(0, 10))
        tk.Label(pos_row, text="Position:", bg=CLR_CARD,
                 fg=CLR_MUTED, font=("Segoe UI", 9)).pack(side="left")
        self._ctrl_pos_var = tk.StringVar(value="0")
        pos_entry = ttk.Entry(pos_row, textvariable=self._ctrl_pos_var, width=14)
        pos_entry.pack(side="left", padx=6)
        ttk.Button(pos_row, text="Set Position",
                   command=self._do_set_position).pack(side="left", padx=4)

        # CCW（−）按鈕
        btn_row = tk.Frame(jog_f, bg=CLR_CARD)
        btn_row.pack()

        self._ccw_btn = tk.Button(
            btn_row, text="−  CCW", width=12, height=3,
            bg=CLR_INFO, fg="white",
            font=("Segoe UI", 12, "bold"), relief="flat", cursor="hand2")
        self._ccw_btn.bind("<ButtonPress-1>",   self._on_ccw_press)
        self._ccw_btn.bind("<ButtonRelease-1>", self._on_ccw_release)
        self._ccw_btn.pack(side="left", padx=12)

        # Stop 按鈕
        self._stop_btn = tk.Button(
            btn_row, text="■  Stop", width=10, height=3,
            bg=CLR_DANGER, fg="white",
            font=("Segoe UI", 11, "bold"), relief="flat", cursor="hand2",
            command=self._do_stop)
        self._stop_btn.pack(side="left", padx=4)

        # CW（+）按鈕
        self._cw_btn = tk.Button(
            btn_row, text="CW  ＋", width=12, height=3,
            bg=CLR_ACCENT, fg="white",
            font=("Segoe UI", 12, "bold"), relief="flat", cursor="hand2")
        self._cw_btn.bind("<ButtonPress-1>",   self._on_cw_press)
        self._cw_btn.bind("<ButtonRelease-1>", self._on_cw_release)
        self._cw_btn.pack(side="left", padx=12)

        tk.Label(jog_card, text="連續點動：長按不放；步進：點一下自動執行；原點：點一下觸發",
                 bg=CLR_CARD, fg=CLR_MUTED,
                 font=("Segoe UI", 8)).pack(pady=(0, 8))

    # ── 驅動按鈕事件 ─────────────────────────────────────────
    def _get_speed_args(self) -> tuple[str, str, str, str]:
        """取得速度設定值（l_speed, f_speed, rate, s_rate）"""
        return (
            self._spd_entries["l_speed"].get(),
            self._spd_entries["f_speed"].get(),
            self._spd_entries["rate"].get(),
            self._spd_entries["s_rate"].get(),
        )

    def _on_mode_change(self):
        """驅動模式切換回調：更新按鈕文字"""
        mode = self._drive_mode_var.get()
        if mode == MODE_ORIGIN:
            self._ccw_btn.config(text="Origin")
            self._cw_btn.config(text="Origin")
        else:
            self._ccw_btn.config(text="−  CCW")
            self._cw_btn.config(text="CW  ＋")

    def _on_ccw_press(self, event):
        """CCW 按鈕按下"""
        if self.ctrl.ems_active:
            return
        mode = self._drive_mode_var.get()
        l, f, r, s = self._get_speed_args()
        ax = self.ctrl.axis_no

        if mode == MODE_CONTINUE:
            # 連續點動：按下即開始，放開停止
            self.ctrl.move_continue(ax, "CCW", l, f, r, s)
            self._poll_status_loop()
        elif mode == MODE_STEP:
            # 步進：只執行一次
            self.ctrl.move_step(ax, "CCW", self._step_dist_var.get(), l, f, r, s)
            self._poll_status_loop()
        elif mode == MODE_ORIGIN:
            # 原點返回
            org_idx = ORG_MODES.index(self._org_mode_var.get()) if self._org_mode_var.get() in ORG_MODES else 0
            self.ctrl.move_origin(ax, org_idx, l, f, r, s)
            self._poll_status_loop()

    def _on_ccw_release(self, event):
        """CCW 按鈕放開：連續模式才停止"""
        if self._drive_mode_var.get() == MODE_CONTINUE:
            self.ctrl.stop()

    def _on_cw_press(self, event):
        """CW 按鈕按下"""
        if self.ctrl.ems_active:
            return
        mode = self._drive_mode_var.get()
        l, f, r, s = self._get_speed_args()
        ax = self.ctrl.axis_no

        if mode == MODE_CONTINUE:
            self.ctrl.move_continue(ax, "CW", l, f, r, s)
            self._poll_status_loop()
        elif mode == MODE_STEP:
            self.ctrl.move_step(ax, "CW", self._step_dist_var.get(), l, f, r, s)
            self._poll_status_loop()
        elif mode == MODE_ORIGIN:
            org_idx = ORG_MODES.index(self._org_mode_var.get()) if self._org_mode_var.get() in ORG_MODES else 0
            self.ctrl.move_origin(ax, org_idx, l, f, r, s)
            self._poll_status_loop()

    def _on_cw_release(self, event):
        """CW 按鈕放開：連續模式才停止"""
        if self._drive_mode_var.get() == MODE_CONTINUE:
            self.ctrl.stop()

    def _do_stop(self):
        """Stop 按鈕：停止所有軸"""
        self.ctrl.stop()

    def _do_set_position(self):
        """Set Position 按鈕：重設當前位置"""
        val = self._ctrl_pos_var.get().strip()
        if not val:
            return
        self.ctrl.set_position(self.ctrl.axis_no, val)

    def _poll_status_loop(self):
        """
        啟動狀態輪詢迴圈（驅動中每 100ms 查詢一次），
        直到狀態為 Stop 為止。
        對應 main.py 的 get_status() → update_status() 迴圈。
        """
        def _check():
            status, pos = self.ctrl.query_status(self.ctrl.axis_no)
            self.root.after(0, lambda: self._ctrl_status_var.set(status))
            if pos:
                self.root.after(0, lambda: self._ctrl_pos_var.set(pos))
            if status == "Driving":
                self.root.after(100, _check)

        threading.Thread(target=_check, daemon=True).start()

    def _query_and_refresh(self):
        """非同步查詢當前軸狀態與位置（用於軸切換時更新顯示）"""
        def _q():
            status, pos = self.ctrl.query_status(self.ctrl.axis_no)
            self.root.after(0, lambda: self._ctrl_status_var.set(status))
            if pos:
                self.root.after(0, lambda: self._ctrl_pos_var.set(pos))

        threading.Thread(target=_q, daemon=True).start()

    # =========================================================================
    # TAB：Teaching Points
    # =========================================================================
    def _build_tab_points(self, parent):
        """Teaching Points 分頁：儲存 / 前往 / 編輯座標點"""
        self._add_status_bar(parent)
        self._build_axis_selector(parent)

        scr = self._scrollable(parent)

        # ── 新增 / 編輯座標點 ──
        add_card = self._card_frame(scr, "新增 / 編輯 Teaching Point")
        af = tk.Frame(add_card, bg=CLR_CARD)
        af.pack(fill="x", padx=12, pady=8)

        tk.Label(af, text="點名稱:", bg=CLR_CARD, fg=CLR_MUTED,
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w", pady=3)
        self._pt_name_var = tk.StringVar()
        ttk.Entry(af, textvariable=self._pt_name_var,
                  width=20).grid(row=0, column=1, sticky="w", padx=8, pady=3)

        # 各軸座標輸入
        # 支援整數（-99999999 ~ 99999999）或小數（-9.9999999 ~ 9.9999999）
        self._pt_pos_vars: Dict[str, tk.StringVar] = {}
        tk.Label(af, text="座標（pulse: -99999999~99999999  |  um/mm: -9.9999999~9.9999999）:",
                 bg=CLR_CARD, fg=CLR_MUTED,
                 font=("Segoe UI", 8)).grid(row=1, column=0, columnspan=4,
                                              sticky="w", pady=(4, 2))
        for i, ax in enumerate(AXES):
            col_base = (i % 3) * 2
            r_base   = 2 + i // 3
            tk.Label(af, text=f"{ax}:", bg=CLR_CARD, fg=CLR_MUTED,
                     font=("Segoe UI", 9), width=3, anchor="e"
                     ).grid(row=r_base, column=col_base, sticky="e", padx=(8, 2), pady=2)
            v = tk.StringVar(value="0")
            self._pt_pos_vars[ax] = v
            # 驗證輸入範圍
            vcmd = (self.root.register(self._validate_coord), "%P")
            ttk.Entry(af, textvariable=v, width=14,
                      validate="key", validatecommand=vcmd
                      ).grid(row=r_base, column=col_base + 1, sticky="w", padx=4, pady=2)

        btn_row = tk.Frame(add_card, bg=CLR_CARD)
        btn_row.pack(padx=12, pady=(0, 8))
        ttk.Button(btn_row, text="儲存 Teaching Point", style="Accent.TButton",
                   command=self._do_save_point).pack(side="left", padx=4)
        ttk.Button(btn_row, text="填入當前位置", style="Info.TButton",
                   command=self._do_fill_current).pack(side="left", padx=4)
        ttk.Button(btn_row, text="清除", style="Flat.TButton",
                   command=lambda: [v.set("0") for v in self._pt_pos_vars.values()]
                   ).pack(side="left", padx=4)

        # ── Teaching Points 清單 ──
        list_card = self._card_frame(scr, "已儲存 Teaching Points")
        self._pts_tree = ttk.Treeview(
            list_card,
            columns=("name", "X", "Y", "Z", "ts"),
            show="headings", height=8)
        for col, w, lbl in [
            ("name", 130, "名稱"),
            ("X",     90, "X"),
            ("Y",     90, "Y"),
            ("Z",     90, "Z"),
            ("ts",   160, "儲存時間"),
        ]:
            self._pts_tree.heading(col, text=lbl)
            self._pts_tree.column(col, width=w)
        # 雙擊載入到輸入欄
        self._pts_tree.bind("<Double-1>", self._on_pt_dclick)

        scroll_y = ttk.Scrollbar(list_card, orient="vertical",
                                  command=self._pts_tree.yview)
        self._pts_tree.configure(yscrollcommand=scroll_y.set)
        self._pts_tree.pack(side="left", fill="x", expand=True, padx=12, pady=8)
        scroll_y.pack(side="right", fill="y", pady=8)

        pt_btn_row = tk.Frame(list_card, bg=CLR_CARD)
        pt_btn_row.pack(padx=12, pady=(0, 8))
        ttk.Button(pt_btn_row, text="✏ 載入編輯", style="Flat.TButton",
                   command=self._do_load_point).pack(side="left", padx=4)
        ttk.Button(pt_btn_row, text="🗑 刪除", style="Danger.TButton",
                   command=self._do_delete_point).pack(side="left", padx=4)

    def _validate_coord(self, value: str) -> bool:
        """
        座標輸入驗證：
          允許空字串、負號開頭、整數、最多7位小數的浮點數。
          整數範圍：-99999999 ~ 99999999
          小數範圍：-9.9999999 ~ 9.9999999
        """
        if value in ("", "-", ".", "-.", "+"):
            return True
        try:
            f = float(value)
            # 有小數點：限制在 ±9.9999999
            if "." in value:
                return -9.9999999 <= f <= 9.9999999
            else:
                return -99999999 <= int(value) <= 99999999
        except ValueError:
            return False

    def _do_save_point(self):
        """儲存 Teaching Point"""
        name = self._pt_name_var.get().strip()
        if not name:
            messagebox.showerror("錯誤", "請輸入點名稱")
            return
        positions = {}
        for ax, var in self._pt_pos_vars.items():
            try:
                positions[ax] = float(var.get() or "0")
            except ValueError:
                positions[ax] = 0.0
        self.ctrl.save_point(name, positions)
        self._refresh_points()
        messagebox.showinfo("完成", f"Teaching Point [{name}] 已儲存")

    def _do_fill_current(self):
        """將控制器內部已知的各軸位置填入輸入欄"""
        for ax, var in self._pt_pos_vars.items():
            var.set(str(self.ctrl.positions.get(ax, 0)))

    def _do_load_point(self):
        """將選取的 Teaching Point 載入到編輯欄位"""
        sel = self._pts_tree.selection()
        if not sel:
            return
        vals = self._pts_tree.item(sel[0])["values"]
        name = vals[0]
        pt   = self.ctrl.saved_points.get(name, {})
        self._pt_name_var.set(name)
        pos  = pt.get("positions", {})
        for ax, var in self._pt_pos_vars.items():
            var.set(str(pos.get(ax, 0)))

    def _on_pt_dclick(self, event):
        """雙擊 Teaching Point 列表 → 載入到編輯欄"""
        self._do_load_point()

    def _do_delete_point(self):
        """刪除選取的 Teaching Point"""
        sel = self._pts_tree.selection()
        if not sel:
            return
        name = self._pts_tree.item(sel[0])["values"][0]
        if messagebox.askyesno("確認", f"確定刪除 [{name}]？"):
            self.ctrl.delete_point(name)
            self._refresh_points()

    def _refresh_points(self):
        """刷新 Teaching Points 樹狀列表"""
        self._pts_tree.delete(*self._pts_tree.get_children())
        for name, data in self.ctrl.saved_points.items():
            pos = data.get("positions", {})
            self._pts_tree.insert("", "end", values=(
                name,
                f"{pos.get('X', 0):.4f}",
                f"{pos.get('Y', 0):.4f}",
                f"{pos.get('Z', 0):.4f}",
                data.get("ts", "")[:19],
            ))

    # =========================================================================
    # TAB：行程錄製
    # =========================================================================
    def _build_tab_recording(self, parent):
        """行程錄製分頁：錄製 / 編輯步驟延遲 / 重播"""
        self._add_status_bar(parent)
        scr = self._scrollable(parent)

        # ── 錄製控制 ──
        rec_card = self._card_frame(scr, "行程錄製")
        rf = tk.Frame(rec_card, bg=CLR_CARD)
        rf.pack(fill="x", padx=12, pady=8)

        tk.Label(rf, text="行程名稱:", bg=CLR_CARD, fg=CLR_MUTED,
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        self._rec_name_var   = tk.StringVar()
        self._rec_status_var = tk.StringVar(value="● 閒置")
        self._rec_count_var  = tk.StringVar(value="0 步")
        ttk.Entry(rf, textvariable=self._rec_name_var,
                  width=24).grid(row=0, column=1, sticky="w", padx=8)
        tk.Label(rf, textvariable=self._rec_status_var,
                 bg=CLR_CARD, fg=CLR_DANGER,
                 font=("Segoe UI", 9, "bold")).grid(row=0, column=2, padx=6)
        tk.Label(rf, textvariable=self._rec_count_var,
                 bg=CLR_CARD, fg=CLR_TEXT,
                 font=("Segoe UI", 9)).grid(row=0, column=3)

        rbtn = tk.Frame(rec_card, bg=CLR_CARD)
        rbtn.pack(padx=12, pady=(0, 8))
        self._rec_start_btn = ttk.Button(rbtn, text="⏺ 開始錄製",
                                          style="Danger.TButton",
                                          command=self._do_start_rec)
        self._rec_start_btn.pack(side="left", padx=4)
        self._rec_stop_btn  = ttk.Button(rbtn, text="⏹ 停止錄製",
                                          command=self._do_stop_rec,
                                          state="disabled")
        self._rec_stop_btn.pack(side="left", padx=4)

        # ── 已錄製步驟列表（可逐步設定延遲）──
        steps_card = self._card_frame(scr, "已錄製步驟（可逐步設定延遲時間）")
        tk.Label(steps_card,
                 text="雙擊延遲欄位可直接修改；每步延遲單位為毫秒（ms）",
                 bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8)
                 ).pack(anchor="w", padx=12, pady=(4, 0))

        tree_frame = tk.Frame(steps_card, bg=CLR_CARD)
        tree_frame.pack(fill="x", padx=12, pady=6)

        self._steps_tree = ttk.Treeview(
            tree_frame,
            columns=("idx", "ts", "cmd", "delay_ms"),
            show="headings", height=8)
        for col, w, lbl in [
            ("idx",      40,  "#"),
            ("ts",      100, "時間"),
            ("cmd",     280, "指令"),
            ("delay_ms", 90, "延遲 (ms)"),
        ]:
            self._steps_tree.heading(col, text=lbl)
            self._steps_tree.column(col, width=w)
        self._steps_tree.bind("<Double-1>", self._on_step_dclick)

        scroll_y2 = ttk.Scrollbar(tree_frame, orient="vertical",
                                   command=self._steps_tree.yview)
        self._steps_tree.configure(yscrollcommand=scroll_y2.set)
        self._steps_tree.pack(side="left", fill="x", expand=True)
        scroll_y2.pack(side="right", fill="y")

        # 全局延遲設定
        gd_row = tk.Frame(steps_card, bg=CLR_CARD)
        gd_row.pack(padx=12, pady=(0, 8))
        tk.Label(gd_row, text="批次設定所有步驟延遲:", bg=CLR_CARD,
                 fg=CLR_MUTED, font=("Segoe UI", 9)).pack(side="left")
        self._global_delay_var = tk.StringVar(value="200")
        ttk.Entry(gd_row, textvariable=self._global_delay_var,
                  width=8).pack(side="left", padx=6)
        tk.Label(gd_row, text="ms", bg=CLR_CARD, fg=CLR_MUTED,
                 font=("Segoe UI", 9)).pack(side="left")
        ttk.Button(gd_row, text="套用", style="Flat.TButton",
                   command=self._apply_global_delay).pack(side="left", padx=6)

        # ── 已儲存行程列表 ──
        list_card = self._card_frame(scr, "已儲存行程")
        self._rec_tree = ttk.Treeview(
            list_card,
            columns=("name", "count", "created"),
            show="headings", height=5)
        for col, w, lbl in [
            ("name",    160, "名稱"),
            ("count",    70, "步數"),
            ("created", 180, "建立時間"),
        ]:
            self._rec_tree.heading(col, text=lbl)
            self._rec_tree.column(col, width=w)
        self._rec_tree.bind("<<TreeviewSelect>>", self._on_rec_select)

        scroll_y3 = ttk.Scrollbar(list_card, orient="vertical",
                                   command=self._rec_tree.yview)
        self._rec_tree.configure(yscrollcommand=scroll_y3.set)
        self._rec_tree.pack(side="left", fill="x", expand=True, padx=12, pady=8)
        scroll_y3.pack(side="right", fill="y", pady=8)

        # ── 重播設定 ──
        play_card = self._card_frame(scr, "重播設定")
        pf = tk.Frame(play_card, bg=CLR_CARD)
        pf.pack(fill="x", padx=12, pady=8)

        tk.Label(pf, text="重複次數:", bg=CLR_CARD, fg=CLR_MUTED,
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w", pady=3)
        self._repeat_var = tk.IntVar(value=1)
        ttk.Spinbox(pf, from_=1, to=9999, textvariable=self._repeat_var,
                    width=8).grid(row=0, column=1, sticky="w", padx=8)

        self._play_progress_var = tk.StringVar(value="—")
        tk.Label(pf, textvariable=self._play_progress_var,
                 bg=CLR_CARD, fg=CLR_MUTED,
                 font=("Segoe UI", 9)).grid(row=0, column=2, padx=16)

        plbtn = tk.Frame(play_card, bg=CLR_CARD)
        plbtn.pack(padx=12, pady=(0, 8))
        ttk.Button(plbtn, text="▶ 重播行程", style="Accent.TButton",
                   command=self._do_play_rec).pack(side="left", padx=4)
        ttk.Button(plbtn, text="■ 停止重播", style="Danger.TButton",
                   command=lambda: self._stop_playback.set()
                   ).pack(side="left", padx=4)

    def _do_start_rec(self):
        """開始錄製行程"""
        name = self._rec_name_var.get().strip()
        self.ctrl.start_recording(name)
        self._rec_status_var.set("🔴 錄製中")
        self._rec_start_btn.state(["disabled"])
        self._rec_stop_btn.state(["!disabled"])
        self._rec_step_timer()

    def _rec_step_timer(self):
        """定時更新錄製步驟計數"""
        if self.ctrl.recording:
            cnt = len(self.ctrl.recorded_steps)
            self._rec_count_var.set(f"{cnt} 步")
            self.root.after(500, self._rec_step_timer)

    def _do_stop_rec(self):
        """停止錄製並儲存行程"""
        rec = self.ctrl.stop_recording()
        self._rec_status_var.set("● 閒置")
        self._rec_count_var.set("0 步")
        self._rec_start_btn.state(["!disabled"])
        self._rec_stop_btn.state(["disabled"])
        self._refresh_recordings()
        self._load_steps_to_tree(rec)

    def _on_rec_select(self, event):
        """選取行程後，載入步驟到編輯列表"""
        sel = self._rec_tree.selection()
        if not sel:
            return
        idx = self._rec_tree.index(sel[0])
        if 0 <= idx < len(self.ctrl.recordings):
            self._load_steps_to_tree(self.ctrl.recordings[idx])

    def _load_steps_to_tree(self, rec: dict):
        """將行程步驟載入步驟樹狀列表"""
        self._steps_tree.delete(*self._steps_tree.get_children())
        for i, step in enumerate(rec.get("steps", [])):
            self._steps_tree.insert("", "end", values=(
                i + 1,
                step.get("ts", ""),
                step.get("tx", step.get("msg", "")),
                step.get("delay_ms", 200),
            ))

    def _on_step_dclick(self, event):
        """雙擊步驟延遲欄位 → 彈出輸入框讓使用者修改單步延遲"""
        sel = self._steps_tree.selection()
        if not sel:
            return
        item     = sel[0]
        vals     = self._steps_tree.item(item)["values"]
        step_idx = int(vals[0]) - 1

        win = tk.Toplevel(self.root)
        win.title("修改步驟延遲")
        win.geometry("300x110")
        win.resizable(False, False)
        win.grab_set()

        tk.Label(win, text=f"步驟 #{vals[0]}  指令: {str(vals[2])[:35]}",
                 font=("Segoe UI", 9), fg=CLR_MUTED).pack(pady=(10, 0))
        delay_var = tk.StringVar(value=str(vals[3]))
        row_f = tk.Frame(win)
        row_f.pack(pady=8)
        tk.Label(row_f, text="延遲 (ms):").pack(side="left")
        ttk.Entry(row_f, textvariable=delay_var, width=10).pack(side="left", padx=6)

        def _apply():
            try:
                ms = int(delay_var.get())
                if ms < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("錯誤", "請輸入有效的正整數毫秒數", parent=win)
                return
            self._steps_tree.item(item, values=(vals[0], vals[1], vals[2], ms))
            rec_sel = self._rec_tree.selection()
            if rec_sel:
                rec_idx = self._rec_tree.index(rec_sel[0])
                if 0 <= rec_idx < len(self.ctrl.recordings):
                    steps = self.ctrl.recordings[rec_idx].get("steps", [])
                    if 0 <= step_idx < len(steps):
                        steps[step_idx]["delay_ms"] = ms
                        self.ctrl._log("INFO", f"步驟 #{step_idx+1} 延遲修改為 {ms}ms")
            win.destroy()

        ttk.Button(win, text="確認", style="Accent.TButton", command=_apply).pack()

    def _apply_global_delay(self):
        """批次設定所有步驟延遲"""
        try:
            ms = int(self._global_delay_var.get())
            if ms < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("錯誤", "請輸入有效的毫秒數")
            return
        for item in self._steps_tree.get_children():
            vals = self._steps_tree.item(item)["values"]
            self._steps_tree.item(item, values=(vals[0], vals[1], vals[2], ms))
        rec_sel = self._rec_tree.selection()
        if rec_sel:
            rec_idx = self._rec_tree.index(rec_sel[0])
            if 0 <= rec_idx < len(self.ctrl.recordings):
                for step in self.ctrl.recordings[rec_idx].get("steps", []):
                    step["delay_ms"] = ms
        self.ctrl._log("INFO", f"所有步驟延遲批次設定為 {ms}ms")

    def _do_play_rec(self):
        """非同步重播選取的行程"""
        sel = self._rec_tree.selection()
        if not sel:
            messagebox.showwarning("警告", "請先選擇行程")
            return
        idx = self._rec_tree.index(sel[0])
        if idx >= len(self.ctrl.recordings):
            return
        rec    = self.ctrl.recordings[idx]
        repeat = self._repeat_var.get()
        self._stop_playback.clear()

        def _progress(done, total):
            self.root.after(0, lambda: self._play_progress_var.set(f"{done} / {total}"))

        threading.Thread(
            target=self.ctrl.play_recording,
            args=(rec, repeat, self._stop_playback, _progress),
            daemon=True,
        ).start()

    def _refresh_recordings(self):
        """刷新已儲存行程列表"""
        self._rec_tree.delete(*self._rec_tree.get_children())
        for rec in self.ctrl.recordings:
            self._rec_tree.insert("", "end", values=(
                rec.get("name", ""),
                rec.get("count", 0),
                rec.get("created", "")[:19],
            ))

    # =========================================================================
    # TAB：LOG
    # =========================================================================
    def _build_tab_log(self, parent):
        """
        LOG 分頁：
        - 黑底 Monospace 文字框顯示所有動作記錄
        - 捲動問題修正：滾輪只在文字框上作用，不影響頁面捲動
        - 提供清除 / 匯出功能
        """
        self._add_status_bar(parent)

        toolbar = tk.Frame(parent, bg=CLR_CARD,
                            highlightbackground=CLR_BORDER, highlightthickness=1)
        toolbar.pack(fill="x")
        tk.Label(toolbar, text="動作 LOG", bg=CLR_CARD, fg=CLR_MUTED,
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=12, pady=6)
        ttk.Button(toolbar, text="清除", style="Flat.TButton",
                   command=self._clear_log).pack(side="left", padx=4, pady=4)
        ttk.Button(toolbar, text="匯出 LOG", style="Info.TButton",
                   command=self._export_log).pack(side="left", padx=4, pady=4)
        self._log_auto_scroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text="自動捲動",
                        variable=self._log_auto_scroll).pack(side="left", padx=8)

        log_frame = tk.Frame(parent, bg=CLR_LOG_BG)
        log_frame.pack(fill="both", expand=True)

        self._log_text = tk.Text(
            log_frame,
            bg=CLR_LOG_BG, fg="#D4D4D4",
            font=("Consolas", 9), relief="flat", bd=0,
            insertbackground="white",
            state="disabled",
            wrap="none",
        )
        log_scroll_y = ttk.Scrollbar(log_frame, orient="vertical",
                                      command=self._log_text.yview)
        log_scroll_x = ttk.Scrollbar(log_frame, orient="horizontal",
                                      command=self._log_text.xview)
        self._log_text.configure(
            yscrollcommand=log_scroll_y.set,
            xscrollcommand=log_scroll_x.set,
        )
        log_scroll_y.pack(side="right", fill="y")
        log_scroll_x.pack(side="bottom", fill="x")
        self._log_text.pack(fill="both", expand=True)

        # 修正捲動問題：滾輪只在 LOG 文字框本身有效，return "break" 阻止冒泡
        def _log_mw(event):
            self._log_text.yview_scroll(int(-1*(event.delta/120)), "units")
            return "break"

        self._log_text.bind("<MouseWheel>", _log_mw)
        self._log_text.bind("<Button-4>",
                            lambda e: (self._log_text.yview_scroll(-1,"units"), "break"))
        self._log_text.bind("<Button-5>",
                            lambda e: (self._log_text.yview_scroll(1,"units"), "break"))

        self._log_text.tag_config("INFO",  foreground="#4EC9B0")
        self._log_text.tag_config("CMD",   foreground="#9CDCFE")
        self._log_text.tag_config("WARN",  foreground="#CE9178")
        self._log_text.tag_config("ERROR", foreground="#F44747")
        self._log_text.tag_config("DEBUG", foreground="#6A6A6A")

    def _clear_log(self):
        """清除 LOG 顯示（不影響檔案 LOG）"""
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")

    def _export_log(self):
        """匯出 LOG 到檔案"""
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All", "*.*")],
            initialfile=f"ds102_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
        )
        if path:
            self.ctrl.export_log(path)
            messagebox.showinfo("完成", f"LOG 已匯出至:\n{path}")

    # =========================================================================
    # LOG 回調
    # =========================================================================
    def _on_log_entry(self, entry: dict):
        """LOG 回調：由 DS102Controller 在每次記錄時呼叫（可在任意執行緒）"""
        self.root.after(0, self._append_log_ui, entry)

    def _append_log_ui(self, entry: dict):
        """在 UI 執行緒中將 LOG 追加到文字框與所有 StatusBar"""
        level = entry.get("level", "INFO")
        ts    = entry.get("ts", "")
        tx    = f"[{entry['tx']}] " if entry.get("tx") else ""
        rx    = f"→ [{entry['rx']}] " if entry.get("rx") else ""
        msg   = entry.get("msg", "")
        line  = f"{ts} [{level:<5}] {tx}{rx}{msg}\n"

        if self._log_text:
            self._log_text.config(state="normal")
            self._log_text.insert("end", line, level)
            if self._log_auto_scroll.get():
                self._log_text.see("end")
            self._log_text.config(state="disabled")

        for sb in self._status_bars:
            sb.update_log(f"[{level}] {msg}")

        self._update_stat_ui()

    # =========================================================================
    # 連線 / EMS
    # =========================================================================
    def _scan_ports(self):
        """重新掃描可用的 COM Port 清單"""
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self._port_cb["values"] = ports
        if ports and not self._port_var.get():
            self._port_var.set(ports[0])

    def _toggle_connect(self):
        """連線 / 中斷按鈕切換"""
        if self.ctrl.connected:
            self.ctrl.disconnect()
            self._conn_dot.itemconfig(self._conn_dot_id, fill=CLR_DANGER)
            self._conn_lbl.config(text="未連線")
            self._conn_btn.config(text="連線", bg=CLR_ACCENT)
            self._fw_var.set("（未連線）")
            # 停用所有分頁的軸選取按鈕
            for btn_group in self._all_axis_btn_groups:
                for b in btn_group.values():
                    b.config(state="disabled")
        else:
            port = self._port_var.get()
            baud = int(self._baud_var.get())
            if not port:
                messagebox.showerror("錯誤", "請選擇 COM Port")
                return
            self._conn_btn.config(text="連線中...", state="disabled", bg=CLR_WARN)
            self.root.update()

            def _do_connect():
                ok, msg = self.ctrl.connect(port, baud)
                self.root.after(0, lambda: self._on_connect_result(ok, msg))

            threading.Thread(target=_do_connect, daemon=True).start()

    def _on_connect_result(self, ok: bool, msg: str):
        """連線結果回調（在 UI 執行緒執行）"""
        self._conn_btn.config(state="normal")
        if ok:
            self._conn_dot.itemconfig(self._conn_dot_id, fill=CLR_ACCENT)
            self._conn_lbl.config(text=f"{self.ctrl.port} @ {self.ctrl.baudrate}")
            self._conn_btn.config(text="中斷", bg=CLR_DANGER)
            self._fw_var.set(f"韌體: {self.ctrl.firmware}  |  {self.ctrl.axis_count} 軸")
            # 依實際軸數啟用 / 停用所有分頁的軸選取按鈕
            for btn_group in self._all_axis_btn_groups:
                for ax, b in btn_group.items():
                    ax_no = int(AXIS_NO[ax])
                    b.config(state="normal" if ax_no <= self.ctrl.axis_count else "disabled")
        else:
            self._conn_btn.config(text="連線", bg=CLR_ACCENT)
            messagebox.showerror("連線失敗", msg)

    def _start_sim(self):
        """啟動模擬模式"""
        self.ctrl.connect_sim()
        self._conn_dot.itemconfig(self._conn_dot_id, fill=CLR_INFO)
        self._conn_lbl.config(text="模擬模式")
        self._conn_btn.config(text="中斷", bg=CLR_DANGER)
        self._fw_var.set("模擬模式 | 6 軸")
        # 模擬模式啟用所有分頁的軸選取按鈕
        for btn_group in self._all_axis_btn_groups:
            for b in btn_group.values():
                b.config(state="normal")
        self._sim_tick()

    def _sim_tick(self):
        """模擬模式下定期產生位置浮動（測試 UI 用）"""
        import random
        if self.ctrl.sim_mode:
            for ax in AXES:
                self.ctrl.positions[ax] += random.uniform(-5, 5)
            self.root.after(800, self._sim_tick)

    def _toggle_ems(self):
        """緊急停止 / 解除切換"""
        if not self.ctrl.ems_active:
            self.ctrl.emergency_stop()
            self._ems_btn.config(text="✅ 解除緊急停止", bg="#2E7D32")
        else:
            self.ctrl.release_ems()
            self._ems_btn.config(text="⛔  緊急停止", bg=CLR_DANGER)

    # =========================================================================
    # 儀表板統計更新
    # =========================================================================
    def _update_stat_ui(self):
        """更新儀表板系統狀態欄"""
        if "conn" in self._stat_vars:
            self._stat_vars["conn"].set("已連線" if self.ctrl.connected else "未連線")
        if "mode" in self._stat_vars:
            modes = {0: "連續", 1: "步進", 2: "原點"}
            val = modes.get(self._drive_mode_var.get(), "—") if hasattr(self, "_drive_mode_var") else "—"
            self._stat_vars["mode"].set(val)
        if "ems" in self._stat_vars:
            self._stat_vars["ems"].set("⚠️ EMS" if self.ctrl.ems_active else "正常")
        if "axes" in self._stat_vars:
            self._stat_vars["axes"].set(str(self.ctrl.axis_count) if self.ctrl.connected else "—")

    # =========================================================================
    # 座標定時輪詢
    # =========================================================================
    def _start_poller(self):
        """每 300ms 更新一次所有 StatusBar 的座標顯示"""
        def _poll():
            for sb in self._status_bars:
                sb.update_coords()
            for ax, lbl in self._dash_pos_vars.items():
                pos  = self.ctrl.positions.get(ax, 0.0)
                unit = self._unit_var.get()
                lbl.set(f"{pos:,.4f}" if unit != "pulse" else f"{pos:,.0f}")
            self.root.after(300, _poll)

        self.root.after(300, _poll)

    # =========================================================================
    # 關閉視窗
    # =========================================================================
    def _on_close(self):
        """關閉視窗前：停止所有軸，中斷連線，自動匯出 LOG"""
        self._stop_playback.set()
        if self.ctrl.connected:
            self.ctrl.stop()
            self.ctrl.disconnect()
        auto_path = str(log_filename).replace(".log", "_history.txt")
        try:
            self.ctrl.export_log(auto_path)
        except Exception:
            pass
        self.root.destroy()


# =============================================================================
# 程式入口
# =============================================================================
def main():
    root = tk.Tk()
    app  = DS102GUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
