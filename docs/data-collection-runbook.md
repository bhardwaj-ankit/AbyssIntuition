# Data collection runbook

This is the canonical script order for moving AbyssIntuition data collection to
a dedicated Windows machine. It covers the one-time historical backfill and the
continuous jobs required afterward.

## What must run

| Job | Script | Frequency | Purpose |
| --- | --- | --- | --- |
| Historical backfill launcher | `deploy/windows/start_historical_backfill.ps1` | Once, then rerun only to resume | Starts independent CryptoHFT liquidation, market archive, and hourly training workers |
| Historical worker | `deploy/windows/run_historical_backfill_worker.ps1` | Called by the launcher | Runs one selected historical task; normally do not invoke it directly |
| Live liquidation collector | `deploy/windows/run_liquidation_collector.ps1` | Continuously | Captures live Binance and Bybit forced-liquidation events and heartbeats |
| Daily archive refresh | `deploy/windows/run_daily_data_refresh.ps1` | Daily | Refreshes replayable sources, extends the forward cohort, backs up databases, and checks health |
| Health watchdog | `deploy/windows/run_data_health_watchdog.ps1` | Every 15 minutes | Detects stale streams or refreshes and restarts/reruns scheduled jobs |
| Scheduled-task installer | `deploy/windows/install_data_tasks.ps1` | Once per machine or after path changes | Installs the collector, daily refresh, and watchdog tasks |
| Portable backup | `deploy/windows/backup_to_portable_ssd.ps1` | Before transfer and periodically | Creates verified SQLite backups, optionally copies caches, and creates a Git bundle |

Only one machine may own the live collector and writable production databases
at a time. Never run collectors on two independent database copies and attempt
to merge them later.

## 1. Prepare the dedicated machine

Use a fixed drive letter for the portable SSD, such as `E:`. Windows Disk
Management can assign a persistent letter. Use NTFS, keep adequate free space,
disable USB selective suspend for the dedicated host, and never disconnect the
drive while a worker or collector is running.

Clone the published branch and install the application:

```powershell
git clone --branch feature/gbdt-dual-head-and-docs `
  https://github.com/bhardwaj-ankit/AbyssIntuition.git
