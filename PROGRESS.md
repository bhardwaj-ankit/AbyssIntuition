# AbyssIntuition — ML Re-architecture Progress Log

Running log for the signal/price model re-architecture effort. **Newest entries
first.** See [docs/project-brief-solution-intent.md](docs/project-brief-solution-intent.md)
for the full design and [DATA_INGESTION.md](DATA_INGESTION.md) for the data
platform inventory and refresh jobs. This file is maintained by the
`update-data-ingestion` skill (`.claude/skills/update-data-ingestion/`) — run it
to append a new entry after ingesting data or retraining.

---

## 2026-09-12 - original workstation collection fully stopped for laptop cutover

- Stopped the active CryptoHFT importer, Binance supplemental worker, live
  liquidation collector, and their PowerShell/executable child processes. The
  historical training worker had already completed before shutdown.
- Removed—not merely disabled—the three Windows Task Scheduler definitions:
  `AbyssIntuition-LiquidationCollector`, `AbyssIntuition-DailyDataRefresh`, and
  `AbyssIntuition-DataHealthWatchdog`. A restart-persistence audit found no
  remaining matching process, service, Startup-folder item, Run-registry entry,
  or Docker installation. This workstation will not resume collection after a
  reboot.
- The resumable CryptoHFT checkpoint contains 2,325 of 127,248 partitions,
  76,087 events, 452 published zero-event partitions, and zero failures. Its
  report intentionally remains `running` because the process was stopped; the
  laptop must resume it from 7,359 cached files (25,723,818 bytes).
- The expanded historical snapshot job completed all six symbols: 10,604
  hourly snapshots and 53,020 resolved labels per symbol, or 63,624 snapshots
  and 318,120 resolved labels in total, spanning 2025-06-27 22:01 UTC through
  2026-09-12 17:01 UTC.
- `liquidation_history.db`, `vision_metrics.db`, `training_v6.db`, and
  `onchain_data.db` all passed SQLite `quick_check` after shutdown. Live
  liquidation capture is now offline until the dedicated laptop assumes
  ownership.

## 2026-09-12 - full historical liquidation and training expansion started

- Started a resumable CryptoHFTData import for every available hourly Binance
  Futures and Bybit partition from 2025-06-28 through the latest completed UTC
  hour: 127,248 requested symbol/venue partitions across BTC, ETH, SOL, XRP,
  NEAR, and PEPE. The public 60-request/minute limit makes this an approximately
  35-hour job; progress is written to
  `runtime/cryptohft_full_history_status.json` and failures remain retryable.
- Added the leakage-safe `archive_cryptohft` model profile. It uses only
  source-isolated `xliq.cryptohft.*` features, requires completed hourly files
  from both venues, treats published zero-event files as observed zeros, and
  never exposes events after the model decision timestamp.
- Started extending hourly training snapshots, Binance positioning, depth,
  funding, taker flow, and daily on-chain context back to the same 2025-06-28
  boundary. Supplemental Binance history is queued after positioning to avoid
  SQLite writer contention.
- The prior 179.83-day model is retained until the expanded cohort is complete
  and passes a new walk-forward comparison. More history increases regime
  coverage but is not assumed to guarantee higher out-of-sample accuracy.
- Regression verification after the archive-profile changes: Ruff passed and
  all 114 tests passed.

## 2026-09-12 - shutdown liquidation recovery completed

- Recovered the exact half-open workstation outage interval from
  2026-08-23 19:05 UTC through 2026-09-08 17:55 UTC using the free
  CryptoHFTData hourly archive. All 4,596 Binance/Bybit symbol partitions
  completed with zero failures and 101,925 exchange-compatible events across
  BTC, ETH, SOL, XRP, NEAR, and PEPE.
- Validated the archive against locally captured overlap data: 40/40 Binance
  and 20/20 Bybit BTCUSDT events matched on timestamp, side, price, quantity,
  and reconstructed live event identifier. Cached objects retain SHA-256 and
  hourly source provenance; reruns are idempotent.
