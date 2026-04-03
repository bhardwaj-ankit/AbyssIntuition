from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import defaultdict, deque
from typing import Any

import websockets

from liquidity_signal.models import Direction, LiquidationEventPoint, LiquidationStreamHealth
from liquidity_signal.service.liquidation_store import LiquidationStore


def _now_ms() -> int:
    return int(time.time() * 1000)


def _opposite_execution_side(liquidated_side: Direction) -> str:
    return "SELL" if liquidated_side == Direction.LONG else "BUY"


def _normalize_binance_force_order(payload: dict[str, Any]) -> LiquidationEventPoint | None:
    order = payload.get("o")
    if not isinstance(order, dict):
        return None

    execution_side = str(order.get("S", "")).upper()
    if execution_side == "SELL":
        liquidated_side = Direction.LONG
    elif execution_side == "BUY":
        liquidated_side = Direction.SHORT
    else:
        return None

    symbol = str(order.get("s", "")).upper()
    ts = int(order.get("T", payload.get("E", 0)))
    qty = float(order.get("z", order.get("q", 0.0)))
    price = float(order.get("ap", order.get("p", 0.0)))
    if not symbol or ts <= 0 or qty <= 0.0 or price <= 0.0:
        return None

    return LiquidationEventPoint(
        source="binance",
        symbol=symbol,
        liquidated_side=liquidated_side,
        execution_side=execution_side,
        price=price,
        quantity=qty,
        notional=price * qty,
        timestamp=ts,
        received_at=_now_ms(),
        exchange_event_id=f"binance:{symbol}:{ts}:{execution_side}:{qty}:{price}",
    )


def _normalize_bybit_liquidation(payload: dict[str, Any]) -> list[LiquidationEventPoint]:
    rows = payload.get("data", [])
    if not isinstance(rows, list):
        return []

    events: list[LiquidationEventPoint] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        side_raw = str(row.get("S", "")).upper()
        if side_raw == "BUY":
            liquidated_side = Direction.LONG
        elif side_raw == "SELL":
            liquidated_side = Direction.SHORT
        else:
            continue

        symbol = str(row.get("s", "")).upper()
        ts = int(row.get("T", 0))
        qty = float(row.get("v", 0.0))
        price = float(row.get("p", 0.0))
        if not symbol or ts <= 0 or qty <= 0.0 or price <= 0.0:
            continue

        events.append(
            LiquidationEventPoint(
                source="bybit",
                symbol=symbol,
                liquidated_side=liquidated_side,
                execution_side=_opposite_execution_side(liquidated_side),
                price=price,
                quantity=qty,
                notional=price * qty,
                timestamp=ts,
                received_at=_now_ms(),
                exchange_event_id=f"bybit:{symbol}:{ts}:{side_raw}:{qty}:{price}",
            )
        )
    return events


