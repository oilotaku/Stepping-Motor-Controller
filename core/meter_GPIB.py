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

# 連續 GPIB 通訊之間的節流延遲（秒）。HP 8153A 內建處理器較慢，緊接著
# 送指令或查詢會 GPIB 緩衝區溢位、出現 Query INTERRUPTED（見
# step-motor.txt）。實際數值待接上真實儀器後校準，先用文件建議值起跳。
GPIB_THROTTLE_SEC = 0.03

# underrange/overrange sentinel 的 log 節流間隔（秒）。無光時每次讀值都
# 走 sentinel 分支，尋光一輪動輒上百次量測，逐筆記錄會灌爆 log，但完全
# 不記錄也不行——2026-08-26 實機就是這條路徑靜默，讓「尋光無動作」的
# 真正原因（量程鎖在 -20dBm 導致必定 underrange）查不出來，只能靠比對
# viRead 的 byte 數反推。折衷成節流記錄。
SENTINEL_LOG_INTERVAL_SEC = 5.0

# SYST:ERR? 回應只有代碼與（通常空的）訊息字串，例如 '-230,""'，沒有
# 可讀文字。對照官方手冊 Appendix I，只收 get_power() 實際會遇到的代碼；
# 查不到的代碼照原始回應顯示，不強行猜測。
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
    def __init__(
        self,
        gpib_address: int,
        channel: int = 1,
        wavelength_nm: int = 1550,
        range_auto: bool = True,
        range_dbm: float = -20.0,
    ):
        """
        初始化 HP 8153A

        :param gpib_address: GPIB 位址 (例如 22)
        :param channel: 1 代表 Slot A (通道1)，2 代表 Slot B (通道2)
        :param wavelength_nm: 設定量測波長，如 1310 或 1550
        :param range_auto: True＝自動量程（預設），False＝鎖定在 `range_dbm`
        :param range_dbm: `range_auto=False` 時使用的固定量程檔位

        🔴 `range_auto` 預設 True 是 2026-08-26 的行為變更。舊版無條件
        送 `RANG:AUTO OFF` + `RANG -20DBM`（理由是速度優化），但輸入功率
        低於 -20dBm 檔位下限時必定 underrange、`get_power()` 判為失敗。
        實機後果：尋光起點量不到值就被 `_search_axis_once()` 判定該步長
        已收斂，整個階段一空轉、滑台一步未移、82 個樣本全部無效，錯誤
        訊息只來自階段二、指向錯誤位置。尋光的起點本來就常常是無光的
        ——那正是要尋光的原因。所以正確性優先於速度：預設自動量程，
        要鎖定由呼叫端明確指定。
        """
        self.rm = pyvisa.ResourceManager()
        resource_str = f"GPIB0::{gpib_address}::INSTR"
        self.ch = channel  # 快取通道編號
        self._last_io = 0.0  # 上次通訊的時間戳，供節流使用
        self.last_error_detail = ""  # get_power() 失敗時的儀器端錯誤碼，供 GUI 顯示
        # 目前是否為自動量程；get_power() 讀到 sentinel 時據此判斷能否
        # 靠切回自動量程救回來，見該方法的自動退回邏輯。
        self._range_auto = bool(range_auto)
        self._last_sentinel_log = 0.0  # sentinel log 節流用的時間戳
        # GPIB 是獨立於 main_ai.py _serial_lock 的另一條物理匯流排，沒有
        # 共享資源，這把鎖只保護本物件內部存取，刻意不與 _serial_lock
        # 巢狀取得，避免無謂的死鎖風險。
        self._lock = threading.RLock()

        try:
            self.instrument = self.rm.open_resource(resource_str)
            self.instrument.timeout = 3000  # 設為 3 秒防超時

            # 1. 驗證儀器連線
            idn = self._query("*IDN?")
            logger.info(f"成功連線到: {idn.strip()}")

            # 2. HP 8153A 初始化配置
            self._write(f":SENS{self.ch}:POW:UNIT DBM")  # 設為 dBm 單位
            # 縮寫必須是 WAVE，WAV 對這台韌體是 Undefined Header（-113）
            # ——2026-08-12 實機查證，見手冊 Chapter 8 SENSe:POWer:WAVElength。
            self._write(f":SENS{self.ch}:POW:WAVE {wavelength_nm}NM")  # 設定波長

            # 3. 量程：預設自動，讓無光/微弱耦光的起點也讀得到真實底噪。
            #    鎖定量程比較快，但那是「已抓到光、功率量級穩定」之後才
            #    成立的最佳化（見 __init__ docstring 的實機事故）。要鎖定
            #    請由呼叫端在確認訊號之後呼叫 set_range_auto(False)。
            if self._range_auto:
                self._write(f":SENS{self.ch}:POW:RANG:AUTO ON")
                range_desc = "自動量程"
            else:
                self._write(f":SENS{self.ch}:POW:RANG:AUTO OFF")
                self._write(f":SENS{self.ch}:POW:RANG {range_dbm}DBM")
                range_desc = f"固定量程 {range_dbm}dBm"

            logger.info(
                f"HP 8153A 通道 {self.ch} 初始化成功。波長: {wavelength_nm}nm, {range_desc}。"
            )

        except pyvisa.errors.VisaIOError as e:
            # 逾時、裝置忙碌、匯流排錯誤都走這條——連線失敗要讓呼叫端
            # 明確知道，不留下半初始化的物件。
            logger.error(f"HP 8153A 連線或初始化失敗（VISA I/O 錯誤）: {e}")
            raise

    def _throttle(self) -> None:
        """
        連續通訊之間的節流。HP 8153A 內建處理器較慢，緊接著送指令會導致
        GPIB 緩衝區溢位、出現 Query INTERRUPTED（見 step-motor.txt）。
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

        以前的介面是「失敗回傳 sentinel -99.0」，跟真實的極低功率讀值難
        以區分——尋光演算法可能把「讀值異常」誤判成「量到超低功率」，
        導向錯誤的搜尋方向。改成明確的 (ok, value) 二元組，呼叫端無法
        忽略失敗這件事。
        """
        # 用 READ 而非 FETC：FETC 只抓已存在的讀值，這台儀器預設 INIT:CONT
        # 是關的（實機查證，2026-08-12），沒有連續量測時 FETC 永遠拿不到
        # 數據、GPIB 逾時。READ 會自己觸發一次量測再回傳。
        try:
            raw_data = self._query(f":READ{self.ch}:POW?")
        except pyvisa.errors.VisaIOError as e:
            # 逾時、裝置忙碌、GPIB 匯流排錯誤——不接住會讓呼叫端（例如
            # 尋光背景執行緒）整條靜默死掉，畫面卡住卻沒有錯誤訊息。
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
            # 再查一次 SYST:ERR? 把儀器端實際錯誤碼帶出來（例如 -230 Data
            # corrupt or stale、+510 Head connection error）；這裡查詢失敗
            # 不能讓 get_power() 本身跟著失敗，查錯誤碼只是錦上添花。
            try:
                self.last_error_detail = _describe_error(self._query("SYST:ERR?").strip())
            except pyvisa.errors.VisaIOError:
                self.last_error_detail = "overflow/underflow sentinel（查詢錯誤碼逾時）"

            # ── 手動量程的自動退回 ──
            # 鎖定量程是樂觀最佳化：一旦讀值跑出該檔位可量測範圍就必須
            # 放棄鎖定，否則會拿到一連串 sentinel。尋光收斂過程中這是
            # 必然會發生的——從底噪爬到耦合峰值可能跨 40dB 以上，鎖在
            # 剛偵測到微弱訊號的檔位，對準峰值就會 overrange。退回後不
            # 再自動鎖回去（要鎖由呼叫端重新決定），避免在檔位邊界反覆切換。
            if not self._range_auto:
                logger.warning(
                    f"HP 8153A 讀值超出固定量程可量測範圍（{self.last_error_detail}），"
                    "自動切回自動量程並重讀一次"
                )
                try:
                    self.set_range_auto(True)
                    raw_retry = self._query(f":READ{self.ch}:POW?")
                    retry_val = float(raw_retry.strip())
                except (pyvisa.errors.VisaIOError, ValueError) as e:
                    logger.error(f"HP 8153A 切回自動量程後重讀失敗: {e}")
                    return False, 0.0
                if abs(retry_val) <= _OVERFLOW_THRESHOLD:
                    self.last_error_detail = ""
                    return True, retry_val
                # 自動量程仍是 sentinel＝真的超出儀器能力（通常是無光），
                # 照常回報失敗，往下走節流記錄。

            self._log_sentinel(value)
            return False, 0.0

        self.last_error_detail = ""
        return True, value

    def _log_sentinel(self, value: float) -> None:
        """
        節流記錄 underrange/overrange sentinel。無光時每次讀值都會走到
        這條路徑，逐筆記錄會灌爆 log，但完全不記錄又會事後查不出原因
        （2026-08-26 實機事故就是這樣）。折衷成每
        SENTINEL_LOG_INTERVAL_SEC 記一次。
        """
        now = time.time()
        if now - self._last_sentinel_log < SENTINEL_LOG_INTERVAL_SEC:
            return
        self._last_sentinel_log = now
        mode = "自動量程" if self._range_auto else "固定量程"
        logger.warning(
            f"HP 8153A 回傳 underrange/overrange sentinel（{value:.3E}，{mode}）："
            f"{self.last_error_detail or '無錯誤碼'}。"
            "輸入功率超出目前可量測範圍——最常見的原因是根本沒有光耦合進來。"
            f"（此訊息每 {SENTINEL_LOG_INTERVAL_SEC:.0f} 秒最多記錄一次）"
        )

    def set_range_auto(self, status: bool):
        """
        動態開啟或關閉自動量程，同步更新 `self._range_auto`——get_power()
        的自動退回邏輯靠這個旗標判斷能否切回自動量程救回來，繞過這個
        方法直接送 SCPI 會讓旗標與儀器實際狀態脫節。
        """
        state = "ON" if status else "OFF"
        self._write(f":SENS{self.ch}:POW:RANG:AUTO {state}")
        self._range_auto = bool(status)

    def set_wavelength(self, wavelength_nm: int) -> None:
        """動態變更量測波長。"""
        self._write(f":SENS{self.ch}:POW:WAVE {wavelength_nm}NM")

    def set_range(self, dbm: float) -> None:
        """動態變更固定量程（手動量程模式下使用，需先關閉自動量程）。"""
        self._write(f":SENS{self.ch}:POW:RANG {dbm}DBM")

    @property
    def range_auto(self) -> bool:
        """目前是否為自動量程（供 GUI 顯示與尋光演算法判斷用）。"""
        return self._range_auto

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