- Found and fixed a separate Bybit PEPE capture defect: Bybit publishes the
  contract as `1000PEPEUSDT`, not `PEPEUSDT`. Recovered 4,026 Bybit PEPE
  archive events over the full affected interval and invalidated only the
  4,048 false historical PEPE heartbeat rows, with an incident audit record.
- Added legacy outer-Zstandard support for pre-2026-08-19 archive objects,
  bounded concurrent downloads under the public rate limit, schema validation,
  atomic resumable reports, exact interval clipping, and symbol-specific
  heartbeat invalidation.
- Final verification: SQLite `quick_check` passed, all 12 current streams were
  healthy with 100% recent continuity, Ruff passed, and 113 tests passed. The
  completed recovery is preserved in verified backup
  `runtime/backups/data-backup-20260912T204941Z.zip`.

## 2026-08-14 - archive-only baseline and staged training path

- Added a first-class `archive_only` data/model profile. Its completeness gate
  requires labels, positioning, depth, trade flow, funding, and on-chain data,
  while reporting live Binance/Bybit liquidations as explicitly optional.
- Made effective-day calculation cadence-aware. The historical cohort uses
  hourly anchors, while the forward liquidation-enhanced cohort remains on its
  five-minute contract.
- Enforced feature separation in the trainer: archive-only runs reject a
  liquidation database and exclude the estimated liquidation-map proxy as well
  as observed exchange liquidation-flow features. The profile is persisted in
  every model and evaluation artifact.
- Added `train-archive-baseline`, which regenerates the archive manifest,
  requires 150 complete cadence-adjusted days, and runs the leakage-safe
  90d/15d/15d walk-forward evaluation with a 30-day step.
- The audited cohort passed data readiness with 179.83 effective days and
  179.79 calendar days. All required archive families had at least 99.93%
  anchor coverage. Both trained folds contained 117 features and zero
  liquidation columns.
- The model failed the deployment gate and was not promoted. Mean directional
  precision was 0.2883, directional coverage 0.3195, balanced accuracy 0.3488,
  return-sign hit rate 0.5043, and range coverage 0.7081. Artifacts are retained
  under `runtime/models/gbdt-archive-only-60m-v1` as a research baseline.
- Added the `archive_hyperliquid` variant. It admits only source-isolated
  `xliq.hyperliquid.*` columns, treats unpublished partitions as missing, and
  never mixes Binance/Bybit events into archive features. Verified coverage is
  86 of 181 cohort dates (47.51%) with 451,256 mapped events across six symbols.
- The like-for-like Hyperliquid run also failed the gate. Compared with the
  archive-only baseline, mean return-sign hit rate improved from 0.5043 to
  0.5133 and range coverage from 0.7081 to 0.7116, while directional precision
  declined from 0.2883 to 0.2856 and balanced accuracy from 0.3488 to 0.3438.
  It remains an optional auxiliary feature, not a substitute for CEX capture.

## 2026-08-12 - Binance route migration and cohort integrity reset

- Detected that Binance had recorded zero liquidation events while Bybit had
  1,817 over the same seven-day observed periods. A controlled probe showed
  that even aggregate trades were silent on the legacy socket.
- Confirmed Binance retired unrouted USD-M market streams on 2026-04-23. Moved
  liquidation capture to `/market/stream` and added a one-second mark-price
  companion stream, so connection completeness now requires actual delivered
  market messages within ten seconds.
- Corrected Binance's `1000PEPEUSDT` stream alias and rescaled its price and
  quantity to PEPE units without changing liquidation notional. All 12 streams
  then passed message-qualified liveness with no active errors.
- Invalidated 3,396 false-positive Binance heartbeat rows and recorded both
  incidents in `liquidation_capture_incidents`; old connected sockets can no
  longer make the completeness gate pass.
- Preserved the affected forward cohort in
  `runtime/training_forward_legacy_binance_route_audit_2026-08-12.db`, including
  1,296 snapshots, 6,480 labels, and 1,296 decisions, then cleared only those
  three active training tables. Both databases passed SQLite `quick_check`, and
  the pre-reset full runtime is also preserved in the verified 17:40 UTC backup.
- Added adaptive archive-gap recovery. The repaired run extended its requested
  August 9-11 window back to August 7 and recovered all positioning, depth,
  trade-flow, funding, on-chain, and Hyperliquid partitions needed by the prior
  cohort before that cohort was quarantined for the independent Binance issue.
