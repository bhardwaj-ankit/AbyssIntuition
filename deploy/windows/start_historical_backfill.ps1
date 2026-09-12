param(
    [Parameter(Mandatory = $true)]
    [string]$DataRoot,

    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")),
    [string]$PythonExe = "python",
    [string[]]$Tasks = @("cryptohft", "market", "training")
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath $RepoRoot).Path
$data = [System.IO.Path]::GetFullPath($DataRoot)
$logs = Join-Path $data "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$worker = Join-Path $PSScriptRoot "run_historical_backfill_worker.ps1"
$powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
$launched = @()

foreach ($task in $Tasks) {
    if ($task -notin @("cryptohft", "market", "training")) {
        throw "Unknown task: $task"
    }
    $arguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", ('"' + $worker + '"'),
        "-Task", $task,
        "-DataRoot", ('"' + $data + '"'),
        "-RepoRoot", ('"' + $repo + '"'),
        "-PythonExe", ('"' + $PythonExe + '"')
    )
    $process = Start-Process `
        -FilePath $powershell `
        -ArgumentList $arguments `
        -WorkingDirectory $repo `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs "$task.stdout.log") `
        -RedirectStandardError (Join-Path $logs "$task.stderr.log") `
        -PassThru
    $launched += [ordered]@{
        task = $task
        pid = $process.Id
        started_at = [datetimeoffset]::Now.ToString("o")
    }
}

$status = [ordered]@{
    repo_root = $repo
    data_root = $data
    launched_at = [datetimeoffset]::Now.ToString("o")
    jobs = $launched
}
$statusPath = Join-Path $data "historical_backfill_processes.json"
$status | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statusPath -Encoding UTF8
$status | ConvertTo-Json -Depth 4
