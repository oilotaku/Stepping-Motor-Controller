import pyvisa
import time
import logging
import threading
from typing import Tuple

logger = logging.getLogger("DS102.meter")


# HP 8153A 在無光纖輸入或超出量程上限時，會回傳 IEEE-488.2 定義的
# Underflow/Overflow sentinel（例如 +9.9E+37 dBm）。這個數值可以被
# float() 正常解析，不會拋 ValueError——所以不能只靠 try/except 抓。
# 判斷式要看數值本身的量級。
_OVERFLOW_THRESHOLD = 1e30

# 連續 GPIB 通訊之間的節流延遲（秒）。見 CLAUDE.md／step-motor.txt：
# HP 8153A 內建處理器較慢，緊接著送指令或查詢會導致 GPIB 緩衝區溢位，
# 出現 Query INTERRUPTED。實際數值待接上真實儀器後校準，這裡先用
# step-motor.txt 記載的建議值起跳。
GPIB_THROTTLE_SEC = 0.03

# SYST:ERR? 回應本身只有代碼與（通常是空的）訊息字串，例如 '-230,""'，
# 沒有可讀文字。這裡對照官方手冊 Appendix I，只收 get_power() 失敗時
# 實際會遇到的代碼；查不到的代碼直接照原始回應顯示，不強行猜測。
_ERROR_DESCRIPTIONS = {
    "-230": "Data corrupt or stale（新量測還沒完成，或根本沒觸發過量測）",
    "-231": "Data questionable（量測精度可疑）",
    "-420": "Query UNTERMINATED（儀器沒有送出回應，量測子系統可能卡住）",
    "+510": "Head connection error（光學頭未連接——若此通道是 Head Interface 模組，需外接光學頭才能量測）",
}


def _describe_error(raw: str) -> str:
    """把 SYST:ERR? 的原始回應（例如 '-230,\"\"'）轉成帶說明的字串。"""
    code = raw.split(",", 1)[0].strip()
    desc = _ERROR_DESCRIPTIONS.get(code)
    return f"{raw} — {desc}" if desc else raw


