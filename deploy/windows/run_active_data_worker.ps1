param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("LiveMarket", "RecentHistory")]
    [string]$Task,
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [Parameter(Mandatory = $true)]
    [string]$DataRoot
)
$ErrorActionPreference = "Stop"
$env:PSModuleAnalysisCachePath = "NUL"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location -LiteralPath $repoRoot
$data = (Resolve-Path -LiteralPath $DataRoot).Path
$module = if ($Task -eq "LiveMarket") { "live_market" } else { "recent_history" }
$log = Join-Path $data "logs\$module.log"
"$(Get-Date -Format o) starting $module" | Tee-Object -FilePath $log -Append
& $PythonExe -B -u -m "liquidity_signal.data.$module" --data-root $data 2>&1 |
    Tee-Object -FilePath $log -Append
exit $LASTEXITCODE
