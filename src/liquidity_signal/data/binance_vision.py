"""Binance Vision bulk-metrics ingester.

Binance's ``/futures/data/*`` REST endpoints (open interest, taker ratio,
long/short ratios) only retain ~30 days, which leaves those high-value
positioning signals ~99% missing on any longer training history.  The same
metrics, however, are published as **daily bulk CSVs** on the Binance Vision
data portal going back years:

    https://data.binance.vision/data/futures/um/daily/metrics/<SYMBOL>/<SYMBOL>-metrics-<YYYY-MM-DD>.zip

Each daily file holds 5-minute rows with columns::

    create_time, symbol, sum_open_interest, sum_open_interest_value,
    count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio,
    count_long_short_ratio, sum_taker_long_short_vol_ratio

This module downloads and caches those files, normalises them into a stable
schema, and stores them in a small SQLite table so the GBDT trainer can fill the
otherwise-missing OI/positioning features by timestamp join.  See
``DATA_INGESTION.md`` for how this fits the wider data strategy.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx

from liquidity_signal.data.tls import httpx_verify

# Normalised metric row keys (what everything downstream consumes).
METRIC_KEYS = (
    "oi", "oi_value", "top_account_ratio", "top_position_ratio",
    "global_account_ratio", "taker_ratio",
)

# Some symbols trade under a scaled alias on Binance futures (e.g. PEPE ->
# 1000PEPE).  Files are published under the alias, but we store under the symbol
# used in the training data so the join stays simple.
SYMBOL_ALIASES = {
    "PEPEUSDT": "1000PEPEUSDT",
}


def exchange_symbol(symbol: str) -> str:
    return SYMBOL_ALIASES.get(symbol.upper(), symbol.upper())


def _parse_create_time(value: str) -> int:
    """``2026-05-28 00:05:00`` (UTC) -> epoch milliseconds."""
    dt = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_metric_rows(csv_text: str) -> list[dict[str, Any]]:
    """Parse a Vision metrics CSV into normalised, timestamp-keyed rows."""
    rows: list[dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for raw in reader:
        create_time = raw.get("create_time")
        if not create_time:
            continue
        try:
            ts = _parse_create_time(create_time)
        except ValueError:
            continue
        rows.append({
            "ts": ts,
            "oi": _to_float(raw.get("sum_open_interest")),
            "oi_value": _to_float(raw.get("sum_open_interest_value")),
            "top_account_ratio": _to_float(raw.get("count_toptrader_long_short_ratio")),
            "top_position_ratio": _to_float(raw.get("sum_toptrader_long_short_ratio")),
            "global_account_ratio": _to_float(raw.get("count_long_short_ratio")),
            "taker_ratio": _to_float(raw.get("sum_taker_long_short_vol_ratio")),
        })
    rows.sort(key=lambda row: row["ts"])
    return rows


class BinanceVisionClient:
    """Downloads and caches daily Vision metrics zips."""

    BASE_URL = "https://data.binance.vision"

    def __init__(self, cache_dir: str = "runtime/vision_cache", timeout: float = 30.0) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(
            base_url=self.BASE_URL, timeout=timeout, verify=httpx_verify()
        )

    def close(self) -> None:
        self._client.close()

    def _metrics_path(self, symbol: str, day: date) -> str:
        sym = exchange_symbol(symbol)
        return f"/data/futures/um/daily/metrics/{sym}/{sym}-metrics-{day:%Y-%m-%d}.zip"

    def fetch_metrics_day(self, symbol: str, day: date) -> list[dict[str, Any]]:
        """Return normalised rows for one day; ``[]`` if the file is absent."""
        cache_file = self.cache_dir / f"{symbol.upper()}-metrics-{day:%Y-%m-%d}.zip"
        if cache_file.exists():
            content = cache_file.read_bytes()
        else:
            response = self._client.get(self._metrics_path(symbol, day))
            if response.status_code == 404:
                return []
            response.raise_for_status()
            content = response.content
            cache_file.write_bytes(content)
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile:
            return []
        name = archive.namelist()[0]
        return normalize_metric_rows(archive.read(name).decode("utf-8"))

    def iter_metrics_range(
        self, symbol: str, start_day: date, end_day: date
    ) -> Iterator[dict[str, Any]]:
        current = start_day
        while current <= end_day:
            yield from self.fetch_metrics_day(symbol, current)
            current += timedelta(days=1)


class VisionMetricsStore:
    """SQLite store of normalised positioning metrics, keyed by (symbol, ts)."""

    def __init__(self, db_path: str = "runtime/vision_metrics.db") -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS vision_metrics(
                symbol TEXT NOT NULL,
                ts INTEGER NOT NULL,
                oi REAL, oi_value REAL,
                top_account_ratio REAL, top_position_ratio REAL,
                global_account_ratio REAL, taker_ratio REAL,
                PRIMARY KEY (symbol, ts))"""
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def upsert_many(self, symbol: str, rows: list[dict[str, Any]]) -> int:
        payload = [
            (symbol.upper(), int(row["ts"]), row.get("oi"), row.get("oi_value"),
             row.get("top_account_ratio"), row.get("top_position_ratio"),
             row.get("global_account_ratio"), row.get("taker_ratio"))
            for row in rows if row.get("ts")
        ]
        self._conn.executemany(
            """INSERT INTO vision_metrics
               (symbol, ts, oi, oi_value, top_account_ratio, top_position_ratio,
                global_account_ratio, taker_ratio)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol, ts) DO UPDATE SET
                 oi=excluded.oi, oi_value=excluded.oi_value,
                 top_account_ratio=excluded.top_account_ratio,
                 top_position_ratio=excluded.top_position_ratio,
                 global_account_ratio=excluded.global_account_ratio,
                 taker_ratio=excluded.taker_ratio""",
            payload,
        )
        self._conn.commit()
        return len(payload)

    def ingest(
        self, client: BinanceVisionClient, symbol: str, start_day: date, end_day: date
    ) -> dict[str, Any]:
        stored = 0
        days = 0
        current = start_day
        while current <= end_day:
            rows = client.fetch_metrics_day(symbol, current)
            if rows:
                stored += self.upsert_many(symbol, rows)
                days += 1
            current += timedelta(days=1)
        return {"symbol": symbol.upper(), "days_with_data": days, "rows_stored": stored}

    def coverage(self, symbol: str) -> dict[str, Any]:
        cur = self._conn.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM vision_metrics WHERE symbol=?",
            (symbol.upper(),),
        )
        count, min_ts, max_ts = cur.fetchone()
        return {"symbol": symbol.upper(), "rows": count or 0,
                "min_ts": min_ts, "max_ts": max_ts}

    def nearest(self, symbol: str, ts: int, tolerance_ms: int = 600_000) -> dict[str, Any] | None:
        """Most recent row at or before ``ts`` within ``tolerance_ms``."""
        cur = self._conn.execute(
            """SELECT ts, oi, oi_value, top_account_ratio, top_position_ratio,
                      global_account_ratio, taker_ratio
               FROM vision_metrics
               WHERE symbol=? AND ts<=? AND ts>=?
               ORDER BY ts DESC LIMIT 1""",
            (symbol.upper(), int(ts), int(ts) - tolerance_ms),
        )
        row = cur.fetchone()
        if row is None:
            return None
        keys = ("ts",) + METRIC_KEYS
        return dict(zip(keys, row))

    def oi_change_pct(self, symbol: str, ts: int, lookback_ms: int = 1_800_000) -> float | None:
        """Percent change in OI notional over ``lookback_ms`` (default 30m)."""
        now = self.nearest(symbol, ts)
        past = self.nearest(symbol, ts - lookback_ms)
        if not now or not past or not past.get("oi_value"):
            return None
        prev = past["oi_value"]
        if prev <= 0:
            return None
        return ((now["oi_value"] - prev) / prev) * 100.0
