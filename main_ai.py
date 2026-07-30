# =============================================================================
# DS102/DS112 步進馬達控制器 — 圖形化版本 v3.0
# 依據 main.py 指令格式完整整合
#
# 依賴套件：pyserial  (pip install pyserial)
# GUI 框架：tkinter（Python 3 內建，無需額外安裝）
#
# 改善項目（v3.0）：
#   1. 執行緒安全：positions / action_history 加入 threading.Lock 保護
#   2. 通訊可靠性：指令逾時保護、ACK 確認、重送機制（最多 3 次）
#   3. 行程錄製僅錄驅動指令（排除查詢類 SB?/POS?）
#   4. 到位確認：步進/原點後輪詢 SB1? 確認 Driving 旗標清除再繼續
#   5. 單位一致性：內部永遠以 pulse 儲存，顯示時依選擇換算
#   6. 軟體行程限制（Software Limit）：超限自動攔截並警告
#   7. Limit / 異常狀態自動彈窗警告並停止
#   8. 連線未建立時鎖定驅動按鈕
#   9. 速度 Profile 命名儲存與快速切換
#  10. Teaching Point 加入「移動至此點」功能，並記錄儲存當時單位
#  11. 實驗數據 CSV 匯出（時間戳 + 各軸位置）
#  12. 座標偏置（Offset）：定義工作原點與機械原點分離
#  13. EMS 解除後要求位置確認才能繼續操作
#  14. 行程重播中鎖定其他移動操作
# =============================================================================

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import serial
import serial.tools.list_ports
import threading
import time
import json
import logging
import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Tuple

# =============================================================================
# 目錄建立與 LOG 系統初始化
# =============================================================================
LOG_DIR = Path("logs")
RECORDING_DIR = Path("recordings")
DATA_DIR = Path("data")  # 實驗數據 CSV 輸出目錄
LOG_DIR.mkdir(exist_ok=True)
RECORDING_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

log_filename = LOG_DIR / f"ds102_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
_file_handler = logging.FileHandler(log_filename, encoding="utf-8")
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
AXES = ["X", "Y", "Z", "U", "V", "W"]
AXIS_NO = {"X": "1", "Y": "2", "Z": "3", "U": "4", "V": "5", "W": "6"}
NO_AXIS = {v: k for k, v in AXIS_NO.items()}  # 反向對應："1"→"X"

# 驅動模式（對應 main.py mode 變數）
MODE_CONTINUE = 0
MODE_STEP = 1
MODE_ORIGIN = 2

# 單位代碼（DS102 UNIT 指令）
UNIT_CODE = {"pulse": "0", "um": "1", "mm": "2"}

# 原點模式清單
ORG_MODES = [f"ORG {i}" for i in range(13)]

# 通訊重送次數上限
MAX_RETRY = 3
# 到位輪詢逾時（秒）
WAIT_TIMEOUT = 30.0
# 到位輪詢間隔（秒）
WAIT_INTERVAL = 0.1

# 顏色主題
CLR_BG = "#F4F3F0"
CLR_CARD = "#FFFFFF"
CLR_BORDER = "#DEDBD3"
CLR_ACCENT = "#1D9E75"
CLR_DANGER = "#D93025"
CLR_INFO = "#1A73E8"
CLR_WARN = "#F9AB00"
CLR_TEXT = "#1F1F1E"
CLR_MUTED = "#80807A"
CLR_LOG_BG = "#1B1B1B"


