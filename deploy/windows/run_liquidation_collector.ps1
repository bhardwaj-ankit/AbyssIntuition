param(
    [Parameter(Mandatory = $true)]
    [string]$CliPath,
    [bool]$PreventSystemSleep = $true
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$logDirectory = Join-Path $repoRoot "runtime\logs"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logPath = Join-Path $logDirectory "liquidation_collector.log"
Set-Location -LiteralPath $repoRoot

if ($PreventSystemSleep) {
    try {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class AbyssPowerState {
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint SetThreadExecutionState(uint executionState);
    public static uint PreventSystemSleep() { return SetThreadExecutionState(0x80000001); }
    public static uint RestoreDefaults() { return SetThreadExecutionState(0x80000000); }
}
"@
        $powerStateResult = [AbyssPowerState]::PreventSystemSleep()
        if ($powerStateResult -eq 0) {
            throw "Windows rejected the collector sleep-prevention request."
        }
        "$(Get-Date -Format o) system sleep prevention enabled; display sleep remains allowed" |
            Tee-Object -FilePath $logPath -Append
    }
    catch {
        "$(Get-Date -Format o) sleep-prevention initialization failed: $($_.Exception.Message)" |
            Tee-Object -FilePath $logPath -Append
        throw
    }
}

# Task Scheduler can terminate a PowerShell wrapper before its synchronous CLI
# child receives shutdown. Clean only collectors for this exact database before
# opening replacement sockets; primary-key deduplication is not a substitute for
# single ownership.
$existingCollectors = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @("liquidity-signal.exe", "python.exe") -and
    $_.CommandLine -like "*capture-liquidations*" -and
    $_.CommandLine -like "*runtime/liquidation_history.db*"
}
if ($existingCollectors) {
    $existingPids = @($existingCollectors | Select-Object -ExpandProperty ProcessId)
    "$(Get-Date -Format o) stopping orphan collector PIDs: $($existingPids -join ',')" |
        Tee-Object -FilePath $logPath -Append
    Stop-Process -Id $existingPids -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

try {
    while ($true) {
        "$(Get-Date -Format o) starting liquidation collector" |
            Tee-Object -FilePath $logPath -Append
        & $CliPath capture-liquidations `
            --symbols "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,NEARUSDT,PEPEUSDT" `
            --db-path "runtime/liquidation_history.db" `
            --status-interval-seconds 30 2>&1 |
            Tee-Object -FilePath $logPath -Append
        $collectorExitCode = $LASTEXITCODE
        "$(Get-Date -Format o) collector exited with code $collectorExitCode; restarting in 15 seconds" |
            Tee-Object -FilePath $logPath -Append
        Start-Sleep -Seconds 15
    }
}
finally {
    if ($PreventSystemSleep) {
        [AbyssPowerState]::RestoreDefaults() | Out-Null
    }
}