- Reworked completeness to evaluate source availability at each accepted
  snapshot rather than across wall-clock gaps, and readiness now requires 150
  effective snapshot-days as well as a 150-day calendar span.
- Full regression: 96 tests passed and Ruff reported no issues. Model training
  remains correctly blocked while a new message-qualified cohort accumulates.

## 2026-08-07 - current funding recovery and always-on deployment path

- Added complete inclusive pagination over Binance's free public funding-rate
  history endpoint, including deduplication, malformed-row handling, active
  interval inference, and the `PEPEUSDT`/`1000PEPEUSDT` exchange alias.
- Integrated current-month funding into `daily-data-refresh`; the pipeline no
  longer waits until Binance publishes the next monthly Vision archive.
- Recovered 120 August funding events into `runtime/vision_metrics.db`: 20 for
  each of the six tracked symbols.
- Tightened collector health persistence from five minutes to 30 seconds. After
  activating the hardened wrapper, all 12 streams were fresh with 100% common
  five-minute continuity, zero reconnects, and operational action `none`.
- Added Docker `collector` and `daily-refresh` services plus a provider-neutral
  always-on Linux deployment guide. This allows the live-only CEX feed to keep
  accumulating when the Windows workstation is off, once an external host is
  selected. The remote runtime must remain the single authoritative database.
- The strict forward cohort is still empty because the overnight shutdown
  created an unrecoverable heartbeat gap. This is expected fail-closed behavior,
  not a funding-data failure; clean samples begin only after a continuous,
  label-resolved capture window exists.

## 2026-08-07 - unattended continuity correction

- The next unattended audit found all 12 streams freshly connected but 0%
  recent continuity. Heartbeats stopped at 20:55 UTC and resumed at 12:40 UTC;
  logs show the collector task restarted when the machine resumed. The clean
  forward cohort correctly remained empty, so missing observations were not
  converted to zero-valued liquidation features.
- Added a collector-scoped Windows `ES_SYSTEM_REQUIRED` guard. It prevents
  system sleep while collection is active but permits display sleep and restores
  default behavior when the task exits. Daily refresh and watchdog tasks also
  request wake-to-run.
- A task restart exposed that Task Scheduler could terminate the PowerShell
  wrapper while leaving its CLI child alive. The validated orphan tree was
  stopped, leaving one collector, and startup now removes only orphaned
  `capture-liquidations` processes targeting the same local database before
  opening new sockets.
- Shutdown, hibernation, network loss, and power loss remain unrecoverable for
  live-only feeds and are reported as continuity gaps.

## 2026-08-07 - verified online backup and recovery

- Added `backup-data`, using SQLite's online backup API for consistent snapshots
  while the WAL-backed liquidation collector remains live. Backups include all
  active databases, operational/readiness artifacts, per-file SHA-256 hashes,
  and a bounded seven-generation rotation.
- Added mandatory restore verification: every new archive is extracted into a
  temporary isolated directory and every database must pass `PRAGMA quick_check`
  and checksum validation before the backup is reported complete.
- Created the first production generation: 532 MB of source databases compressed
  to 120,673,770 bytes in 8.9 seconds. All four SQLite databases and three JSON
  artifacts restored and verified successfully.
- Added `restore-data-backup`; it performs an atomic restore to a new directory
  and refuses to overwrite an existing path or any live database.
- Integrated backup freshness into operational health and the self-healing
  watchdog. The scheduled daily order is now refresh, verified backup, then
  health audit; a failed or stale backup triggers a daily-job rerun.

## 2026-08-07 - automated strict forward cohort

- Added `daily-data-refresh`, a resumable orchestrator for Binance positioning,
  depth, observed taker flow, prior-month funding, open on-chain metrics, and
  Hyperliquid auxiliary liquidations. It retries three completed UTC days and
  writes an atomic per-step status report.
- Separated new training data into `runtime/training_forward.db`; the incomplete
  February-August research database is no longer used as the denominator for
  native liquidation accumulation.