# =============================================================================
# 後端控制器
# =============================================================================
class DS102Controller:
    """
    DS102/DS112 控制器核心類別。
    所有串列通訊均在此集中管理，GUI 只呼叫公開方法。
    指令格式完全依照 main.py 範本。
    """

    def __init__(self):
        self.ser: Optional[serial.Serial] = None
        self.port = ""
        self.baudrate = 38400
        self.connected = False
        self.sim_mode = False
        self.ems_active = False

        # 當前選取軸號（字串"1"~"6"）
        self.axis_no = "1"
        self.drive_mode = MODE_CONTINUE

        # ── 執行緒鎖（保護共享資料，避免競爭條件）──
        self._lock = threading.Lock()

        # 各軸位置（內部永遠以 pulse 為單位儲存）
        self._positions_pulse: Dict[str, float] = {ax: 0.0 for ax in AXES}
        # 各軸座標偏置（工作原點 offset，以 pulse 為單位）
        self._offsets: Dict[str, float] = {ax: 0.0 for ax in AXES}
        # 各軸軟體行程限制（pulse，None 表示不限制）
        self.sw_limits: Dict[str, Tuple[Optional[float], Optional[float]]] = {
            ax: (None, None) for ax in AXES  # (CCW_limit, CW_limit)
        }

        self.firmware = ""
        self.axis_count = 0
        self._unit = "um"  # 當前單位（供換算顯示用）

        # Teaching Points
        self.saved_points: Dict[str, dict] = {}

        # 動作歷史（含執行緒鎖保護）
        self._history_lock = threading.Lock()
        self.action_history: List[dict] = []

        # 行程錄製（僅錄驅動指令，排除查詢類指令）
        self.recording = False
        self.recorded_steps: List[dict] = []
        self._recording_name = ""
        self.recordings: List[dict] = []

        # 實驗數據記錄（CSV）
        self._data_log: List[dict] = []
        self._data_logging = False

        # 速度 Profile
        self.speed_profiles: Dict[str, dict] = {}

        # GUI LOG 回調
        self._log_cb = None

        # 重播鎖定旗標（重播中禁止其他移動操作）
        self.playback_running = False

        # 狀態異常回調（用於 GUI 彈窗）
        self._alarm_cb = None

    # =========================================================================
    # 屬性：positions（對外公開，自動扣除 offset）
    # =========================================================================
    @property
    def positions(self) -> Dict[str, float]:
        """
        回傳各軸工作座標（= 機械位置 − offset）。
        此為對外公開的顯示用座標，內部儲存以 _positions_pulse 為準。
        """
        with self._lock:
            return {ax: self._positions_pulse[ax] - self._offsets[ax] for ax in AXES}

    def set_offset_here(self, axis_no: str):
        """將當前位置設為工作原點（offset = 目前機械位置）"""
        ax = NO_AXIS.get(axis_no)
        if ax:
            with self._lock:
                self._offsets[ax] = self._positions_pulse[ax]
            self._log("INFO", f"軸 {ax} 工作原點已設為當前位置")

    def clear_offset(self, axis_no: str):
        """清除工作原點偏置，回復機械座標"""
        ax = NO_AXIS.get(axis_no)
        if ax:
            with self._lock:
                self._offsets[ax] = 0.0
            self._log("INFO", f"軸 {ax} 偏置已清除")

    # =========================================================================
    # LOG 系統
    # =========================================================================
    def set_log_callback(self, cb):
        self._log_cb = cb

    def set_alarm_callback(self, cb):
        """設定狀態異常回調（供 GUI 顯示彈窗警告）"""
        self._alarm_cb = cb

    def _log(self, level: str, msg: str, tx: str = "", rx: str = ""):
        """
        統一 LOG 記錄入口。
        level: INFO / DEBUG / WARN / ERROR
        tx:    發送的原始串列指令（人讀描述寫在 msg）
        rx:    控制器回應字串
        """
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        entry = {"ts": ts, "level": level, "msg": msg, "tx": tx, "rx": rx}

        with self._history_lock:
            self.action_history.append(entry)

        # ── 行程錄製：只記錄驅動指令（GO / STOP / MEMSW），排除查詢 ──
        # 查詢指令特徵：以 ? 結尾，或包含 SB1/SB2/SB3/POS?/CONTA/IDN/VER
        _is_query = tx.endswith("?") or any(
            k in tx for k in ["SB1?", "SB2?", "SB3?", "POS?", "CONTA?", "IDN?", "VER?"]
        )
        if self.recording and tx and not _is_query:
            self.recorded_steps.append({**entry, "delay_ms": 200})

        if self._log_cb:
            self._log_cb(entry)

        log_line = f"TX=[{tx}] RX=[{rx}] {msg}" if tx else msg
        getattr(
            logger,
            {"ERROR": "error", "WARN": "warning", "DEBUG": "debug"}.get(level, "info"),
        )(log_line)

    # =========================================================================
    # 串列通訊底層（含重送機制與逾時保護）
    # =========================================================================
    def _serial_write(self, cmd: str):
        """
        發送指令，不等待回應。
        對應 main.py serial_write()。
        重送：最多 MAX_RETRY 次，發送前先 flush 輸入緩衝。
        """
        raw = (cmd + "\r").encode("utf-8")
        if self.sim_mode:
            self._sim_parse(cmd)
            self._log("INFO", "發送指令（模擬）", tx=cmd, rx="(sim)")
            return

        for attempt in range(1, MAX_RETRY + 1):
            if not (self.ser and self.ser.is_open):
                break
            try:
                self.ser.reset_input_buffer()  # 清除殘留回應
                self.ser.write(raw)
                self._log("INFO", f"發送指令 (嘗試{attempt})", tx=cmd)
                return
            except serial.SerialException as e:
                self._log("WARN", f"寫入失敗 (嘗試{attempt}/{MAX_RETRY}): {e}", tx=cmd)
                time.sleep(0.05 * attempt)

        self._log("ERROR", f"指令發送失敗（{MAX_RETRY} 次均失敗）", tx=cmd)

    def _serial_write_read(self, cmd: str, timeout: float = 2.0) -> str:
        """
        發送指令並等待回應，回傳解碼字串。
        對應 main.py serial_write_read()。
        使用本地 timeout 保護，避免永久阻塞。
        重送機制：最多 MAX_RETRY 次。
        """
        raw = (cmd + "\r").encode("utf-8")
        if self.sim_mode:
            resp = self._sim_query(cmd)
            self._log("DEBUG", f"查詢（模擬）", tx=cmd, rx=resp)
            return resp

        for attempt in range(1, MAX_RETRY + 1):
            if not (self.ser and self.ser.is_open):
                break
            try:
                self.ser.reset_input_buffer()
                self.ser.write(raw)
                self._log("DEBUG", f"TX: {cmd} (嘗試{attempt})", tx=cmd)
                # 使用獨立 timeout 讀取回應
                self.ser.timeout = timeout
                data = self.ser.read_until(b"\r")
                self.ser.timeout = 2.0  # 還原預設
                resp = data.decode("utf-8", errors="ignore").strip()
                if resp:
                    self._log("DEBUG", f"RX: {resp}", rx=resp)
                    return resp
                self._log("WARN", f"空回應 (嘗試{attempt}/{MAX_RETRY})", tx=cmd)
            except serial.SerialException as e:
                self._log("WARN", f"讀寫失敗 (嘗試{attempt}/{MAX_RETRY}): {e}", tx=cmd)
            time.sleep(0.05 * attempt)

        self._log("ERROR", f"查詢失敗（{MAX_RETRY} 次均無回應）", tx=cmd)
        return ""

    # =========================================================================
    # 模擬模式
    # =========================================================================
    def _sim_parse(self, cmd: str):
        """解析驅動指令並更新模擬位置"""
        m = re.search(r":PULS\s+([\d.\-]+):GO\s+ABS", cmd)
        if m:
            ax = NO_AXIS.get(self.axis_no)
            if ax:
                with self._lock:
                    self._positions_pulse[ax] = float(m.group(1))
            return
        m = re.search(r":PULS\s+([\d.]+):GO\s+(CW|CCW)\b", cmd)
        if m and "ABS" not in cmd:
            ax = NO_AXIS.get(self.axis_no)
            if ax:
                d = float(m.group(1))
                with self._lock:
                    self._positions_pulse[ax] += d if m.group(2) == "CW" else -d
            return
        m = re.search(r"AXI(\d):POS\s+([\d.\-]+)", cmd)
        if m:
            ax = NO_AXIS.get(m.group(1))
            if ax:
                with self._lock:
                    self._positions_pulse[ax] = float(m.group(2))

    def _sim_query(self, cmd: str) -> str:
        if "*IDN?" in cmd:
            return "SURUGA,DS102,1.0"
        if "DS102VER?" in cmd:
            return "Ver.1.0.0 (Sim)"
        if "CONTA?" in cmd:
            return "6"
        if ":SB3?" in cmd:
            return "1"
        if ":SB1?" in cmd:
            return "0"
        if ":POS?" in cmd:
            ax = NO_AXIS.get(self.axis_no)
            with self._lock:
                return str(int(self._positions_pulse.get(ax, 0))) if ax else "0"
        return "0"

    # =========================================================================
    # 連線管理
    # =========================================================================
    def connect(self, port: str, baudrate: int = 38400) -> Tuple[bool, str]:
        """
        開啟 COM port 並驗證為 DS102/DS112 控制器。
        流程完全依照 main.py comm_port_open()。
        """
        if self.ser and self.ser.is_open:
            self.ser.close()
        try:
            self.ser = serial.Serial(port, baudrate, timeout=2)
        except serial.SerialException as e:
            return False, f"COM port 開啟失敗: {e}"

        # 驗證 IDN
        r = self._serial_write_read("*IDN?")
        if "SURUGA,DS1" not in str(r):
            self.ser.close()
            return False, (
                f"{port} 回應非預期：{r!r}\n" f"請確認連接的是 DS102/DS112 控制器。"
            )

        # 韌體版本
        self.firmware = self._serial_write_read("DS102VER?")
        self._log("INFO", f"韌體版本: {self.firmware}")

        # 軸數
        conta = self._serial_write_read("CONTA?")
        try:
            self.axis_count = int(conta)
        except ValueError:
            self.axis_count = 2
            self._log("WARN", f"CONTA? 異常({conta!r})，預設 2 軸")

        # 初始化各軸 UNIT=pulse, SELSP=0
        for i in range(self.axis_count):
            self._serial_write(f"AXI{i+1}:UNIT {UNIT_CODE['um']}:SELSP 0")
            time.sleep(0.1)

        self.port = port
        self.baudrate = baudrate
        self.connected = True
        self.sim_mode = False
        msg = f"已連線至 {port}（{self.axis_count} 軸，韌體 {self.firmware}）"
        self._log("INFO", msg)
        return True, msg

    def connect_sim(self):
        self.connected = True
        self.sim_mode = True
        self.firmware = "Simulator"
        self.axis_count = 6
        self._log("INFO", "模擬模式啟動")

    def disconnect(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.connected = False
        self.sim_mode = False
        self._log("INFO", "已中斷連線")

    # =========================================================================
    # 單位管理
    # =========================================================================
    def set_unit(self, unit: str):
        """
        切換單位並傳送 UNIT 指令給所有已啟用軸。
        內部 _positions_pulse 不受影響，永遠以 pulse 儲存。
        """
        self._unit = unit
        if self.connected:
            for i in range(self.axis_count):
                self._serial_write(f"AXI{i+1}:UNIT {UNIT_CODE.get(unit,'0')}")
        self._log("INFO", f"移動單位切換為 {unit}")

    def pulse_to_display(self, pulse_val: float) -> float:
        """pulse 轉換為當前顯示單位的數值（供 GUI 顯示用）"""
        if self._unit == "um":
            return pulse_val  # DS102 1 pulse = 1 um（依實際系統調整）
        elif self._unit == "mm":
            return pulse_val / 1000.0
        return pulse_val  # pulse

    def display_to_pulse(self, display_val: float) -> float:
        """當前顯示單位的數值轉換回 pulse（供指令發送用）"""
        if self._unit == "um":
            return display_val
        elif self._unit == "mm":
            return display_val * 1000.0
        return display_val

    # =========================================================================
    # 軟體行程限制
    # =========================================================================
    def _check_sw_limit(self, axis_no: str, target_pulse: float) -> Tuple[bool, str]:
        """
        檢查目標位置是否超出軟體行程限制。
        回傳 (允許移動?, 原因訊息)。
        """
        ax = NO_AXIS.get(axis_no)
        if not ax:
            return True, ""
        ccw_lim, cw_lim = self.sw_limits.get(ax, (None, None))
        if ccw_lim is not None and target_pulse < ccw_lim:
            return False, (
                f"軸 {ax} 目標 {target_pulse:.1f} pulse "
                f"超出 CCW 限制 {ccw_lim:.1f} pulse"
            )
        if cw_lim is not None and target_pulse > cw_lim:
            return False, (
                f"軸 {ax} 目標 {target_pulse:.1f} pulse "
                f"超出 CW 限制 {cw_lim:.1f} pulse"
            )
        return True, ""

    # =========================================================================
    # 驅動指令（依照 main.py move_stage() 格式）
    # =========================================================================
    def move_continue(
        self,
        axis_no: str,
        direction: str,
        l_speed: str,
        f_speed: str,
        rate: str,
        s_rate: str,
    ):
        """
        連續點動（長按不放，放開後送 STOP 0）。
        格式：AXI{n}:L0 {l}:R0 {r}:S0 {s}:F0 {f}:GO CWJ / CCWJ
        """
        if self.ems_active or self.playback_running:
            return
        dir_str = "CWJ" if direction == "CW" else "CCWJ"
        cmd = (
            f"AXI{axis_no}:L0 {l_speed}:R0 {rate}"
            f":S0 {s_rate}:F0 {f_speed}:GO {dir_str}"
        )
        self._serial_write(cmd)
        self._log("INFO", f"連續點動 軸{axis_no} {direction}", tx=cmd)

    def move_step(
        self,
        axis_no: str,
        direction: str,
        amount: str,
        l_speed: str,
        f_speed: str,
        rate: str,
        s_rate: str,
        wait_done: bool = True,
    ):
        """
        步進移動（一次性）。
        格式：AXI{n}:L0 {l}:R0 {r}:S0 {s}:F0 {f}:PULS {p}:GO CW / CCW
        wait_done=True 時，發送後阻塞直到到位（輪詢 SB1? Driving 位元清除）。
        amount 單位依當前 _unit，內部換算為 pulse 後進行限制檢查。
        """
        if self.ems_active or self.playback_running:
            return
        # 換算為 pulse，進行軟體限位檢查
        try:
            amount_f = float(amount)
            pulse_amt = self.display_to_pulse(amount_f)
        except ValueError:
            self._log("ERROR", f"步進距離格式錯誤: {amount}")
            return

        ax = NO_AXIS.get(axis_no)
        if ax:
            with self._lock:
                cur = self._positions_pulse[ax]
            target = cur + (pulse_amt if direction == "CW" else -pulse_amt)
            ok, reason = self._check_sw_limit(axis_no, target)
            if not ok:
                self._log("WARN", f"軟體限位攔截: {reason}")
                if self._alarm_cb:
                    self._alarm_cb("軟體行程限制", reason)
                return

        cmd = (
            f"AXI{axis_no}:L0 {l_speed}:R0 {rate}"
            f":S0 {s_rate}:F0 {f_speed}:PULS {amount}:GO {direction}"
        )
        self._serial_write(cmd)
        self._log("INFO", f"步進 軸{axis_no} {direction} {amount} {self._unit}", tx=cmd)

        if wait_done and not self.sim_mode:
            self._wait_axis_stop(axis_no)

    def move_origin(
        self,
        axis_no: str,
        org_type: int,
        l_speed: str,
        f_speed: str,
        rate: str,
        s_rate: str,
        wait_done: bool = True,
    ):
        """
        原點返回。
        格式：AXI{n}:MEMSW0 {type} → AXI{n}:L0...:GO ORG
        wait_done=True 時等待到位。
        """
        if self.ems_active or self.playback_running:
            return
        self._serial_write(f"AXI{axis_no}:MEMSW0 {org_type}")
        time.sleep(0.1)
        cmd = f"AXI{axis_no}:L0 {l_speed}:R0 {rate}" f":S0 {s_rate}:F0 {f_speed}:GO ORG"
        self._serial_write(cmd)
        self._log("INFO", f"原點返回 軸{axis_no} ORG{org_type}", tx=cmd)
        if wait_done and not self.sim_mode:
            self._wait_axis_stop(axis_no)

    def stop(self):
        """停止所有軸（STOP 0）"""
        cmd = "STOP 0"
        self._serial_write(cmd)
        self._log("INFO", "停止所有軸", tx=cmd)

    def emergency_stop(self):
        """緊急停止：繞過所有佇列直接寫入串列埠"""
        self.ems_active = True
        self.playback_running = False
        raw = b"STOP 0\r"
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(raw)
            except Exception:
                pass
        self._log("ERROR", "🚨 緊急停止！", tx="STOP 0")

    def release_ems(self):
        """解除緊急停止（GUI 層需額外要求位置確認）"""
        self.ems_active = False
        self._log("INFO", "緊急停止已解除，請確認各軸位置後再操作")

    # =========================================================================
    # 到位等待（核心改善：確保步進完成後再繼續）
    # =========================================================================
    def _wait_axis_stop(self, axis_no: str, timeout: float = WAIT_TIMEOUT) -> bool:
        """
        阻塞等待指定軸停止（SB1 bit6 Driving 旗標清除）。
        同時偵測異常狀態（Limit）並觸發警報回調。
        回傳：True=正常停止，False=逾時或異常。
        此方法應在背景執行緒呼叫，避免凍結 UI。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ems_active:
                return False
            status, pos = self.query_status(axis_no)
            # 更新機械位置
            ax = NO_AXIS.get(axis_no)
            if ax and pos:
                try:
                    with self._lock:
                        self._positions_pulse[ax] = float(pos)
                except ValueError:
                    pass
            # 記錄數據
            if self._data_logging:
                self._record_data_point()

            if status == "Stop":
                return True
            if status == "Driving":
                time.sleep(WAIT_INTERVAL)
                continue
            # 其他狀態（Limit / 異常）→ 觸發警報
            self._log("WARN", f"軸{axis_no} 異常狀態: {status}")
            if self._alarm_cb:
                self._alarm_cb(f"軸 {NO_AXIS.get(axis_no,'?')} 異常", status)
            return False

        self._log("WARN", f"軸{axis_no} 等待到位逾時（{timeout}s）")
        return False

    # =========================================================================
    # 狀態查詢（對應 main.py update_status()）
    # =========================================================================
    def query_status(self, axis_no: str) -> Tuple[str, str]:
        """
        查詢指定軸狀態與位置。
        回傳 (狀態字串, 位置字串_pulse)。
        """
        sb3 = self._serial_write_read(f"AXI{axis_no}:SB3?")
        try:
            if not (int(sb3) & 0x01):
                return "軸無法選取", ""
        except (ValueError, TypeError):
            return "通訊錯誤", ""

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

        pos = self._serial_write_read(f"AXI{axis_no}:POS?")
        ax = NO_AXIS.get(axis_no)
        if ax and pos:
            try:
                with self._lock:
                    self._positions_pulse[ax] = float(pos)
            except ValueError:
                pass

        return status, pos

    def set_position(self, axis_no: str, value: str):
        """設定當前位置（AXI{n}:POS {val}）"""
        cmd = f"AXI{axis_no}:POS {value}"
        self._serial_write(cmd)
        # 同步更新內部值（換算為 pulse）
        ax = NO_AXIS.get(axis_no)
        if ax:
            try:
                with self._lock:
                    self._positions_pulse[ax] = self.display_to_pulse(float(value))
            except ValueError:
                pass
        self._log("INFO", f"軸{axis_no} 位置設為 {value} {self._unit}", tx=cmd)

    # =========================================================================
    # 速度 Profile 管理
    # =========================================================================
    def save_speed_profile(
        self, name: str, l_speed: str, f_speed: str, rate: str, s_rate: str
    ):
        """儲存速度 Profile"""
        self.speed_profiles[name] = {
            "l_speed": l_speed,
            "f_speed": f_speed,
            "rate": rate,
            "s_rate": s_rate,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        self._persist_profiles()
        self._log("INFO", f"速度 Profile [{name}] 已儲存")

    def delete_speed_profile(self, name: str):
        self.speed_profiles.pop(name, None)
        self._persist_profiles()
        self._log("INFO", f"速度 Profile [{name}] 已刪除")

    def _persist_profiles(self):
        p = RECORDING_DIR / "speed_profiles.json"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(self.speed_profiles, f, ensure_ascii=False, indent=2)

    def load_speed_profiles(self):
        p = RECORDING_DIR / "speed_profiles.json"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                self.speed_profiles = json.load(f)
            self._log("INFO", f"載入 {len(self.speed_profiles)} 個速度 Profile")

    # =========================================================================
    # Teaching Points
    # =========================================================================
    def save_point(self, name: str, positions: Dict[str, float], unit: str):
        """
        儲存 Teaching Point。
        同時記錄當時的單位，避免日後換算混亂。
        positions: 以當前 unit 為單位的座標值。
        """
        # 統一換算為 pulse 儲存
        pos_pulse = {ax: self.display_to_pulse(v) for ax, v in positions.items()}
        self.saved_points[name] = {
            "positions_pulse": pos_pulse,
            "unit_at_save": unit,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        self._log("INFO", f"Teaching Point [{name}] 已儲存 (單位={unit}): {positions}")
        self._persist_points()

    def delete_point(self, name: str):
        self.saved_points.pop(name, None)
        self._log("INFO", f"Teaching Point [{name}] 已刪除")
        self._persist_points()

    def goto_point(
        self,
        name: str,
        l_speed: str,
        f_speed: str,
        rate: str,
        s_rate: str,
        wait_done: bool = True,
    ):
        """
        移動至 Teaching Point。
        對各軸依序發送步進指令（先確認 X→Y→Z 順序或可設定）。
        """
        if name not in self.saved_points:
            self._log("ERROR", f"Teaching Point [{name}] 不存在")
            return False
        if self.ems_active or self.playback_running:
            return False

        pt = self.saved_points[name]
        pos_pulse = pt.get("positions_pulse", {})
        self._log("INFO", f"移動至 Teaching Point [{name}]")

        for ax, target_pulse in pos_pulse.items():
            axis_no = AXIS_NO.get(ax)
            if not axis_no:
                continue
            # 僅移動有啟用的軸
            if int(axis_no) > self.axis_count and not self.sim_mode:
                continue
            ok, reason = self._check_sw_limit(axis_no, target_pulse)
            if not ok:
                self._log("WARN", f"Teaching goto 軟體限位: {reason}")
                if self._alarm_cb:
                    self._alarm_cb("軟體行程限制", reason)
                return False

            with self._lock:
                cur_pulse = self._positions_pulse.get(ax, 0.0)
            delta_pulse = target_pulse - cur_pulse
            if abs(delta_pulse) < 0.5:  # 已在目標位置（<0.5 pulse）
                continue
            direction = "CW" if delta_pulse > 0 else "CCW"
            # 換算為當前顯示單位發送
            amt_display = abs(self.pulse_to_display(delta_pulse))
            self.move_step(
                axis_no,
                direction,
                f"{amt_display:.4f}",
                l_speed,
                f_speed,
                rate,
                s_rate,
                wait_done=wait_done,
            )
        return True

    def _persist_points(self):
        p = RECORDING_DIR / "teaching_points.json"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(self.saved_points, f, ensure_ascii=False, indent=2)

    def load_points(self):
        p = RECORDING_DIR / "teaching_points.json"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                self.saved_points = json.load(f)
            self._log("INFO", f"載入 {len(self.saved_points)} 個 Teaching Points")

    # =========================================================================
    # 行程錄製與重播
    # =========================================================================
    def start_recording(self, name: str = ""):
        self.recording = True
        self.recorded_steps = []
        self._recording_name = name or f"rec_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self._log("INFO", f"開始錄製行程: {self._recording_name}")

    def stop_recording(self) -> dict:
        self.recording = False
        rec = {
            "name": self._recording_name,
            "created": datetime.now().isoformat(timespec="seconds"),
            "steps": list(self.recorded_steps),
            "count": len(self.recorded_steps),
            "unit": self._unit,
        }
        self.recordings.append(rec)
        path = RECORDING_DIR / f"{self._recording_name}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        self._log(
            "INFO", f"行程 [{rec['name']}] 已儲存，{rec['count']} 步（純驅動指令）"
        )
        return rec

    def play_recording(
        self,
        rec: dict,
        repeat: int = 1,
        stop_event: Optional[threading.Event] = None,
        progress_cb=None,
    ):
        """
        重播行程。
        - 重播前設定 playback_running=True，鎖定其他移動操作。
        - 每步先等待前一步到位，再發送下一步，確保精度。
        - 發送完畢才等待 delay_ms（不含到位等待時間）。
        """
        self.playback_running = True
        steps = rec.get("steps", [])
        total = len(steps) * repeat
        done = 0
        self._log("INFO", f"開始重播 [{rec['name']}] × {repeat}，共 {total} 步")

        try:
            for _ in range(repeat):
                for step in steps:
                    if stop_event and stop_event.is_set():
                        self._log("WARN", "重播已中止")
                        return
                    if self.ems_active:
                        self._log("WARN", "EMS 中止重播")
                        return
                    tx = step.get("tx", "")
                    if tx:
                        self._serial_write(tx)
                        # 若為驅動指令，等待到位
                        if "GO" in tx and "GO ORG" not in tx:
                            ax_m = re.search(r"AXI(\d)", tx)
                            if ax_m and not self.sim_mode:
                                self._wait_axis_stop(ax_m.group(1))
                    delay_ms = step.get("delay_ms", 200)
                    time.sleep(delay_ms / 1000.0)
                    done += 1
                    if progress_cb:
                        progress_cb(done, total)
        finally:
            self.playback_running = False

        self._log("INFO", f"行程 [{rec['name']}] 重播完成")

    def load_recordings_from_disk(self):
        for p in sorted(RECORDING_DIR.glob("*.json")):
            if p.name in ("teaching_points.json", "speed_profiles.json"):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    rec = json.load(f)
                if not any(r.get("name") == rec.get("name") for r in self.recordings):
                    self.recordings.append(rec)
            except Exception as e:
                self._log("WARN", f"無法載入行程 {p.name}: {e}")

    # =========================================================================
    # 實驗數據記錄（CSV）
    # =========================================================================
    def start_data_log(self):
        self._data_log = []
        self._data_logging = True
        self._log("INFO", "實驗數據記錄已啟動")

    def stop_data_log(self) -> str:
        """停止記錄並儲存 CSV，回傳檔案路徑"""
        self._data_logging = False
        path = DATA_DIR / f"data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["ts"] + AXES)
            writer.writeheader()
            writer.writerows(self._data_log)
        self._log("INFO", f"實驗數據已匯出: {path}（{len(self._data_log)} 筆）")
        return str(path)

    def _record_data_point(self):
        """記錄當前時間戳與各軸位置（在 _wait_axis_stop 中定期呼叫）"""
        with self._lock:
            row = {"ts": datetime.now().isoformat(timespec="milliseconds")}
            row.update({ax: self._positions_pulse[ax] for ax in AXES})
        with self._history_lock:
            self._data_log.append(row)

    # =========================================================================
    # LOG 匯出
    # =========================================================================
    def export_log(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            with self._history_lock:
                history = list(self.action_history)
            for h in history:
                tx = f" TX=[{h['tx']}]" if h.get("tx") else ""
                rx = f" RX=[{h['rx']}]" if h.get("rx") else ""
                f.write(f"[{h['ts']}] [{h['level']}]{tx}{rx} {h['msg']}\n")
        self._log("INFO", f"LOG 已匯出: {path}")


# =============================================================================
# 常駐狀態列
# =============================================================================
class StatusBar(tk.Frame):
    """每個分頁底部的常駐狀態列：顯示所有軸工作座標、單位、最新 LOG。"""

    def __init__(self, parent, ctrl: DS102Controller, unit_var: tk.StringVar, **kwargs):
        super().__init__(parent, bg=CLR_BORDER, **kwargs)
        self.ctrl = ctrl
        self._unit_var = unit_var  # 共用全域單位變數

        coord_frame = tk.Frame(self, bg=CLR_CARD)
        coord_frame.pack(fill="x", padx=1, pady=(1, 0))

        self._coord_labels: Dict[str, tk.Label] = {}
        for ax in AXES:
            cell = tk.Frame(coord_frame, bg=CLR_CARD)
            cell.pack(side="left", padx=6, pady=2)
            tk.Label(
                cell,
                text=f"{ax}:",
                bg=CLR_CARD,
                fg=CLR_MUTED,
                font=("Segoe UI", 8, "bold"),
            ).pack(side="left")
            lbl = tk.Label(
                cell,
                text="0.000",
                bg=CLR_CARD,
                fg=CLR_TEXT,
                font=("Consolas", 10, "bold"),
                width=10,
                anchor="e",
            )
            lbl.pack(side="left")
            self._coord_labels[ax] = lbl

        unit_frame = tk.Frame(coord_frame, bg=CLR_CARD)
        unit_frame.pack(side="right", padx=6)
        tk.Label(
            unit_frame, text="單位:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 8)
        ).pack(side="left")
        tk.Label(
            unit_frame,
            textvariable=self._unit_var,
            bg=CLR_CARD,
            fg=CLR_ACCENT,
            font=("Segoe UI", 9, "bold"),
            width=6,
        ).pack(side="left")

        log_frame = tk.Frame(self, bg="#E8E7E2")
        log_frame.pack(fill="x", padx=1, pady=(0, 1))
        self._log_var = tk.StringVar(value="就緒")
        tk.Label(
            log_frame,
            textvariable=self._log_var,
            bg="#E8E7E2",
            fg=CLR_MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", padx=6, pady=1)

    def update_coords(self):
        """刷新各軸工作座標（含偏置）及換算顯示"""
        pos_work = self.ctrl.positions  # 已扣除 offset
        unit = self._unit_var.get()
        for ax, lbl in self._coord_labels.items():
            pulse_val = pos_work.get(ax, 0.0)
            disp = self.ctrl.pulse_to_display(pulse_val)
            # 依單位選擇小數位數
            decimals = 0 if unit == "pulse" else (3 if unit == "um" else 6)
            lbl.config(text=f"{disp:,.{decimals}f}")

    def update_log(self, msg: str):
        self._log_var.set(msg[:100])


# =============================================================================
# 主 GUI
# =============================================================================
class DS102GUI:

    def __init__(self, root: tk.Tk):
        self.root = root
        self.ctrl = DS102Controller()
        self.ctrl.set_log_callback(self._on_log_entry)
        self.ctrl.set_alarm_callback(self._on_alarm)

        # 全域 StringVar
        self._unit_var = tk.StringVar(value="pulse")
        self._axis_no_var = tk.StringVar(value="1")

        # 事件
        self._stop_playback = threading.Event()

        # 按鈕組（多分頁同步更新）
        self._all_axis_btn_groups: List[Dict[str, tk.Button]] = []
        self._all_unit_btn_groups: List[Dict[str, tk.Button]] = []

        # StatusBar 清單
        self._status_bars: List[StatusBar] = []

        # LOG 文字框
        self._log_text: Optional[tk.Text] = None
        self._log_auto_scroll = tk.BooleanVar(value=True)

        # 移動控制 UI 參考
        self._ctrl_status_var = tk.StringVar(value="Stop")
        self._ctrl_pos_var = tk.StringVar(value="0")
        self._drive_mode_var = tk.IntVar(value=MODE_CONTINUE)

        # 驅動按鈕參考（連線前 disable）
        self._drive_buttons: List[tk.Button] = []

        self._build_window()
        self._build_top_bar()
        self._build_notebook()

        self.ctrl.load_points()
        self.ctrl.load_recordings_from_disk()
        self.ctrl.load_speed_profiles()
        self._refresh_points()
        self._refresh_recordings()
        self._refresh_profiles()
        self._start_poller()

        self.ctrl._log("INFO", "DS102  圖形化控制器啟動")

    # =========================================================================
    # 視窗骨架
    # =========================================================================
    def _build_window(self):
        self.root.title("DS102 / DS112 步進馬達控制器 ")
        self.root.configure(bg=CLR_BG)
        self.root.minsize(1020, 720)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=CLR_BG, foreground=CLR_TEXT, font=("Segoe UI", 10))
        s.configure("TFrame", background=CLR_BG)
        s.configure("TLabel", background=CLR_BG, foreground=CLR_TEXT)
        s.configure("TEntry", fieldbackground=CLR_CARD, foreground=CLR_TEXT)
        s.configure("TCombobox", fieldbackground=CLR_CARD, foreground=CLR_TEXT)
        s.configure("TNotebook", background=CLR_BG, borderwidth=0)
        s.configure("TNotebook.Tab", padding=[14, 7], font=("Segoe UI", 10))
        s.map(
            "TNotebook.Tab", background=[("selected", CLR_CARD), ("!selected", CLR_BG)]
        )
        for name, bg, fg, abg in [
            ("Accent", CLR_ACCENT, "white", "#138A5F"),
            ("Danger", CLR_DANGER, "white", "#B52C22"),
            ("Info", CLR_INFO, "white", "#1557B0"),
            ("Warn", CLR_WARN, "white", "#C88000"),
            ("Flat", CLR_BORDER, CLR_TEXT, "#CCCAC3"),
        ]:
            s.configure(
                f"{name}.TButton",
                background=bg,
                foreground=fg,
                font=("Segoe UI", 10, "bold"),
                padding=[10, 5],
            )
            s.map(f"{name}.TButton", background=[("active", abg)])
        s.configure(
            "Treeview",
            background=CLR_CARD,
            fieldbackground=CLR_CARD,
            foreground=CLR_TEXT,
            rowheight=26,
        )
        s.configure(
            "Treeview.Heading",
            background=CLR_BG,
            foreground=CLR_MUTED,
            font=("Segoe UI", 9),
        )

    def _build_top_bar(self):
        top = tk.Frame(
            self.root, bg=CLR_CARD, highlightbackground=CLR_BORDER, highlightthickness=1
        )
        top.pack(fill="x")

        left = tk.Frame(top, bg=CLR_CARD)
        left.pack(side="left", padx=16, pady=8)
        tk.Label(
            left,
            text="DS102 / DS112  馬達控制器",
            bg=CLR_CARD,
            fg=CLR_TEXT,
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor="w")
        self._fw_var = tk.StringVar(value="（未連線）")
        tk.Label(
            left,
            textvariable=self._fw_var,
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
        ).pack(anchor="w")

        right = tk.Frame(top, bg=CLR_CARD)
        right.pack(side="right", padx=16, pady=8)
        self._ems_btn = tk.Button(
            right,
            text="⛔  緊急停止",
            bg=CLR_DANGER,
            fg="white",
            font=("Segoe UI", 11, "bold"),
            relief="flat",
            padx=14,
            pady=6,
            cursor="hand2",
            command=self._toggle_ems,
        )
        self._ems_btn.pack(side="right", padx=(10, 0))

        conn_f = tk.Frame(right, bg=CLR_CARD)
        conn_f.pack(side="right")
        self._conn_dot = tk.Canvas(
            conn_f, width=10, height=10, bg=CLR_CARD, highlightthickness=0
        )
        self._conn_dot.pack(side="left", padx=(0, 4))
        self._conn_dot_id = self._conn_dot.create_oval(
            1, 1, 9, 9, fill=CLR_DANGER, outline=""
        )
        self._conn_lbl = tk.Label(
            conn_f, text="未連線", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 10)
        )
        self._conn_lbl.pack(side="left")

        cr = tk.Frame(top, bg=CLR_CARD)
        cr.pack(side="left", padx=20, pady=8)

        tk.Label(
            cr, text="Port:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).grid(row=0, column=0, sticky="e", padx=(0, 4))
        self._port_var = tk.StringVar()
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self._port_cb = ttk.Combobox(
            cr, textvariable=self._port_var, values=ports, width=12, state="readonly"
        )
        if ports:
            self._port_var.set(ports[0])
        self._port_cb.grid(row=0, column=1, padx=(0, 6))

        tk.Label(
            cr, text="Baud:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).grid(row=0, column=2, sticky="e", padx=(0, 4))
        self._baud_var = tk.StringVar(value="38400")
        ttk.Combobox(
            cr,
            textvariable=self._baud_var,
            values=["38400", "19200", "9600", "4800"],
            width=8,
            state="readonly",
        ).grid(row=0, column=3, padx=(0, 8))

        tk.Button(
            cr,
            text="↻",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            relief="flat",
            font=("Segoe UI", 11),
            cursor="hand2",
            command=self._scan_ports,
        ).grid(row=0, column=4, padx=(0, 4))
        self._conn_btn = tk.Button(
            cr,
            text="連線",
            bg=CLR_ACCENT,
            fg="white",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
            command=self._toggle_connect,
        )
        # self._conn_btn.grid(row=0, column=5, padx=(0, 4))
        # tk.Button(
        #     cr,
        #     text="模擬模式",
        #     bg=CLR_INFO,
        #     fg="white",
        #     font=("Segoe UI", 10, "bold"),
        #     relief="flat",
        #     padx=10,
        #     pady=4,
        #     cursor="hand2",
        #     command=self._start_sim,
        # ).grid(row=0, column=6)

    # =========================================================================
    # Notebook
    # =========================================================================
    def _build_notebook(self):
        self._nb = ttk.Notebook(self.root)
        self._nb.pack(fill="both", expand=True)
        for name, builder in [
            ("儀表板", self._build_tab_dashboard),
            ("移動控制", self._build_tab_control),
            ("Teaching", self._build_tab_points),
            ("行程錄製", self._build_tab_recording),
            ("LOG", self._build_tab_log),
        ]:
            f = ttk.Frame(self._nb)
            self._nb.add(f, text=f"  {name}  ")
            builder(f)

    # ── 通用 UI 元件 ──────────────────────────────────────────
    def _scrollable(self, parent) -> tk.Frame:
        canvas = tk.Canvas(parent, bg=CLR_BG, highlightthickness=0)
        scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        frame = tk.Frame(canvas, bg=CLR_BG)
        frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=frame, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        def _mw(ev):
            canvas.yview_scroll(int(-1 * (ev.delta / 120)), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _mw))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return frame

    def _card(self, parent, title="", pady=(6, 6)) -> tk.Frame:
        outer = tk.Frame(parent, bg=CLR_BG)
        outer.pack(fill="x", padx=12, pady=pady)
        inner = tk.Frame(
            outer, bg=CLR_CARD, highlightbackground=CLR_BORDER, highlightthickness=1
        )
        inner.pack(fill="x")
        if title:
            tk.Label(
                inner,
                text=title,
                bg=CLR_CARD,
                fg=CLR_MUTED,
                font=("Segoe UI", 8, "bold"),
            ).pack(anchor="w", padx=12, pady=(6, 0))
        return inner

    def _add_status_bar(self, parent) -> StatusBar:
        sb = StatusBar(parent, self.ctrl, self._unit_var)
        sb.pack(side="bottom", fill="x")
        self._status_bars.append(sb)
        return sb

    # ── 軸選取 + 單位工具列 ───────────────────────────────────
    def _build_axis_selector(self, parent):
        row = tk.Frame(
            parent, bg=CLR_CARD, highlightbackground=CLR_BORDER, highlightthickness=1
        )
        row.pack(fill="x")
        tk.Label(
            row, text="軸選取:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left", padx=(12, 6), pady=6)

        cur_ax = next((a for a, n in AXIS_NO.items() if n == self.ctrl.axis_no), "X")
        axis_btns: Dict[str, tk.Button] = {}
        for ax in AXES:
            b = tk.Button(
                row,
                text=ax,
                width=3,
                relief="flat",
                font=("Segoe UI", 10, "bold"),
                cursor="hand2",
                bg=CLR_ACCENT if ax == cur_ax else CLR_BORDER,
                fg="white" if ax == cur_ax else CLR_TEXT,
                command=lambda a=ax, n=AXIS_NO[ax]: self._select_axis(a, n),
            )
            b.pack(side="left", padx=2, pady=4)
            axis_btns[ax] = b
        self._all_axis_btn_groups.append(axis_btns)

        ttk.Separator(row, orient="vertical").pack(
            side="left", fill="y", padx=10, pady=4
        )
        tk.Label(
            row, text="移動單位:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left", padx=(0, 6))

        cur_unit = self._unit_var.get()
        unit_btns: Dict[str, tk.Button] = {}
        for unit in ["pulse", "um", "mm"]:
            b = tk.Button(
                row,
                text=unit,
                width=6,
                relief="flat",
                font=("Segoe UI", 9),
                cursor="hand2",
                bg=CLR_ACCENT if unit == cur_unit else CLR_BORDER,
                fg="white" if unit == cur_unit else CLR_TEXT,
                command=lambda u=unit: self._select_unit(u),
            )
            b.pack(side="left", padx=2, pady=4)
            unit_btns[unit] = b
        self._all_unit_btn_groups.append(unit_btns)

    def _select_axis(self, ax_name: str, ax_no: str):
        self.ctrl.axis_no = ax_no
        self._axis_no_var.set(ax_no)
        for grp in self._all_axis_btn_groups:
            for a, b in grp.items():
                b.config(
                    bg=CLR_ACCENT if a == ax_name else CLR_BORDER,
                    fg="white" if a == ax_name else CLR_TEXT,
                )
        self.ctrl._log("INFO", f"選取軸 {ax_name} ({ax_no})")
        self._async_query()

    def _select_unit(self, unit: str):
        self._unit_var.set(unit)
        self.ctrl.set_unit(unit)
        for grp in self._all_unit_btn_groups:
            for u, b in grp.items():
                b.config(
                    bg=CLR_ACCENT if u == unit else CLR_BORDER,
                    fg="white" if u == unit else CLR_TEXT,
                )

    # =========================================================================
    # TAB：儀表板
    # =========================================================================
    def _build_tab_dashboard(self, parent):
        self._add_status_bar(parent)
        scr = self._scrollable(parent)

        # ── 系統狀態 ──
        stat_card = self._card(scr, "系統狀態")
        stat_row = tk.Frame(stat_card, bg=CLR_CARD)
        stat_row.pack(fill="x", padx=12, pady=8)
        self._stat_vars: Dict[str, tk.StringVar] = {}
        for i, (key, lbl, val) in enumerate(
            [
                ("conn", "連線狀態", "未連線"),
                ("axes", "可用軸數", "—"),
                ("ems", "EMS", "正常"),
                ("play", "重播狀態", "閒置"),
                ("dlog", "數據記錄", "停止"),
            ]
        ):
            cell = tk.Frame(stat_row, bg="#F0EEE8")
            cell.grid(row=0, column=i, padx=4, sticky="ew")
            stat_row.columnconfigure(i, weight=1)
            tk.Label(
                cell, text=lbl, bg="#F0EEE8", fg=CLR_MUTED, font=("Segoe UI", 8)
            ).pack(pady=(6, 0))
            v = tk.StringVar(value=val)
            self._stat_vars[key] = v
            tk.Label(
                cell,
                textvariable=v,
                bg="#F0EEE8",
                fg=CLR_TEXT,
                font=("Segoe UI", 12, "bold"),
            ).pack(pady=(0, 6))

        # ── 各軸位置（顯示工作座標） ──
        pos_card = self._card(scr, "各軸工作座標（已套用偏置）")
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
            tk.Label(
                cell,
                text=f"{ax} 軸",
                bg="#F0EEE8",
                fg=CLR_MUTED,
                font=("Segoe UI", 9, "bold"),
            ).pack(anchor="w", padx=8, pady=(6, 0))
            pv = tk.StringVar(value="0")
            self._dash_pos_vars[ax] = pv
            tk.Label(
                cell,
                textvariable=pv,
                bg="#F0EEE8",
                fg=CLR_TEXT,
                font=("Consolas", 18, "bold"),
            ).pack(anchor="w", padx=8)
            sv = tk.StringVar(value="—")
            self._dash_status_vars[ax] = sv
            tk.Label(
                cell, textvariable=sv, bg="#F0EEE8", fg=CLR_MUTED, font=("Segoe UI", 8)
            ).pack(anchor="w", padx=8, pady=(0, 2))
            # 工作原點設定
            btn_f = tk.Frame(cell, bg="#F0EEE8")
            btn_f.pack(fill="x", padx=8, pady=(0, 6))
            tk.Button(
                btn_f,
                text="此處設工作原點",
                bg="#E8F5E9",
                fg="#2E7D32",
                font=("Segoe UI", 8),
                relief="flat",
                cursor="hand2",
                command=lambda a=AXIS_NO[ax]: self.ctrl.set_offset_here(a),
            ).pack(side="left", padx=(0, 4))
            tk.Button(
                btn_f,
                text="清除偏置",
                bg=CLR_BORDER,
                fg=CLR_TEXT,
                font=("Segoe UI", 8),
                relief="flat",
                cursor="hand2",
                command=lambda a=AXIS_NO[ax]: self.ctrl.clear_offset(a),
            ).pack(side="left")

        # ── 速度設定 ──
        spd_card = self._card(scr, "速度設定")
        spd_f = tk.Frame(spd_card, bg=CLR_CARD)
        spd_f.pack(fill="x", padx=12, pady=8)
        self._spd_vars: Dict[str, tk.StringVar] = {}
        for r, (key, lbl, default, unit) in enumerate(
            [
                ("l_speed", "Start-up Speed (L)", "100", "pps"),
                ("rate", "Accel/Decel Rate (R)", "100", "ms"),
                ("s_rate", "S-curve Rate (S)", "100", "%"),
                ("f_speed", "Driving Speed (F)", "1000", "pps"),
            ]
        ):
            tk.Label(
                spd_f,
                text=lbl,
                bg=CLR_CARD,
                fg=CLR_MUTED,
                font=("Segoe UI", 9),
                width=22,
                anchor="w",
            ).grid(row=r, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=default)
            self._spd_vars[key] = v
            ttk.Entry(spd_f, textvariable=v, width=12).grid(row=r, column=1, padx=8)
            tk.Label(
                spd_f, text=unit, bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
            ).grid(row=r, column=2, sticky="w")

        # Profile 快速切換
        prof_row = tk.Frame(spd_card, bg=CLR_CARD)
        prof_row.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(
            prof_row, text="Profile:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left")
        self._profile_var = tk.StringVar()
        self._profile_cb = ttk.Combobox(
            prof_row, textvariable=self._profile_var, width=16, state="readonly"
        )
        self._profile_cb.pack(side="left", padx=6)
        self._profile_cb.bind("<<ComboboxSelected>>", self._load_profile)
        ttk.Button(
            prof_row, text="載入", style="Flat.TButton", command=self._load_profile
        ).pack(side="left", padx=2)
        ttk.Button(
            prof_row, text="儲存目前設定", command=self._save_profile_dialog
        ).pack(side="left", padx=6)
        ttk.Button(
            prof_row, text="刪除", style="Danger.TButton", command=self._delete_profile
        ).pack(side="left", padx=2)

        # ── 軟體行程限制 ──
        lim_card = self._card(scr, "軟體行程限制（Software Limit）")
        lim_note = tk.Label(
            lim_card,
            text="單位：pulse。留空表示不限制。設定後馬上生效，超限指令將被攔截。",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 8),
        )
        lim_note.pack(anchor="w", padx=12, pady=(2, 4))
        lim_grid = tk.Frame(lim_card, bg=CLR_CARD)
        lim_grid.pack(fill="x", padx=12, pady=(0, 8))
        self._lim_vars: Dict[str, Tuple[tk.StringVar, tk.StringVar]] = {}
        tk.Label(
            lim_grid,
            text="軸",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
            width=4,
        ).grid(row=0, column=0)
        tk.Label(
            lim_grid,
            text="CCW 限制",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
            width=14,
        ).grid(row=0, column=1)
        tk.Label(
            lim_grid,
            text="CW 限制",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
            width=14,
        ).grid(row=0, column=2)
        for r, ax in enumerate(AXES, start=1):
            tk.Label(
                lim_grid,
                text=ax,
                bg=CLR_CARD,
                fg=CLR_TEXT,
                font=("Segoe UI", 9, "bold"),
                width=4,
            ).grid(row=r, column=0, pady=2)
            ccw_v = tk.StringVar(value="")
            cw_v = tk.StringVar(value="")
            self._lim_vars[ax] = (ccw_v, cw_v)
            ttk.Entry(lim_grid, textvariable=ccw_v, width=14).grid(
                row=r, column=1, padx=4, pady=2
            )
            ttk.Entry(lim_grid, textvariable=cw_v, width=14).grid(
                row=r, column=2, padx=4, pady=2
            )
        ttk.Button(
            lim_card,
            text="套用限制設定",
            style="Accent.TButton",
            command=self._apply_sw_limits,
        ).pack(padx=12, pady=(0, 8))

        # ── 實驗數據記錄 ──
        data_card = self._card(scr, "實驗數據記錄（CSV）")
        data_row = tk.Frame(data_card, bg=CLR_CARD)
        data_row.pack(fill="x", padx=12, pady=8)
        self._dlog_status_var = tk.StringVar(value="停止")
        tk.Label(
            data_row,
            textvariable=self._dlog_status_var,
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 12))
        self._dlog_start_btn = ttk.Button(
            data_row,
            text="▶ 開始記錄",
            style="Accent.TButton",
            command=self._start_data_log,
        )
        self._dlog_start_btn.pack(side="left", padx=4)
        self._dlog_stop_btn = ttk.Button(
            data_row, text="■ 停止並匯出", command=self._stop_data_log, state="disabled"
        )
        self._dlog_stop_btn.pack(side="left", padx=4)

    # =========================================================================
    # TAB：移動控制
    # =========================================================================
    def _build_tab_control(self, parent):
        self._add_status_bar(parent)
        self._build_axis_selector(parent)
        scr = self._scrollable(parent)

        # ── 驅動模式 ──
        mode_card = self._card(scr, "驅動模式")
        mode_f = tk.Frame(mode_card, bg=CLR_CARD)
        mode_f.pack(fill="x", padx=12, pady=8)
        self._step_dist_var = tk.StringVar(value="1000")
        self._org_mode_var = tk.StringVar(value="ORG 0")
        for mode_val, mode_lbl in [
            (MODE_CONTINUE, "連續點動 (Continue)"),
            (MODE_STEP, "步進 (Step)"),
            (MODE_ORIGIN, "原點返回 (Origin)"),
        ]:
            rf = tk.Frame(mode_f, bg=CLR_CARD)
            rf.pack(fill="x", pady=2)
            tk.Radiobutton(
                rf,
                text=mode_lbl,
                variable=self._drive_mode_var,
                value=mode_val,
                bg=CLR_CARD,
                fg=CLR_TEXT,
                font=("Segoe UI", 10),
                activebackground=CLR_CARD,
                command=self._on_mode_change,
                width=22,
                anchor="w",
            ).pack(side="left")
            if mode_val == MODE_STEP:
                ttk.Entry(rf, textvariable=self._step_dist_var, width=12).pack(
                    side="left", padx=4
                )
                tk.Label(
                    rf,
                    text="（當前單位）",
                    bg=CLR_CARD,
                    fg=CLR_MUTED,
                    font=("Segoe UI", 8),
                ).pack(side="left")
            elif mode_val == MODE_ORIGIN:
                ttk.Combobox(
                    rf,
                    textvariable=self._org_mode_var,
                    values=ORG_MODES,
                    width=10,
                    state="readonly",
                ).pack(side="left", padx=4)

        # ── 驅動按鈕 ──
        drv_card = self._card(scr, "驅動（長按=連續點動，點擊=步進/原點）")
        drv_f = tk.Frame(drv_card, bg=CLR_CARD)
        drv_f.pack(pady=12)

        # 當前軸 + 狀態顯示
        info_row = tk.Frame(drv_f, bg=CLR_CARD)
        info_row.pack(pady=(0, 6))
        tk.Label(
            info_row, text="軸:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left")
        tk.Label(
            info_row,
            textvariable=self._axis_no_var,
            bg=CLR_CARD,
            fg=CLR_ACCENT,
            font=("Segoe UI", 14, "bold"),
        ).pack(side="left", padx=(4, 16))
        tk.Label(
            info_row, text="狀態:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left")
        tk.Label(
            info_row,
            textvariable=self._ctrl_status_var,
            bg=CLR_CARD,
            fg=CLR_TEXT,
            font=("Segoe UI", 11, "bold"),
        ).pack(side="left", padx=4)

        # 位置設定
        pos_row = tk.Frame(drv_f, bg=CLR_CARD)
        pos_row.pack(pady=(0, 10))
        tk.Label(
            pos_row, text="Position:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).pack(side="left")
        ttk.Entry(pos_row, textvariable=self._ctrl_pos_var, width=14).pack(
            side="left", padx=6
        )
        ttk.Button(pos_row, text="Set Position", command=self._do_set_position).pack(
            side="left", padx=4
        )

        # CCW / Stop / CW
        btn_row = tk.Frame(drv_f, bg=CLR_CARD)
        btn_row.pack()
        self._ccw_btn = tk.Button(
            btn_row,
            text="−  CCW",
            width=12,
            height=3,
            bg=CLR_INFO,
            fg="white",
            font=("Segoe UI", 12, "bold"),
            relief="flat",
            cursor="hand2",
            state="disabled",
        )
        self._ccw_btn.bind("<ButtonPress-1>", self._on_ccw_press)
        self._ccw_btn.bind("<ButtonRelease-1>", self._on_ccw_release)
        self._ccw_btn.pack(side="left", padx=12)

        stop_btn = tk.Button(
            btn_row,
            text="■  Stop",
            width=10,
            height=3,
            bg=CLR_DANGER,
            fg="white",
            font=("Segoe UI", 11, "bold"),
            relief="flat",
            cursor="hand2",
            state="disabled",
            command=self._do_stop,
        )
        stop_btn.pack(side="left", padx=4)

        self._cw_btn = tk.Button(
            btn_row,
            text="CW  ＋",
            width=12,
            height=3,
            bg=CLR_ACCENT,
            fg="white",
            font=("Segoe UI", 12, "bold"),
            relief="flat",
            cursor="hand2",
            state="disabled",
        )
        self._cw_btn.bind("<ButtonPress-1>", self._on_cw_press)
        self._cw_btn.bind("<ButtonRelease-1>", self._on_cw_release)
        self._cw_btn.pack(side="left", padx=12)

        self._drive_buttons.extend([self._ccw_btn, stop_btn, self._cw_btn])
        tk.Label(
            drv_card,
            text="連線後方可使用；EMS 或重播中驅動按鈕自動鎖定",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 8),
        ).pack(pady=(0, 8))

    # ── 模式與驅動事件 ────────────────────────────────────────
    def _on_mode_change(self):
        mode = self._drive_mode_var.get()
        if mode == MODE_ORIGIN:
            self._ccw_btn.config(text="Origin")
            self._cw_btn.config(text="Origin")
        else:
            self._ccw_btn.config(text="−  CCW")
            self._cw_btn.config(text="CW  ＋")

    def _get_spd(self):
        return (
            self._spd_vars["l_speed"].get(),
            self._spd_vars["f_speed"].get(),
            self._spd_vars["rate"].get(),
            self._spd_vars["s_rate"].get(),
        )

    def _on_ccw_press(self, event):
        if (
            not self.ctrl.connected
            or self.ctrl.ems_active
            or self.ctrl.playback_running
        ):
            return
        mode = self._drive_mode_var.get()
        ax = self.ctrl.axis_no
        l, f, r, s = self._get_spd()
        if mode == MODE_CONTINUE:
            self.ctrl.move_continue(ax, "CCW", l, f, r, s)
            self._poll_status()
        elif mode == MODE_STEP:
            threading.Thread(
                target=self.ctrl.move_step,
                args=(ax, "CCW", self._step_dist_var.get(), l, f, r, s, True),
                daemon=True,
            ).start()
            self._poll_status()
        elif mode == MODE_ORIGIN:
            org_idx = (
                ORG_MODES.index(self._org_mode_var.get())
                if self._org_mode_var.get() in ORG_MODES
                else 0
            )
            threading.Thread(
                target=self.ctrl.move_origin,
                args=(ax, org_idx, l, f, r, s, True),
                daemon=True,
            ).start()
            self._poll_status()

    def _on_ccw_release(self, event):
        if self._drive_mode_var.get() == MODE_CONTINUE:
            self.ctrl.stop()

    def _on_cw_press(self, event):
        if (
            not self.ctrl.connected
            or self.ctrl.ems_active
            or self.ctrl.playback_running
        ):
            return
        mode = self._drive_mode_var.get()
        ax = self.ctrl.axis_no
        l, f, r, s = self._get_spd()
        if mode == MODE_CONTINUE:
            self.ctrl.move_continue(ax, "CW", l, f, r, s)
            self._poll_status()
        elif mode == MODE_STEP:
            threading.Thread(
                target=self.ctrl.move_step,
                args=(ax, "CW", self._step_dist_var.get(), l, f, r, s, True),
                daemon=True,
            ).start()
            self._poll_status()
        elif mode == MODE_ORIGIN:
            org_idx = (
                ORG_MODES.index(self._org_mode_var.get())
                if self._org_mode_var.get() in ORG_MODES
                else 0
            )
            threading.Thread(
                target=self.ctrl.move_origin,
                args=(ax, org_idx, l, f, r, s, True),
                daemon=True,
            ).start()
            self._poll_status()

    def _on_cw_release(self, event):
        if self._drive_mode_var.get() == MODE_CONTINUE:
            self.ctrl.stop()

    def _do_stop(self):
        self.ctrl.stop()

    def _do_set_position(self):
        val = self._ctrl_pos_var.get().strip()
        if val:
            self.ctrl.set_position(self.ctrl.axis_no, val)

    def _poll_status(self):
        """非同步輪詢狀態直到停止"""

        def _check():
            status, pos = self.ctrl.query_status(self.ctrl.axis_no)
            self.root.after(0, lambda: self._ctrl_status_var.set(status))
            if pos:
                self.root.after(0, lambda: self._ctrl_pos_var.set(pos))
            if status == "Driving":
                self.root.after(100, _check)

        threading.Thread(target=_check, daemon=True).start()

    def _async_query(self):
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
        self._add_status_bar(parent)
        self._build_axis_selector(parent)
        scr = self._scrollable(parent)

        add_card = self._card(scr, "新增 / 編輯 Teaching Point")
        af = tk.Frame(add_card, bg=CLR_CARD)
        af.pack(fill="x", padx=12, pady=8)

        tk.Label(
            af, text="點名稱:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).grid(row=0, column=0, sticky="w", pady=3)
        self._pt_name_var = tk.StringVar()
        ttk.Entry(af, textvariable=self._pt_name_var, width=20).grid(
            row=0, column=1, sticky="w", padx=8
        )

        self._pt_pos_vars: Dict[str, tk.StringVar] = {}
        tk.Label(
            af,
            text="座標（pulse: −99999999~99999999 | um/mm: −9.9999999~9.9999999）",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 8),
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 2))
        vcmd = (self.root.register(self._validate_coord), "%P")
        for i, ax in enumerate(AXES):
            r_base, col_base = 2 + i // 3, (i % 3) * 2
            tk.Label(
                af,
                text=f"{ax}:",
                bg=CLR_CARD,
                fg=CLR_MUTED,
                font=("Segoe UI", 9),
                width=3,
                anchor="e",
            ).grid(row=r_base, column=col_base, sticky="e", padx=(8, 2), pady=2)
            v = tk.StringVar(value="0")
            self._pt_pos_vars[ax] = v
            ttk.Entry(
                af, textvariable=v, width=14, validate="key", validatecommand=vcmd
            ).grid(row=r_base, column=col_base + 1, sticky="w", padx=4, pady=2)

        pbtn = tk.Frame(add_card, bg=CLR_CARD)
        pbtn.pack(padx=12, pady=(0, 8))
        ttk.Button(
            pbtn,
            text="儲存 Teaching Point",
            style="Accent.TButton",
            command=self._do_save_point,
        ).pack(side="left", padx=4)
        ttk.Button(
            pbtn,
            text="填入當前位置",
            style="Info.TButton",
            command=self._do_fill_current,
        ).pack(side="left", padx=4)
        ttk.Button(
            pbtn,
            text="清除",
            style="Flat.TButton",
            command=lambda: [v.set("0") for v in self._pt_pos_vars.values()],
        ).pack(side="left", padx=4)

        list_card = self._card(scr, "已儲存 Teaching Points")
        self._pts_tree = ttk.Treeview(
            list_card,
            columns=("name", "X", "Y", "Z", "unit", "ts"),
            show="headings",
            height=8,
        )
        for col, w, lbl in [
            ("name", 120, "名稱"),
            ("X", 90, "X"),
            ("Y", 90, "Y"),
            ("Z", 90, "Z"),
            ("unit", 60, "單位"),
            ("ts", 160, "時間"),
        ]:
            self._pts_tree.heading(col, text=lbl)
            self._pts_tree.column(col, width=w)
        self._pts_tree.bind("<Double-1>", lambda e: self._do_load_point())
        sy = ttk.Scrollbar(list_card, orient="vertical", command=self._pts_tree.yview)
        self._pts_tree.configure(yscrollcommand=sy.set)
        self._pts_tree.pack(side="left", fill="x", expand=True, padx=12, pady=8)
        sy.pack(side="right", fill="y", pady=8)

        ptbtn = tk.Frame(list_card, bg=CLR_CARD)
        ptbtn.pack(padx=12, pady=(0, 8))
        ttk.Button(
            ptbtn, text="✏ 載入編輯", style="Flat.TButton", command=self._do_load_point
        ).pack(side="left", padx=4)
        ttk.Button(
            ptbtn,
            text="▶ 移動至此點",
            style="Accent.TButton",
            command=self._do_goto_point,
        ).pack(side="left", padx=4)
        ttk.Button(
            ptbtn, text="🗑 刪除", style="Danger.TButton", command=self._do_delete_point
        ).pack(side="left", padx=4)

    def _validate_coord(self, value: str) -> bool:
        if value in ("", "-", ".", "-.", "+"):
            return True
        try:
            f = float(value)
            return (
                (-9.9999999 <= f <= 9.9999999)
                if "." in value
                else (-99999999 <= int(value) <= 99999999)
            )
        except ValueError:
            return False

    def _do_save_point(self):
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
        unit = self._unit_var.get()
        self.ctrl.save_point(name, positions, unit)
        self._refresh_points()

    def _do_fill_current(self):
        unit = self._unit_var.get()
        for ax, var in self._pt_pos_vars.items():
            pulse = self.ctrl._positions_pulse.get(ax, 0.0)
            disp = self.ctrl.pulse_to_display(pulse)
            dec = 0 if unit == "pulse" else (3 if unit == "um" else 6)
            var.set(f"{disp:.{dec}f}")

    def _do_load_point(self):
        sel = self._pts_tree.selection()
        if not sel:
            return
        name = self._pts_tree.item(sel[0])["values"][0]
        pt = self.ctrl.saved_points.get(name, {})
        self._pt_name_var.set(name)
        pos_pulse = pt.get("positions_pulse", {})
        unit = self._unit_var.get()
        for ax, var in self._pt_pos_vars.items():
            p_val = pos_pulse.get(ax, 0.0)
            disp = self.ctrl.pulse_to_display(p_val)
            dec = 0 if unit == "pulse" else (3 if unit == "um" else 6)
            var.set(f"{disp:.{dec}f}")

    def _do_goto_point(self):
        sel = self._pts_tree.selection()
        if not sel:
            messagebox.showwarning("警告", "請先選取 Teaching Point")
            return
        name = self._pts_tree.item(sel[0])["values"][0]
        l, f, r, s = self._get_spd()
        threading.Thread(
            target=self.ctrl.goto_point, args=(name, l, f, r, s, True), daemon=True
        ).start()
        self._poll_status()

    def _do_delete_point(self):
        sel = self._pts_tree.selection()
        if not sel:
            return
        name = self._pts_tree.item(sel[0])["values"][0]
        if messagebox.askyesno("確認", f"確定刪除 [{name}]？"):
            self.ctrl.delete_point(name)
            self._refresh_points()

    def _refresh_points(self):
        self._pts_tree.delete(*self._pts_tree.get_children())
        for name, data in self.ctrl.saved_points.items():
            pos_p = data.get("positions_pulse", {})
            unit = data.get("unit_at_save", "um")

            def _fmt(p, u=unit):
                d = self.ctrl.pulse_to_display(p)
                dec = 0 if u == "pulse" else (3 if u == "um" else 6)
                return f"{d:.{dec}f}"

            self._pts_tree.insert(
                "",
                "end",
                values=(
                    name,
                    _fmt(pos_p.get("X", 0)),
                    _fmt(pos_p.get("Y", 0)),
                    _fmt(pos_p.get("Z", 0)),
                    unit,
                    data.get("ts", "")[:19],
                ),
            )

    # =========================================================================
    # TAB：行程錄製
    # =========================================================================
    def _build_tab_recording(self, parent):
        self._add_status_bar(parent)
        scr = self._scrollable(parent)

        rec_card = self._card(scr, "行程錄製")
        rf = tk.Frame(rec_card, bg=CLR_CARD)
        rf.pack(fill="x", padx=12, pady=8)
        tk.Label(
            rf, text="行程名稱:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).grid(row=0, column=0, sticky="w")
        self._rec_name_var = tk.StringVar()
        self._rec_status_var = tk.StringVar(value="● 閒置")
        self._rec_count_var = tk.StringVar(value="0 步")
        ttk.Entry(rf, textvariable=self._rec_name_var, width=24).grid(
            row=0, column=1, sticky="w", padx=8
        )
        tk.Label(
            rf,
            textvariable=self._rec_status_var,
            bg=CLR_CARD,
            fg=CLR_DANGER,
            font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=2, padx=6)
        tk.Label(
            rf,
            textvariable=self._rec_count_var,
            bg=CLR_CARD,
            fg=CLR_TEXT,
            font=("Segoe UI", 9),
        ).grid(row=0, column=3)

        rbtn = tk.Frame(rec_card, bg=CLR_CARD)
        rbtn.pack(padx=12, pady=(0, 8))
        self._rec_start_btn = ttk.Button(
            rbtn, text="⏺ 開始錄製", style="Danger.TButton", command=self._do_start_rec
        )
        self._rec_start_btn.pack(side="left", padx=4)
        self._rec_stop_btn = ttk.Button(
            rbtn, text="⏹ 停止錄製", command=self._do_stop_rec, state="disabled"
        )
        self._rec_stop_btn.pack(side="left", padx=4)

        steps_card = self._card(scr, "已錄製步驟（僅含驅動指令，雙擊延遲欄可修改）")
        tf = tk.Frame(steps_card, bg=CLR_CARD)
        tf.pack(fill="x", padx=12, pady=6)
        self._steps_tree = ttk.Treeview(
            tf, columns=("idx", "ts", "cmd", "delay_ms"), show="headings", height=8
        )
        for col, w, lbl in [
            ("idx", 40, "#"),
            ("ts", 100, "時間"),
            ("cmd", 300, "指令"),
            ("delay_ms", 90, "延遲(ms)"),
        ]:
            self._steps_tree.heading(col, text=lbl)
            self._steps_tree.column(col, width=w)
        self._steps_tree.bind("<Double-1>", self._on_step_dclick)
        sy2 = ttk.Scrollbar(tf, orient="vertical", command=self._steps_tree.yview)
        self._steps_tree.configure(yscrollcommand=sy2.set)
        self._steps_tree.pack(side="left", fill="x", expand=True)
        sy2.pack(side="right", fill="y")

        gd = tk.Frame(steps_card, bg=CLR_CARD)
        gd.pack(padx=12, pady=(0, 8))
        tk.Label(
            gd,
            text="批次設定所有步驟延遲:",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left")
        self._global_delay_var = tk.StringVar(value="200")
        ttk.Entry(gd, textvariable=self._global_delay_var, width=8).pack(
            side="left", padx=6
        )
        tk.Label(gd, text="ms", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)).pack(
            side="left"
        )
        ttk.Button(
            gd, text="套用", style="Flat.TButton", command=self._apply_global_delay
        ).pack(side="left", padx=6)

        list_card = self._card(scr, "已儲存行程")
        self._rec_tree = ttk.Treeview(
            list_card,
            columns=("name", "count", "unit", "created"),
            show="headings",
            height=5,
        )
        for col, w, lbl in [
            ("name", 160, "名稱"),
            ("count", 60, "步數"),
            ("unit", 60, "單位"),
            ("created", 180, "建立時間"),
        ]:
            self._rec_tree.heading(col, text=lbl)
            self._rec_tree.column(col, width=w)
        self._rec_tree.bind("<<TreeviewSelect>>", self._on_rec_select)
        sy3 = ttk.Scrollbar(list_card, orient="vertical", command=self._rec_tree.yview)
        self._rec_tree.configure(yscrollcommand=sy3.set)
        self._rec_tree.pack(side="left", fill="x", expand=True, padx=12, pady=8)
        sy3.pack(side="right", fill="y", pady=8)

        play_card = self._card(scr, "重播設定")
        pf = tk.Frame(play_card, bg=CLR_CARD)
        pf.pack(fill="x", padx=12, pady=8)
        tk.Label(
            pf, text="重複次數:", bg=CLR_CARD, fg=CLR_MUTED, font=("Segoe UI", 9)
        ).grid(row=0, column=0, sticky="w", pady=3)
        self._repeat_var = tk.IntVar(value=1)
        ttk.Spinbox(pf, from_=1, to=9999, textvariable=self._repeat_var, width=8).grid(
            row=0, column=1, sticky="w", padx=8
        )
        self._play_progress_var = tk.StringVar(value="—")
        tk.Label(
            pf,
            textvariable=self._play_progress_var,
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9),
        ).grid(row=0, column=2, padx=16)

        plbtn = tk.Frame(play_card, bg=CLR_CARD)
        plbtn.pack(padx=12, pady=(0, 8))
        ttk.Button(
            plbtn, text="▶ 重播行程", style="Accent.TButton", command=self._do_play_rec
        ).pack(side="left", padx=4)
        ttk.Button(
            plbtn,
            text="■ 停止重播",
            style="Danger.TButton",
            command=lambda: self._stop_playback.set(),
        ).pack(side="left", padx=4)

    def _do_start_rec(self):
        self.ctrl.start_recording(self._rec_name_var.get().strip())
        self._rec_status_var.set("🔴 錄製中")
        self._rec_start_btn.state(["disabled"])
        self._rec_stop_btn.state(["!disabled"])
        self._rec_step_timer()

    def _rec_step_timer(self):
        if self.ctrl.recording:
            self._rec_count_var.set(f"{len(self.ctrl.recorded_steps)} 步")
            self.root.after(500, self._rec_step_timer)

    def _do_stop_rec(self):
        rec = self.ctrl.stop_recording()
        self._rec_status_var.set("● 閒置")
        self._rec_count_var.set("0 步")
        self._rec_start_btn.state(["!disabled"])
        self._rec_stop_btn.state(["disabled"])
        self._refresh_recordings()
        self._load_steps_to_tree(rec)

    def _on_rec_select(self, event):
        sel = self._rec_tree.selection()
        if not sel:
            return
        idx = self._rec_tree.index(sel[0])
        if 0 <= idx < len(self.ctrl.recordings):
            self._load_steps_to_tree(self.ctrl.recordings[idx])

    def _load_steps_to_tree(self, rec: dict):
        self._steps_tree.delete(*self._steps_tree.get_children())
        for i, step in enumerate(rec.get("steps", [])):
            self._steps_tree.insert(
                "",
                "end",
                values=(
                    i + 1,
                    step.get("ts", ""),
                    step.get("tx", step.get("msg", "")),
                    step.get("delay_ms", 200),
                ),
            )

    def _on_step_dclick(self, event):
        sel = self._steps_tree.selection()
        if not sel:
            return
        item = sel[0]
        vals = self._steps_tree.item(item)["values"]
        step_idx = int(vals[0]) - 1
        win = tk.Toplevel(self.root)
        win.title("修改步驟延遲")
        win.geometry("300x110")
        win.resizable(False, False)
        win.grab_set()
        tk.Label(
            win,
            text=f"步驟 #{vals[0]}  指令: {str(vals[2])[:35]}",
            font=("Segoe UI", 9),
            fg=CLR_MUTED,
        ).pack(pady=(10, 0))
        dvar = tk.StringVar(value=str(vals[3]))
        rf = tk.Frame(win)
        rf.pack(pady=8)
        tk.Label(rf, text="延遲 (ms):").pack(side="left")
        ttk.Entry(rf, textvariable=dvar, width=10).pack(side="left", padx=6)

        def _apply():
            try:
                ms = int(dvar.get())
                assert ms >= 0
            except Exception:
                messagebox.showerror("錯誤", "請輸入有效正整數", parent=win)
                return
            self._steps_tree.item(item, values=(vals[0], vals[1], vals[2], ms))
            rec_sel = self._rec_tree.selection()
            if rec_sel:
                ridx = self._rec_tree.index(rec_sel[0])
                if 0 <= ridx < len(self.ctrl.recordings):
                    steps = self.ctrl.recordings[ridx].get("steps", [])
                    if 0 <= step_idx < len(steps):
                        steps[step_idx]["delay_ms"] = ms
            win.destroy()

        ttk.Button(win, text="確認", style="Accent.TButton", command=_apply).pack()

    def _apply_global_delay(self):
        try:
            ms = int(self._global_delay_var.get())
            assert ms >= 0
        except Exception:
            messagebox.showerror("錯誤", "請輸入有效毫秒數")
            return
        for item in self._steps_tree.get_children():
            vals = self._steps_tree.item(item)["values"]
            self._steps_tree.item(item, values=(vals[0], vals[1], vals[2], ms))
        rec_sel = self._rec_tree.selection()
        if rec_sel:
            ridx = self._rec_tree.index(rec_sel[0])
            if 0 <= ridx < len(self.ctrl.recordings):
                for step in self.ctrl.recordings[ridx].get("steps", []):
                    step["delay_ms"] = ms

    def _do_play_rec(self):
        sel = self._rec_tree.selection()
        if not sel:
            messagebox.showwarning("警告", "請先選擇行程")
            return
        idx = self._rec_tree.index(sel[0])
        if idx >= len(self.ctrl.recordings):
            return
        self._stop_playback.clear()

        def _progress(done, total):
            self.root.after(0, lambda: self._play_progress_var.set(f"{done}/{total}"))
            self.root.after(0, self._update_stat_ui)

        threading.Thread(
            target=self.ctrl.play_recording,
            args=(
                self.ctrl.recordings[idx],
                self._repeat_var.get(),
                self._stop_playback,
                _progress,
            ),
            daemon=True,
        ).start()

    def _refresh_recordings(self):
        self._rec_tree.delete(*self._rec_tree.get_children())
        for rec in self.ctrl.recordings:
            self._rec_tree.insert(
                "",
                "end",
                values=(
                    rec.get("name", ""),
                    rec.get("count", 0),
                    rec.get("unit", "—"),
                    rec.get("created", "")[:19],
                ),
            )

    # =========================================================================
    # TAB：LOG
    # =========================================================================
    def _build_tab_log(self, parent):
        self._add_status_bar(parent)
        toolbar = tk.Frame(
            parent, bg=CLR_CARD, highlightbackground=CLR_BORDER, highlightthickness=1
        )
        toolbar.pack(fill="x")
        tk.Label(
            toolbar,
            text="動作 LOG",
            bg=CLR_CARD,
            fg=CLR_MUTED,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=12, pady=6)
        ttk.Button(
            toolbar, text="清除", style="Flat.TButton", command=self._clear_log
        ).pack(side="left", padx=4, pady=4)
        ttk.Button(
            toolbar, text="匯出 LOG", style="Info.TButton", command=self._export_log
        ).pack(side="left", padx=4, pady=4)
        ttk.Checkbutton(toolbar, text="自動捲動", variable=self._log_auto_scroll).pack(
            side="left", padx=8
        )

        log_frame = tk.Frame(parent, bg=CLR_LOG_BG)
        log_frame.pack(fill="both", expand=True)
        self._log_text = tk.Text(
            log_frame,
            bg=CLR_LOG_BG,
            fg="#D4D4D4",
            font=("Consolas", 9),
            relief="flat",
            bd=0,
            state="disabled",
            wrap="none",
        )
        lsy = ttk.Scrollbar(log_frame, orient="vertical", command=self._log_text.yview)
        lsx = ttk.Scrollbar(
            log_frame, orient="horizontal", command=self._log_text.xview
        )
        self._log_text.configure(yscrollcommand=lsy.set, xscrollcommand=lsx.set)
        lsy.pack(side="right", fill="y")
        lsx.pack(side="bottom", fill="x")
        self._log_text.pack(fill="both", expand=True)

        def _mw(e):
            self._log_text.yview_scroll(int(-1 * (e.delta / 120)), "units")
            return "break"

        self._log_text.bind("<MouseWheel>", _mw)
        for tag, clr in [
            ("INFO", "#4EC9B0"),
            ("WARN", "#CE9178"),
            ("ERROR", "#F44747"),
            ("DEBUG", "#6A6A6A"),
        ]:
            self._log_text.tag_config(tag, foreground=clr)

    def _clear_log(self):
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")

    def _export_log(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All", "*.*")],
            initialfile=f"ds102_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
        )
        if path:
            self.ctrl.export_log(path)
            messagebox.showinfo("完成", f"LOG 已匯出:\n{path}")

    # =========================================================================
    # LOG 回調
    # =========================================================================
    def _on_log_entry(self, entry: dict):
        self.root.after(0, self._append_log_ui, entry)

    def _append_log_ui(self, entry: dict):
        level = entry.get("level", "INFO")
        ts = entry.get("ts", "")
        tx = f"[{entry['tx']}] " if entry.get("tx") else ""
        rx = f"→[{entry['rx']}] " if entry.get("rx") else ""
        line = f"{ts} [{level:<5}] {tx}{rx}{entry.get('msg','')}\n"
        if self._log_text:
            self._log_text.config(state="normal")
            self._log_text.insert("end", line, level)
            if self._log_auto_scroll.get():
                self._log_text.see("end")
            self._log_text.config(state="disabled")
        for sb in self._status_bars:
            sb.update_log(f"[{level}] {entry.get('msg','')}")
        self._update_stat_ui()

    # =========================================================================
    # 警報回調
    # =========================================================================
    def _on_alarm(self, title: str, msg: str):
        self.root.after(0, lambda: self._show_alarm(title, msg))

    def _show_alarm(self, title: str, msg: str):
        self.ctrl.stop()
        messagebox.showwarning(f"⚠️  {title}", msg)

    # =========================================================================
    # 連線 / EMS
    # =========================================================================
    def _scan_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self._port_cb["values"] = ports
        if ports and not self._port_var.get():
            self._port_var.set(ports[0])

    def _toggle_connect(self):
        if self.ctrl.connected:
            self.ctrl.disconnect()
            self._conn_dot.itemconfig(self._conn_dot_id, fill=CLR_DANGER)
            self._conn_lbl.config(text="未連線")
            self._conn_btn.config(text="連線", bg=CLR_ACCENT)
            self._fw_var.set("（未連線）")
            self._set_drive_buttons_state("disabled")
            self._set_axis_btns_state("disabled")
        else:
            port = self._port_var.get()
            baud = int(self._baud_var.get())
            if not port:
                messagebox.showerror("錯誤", "請選擇 COM Port")
                return
            self._conn_btn.config(text="連線中...", state="disabled", bg=CLR_WARN)
            self.root.update()

            def _do():
                ok, msg = self.ctrl.connect(port, baud)
                self.root.after(0, lambda: self._on_connect_result(ok, msg))

            threading.Thread(target=_do, daemon=True).start()

    def _on_connect_result(self, ok: bool, msg: str):
        self._conn_btn.config(state="normal")
        if ok:
            self._conn_dot.itemconfig(self._conn_dot_id, fill=CLR_ACCENT)
            self._conn_lbl.config(text=f"{self.ctrl.port} @ {self.ctrl.baudrate}")
            self._conn_btn.config(text="中斷", bg=CLR_DANGER)
            self._fw_var.set(f"韌體: {self.ctrl.firmware} | {self.ctrl.axis_count} 軸")
            self._set_drive_buttons_state("normal")
            for grp in self._all_axis_btn_groups:
                for ax, b in grp.items():
                    b.config(
                        state=(
                            "normal"
                            if int(AXIS_NO[ax]) <= self.ctrl.axis_count
                            else "disabled"
                        )
                    )
        else:
            self._conn_btn.config(text="連線", bg=CLR_ACCENT)
            messagebox.showerror("連線失敗", msg)

    def _start_sim(self):
        self.ctrl.connect_sim()
        self._conn_dot.itemconfig(self._conn_dot_id, fill=CLR_INFO)
        self._conn_lbl.config(text="模擬模式")
        self._conn_btn.config(text="中斷", bg=CLR_DANGER)
        self._fw_var.set("模擬模式 | 6 軸")
        self._set_drive_buttons_state("normal")
        self._set_axis_btns_state("normal")
        self._sim_tick()

    def _sim_tick(self):
        import random

        if self.ctrl.sim_mode:
            for ax in AXES:
                with self.ctrl._lock:
                    self.ctrl._positions_pulse[ax] += random.uniform(-2, 2)
            self.root.after(800, self._sim_tick)

    def _set_drive_buttons_state(self, state: str):
        for b in self._drive_buttons:
            try:
                b.config(state=state)
            except tk.TclError:
                pass

    def _set_axis_btns_state(self, state: str):
        for grp in self._all_axis_btn_groups:
            for b in grp.values():
                b.config(state=state)

    def _toggle_ems(self):
        if not self.ctrl.ems_active:
            self.ctrl.emergency_stop()
            self._ems_btn.config(text="✅ 解除緊急停止", bg="#2E7D32")
            self._set_drive_buttons_state("disabled")
        else:
            if messagebox.askyesno(
                "解除緊急停止",
                "請確認各軸已移離危險位置，\n且操作環境安全後再解除。\n\n確認解除？",
            ):
                self.ctrl.release_ems()
                self._ems_btn.config(text="⛔  緊急停止", bg=CLR_DANGER)
                if self.ctrl.connected:
                    self._set_drive_buttons_state("normal")

    # =========================================================================
    # 速度 Profile
    # =========================================================================
    def _save_profile_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("儲存速度 Profile")
        win.geometry("280x90")
        win.resizable(False, False)
        win.grab_set()
        tk.Label(win, text="Profile 名稱:").pack(pady=(10, 4))
        nv = tk.StringVar()
        ttk.Entry(win, textvariable=nv, width=24).pack()

        def _save():
            name = nv.get().strip()
            if not name:
                messagebox.showerror("錯誤", "請輸入名稱", parent=win)
                return
            l, f, r, s = self._get_spd()
            self.ctrl.save_speed_profile(name, l, f, r, s)
            self._refresh_profiles()
            win.destroy()

        ttk.Button(win, text="儲存", style="Accent.TButton", command=_save).pack(pady=8)

    def _load_profile(self, event=None):
        name = self._profile_var.get()
        p = self.ctrl.speed_profiles.get(name)
        if not p:
            return
        self._spd_vars["l_speed"].set(p["l_speed"])
        self._spd_vars["f_speed"].set(p["f_speed"])
        self._spd_vars["rate"].set(p["rate"])
        self._spd_vars["s_rate"].set(p["s_rate"])
        self.ctrl._log("INFO", f"載入速度 Profile [{name}]")

    def _delete_profile(self):
        name = self._profile_var.get()
        if not name:
            return
        if messagebox.askyesno("確認", f"刪除 Profile [{name}]？"):
            self.ctrl.delete_speed_profile(name)
            self._refresh_profiles()

    def _refresh_profiles(self):
        names = list(self.ctrl.speed_profiles.keys())
        self._profile_cb["values"] = names
        if names and not self._profile_var.get():
            self._profile_var.set(names[0])

    # =========================================================================
    # 軟體行程限制
    # =========================================================================
    def _apply_sw_limits(self):
        for ax, (ccw_v, cw_v) in self._lim_vars.items():
            try:
                ccw = float(ccw_v.get()) if ccw_v.get().strip() else None
            except ValueError:
                ccw = None
            try:
                cw = float(cw_v.get()) if cw_v.get().strip() else None
            except ValueError:
                cw = None
            self.ctrl.sw_limits[ax] = (ccw, cw)
            self.ctrl._log("INFO", f"軸 {ax} 軟體限制: CCW={ccw}, CW={cw}")
        messagebox.showinfo("完成", "軟體行程限制已套用")

    # =========================================================================
    # 實驗數據記錄
    # =========================================================================
    def _start_data_log(self):
        self.ctrl.start_data_log()
        self._dlog_status_var.set("🔴 記錄中")
        self._dlog_start_btn.state(["disabled"])
        self._dlog_stop_btn.state(["!disabled"])
        if "dlog" in self._stat_vars:
            self._stat_vars["dlog"].set("記錄中")

    def _stop_data_log(self):
        path = self.ctrl.stop_data_log()
        self._dlog_status_var.set("停止")
        self._dlog_start_btn.state(["!disabled"])
        self._dlog_stop_btn.state(["disabled"])
        if "dlog" in self._stat_vars:
            self._stat_vars["dlog"].set("停止")
        messagebox.showinfo("完成", f"數據已匯出:\n{path}")

    # =========================================================================
    # 儀表板狀態更新
    # =========================================================================
    def _update_stat_ui(self):
        if "conn" in self._stat_vars:
            self._stat_vars["conn"].set("已連線" if self.ctrl.connected else "未連線")
        if "axes" in self._stat_vars:
            self._stat_vars["axes"].set(
                str(self.ctrl.axis_count) if self.ctrl.connected else "—"
            )
        if "ems" in self._stat_vars:
            self._stat_vars["ems"].set("⚠️ EMS" if self.ctrl.ems_active else "正常")
        if "play" in self._stat_vars:
            self._stat_vars["play"].set(
                "🔴 重播中" if self.ctrl.playback_running else "閒置"
            )
        if self.ctrl.playback_running:
            self._set_drive_buttons_state("disabled")
        elif self.ctrl.connected and not self.ctrl.ems_active:
            self._set_drive_buttons_state("normal")

    # =========================================================================
    # 座標定時輪詢
    # =========================================================================
    def _start_poller(self):
        def _poll():
            unit = self._unit_var.get()
            dec = 0 if unit == "pulse" else (3 if unit == "um" else 6)
            pos_work = self.ctrl.positions
            for ax, lbl in self._dash_pos_vars.items():
                disp = self.ctrl.pulse_to_display(pos_work.get(ax, 0.0))
                lbl.set(f"{disp:,.{dec}f}")
            for sb in self._status_bars:
                sb.update_coords()
            self._update_stat_ui()
            self.root.after(300, _poll)

        self.root.after(300, _poll)

    # =========================================================================
    # 關閉
    # =========================================================================
    def _on_close(self):
        self._stop_playback.set()
        if self.ctrl.connected:
            self.ctrl.stop()
            self.ctrl.disconnect()
        auto = str(log_filename).replace(".log", "_history.txt")
        try:
            self.ctrl.export_log(auto)
        except Exception:
            pass
        self.root.destroy()


# =============================================================================
# 程式入口
# =============================================================================
def main():
    root = tk.Tk()
    DS102GUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
