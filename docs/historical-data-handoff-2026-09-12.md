# Historical data recovery and training handoff

This document preserves the operational context needed to continue the work on
another machine. It is the durable project handoff; the ChatGPT conversation
itself is not required to understand or resume the jobs.

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

## Full-history jobs started on the original machine

The original full CryptoHFTData request covers the half-open range
`[2025-06-28T00:00:00Z, 2026-09-12T20:00:00Z)` and contains 127,248 partitions.
At the 2026-09-12 21:16 UTC checkpoint it had processed 600 partitions, stored
20,758 events, and recorded zero failures. The public rate limit makes the full
run approximately 35 hours.

Other active/queued work at handoff:

- extend `training_v6.db` from 179.83 effective days to the June 2025 boundary;
- extend Binance positioning, depth, funding, and observed taker flow;
- extend open on-chain daily context (the requested extension completed);
- run the supplemental Binance job after positioning to avoid SQLite writer
  contention.

Local progress files and logs are deliberately not committed. On a transferred
data root, inspect `cryptohft_full_history_status.json`,
`historical_backfill_processes.json`, and the `logs` directory.

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
