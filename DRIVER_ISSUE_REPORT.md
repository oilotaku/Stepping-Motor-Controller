# DS112 無法使用 — 根本原因：SentinelOne Device Control 封鎖

> **狀態：已解決（2026-07-31）**
> Device Control 政策已核可此裝置，如本文預期般無需重裝驅動即恢復。
> 驗證：`probe_ds102.py` 認出 `COM4  0DFD:0002`，`*IDN?` 回應
> `SURUGA,DS102,0,VER4.00` @ 38400。
> 以下內容保留作為排查紀錄，若日後政策變動導致同樣症狀可直接沿用。

日期：2026-07-30
機器：Windows 11 Pro 10.0.26200

## 結論

實驗室設備 **SURUGA SEIKI DS112 步進馬達控制器**的 USB 連線被
**SentinelOne Device Control 封鎖**。驅動程式本身完全正常。

`SentinelOne/Operational` 事件記錄（事件 ID 77）：

```
USB device SURUGA SEIKI SURUGA SEIKI DS102 was blocked | Class: 0ffh
```

每一次接上或重試都產生一筆，時間與裝置啟動失敗完全對應：
11:52:20、11:59:52、12:02:12、12:18:25、12:21:46、12:23:01、12:29:42。

## 請求

請在 SentinelOne 管理主控台的 Device Control 政策中核可此裝置：

```
硬體 ID   : USB\VID_0DFD&PID_0002
USB Class : 0xFF（vendor-specific，FTDI 晶片）
裝置名稱  : SURUGA SEIKI DS102 / DS112 步進馬達控制器
用途      : 實驗室量測設備，由 Python 程式控制
```

核可後裝置應立即可用，無需重裝驅動。

## 機制

`SentinelDeviceControl.sys`（25.2.6.442，StartMode=Boot，Running）
註冊為**下層篩選器**，同時掛在兩個裝置類別上：

```
USB   類別 {36fc9e60-c465-11cf-8056-444553540000}  LowerFilters: {SentinelDeviceControl}
Ports 類別 {4d36e978-e325-11ce-bfc1-08002be10318}  LowerFilters: {SentinelDeviceControl}
```

下層篩選器位於功能驅動之下。政策未核可裝置時，它使該裝置的啟動 IRP
失敗，PnP 回報為：

```
Problem Code  : 10          (CM_PROB_FAILED_START)
Problem Status: 0xC000036C  (STATUS_DRIVER_BLOCKED)
```

這個狀態碼容易誤導——它看起來像簽章或 Code Integrity 問題，實際上是
裝置層級被篩選器攔下。

## 已排除的原因（供參考，避免重複排查）

1. **驅動程式正常** — `sc start ftdibus` 回報 `STATE: 4 RUNNING`，
   驅動映像成功載入核心。
2. **簽章正常** — `ftdibus.sys` / `ftser2k.sys` 均為 FTDI CDM 2.12.36.20，
   簽章者 `CN=Microsoft Windows Hardware Compatibility Publisher`，
   Authenticode `Valid`，並有 `CN=Microsoft Time-Stamp Service` 時間戳記。
3. **驅動安裝正確** — 兩層都已正確綁定：

   ```
   USB\VID_0DFD&PID_0002              → oem57.inf  USB Serial Converter (Class=USB)
     └─ FTDIBUS\COMPORT&VID_0DFD...   → oem58.inf  USB Serial Port      (Class=Ports)
   ```

   （因駿河精機使用自訂 VID/PID，FTDI 官方 INF 不會自動比對，
   需在裝置管理員取消勾選「顯示相容硬體」手動指派。）
4. **不是 WDAC 政策** — `CiTool --list-policies` 顯示強制執行中的只有
   微軟自家平台政策，無任何公司自訂政策。
5. **不是 HVCI／記憶體完整性** — 驅動在 HVCI 開啟狀態下成功載入（見第 1 點）。
6. **不是 Secure Boot 或驅動簽章強制** — 已測試開機時停用驅動簽章強制，
   結果不變。
7. **不是裝置安裝限制原則** — `HKLM:\SOFTWARE\Policies\Microsoft\Windows\
   DeviceInstall` 不存在。

## 注意：USB-RS232 轉接線不是可行的替代方案

`SentinelDeviceControl` 同時篩選 **Ports 類別**，因此任何 USB 序列轉接線
（FTDI、CH340、PL2303，或 CDC-ACM 類別相容者）都會經過同一個政策評估，
可能同樣被封鎖。唯一的解法是在 Device Control 政策中核可裝置。

## 程式端狀態

已完成，無需再改。`test.py` 的 `find_ds_port()` 以 VID/PID
`0x0DFD/0x0002` 辨識 USB 裝置，找不到時跳過 Intel AMT SOL 等非儀器埠、
對其餘候選輪詢 `*IDN?` 並依序嘗試 38400/19200/9600/4800。

解除封鎖後驗證：

```
venv\Scripts\python.exe probe_ds102.py
```

預期看到 COM 埠被以 VID/PID 認出，且 `*IDN?` 回應 `SURUGA,DS1...`。
