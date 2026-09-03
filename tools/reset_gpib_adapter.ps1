<#
.SYNOPSIS
    重置 NI GPIB-USB-HS 轉接器（VID_3923&PID_709B），修復「HP 8153A 斷電重開後
    NI-VISA 抓不到 GPIB0」的狀況，效果等同拔插 USB 線，但不用真的動線。

.DESCRIPTION
    症狀：儀器（HP 8153A）斷電、重開機，或匯流排異常斷線後，
    pyvisa 的 rm.list_resources() 看不到任何 GPIB0::*::INSTR，
    但裝置管理員裡 "NI GPIB-USB-HS" 的狀態顯示正常（OK）。
    這是轉接器內部的 GPIB 邏輯層卡住，不是裝置本身壞掉。

    本腳本用 Disable-PnpDevice + Enable-PnpDevice 強迫 Windows 對這個
    USB 裝置送出重置訊號，效果跟拔插線一樣，但不需要找到實體轉接器。

    需要系統管理員權限，若目前不是以系統管理員身分執行，會自動跳出
    UAC 提示要求授權（不會靜默略過）。

.NOTES
    2026-08-12 實機驗證：軟體重置後 GPIB0::21::INSTR 立即恢復，
    *IDN? 回應正常。
#>

$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)

if (-not $isAdmin) {
    Write-Output "需要系統管理員權限，重新以提升權限啟動（會跳 UAC，請點「是」）..."
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"" -Wait
    exit
}

$device = Get-PnpDevice | Where-Object { $_.InstanceId -like "USB\VID_3923&PID_709B*" }

if (-not $device) {
    Write-Output "找不到 NI GPIB-USB-HS（VID_3923&PID_709B）。請確認轉接器已插上 USB。"
    exit 1
}

Write-Output "找到裝置：$($device.FriendlyName)　目前狀態：$($device.Status)"
Write-Output "停用中..."
Disable-PnpDevice -InstanceId $device.InstanceId -Confirm:$false
Start-Sleep -Seconds 2

Write-Output "重新啟用中..."
Enable-PnpDevice -InstanceId $device.InstanceId -Confirm:$false
Start-Sleep -Seconds 2

$after = Get-PnpDevice -InstanceId $device.InstanceId
Write-Output "重置後狀態：$($after.Status)　$($after.Problem)"
Write-Output ""
Write-Output "接下來可以用下面這行確認 NI-VISA 是否看得到 GPIB0："
Write-Output "  venv\Scripts\python.exe -c ""import pyvisa; print(pyvisa.ResourceManager().list_resources())"""
