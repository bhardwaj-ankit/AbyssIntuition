"""Supplemental Binance Vision ingestion for depth, funding, and trade flow."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from liquidity_signal.data.binance_vision import exchange_symbol


def _timestamp_ms(value: str) -> int:
    parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    return int(parsed.timestamp() * 1000)


def _imbalance(bid: float, ask: float) -> float:
    total = bid + ask
    return (bid - ask) / total if total > 0 else 0.0


def normalize_depth_rows(csv_text: str) -> list[dict[str, Any]]:
    """Aggregate Binance percentage-band depth snapshots into five-minute rows."""
    buckets: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    current_ts: int | None = None
    bands: dict[float, float] = {}

    def flush() -> None:
        nonlocal bands, current_ts
        if current_ts is None:
            return
        # Timestamp the aggregate at the bucket end. It is only safe to join to
        # a decision once every raw snapshot in that five-minute bucket exists.
        bucket_ts = ((current_ts // 300_000) + 1) * 300_000
        target = buckets[bucket_ts]
        for tag, band in (("020", 0.2), ("1", 1.0), ("5", 5.0)):
            bid = float(bands.get(-band, 0.0))
            ask = float(bands.get(band, 0.0))
            target[f"imbalance_{tag}"] += _imbalance(bid, ask)
            target[f"total_notional_{tag}"] += bid + ask
        total_020 = float(bands.get(-0.2, 0.0)) + float(bands.get(0.2, 0.0))
        total_5 = float(bands.get(-5.0, 0.0)) + float(bands.get(5.0, 0.0))
        target["concentration_020"] += total_020 / total_5 if total_5 > 0 else 0.0
        target["snapshots"] += 1.0
        bands = {}

    for raw in csv.DictReader(io.StringIO(csv_text)):
        try:
            ts = _timestamp_ms(raw["timestamp"])
            percentage = float(raw["percentage"])
            notional = float(raw["notional"])
        except (KeyError, TypeError, ValueError):
            continue
        if current_ts is not None and ts != current_ts:
            flush()
        current_ts = ts
        bands[percentage] = notional
    flush()

    rows: list[dict[str, Any]] = []
    for ts, values in sorted(buckets.items()):
        count = max(values["snapshots"], 1.0)
        rows.append({
            "ts": ts,
            "imbalance_020": values["imbalance_020"] / count,
            "imbalance_1": values["imbalance_1"] / count,
            "imbalance_5": values["imbalance_5"] / count,
            "total_notional_020": values["total_notional_020"] / count,
            "total_notional_1": values["total_notional_1"] / count,
            "total_notional_5": values["total_notional_5"] / count,
            "concentration_020": values["concentration_020"] / count,
            "snapshots": int(values["snapshots"]),
        })
    return rows


def normalize_funding_rows(csv_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in csv.DictReader(io.StringIO(csv_text)):
        try:
            rows.append({
                "ts": int(raw["calc_time"]),
                "funding_interval_hours": int(raw["funding_interval_hours"]),
                "funding_rate": float(raw["last_funding_rate"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(rows, key=lambda row: row["ts"])


def normalize_funding_api_rows(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize public REST rows and infer the active funding interval."""
    valid: list[tuple[int, float]] = []
    for raw in raw_rows:
        try:
            valid.append((int(raw["fundingTime"]), float(raw["fundingRate"])))
        except (KeyError, TypeError, ValueError):
            continue
    ordered = sorted(set(valid))
    rows: list[dict[str, Any]] = []
    for index, (timestamp, rate) in enumerate(ordered):
        if len(ordered) == 1:
            interval_hours = 8
        elif index:
            interval_hours = round((timestamp - ordered[index - 1][0]) / 3_600_000)
        else:
            interval_hours = round((ordered[1][0] - timestamp) / 3_600_000)
        rows.append({
            "ts": timestamp,
            "funding_interval_hours": max(1, min(interval_hours, 24)),
            "funding_rate": rate,
        })
    return rows


