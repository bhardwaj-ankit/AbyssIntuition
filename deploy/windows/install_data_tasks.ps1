param(
    [string]$DailyRefreshTime = "04:30",
    [switch]$StartCollector
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$collectorScript = (Resolve-Path (Join-Path $PSScriptRoot "run_liquidation_collector.ps1")).Path
$refreshScript = (Resolve-Path (Join-Path $PSScriptRoot "run_daily_data_refresh.ps1")).Path
$cli = Get-Command liquidity-signal -ErrorAction Stop
$cliPath = $cli.Source
$powerShellPath = (Get-Command powershell.exe -ErrorAction Stop).Source
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$collectorArguments = (
    "-NoProfile -ExecutionPolicy Bypass -File `"$collectorScript`" " +
    "-CliPath `"$cliPath`""
)
$refreshArguments = (
    "-NoProfile -ExecutionPolicy Bypass -File `"$refreshScript`" " +
    "-CliPath `"$cliPath`""
)
$collectorAction = New-ScheduledTaskAction `
    -Execute $powerShellPath `
    -Argument $collectorArguments `
    -WorkingDirectory $repoRoot
$refreshAction = New-ScheduledTaskAction `
    -Execute $powerShellPath `
    -Argument $refreshArguments `
    -WorkingDirectory $repoRoot
$collectorTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$refreshTrigger = New-ScheduledTaskTrigger -Daily -At $DailyRefreshTime
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited
$collectorSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$refreshSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)

Register-ScheduledTask `
    -TaskName "AbyssIntuition-LiquidationCollector" `
    -Description "Continuously capture free Binance and Bybit liquidation events." `
    -Action $collectorAction `
    -Trigger $collectorTrigger `
    -Principal $principal `
    -Settings $collectorSettings `
    -Force | Out-Null
Register-ScheduledTask `
    -TaskName "AbyssIntuition-DailyDataRefresh" `
    -Description "Refresh free archives and grow the strict forward training cohort." `
    -Action $refreshAction `
    -Trigger $refreshTrigger `
    -Principal $principal `
    -Settings $refreshSettings `
    -Force | Out-Null

& (Join-Path $PSScriptRoot "install_health_watchdog_task.ps1") `
    -CliPath $cliPath `
    -IntervalMinutes 15

if ($StartCollector) {
    Start-ScheduledTask -TaskName "AbyssIntuition-LiquidationCollector"
}

Write-Output "Installed AbyssIntuition-LiquidationCollector at logon."
Write-Output "Installed AbyssIntuition-DailyDataRefresh daily at $DailyRefreshTime local time."
Write-Output "Installed AbyssIntuition-DataHealthWatchdog every 15 minutes."
Write-Output "CLI: $cliPath"