class HP8153APowerMeter:
    def __init__(self, gpib_address: int, channel: int = 1, wavelength_nm: int = 1550):
        """
        初始化 HP 8153A
        :param gpib_address: GPIB 位址 (例如 22)
        :param channel: 1 代表 Slot A (通道1)，2 代表 Slot B (通道2)
        :param wavelength_nm: 設定量測波長，如 1310 或 1550
        """
        self.rm = pyvisa.ResourceManager()
        resource_str = f"GPIB0::{gpib_address}::INSTR"
        self.ch = channel  # 快取通道編號
        self._last_io = 0.0  # 上次通訊的時間戳，供節流使用
        self.last_error_detail = ""  # get_power() 失敗時的儀器端錯誤碼，供 GUI 顯示
        # GPIB 是獨立於 main_ai.py _serial_lock 的另一條物理匯流排
        # （RS-232 vs GPIB，沒有共享資源），這把鎖只保護本物件內部的
        # 存取，刻意不與 _serial_lock 巢狀取得，避免無謂的死鎖風險。
        self._lock = threading.RLock()

        try:
            self.instrument = self.rm.open_resource(resource_str)
            self.instrument.timeout = 3000  # 設為 3 秒防超時

            # 1. 驗證儀器連線
            idn = self._query("*IDN?")
            logger.info(f"成功連線到: {idn.strip()}")

            # 2. HP 8153A 初始化配置
            self._write(f":SENS{self.ch}:POW:UNIT DBM")  # 設為 dBm 單位
            # 縮寫必須是 WAVE（SCPI 大寫即必要縮寫），WAV 這三個字母對這台
            # 韌體是 Undefined Header（-113）——2026-08-12 實機查證，見手冊
            # Chapter 8 SENSe:POWer:WAVElength。
            self._write(f":SENS{self.ch}:POW:WAVE {wavelength_nm}NM")  # 設定波長

            # 3. 關鍵速度優化：在細對光前，建議將功率計鎖定在某一適當量程，關閉自動切換檔位
            #    若對光初期完全沒光(雜訊階段)，可先保留 ON，等抓到微弱光訊號時再由程式控制 OFF
            self._write(f":SENS{self.ch}:POW:RANG:AUTO OFF")
            self._write(f":SENS{self.ch}:POW:RANG -20DBM")  # 固定在 -20dBm 檔位

            logger.info(
                f"HP 8153A 通道 {self.ch} 初始化成功。波長: {wavelength_nm}nm, 已固定量程。"
            )

        except pyvisa.errors.VisaIOError as e:
            # GPIB 逾時、裝置忙碌、匯流排錯誤都會走這條——連線失敗要讓
            # 呼叫端明確知道，而不是留下一個半初始化的物件。
            logger.error(f"HP 8153A 連線或初始化失敗（VISA I/O 錯誤）: {e}")
            raise

    def _throttle(self) -> None:
        """
        連續通訊之間的節流。

        HP 8153A 內建處理器較慢，緊接著送指令會導致 GPIB 緩衝區溢位、
        出現 Query INTERRUPTED——這是 step-motor.txt 記載的既有教訓，
        但這支檔案原本完全沒有實作（import time 是死 import）。
        """
        elapsed = time.time() - self._last_io
        if elapsed < GPIB_THROTTLE_SEC:
            time.sleep(GPIB_THROTTLE_SEC - elapsed)
        self._last_io = time.time()

    def _write(self, cmd: str) -> None:
        with self._lock:
            self._throttle()
            self.instrument.write(cmd)

    def _query(self, cmd: str) -> str:
        with self._lock:
            self._throttle()
            return self.instrument.query(cmd)

    def get_power(self) -> Tuple[bool, float]:
        """
        讀取 HP 8153A 當前功率 (dBm)。

        回傳 (ok, value)：
          ok=True  → value 是有效讀值
          ok=False → value 為 0.0，呼叫端不可把它當成真實功率使用

        以前的介面是「失敗回傳 sentinel -99.0」，這個 sentinel 跟真實的
        極低功率讀值難以區分——尋光演算法若把「儀器讀值異常」誤判成
        「真的量到超低功率」，會被導向錯誤的搜尋方向。改成明確的
        (ok, value) 二元組，呼叫端無法忽略失敗這件事。
        """
        # 用 READ 而非 FETC：FETC 只抓「已存在」的讀值，這台儀器預設
        # INIT:CONT 是關的（實機查證，2026-08-12），沒有連續量測時 FETC
        # 永遠拿不到數據、GPIB 逾時。READ 會自己觸發一次量測再回傳，
        # 不依賴連續模式，channel B 實機驗證過可正常回應。
        try:
            raw_data = self._query(f":READ{self.ch}:POW?")
        except pyvisa.errors.VisaIOError as e:
            # 逾時、裝置忙碌、GPIB 匯流排錯誤——舊版完全沒接這個例外，
            # 會直接往上拋出呼叫端（例如尋光演算法的背景執行緒），
            # 若呼叫端也沒接就會讓整條執行緒靜默死掉，畫面卡住但沒有
            # 任何錯誤訊息。這裡收斂成正常的失敗回傳。
            logger.error(f"HP 8153A 讀取失敗（VISA I/O 錯誤）: {e}")
            self.last_error_detail = str(e)
            return False, 0.0

        try:
            value = float(raw_data.strip())
        except ValueError:
            logger.error(f"HP 8153A 回應無法解析為數值: {raw_data!r}")
            self.last_error_detail = f"回應無法解析: {raw_data!r}"
            return False, 0.0

        # IEEE-488.2 的 Underflow/Overflow sentinel（例如 +9.9E+37）
        # 可以被 float() 正常解析，不會拋例外，必須另外判斷量級。
        if abs(value) > _OVERFLOW_THRESHOLD:
            # 再查一次 SYST:ERR? 把儀器端的實際錯誤碼帶出來（例如
            # -230 Data corrupt or stale、+510 Head connection error），
            # 讓呼叫端能顯示比「已停止更新」更具體的原因。這裡失敗也
            # 不能讓 get_power() 本身跟著失敗——查錯誤碼只是錦上添花。
            try:
                self.last_error_detail = _describe_error(self._query("SYST:ERR?").strip())
            except pyvisa.errors.VisaIOError:
                self.last_error_detail = "overflow/underflow sentinel（查詢錯誤碼逾時）"
            return False, 0.0

        self.last_error_detail = ""
        return True, value

    def set_range_auto(self, status: bool):
        """動態開啟或關閉自動量程"""
        state = "ON" if status else "OFF"
        self._write(f":SENS{self.ch}:POW:RANG:AUTO {state}")

    def set_wavelength(self, wavelength_nm: int) -> None:
        """動態變更量測波長。"""
        self._write(f":SENS{self.ch}:POW:WAVE {wavelength_nm}NM")

    def set_range(self, dbm: float) -> None:
        """動態變更固定量程（手動量程模式下使用，需先關閉自動量程）。"""
        self._write(f":SENS{self.ch}:POW:RANG {dbm}DBM")

    def close(self):
        with self._lock:
            try:
                self.instrument.close()
            finally:
                self.rm.close()


if __name__ == "__main__":
    # 獨立執行本檔案時，logger 預設沒有 handler 不會有任何輸出——
    # 補一個 basicConfig 讓這支手動測試腳本維持可用。
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # 配置硬體連線
    power_meter = HP8153APowerMeter(gpib_address=22, channel=1, wavelength_nm=1550)

    power_meter.close()
    logger.info("資源已安全釋放。")