def normalize_kline_flow_rows(csv_text: str) -> list[dict[str, Any]]:
    """Extract observed taker flow from Binance's five-minute kline archive."""
    rows: list[dict[str, Any]] = []
    for raw in csv.DictReader(io.StringIO(csv_text)):
        try:
            open_price = float(raw["open"])
            high = float(raw["high"])
            low = float(raw["low"])
            close = float(raw["close"])
            quote_volume = float(raw["quote_volume"])
            taker_buy_quote = float(raw["taker_buy_quote_volume"])
            trade_count = int(raw.get("count", raw.get("trades", 0)))
            # close_time is the final millisecond inside the candle. Advancing
            # one millisecond makes the feature available only after it closes.
            ts = int(raw["close_time"]) + 1
        except (KeyError, TypeError, ValueError):
            continue
        if close <= 0 or quote_volume < 0 or taker_buy_quote < 0:
            continue
        taker_buy_quote = min(taker_buy_quote, quote_volume)
        taker_sell_quote = max(0.0, quote_volume - taker_buy_quote)
        buy_flow_ratio = taker_buy_quote / quote_volume if quote_volume > 0 else 0.5
        rows.append({
            "ts": ts,
            "buy_flow_ratio": buy_flow_ratio,
            "taker_buy_quote": taker_buy_quote,
            "taker_sell_quote": taker_sell_quote,
            "quote_volume": quote_volume,
            "trade_count": trade_count,
            "avg_trade_notional": quote_volume / trade_count if trade_count > 0 else 0.0,
            "range_bps": ((high - low) / close) * 10_000,
            "return_bps": ((close - open_price) / open_price) * 10_000
            if open_price > 0
            else 0.0,
        })
    return sorted(rows, key=lambda row: row["ts"])


