"""Recover exchange liquidation events from CryptoHFTData's public archive."""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
from collections import deque
from collections.abc import Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from liquidity_signal.models import Direction, LiquidationEventPoint
from liquidity_signal.service.liquidation_store import LiquidationStore


API_URL = "https://api.cryptohftdata.com/v1/download"
VENUE_IDS = {"binance": "binance_futures", "bybit": "bybit"}
SYMBOL_ALIASES = {"PEPEUSDT": ("1000PEPEUSDT", 1_000.0)}
PROVENANCE_PREFIX = "cryptohft_recovery"
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _positive_float(*values: Any) -> float | None:
    for value in values:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _zstd_frame_content_size(payload: bytes) -> int:
    """Read the standard frame content size needed by PyArrow's Zstd codec."""
    if len(payload) < 6 or payload[:4] != _ZSTD_MAGIC:
        raise ValueError("Not a standard Zstandard frame")
    descriptor = payload[4]
    if descriptor & 0x18:
        raise ValueError("Unsupported reserved bits in Zstandard frame header")
    content_size_flag = descriptor >> 6
    single_segment = bool(descriptor & 0x20)
    dictionary_flag = descriptor & 0x03
    offset = 5 if single_segment else 6
    dictionary_size = (0, 1, 2, 4)[dictionary_flag]
    offset += dictionary_size
    if content_size_flag == 0:
        content_size_bytes = 1 if single_segment else 0
    else:
        content_size_bytes = 1 << content_size_flag
    if content_size_bytes == 0 or len(payload) < offset + content_size_bytes:
        raise ValueError("Zstandard frame does not declare its content size")
    content_size = int.from_bytes(
        payload[offset : offset + content_size_bytes], "little"
    )
    if content_size_bytes == 2:
        content_size += 256
    return content_size


def normalize_cryptohft_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    venue: str,
    requested_symbol: str,
    start_ts: int,
    end_ts: int,
) -> list[LiquidationEventPoint]:
    """Normalize archived rows while preserving live-feed event identifiers."""
    source = venue.lower()
    if source not in VENUE_IDS:
        raise ValueError(f"Unsupported CryptoHFTData venue: {venue}")
    symbol = requested_symbol.upper()
    exchange_symbol, multiplier = SYMBOL_ALIASES.get(symbol, (symbol, 1.0))
    events: list[LiquidationEventPoint] = []

    for row in rows:
        if str(row.get("symbol", "")).upper() != exchange_symbol:
            continue
        try:
            event_ts = int(row.get("trade_time", 0))
            received_ns = int(row.get("received_time", 0))
        except (TypeError, ValueError):
            continue
        if event_ts < start_ts or event_ts >= end_ts:
            continue

        execution_side = str(row.get("side", "")).upper()
        if execution_side == "SELL":
            liquidated_side = Direction.LONG
        elif execution_side == "BUY":
            liquidated_side = Direction.SHORT
        else:
            continue
        archive_price = _positive_float(row.get("average_price"), row.get("price"))
        archive_quantity = _positive_float(
            row.get("filled_quantity"),
            row.get("last_filled_quantity"),
            row.get("quantity"),
        )
        if archive_price is None or archive_quantity is None:
            continue

        price = archive_price / multiplier
        quantity = archive_quantity * multiplier
        # Bybit's raw allLiquidation side describes the liquidated position;
        # CryptoHFTData exposes the resulting execution side instead.
        id_side = (
            ("SELL" if execution_side == "BUY" else "BUY")
            if source == "bybit"
            else execution_side
        )
        event_id = (
            f"{source}:{exchange_symbol}:{event_ts}:{id_side}:{quantity}:{price}"
        )
        events.append(
            LiquidationEventPoint(
                source=source,
                symbol=symbol,
                liquidated_side=liquidated_side,
                execution_side=execution_side,
                price=price,
                quantity=quantity,
                notional=price * quantity,
                timestamp=event_ts,
                received_at=received_ns // 1_000_000 if received_ns > 0 else None,
                exchange_event_id=event_id,
            )
        )
    return events


@dataclass(frozen=True)
class ArchiveFetch:
    status: str
    rows: list[dict[str, Any]]
    evidence: str
    cache_hit: bool = False