cd AbyssIntuition
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[ml,dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

Use one data directory for every job:

```powershell
$DataRoot = "E:\AbyssIntuitionData"
$PythonExe = (Resolve-Path ".\.venv\Scripts\python.exe").Path
New-Item -ItemType Directory -Force -Path $DataRoot
```

Do not use an SSD path for one script and `repo\runtime` for another. That
would create two incomplete data estates.

## 2. Transfer existing data from the original machine

Connect the SSD to the original machine and run:

```powershell
.\deploy\windows\backup_to_portable_ssd.ps1 `
  -PortableDataRoot "E:\AbyssIntuitionData" `
  -PythonExe ".\.venv\Scripts\python.exe" `
  -IncludeCaches
```

This performs transactionally consistent SQLite online backups, verifies
`quick_check` and SHA-256 hashes, copies resumable download caches, and creates
`AbyssIntuition.bundle`. A final cache sync should be done after the original
historical workers stop.

Before starting the destination collector:

1. Stop the original collector and historical workers cleanly.
2. Run the portable backup one final time with `-IncludeCaches`.
3. Safely eject the SSD.
4. Connect it to the dedicated machine using the same fixed drive letter.
5. Confirm that no process on the original machine still writes those data.

## 3. Restore the verified databases

The portable `backups` directory contains a verified ZIP. Restore into an empty
directory; the restore command refuses to overwrite existing files:

```powershell
$Backup = Get-ChildItem "$DataRoot\backups\data-backup-*.zip" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1

& $PythonExe -m liquidity_signal.cli verify-data-backup $Backup.FullName
& $PythonExe -m liquidity_signal.cli restore-data-backup `
  $Backup.FullName "$DataRoot\restored"
```

Move the restored database files into `$DataRoot` only after verifying their
names and confirming the target files do not already exist. Preserve the cache
directories directly under `$DataRoot`.

## 4. Start or resume historical collection

Run the launcher once:

```powershell
.\deploy\windows\start_historical_backfill.ps1 `
  -DataRoot $DataRoot `
  -PythonExe $PythonExe
```

It launches three hidden, independent workers:

- `cryptohft`: all available hourly Binance Futures and Bybit liquidations from
  2025-06-28 through the latest safely completed archive hour;
- `market`: Binance positioning, depth, funding, taker flow, and open on-chain
  history;
- `training`: hourly snapshots and resolved labels for all six symbols.

The process list is written to
`$DataRoot\historical_backfill_processes.json`. Check it and the logs with:

```powershell
Get-Content "$DataRoot\historical_backfill_processes.json"
Get-Content "$DataRoot\cryptohft_full_history_status.json"
Get-Content "$DataRoot\logs\cryptohft.stderr.log" -Tail 50
Get-Content "$DataRoot\logs\market.stderr.log" -Tail 50
Get-Content "$DataRoot\logs\training.stderr.log" -Tail 50
```

The CryptoHFT report is complete only when `status` is `complete`,
`processed_partitions` equals `total_partitions`, and `failed_partitions` is
zero. An empty stderr log is expected. The operation is idempotent; rerun only
the interrupted task if necessary:

```powershell
.\deploy\windows\start_historical_backfill.ps1 `
  -DataRoot $DataRoot `
  -PythonExe $PythonExe `
  -Tasks cryptohft
```

Use `-Tasks market` or `-Tasks training` for the other workers. Do not launch a
second copy of a task while its first process is still active.

## 5. Install continuous collection

After the historical market worker has finished, install all continuous jobs.
Run this from a normal PowerShell session for the dedicated Windows user:

```powershell
.\deploy\windows\install_data_tasks.ps1 `
  -DataRoot $DataRoot `
  -DailyRefreshTime "04:30" `
  -StartCollector
```

This installs:

- `AbyssIntuition-LiquidationCollector` at user logon with automatic restart;
- `AbyssIntuition-DailyDataRefresh` daily at 04:30 local time, with
  `StartWhenAvailable` and wake enabled;
- `AbyssIntuition-DataHealthWatchdog` every 15 minutes.

These tasks use an interactive Windows principal. The dedicated user must
remain logged in. The collector prevents system sleep while allowing the
display to turn off. A power loss, network outage, user logout, or unplugged SSD
still interrupts live collection.

Check scheduled tasks immediately:

```powershell
Get-ScheduledTask -TaskName "AbyssIntuition-*" |
  Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName "AbyssIntuition-LiquidationCollector"
Get-Content "$DataRoot\logs\liquidation_collector.log" -Tail 50
```

Wait at least 30 minutes before retiring the old host. Confirm all 12 streams
are fresh and common-bucket continuity is at least 95%:

```powershell
& $PythonExe -m liquidity_signal.cli data-operations-health `
  --liquidation-db "$DataRoot\liquidation_history.db" `
  --refresh-report-path "$DataRoot\daily_refresh_status.json" `
  --backup-status-path "$DataRoot\data_backup_status.json" `
  --output-path "$DataRoot\data_operations_health.json"
```

## 6. Daily operating checks

Check these files:

```powershell
Get-Content "$DataRoot\data_operations_health.json"
Get-Content "$DataRoot\daily_refresh_status.json"
Get-Content "$DataRoot\logs\data_health_watchdog.log" -Tail 100
Get-Content "$DataRoot\logs\daily_data_refresh.log" -Tail 100
Get-ChildItem "$DataRoot\backups" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 5 Name, Length, LastWriteTime
```

Healthy collection requires:

- all Binance and Bybit streams fresh;
- at least 95% common five-minute heartbeat coverage;
- the daily refresh successful and less than 36 hours old;
- a verified backup less than 36 hours old;
- no failed archive partitions silently treated as zero events.

## 7. Train only after completeness passes

Historical download completion does not itself authorize model promotion. Run
the source-isolated CryptoHFT profile against the SSD databases:

```powershell
& $PythonExe -m liquidity_signal.cli train-archive-baseline `
  --include-cryptohft `
  --db-path "$DataRoot\training_v6.db" `
  --vision-db "$DataRoot\vision_metrics.db" `
  --onchain-db "$DataRoot\onchain_data.db" `
  --liquidation-audit-db "$DataRoot\liquidation_history.db" `
  --cross-venue-db "$DataRoot\cross_venue.db" `
  --completeness-manifest "$DataRoot\data_completeness_archive_cryptohft.json" `
  --output-dir "$DataRoot\models\gbdt-archive-cryptohft-60m-v1"
```

The expanded model must be compared with the retained 180-day baseline under
the same walk-forward windows. More days improve regime coverage but do not
guarantee better out-of-sample accuracy.

## Minimal command order

For a prepared machine receiving a completed SSD transfer, the required order
is:

```text
1. backup_to_portable_ssd.ps1       # original machine
2. verify-data-backup / restore     # destination machine
3. start_historical_backfill.ps1    # resume incomplete historical work
4. install_data_tasks.ps1           # continuous collector + daily refresh + watchdog
5. data-operations-health           # verify for at least 30 minutes
6. train-archive-baseline           # only after completeness passes
```

The detailed recovery rationale and prior incident history are preserved in
`docs/historical-data-handoff-2026-09-12.md`.
