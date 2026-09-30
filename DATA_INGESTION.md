# Data ingestion (free/public sources only)

Active universe (2026-09-17): BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, NEARUSDT.
PEPE is retired from default collection, market selection, and GBDT training.
Existing PEPE records, backups, and legacy source conversions are retained for
historical audit only. Earlier six-symbol counts below describe archived data.

Collection was activated on the destination laptop on 2026-09-17. The active
NTFS root is `D:\AbyssIntuitionData`. Ten liquidation streams and minute REST
market observations run continuously; `RecentHistory` catches up mature hourly
samples and CryptoHFT each hour. Binance/on-chain archives refresh daily at
04:30 Dubai time. Daily refresh explicitly disables cross-venue, Hyperliquid,
and automatic training. `live_market.db` retains raw public observations with
request/receive timestamps; it does not change the archive feature contract.
See the bootstrap activation section and the timestamped runtime audit for
actual coverage, PIDs, backup verification, and the continuity gate.

The active training pipeline does not require Tardis, CoinGlass, Coinalyze,
Velo, or any paid API key. Data readiness is measured by
`runtime/data_completeness.json`, and normal GBDT training fails closed while a
required signal family is incomplete.

> **Collector ownership notice (2026-09-12 21:48 UTC):** collection was fully
> stopped on the original workstation for transfer to a dedicated laptop. All
> three AbyssIntuition scheduled tasks were unregistered, and the restart audit
> found no remaining process or auto-start mechanism. Live liquidation capture
> is offline until the laptop starts its collector. The CryptoHFT backfill must
> resume from 2,325/127,248 partitions; see
> [the handoff](docs/historical-data-handoff-2026-09-12.md).

The current model is data-bound: corrected labels and 180 days of positioning
data did not produce a stable walk-forward edge. This phase therefore adds
archived depth, funding, observed taker flow, and daily on-chain context while
measuring each addition rather than treating more columns as automatic edge.

## Integrated sources

