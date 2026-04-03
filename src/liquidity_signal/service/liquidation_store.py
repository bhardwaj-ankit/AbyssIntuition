from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from liquidity_signal.models import LiquidationEventPoint


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _default_db_path() -> Path:
    target = _repo_root() / "runtime"
    target.mkdir(parents=True, exist_ok=True)
    return target / "liquidation_history.db"


class LiquidationStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or _default_db_path()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS liquidation_events (
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    liquidated_side TEXT NOT NULL,
                    execution_side TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    notional REAL NOT NULL,
                    event_ts INTEGER NOT NULL,
                    received_at INTEGER,
                    exchange_event_id TEXT,
                    raw_json TEXT NOT NULL,
                    PRIMARY KEY(source, exchange_event_id)
                );

                CREATE TABLE IF NOT EXISTS liquidation_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    generated_at INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    range_pct REAL NOT NULL,
                    resolution INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_time
                ON liquidation_snapshots(symbol, generated_at DESC);

                CREATE TABLE IF NOT EXISTS liquidation_tiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    generated_at INTEGER NOT NULL,
                    range_pct REAL NOT NULL,
                    resolution INTEGER NOT NULL,
                    tile_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tiles_symbol_res_time
                ON liquidation_tiles(symbol, resolution, generated_at DESC);
                """
            )

    def persist_events(self, events: list[LiquidationEventPoint]) -> None:
        if not events:
            return
        rows = []
        for event in events:
            rows.append(
                (
                    event.source,
                    event.symbol,
                    event.liquidated_side.value,
                    event.execution_side,
                    event.price,
                    event.quantity,
                    event.notional,
                    event.timestamp,
                    event.received_at,
                    event.exchange_event_id or f"{event.source}:{event.symbol}:{event.timestamp}:{event.price}:{event.quantity}",
                    json.dumps(event.model_dump()),
                )
            )
        with self._lock, self._conn:
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO liquidation_events (
                    source, symbol, liquidated_side, execution_side, price, quantity, notional,
                    event_ts, received_at, exchange_event_id, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def load_recent_events(self, symbol: str, limit: int = 200) -> list[LiquidationEventPoint]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT raw_json FROM liquidation_events
                WHERE symbol = ?
                ORDER BY event_ts DESC
                LIMIT ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        return [LiquidationEventPoint.model_validate(json.loads(row["raw_json"])) for row in rows]

    def count_events_since(self, symbol: str, since_ms: int) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS count FROM liquidation_events
                WHERE symbol = ? AND event_ts >= ?
                """,
                (symbol.upper(), since_ms),
            ).fetchone()
        return int(row["count"]) if row else 0

    def persist_snapshot(self, symbol: str, generated_at: int, source: str, range_pct: float, resolution: int, payload: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO liquidation_snapshots (symbol, generated_at, source, range_pct, resolution, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (symbol.upper(), generated_at, source, range_pct, resolution, json.dumps(payload)),
            )

    def replay_snapshots(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT payload_json FROM liquidation_snapshots
                WHERE symbol = ?
                ORDER BY generated_at DESC
                LIMIT ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def persist_tile(self, symbol: str, generated_at: int, range_pct: float, resolution: int, tile: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO liquidation_tiles (symbol, generated_at, range_pct, resolution, tile_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (symbol.upper(), generated_at, range_pct, resolution, json.dumps(tile)),
            )

    def get_latest_tile(self, symbol: str, resolution: int, range_pct: float) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT tile_json FROM liquidation_tiles
                WHERE symbol = ? AND resolution = ? AND range_pct = ?
                ORDER BY generated_at DESC
                LIMIT 1
                """,
                (symbol.upper(), resolution, range_pct),
            ).fetchone()
        return json.loads(row["tile_json"]) if row else None

    def calibration_profile(self, symbol: str, lookback_hours: int = 24) -> dict[str, float]:
        cutoff = int(time.time() * 1000) - lookback_hours * 60 * 60 * 1000
        with self._lock:
            snapshot_rows = self._conn.execute(
                """
                SELECT generated_at, payload_json FROM liquidation_snapshots
                WHERE symbol = ? AND generated_at >= ?
                ORDER BY generated_at ASC
                LIMIT 240
                """,
                (symbol.upper(), cutoff),
            ).fetchall()
            event_rows = self._conn.execute(
                """
                SELECT raw_json FROM liquidation_events
                WHERE symbol = ? AND event_ts >= ?
                ORDER BY event_ts ASC
                LIMIT 1000
                """,
                (symbol.upper(), cutoff),
            ).fetchall()

        snapshots = [(int(row["generated_at"]), json.loads(row["payload_json"])) for row in snapshot_rows]
        events = [json.loads(row["raw_json"]) for row in event_rows]
        if not snapshots or not events:
            return {
                "intensity_multiplier": 1.0,
                "long_bias_multiplier": 1.0,
                "short_bias_multiplier": 1.0,
                "historical_event_count": float(len(events)),
            }

        long_notional = 0.0
        short_notional = 0.0
        realized_to_predicted: list[float] = []

        for event in events:
            event_ts = int(event.get("timestamp", 0))
            if event_ts <= 0:
                continue
            chosen_snapshot = None
            for generated_at, payload in reversed(snapshots):
                if generated_at <= event_ts:
                    chosen_snapshot = payload
                    break
            if chosen_snapshot is None:
                chosen_snapshot = snapshots[-1][1]

            price_levels = chosen_snapshot.get("heatmap_price_levels", [])
            heatmap = chosen_snapshot.get("heatmap", [])
            if not price_levels or not heatmap:
                continue
            last_slice = heatmap[-1]
            side = event.get("liquidated_side")
            intensities = (
                last_slice.get("long_liquidation_intensity", [])
                if side == "LONG"
                else last_slice.get("short_liquidation_intensity", [])
            )
            if not intensities:
                continue
            nearest_idx = min(range(len(price_levels)), key=lambda idx: abs(price_levels[idx] - float(event["price"])))
            predicted = float(intensities[nearest_idx])
            if predicted > 0:
                realized_to_predicted.append(float(event["notional"]) / predicted)

            if side == "LONG":
                long_notional += float(event["notional"])
            else:
                short_notional += float(event["notional"])

        if realized_to_predicted:
            median_ratio = sorted(realized_to_predicted)[len(realized_to_predicted) // 2]
            intensity_multiplier = max(0.6, min(2.5, median_ratio / 8_000_000.0))
        else:
            intensity_multiplier = 1.0

        total_notional = long_notional + short_notional
        if total_notional <= 0.0:
            long_bias_multiplier = 1.0
            short_bias_multiplier = 1.0
        else:
            long_share = long_notional / total_notional
            short_share = short_notional / total_notional
            long_bias_multiplier = max(0.7, min(1.4, long_share / 0.5))
            short_bias_multiplier = max(0.7, min(1.4, short_share / 0.5))

        return {
            "intensity_multiplier": intensity_multiplier,
            "long_bias_multiplier": long_bias_multiplier,
            "short_bias_multiplier": short_bias_multiplier,
            "historical_event_count": float(len(events)),
        }
