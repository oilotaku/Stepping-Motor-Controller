import pyvisa
import time


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

        try:
            self.instrument = self.rm.open_resource(resource_str)
            self.instrument.timeout = 3000  # 設為 3 秒防超時

            # 1. 驗證儀器連線
            idn = self.instrument.query("*IDN?")
            print(f"成功連線到: {idn.strip()}")

            # 2. HP 8153A 初始化配置
            self.instrument.write(f":SENS{self.ch}:POW:UNIT DBM")  # 設為 dBm 單位
            self.instrument.write(
                f":SENS{self.ch}:POW:WAV {wavelength_nm}NM"
            )  # 設定波長

            # 3. 關鍵速度優化：在細對光前，建議將功率計鎖定在某一適當量程，關閉自動切換檔位
            #    若對光初期完全沒光(雜訊階段)，可先保留 ON，等抓到微弱光訊號時再由程式控制 OFF
            self.instrument.write(f":SENS{self.ch}:POW:RANG:AUTO OFF")
            self.instrument.write(
                f":SENS{self.ch}:POW:RANG -20DBM"
            )  # 固定在 -20dBm 檔位 

            print(
                f"HP 8153A 通道 {self.ch} 初始化成功。波長: {wavelength_nm}nm, 已固定量程。"
            )

        except Exception as e:
            print(f"HP 8153A 連線或初始化失敗: {e}")
            raise

    def get_power(self) -> float:
        """讀取 HP 8153A 當前功率 (dBm)"""
        try:
            # 針對 HP 8153A 的標準讀取指令
            raw_data = self.instrument.query(f":FETC{self.ch}:POW?")
            return float(raw_data.strip())
        except ValueError:
            # 當無光纖輸入或超出量程上限時，HP 8153A 可能會回傳大於或小於極限的值（例如 9.9E+37 等代表 Underflow/Overflow）
            return -99.0

    def set_range_auto(self, status: bool):
        """動態開啟或關閉自動量程"""
        state = "ON" if status else "OFF"
        self.instrument.write(f":SENS{self.ch}:POW:RANG:AUTO {state}")

    def close(self):
        self.instrument.close()
        self.rm.close()


if __name__ == "__main__":
    # 配置硬體連線
    power_meter = HP8153APowerMeter(gpib_address=22, channel=1, wavelength_nm=1550)

    power_meter.close()
    print("資源已安全釋放。")
