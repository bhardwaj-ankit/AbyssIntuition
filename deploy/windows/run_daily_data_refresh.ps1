param(
    [Parameter(Mandatory = $true)]
    [string]$CliPath,
    [string]$DataRoot = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$runtimeDirectory = if ($DataRoot) {
    [System.IO.Path]::GetFullPath($DataRoot)
} else {
    Join-Path $repoRoot "runtime"
}
$logDirectory = Join-Path $runtimeDirectory "logs"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logPath = Join-Path $logDirectory "daily_data_refresh.log"
$liquidationDb = Join-Path $runtimeDirectory "liquidation_history.db"
$marketDb = Join-Path $runtimeDirectory "vision_metrics.db"
$onchainDb = Join-Path $runtimeDirectory "onchain_data.db"
$crossVenueDb = Join-Path $runtimeDirectory "cross_venue.db"
$trainingDb = Join-Path $runtimeDirectory "training_forward.db"
$completenessPath = Join-Path $runtimeDirectory "data_completeness_forward.json"
$refreshPath = Join-Path $runtimeDirectory "daily_refresh_status.json"
$backupPath = Join-Path $runtimeDirectory "data_backup_status.json"
$healthPath = Join-Path $runtimeDirectory "data_operations_health.json"
$backupDirectory = Join-Path $runtimeDirectory "backups"
Set-Location -LiteralPath $repoRoot

"$(Get-Date -Format o) starting free-source daily refresh" |
    Tee-Object -FilePath $logPath -Append
& $CliPath daily-data-refresh `
    --retry-days 3 `
    --forward-lookback-hours 36 `
    --market-db $marketDb `
    --onchain-db $onchainDb `
    --liquidation-db $liquidationDb `
    --cross-venue-db $crossVenueDb `
    --forward-training-db $trainingDb `
    --completeness-path $completenessPath `
    --report-path $refreshPath 2>&1 |
    Tee-Object -FilePath $logPath -Append
$refreshExitCode = $LASTEXITCODE
"$(Get-Date -Format o) daily refresh exited with code $refreshExitCode" |
    Tee-Object -FilePath $logPath -Append
& $CliPath backup-data `
    --databases "$liquidationDb,$trainingDb,$marketDb,$onchainDb,$crossVenueDb" `
    --artifacts "$refreshPath,$completenessPath,$healthPath" `
    --output-dir $backupDirectory `
    --status-path $backupPath 2>&1 |
    Tee-Object -FilePath $logPath -Append
$backupExitCode = $LASTEXITCODE
"$(Get-Date -Format o) data backup exited with code $backupExitCode" |
    Tee-Object -FilePath $logPath -Append
& $CliPath data-operations-health `
    --liquidation-db $liquidationDb `
    --refresh-report-path $refreshPath `
    --backup-status-path $backupPath `
    --output-path $healthPath 2>&1 |
    Tee-Object -FilePath $logPath -Append
$healthExitCode = $LASTEXITCODE
"$(Get-Date -Format o) operations health exited with code $healthExitCode" |
    Tee-Object -FilePath $logPath -Append
if ($refreshExitCode -ne 0) {
    exit $refreshExitCode
}
if ($healthExitCode -ne 0) {
    exit $healthExitCode
}
exit $backupExitCode