class LiquidationRuntime:
    """In-memory runtime for normalized liquidation events and stream health."""

    def __init__(self, max_events_per_symbol: int = 500, store: LiquidationStore | None = None) -> None:
        self._max_events_per_symbol = max_events_per_symbol
        self._lock = threading.Lock()
        self._store = store
        self._events: dict[tuple[str, str], deque[LiquidationEventPoint]] = defaultdict(
            lambda: deque(maxlen=self._max_events_per_symbol)
        )
        self._health: dict[tuple[str, str], LiquidationStreamHealth] = {}
        self._threads: dict[tuple[str, str], threading.Thread] = {}
        self._bootstrapped: set[str] = set()
        self._stop = threading.Event()

    def close(self) -> None:
        self._stop.set()
        for thread in list(self._threads.values()):
            thread.join(timeout=1.0)

    def ensure_symbol(self, symbol: str) -> None:
        normalized = symbol.upper()
        if self._store is not None and normalized not in self._bootstrapped:
            with self._lock:
                if normalized not in self._bootstrapped:
                    for event in reversed(self._store.load_recent_events(normalized, limit=self._max_events_per_symbol)):
                        self._events[(event.source, normalized)].append(event)
                    self._bootstrapped.add(normalized)
        for source, target in (
            ("binance", self._run_binance_stream),
            ("bybit", self._run_bybit_stream),
        ):
            key = (source, normalized)
            with self._lock:
                if key in self._threads and self._threads[key].is_alive():
                    continue
                self._health.setdefault(
                    key,
                    LiquidationStreamHealth(
                        source=source,
                        symbol=normalized,
                        connected=False,
                        events_buffered=0,
                        reconnects=0,
                    ),
                )
                thread = threading.Thread(target=target, args=(normalized,), daemon=True, name=f"{source}-{normalized}")
                self._threads[key] = thread
                thread.start()

    def get_recent_events(self, symbol: str, limit: int = 50) -> list[LiquidationEventPoint]:
        normalized = symbol.upper()
        with self._lock:
            rows: list[LiquidationEventPoint] = []
            for source in ("binance", "bybit"):
                rows.extend(list(self._events.get((source, normalized), [])))
        rows.sort(key=lambda row: row.timestamp, reverse=True)
        return rows[:limit]

    def get_stream_health(self, symbol: str) -> list[LiquidationStreamHealth]:
        normalized = symbol.upper()
        with self._lock:
            rows = [
                self._health[(source, normalized)]
                for source in ("binance", "bybit")
                if (source, normalized) in self._health
            ]
        return rows

    def _append_event(self, event: LiquidationEventPoint) -> None:
        key = (event.source, event.symbol)
        with self._lock:
            bucket = self._events[key]
            if event.exchange_event_id and any(existing.exchange_event_id == event.exchange_event_id for existing in bucket):
                return
            bucket.append(event)
            health = self._health.get(key)
            if health is not None:
                health.last_message_ts = event.received_at
                health.last_event_ts = event.timestamp
                health.events_buffered = len(bucket)
                health.last_error = None
        if self._store is not None:
            self._store.persist_events([event])

    def _set_health(
        self,
        source: str,
        symbol: str,
        *,
        connected: bool | None = None,
        last_message_ts: int | None = None,
        last_error: str | None = None,
        reconnect_increment: bool = False,
    ) -> None:
        key = (source, symbol)
        with self._lock:
            health = self._health.setdefault(
                key,
                LiquidationStreamHealth(source=source, symbol=symbol, connected=False, events_buffered=0, reconnects=0),
            )
            if connected is not None:
                health.connected = connected
            if last_message_ts is not None:
                health.last_message_ts = last_message_ts
            if last_error is not None:
                health.last_error = last_error
            if reconnect_increment:
                health.reconnects += 1

    def _run_binance_stream(self, symbol: str) -> None:
        asyncio.run(self._binance_stream_loop(symbol))

    async def _binance_stream_loop(self, symbol: str) -> None:
        uri = f"wss://fstream.binance.com/ws/{symbol.lower()}@forceOrder"
        while not self._stop.is_set():
            try:
                async with websockets.connect(uri, ping_interval=20, ping_timeout=20) as ws:
                    self._set_health("binance", symbol, connected=True, last_error=None)
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        except asyncio.TimeoutError:
                            continue
                        payload = json.loads(raw)
                        event = _normalize_binance_force_order(payload)
                        self._set_health("binance", symbol, last_message_ts=_now_ms())
                        if event is not None:
                            self._append_event(event)
            except Exception as exc:
                self._set_health(
                    "binance",
                    symbol,
                    connected=False,
                    last_error=str(exc),
                    reconnect_increment=True,
                )
                await asyncio.sleep(2.0)

    def _run_bybit_stream(self, symbol: str) -> None:
        asyncio.run(self._bybit_stream_loop(symbol))

    async def _bybit_stream_loop(self, symbol: str) -> None:
        uri = "wss://stream.bybit.com/v5/public/linear"
        topic = f"allLiquidation.{symbol}"
        while not self._stop.is_set():
            try:
                async with websockets.connect(uri, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
                    self._set_health("bybit", symbol, connected=True, last_error=None)
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        except asyncio.TimeoutError:
                            continue
                        payload = json.loads(raw)
                        self._set_health("bybit", symbol, last_message_ts=_now_ms())
                        for event in _normalize_bybit_liquidation(payload):
                            self._append_event(event)
            except Exception as exc:
                self._set_health(
                    "bybit",
                    symbol,
                    connected=False,
                    last_error=str(exc),
                    reconnect_increment=True,
                )
                await asyncio.sleep(2.0)
