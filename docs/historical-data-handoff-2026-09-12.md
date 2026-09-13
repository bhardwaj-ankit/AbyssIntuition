# Historical data recovery and training handoff

This document preserves the operational context needed to continue the work on
another machine. It is the durable project handoff; the ChatGPT conversation
itself is not required to understand or resume the jobs.

New laptop sessions must start with
[`NEW_LAPTOP_BOOTSTRAP.md`](NEW_LAPTOP_BOOTSTRAP.md), which consolidates the
complete verified state and the no-assumption execution contract.

For the exact installation and script order, use
[`data-collection-runbook.md`](data-collection-runbook.md).

## Objective

Recover all available observed Binance Futures and Bybit liquidation events for
BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, NEARUSDT, and PEPEUSDT, then extend the
hourly training cohort and companion feature stores back to 2025-06-28. More
history is intended to improve regime coverage; it does not guarantee accuracy.
The expanded model must beat the existing 180-day baseline under the same
leakage-safe walk-forward gates before promotion.

## Findings and completed repairs

- The Windows workstation was offline from 2026-08-23 19:05 UTC through
  2026-09-08 17:55 UTC. CryptoHFTData recovered 101,925 unique CEX liquidation
  events for that exact half-open interval from 4,596 hourly venue/symbol
  partitions with zero failures.
- A separate Bybit PEPE defect was found. Bybit publishes `1000PEPEUSDT`; the
  project now maps it to PEPE units correctly. The full affected archive interval
  was recovered with 4,026 events, and only the 4,048 false PEPE heartbeat rows
  were invalidated with an audit incident.
- Archive/live overlap validation matched event timestamp, side, price,
  quantity, and the reconstructed exchange-compatible identifier. Imports are
  idempotent and cached objects retain SHA-256 provenance.
- CryptoHFTData exposes hourly Binance Futures and Bybit Parquet data beginning
  2025-06-28. A one-hour historical probe across all six symbols and both venues
  completed 12 partitions, stored 820 events, and had no failures.
- The shared ChatGPT discussion was used for its CoinGlass interpretation:
  long/short ratios, taker flow, OI/funding, and liquidation heatmap estimates
  are distinct. CoinGlass heatmaps are estimated levels, not observed historical
  liquidation-event ground truth. Shared reference:
  https://chatgpt.com/share/6aa5bc07-b714-83eb-81e8-e7c1b4d3f6b0

## Final original-workstation cutover state

The original full CryptoHFTData request covers the half-open range
`[2025-06-28T00:00:00Z, 2026-09-12T20:00:00Z)` and contains 127,248 partitions.
The user requested a clean handoff to another laptop, so every related process
was stopped at the 2026-09-12 21:48 UTC audit. The final resumable checkpoint
is:

- 2,325 processed partitions;
- 76,087 stored liquidation events;
- 452 published zero-event partitions;
- zero failed partitions;
- 7,359 cached source files totalling 25,723,818 bytes;
- last report update `2026-09-12T21:44:23.296520+00:00`.

`cryptohft_full_history_status.json` still says `running` because the importer
was deliberately terminated between checkpoints. This is expected and is not
a database-integrity failure. The laptop should rerun the same idempotent task;
cached files and archive provenance prevent completed work from being lost.

The historical snapshot worker completed before shutdown. `training_v6.db`
contains 10,604 hourly snapshots and 53,020 resolved labels for each of the six
symbols: 63,624 snapshots and 318,120 resolved labels total. Its timestamp span
is 2025-06-27 22:01 UTC through 2026-09-12 17:01 UTC. The requested on-chain
extension also completed. The supplemental market worker was stopped during
its final run and should be rerun on the laptop; completed archive records and
caches make that operation resumable.

Post-stop SQLite verification returned `ok` for:

- `runtime/liquidation_history.db`;
- `runtime/vision_metrics.db`;
- `runtime/training_v6.db`;
- `runtime/onchain_data.db`.

The following exact process tree was stopped during the handoff:

- CryptoHFT Python importer PID 25744;
- supplemental PowerShell wrapper PID 11628 and Python child PID 28160;
- collector PowerShell wrapper PID 4680, CLI shim PID 23144, and Python child
  PID 9400.

The historical training PowerShell wrapper PID 18024 had already exited after
finishing all six symbols.

To prevent any restart on the original workstation, these task definitions were
removed from Windows Task Scheduler:

- `AbyssIntuition-LiquidationCollector`;
- `AbyssIntuition-DailyDataRefresh`;
- `AbyssIntuition-DataHealthWatchdog`.

A full restart-persistence audit found no remaining matching process, scheduled
task, Windows service, Startup-folder entry, Run-registry entry, or Docker
installation. Therefore this workstation will not collect after reboot. Live
liquidation collection remains offline until the destination laptop starts its
collector, and that interval must be treated as a real continuity gap.

