"""Free cross-venue archives used by the leakage-safe training profile.

The module deliberately keeps every source in its own namespace.  A missing
Bybit or Binance archive remains missing; it is never converted into a zero
flow observation.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import os
import sqlite3
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx


BYBIT_ARCHIVE_SYMBOLS = {"PEPEUSDT": "1000PEPEUSDT"}


def bybit_archive_symbol(symbol: str) -> str:
    requested = symbol.upper()
    return BYBIT_ARCHIVE_SYMBOLS.get(requested, requested)


def _bucket_close_ms(timestamp_ms: int) -> int:
    return ((timestamp_ms // 300_000) + 1) * 300_000


def normalize_bybit_trade_rows(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Aggregate Bybit's tick archive into causal five-minute flow bars."""
    buckets: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for raw in csv.DictReader(lines):
        try:
            timestamp_ms = int(float(raw["timestamp"]) * 1000)
            price = float(raw["price"])
            size = float(raw["size"])
            side = raw["side"].strip().lower()
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if timestamp_ms < 0 or price <= 0 or size <= 0 or side not in {"buy", "sell"}:
            continue
        quote_notional = price * size
        bucket = buckets[_bucket_close_ms(timestamp_ms)]
        if bucket["trade_count"] == 0:
            bucket["open_price"] = price
        bucket["close_price"] = price
        bucket["quote_volume"] += quote_notional
        bucket[f"{side}_quote"] += quote_notional
        bucket["trade_count"] += 1
        bucket["max_trade_notional"] = max(
            bucket["max_trade_notional"], quote_notional
        )

    normalized: list[dict[str, Any]] = []
    for ts, bucket in sorted(buckets.items()):
        quote_volume = bucket["quote_volume"]
        trade_count = int(bucket["trade_count"])
        open_price = bucket["open_price"]
        close_price = bucket["close_price"]
        normalized.append({
            "ts": ts,
            "buy_flow_ratio": bucket["buy_quote"] / quote_volume,
            "taker_buy_quote": bucket["buy_quote"],
            "taker_sell_quote": bucket["sell_quote"],
            "quote_volume": quote_volume,
            "trade_count": trade_count,
            "avg_trade_notional": quote_volume / trade_count,
            "max_trade_notional": bucket["max_trade_notional"],
            "return_bps": ((close_price - open_price) / open_price) * 10_000,
        })
    return normalized


def normalize_binance_spot_kline_rows(csv_text: str) -> list[dict[str, Any]]:
    """Normalize headerless spot klines, including post-2025 microsecond times."""
    fields = (
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume",
        "ignore",
    )
    parsed = list(csv.reader(csv_text.splitlines()))
    if not parsed:
        return []
    if parsed[0] and parsed[0][0].strip().lower() == "open_time":
        iterator = (dict(zip(parsed[0], values)) for values in parsed[1:])
    else:
        iterator = (dict(zip(fields, values)) for values in parsed)

    normalized: list[dict[str, Any]] = []
    for raw in iterator:
        try:
            open_price = float(raw["open"])
            high = float(raw["high"])
            low = float(raw["low"])
            close = float(raw["close"])
            quote_volume = float(raw["quote_volume"])
            taker_buy_quote = float(raw["taker_buy_quote_volume"])
            trade_count = int(raw["count"])
            close_time = int(raw["close_time"])
        except (KeyError, TypeError, ValueError):
            continue
        # Binance spot archives switched from milliseconds to microseconds on
        # 2025-01-01.  Convert both representations to the first usable ms.
        if close_time >= 100_000_000_000_000:
            close_time //= 1000
        if close <= 0 or quote_volume < 0 or taker_buy_quote < 0:
            continue
        taker_buy_quote = min(taker_buy_quote, quote_volume)
        taker_sell_quote = max(0.0, quote_volume - taker_buy_quote)
        normalized.append({
            "ts": close_time + 1,
            "buy_flow_ratio": (
                taker_buy_quote / quote_volume if quote_volume > 0 else 0.5
            ),
            "taker_buy_quote": taker_buy_quote,
            "taker_sell_quote": taker_sell_quote,
            "quote_volume": quote_volume,
            "trade_count": trade_count,
            "avg_trade_notional": (
                quote_volume / trade_count if trade_count > 0 else 0.0
            ),
            "max_trade_notional": None,
            "return_bps": (
                ((close - open_price) / open_price) * 10_000
                if open_price > 0
                else 0.0
            ),
            "range_bps": ((high - low) / close) * 10_000,
        })
    return sorted(normalized, key=lambda row: row["ts"])


