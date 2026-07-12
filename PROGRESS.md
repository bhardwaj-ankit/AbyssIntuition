# AbyssIntuition — ML Re-architecture Progress Log

Running log for the signal/price model re-architecture effort. **Newest entries
first.** See [docs/project-brief-solution-intent.md](docs/project-brief-solution-intent.md)
for the full design and [DATA_INGESTION.md](DATA_INGESTION.md) for the data
platform inventory and refresh jobs. This file is maintained by the
`update-data-ingestion` skill (`.claude/skills/update-data-ingestion/`) — run it
to append a new entry after ingesting data or retraining.

---

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
  symbols; integrate Coinglass liquidation cluster levels (see
  `DATA_INGESTION.md` §2).

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