class BinanceVisionSupplementalClient:
    BASE_URL = "https://data.binance.vision"

    def __init__(
        self,
        cache_dir: str = "runtime/vision_supplemental_cache",
        timeout: float = 90.0,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(base_url=self.BASE_URL, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _read_zip(self, remote_path: str, cache_file: Path) -> str | None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        if cache_file.exists():
            content = cache_file.read_bytes()
        else:
            response = self._client.get(remote_path)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            content = response.content
            cache_file.write_bytes(content)
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
            return archive.read(archive.namelist()[0]).decode("utf-8")
        except (zipfile.BadZipFile, IndexError, UnicodeDecodeError):
            return None

    def fetch_depth_day(self, symbol: str, day: date) -> list[dict[str, Any]]:
        requested = symbol.upper()
        remote = exchange_symbol(requested)
        name = f"{remote}-bookDepth-{day:%Y-%m-%d}.zip"
        text = self._read_zip(
            f"/data/futures/um/daily/bookDepth/{remote}/{name}",
            self.cache_dir / "bookDepth" / requested / name,
        )
        return normalize_depth_rows(text) if text else []

    def fetch_funding_month(self, symbol: str, month: date) -> list[dict[str, Any]]:
        requested = symbol.upper()
        remote = exchange_symbol(requested)
        name = f"{remote}-fundingRate-{month:%Y-%m}.zip"
        text = self._read_zip(
            f"/data/futures/um/monthly/fundingRate/{remote}/{name}",
            self.cache_dir / "fundingRate" / requested / name,
        )
        return normalize_funding_rows(text) if text else []

    def fetch_kline_month(self, symbol: str, month: date) -> list[dict[str, Any]]:
        requested = symbol.upper()
        remote = exchange_symbol(requested)
        name = f"{remote}-5m-{month:%Y-%m}.zip"
        text = self._read_zip(
            f"/data/futures/um/monthly/klines/{remote}/5m/{name}",
            self.cache_dir / "klines_5m" / requested / name,
        )
        return normalize_kline_flow_rows(text) if text else []

    def fetch_kline_day(self, symbol: str, day: date) -> list[dict[str, Any]]:
        requested = symbol.upper()
        remote = exchange_symbol(requested)
        name = f"{remote}-5m-{day:%Y-%m-%d}.zip"
        text = self._read_zip(
            f"/data/futures/um/daily/klines/{remote}/5m/{name}",
            self.cache_dir / "klines_5m" / requested / name,
        )
        return normalize_kline_flow_rows(text) if text else []


class VisionSupplementalStore:
    def __init__(self, db_path: str = "runtime/vision_metrics.db") -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS vision_depth_features(
                symbol TEXT NOT NULL,
                ts INTEGER NOT NULL,
                imbalance_020 REAL NOT NULL,
                imbalance_1 REAL NOT NULL,
                imbalance_5 REAL NOT NULL,
                total_notional_020 REAL NOT NULL,
                total_notional_1 REAL NOT NULL,
                total_notional_5 REAL NOT NULL,
                concentration_020 REAL NOT NULL,
                snapshots INTEGER NOT NULL,
                PRIMARY KEY(symbol, ts)
            );
            CREATE TABLE IF NOT EXISTS vision_funding(
                symbol TEXT NOT NULL,
                ts INTEGER NOT NULL,
                funding_interval_hours INTEGER NOT NULL,
                funding_rate REAL NOT NULL,
                PRIMARY KEY(symbol, ts)
            );
            CREATE TABLE IF NOT EXISTS vision_trade_flow(
                symbol TEXT NOT NULL,
                ts INTEGER NOT NULL,
                buy_flow_ratio REAL NOT NULL,
                taker_buy_quote REAL NOT NULL,
                taker_sell_quote REAL NOT NULL,
                quote_volume REAL NOT NULL,
                trade_count INTEGER NOT NULL,
                avg_trade_notional REAL NOT NULL,
                range_bps REAL NOT NULL,
                return_bps REAL NOT NULL,
                PRIMARY KEY(symbol, ts)
            );
            CREATE TABLE IF NOT EXISTS vision_archive_ingest(
                data_type TEXT NOT NULL,
                symbol TEXT NOT NULL,
                period TEXT NOT NULL,
                status TEXT NOT NULL,
                rows_stored INTEGER NOT NULL,
                PRIMARY KEY(data_type, symbol, period)
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _ingested(self, data_type: str, symbol: str, period: str) -> bool:
        row = self._conn.execute(
            "SELECT status FROM vision_archive_ingest WHERE data_type=? AND symbol=? AND period=?",
            (data_type, symbol.upper(), period),
        ).fetchone()
        return bool(row and row[0] == "complete")

    def _record(self, data_type: str, symbol: str, period: str, status: str, rows: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO vision_archive_ingest VALUES(?,?,?,?,?)",
            (data_type, symbol.upper(), period, status, rows),
        )
        self._conn.commit()

    def upsert_depth(self, symbol: str, rows: list[dict[str, Any]]) -> int:
        self._conn.executemany(
            """INSERT OR REPLACE INTO vision_depth_features VALUES(?,?,?,?,?,?,?,?,?,?)""",
            [(
                symbol.upper(), row["ts"], row["imbalance_020"], row["imbalance_1"],
                row["imbalance_5"], row["total_notional_020"],
                row["total_notional_1"], row["total_notional_5"],
                row["concentration_020"], row["snapshots"],
            ) for row in rows],
        )
        self._conn.commit()
        return len(rows)

    def upsert_funding(self, symbol: str, rows: list[dict[str, Any]]) -> int:
        self._conn.executemany(
            "INSERT OR REPLACE INTO vision_funding VALUES(?,?,?,?)",
            [(
                symbol.upper(), row["ts"], row["funding_interval_hours"],
                row["funding_rate"],
            ) for row in rows],
        )
        self._conn.commit()
        return len(rows)

    def upsert_trade_flow(self, symbol: str, rows: list[dict[str, Any]]) -> int:
        self._conn.executemany(
            "INSERT OR REPLACE INTO vision_trade_flow VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(
                symbol.upper(), row["ts"], row["buy_flow_ratio"],
                row["taker_buy_quote"], row["taker_sell_quote"],
                row["quote_volume"], row["trade_count"],
                row["avg_trade_notional"], row["range_bps"], row["return_bps"],
            ) for row in rows],
        )
        self._conn.commit()
        return len(rows)

    def ingest_depth_range(
        self,
        client: BinanceVisionSupplementalClient,
        symbol: str,
        start_day: date,
        end_day: date,
    ) -> dict[str, Any]:
        stored = completed = missing = 0
        current = start_day
        while current <= end_day:
            period = current.isoformat()
            if self._ingested("bookDepth", symbol, period):
                completed += 1
                current += timedelta(days=1)
                continue
            rows = client.fetch_depth_day(symbol, current)
            if rows:
                count = self.upsert_depth(symbol, rows)
                self._record("bookDepth", symbol, period, "complete", count)
                stored += count
                completed += 1
            else:
                self._record("bookDepth", symbol, period, "missing", 0)
                missing += 1
            current += timedelta(days=1)
        return {"stored_rows": stored, "complete_days": completed, "missing_days": missing}

    def ingest_funding_range(
        self,
        client: BinanceVisionSupplementalClient,
        symbol: str,
        start_day: date,
        end_day: date,
    ) -> dict[str, Any]:
        stored = completed = missing = 0
        month = start_day.replace(day=1)
        while month <= end_day:
            period = month.strftime("%Y-%m")
            if self._ingested("fundingRate", symbol, period):
                completed += 1
            else:
                rows = [
                    row for row in client.fetch_funding_month(symbol, month)
                    if start_day <= datetime.fromtimestamp(row["ts"] / 1000, timezone.utc).date() <= end_day
                ]
                if rows:
                    count = self.upsert_funding(symbol, rows)
                    self._record("fundingRate", symbol, period, "complete", count)
                    stored += count
                    completed += 1
                else:
                    self._record("fundingRate", symbol, period, "missing", 0)
                    missing += 1
            month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        return {"stored_rows": stored, "complete_months": completed, "missing_months": missing}

    def ingest_public_funding_range(
        self,
        client: Any,
        symbol: str,
        start_ts: int,
        end_ts: int,
    ) -> dict[str, Any]:
        """Upsert current funding events without marking a partial month complete."""
        remote_symbol = exchange_symbol(symbol.upper())
        raw_rows = client.get_funding_rate_history(remote_symbol, start_ts, end_ts)
        rows = normalize_funding_api_rows(raw_rows)
        return {
            "requested_symbol": symbol.upper(),
            "exchange_symbol": remote_symbol,
            "received_rows": len(raw_rows),
            "stored_rows": self.upsert_funding(symbol, rows),
        }

    def ingest_trade_flow_range(
        self,
        client: BinanceVisionSupplementalClient,
        symbol: str,
        start_day: date,
        end_day: date,
    ) -> dict[str, Any]:
        """Prefer compact monthly archives and fall back to current daily files."""
        stored = complete_months = complete_days = missing_days = 0
        month = start_day.replace(day=1)
        while month <= end_day:
            next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
            month_end = next_month - timedelta(days=1)
            range_start = max(start_day, month)
            range_end = min(end_day, month_end)
            monthly_rows = client.fetch_kline_month(symbol, month)
            if monthly_rows:
                selected = [
                    row for row in monthly_rows
                    if range_start
                    # Kline features become usable immediately after close.  The
                    # final candle of a UTC month therefore has a timestamp at
                    # 00:00 of the next month; subtract one millisecond only for
                    # source-period selection so that candle is not discarded.
                    <= datetime.fromtimestamp((row["ts"] - 1) / 1000, timezone.utc).date()
                    <= range_end
                ]
                stored += self.upsert_trade_flow(symbol, selected)
                self._record(
                    "kline5m_month", symbol, month.strftime("%Y-%m"), "complete", len(selected)
                )
                complete_months += 1
            else:
                current = range_start
                while current <= range_end:
                    rows = client.fetch_kline_day(symbol, current)
                    if rows:
                        stored += self.upsert_trade_flow(symbol, rows)
                        self._record("kline5m_day", symbol, current.isoformat(), "complete", len(rows))
                        complete_days += 1
                    else:
                        self._record("kline5m_day", symbol, current.isoformat(), "missing", 0)
                        missing_days += 1
                    current += timedelta(days=1)
            month = next_month
        return {
            "stored_rows": stored,
            "complete_months": complete_months,
            "complete_days": complete_days,
            "missing_days": missing_days,
        }

    def nearest_depth(
        self, symbol: str, ts: int, tolerance_ms: int = 600_000
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT ts, imbalance_020, imbalance_1, imbalance_5,
                      total_notional_020, total_notional_1, total_notional_5,
                      concentration_020, snapshots
               FROM vision_depth_features
               WHERE symbol=? AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1""",
            (symbol.upper(), ts, ts - tolerance_ms),
        ).fetchone()
        if not row:
            return None
        keys = (
            "ts", "imbalance_020", "imbalance_1", "imbalance_5",
            "total_notional_020", "total_notional_1", "total_notional_5",
            "concentration_020", "snapshots",
        )
        return dict(zip(keys, row))

    def nearest_funding(
        self, symbol: str, ts: int, tolerance_ms: int = 43_200_000
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT ts, funding_interval_hours, funding_rate FROM vision_funding
               WHERE symbol=? AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1""",
            (symbol.upper(), ts, ts - tolerance_ms),
        ).fetchone()
        if not row:
            return None
        return dict(zip(("ts", "funding_interval_hours", "funding_rate"), row))

    def nearest_trade_flow(
        self, symbol: str, ts: int, tolerance_ms: int = 600_000
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT ts, buy_flow_ratio, taker_buy_quote, taker_sell_quote,
                      quote_volume, trade_count, avg_trade_notional, range_bps,
                      return_bps
               FROM vision_trade_flow
               WHERE symbol=? AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1""",
            (symbol.upper(), ts, ts - tolerance_ms),
        ).fetchone()
        if not row:
            return None
        keys = (
            "ts", "buy_flow_ratio", "taker_buy_quote", "taker_sell_quote",
            "quote_volume", "trade_count", "avg_trade_notional", "range_bps",
            "return_bps",
        )
        return dict(zip(keys, row))

    def coverage(self, table: str, symbol: str) -> dict[str, Any]:
        if table not in {"vision_depth_features", "vision_funding", "vision_trade_flow"}:
            raise ValueError("Unsupported supplemental coverage table.")
        count, min_ts, max_ts = self._conn.execute(
            f"SELECT COUNT(*), MIN(ts), MAX(ts) FROM {table} WHERE symbol=?",
            (symbol.upper(),),
        ).fetchone()
        return {"rows": count or 0, "min_ts": min_ts, "max_ts": max_ts}


def build_completeness_manifest(
    training_db: str,
    market_db: str,
    liquidation_db: str,
    symbols: list[str],
    onchain_db: str | None = None,
    *,
    require_live_liquidations: bool = True,
    expected_snapshot_interval_minutes: int = 5,
    data_profile: str = "liquidation_enhanced",
) -> dict[str, Any]:
    """Measure whether every required historical signal family is populated."""
    if expected_snapshot_interval_minutes <= 0:
        raise ValueError("expected_snapshot_interval_minutes must be positive")
    uses_cryptohft = "cryptohft" in data_profile
    training = sqlite3.connect(training_db)
    market = sqlite3.connect(market_db)
    liquidation = sqlite3.connect(liquidation_db)
    checks: dict[str, Any] = {}
    snapshots, min_ts, max_ts = training.execute(
        "SELECT COUNT(*), MIN(event_ts), MAX(event_ts) FROM training_snapshots"
    ).fetchone()
    labels, v2_labels = training.execute(
        """SELECT COUNT(*), SUM(json_extract(raw_json, '$.raw_payload.label_version')
           = 'triple-barrier-v2') FROM training_labels"""
    ).fetchone()
    checks["labels_v2"] = {
        "rows": labels,
        "coverage": (v2_labels or 0) / labels if labels else 0.0,
        "complete": bool(labels and labels == v2_labels),
    }

    anchor_rows = (
        training.execute(
            "SELECT DISTINCT symbol, event_ts FROM training_snapshots"
        ).fetchall()
        if snapshots
        else []
    )
    market.execute(
        """CREATE TEMP TABLE cohort_anchors(
               symbol TEXT NOT NULL, event_ts INTEGER NOT NULL,
               PRIMARY KEY(symbol, event_ts)
           ) WITHOUT ROWID"""
    )
    market.executemany("INSERT INTO cohort_anchors VALUES(?,?)", anchor_rows)
    for name, table, tolerance_ms in (
        ("positioning", "vision_metrics", 600_000),
        ("depth", "vision_depth_features", 600_000),
        ("trade_flow", "vision_trade_flow", 600_000),
        ("funding", "vision_funding", 43_200_000),
    ):
        rows_by_symbol = {}
        ratios = []
        table_exists = bool(market.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone())
        for symbol in symbols:
            expected, covered = (
                market.execute(
                    f"""SELECT COUNT(*), COALESCE(SUM(EXISTS(
                               SELECT 1 FROM {table} AS data
                               WHERE data.symbol=anchors.symbol
                                     AND data.ts<=anchors.event_ts
                                     AND data.ts>=anchors.event_ts-?
                           )), 0)
                        FROM cohort_anchors AS anchors WHERE anchors.symbol=?""",
                    (tolerance_ms, symbol.upper()),
                ).fetchone()
                if table_exists and snapshots
                else (0, 0)
            )
            ratio = min(1.0, covered / expected) if expected else 0.0
            rows_by_symbol[symbol.upper()] = {
                "expected_snapshots": expected,
                "covered_snapshots": covered,
                "coverage": round(ratio, 4),
            }
            ratios.append(ratio)
        coverage = min(ratios) if ratios else 0.0
        checks[name] = {
            "symbols": rows_by_symbol,
            "minimum_coverage": round(coverage, 4),
            "complete": coverage >= 0.95,
        }

    liquidation.execute(
        """CREATE TEMP TABLE cohort_anchors(
               symbol TEXT NOT NULL, event_ts INTEGER NOT NULL,
               PRIMARY KEY(symbol, event_ts)
           ) WITHOUT ROWID"""
    )
    liquidation.executemany("INSERT INTO cohort_anchors VALUES(?,?)", anchor_rows)
    liquidation_by_symbol: dict[str, Any] = {}
    liquidation_ratios = []
    liquidation_rows = 0
    for symbol in symbols:
        count, first_ts, last_ts = liquidation.execute(
            """SELECT COUNT(*), MIN(event_ts), MAX(event_ts) FROM liquidation_events
               WHERE symbol=? AND source IN ('binance','bybit')
                     AND event_ts BETWEEN ? AND ?""",
            (symbol.upper(), min_ts, max_ts),
        ).fetchone()
        expected, covered = liquidation.execute(
            """SELECT COUNT(*), COALESCE(SUM((
                       SELECT COUNT(DISTINCT source)
                       FROM liquidation_capture_heartbeats AS heartbeat
                       WHERE heartbeat.symbol=anchors.symbol
                             AND heartbeat.bucket_ts=(anchors.event_ts / 300000) * 300000
                             AND heartbeat.connected=1
                             AND heartbeat.source IN ('binance','bybit')
                   ) >= 2), 0)
               FROM cohort_anchors AS anchors WHERE anchors.symbol=?""",
            (symbol.upper(),),
        ).fetchone()
        ratio = min(1.0, covered / expected) if expected else 0.0
        liquidation_by_symbol[symbol.upper()] = {
            "rows": count,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "expected_snapshots": expected,
            "covered_snapshots": covered,
            "capture_coverage": round(ratio, 4),
        }
        liquidation_rows += count
        liquidation_ratios.append(ratio)
    liquidation_coverage = min(liquidation_ratios) if liquidation_ratios else 0.0
    checks["liquidations"] = {
        "rows": liquidation_rows,
        "symbols": liquidation_by_symbol,
        "minimum_capture_coverage": round(liquidation_coverage, 4),
        "complete": liquidation_coverage >= 0.95,
        "required": require_live_liquidations,
        "reason": (
            "Free exchange liquidation feeds are live-only. Run capture-liquidations "
            "continuously while the next training dataset accumulates."
        ),
    }
    if uses_cryptohft:
        archive_by_symbol: dict[str, Any] = {}
        archive_ratios = []
        archive_rows = 0
        for symbol in symbols:
            count, first_ts, last_ts = liquidation.execute(
                """SELECT COUNT(*), MIN(event_ts), MAX(event_ts)
                   FROM liquidation_events
                   WHERE symbol=? AND source IN ('binance','bybit')
                         AND event_ts BETWEEN ? AND ?""",
                (symbol.upper(), min_ts, max_ts),
            ).fetchone()
            expected, covered = liquidation.execute(
                """SELECT COUNT(*), COALESCE(SUM((
                           SELECT COUNT(DISTINCT archive.source || '|' || archive.period)
                           FROM liquidation_archive_ingest AS archive
                           WHERE archive.symbol=anchors.symbol
                                 AND archive.status='complete'
                                 AND archive.source IN (
                                     'cryptohft_recovery_binance',
                                     'cryptohft_recovery_bybit'
                                 )
                                 AND archive.period IN (
                                     strftime(
                                         '%Y-%m-%dT%H:00:00Z',
                                         anchors.event_ts / 1000,
                                         'unixepoch'
                                     ),
                                     strftime(
                                         '%Y-%m-%dT%H:00:00Z',
                                         (anchors.event_ts - 3600000) / 1000,
                                         'unixepoch'
                                     )
                                 )
                       ) >= 4), 0)
                   FROM cohort_anchors AS anchors WHERE anchors.symbol=?""",
                (symbol.upper(),),
            ).fetchone()
            ratio = min(1.0, covered / expected) if expected else 0.0
            archive_by_symbol[symbol.upper()] = {
                "rows": int(count or 0),
                "first_ts": first_ts,
                "last_ts": last_ts,
                "expected_snapshots": int(expected or 0),
                "covered_snapshots": int(covered or 0),
                "archive_coverage": round(ratio, 4),
            }
            archive_rows += int(count or 0)
            archive_ratios.append(ratio)
        minimum_archive_coverage = (
            min(archive_ratios) if archive_ratios else 0.0
        )
        checks["cryptohft_liquidations"] = {
            "source": "CryptoHFTData Binance Futures and Bybit hourly archives",
            "rows": archive_rows,
            "symbols": archive_by_symbol,
            "minimum_archive_coverage": round(minimum_archive_coverage, 4),
            "complete": minimum_archive_coverage >= 0.95,
            "required": True,
            "note": (
                "Both venue partitions for the full 60-minute feature window "
                "must be complete; a published zero-event partition is valid coverage."
            ),
        }
    if onchain_db and Path(onchain_db).exists() and snapshots:
        from liquidity_signal.data.open_onchain import OpenOnchainStore, SYMBOL_NETWORK

        onchain = OpenOnchainStore(onchain_db)
        onchain._conn.execute(
            """CREATE TEMP TABLE cohort_anchors(
                   symbol TEXT NOT NULL, event_ts INTEGER NOT NULL,
                   PRIMARY KEY(symbol, event_ts)
               ) WITHOUT ROWID"""
        )
        onchain._conn.executemany("INSERT INTO cohort_anchors VALUES(?,?)", anchor_rows)
        networks: dict[str, Any] = {}
        ratios = []
        for network in sorted({SYMBOL_NETWORK[symbol.upper()] for symbol in symbols}):
            network_symbols = [
                symbol.upper()
                for symbol in symbols
                if SYMBOL_NETWORK[symbol.upper()] == network
            ]
            placeholders = ",".join("?" for _ in network_symbols)
            expected, covered = onchain._conn.execute(
                f"""SELECT COUNT(*), COALESCE(SUM(EXISTS(
                           SELECT 1 FROM onchain_daily AS data
                           WHERE data.network=? AND data.chain_tvl_usd IS NOT NULL
                                 AND data.ts<=(anchors.event_ts / 86400000) * 86400000
                                             - 86400000
                                 AND data.ts>=(anchors.event_ts / 86400000) * 86400000
                                             - 345600000
                       )), 0)
                    FROM cohort_anchors AS anchors
                    WHERE anchors.symbol IN ({placeholders})""",
                (network, *network_symbols),
            ).fetchone()
            ratio = min(1.0, covered / expected) if expected else 0.0
            networks[network] = {
                "expected_snapshots": expected,
                "covered_snapshots": covered,
                "tvl_coverage": round(ratio, 4),
            }
            ratios.append(ratio)
        onchain.close()
        minimum = min(ratios) if ratios else 0.0
        checks["onchain"] = {
            "networks": networks,
            "minimum_tvl_coverage": round(minimum, 4),
            "complete": minimum >= 0.95,
            "note": "Activity/transaction metrics are additionally available for BTC, ETH, and XRP.",
        }
    elif not snapshots:
        checks["onchain"] = {
            "complete": False,
            "reason": "The forward training cohort has no snapshots yet.",
        }
    else:
        checks["onchain"] = {
            "complete": False,
            "reason": "Open on-chain data has not been ingested.",
        }
    auxiliary_checks: dict[str, Any] = {}
    archive_table = liquidation.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type='table' AND name='liquidation_archive_ingest'"""
    ).fetchone()
    if archive_table and snapshots:
        start_day = datetime.fromtimestamp(min_ts / 1000, timezone.utc).date()
        end_day = datetime.fromtimestamp(max_ts / 1000, timezone.utc).date()
        expected_days = (end_day - start_day).days + 1
        archive_symbols: dict[str, Any] = {}
        ratios = []
        for symbol in symbols:
            published_days, unavailable_days, event_rows = liquidation.execute(
                """SELECT SUM(status='complete'), SUM(status='unavailable'),
                          SUM(CASE WHEN status='complete' THEN rows_stored ELSE 0 END)
                   FROM liquidation_archive_ingest
                   WHERE source='hyperliquid_archive' AND symbol=?
                         AND period BETWEEN ? AND ?""",
                (symbol.upper(), start_day.isoformat(), end_day.isoformat()),
            ).fetchone()
            ratio = min(1.0, float(published_days or 0) / expected_days)
            ratios.append(ratio)
            archive_symbols[symbol.upper()] = {
                "published_days": int(published_days or 0),
                "unavailable_days": int(unavailable_days or 0),
                "events": int(event_rows or 0),
                "period_coverage": round(ratio, 4),
            }
        auxiliary_checks["hyperliquid_liquidations"] = {
            "source": "Chainticks/perp-data (HyperCore S3-derived)",
            "license": "CC-BY-4.0",
            "symbols": archive_symbols,
            "minimum_period_coverage": round(min(ratios) if ratios else 0.0, 4),
            "complete": bool(ratios and min(ratios) >= 0.95),
            "required": False,
            "note": (
                "Observed cross-venue liquidations improve the feature set but do not "
                "satisfy the required Binance/Bybit USD-M capture check."
            ),
        }
    training.close()
    market.close()
    liquidation.close()
    expected_snapshots_per_day = 1_440 / expected_snapshot_interval_minutes
    effective_days = (
        len(anchor_rows) / (len(symbols) * expected_snapshots_per_day)
        if anchor_rows and symbols
        else 0.0
    )
    calendar_span_days = (
        max(0.0, (int(max_ts) - int(min_ts)) / 86_400_000)
        if min_ts is not None and max_ts is not None
        else 0.0
    )
    return {
        "version": 2,
        "data_profile": data_profile,
        "required_checks": [
            name
            for name in checks
            if name != "liquidations" or require_live_liquidations
        ],
        "excluded_feature_families": (
            []
            if require_live_liquidations
            else (
                ["estimated_liquidation_map"]
                + ([] if uses_cryptohft else ["binance_bybit_liquidation_events"])
            )
        ),
        "training_db": training_db,
        "market_db": market_db,
        "liquidation_db": liquidation_db,
        "onchain_db": onchain_db,
        "period": {
            "min_ts": min_ts,
            "max_ts": max_ts,
            "calendar_span_days": round(calendar_span_days, 4),
            "effective_snapshot_days": round(effective_days, 4),
            "expected_snapshot_interval_minutes": expected_snapshot_interval_minutes,
        },
        "symbols": [symbol.upper() for symbol in symbols],
        "checks": checks,
        "auxiliary_checks": auxiliary_checks,
        "complete": all(
            check["complete"]
            for name, check in checks.items()
            if name != "liquidations" or require_live_liquidations
        ),
    }


def write_manifest(manifest: dict[str, Any], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
