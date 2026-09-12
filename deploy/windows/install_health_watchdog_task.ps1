param(
    [string]$CliPath = "",
    [int]$IntervalMinutes = 15
)

$ErrorActionPreference = "Stop"
if ($IntervalMinutes -lt 5) {
    throw "IntervalMinutes must be at least 5."
}
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$watchdogScript = (Resolve-Path (Join-Path $PSScriptRoot "run_data_health_watchdog.ps1")).Path
if (-not $CliPath) {
    $CliPath = (Get-Command liquidity-signal -ErrorAction Stop).Source
}
$powerShellPath = (Get-Command powershell.exe -ErrorAction Stop).Source
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = (
    "-NoProfile -ExecutionPolicy Bypass -File `"$watchdogScript`" " +
    "-CliPath `"$CliPath`""
)
$action = New-ScheduledTaskAction `
    -Execute $powerShellPath `
    -Argument $arguments `
    -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask `
    -TaskName "AbyssIntuition-DataHealthWatchdog" `
    -Description "Audit data liveness and self-heal stale AbyssIntuition tasks." `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null
Write-Output "Installed AbyssIntuition-DataHealthWatchdog every $IntervalMinutes minutes."
