"""Free Hyperliquid liquidation history published by Chainticks on Hugging Face."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from liquidity_signal.models import Direction, LiquidationEventPoint
from liquidity_signal.service.liquidation_store import LiquidationStore


REPO_ID = "Chainticks/perp-data"
DATASET_PREFIX = "hyperliquid_chain/liquidations"
SOURCE = "hyperliquid_archive"

# Hyperliquid names perpetuals by their base asset. Its kPEPE contract represents
# 1,000 PEPE, so both price and quantity must be rescaled before joining it to
# Binance's PEPEUSDT training rows. Notional is unchanged by that conversion.
ASSET_TO_SYMBOL: dict[str, tuple[str, float]] = {
    "BTC": ("BTCUSDT", 1.0),
    "ETH": ("ETHUSDT", 1.0),
    "SOL": ("SOLUSDT", 1.0),
    "XRP": ("XRPUSDT", 1.0),
    "NEAR": ("NEARUSDT", 1.0),
    "KPEPE": ("PEPEUSDT", 1_000.0),
    "PEPE": ("PEPEUSDT", 1.0),
}


def _timestamp_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _raw_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def normalize_hyperliquid_rows(
    rows: Iterable[Mapping[str, Any]],
    symbols: set[str] | None = None,
) -> list[LiquidationEventPoint]:
    """Normalize only the liquidated user's fill, excluding maker counterpart rows."""
    selected = {symbol.upper() for symbol in symbols} if symbols else None
    events: list[LiquidationEventPoint] = []
    for row in rows:
        mapped = ASSET_TO_SYMBOL.get(str(row.get("symbol", "")).upper())
        if not mapped:
            continue
        symbol, contract_multiplier = mapped
        if selected is not None and symbol not in selected:
            continue

        raw = _raw_payload(row.get("raw_json"))
        event = raw.get("event")
        if not isinstance(event, dict):
            continue
        liquidation = event.get("liquidation")
        if not isinstance(liquidation, dict):
            continue

        # The archive contains both sides of each matched fill. Only one row is
        # the position that was liquidated; retaining the other side would at
        # least double-count notional and sometimes reverse the direction.
        liquidated_user = str(liquidation.get("liquidatedUser", "")).lower()
        fill_user = str(event.get("user", "")).lower()
        if not liquidated_user or fill_user != liquidated_user:
            continue

        direction = str(event.get("dir", "")).lower()
        if direction == "close long":
            liquidated_side = Direction.LONG
            execution_side = "SELL"
        elif direction == "close short":
            liquidated_side = Direction.SHORT
            execution_side = "BUY"
        else:
            continue

        event_ts = _timestamp_ms(row.get("exchange_time"))
        received_at = _timestamp_ms(row.get("recorded_at"))
        try:
            archive_price = float(row.get("price", 0.0))
            archive_size = float(row.get("size", 0.0))
            notional = float(row.get("notional_usd", 0.0))
        except (TypeError, ValueError):
            continue
        price = archive_price / contract_multiplier
        quantity = archive_size * contract_multiplier
        if not event_ts or price <= 0 or quantity <= 0 or notional <= 0:
            continue

        liquidation_id = str(row.get("liquidation_id", ""))
        trade_id = str(event.get("tid", ""))
        block_number = str(row.get("block_number", ""))
        event_id = f"hyperliquid:{liquidation_id}:{block_number}:{trade_id}:{fill_user}"
        events.append(
            LiquidationEventPoint(
                source=SOURCE,
                symbol=symbol,
                liquidated_side=liquidated_side,
                execution_side=execution_side,
                price=price,
                quantity=quantity,
                notional=notional,
                timestamp=event_ts,
                received_at=received_at,
                exchange_event_id=event_id,
            )
        )
    return events


class HyperliquidArchiveClient:
    """Read CC-BY-4.0 daily Parquet partitions without an API key."""

    API_URL = f"https://huggingface.co/api/datasets/{REPO_ID}/tree/main"
    DOWNLOAD_URL = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"

    def __init__(
        self,
        cache_dir: str = "runtime/hyperliquid_liquidation_cache",
        timeout: float = 120.0,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def available_partitions(self) -> dict[date, list[str]]:
        response = self._client.get(
            f"{self.API_URL}/{DATASET_PREFIX}",
            params={"recursive": "true", "expand": "false", "limit": 1000},
        )
        response.raise_for_status()
        partitions: dict[date, list[str]] = defaultdict(list)
        for item in response.json():
            path = str(item.get("path", ""))
            if item.get("type") != "file" or not path.endswith(".parquet"):
                continue
            marker = "/date="
            if marker not in path:
                continue
            day_text = path.split(marker, 1)[1].split("/", 1)[0]
            try:
                day = date.fromisoformat(day_text)
            except ValueError:
                continue
            partitions[day].append(path)
        return {day: sorted(paths) for day, paths in sorted(partitions.items())}

    def fetch_partition(self, remote_path: str) -> list[dict[str, Any]]:
        cache_file = self.cache_dir / remote_path
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        if not cache_file.exists():
            response = self._client.get(f"{self.DOWNLOAD_URL}/{remote_path}")
            response.raise_for_status()
            cache_file.write_bytes(response.content)
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "Parquet ingestion requires the project ML extras: pip install -e .[ml]"
            ) from exc
        frame = pd.read_parquet(cache_file)
        return frame.to_dict(orient="records")


def ingest_hyperliquid_range(
    client: HyperliquidArchiveClient,
    store: LiquidationStore,
    start_day: date,
    end_day: date,
    symbols: list[str],
) -> dict[str, Any]:
    """Ingest every published partition and preserve unavailable dates as gaps."""
    if end_day < start_day:
        raise ValueError("end_day must be on or after start_day")
    selected = {symbol.upper() for symbol in symbols}
    partitions = client.available_partitions()
    summary: dict[str, Any] = {
        "source": SOURCE,
        "license": "CC-BY-4.0",
        "published_days": 0,
        "unavailable_days": 0,
        "events_stored": 0,
        "events_by_symbol": {symbol: 0 for symbol in sorted(selected)},
        "unavailable_dates": [],
    }
    current = start_day
    while current <= end_day:
        paths = partitions.get(current, [])
        period = current.isoformat()
        if not paths:
            summary["unavailable_days"] += 1
            summary["unavailable_dates"].append(period)
            for symbol in selected:
                store.record_archive_period(
                    SOURCE, symbol, period, "unavailable", 0, []
                )
            current += timedelta(days=1)
            continue

        rows: list[dict[str, Any]] = []
        for path in paths:
            rows.extend(client.fetch_partition(path))
        events = normalize_hyperliquid_rows(rows, selected)
        store.persist_events(events)
        counts: dict[str, int] = defaultdict(int)
        for event in events:
            counts[event.symbol] += 1
        for symbol in selected:
            count = counts[symbol]
            store.record_archive_period(SOURCE, symbol, period, "complete", count, paths)
            summary["events_by_symbol"][symbol] += count
        summary["published_days"] += 1
        summary["events_stored"] += len(events)
        current += timedelta(days=1)
    return summary
