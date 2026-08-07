import pyvisa
import time
from typing import Tuple


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

        try:
            self.instrument = self.rm.open_resource(resource_str)
            self.instrument.timeout = 3000  # 設為 3 秒防超時

            # 1. 驗證儀器連線
            idn = self._query("*IDN?")
            print(f"成功連線到: {idn.strip()}")

            # 2. HP 8153A 初始化配置
            self._write(f":SENS{self.ch}:POW:UNIT DBM")  # 設為 dBm 單位
            self._write(f":SENS{self.ch}:POW:WAV {wavelength_nm}NM")  # 設定波長

            # 3. 關鍵速度優化：在細對光前，建議將功率計鎖定在某一適當量程，關閉自動切換檔位
            #    若對光初期完全沒光(雜訊階段)，可先保留 ON，等抓到微弱光訊號時再由程式控制 OFF
            self._write(f":SENS{self.ch}:POW:RANG:AUTO OFF")
            self._write(f":SENS{self.ch}:POW:RANG -20DBM")  # 固定在 -20dBm 檔位

            print(
                f"HP 8153A 通道 {self.ch} 初始化成功。波長: {wavelength_nm}nm, 已固定量程。"
            )

        except pyvisa.errors.VisaIOError as e:
            # GPIB 逾時、裝置忙碌、匯流排錯誤都會走這條——連線失敗要讓
            # 呼叫端明確知道，而不是留下一個半初始化的物件。
            print(f"HP 8153A 連線或初始化失敗（VISA I/O 錯誤）: {e}")
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
        self._throttle()
        self.instrument.write(cmd)

    def _query(self, cmd: str) -> str:
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
        try:
            raw_data = self._query(f":FETC{self.ch}:POW?")
        except pyvisa.errors.VisaIOError as e:
            # 逾時、裝置忙碌、GPIB 匯流排錯誤——舊版完全沒接這個例外，
            # 會直接往上拋出呼叫端（例如尋光演算法的背景執行緒），
            # 若呼叫端也沒接就會讓整條執行緒靜默死掉，畫面卡住但沒有
            # 任何錯誤訊息。這裡收斂成正常的失敗回傳。
            print(f"HP 8153A 讀取失敗（VISA I/O 錯誤）: {e}")
            return False, 0.0

        try:
            value = float(raw_data.strip())
        except ValueError:
            print(f"HP 8153A 回應無法解析為數值: {raw_data!r}")
            return False, 0.0

        # IEEE-488.2 的 Underflow/Overflow sentinel（例如 +9.9E+37）
        # 可以被 float() 正常解析，不會拋例外，必須另外判斷量級。
        if abs(value) > _OVERFLOW_THRESHOLD:
            return False, 0.0

        return True, value

    def set_range_auto(self, status: bool):
        """動態開啟或關閉自動量程"""
        state = "ON" if status else "OFF"
        self._write(f":SENS{self.ch}:POW:RANG:AUTO {state}")

    def close(self):
        try:
            self.instrument.close()
        finally:
            self.rm.close()


if __name__ == "__main__":
    # 配置硬體連線
    power_meter = HP8153APowerMeter(gpib_address=22, channel=1, wavelength_nm=1550)

    power_meter.close()
    print("資源已安全釋放。")