Local progress files and logs are deliberately not committed. On a transferred
data root, inspect `cryptohft_full_history_status.json`, the `logs` directory,
and `historical_backfill_processes.json` when present. The last file is output
from the destination launcher and is created fresh when the workers start; its
absence from the stopped transfer is not a database-integrity failure.

## Model/data contract added

`train-archive-baseline --include-cryptohft` selects the new
`archive_cryptohft` profile. It:

- exposes only source-isolated `xliq.cryptohft.*` liquidation features;
- requires complete Binance and Bybit hourly partitions for every full
  60-minute feature window;
- treats a published zero-event partition as an observed zero;
- treats missing/failed partitions as missing values;
- clips events at the decision timestamp and does not use future events;
- does not fabricate local WebSocket heartbeats;
- remains separate from Hyperliquid and estimated liquidation-map features.

The implementation passed all 114 project tests and Ruff at handoff.

## Completed portable SSD transfer

The stopped runtime was transferred on 2026-09-13 to
`D:\AbyssIntuitionData` on the attached `Extreme SSD`. The raw runtime copy has
13,555 files totalling 10,147,851,429 bytes; source and destination counts and
bytes match, and Robocopy reported zero failed files and zero mismatches. All
six production database files are byte-for-byte SHA-256 matches with their C:
sources and passed SQLite `quick_check`:

- `liquidation_history.db`;
- `training_v6.db`;
- `training_forward.db`;
- `vision_metrics.db`;
- `onchain_data.db`;
- `cross_venue.db`.

A second, exhaustive SHA-256 audit hashed every one of the 13,555 runtime files
on both drives. It found zero missing files, extra files, size mismatches, or
hash mismatches. Generated SQLite databases are deliberately excluded from Git
tracking and must travel through this verified data transfer; reusable LoRA
exports and training configuration JSON files remain versioned in Git.

The independently verified online-backup archive is
`verified_backups/data-backup-20260913T082636Z.zip`, SHA-256
`d48fc387a27a112bd88cca40cdcc017389a2f63e7402e53ab6e8ffee2cc467f2`.
The verified repository bundle is `AbyssIntuition.bundle` and contains branch
`feature/gbdt-dual-head-and-docs`. Its current branch tip and SHA-256 are in the
full machine-readable record, `TRANSFER_MANIFEST.json`, on the SSD. The
untracked workspace setting was preserved separately at
`workspace_uncommitted/.claude/settings.json`.

The raw SQLite files and their online-backup copies can have different hashes
because SQLite's online backup API may rewrite page layout. This is expected:
the raw C:-to-SSD copies match each other exactly, while the backup archive has
its own verified hashes and successful `quick_check` results.

The SSD is currently formatted as exFAT. Keep this copy for transfer and
backup, but use NTFS for databases that workers will actively write. Either
restore the verified backup onto the laptop's internal NTFS disk or, after
separately safeguarding all existing SSD contents, reformat/repartition the
SSD as NTFS. Do not run SQLite writers directly on this exFAT data root.

## Continue on another Windows machine

1. Clone this branch and install the project:

   ```powershell
   git clone https://github.com/bhardwaj-ankit/AbyssIntuition.git
   cd AbyssIntuition
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -e ".[ml,dev]"
   ```

2. Connect the portable SSD and choose a data directory, for example
   `E:\AbyssIntuitionData`. Do not let two machines write the same SQLite files.

3. Restore the latest verified backup to the data root, and copy the cache
   directories if they were preserved. Cache files are optional but prevent
   already completed archive partitions from being downloaded again.

4. Start or resume the three independent workers:

   ```powershell
   .\deploy\windows\start_historical_backfill.ps1 `
     -DataRoot "E:\AbyssIntuitionData" `
     -PythonExe ".\.venv\Scripts\python.exe"
   ```

   The workers are `cryptohft`, `market`, and `training`. They write separate
   logs and use separate SQLite writers. Re-running the archive worker is safe
   and resumes from cached downloads/idempotent database keys.

5. After all workers finish, generate the archive manifest and run the
   walk-forward comparison using the database paths under the portable data
   root. Never promote solely because the training span increased.

## Transfer from the original machine

With the portable SSD connected, create transactionally consistent SQLite
backups and optionally copy resumable caches:

```powershell
.\deploy\windows\backup_to_portable_ssd.ps1 `
  -PortableDataRoot "E:\AbyssIntuitionData" `
  -PythonExe "C:\path\to\python.exe" `
  -IncludeCaches
```

The database backup uses SQLite's online backup API and verifies `quick_check`
and SHA-256 hashes. It also creates and verifies `AbyssIntuition.bundle`, which
contains the committed branch without requiring a GitHub push. Clone it on the
other machine with:

```powershell
git clone "E:\AbyssIntuitionData\AbyssIntuition.bundle" AbyssIntuition
```

For a clean ownership handoff, stop the original workers after this copy and
before starting the destination workers. Copying caches while downloads
continue is safe to retry, but the final cache sync should be performed after
the original archive worker stops.
