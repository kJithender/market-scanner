<#
.SYNOPSIS
    Registers (or updates) the daily 06:33 BlowingStocksScreener task.

.DESCRIPTION
    Creates a Windows scheduled task named MarketScanner-BlowingStocks that
    runs scripts\run_blowing_stocks.ps1 every day at 06:33 local time.

    06:33 Pacific is 09:33 Eastern — three minutes after the opening bell. With
    the default fifteen-minute delayed tape the measurement window closes near
    09:18 ET, so the run screens the completed premarket session while the
    opening drive is still live.

    Every day, not weekdays: the task is cheap on a closed market (one batched
    request establishes there is no session to screen) and running it daily
    means a holiday schedule never has to be maintained here.

    StartWhenAvailable is set so a machine that was asleep at 06:33 still runs
    the screen when it wakes. It does not wake the machine by itself.

    Re-running this script updates the existing task in place. Remove it with:

        Unregister-ScheduledTask -TaskName "MarketScanner-BlowingStocks" -Confirm:$false
#>
[CmdletBinding()]
param(
    [string]$Time = "06:33",
    [string]$TaskName = "MarketScanner-BlowingStocks"
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
$script = Join-Path $projectDir "scripts\run_blowing_stocks.ps1"
if (-not (Test-Path $script)) { throw "Not found: $script" }

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $projectDir

$trigger = New-ScheduledTaskTrigger -Daily -At $Time

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

$description = "BlowingStocksScreener: low-float momentum and catalyst breakout screens, " +
               "daily at $Time local (09:33 ET). Keeps one week of dated reports."

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description $description `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Output ("Registered '{0}' ({1}); next run {2}" -f $TaskName, $task.State, $info.NextRunTime)