class CryptoHFTArchiveClient:
    """Rate-limited, resumable reader for hourly public Parquet objects."""

    def __init__(
        self,
        cache_dir: str = "runtime/cryptohft_liquidation_cache",
        timeout: float = 120.0,
        requests_per_minute: int = 55,
        max_retries: int = 5,
        download_workers: int = 8,
    ) -> None:
        if requests_per_minute < 1:
            raise ValueError("requests_per_minute must be positive")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.requests_per_minute = requests_per_minute
        self.max_retries = max_retries
        self.download_workers = max(1, download_workers)
        self._request_times: deque[float] = deque()
        self._rate_lock = threading.Lock()
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def remote_path(venue: str, symbol: str, hour: datetime) -> str:
        source = venue.lower()
        if source not in VENUE_IDS:
            raise ValueError(f"Unsupported CryptoHFTData venue: {venue}")
        normalized = symbol.upper()
        exchange_symbol = SYMBOL_ALIASES.get(normalized, (normalized, 1.0))[0]
        utc_hour = _utc_datetime(hour)
        return (
            f"{VENUE_IDS[source]}/{utc_hour:%Y-%m-%d}/{utc_hour:%H}/"
            f"{exchange_symbol}_liquidations.parquet"
        )

    def _wait_for_request_slot(self) -> None:
        with self._rate_lock:
            now = time.monotonic()
            while self._request_times and now - self._request_times[0] >= 60.0:
                self._request_times.popleft()
            if len(self._request_times) >= self.requests_per_minute:
                wait_for = 60.0 - (now - self._request_times[0]) + 0.05
                if wait_for > 0:
                    time.sleep(wait_for)
                now = time.monotonic()
                while (
                    self._request_times
                    and now - self._request_times[0] >= 60.0
                ):
                    self._request_times.popleft()
            self._request_times.append(time.monotonic())

    @staticmethod
    def _read_parquet(path: Path) -> list[dict[str, Any]]:
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "Parquet ingestion requires the project ML extras: pip install -e .[ml]"
            ) from exc
        prefix = path.read_bytes()[:4]
        if prefix == _ZSTD_MAGIC:
            try:
                import pyarrow as pa
            except ImportError as exc:  # pragma: no cover - pandas engine dependency
                raise RuntimeError(
                    "Legacy archive decompression requires pyarrow."
                ) from exc
            compressed = path.read_bytes()
            content_size = _zstd_frame_content_size(compressed)
            decompressed = pa.Codec("zstd").decompress(compressed, content_size)
            frame = pd.read_parquet(io.BytesIO(bytes(decompressed)))
        else:
            frame = pd.read_parquet(path)
        required = {
            "received_time",
            "trade_time",
            "symbol",
            "side",
            "price",
            "average_price",
            "quantity",
            "filled_quantity",
        }
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(
                f"Unexpected CryptoHFTData schema in {path}: missing {sorted(missing)}"
            )
        return frame.to_dict(orient="records")

    def fetch_hour(self, venue: str, symbol: str, hour: datetime) -> ArchiveFetch:
        remote_path = self.remote_path(venue, symbol, hour)
        cache_file = self.cache_dir / remote_path
        missing_file = cache_file.with_suffix(cache_file.suffix + ".missing")
        temp_file = cache_file.with_suffix(cache_file.suffix + ".part")
        stable_url = f"{API_URL}?file={remote_path}"
        if cache_file.exists():
            digest = hashlib.sha256(cache_file.read_bytes()).hexdigest()
            return ArchiveFetch(
                "complete",
                self._read_parquet(cache_file),
                f"{stable_url}#sha256={digest}",
                cache_hit=True,
            )
        if missing_file.exists():
            return ArchiveFetch("no_events", [], stable_url, cache_hit=True)
        if temp_file.exists():
            try:
                rows = self._read_parquet(temp_file)
            except (OSError, ValueError):
                pass
            else:
                digest = hashlib.sha256(temp_file.read_bytes()).hexdigest()
                temp_file.replace(cache_file)
                return ArchiveFetch(
                    "complete",
                    rows,
                    f"{stable_url}#sha256={digest}",
                    cache_hit=True,
                )

        cache_file.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(self.max_retries + 1):
            self._wait_for_request_slot()
            try:
                response = self._client.get(API_URL, params={"file": remote_path})
            except httpx.HTTPError:
                if attempt >= self.max_retries:
                    raise
                time.sleep(min(2**attempt, 30))
                continue
            if response.status_code == 404:
                missing_file.write_text(
                    "CryptoHFTData publishes an object only for symbol-hours with events.\n",
                    encoding="utf-8",
                )
                return ArchiveFetch("no_events", [], stable_url)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self.max_retries:
                    response.raise_for_status()
                retry_after = response.headers.get("Retry-After", "")
                try:
                    wait_for = float(retry_after)
                except ValueError:
                    wait_for = min(2**attempt, 30)
                time.sleep(max(wait_for, 1.0))
                continue
            response.raise_for_status()
            temp_file.write_bytes(response.content)
            rows = self._read_parquet(temp_file)
            temp_file.replace(cache_file)
            digest = hashlib.sha256(response.content).hexdigest()
            return ArchiveFetch(
                "complete", rows, f"{stable_url}#sha256={digest}"
            )
        raise RuntimeError(f"Retries exhausted for {remote_path}")  # pragma: no cover


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(
        f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def ingest_cryptohft_range(
    client: CryptoHFTArchiveClient,
    store: LiquidationStore,
    start: datetime,
    end: datetime,
    symbols: list[str],
    *,
    venues: list[str] | None = None,
    report_path: str = "runtime/cryptohft_recovery_status.json",
) -> dict[str, Any]:
    """Recover a half-open UTC interval and persist hourly audit provenance."""
    start_utc = _utc_datetime(start)
    end_utc = _utc_datetime(end)
    if end_utc <= start_utc:
        raise ValueError("end must be after start")
    selected_symbols = sorted({symbol.upper() for symbol in symbols})
    selected_venues = [venue.lower() for venue in (venues or list(VENUE_IDS))]
    unknown = set(selected_venues) - set(VENUE_IDS)
    if unknown:
        raise ValueError(f"Unsupported CryptoHFTData venues: {sorted(unknown)}")

    first_hour = start_utc.replace(minute=0, second=0, microsecond=0)
    hours: list[datetime] = []
    current = first_hour
    while current < end_utc:
        hours.append(current)
        current += timedelta(hours=1)
    total_partitions = len(hours) * len(selected_symbols) * len(selected_venues)
    report: dict[str, Any] = {
        "source": PROVENANCE_PREFIX,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "start": start_utc.isoformat(),
        "end_exclusive": end_utc.isoformat(),
        "symbols": selected_symbols,
        "venues": selected_venues,
        "total_partitions": total_partitions,
        "processed_partitions": 0,
        "downloaded_partitions": 0,
        "cached_partitions": 0,
        "zero_event_partitions": 0,
        "failed_partitions": 0,
        "events_processed": 0,
        "events_by_venue": {venue: 0 for venue in selected_venues},
        "events_by_symbol": {symbol: 0 for symbol in selected_symbols},
        "failures": [],
    }
    target_report = Path(report_path)
    _write_report(target_report, report)
    start_ts = int(start_utc.timestamp() * 1000)
    end_ts = int(end_utc.timestamp() * 1000)

    partitions = [
        (hour, venue, symbol)
        for hour in hours
        for venue in selected_venues
        for symbol in selected_symbols
    ]
    future_partitions: dict[
        Future[ArchiveFetch], tuple[datetime, str, str]
    ] = {}
    with ThreadPoolExecutor(max_workers=client.download_workers) as executor:
        for hour, venue, symbol in partitions:
            future = executor.submit(client.fetch_hour, venue, symbol, hour)
            future_partitions[future] = (hour, venue, symbol)

        for future in as_completed(future_partitions):
            hour, venue, symbol = future_partitions[future]
            period = hour.strftime("%Y-%m-%dT%H:00:00Z")
            provenance_source = f"{PROVENANCE_PREFIX}_{venue}"
            report["current_partition"] = {
                "venue": venue,
                "symbol": symbol,
                "hour": period,
            }
            try:
                fetched = future.result()
                events = normalize_cryptohft_rows(
                    fetched.rows,
                    venue=venue,
                    requested_symbol=symbol,
                    start_ts=start_ts,
                    end_ts=end_ts,
                )
                store.persist_events(events)
                store.record_archive_period(
                    provenance_source,
                    symbol,
                    period,
                    "complete",
                    len(events),
                    [fetched.evidence],
                )
                report["events_processed"] += len(events)
                report["events_by_venue"][venue] += len(events)
                report["events_by_symbol"][symbol] += len(events)
                if fetched.cache_hit:
                    report["cached_partitions"] += 1
                else:
                    report["downloaded_partitions"] += 1
                if fetched.status == "no_events":
                    report["zero_event_partitions"] += 1
            except Exception as exc:  # preserve progress and continue recovery
                store.record_archive_period(
                    provenance_source, symbol, period, "failed", 0, []
                )
                report["failed_partitions"] += 1
                report["failures"].append(
                    {
                        "venue": venue,
                        "symbol": symbol,
                        "hour": period,
                        "error": str(exc),
                    }
                )
            report["processed_partitions"] += 1
            report["updated_at"] = datetime.now(timezone.utc).isoformat()
            _write_report(target_report, report)

    report.pop("current_partition", None)
    report["status"] = "complete" if report["failed_partitions"] == 0 else "partial"
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    _write_report(target_report, report)
    return report
