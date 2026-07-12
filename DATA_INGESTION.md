# Data Ingestion Platforms

This document tracks every external data source the signal/price model depends
on: what is **integrated today**, what **should be integrated** to lift the
model off its current data ceiling, cost (free/paid), and the **refresh cadence**
(daily/weekly job) each one needs.

Why this matters: the model's accuracy is currently **data-bound, not
model-bound** (see [docs/project-brief-solution-intent.md](docs/project-brief-solution-intent.md) §7).
The single biggest defect was that Binance's live derivative endpoints only
retain ~30 days, so open-interest/positioning signals were ~99% missing. The
Binance Vision ingester (below) fixes that for backfill; the remaining gaps are
liquidation cluster levels and longer multi-regime history.

---

## 1. Integrated today

| Source | Access | Cost | What it provides | Retention | Refresh |
|---|---|---|---|---|---|
| **Binance Futures REST** (`fapi.binance.com`) | `data/binance_client.py` | Free | klines, depth, trades, mark price, funding, **live** OI/taker/long-short ratios | klines/funding: long; `futures/data/*`: **~30 days** | Live / on-demand |
| **Binance Vision bulk metrics** (`data.binance.vision`) | `data/binance_vision.py` + `ingest-vision-metrics` | Free | daily 5-min CSV of OI, OI notional, taker ratio, global + top-trader long/short ratios | **years** | **Daily job** (yesterday's file) |
| **Local triple-barrier label store** | `runtime/training_v5.db` etc. | Free | resolved LONG/SHORT/FLAT + terminal price per horizon | local | Continuous capture |

### Binance Vision — the key backfill fix

`/futures/data/*` (30-day retention) → replaced for history by daily bulk CSVs at:

```
https://data.binance.vision/data/futures/um/daily/metrics/<SYMBOL>/<SYMBOL>-metrics-<YYYY-MM-DD>.zip
```

Ingest a range into `runtime/vision_metrics.db`:

```bash
liquidity-signal ingest-vision-metrics \
  --symbols "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,NEARUSDT,PEPEUSDT" \
  --start 2026-03-22 --end 2026-07-11
```

**Current coverage** (updated by the `update-data-ingestion` skill; see
[PROGRESS.md](PROGRESS.md) for the log): all 6 tracked symbols span
`2026-03-22 -> 2026-07-12` (113 days, 31,968 rows/symbol) as of 2026-07-12.
Re-run the ingest command above with an updated `--start`/`--end` (or invoke the
skill) to catch up newly published days.

Then train with those features filled in (missing OI → real OI):

```bash
liquidity-signal train-gbdt --horizon-minutes 60 --vision-db runtime/vision_metrics.db
```

Notes:
- Files are cached under `runtime/vision_cache/` so re-runs are free.
- Symbol aliasing is handled (`PEPEUSDT` is published as `1000PEPEUSDT`).
- The trainer joins metrics to each snapshot by nearest timestamp and computes a
  30-minute OI change; columns only survive if populated across the split.

---

## 2. Recommended to integrate next

Ordered by expected impact on model edge.

| Source | Cost | Why integrate | Retention | Suggested cadence |
|---|---|---|---|---|
| **Coinglass API** | Paid (free tier limited) | **Liquidation cluster levels / heatmap** — our `liquidation_map` currently keeps only a scalar confidence and discards the cluster levels that act as short-horizon price magnets. Highest-value *new* signal. | long | Daily |
| **Coinalyze API** | Free tier | Cross-exchange aggregated OI, funding, liquidations — a second, exchange-independent view of positioning (reduces Binance-only bias). | months | Daily |
| **Velo Data** | Free/low | Cross-exchange futures OI/funding/liquidations, quant-friendly bulk access. | long | Daily/Weekly |
| **Tardis.dev** | Paid ($$) | Tick-level historical order books + OI + funding + liquidations since 2019. The gold standard when microstructure/order-book depth history is needed. | since 2019 | One-time backfill + weekly |
| **Amberdata / Kaiko / CryptoQuant** | Paid ($$$) | Institutional depth incl. on-chain + derivatives; consider only if scaling to many assets or on-chain features. | deep | Weekly |

### Highest-value next step

**Coinglass liquidation levels.** The model already ingests a liquidation-map
scalar; feeding actual cluster price levels (distance-to-nearest-cluster in bps)
is the most promising unexploited short-horizon signal and directly complements
the OI data now available from Vision.

---

## 3. Scheduled jobs

Set these up so data stays fresh without manual runs. Options: OS scheduler
(cron / Task Scheduler), or the repo's `/schedule` cloud routines.

### Daily — Binance Vision metrics (yesterday)

Backfills the previous complete UTC day for all tracked symbols. Run a few hours
after 00:00 UTC so the file is published.

```bash
# 00:30 UTC daily
YESTERDAY=$(date -u -d 'yesterday' +%F)
liquidity-signal ingest-vision-metrics \
  --symbols "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,NEARUSDT,PEPEUSDT" \
  --start "$YESTERDAY" --end "$YESTERDAY" \
  --db-path runtime/vision_metrics.db
```

**cron:**
```
30 0 * * * cd /path/to/AbyssIntuition && . .venv/bin/activate && \
  liquidity-signal ingest-vision-metrics --symbols "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,NEARUSDT,PEPEUSDT" \
  --start "$(date -u -d yesterday +\%F)" --end "$(date -u -d yesterday +\%F)" >> runtime/logs/vision_ingest.log 2>&1
```

**Windows Task Scheduler (PowerShell):**
```powershell
$y = (Get-Date).ToUniversalTime().AddDays(-1).ToString('yyyy-MM-dd')
liquidity-signal ingest-vision-metrics --symbols "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,NEARUSDT,PEPEUSDT" --start $y --end $y --db-path runtime/vision_metrics.db
```

### Weekly — retrain + evaluate (walk-forward)

Retrain per horizon with the freshly ingested metrics and re-check the gate.
Nothing goes live automatically — the fail-closed gate (`ai/deployment.py`)
still requires the strategy-level PnL backtest.

```bash
# Sunday 01:00 UTC
for H in 60 240; do
  liquidity-signal train-gbdt --horizon-minutes $H --vision-db runtime/vision_metrics.db
done
```

### Continuous — live snapshot capture

Keep the engine/bot capturing live snapshots (with full live positioning) so the
label store keeps growing toward the 6–12 months / multiple regimes needed for a
trustworthy gate pass. This is the only source of *forward* ground-truth labels.

---

## 4. Data readiness snapshot

_Last refreshed: 2026-07-12 (via `update-data-ingestion` skill)._

| Signal family | Source | Historical status |
|---|---|---|
| Price/volume/structure/regime/session | klines | ✅ full history |
| Funding | `fapi` fundingRate | ✅ ~74%+ |
| Open interest + long/short + taker ratios | **Vision** (was: 30-day API) | ✅ 2026-03-22 -> 2026-07-12 (113 days, 6/6 symbols) |
| Liquidation cluster levels | — | ❌ not yet integrated (Coinglass) |
| Multi-regime span (6–12 mo) | live capture | ⏳ accumulating (113/180+ days) |

See [docs/project-brief-solution-intent.md](docs/project-brief-solution-intent.md)
for the measured impact of each and the deploy-gate criteria, and
[PROGRESS.md](PROGRESS.md) for the dated change log.