class BybitPublicClient:
    """Client for Bybit's keyless trade archive and V5 market endpoints."""

    ARCHIVE_URL = "https://public.bybit.com"
    API_URL = "https://api.bybit.com"

    def __init__(
        self,
        cache_dir: str = "runtime/cross_venue_cache",
        timeout: float = 90.0,
        retain_trade_archives: bool = False,
        download_workers: int = 4,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.retain_trade_archives = retain_trade_archives
        self.download_workers = max(1, min(int(download_workers), 8))
        self._archive = httpx.Client(base_url=self.ARCHIVE_URL, timeout=timeout)
        self._api = httpx.Client(base_url=self.API_URL, timeout=timeout)

    def close(self) -> None:
        self._archive.close()
        self._api.close()

    def fetch_trade_day(
        self, symbol: str, day: date, *, _retry_corrupt_cache: bool = True
    ) -> dict[str, Any] | None:
        requested = symbol.upper()
        remote = bybit_archive_symbol(requested)
        name = f"{remote}{day:%Y-%m-%d}.csv.gz"
        target = self.cache_dir / "bybit_trades" / remote / name
        target.parent.mkdir(parents=True, exist_ok=True)
        downloaded = False
        if not target.exists():
            part = target.with_suffix(target.suffix + ".part")
            digest = hashlib.sha256()
            byte_count = 0
            try:
                with self._archive.stream("GET", f"/trading/{remote}/{name}") as response:
                    if response.status_code == 404:
                        return None
                    response.raise_for_status()
                    with part.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            handle.write(chunk)
                            digest.update(chunk)
                            byte_count += len(chunk)
                os.replace(part, target)
                downloaded = True
            except Exception:
                part.unlink(missing_ok=True)
                raise
            sha256 = digest.hexdigest()
        else:
            byte_count = target.stat().st_size
            sha256 = _sha256_file(target)

        try:
            with gzip.open(target, "rt", encoding="utf-8", newline="") as handle:
                rows = normalize_bybit_trade_rows(handle)
        except (gzip.BadGzipFile, OSError, UnicodeDecodeError, zlib.error):
            # A cached object may be truncated by an interrupted process.  It
            # must not poison every subsequent resumable retry.
            target.unlink(missing_ok=True)
            if not downloaded and _retry_corrupt_cache:
                return self.fetch_trade_day(
                    symbol, day, _retry_corrupt_cache=False
                )
            raise
        result = {
            "requested_symbol": requested,
            "exchange_symbol": remote,
            "rows": rows,
            "bytes_downloaded": byte_count,
            "sha256": sha256,
        }
        if not self.retain_trade_archives:
            target.unlink(missing_ok=True)
        return result

    def _paged(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = ""
        seen_cursors: set[str] = set()
        while True:
            query = dict(params)
            if cursor:
                query["cursor"] = cursor
            response = self._api.get(path, params=query)
            response.raise_for_status()
            payload = response.json()
            if int(payload.get("retCode", -1)) != 0:
                raise RuntimeError(
                    f"Bybit {path} failed: {payload.get('retMsg', 'unknown error')}"
                )
            result = payload.get("result") or {}
            rows.extend(result.get("list") or [])
            next_cursor = str(result.get("nextPageCursor") or "")
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return rows

    def fetch_positioning_day(self, symbol: str, day: date) -> dict[str, Any]:
        requested = symbol.upper()
        remote = bybit_archive_symbol(requested)
        start_ts = int(
            datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp()
            * 1000
        )
        end_ts = start_ts + 86_400_000 - 1
        common = {
            "category": "linear",
            "symbol": remote,
            "startTime": start_ts,
            "endTime": end_ts,
            "limit": 200,
        }
        oi_raw = self._paged(
            "/v5/market/open-interest",
            {**common, "intervalTime": "5min"},
        )
        ratio_raw = self._paged(
            "/v5/market/account-ratio",
            {**common, "period": "5min"},
        )
        funding_raw = self._paged("/v5/market/funding/history", common)

        positions: dict[int, dict[str, Any]] = {}
        for raw in oi_raw:
            try:
                ts = int(raw["timestamp"])
                value = float(raw["openInterest"])
            except (KeyError, TypeError, ValueError):
                continue
            positions.setdefault(ts, {"ts": ts})["open_interest"] = value
        for raw in ratio_raw:
            try:
                ts = int(raw["timestamp"])
                buy = float(raw["buyRatio"])
                sell = float(raw["sellRatio"])
            except (KeyError, TypeError, ValueError):
                continue
            row = positions.setdefault(ts, {"ts": ts})
            row["long_ratio"] = buy
            row["short_ratio"] = sell
        funding: list[dict[str, Any]] = []
        for raw in funding_raw:
            try:
                funding.append({
                    "ts": int(raw["fundingRateTimestamp"]),
                    "funding_rate": float(raw["fundingRate"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
        canonical = json.dumps(
            {"positioning": sorted(positions.values(), key=lambda row: row["ts"]),
             "funding": sorted(funding, key=lambda row: row["ts"])},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return {
            "requested_symbol": requested,
            "exchange_symbol": remote,
            "positioning": sorted(positions.values(), key=lambda row: row["ts"]),
            "funding": sorted(funding, key=lambda row: row["ts"]),
            "bytes_downloaded": len(canonical),
            "sha256": hashlib.sha256(canonical).hexdigest(),
        }


class BinanceSpotArchiveClient:
    """Keyless Binance spot five-minute kline archive client."""

    BASE_URL = "https://data.binance.vision"

    def __init__(
        self,
        cache_dir: str = "runtime/cross_venue_cache",
        timeout: float = 90.0,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(base_url=self.BASE_URL, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _fetch_zip(self, remote_path: str, target: Path) -> dict[str, Any] | None:
        import io
        import zipfile

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            content = target.read_bytes()
        else:
            response = self._client.get(remote_path)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            content = response.content
            target.write_bytes(content)
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
            text = archive.read(archive.namelist()[0]).decode("utf-8")
        except (zipfile.BadZipFile, IndexError, UnicodeDecodeError):
            return None
        return {
            "rows": normalize_binance_spot_kline_rows(text),
            "bytes_downloaded": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def fetch_month(self, symbol: str, month: date) -> dict[str, Any] | None:
        remote = symbol.upper()
        name = f"{remote}-5m-{month:%Y-%m}.zip"
        return self._fetch_zip(
            f"/data/spot/monthly/klines/{remote}/5m/{name}",
            self.cache_dir / "binance_spot_5m" / remote / name,
        )

    def fetch_day(self, symbol: str, day: date) -> dict[str, Any] | None:
        remote = symbol.upper()
        name = f"{remote}-5m-{day:%Y-%m-%d}.zip"
        return self._fetch_zip(
            f"/data/spot/daily/klines/{remote}/5m/{name}",
            self.cache_dir / "binance_spot_5m" / remote / name,
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class CrossVenueStore:
    def __init__(self, db_path: str = "runtime/cross_venue.db") -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS venue_trade_flow(
                source TEXT NOT NULL,
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                ts INTEGER NOT NULL,
                buy_flow_ratio REAL NOT NULL,
                taker_buy_quote REAL NOT NULL,
                taker_sell_quote REAL NOT NULL,
                quote_volume REAL NOT NULL,
                trade_count INTEGER NOT NULL,
                avg_trade_notional REAL NOT NULL,
                max_trade_notional REAL,
                return_bps REAL NOT NULL,
                PRIMARY KEY(source, market, symbol, ts)
            );
            CREATE TABLE IF NOT EXISTS venue_positioning(
                source TEXT NOT NULL,
                symbol TEXT NOT NULL,
                ts INTEGER NOT NULL,
                open_interest REAL,
                long_ratio REAL,
                short_ratio REAL,
                PRIMARY KEY(source, symbol, ts)
            );
            CREATE TABLE IF NOT EXISTS venue_funding(
                source TEXT NOT NULL,
                symbol TEXT NOT NULL,
                ts INTEGER NOT NULL,
                funding_rate REAL NOT NULL,
                PRIMARY KEY(source, symbol, ts)
            );
            CREATE TABLE IF NOT EXISTS cross_venue_ingest(
                source TEXT NOT NULL,
                data_type TEXT NOT NULL,
                symbol TEXT NOT NULL,
                period TEXT NOT NULL,
                status TEXT NOT NULL,
                rows_stored INTEGER NOT NULL,
                bytes_downloaded INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT,
                error TEXT,
                updated_ts INTEGER NOT NULL,
                PRIMARY KEY(source, data_type, symbol, period)
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _complete(self, source: str, data_type: str, symbol: str, period: str) -> bool:
        row = self._conn.execute(
            """SELECT status FROM cross_venue_ingest
               WHERE source=? AND data_type=? AND symbol=? AND period=?""",
            (source, data_type, symbol.upper(), period),
        ).fetchone()
        return bool(row and row[0] == "complete")

    def _record(
        self,
        source: str,
        data_type: str,
        symbol: str,
        period: str,
        status: str,
        rows: int,
        *,
        bytes_downloaded: int = 0,
        sha256: str | None = None,
        error: str | None = None,
    ) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO cross_venue_ingest
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                source,
                data_type,
                symbol.upper(),
                period,
                status,
                rows,
                bytes_downloaded,
                sha256,
                error,
                int(datetime.now(timezone.utc).timestamp() * 1000),
            ),
        )
        self._conn.commit()

    def upsert_trade_flow(
        self, source: str, market: str, symbol: str, rows: list[dict[str, Any]]
    ) -> int:
        self._conn.executemany(
            """INSERT OR REPLACE INTO venue_trade_flow
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(
                source,
                market,
                symbol.upper(),
                row["ts"],
                row["buy_flow_ratio"],
                row["taker_buy_quote"],
                row["taker_sell_quote"],
                row["quote_volume"],
                row["trade_count"],
                row["avg_trade_notional"],
                row.get("max_trade_notional"),
                row["return_bps"],
            ) for row in rows],
        )
        self._conn.commit()
        return len(rows)

    def upsert_positioning(self, source: str, symbol: str, rows: list[dict[str, Any]]) -> int:
        self._conn.executemany(
            """INSERT OR REPLACE INTO venue_positioning VALUES(?,?,?,?,?,?)""",
            [(
                source,
                symbol.upper(),
                row["ts"],
                row.get("open_interest"),
                row.get("long_ratio"),
                row.get("short_ratio"),
            ) for row in rows],
        )
        self._conn.commit()
        return len(rows)

    def upsert_funding(self, source: str, symbol: str, rows: list[dict[str, Any]]) -> int:
        self._conn.executemany(
            "INSERT OR REPLACE INTO venue_funding VALUES(?,?,?,?)",
            [(source, symbol.upper(), row["ts"], row["funding_rate"]) for row in rows],
        )
        self._conn.commit()
        return len(rows)

    def ingest_bybit_trades_range(
        self, client: BybitPublicClient, symbol: str, start_day: date, end_day: date
    ) -> dict[str, Any]:
        return self._ingest_days(
            "bybit",
            "perpetual_trades",
            symbol,
            start_day,
            end_day,
            client.fetch_trade_day,
            lambda result: self.upsert_trade_flow(
                "bybit", "perpetual", symbol, result["rows"]
            ),
            workers=getattr(client, "download_workers", 1),
        )

    def ingest_bybit_positioning_range(
        self, client: BybitPublicClient, symbol: str, start_day: date, end_day: date
    ) -> dict[str, Any]:
        def store(result: dict[str, Any]) -> int:
            count = self.upsert_positioning(
                "bybit", symbol, result["positioning"]
            )
            self.upsert_funding("bybit", symbol, result["funding"])
            return count + len(result["funding"])

        return self._ingest_days(
            "bybit",
            "positioning",
            symbol,
            start_day,
            end_day,
            client.fetch_positioning_day,
            store,
            workers=min(getattr(client, "download_workers", 1), 3),
        )

    def _ingest_days(
        self,
        source: str,
        data_type: str,
        symbol: str,
        start_day: date,
        end_day: date,
        fetch: Any,
        store: Any,
        *,
        workers: int = 1,
    ) -> dict[str, Any]:
        if end_day < start_day:
            raise ValueError("end_day must be on or after start_day")
        stored = complete = missing = failed = bytes_downloaded = 0
        pending: list[date] = []
        current = start_day
        while current <= end_day:
            if self._complete(source, data_type, symbol, current.isoformat()):
                complete += 1
            else:
                pending.append(current)
            current += timedelta(days=1)

        def consume(current_day: date, result: dict[str, Any] | None) -> None:
            nonlocal stored, complete, missing, bytes_downloaded
            period = current_day.isoformat()
            if result is None:
                self._record(source, data_type, symbol, period, "missing", 0)
                missing += 1
            else:
                count = store(result)
                byte_count = int(result.get("bytes_downloaded", 0))
                self._record(
                    source,
                    data_type,
                    symbol,
                    period,
                    "complete",
                    count,
                    bytes_downloaded=byte_count,
                    sha256=result.get("sha256"),
                )
                stored += count
                bytes_downloaded += byte_count
                complete += 1

        if workers > 1 and len(pending) > 1:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_days = {
                    executor.submit(fetch, symbol, current_day): current_day
                    for current_day in pending
                }
                for future in as_completed(future_days):
                    current_day = future_days[future]
                    try:
                        consume(current_day, future.result())
                    except Exception as exc:
                        self._record(
                            source,
                            data_type,
                            symbol,
                            current_day.isoformat(),
                            "failed",
                            0,
                            error=str(exc)[:1000],
                        )
                        failed += 1
            return {
                "stored_rows": stored,
                "complete_days": complete,
                "missing_days": missing,
                "failed_days": failed,
                "bytes_downloaded": bytes_downloaded,
            }

        for current in pending:
            period = current.isoformat()
            if self._complete(source, data_type, symbol, period):
                complete += 1
                continue
            try:
                consume(current, fetch(symbol, current))
            except Exception as exc:
                self._record(
                    source,
                    data_type,
                    symbol,
                    period,
                    "failed",
                    0,
                    error=str(exc)[:1000],
                )
                failed += 1
        return {
            "stored_rows": stored,
            "complete_days": complete,
            "missing_days": missing,
            "failed_days": failed,
            "bytes_downloaded": bytes_downloaded,
        }

    def ingest_binance_spot_range(
        self,
        client: BinanceSpotArchiveClient,
        symbol: str,
        start_day: date,
        end_day: date,
    ) -> dict[str, Any]:
        if end_day < start_day:
            raise ValueError("end_day must be on or after start_day")
        stored = complete_months = complete_days = missing = failed = 0
        month = start_day.replace(day=1)
        while month <= end_day:
            next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
            month_end = next_month - timedelta(days=1)
            range_start = max(start_day, month)
            range_end = min(end_day, month_end)
            full_month = range_start == month and range_end == month_end
            month_period = month.strftime("%Y-%m")
            if full_month and self._complete("binance", "spot_kline5m_month", symbol, month_period):
                complete_months += 1
            elif full_month:
                try:
                    result = client.fetch_month(symbol, month)
                    if result is not None:
                        count = self.upsert_trade_flow(
                            "binance", "spot", symbol, result["rows"]
                        )
                        self._record(
                            "binance", "spot_kline5m_month", symbol, month_period,
                            "complete", count,
                            bytes_downloaded=result["bytes_downloaded"],
                            sha256=result["sha256"],
                        )
                        stored += count
                        complete_months += 1
                        month = next_month
                        continue
                except Exception as exc:
                    self._record(
                        "binance", "spot_kline5m_month", symbol, month_period,
                        "failed", 0, error=str(exc)[:1000],
                    )
            current = range_start
            while current <= range_end:
                period = current.isoformat()
                if self._complete("binance", "spot_kline5m_day", symbol, period):
                    complete_days += 1
                    current += timedelta(days=1)
                    continue
                try:
                    result = client.fetch_day(symbol, current)
                    if result is None:
                        self._record(
                            "binance", "spot_kline5m_day", symbol, period, "missing", 0
                        )
                        missing += 1
                    else:
                        count = self.upsert_trade_flow(
                            "binance", "spot", symbol, result["rows"]
                        )
                        self._record(
                            "binance", "spot_kline5m_day", symbol, period,
                            "complete", count,
                            bytes_downloaded=result["bytes_downloaded"],
                            sha256=result["sha256"],
                        )
                        stored += count
                        complete_days += 1
                except Exception as exc:
                    self._record(
                        "binance", "spot_kline5m_day", symbol, period,
                        "failed", 0, error=str(exc)[:1000],
                    )
                    failed += 1
                current += timedelta(days=1)
            month = next_month
        return {
            "stored_rows": stored,
            "complete_months": complete_months,
            "complete_days": complete_days,
            "missing_periods": missing,
            "failed_periods": failed,
        }

    def nearest_trade_flow(
        self,
        source: str,
        market: str,
        symbol: str,
        ts: int,
        tolerance_ms: int = 600_000,
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT ts, buy_flow_ratio, taker_buy_quote, taker_sell_quote,
                      quote_volume, trade_count, avg_trade_notional,
                      max_trade_notional, return_bps
               FROM venue_trade_flow
               WHERE source=? AND market=? AND symbol=? AND ts<=? AND ts>=?
               ORDER BY ts DESC LIMIT 1""",
            (source, market, symbol.upper(), ts, ts - tolerance_ms),
        ).fetchone()
        if not row:
            return None
        keys = (
            "ts", "buy_flow_ratio", "taker_buy_quote", "taker_sell_quote",
            "quote_volume", "trade_count", "avg_trade_notional",
            "max_trade_notional", "return_bps",
        )
        return dict(zip(keys, row))

    def nearest_positioning(
        self, source: str, symbol: str, ts: int, tolerance_ms: int = 600_000
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT ts, open_interest, long_ratio, short_ratio
               FROM venue_positioning
               WHERE source=? AND symbol=? AND ts<=? AND ts>=?
               ORDER BY ts DESC LIMIT 1""",
            (source, symbol.upper(), ts, ts - tolerance_ms),
        ).fetchone()
        if not row:
            return None
        result = dict(zip(("ts", "open_interest", "long_ratio", "short_ratio"), row))
        if result["open_interest"] is not None:
            comparison_ts = int(result["ts"]) - 3_600_000
            previous = self._conn.execute(
                """SELECT ts, open_interest FROM venue_positioning
                   WHERE source=? AND symbol=? AND ts<=? AND open_interest IS NOT NULL
                   ORDER BY ts DESC LIMIT 1""",
                (source, symbol.upper(), comparison_ts),
            ).fetchone()
            if (
                previous
                and int(previous[0]) >= comparison_ts - 600_000
                and previous[1]
                and float(previous[1]) > 0
            ):
                result["open_interest_change_1h_pct"] = (
                    (float(result["open_interest"]) - float(previous[1]))
                    / float(previous[1])
                    * 100
                )
        return result

    def nearest_funding(
        self, source: str, symbol: str, ts: int, tolerance_ms: int = 43_200_000
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT ts, funding_rate FROM venue_funding
               WHERE source=? AND symbol=? AND ts<=? AND ts>=?
               ORDER BY ts DESC LIMIT 1""",
            (source, symbol.upper(), ts, ts - tolerance_ms),
        ).fetchone()
        return dict(zip(("ts", "funding_rate"), row)) if row else None

    def coverage(self, source: str, data_type: str, symbol: str) -> dict[str, Any]:
        rows = self._conn.execute(
            """SELECT status, COUNT(*), COALESCE(SUM(rows_stored), 0),
                      COALESCE(SUM(bytes_downloaded), 0)
               FROM cross_venue_ingest
               WHERE source=? AND data_type=? AND symbol=? GROUP BY status""",
            (source, data_type, symbol.upper()),
        ).fetchall()
        statuses = {
            status: {"periods": count, "rows": stored, "bytes": byte_count}
            for status, count, stored, byte_count in rows
        }
        return {"symbol": symbol.upper(), "statuses": statuses}

    def feature_coverage(
        self, training_db: str, symbols: list[str], tolerance_ms: int = 600_000
    ) -> dict[str, Any]:
        """Measure usable source rows at the actual snapshot timestamps."""
        training = sqlite3.connect(training_db)
        placeholders = ",".join("?" for _ in symbols)
        anchors = training.execute(
            f"""SELECT symbol, event_ts FROM training_snapshots
                WHERE symbol IN ({placeholders}) ORDER BY event_ts""",
            tuple(symbol.upper() for symbol in symbols),
        ).fetchall()
        training.close()
        families = {"bybit_trades": 0, "bybit_positioning": 0, "binance_spot": 0}
        by_symbol: dict[str, dict[str, int]] = defaultdict(
            lambda: {"anchors": 0, **{key: 0 for key in families}}
        )
        for symbol, ts in anchors:
            by_symbol[symbol]["anchors"] += 1
            checks = {
                "bybit_trades": self.nearest_trade_flow(
                    "bybit", "perpetual", symbol, ts, tolerance_ms
                ),
                "bybit_positioning": self.nearest_positioning(
                    "bybit", symbol, ts, tolerance_ms
                ),
                "binance_spot": self.nearest_trade_flow(
                    "binance", "spot", symbol, ts, tolerance_ms
                ),
            }
            for family, row in checks.items():
                if row is not None:
                    families[family] += 1
                    by_symbol[symbol][family] += 1
        total = len(anchors)
        return {
            "anchors": total,
            "coverage": {
                key: (count / total if total else 0.0) for key, count in families.items()
            },
            "by_symbol": {
                symbol: {
                    **counts,
                    "coverage": {
                        key: counts[key] / counts["anchors"] if counts["anchors"] else 0.0
                        for key in families
                    },
                }
                for symbol, counts in sorted(by_symbol.items())
            },
        }


def attach_cross_venue_features(
    features: dict[str, Any],
    store: CrossVenueStore,
    symbol: str,
    event_ts: int,
) -> None:
    """Attach source-isolated values and explicitly named cross-market spreads."""
    bybit_flow = store.nearest_trade_flow("bybit", "perpetual", symbol, event_ts)
    spot_flow = store.nearest_trade_flow("binance", "spot", symbol, event_ts)
    positioning = store.nearest_positioning("bybit", symbol, event_ts)
    funding = store.nearest_funding("bybit", symbol, event_ts)

    def add_flow(prefix: str, row: dict[str, Any]) -> None:
        buy_ratio = float(row["buy_flow_ratio"])
        features[f"{prefix}.buy_flow_ratio"] = buy_ratio
        features[f"{prefix}.taker_imbalance"] = (2 * buy_ratio) - 1
        features[f"{prefix}.quote_volume_log"] = math.log1p(float(row["quote_volume"]))
        features[f"{prefix}.trade_count_log"] = math.log1p(int(row["trade_count"]))
        features[f"{prefix}.avg_trade_notional_log"] = math.log1p(
            float(row["avg_trade_notional"])
        )
        if row.get("max_trade_notional") is not None:
            features[f"{prefix}.max_trade_notional_log"] = math.log1p(
                float(row["max_trade_notional"])
            )
        features[f"{prefix}.return_bps"] = float(row["return_bps"])

    if bybit_flow:
        add_flow("xvenue.bybit_perp", bybit_flow)
    if spot_flow:
        add_flow("xvenue.binance_spot", spot_flow)
    if bybit_flow and spot_flow:
        features["xvenue.spot_perp.buy_ratio_spread"] = (
            float(spot_flow["buy_flow_ratio"]) - float(bybit_flow["buy_flow_ratio"])
        )
        features["xvenue.spot_perp.return_spread_bps"] = (
            float(spot_flow["return_bps"]) - float(bybit_flow["return_bps"])
        )
        features["xvenue.spot_perp.log_volume_ratio"] = math.log(
            (float(spot_flow["quote_volume"]) + 1.0)
            / (float(bybit_flow["quote_volume"]) + 1.0)
        )
    if positioning:
        open_interest = positioning.get("open_interest")
        if open_interest is not None and float(open_interest) >= 0:
            features["xvenue.bybit_positioning.open_interest_log"] = math.log1p(
                float(open_interest)
            )
        for source_key, target_key in (
            ("long_ratio", "long_ratio"),
            ("short_ratio", "short_ratio"),
            ("open_interest_change_1h_pct", "oi_change_1h_pct"),
        ):
            value = positioning.get(source_key)
            if value is not None:
                features[f"xvenue.bybit_positioning.{target_key}"] = float(value)
    if funding:
        features["xvenue.bybit_positioning.funding_rate_bps"] = (
            float(funding["funding_rate"]) * 10_000
        )


def require_cross_venue_coverage(
    manifest: dict[str, Any],
    store: CrossVenueStore,
    training_db: str,
    symbols: list[str],
    *,
    minimum_coverage: float = 0.95,
) -> dict[str, Any]:
    """Add the actual-anchor cross-venue gate to a completeness manifest."""
    if not 0 <= minimum_coverage <= 1:
        raise ValueError("minimum_coverage must be between zero and one")
    report = store.feature_coverage(training_db, symbols)
    observed = list(report["coverage"].values())
    minimum_observed = min(observed) if observed else 0.0
    check = {
        **report,
        "minimum_coverage": round(minimum_observed, 4),
        "required_coverage": minimum_coverage,
        "complete": minimum_observed >= minimum_coverage,
    }
    manifest.setdefault("checks", {})["cross_venue"] = check
    required = manifest.setdefault("required_checks", [])
    if "cross_venue" not in required:
        required.append("cross_venue")
    manifest["cross_venue_db"] = store.db_path
    manifest["complete"] = bool(manifest.get("complete") and check["complete"])
    return manifest
