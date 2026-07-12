---
name: update-data-ingestion
description: Catch up AbyssIntuition's market data (Binance Vision OI/positioning backfill), recompute data coverage, and refresh DATA_INGESTION.md and PROGRESS.md with the current numbers. Use when asked to "update data ingestion", "refresh market data", "sync vision metrics", "fetch latest data", "update progress", or periodically (daily/weekly) to keep data-readiness docs accurate.
---

# Update Data Ingestion

Keeps AbyssIntuition's market data current and keeps `DATA_INGESTION.md` /
`PROGRESS.md` describing reality instead of going stale. This is the project's
data-ops runbook, not a code-change task — do not modify model/training code
while running it.

Background: the live Binance `/futures/data/*` API only retains ~30 days of
OI/taker/long-short-ratio history. `src/liquidity_signal/data/binance_vision.py`
backfills the same metrics from Binance Vision's daily bulk CSVs (years of
history) into `runtime/vision_metrics.db`. That store needs to be kept caught up
as new days publish, and the docs need to reflect current coverage and model
status. See `docs/project-brief-solution-intent.md` §7 for the full context.

## Steps

1. **Check current coverage.**
   ```bash
   python -c "
   import sqlite3, datetime as dt
   con = sqlite3.connect('runtime/vision_metrics.db')
   for sym, n, mn, mx in con.cursor().execute(
       'select symbol,count(*),min(ts),max(ts) from vision_metrics group by symbol order by symbol'):
       print(sym, n, dt.datetime.utcfromtimestamp(mn/1000).date(), '->', dt.datetime.utcfromtimestamp(mx/1000).date())
   "
   ```
   If `runtime/vision_metrics.db` doesn't exist yet, start from
   `2026-03-22` (the earliest date used in prior ingests) or ask the user for a
   start date.

2. **Fetch the gap.** Vision publishes with roughly a 1-day lag, so the target
   end date is yesterday (UTC). For each symbol, the start date is the day
   after its current `max(ts)` (or the initial start date if the DB is empty).
   Run one ingest per distinct start date needed (usually all symbols share the
   same gap since they're ingested together):
   ```bash
   liquidity-signal ingest-vision-metrics \
     --symbols "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,NEARUSDT,PEPEUSDT" \
     --start <day-after-max-ts> --end <yesterday-utc> \
     --db-path runtime/vision_metrics.db
   ```
   Binance Vision returns 404 for dates it hasn't published yet — the command
   handles that per-day and reports `days_with_data` less than the requested
   range; that's expected, not an error, as long as most days land.

3. **Re-check coverage** with the same query as step 1 to confirm the new
   `max(ts)` per symbol and get the row/day counts for the doc update.

4. **Check model status** (for the progress entry, don't retrain unless asked):
   ```bash
   for f in runtime/models/*/evaluation.json; do
     python -c "
   import json,sys
   d=json.load(open(sys.argv[1]))
   g=d.get('deployment_gate',{})
   print(sys.argv[1], 'passed=', g.get('passed'))
   " "$f"
   done
   ```
   If no evaluations exist yet, note that in the progress entry instead of
   fabricating numbers.

5. **Update `DATA_INGESTION.md`.** In the "Binance Vision — the key backfill
   fix" section and the "§4 Data readiness snapshot" table, update the coverage
   dates/row counts to the numbers from step 3. Don't rewrite the platform
   inventory (§1/§2) unless a new source was actually integrated — that's a
   code change, not a data refresh, and belongs in a separate task.

6. **Append to `PROGRESS.md`.** Add a new entry **at the top** (newest first)
   using this template, filling in only what was actually measured:
   ```markdown
   ## <YYYY-MM-DD> — <one-line summary>

   - **Data**: Vision metrics coverage <old range> -> <new range> (<N> days) for
     <symbols>; <rows>/symbol in `runtime/vision_metrics.db`.
   - **Model**: <gate status per horizon if checked, or "no retrain this entry">.
   - **Next**: <one concrete next step from DATA_INGESTION.md §2/§7.4, e.g.
     "extend history further" or "integrate Coinglass liquidation levels">.
   ```
   Keep entries terse — this is a log, not a report. Do not delete prior entries.

7. **Report to the user**: what was fetched (days/rows), current coverage span
   per symbol, and current model gate status if checked. Do not commit the doc
   changes unless the user explicitly asks — leave them staged/visible for
   review, consistent with this project's normal commit discipline.

## Notes

- This skill only touches `runtime/vision_metrics.db`, `runtime/vision_cache/`,
  `DATA_INGESTION.md`, and `PROGRESS.md`. It never edits trainer/engine code.
- If the user asks to also retrain, that's a separate action: run
  `liquidity-signal train-gbdt --horizon-minutes <H> --vision-db runtime/vision_metrics.db`
  per horizon, then include the fresh gate results in the progress entry.
- If Binance Vision changes its URL scheme or CSV schema, `normalize_metric_rows`
  in `binance_vision.py` will need updating — that's a code fix, flag it rather
  than silently working around it here.
