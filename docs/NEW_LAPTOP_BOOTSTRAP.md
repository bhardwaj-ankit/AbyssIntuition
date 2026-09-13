# New laptop bootstrap: complete Codex and data handoff

This is the single entry point for continuing AbyssIntuition on the dedicated
Windows laptop. It exists because a Git clone does not contain the prior chat
thread. It preserves the decisions and state that the next Codex session must
not guess.

## Copy-paste prompt for Codex on the new laptop

```text
Open and read docs/NEW_LAPTOP_BOOTSTRAP.md completely, then read every file in
its "Mandatory project context" section in the stated order. Treat those files
and the attached SSD's TRANSFER_MANIFEST.json as the complete prior-session
context. Do not assume any path, drive letter, process state, scheduled-task
state, data completeness, or user intent. Begin with the document's read-only
preflight, report every discovered value and any discrepancy, and stop on a
conflict. If and only if preflight passes, continue the documented NTFS copy and
resume exactly the unfinished cryptohft and market jobs. Do not start duplicate
collectors, do not run active SQLite databases on exFAT, do not rerun completed
training without evidence, and do not train or promote a model until the
documented completeness and walk-forward gates pass. Keep me updated with exact
commands, paths, counts, hashes, PIDs, and status; never silently fill in a
missing fact.
```

## Instruction contract for the next Codex session

Before changing files, copying data, or starting a process, the agent must:

1. Read this file completely.
2. Read the mandatory source documents listed below completely.
3. Inspect the actual Git branch, working tree, attached volumes, SSD transfer
   manifest, database files, running processes, and scheduled tasks.
4. Report the discovered paths and state. Do not assume the SSD drive letter,
   repository path, Python path, available disk space, or whether a job is
   already running.
5. Stop and report any conflict between this snapshot and actual machine state.
   The current Git tree, SSD manifest, and database checks take precedence over
   older dated prose, but a discrepancy must never be silently resolved.
6. Never start two copies of a worker or allow two machines to write the same
   production SQLite data estate.
7. Never delete, reformat, repartition, overwrite, or replace data without the
   user's explicit approval and an exact target-path check.

The agent may proceed with read-only preflight checks immediately. It may copy
into a confirmed empty NTFS destination and start the documented jobs only
after all preflight checks pass.

## Mandatory project context

Read these files in this order:

1. `docs/NEW_LAPTOP_BOOTSTRAP.md` (this file)
2. `PROGRESS.md`
3. `docs/historical-data-handoff-2026-09-12.md`
4. `docs/data-collection-runbook.md`
5. `DATA_INGESTION.md`
6. `docs/project-brief-solution-intent.md`
7. `README.md`
8. `docs/public-liquidation-data-research.md`
9. `docs/local-lora-training.md`

For liquidation-map product interpretation also read:

- `docs/deep-research-report.md`
- `docs/liquidation-api-white-paper.md`
- `docs/liquidation-api-trade-signal-guide.md`

The machine-readable transfer record is not in Git. Read
`TRANSFER_MANIFEST.json` from the attached SSD after discovering its actual
drive letter and `AbyssIntuitionData` directory.

## User objective and decisions already made

- Recover the workstation shutdown gap and then ingest **all liquidation
  history available from the selected free source**, not only the outage
  interval, so training covers more market regimes.
- CryptoHFTData is the selected source for observed Binance Futures and Bybit
  hourly liquidation events. Its available history begins 2025-06-28. Do not
  claim that this source supplies data before that date.
- Continue collection on a different, dedicated laptop. The original
  workstation must remain stopped and must not regain automatic ownership.
- Preserve the full data estate on portable SSD, while keeping source code,
  reusable configuration, documentation, and generated LoRA exports in Git.
  SQLite databases and machine-specific settings are deliberately not stored
  in Git.
- Use more history to improve regime coverage, but never state that more days
  guarantee better accuracy. A candidate model must pass the same leakage-safe
  walk-forward and deployment gates before promotion.
- The shared ChatGPT finding was about interpreting CoinGlass long/short,
  liquidation, and heatmap data. CoinGlass liquidation maps/heatmaps are
  estimated liquidation levels. They are not interchangeable with observed
  forced-liquidation events. Reference supplied by the user:
  https://chatgpt.com/share/6aa5bc07-b714-83eb-81e8-e7c1b4d3f6b0

## Git state at final transfer

