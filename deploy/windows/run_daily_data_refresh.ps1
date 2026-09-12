param(
    [Parameter(Mandatory = $true)]
    [string]$CliPath
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$logDirectory = Join-Path $repoRoot "runtime\logs"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logPath = Join-Path $logDirectory "daily_data_refresh.log"
Set-Location -LiteralPath $repoRoot

"$(Get-Date -Format o) starting free-source daily refresh" |
    Tee-Object -FilePath $logPath -Append
& $CliPath daily-data-refresh `
    --retry-days 3 `
    --forward-lookback-hours 36 2>&1 |
    Tee-Object -FilePath $logPath -Append
$refreshExitCode = $LASTEXITCODE
"$(Get-Date -Format o) daily refresh exited with code $refreshExitCode" |
    Tee-Object -FilePath $logPath -Append
& $CliPath backup-data 2>&1 |
    Tee-Object -FilePath $logPath -Append
$backupExitCode = $LASTEXITCODE
"$(Get-Date -Format o) data backup exited with code $backupExitCode" |
    Tee-Object -FilePath $logPath -Append
& $CliPath data-operations-health 2>&1 |
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
