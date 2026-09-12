param(
    [Parameter(Mandatory = $true)]
    [string]$PortableDataRoot,

    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")),
    [string]$PythonExe = "python",
    [switch]$IncludeCaches,
    [switch]$SkipRepositoryBundle
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath $RepoRoot).Path
$destination = [System.IO.Path]::GetFullPath($PortableDataRoot)
New-Item -ItemType Directory -Force -Path $destination | Out-Null
Set-Location -LiteralPath $repo
$python = if (Test-Path -LiteralPath $PythonExe) {
    (Resolve-Path -LiteralPath $PythonExe).Path
} else {
    (Get-Command $PythonExe -ErrorAction Stop).Source
}

$databases = @(
    "runtime/liquidation_history.db",
    "runtime/training_v6.db",
    "runtime/training_forward.db",
    "runtime/vision_metrics.db",
    "runtime/onchain_data.db",
    "runtime/cross_venue.db"
) | Where-Object { Test-Path -LiteralPath $_ }
$backupDir = Join-Path $destination "backups"
$statusPath = Join-Path $destination "data_backup_status.json"

& $python -m liquidity_signal.cli backup-data `
    --databases ($databases -join ',') `
    --artifacts "" `
    --output-dir $backupDir `
    --status-path $statusPath `
    --retain 3
if ($LASTEXITCODE -ne 0) {
    throw "Verified SQLite backup failed with exit code $LASTEXITCODE"
}

if ($IncludeCaches) {
    foreach ($cache in @(
        "cryptohft_liquidation_cache",
        "vision_cache",
        "vision_supplemental_cache",
        "cross_venue_cache"
    )) {
        $source = Join-Path $repo "runtime\$cache"
        if (Test-Path -LiteralPath $source) {
            $target = Join-Path $destination $cache
            robocopy $source $target /E /Z /FFT /R:2 /W:2 /NP | Out-Host
            if ($LASTEXITCODE -ge 8) {
                throw "robocopy failed for $cache with exit code $LASTEXITCODE"
            }
        }
    }
}

if (-not $SkipRepositoryBundle) {
    $branch = (& git -C $repo branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $branch) {
        throw "Could not identify the current Git branch"
    }
    $bundlePath = Join-Path $destination "AbyssIntuition.bundle"
    & git -C $repo bundle create $bundlePath $branch
    if ($LASTEXITCODE -ne 0) {
        throw "Git bundle creation failed with exit code $LASTEXITCODE"
    }
    & git bundle verify $bundlePath
    if ($LASTEXITCODE -ne 0) {
        throw "Git bundle verification failed with exit code $LASTEXITCODE"
    }
}

Write-Output "Portable backup completed at $destination"