- Repository: `bhardwaj-ankit/AbyssIntuition`
- Branch: `feature/gbdt-dual-head-and-docs`
- Required remote commit: `505adc20ebb3f116a12d38ccd2a5cd6b741bd932`
- Short commit: `505adc2`
- At final verification, local HEAD, the remote branch, and the SSD Git bundle
  all pointed to this commit; the working tree had zero uncommitted entries.
- The two final handoff commits were:
  - `afd8068` - document verified SSD handoff and fix Windows launchers
  - `505adc2` - preserve training exports and keep runtime databases portable
- All 114 project tests passed before the final push.

Do not treat an ignored SQLite database as missing from Git. That exclusion is
intentional. The database is transferred and verified separately.

## Original workstation shutdown and recovery history

The original workstation was offline from 2026-08-23 19:05 UTC through
2026-09-08 17:55 UTC. The exact half-open interval was recovered using
CryptoHFTData:

- 4,596 Binance/Bybit venue-symbol hourly partitions;
- 101,925 unique observed CEX liquidation events;
- zero failed partitions;
- BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, NEARUSDT, and PEPEUSDT;
- archive/live overlap validation matched timestamp, side, price, quantity,
  and reconstructed event identifier.

A separate Bybit PEPE defect was repaired. Bybit publishes
`1000PEPEUSDT`, which must be normalized into PEPE units. The repair recovered
4,026 events and invalidated only 4,048 false historical heartbeat rows, with
an incident audit retained in the data.

At the final source-machine cutover, the CryptoHFT full-history importer, the
supplemental market worker, and the live collector were stopped. These Windows
scheduled tasks were removed:

- `AbyssIntuition-LiquidationCollector`
- `AbyssIntuition-DailyDataRefresh`
- `AbyssIntuition-DataHealthWatchdog`

The source audit found no remaining related process, service, Startup item,
Run-registry entry, Docker deployment, or scheduled task. It must remain that
way. Live collection has been offline since the cutover until the destination
laptop assumes ownership.

## Exact unfinished and completed work

### CryptoHFT full-history liquidation job: unfinished and resumable

Requested half-open interval at cutover:

`[2025-06-28T00:00:00Z, 2026-09-12T20:00:00Z)`

Final stopped checkpoint:

- 2,325 processed partitions of 127,248;
- 76,087 stored liquidation events;
- 452 published zero-event partitions;
- zero failed partitions;
- 7,359 cached source files, 25,723,818 bytes;
- last report update `2026-09-12T21:44:23.296520+00:00`.

The copied `cryptohft_full_history_status.json` says `running` because the
process was deliberately terminated between checkpoints. This is stale process
state, not database corruption. Confirm that no matching process is actually
running on the destination, then resume exactly one `cryptohft` worker. The
import is idempotent and reuses cached objects and database keys.

The destination worker computes a new safe end hour, so it can recover the
additional completed hourly archives accumulated since cutover.

### Historical training snapshot job: complete

`training_v6.db` contains:

- 10,604 hourly snapshots per symbol;
- 53,020 resolved labels per symbol;
- 63,624 snapshots and 318,120 resolved labels in total;
- timestamp span 2025-06-27 22:01 UTC through 2026-09-12 17:01 UTC;
- all six symbols listed above.

Do not rerun the `training` historical worker merely because it appears in the
default launcher task list. Audit this database first. At the recorded state,
only `cryptohft` and `market` need to resume. A training rerun is allowed only
if the audit finds that the destination copy differs or new required hourly
anchors need to be generated.

### Supplemental market job: unfinished and resumable

The required `market` worker intentionally runs these three commands:

- `ingest-vision-metrics` for Binance Vision OI and positioning;
- `ingest-open-onchain` for daily open on-chain context;
- `ingest-vision-supplemental` for Binance depth, funding, and taker flow.

It does **not** run `ingest-cross-venue` and does not write `cross_venue.db`.
Resume one `market` worker. Existing rows and caches are designed for
repeatable ingestion.

Cross-venue ingestion is a separate optional feature pipeline. The transferred
`cross_venue.db` preserves Bybit trades/positioning/funding and Binance spot
flow beginning 2026-02-06; it covered the recorded 180-day archive-training
anchors. The daily refresh invokes the cross-venue pipeline for its rolling
recovery window and extends it forward. Do not silently add cross-venue work to
the historical `market` worker. If the user later requests cross-venue history
before 2026-02-06, audit source availability and run `ingest-cross-venue` as a
separate, explicitly reported task.

## SSD transfer inventory and proof