- Added a capture-eligibility gate: resolved forward examples are created only
  when at least 95% of the candidate five-minute buckets have simultaneous
  Binance and Bybit heartbeats for all six symbols.
- The first audit detected a real 15:30-19:35 UTC collector gap. Its 72 resolved
  snapshots were rejected and preserved in
  `runtime/training_forward_gap_audit_2026-08-06.db`; the clean cohort remains
  empty rather than laundering missing liquidation observations into zeros.
- Refreshed August 3-5 successfully: 864 Binance positioning and observed-flow
  rows per symbol, 288 depth rows per symbol, 9 Coin Metrics rows, 15 DefiLlama
  rows, and 4,312 observed Hyperliquid fills. The August 5 Hyperliquid partition
  was not yet published and will be retried automatically.
- Added Windows scheduled-task wrappers for an automatically restarting
  collector and a daily 04:30 local refresh. Task installation remains an
  explicit user-controlled machine-level operation. Both tasks were installed;
  collector ownership was transferred from temporary PID 17072 to the running
  scheduled task (new collector PID 33452).
- Added `data-operations-health`. It fails closed on stale source/symbol feeds,
  less than 95% recent dual-feed continuity, or a failed/overdue refresh while
  reporting expected model-history accumulation separately. The first live
  audit passed with 12/12 fresh streams and 100% recent continuity. The
  task-owned collector then persisted its first 20:20 UTC bucket with all 12
  streams connected, zero reconnects, and no current errors.
- Added and installed `AbyssIntuition-DataHealthWatchdog`, scheduled every 15
  minutes. It restarts stale collection, reruns failed/overdue archive refreshes,
  and only records non-recoverable historical continuity gaps. Manual and
  Task-Scheduler executions both returned action `none`; the scheduled run
  completed with result code 0 and an append-only JSONL audit record.
- Strict training remains blocked until the clean cohort is complete and spans
  150 days; no model was promoted.

## 2026-08-06 - observed Binance taker-flow backfill and ablation

- Added a resumable free Binance Public Data 5-minute kline ingester. It stores
  quote volume, trade count, average trade notional, candle range/return, and
  observed taker-buy versus taker-sell quote flow only after candle close.
- Backfilled 51,841 availability-timestamped rows per symbol for BTC, ETH, SOL,
  XRP, NEAR, and PEPE. The completeness manifest reports 99.95% minimum coverage
  for this newly required family.
- Added the observed flow signals to GBDT snapshots and fixed the UTC
  month-boundary selection so the last candle of each archive is retained.
- Ran the same 60-minute two-fold shadow walk-forward evaluation. Mean
  directional precision was 0.2840, directional coverage 0.2782, balanced
  accuracy 0.3429, sign-hit 0.5057, and range coverage 0.7116. This was worse
  than the prior Hyperliquid-enabled run's 0.3027 precision, so the deployment
  gate remains closed and no predictive-edge claim is made.

## 2026-08-06 - public archive audit and Hyperliquid liquidation backfill

- Searched GitHub, Binance's object index, Hugging Face, Kaggle, Internet
  Archive CDX, and official protocol archives for the missing six-symbol
  liquidation history. No free source provides complete Binance USD-M events
  for 2026-02-07 through 2026-08-05.
- Found and integrated `Chainticks/perp-data`: CC-BY-4.0 observed Hyperliquid
  fills derived from HyperCore S3. The published range at audit time has 86
  daily partitions from May 9 through August 4, with two absent dates.
- Added liquidated-user filtering, side normalization, `kPEPE` denomination
  conversion, resumable Parquet caching, per-period provenance records, and a
  CLI backfill command.
- Reports this source as auxiliary cross-venue coverage. It improves the model
  feature set but cannot make the required Binance/Bybit capture gate pass.
- Ingested all 86 available partitions: 451,256 filtered fills (272,818 BTC,
  77,422 ETH, 55,980 SOL, 18,344 XRP, 22,228 NEAR, and 4,464 PEPE). The manifest
  reports 47.51% period coverage and explicitly records 94 unavailable dates.
- Ran a 60m shadow walk-forward ablation. Archive-enabled directional precision
  was 0.3027 versus 0.3100 without liquidations; sign-hit was 0.5113 versus
  0.5057. Both failed, so this data has not yet demonstrated deployable edge.

