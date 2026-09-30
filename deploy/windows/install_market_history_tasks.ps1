param(
    [Parameter(Mandatory = $true)] [string]$DataRoot,
    [string]$PythonExe = "",
    [switch]$StartWorkers
)
$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not $PythonExe) { $PythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe" }
$python = (Resolve-Path -LiteralPath $PythonExe).Path
$data = (Resolve-Path -LiteralPath $DataRoot).Path
$worker = (Resolve-Path (Join-Path $PSScriptRoot "run_active_data_worker.ps1")).Path
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
foreach ($kind in @("LiveMarket", "RecentHistory")) {
    $taskName = "AbyssIntuition-$kind"
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing -and $existing.State -eq "Running") { throw "$taskName is already running" }
    $arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$worker`" -Task $kind -PythonExe `"$python`" -DataRoot `"$data`""
    $action = New-ScheduledTaskAction -Execute (Get-Command powershell.exe).Source -Argument $arguments -WorkingDirectory $repoRoot
    if ($kind -eq "LiveMarket") {
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
        $limit = [TimeSpan]::Zero
    } else {
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(1) -RepetitionInterval (New-TimeSpan -Hours 1)
        $limit = New-TimeSpan -Hours 4
    }
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -WakeToRun -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 2) -ExecutionTimeLimit $limit
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    if ($StartWorkers) { Start-ScheduledTask -TaskName $taskName }
    Write-Output "Installed $taskName; data=$data; python=$python"
}