The source SSD was labeled `Extreme SSD` and mounted as `D:` on the original
workstation. The drive letter on the destination is unknown and must be
discovered. It is formatted as **exFAT**.

Transfer root on the source machine: `D:\AbyssIntuitionData`

Verified raw runtime copy:

- 13,555 files;
- 10,147,851,429 bytes;
- every one of the 13,555 source/destination pairs was hashed with SHA-256;
- zero missing files;
- zero extra runtime files;
- zero size mismatches;
- zero hash mismatches.

Additional transfer artifacts bring the transfer-root total to 13,560 files.
The transfer root contains:

- the raw runtime contents directly at its root;
- `verified_backups/data-backup-20260913T082636Z.zip`;
- `verified_backup_status.json`;
- `AbyssIntuition.bundle`;
- `TRANSFER_MANIFEST.json`;
- `workspace_uncommitted/.claude/settings.json`.

Verified backup archive:

- size: 501,260,855 bytes;
- SHA-256:
  `d48fc387a27a112bd88cca40cdcc017389a2f63e7402e53ab6e8ffee2cc467f2`;
- archive verification: valid;
- all included SQLite `quick_check` results: `ok`.

Final Git bundle:

- branch tip: `505adc20ebb3f116a12d38ccd2a5cd6b741bd932`;
- size: 19,616,542 bytes;
- SHA-256:
  `73a9c2938c5fbe9f8ad597792b46ae899d417f13e8e2955ddc40acc998b2e943`;
- `git bundle verify`: complete history and valid.

Raw production database proof:

| File | Bytes | Raw SHA-256 | SQLite check |
| --- | ---: | --- | --- |
| `liquidation_history.db` | 607,752,192 | `0eaa70b3258bea1ad55ba5928b591adf3c1c090eec9761b70a95ad7803f97310` | `ok` |
| `training_v6.db` | 1,268,961,280 | `c12d91c3e474b7caee31f93f1965e28eb9a912ec68fc0438d01265dbb0c3d855` | `ok` |
| `training_forward.db` | 424,652,800 | `406ee9f0dd3f3b896a0a47813fc75d84a367603a652f3a3129a894128932fec2` | `ok` |
| `vision_metrics.db` | 220,893,184 | `fd7c7c017c9c7399bd4ed8a339be1a0d0b6a26ff201ba2e5326cad1a297fc4bf` | `ok` |
| `onchain_data.db` | 159,744 | `0dde1e04ffedc3f9c7c8073845e009f01b0ee516a789a74212216742c82fafaa` | `ok` |
| `cross_venue.db` | 138,452,992 | `c1677a29bbf2d81d10049d79db73a0bdeb18c0cd565865cba45226843eb384be` | `ok` |

SQLite online-backup files can have different byte hashes from raw database
files because the online backup API can rewrite page layout. This is expected.
Compare raw copies with the raw hashes above; verify the archive with its own
manifest and `quick_check` results.

## Storage rule on the destination laptop

Do not run continuously written SQLite databases directly on the exFAT SSD.
Use the SSD as verified transfer/backup media. Select a confirmed NTFS volume
with adequate free space for the active data root. Do not reformat the SSD; it
contains other user data and no format operation was authorized.

Recommended pattern after discovering actual paths:

```text
<cloned repository>                 code, docs, virtual environment
<NTFS volume>\AbyssIntuitionData    active databases, caches, logs, models
<SSD>\AbyssIntuitionData            immutable transfer source / backup
```

If the proposed NTFS data root already contains files, do not overwrite it.
Inventory and compare it first, then ask the user how to proceed.

## Model and feature contract

The predictor is a leakage-safe, two-head gradient-boosted tree system, not an
LLM predictor:

```text
historical snapshots at decision time
  |-- price/trend/momentum/volatility/market structure
  |-- archived OI, OI change, funding, basis, long-short ratios
  |-- taker and trade flow, depth, spot/perpetual divergences
  |-- daily on-chain context (joined without future-day leakage)
  |-- source-isolated observed liquidation flow
  |-- cross-asset BTC context and relative movement
  `-- cyclical session/weekday context
                    |
       leakage removal + relative-price conversion
       honest NaN handling + sparse-column pruning
                    |
          chronological purged walk-forward folds
                    |
       +------------+----------------------+
       |                                   |
classification head                 regression heads
LONG / SHORT / FLAT                  q10 / q50 / q90 return
probability + abstention             target and price range
       +------------+----------------------+
                    |
       fail-closed model and strategy gates
                    |
           shadow only until all gates pass