## 2026-08-06 - free/open data pivot and on-chain backfill

- Removed paid-provider assumptions from the active acquisition plan. Tardis,
  CoinGlass, Coinalyze, and Velo are not required by the implementation.
- Added `ingest-open-onchain`, backed by the no-key Coin Metrics Community and
  DefiLlama endpoints. Stored 180 daily rows for each represented network;
  BTC/ETH/XRP include active-address and transaction metrics, while all five
  networks include chain TVL.
- Added leakage-safe on-chain level and 7-day-change features. Training uses
  only the previous completed UTC day.
- Added `capture-liquidations`, which normalizes and persists free Binance and
  Bybit public WebSocket feeds for all six symbols. The smoke test captured a
  real Bybit BTCUSDT liquidation event.
- Confirmed the remaining limitation: there is no free six-month bulk USD-M
  liquidation archive for this contract set. The gate therefore remains
  incomplete until continuous capture builds per-symbol historical coverage.

## 2026-08-06 - depth/funding completion and retraining gate

- Added a resumable Binance Vision supplemental ingester for percentage-band
  book depth and funding archives. Stored 51,552 depth rows and 525 funding
  events per symbol across all six symbols.
- Added seven leakage-safe archived depth features to the GBDT contract; depth
  aggregates are timestamped at bucket completion so they cannot see future
  snapshots.
- Added `data-completeness`. Labels, positioning, depth, and funding now pass;
  actual decision-time joins are 99.5%+.
- Historical liquidation coverage remains incomplete. Standard `train-gbdt` and
  `walk-forward-gbdt` now fail closed until the manifest is complete. The only
  override, `--allow-incomplete-data`, is explicitly limited to shadow research.
- Free exchange feeds are live-only, so complete historical forced-order data
  must accumulate forward; the manifest measures five-minute, dual-feed
  connection coverage per symbol.

## 2026-08-06 - v2 labels, 180-day dataset, and walk-forward evaluation

- **Data**: built `runtime/training_v6.db` without modifying the v5 baseline:
  25,896 hourly snapshots across 6 symbols from 2026-02-06 through 2026-08-05,
  with 129,480 resolved labels. All labels use `triple-barrier-v2`; barrier
  outcome and horizon-close regression target are stored separately.
- **Positioning**: extended `runtime/vision_metrics.db` to 51,550 rows/symbol
  over 179 complete UTC days (2026-02-07 through 2026-08-05).
- **Feature contract**: historical depth/trade-flow proxies are identified as
  `proxy` and excluded from production model columns. Missing funding and basis
  are now NaN rather than false zero observations.
- **Evaluation**: added `walk-forward-gbdt` with rolling 90d train / 15d
  validation / 15d test windows and non-overlapping tests. Both available folds
  failed for every evaluated horizon:

  | Horizon | Mean directional precision | Mean sign hit | Mean range coverage | Gate |
  |---|---:|---:|---:|---|
  | 15m | 0.4024 | 0.5028 | 0.7340 | FAIL |
  | 60m | 0.2824 | 0.5050 | 0.6942 | FAIL |
  | 240m | 0.3719 | 0.4937 | 0.6679 | FAIL |

- **Decision**: models remain shadow-only. More rows of the same candle-derived
  feature set are not sufficient; the next work should add genuinely new data,
  especially observed liquidation flow and archived real order flow/depth.

## 2026-07-12 — Vision gap fill + skill setup

- **Data**: caught up the Vision metrics gap from the last ingest run.
  Coverage extended from `2026-03-22 → 2026-06-23` (90 days) to
  `2026-03-22 → 2026-07-12` (113 days) across all 6 tracked symbols
  (BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, NEARUSDT, PEPEUSDT).
  31,968 rows/symbol in `runtime/vision_metrics.db`.
- **Tooling**: added `.claude/skills/update-data-ingestion/` so this catch-up +
  doc refresh can be re-run on demand instead of by hand.
- **Model**: no retrain this entry; last measured gate status (see prior entry)
  still stands. Re-run `train-gbdt --vision-db` to refresh against the longer
  history.
