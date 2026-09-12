param(
    [Parameter(Mandatory = $true)]
    [string]$CliPath
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$runtimeDirectory = Join-Path $repoRoot "runtime"
$logDirectory = Join-Path $runtimeDirectory "logs"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logPath = Join-Path $logDirectory "data_health_watchdog.log"
$healthPath = Join-Path $runtimeDirectory "data_operations_health.json"
$auditPath = Join-Path $runtimeDirectory "watchdog_interventions.jsonl"
Set-Location -LiteralPath $repoRoot

& $CliPath data-operations-health --no-fail-on-unhealthy 2>&1 |
    Tee-Object -FilePath $logPath -Append
$healthCommandExitCode = $LASTEXITCODE
$action = "health_command_failed"
$actionSucceeded = $false
$details = $null

if (Test-Path -LiteralPath $healthPath) {
    $health = Get-Content -LiteralPath $healthPath -Raw | ConvertFrom-Json
    $action = [string]$health.recommended_action
    switch ($action) {
        "restart_collector" {
            Stop-ScheduledTask -TaskName "AbyssIntuition-LiquidationCollector" -ErrorAction SilentlyContinue
            Start-ScheduledTask -TaskName "AbyssIntuition-LiquidationCollector"
            $actionSucceeded = $true
            $details = "Collector task restarted because one or more streams were stale."
        }
        "rerun_daily_refresh" {
            $refreshTask = Get-ScheduledTask -TaskName "AbyssIntuition-DailyDataRefresh"
            if ($refreshTask.State -ne "Running") {
                Start-ScheduledTask -TaskName "AbyssIntuition-DailyDataRefresh"
                $details = "Daily refresh task started after failed or overdue status."
            }
            else {
                $details = "Daily refresh task was already running."
            }
            $actionSucceeded = $true
        }
        "record_continuity_gap" {
            $actionSucceeded = $true
            $details = "Continuity gap recorded; fresh streams were not restarted."
        }
        "none" {
            $actionSucceeded = $true
            $details = "All operational checks passed."
        }
        default {
            $details = "Health command did not produce a recognized action."
        }
    }
}

$record = [ordered]@{
    checked_at = (Get-Date).ToUniversalTime().ToString("o")
    action = $action
    action_succeeded = $actionSucceeded
    health_command_exit_code = $healthCommandExitCode
    details = $details
}
($record | ConvertTo-Json -Compress) | Add-Content -LiteralPath $auditPath -Encoding UTF8
($record | ConvertTo-Json) | Tee-Object -FilePath $logPath -Append
if (-not $actionSucceeded) {
    exit 1
}
exit 0