```

Non-negotiable modeling rules:

- Labels are `triple-barrier-v2` direction outcomes plus actual horizon-close
  forward return for regression.
- Splits are chronological and purged, never randomized.
- Old engine decisions, scoring, risk outputs, trade decisions, future data,
  and other leakage columns are excluded.
- Absolute price levels are converted to relative distances/basis points.
- Missing data remains `NaN`; it must not be silently converted to zero.
- `archive_only` excludes observed and estimated liquidation features.
- `archive_hyperliquid` is a separate optional auxiliary profile and previously
  failed the promotion gate.
- The target historical profile is `archive_cryptohft`. It admits only
  source-isolated `xliq.cryptohft.*` liquidation features, requires complete
  hourly Binance and Bybit partitions for the feature window, treats published
  zero-event files as observed zero, and treats missing/failed partitions as
  missing rather than zero.
- Estimated liquidation maps are not observed event ground truth and must not
  be mixed into the CryptoHFT observed-event feature family.
- The classifier predicts LONG/SHORT/FLAT. Quantile regressors predict q10,
  q50, and q90 forward return, from which the price target/range is derived.
- The deterministic risk/execution layer remains separate. The model must not
  choose order size, leverage, or liquidation math.
- The previous 180-day archive baseline and Hyperliquid comparison both failed
  the deployment gate. No model was promoted.
- More data authorizes a new evaluation, not promotion. Require at least two
  non-overlapping walk-forward windows, classification and range metrics, and
  the existing strategy-level gate before leaving shadow mode.

## Destination-laptop execution order

### Phase 1: read-only preflight

The agent must report all results before starting workers:

1. Confirm the repository is on the required branch and at or ahead of
   `505adc2`; fetch first if network access is available.
2. Confirm the Git working tree state. Preserve unexpected user changes.
3. Discover the SSD by volume label and the presence of
   `AbyssIntuitionData/TRANSFER_MANIFEST.json`; do not assume a drive letter.
4. Parse the manifest and require `status = complete`, full-runtime exact match,
   six database matches, six successful `quick_check` values, valid backup,
   and bundle commit matching the expected commit.
5. Recompute the backup and bundle SHA-256 values and compare them with the
   values above and in the manifest.
6. Verify the backup using `liquidity_signal.cli verify-data-backup` after the
   virtual environment is installed.
7. Inventory active AbyssIntuition processes and Windows scheduled tasks.
   Starting is forbidden if a duplicate destination worker/collector exists.
   A running Docker service alone is not proof of a project workload: inspect
   `docker ps --no-trunc` and `docker compose ps --all` before clearing this
   check. If process command lines are inaccessible, obtain sufficient
   read-only process visibility or report the check as unresolved.
8. Confirm an NTFS destination path and available capacity. Do not select exFAT
   for the active SQLite data root.

`historical_backfill_processes.json` is launcher output, not a required input
artifact. Its absence from the SSD is expected when no active launcher record
was preserved and is not a data-integrity conflict. The destination launcher
creates a fresh file after it starts the two workers.

### Phase 2: environment preparation

From the cloned repository:

```powershell
git checkout feature/gbdt-dual-head-and-docs
git pull --ff-only
git rev-parse HEAD

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[ml,dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

Expected baseline: commit `505adc2` or a documented later commit, and all 114
tests passing. If a later commit exists, inspect its changes instead of
assuming it is compatible.

### Phase 3: transfer into a confirmed empty NTFS data root

Set values only after discovery. The following names are placeholders:

```powershell
$SsdRoot = "<DISCOVERED_SSD_DRIVE>:\AbyssIntuitionData"
$DataRoot = "<CONFIRMED_NTFS_PATH>\AbyssIntuitionData"
$PythonExe = (Resolve-Path ".\.venv\Scripts\python.exe").Path
```

Copy the raw runtime tree while excluding transfer-only files. Do not run this
until `$DataRoot` is confirmed empty:

```powershell
New-Item -ItemType Directory -Force -Path $DataRoot

robocopy $SsdRoot $DataRoot /E /Z /FFT /COPY:DAT /DCOPY:DAT /R:2 /W:2 /XJ `
  /XD "$SsdRoot\verified_backups" "$SsdRoot\workspace_uncommitted" `
  /XF "AbyssIntuition.bundle" "TRANSFER_MANIFEST.json" `
      "verified_backup_status.json"

if ($LASTEXITCODE -ge 8) {
    throw "Data copy failed with Robocopy exit code $LASTEXITCODE"
}
```

After copying, compare relative file lists, sizes, and SHA-256 values against
the SSD raw runtime. Require the same 13,555 files and 10,147,851,429 bytes with
zero mismatches before starting a worker. Run SQLite `quick_check` on at least
the six production databases listed above.

### Phase 4: resume only unfinished historical work

At the recorded handoff, start exactly these two tasks:

```powershell
.\deploy\windows\start_historical_backfill.ps1 `
  -DataRoot $DataRoot `
  -PythonExe $PythonExe `
  -Tasks cryptohft,market
```

Do not include `training` unless its completed state has first been audited and
a concrete reason to extend it is reported.

Immediately record and inspect:

```powershell
Get-Content "$DataRoot\historical_backfill_processes.json"
Get-Content "$DataRoot\cryptohft_full_history_status.json"
Get-Content "$DataRoot\logs\cryptohft.stderr.log" -Tail 100
Get-Content "$DataRoot\logs\market.stderr.log" -Tail 100
```

An empty stderr log is normal. Do not infer completion from a process exit
alone. CryptoHFT completion requires:

- report `status` equals `complete`;
- `processed_partitions` equals `total_partitions`;
- `failed_partitions` equals zero;
- database integrity still passes;
- completeness/provenance records contain no missing partition silently
  treated as an observed zero.

### Phase 5: transfer continuous ownership

Install continuous jobs only after the historical market worker completes and
the destination database checks pass. Activate the virtual environment so the
installer resolves the correct `liquidity-signal` executable:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1

.\deploy\windows\install_data_tasks.ps1 `
  -DataRoot $DataRoot `
  -DailyRefreshTime "04:30" `
  -StartCollector
```

This installs the at-logon live liquidation collector, daily 04:30 archive
refresh, and 15-minute health watchdog. The dedicated Windows user must remain
logged in. Verify scheduled-task state and watch logs for at least 30 minutes.
Only the destination laptop may own these jobs.

### Phase 6: train only after data completion

Do not train or promote immediately after copying. First require the historical
jobs and data-completeness gates to pass. Then run the documented
`train-archive-baseline --include-cryptohft` command from
`docs/data-collection-runbook.md`, using the NTFS `$DataRoot` database paths.

Compare the result with the retained archive-only 180-day baseline using the
same walk-forward folds and gates. Keep every candidate shadow-only unless all
model and strategy criteria pass.

## Required status report from the destination agent

Before it claims success, the agent must report:

- repository path, branch, HEAD, remote comparison, and working-tree state;
- discovered SSD label, drive letter, filesystem, transfer-root path, and
  manifest status;
- backup and bundle hash results;
- chosen NTFS data-root path, filesystem, free space, copied file count/bytes,
  and full comparison result;
- all database integrity results;
- pre-existing and newly started processes/tasks, with PIDs where applicable;
- CryptoHFT processed/total/failure/event counts;
- market-worker state;
- whether continuous collection has or has not assumed ownership;
- exact remaining blockers before training;
- any discrepancy from this document, without silently choosing a resolution.

## Prohibited shortcuts

- Do not assume the SSD is `D:` or `E:`.
- Do not run active SQLite databases from exFAT.
- Do not format or repartition the SSD.
- Do not overwrite a non-empty data root.
- Do not restore `.claude/settings.json` blindly; it contains paths and
  permissions specific to the original workstation.
- Do not start duplicate workers or collectors.
- Do not mark the stale copied `running` report as proof of a live process.
- Do not treat missing archive partitions as zero-event observations.
- Do not merge independent live database copies.
- Do not substitute CoinGlass estimated heatmaps or Hyperliquid auxiliary data
  for required Binance/Bybit observed liquidation coverage.
- Do not train on engine decisions, future-derived fields, or randomized
  splits.
- Do not promote a model because it uses more days or has a single favorable
  test window.
- Do not commit SQLite databases, caches, secrets, or machine-specific settings
  to Git.

## Completion definition

The laptop handoff is operationally complete only when:

1. the repository and SSD/NTFS data copies are verified;
2. CryptoHFT and supplemental market history are complete with zero failures;
3. the live collector, daily refresh, and watchdog are installed on only the
   destination laptop;
4. live stream freshness and at least 95% common five-minute heartbeat coverage
   remain healthy for at least 30 minutes;
5. a current verified backup exists;
6. the expanded completeness manifest passes; and
7. training is evaluated walk-forward and remains shadow-only unless every
   deployment gate passes.