| Source | Access and license | Stored data | Refresh |
|---|---|---|---|
| Binance Futures REST/WebSocket | Public, no key for market streams | live market state, current funding history, and `forceOrder` liquidation events | Continuous liquidation capture; daily funding catch-up |
| [Binance Public Data / Vision](https://github.com/binance/binance-public-data) | Public downloads; downloader repository is MIT licensed | 5-minute positioning metrics, percentage-band depth, funding, and observed taker-buy/sell quote flow | Daily depth/metrics/klines; monthly funding and kline archives |
| Bybit public WebSocket | Public, no key | `allLiquidation.{symbol}` events | Continuous |
| [CryptoHFTData liquidation archive](https://www.cryptohftdata.com/datasets/crypto-liquidation-data) | Free hourly Parquet; no key required at the public rate limit | observed Binance Futures and Bybit liquidation events from 2025-06-28 onward | Incident recovery and historical research backfills |
| Chainticks Perp Data / HyperCore S3 | CC-BY-4.0, no key | observed Hyperliquid liquidated-user fills for all six tracked assets | Daily, partial history |
| Coin Metrics Community API | No key; free for non-commercial use under its Creative Commons terms | daily active addresses, transaction count, market cap and price for BTC, ETH, XRP | Daily |
| DefiLlama free API | No key for the endpoints used | historical chain TVL for Bitcoin, Ethereum, Solana, XRPL, Near | Daily |
| Local label store | Local SQLite | triple-barrier-v2 outcome and separate horizon-close target | Continuous |

Binance split USD-M WebSocket traffic into `/public`, `/market`, and `/private`
routes and retired the legacy unrouted market path on 2026-04-23. Liquidations
and the companion mark-price liveness stream now use `/market`. A connected
socket is not counted as complete unless the companion stream has delivered an
actual message within ten seconds. `PEPEUSDT` is subscribed remotely as
`1000PEPEUSDT`; price and quantity are converted back to PEPE units while
notional remains unchanged. Bybit uses the same remote alias and conversion.

PEPE uses Ethereum chain context. Coin Metrics Community does not expose the
selected metrics for SOL, NEAR, or PEPE, so those symbols receive DefiLlama TVL
features only. All daily on-chain rows are joined only after the UTC day is
complete; a decision never sees same-day future data.

## Historical backfill already completed

The following local databases cover the 2026-02-06 through 2026-08-05 training
window:

- `runtime/training_v6.db`: 25,896 hourly snapshots and 129,480 v2 labels.
- `runtime/vision_metrics.db`: 51,550 positioning rows, 51,552 archived depth
  rows, 51,841 observed 5-minute taker-flow rows, and 525 funding events per
  symbol.
- `runtime/onchain_data.db`: 180 daily rows for each of BTC, ETH, SOL, XRP, and
  NEAR. BTC/ETH/XRP include network activity; all five include chain TVL.

Reproduce or extend the free backfills with:

```bash
liquidity-signal ingest-vision-metrics \
  --symbols "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,NEARUSDT" \
  --start 2026-02-07 --end 2026-08-05

liquidity-signal ingest-vision-supplemental \
  --start 2026-02-07 --end 2026-08-05

liquidity-signal ingest-open-onchain \
  --start 2026-02-07 --end 2026-08-05

liquidity-signal ingest-hyperliquid-liquidations \
  --start 2026-02-07 --end 2026-08-05

liquidity-signal data-completeness
```

The archive ingesters are resumable and cache downloads under `runtime/`.
`PEPEUSDT` is automatically mapped to Binance's `1000PEPEUSDT` archive name.
The daily job also paginates Binance's public funding-history REST endpoint for
the current UTC month, so it no longer waits for the monthly Vision archive.
For a focused recovery run:

```bash
liquidity-signal ingest-public-funding --start 2026-08-01 --end 2026-08-07
```

For an exact collector-outage interval, recover archived CEX liquidations with
a half-open UTC range:

```powershell
liquidity-signal ingest-cryptohft-liquidations `
  --start 2026-08-23T19:05:00Z `
  --end 2026-09-08T17:55:00Z
```

This writes exchange-compatible Binance/Bybit rows to the liquidation store,
hourly recovery provenance to `liquidation_archive_ingest`, cached source files
under `runtime/cryptohft_liquidation_cache/`, and resumable progress to
`runtime/cryptohft_recovery_status.json`. It does not invent offline collector
heartbeats or historical decision snapshots.

## Historical liquidation coverage and the remaining gap

The best verified open archive is `Chainticks/perp-data` on Hugging Face. It
publishes CC-BY-4.0 Parquet generated from Hyperliquid's official HyperCore S3
fills. The importer:

- keeps only the fill belonging to `liquidatedUser`, preventing maker-side
  duplicates from doubling or reversing the observed flow;
- maps BTC, ETH, SOL, XRP, NEAR, and `kPEPE` to the six Binance training symbols;
- rescales the 1,000-PEPE contract to PEPE units without changing notional;
- uses exchange time, not the later archive publication time, for leakage-safe
  joins; and
- records published and unavailable UTC partitions per symbol in SQLite.

At the 2026-08-06 audit, the archive exposed 86 daily partitions from
2026-05-09 through 2026-08-04, with 2026-06-14 and 2026-07-13 absent. It is
therefore valuable observed cross-venue context, but not a complete six-month
history and not a substitute for Binance USD-M liquidations. Its coverage is
reported under `auxiliary_checks`; it can never make the required CEX capture
check pass.

A source-isolated 60-minute walk-forward ablation also prevents us from
overstating its value. The trainer exposes only `xliq.hyperliquid.*` fields,
encodes unpublished partitions as missing, and never mixes live CEX events into
the archive profile. With Hyperliquid, mean directional precision was 0.2856
versus 0.2883 without liquidation features; sign-hit was 0.5133 versus 0.5043.
Both variants failed the deployment gate. The archive is retained as an honest
observed auxiliary source, but it has not established directional edge on this
partial history.

Binance's 5-minute kline archive supplies real quote volume, trade count, and
taker-buy quote volume. The ingester derives taker-sell quote volume and joins
each candle only after its close, with 99.95% minimum coverage across all six
symbols. A like-for-like 60-minute walk-forward run with these flow features
also failed the gate: mean directional precision was 0.2840, balanced accuracy
0.3429, sign-hit 0.5057, and range coverage 0.7116. The valid data is retained,
but this negative result rules out claiming that flow coverage alone fixes the
current model.

Binance and Bybit provide current liquidation events through public WebSockets.
CryptoHFTData provides free hourly files from 2025-06-28 onward. Its archived
exchange events can extend an explicitly separated historical model profile,
but it does not replace the local live-continuity contract. Liquidations still
cannot be reconstructed from candles, OI, or a heatmap without fabricating data.

The full six-symbol/two-venue archive can be populated or resumed with:

```powershell
liquidity-signal ingest-cryptohft-liquidations `
  --start 2025-06-28T00:00:00Z `
  --end 2026-09-12T20:00:00Z `
  --report-path runtime/cryptohft_full_history_status.json
```

After all companion historical families and hourly snapshots cover the same
cohort, evaluate the source-isolated archive variant with:

```powershell
liquidity-signal train-archive-baseline --include-cryptohft
```

The completeness gate requires both hourly venue partitions at every training
anchor, including valid zero-event partitions. It does not use fabricated live
heartbeats. The model still requires walk-forward improvement before promotion.

The repository therefore accumulates normalized liquidation events forward:

```bash
liquidity-signal capture-liquidations \
  --symbols "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,NEARUSDT" \
  --db-path runtime/liquidation_history.db
```

Run this command continuously as a service. A short smoke test on 2026-08-06
connected to both exchanges and persisted a real Bybit BTCUSDT event. The
completeness gate measures five-minute heartbeats with both feeds connected,
not merely event count, so sparse events or collector downtime cannot falsely
mark this family complete. The service persists health every 30 seconds so each
five-minute completeness bucket receives repeated observations.

This means the honest free-only paths are:

1. Keep the full feature contract and wait while liquidation history
   accumulates. This is the production path.
2. Train the explicitly separated archive-only baseline now. It excludes both
   observed Binance/Bybit liquidation flow and the estimated liquidation-map
   proxy; missing events are never converted to zeros:

   ```powershell
   liquidity-signal train-archive-baseline
   liquidity-signal train-archive-baseline --include-cryptohft
   liquidity-signal train-archive-baseline --include-hyperliquid
   ```

   This command uses an hourly cadence contract, audits the 180-day historical
   cohort, and writes a profile-specific completeness manifest. The Hyperliquid
   variant uses only published archive partitions and records missing dates as
   unavailable rather than zero. Model output is research-only unless every
   walk-forward fold passes the deployment gate. Both 2026-08-14 runs failed
   that gate and were not promoted.
3. Use `--allow-incomplete-data` only for additional shadow experiments. This
   bypass is never deployment authorization.

Free cross-venue context is now ingested separately from Binance perpetual
features:

```powershell
liquidity-signal ingest-cross-venue `
  --start 2026-02-06 --end 2026-08-05
```

This stores Bybit tick-trade flow, five-minute open interest and account
long/short ratios, Bybit funding, and Binance spot taker flow in
`runtime/cross_venue.db`. Every downloaded archive/day has a status, byte count,
and SHA-256 digest. Runs resume at incomplete partitions, `PEPEUSDT` is mapped
to Bybit's archived `1000PEPEUSDT` contract, and missing or failed partitions
remain missing. Binance's post-2025 microsecond timestamps are normalized to
milliseconds before causal joins. Train the isolated ablation only after its
actual-anchor coverage gate passes:

```powershell
liquidity-signal train-archive-baseline --include-cross-venue
liquidity-signal train-archive-baseline `
  --include-cross-venue --include-hyperliquid
```

The liquidation-enhanced program remains staged: an early experimental audit
after 30 effective days, comparison runs after 60 and 90 days, and the strict
two-fold production-candidate evaluation after 150 effective days. Each stage
still requires at least 95% coverage for every required family. Earlier stages
cannot promote a production model.

## Refresh schedule

The preferred daily entry point refreshes every free source, retries three days
to recover late partitions, grows a separate forward-only cohort, writes
`runtime/daily_refresh_status.json`, and regenerates
`runtime/data_completeness_forward.json`:

```powershell
liquidity-signal daily-data-refresh
```

The three-day window is only the normal fast path. Before downloading, the job
audits every day represented in the forward cohort and automatically extends
back to a missing positioning, depth, flow, or required prior-day on-chain
partition. This recovered the August 7 archives after a multi-day workstation
shutdown. Completeness is measured at actual snapshot anchors, so deliberate
offline gaps are not misrepresented as observed zero data, and effective
snapshot-days—not only first/last timestamps—control training readiness.

It does not append forward samples merely because a collector process exists.
The label-resolved window must have at least 95% five-minute heartbeat coverage
from both Binance and Bybit for every symbol. Strict walk-forward training is
automatically skipped until all data checks pass and the cohort spans the 150
days required for two non-overlapping 90d/15d/15d folds with a 30d step.

Operational liveness is audited independently from model readiness:

```powershell
liquidity-signal data-operations-health
```

This command writes `runtime/data_operations_health.json` and exits non-zero if
any stream is more than ten minutes stale, recent common-bucket continuity is
below 95%, the daily refresh failed/is over 36 hours old, or the most recent
verified backup is stale. An accumulating training cohort is reported but does
not make healthy collection appear failed.

## Backup and recovery

The daily task uses SQLite's online backup API after refresh, so WAL-backed live
collection can continue while a transactionally consistent snapshot is taken.
Each compressed generation contains all five active databases, readiness/health
artifacts, a SHA-256 manifest, and a successful SQLite `quick_check`. Seven
generations are retained by default.

```powershell
liquidity-signal backup-data
liquidity-signal verify-data-backup runtime/backups/data-backup-YYYYMMDDTHHMMSSZ.zip
```

Recovery is deliberately non-destructive. This command refuses an existing
target and restores only to an isolated directory:

```powershell
liquidity-signal restore-data-backup `
  runtime/backups/data-backup-YYYYMMDDTHHMMSSZ.zip `
  runtime/restored/incident-YYYYMMDD
```

Inspect the restored databases before any manual production swap. Stop the
collector task before replacing a live database; the application never performs
that destructive step automatically.

For persistent Windows operation, install the repository tasks from PowerShell:

```powershell
.\deploy\windows\install_data_tasks.ps1 -StartCollector
```

This registers an at-logon collector with automatic restart, a daily 04:30
local-time refresh, and a 15-minute operational watchdog. The watchdog restarts
the collector only when streams are stale, reruns a failed/overdue refresh, and
records continuity-only gaps without restarting because lost buckets cannot be
recovered. Logs are written under `runtime/logs/`; actions are appended to
`runtime/watchdog_interventions.jsonl`. Registering tasks is an explicit
machine-level action, so the installer is provided but is not run automatically
by the application.

While the collector task is running, its wrapper requests Windows
`ES_SYSTEM_REQUIRED`: the display may sleep, but the system itself stays awake
so live-only liquidation events are not lost. This request ends with the task.
It cannot protect against hibernation, shutdown, network loss, or power loss;
production-quality accumulation still requires an always-on machine or server.
On task startup the wrapper also removes only orphaned `capture-liquidations`
processes targeting this repository's `runtime/liquidation_history.db`. This
prevents duplicate WebSocket ownership after a forced Task Scheduler restart.

For collection while this workstation is shut down, deploy the Docker
`collector` and `daily-refresh` services on an always-on Linux host. The
provider-neutral instructions and safe source-of-truth rules are in
[`deploy/linux/ALWAYS_ON_DATA_COLLECTION.md`](deploy/linux/ALWAYS_ON_DATA_COLLECTION.md).
An existing low-power device is the most dependable no-subscription option;
eligible Google or Oracle free compute can also host it. GitHub Actions is not
appropriate for live capture because jobs are time-bounded and schedules can
be delayed.

The component commands remain available for manual recovery:

```powershell
$day = (Get-Date).ToUniversalTime().AddDays(-1).ToString('yyyy-MM-dd')
liquidity-signal ingest-vision-metrics --start $day --end $day
liquidity-signal ingest-vision-supplemental --start $day --end $day --no-include-funding
liquidity-signal ingest-public-funding --start $day --end $day
liquidity-signal ingest-open-onchain --start $day --end $day
liquidity-signal ingest-hyperliquid-liquidations --start $day --end $day
liquidity-signal ingest-cross-venue --start $day --end $day
liquidity-signal data-completeness
```

Monthly, re-run `ingest-vision-supplemental` with funding enabled for the prior
month; the daily orchestrator also rechecks the prior month and skips it after a
completed ledger entry. Continuously, run `capture-liquidations`. The daily job
creates resolved five-minute forward snapshots only from capture-eligible
windows. It then runs walk-forward training only when the strict manifest and
minimum-history gate are both satisfied.

```bash
liquidity-signal walk-forward-gbdt \
  --horizon-minutes 60 \
  --vision-db runtime/vision_metrics.db \
  --onchain-db runtime/onchain_data.db
```

## Current readiness

| Signal family | Status |
|---|---|
| v2 labels | Complete |
| OI, long/short ratios, taker positioning | 99.55% minimum coverage |
| Archived percentage-band depth | 99.56% minimum coverage |
| Observed Binance 5-minute taker flow | 99.95% minimum coverage |
| Funding | Historical archive backfilled; current month now caught up daily from public REST |
| Daily chain TVL | Backfilled for all five represented networks |
| Active addresses and transaction count | Backfilled for BTC, ETH, XRP |
| Hyperliquid observed liquidations | Partial archive; 86 published days from May 9 through August 4 at audit time |
| Bybit OI, long/short ratio, funding | Complete at 100% of all 25,896 archive-training anchors |
| Binance spot taker flow | Complete at 100% of all 25,896 archive-training anchors |
| Bybit archived tick-trade flow | Complete at 100% of all 25,896 archive-training anchors |
| Historical CEX liquidations | CryptoHFTData provides observed Binance/Bybit archives from 2025-06-28; the full import is resumable at the verified 2,325/127,248 checkpoint, and the exact workstation outage interval is already recovered |
| Strict forward cohort | Initialized separately; waits for >=95% dual-feed continuity before adding samples |
| Multi-regime span | Still accumulating toward 6-12 months |

See `PROGRESS.md` for measured model results and
`docs/project-brief-solution-intent.md` for the deployment gate.
