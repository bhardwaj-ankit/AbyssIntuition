param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("cryptohft", "market", "training")]
    [string]$Task,

    [Parameter(Mandatory = $true)]
    [string]$DataRoot,

    [string]$RepoRoot = "",
    [string]$PythonExe = "python",
    [string]$HistoryStart = "2025-06-28",
    [string]$ArchiveEndExclusive = "",
    [string]$MarketEnd = ""
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) {
    $RepoRoot = Join-Path $PSScriptRoot "..\.."
}
$repo = (Resolve-Path -LiteralPath $RepoRoot).Path
$data = [System.IO.Path]::GetFullPath($DataRoot)
New-Item -ItemType Directory -Force -Path $data | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $data "logs") | Out-Null
Set-Location -LiteralPath $repo

$python = if (Test-Path -LiteralPath $PythonExe) {
    (Resolve-Path -LiteralPath $PythonExe).Path
} else {
    (Get-Command $PythonExe -ErrorAction Stop).Source
}

$symbols = "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,NEARUSDT,PEPEUSDT"
$startDate = [datetime]::ParseExact(
    $HistoryStart,
    "yyyy-MM-dd",
    [Globalization.CultureInfo]::InvariantCulture
)

function Invoke-LiquiditySignal {
    param([string[]]$Arguments)
    & $python -m liquidity_signal.cli @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "liquidity-signal failed with exit code ${LASTEXITCODE}: $Arguments"
    }
}

if ($Task -eq "cryptohft") {
    if (-not $ArchiveEndExclusive) {
        $now = [datetimeoffset]::UtcNow
        $safeHour = [datetimeoffset]::new(
            $now.Year, $now.Month, $now.Day, $now.Hour, 0, 0, [timespan]::Zero
        ).AddHours(-1)
        $ArchiveEndExclusive = $safeHour.ToString("yyyy-MM-ddTHH:00:00Z")
    }
    Invoke-LiquiditySignal @(
        "ingest-cryptohft-liquidations",
        "--start", "$HistoryStart`T00:00:00Z",
        "--end", $ArchiveEndExclusive,
        "--symbols", $symbols,
        "--venues", "binance,bybit",
        "--db-path", (Join-Path $data "liquidation_history.db"),
        "--cache-dir", (Join-Path $data "cryptohft_liquidation_cache"),
        "--report-path", (Join-Path $data "cryptohft_full_history_status.json"),
        "--requests-per-minute", "60",
        "--download-workers", "8"
    )
}

if ($Task -eq "market") {
    if (-not $MarketEnd) {
        $MarketEnd = [datetime]::UtcNow.AddDays(-1).ToString("yyyy-MM-dd")
    }
    $visionDb = Join-Path $data "vision_metrics.db"
    Invoke-LiquiditySignal @(
        "ingest-vision-metrics", "--symbols", $symbols,
        "--start", $HistoryStart, "--end", $MarketEnd,
        "--db-path", $visionDb,
        "--cache-dir", (Join-Path $data "vision_cache")
    )
    Invoke-LiquiditySignal @(
        "ingest-open-onchain", "--start", $HistoryStart, "--end", $MarketEnd,
        "--db-path", (Join-Path $data "onchain_data.db")
    )
    Invoke-LiquiditySignal @(
        "ingest-vision-supplemental", "--symbols", $symbols,
        "--start", $HistoryStart, "--end", $MarketEnd,
        "--db-path", $visionDb,
        "--cache-dir", (Join-Path $data "vision_supplemental_cache")
    )
}

if ($Task -eq "training") {
    $lookbackHours = [math]::Ceiling(
        ([datetime]::UtcNow - $startDate).TotalHours
    )
    $maxSamples = $lookbackHours + 24
    foreach ($symbol in $symbols.Split(',')) {
        Invoke-LiquiditySignal @(
            "backfill-training", "--symbol", $symbol,
            "--lookback-hours", "$lookbackHours",
            "--step-minutes", "60",
            "--max-samples", "$maxSamples",
            "--db-path", (Join-Path $data "training_v6.db")
        )
    }
}