- **Next**: extend ingestion further back if Vision has earlier files for these
  symbols and keep the free liquidation collector running continuously.

## 2026-07-12 — Binance Vision ingester + leakage-free GBDT trainer

- **Root cause found**: the old "signal validator" experiments were training
  models to imitate the engine's own directional signal, which measured
  **0.393 directional precision vs. a 0.517 majority baseline** on resolved
  labels — anti-predictive. Every prior model failed the deployment gate as a
  result, not because of insufficient model capacity.
- **Built** `src/liquidity_signal/ai/gbdt_trainer.py`: leakage-free two-head
  `HistGradientBoosting` model (LONG/SHORT/FLAT classification + q10/q50/q90
  forward-return regression) trained only on resolved triple-barrier outcomes.
  Excludes every engine-decision/scoring/risk field, converts absolute prices
  to bps-from-close, treats missing derivatives as NaN, adds cross-asset BTC
  context, purged/embargoed chronological split.
- **Built** `src/liquidity_signal/data/binance_vision.py`: ingests Binance
  Vision's daily bulk metrics CSVs (years of history) to backfill OI/taker/
  long-short-ratio data that the live 30-day REST API cannot serve historically.
  Verified: ingesting the full 90-day span raised populated feature count from
  95 to 101 and 60m directional precision from 0.318 to 0.357.
- **Measured gate status** (on `runtime/training_v5.db`, pre-gap-fill, 90 days):

  | Horizon | Directional precision | Sign hit rate | Gate |
  |---|---|---|---|
  | 15m | 0.419 | 0.507 | FAIL |
  | 60m | 0.318 → 0.357 (+Vision) | 0.502 | FAIL |
  | 240m | 0.404 | 0.505 | FAIL |

- **Conclusion**: the ceiling is data (single ~90-day regime, no liquidation
  cluster levels), not model architecture. The fail-closed deployment gate
  (`ai/deployment.py`) correctly keeps every candidate in `shadow_only`.
- **Docs**: `docs/project-brief-solution-intent.md` (re-architecture brief),
  `DATA_INGESTION.md` (platform inventory + refresh jobs).
- Branch: `feature/gbdt-dual-head-and-docs`.
## 2026-08-14 - cross-venue flow and positioning phase

- Re-verified live liquidation collection: the prior 24 hours contained 2,086
  Binance and 1,201 Bybit liquidation events. Both exchanges had fresh,
  message-qualified heartbeats for all six symbols; sparse Bybit liquidation
  activity was not misclassified as downtime.
- Added a free, keyless cross-venue pipeline for Bybit archived tick trades,
  Bybit five-minute OI/account ratios and funding, plus Binance spot five-minute
  taker flow. It is source-isolated, causal, restartable, and records partition
  status, bytes, checksum, and errors instead of treating absence as zero.
- Handled two non-obvious source contracts: Bybit archives PEPE under
  `1000PEPEUSDT`, and Binance spot archives use headerless microsecond timestamps
  after 2025-01-01.
- Completed all six symbols for Bybit positioning/funding (1,086 daily
  partitions, 316,026 positioning records plus 3,258 funding events), Binance
  spot flow (312,768 bars), and Bybit tick-trade flow (312,768 bars). All three
  families cover 100% of the 25,896 historical training anchors. The trade
  archive transferred about 40 GB but retains only normalized bars and audited
  checksums, not bulky raw files.
- Added source-isolated GBDT features, explicit spot/perpetual divergences,
  a fail-closed 95% actual-anchor coverage gate, daily-refresh integration, and
  backup coverage for `runtime/cross_venue.db`.
- Ran the two-fold 60-minute cross-venue ablation. Mean directional precision
  improved from the prior archive-only 0.2883 to 0.3045, directional coverage
  was 0.1597, balanced accuracy 0.3510, sign hit 0.5054, and range coverage
  0.7116. The combined cross-venue + Hyperliquid run produced 0.2974 precision,
  0.1905 coverage, 0.3493 balanced accuracy, 0.5038 sign hit, and 0.7144 range
  coverage. Both failed the deployment gate, so no model was promoted.
