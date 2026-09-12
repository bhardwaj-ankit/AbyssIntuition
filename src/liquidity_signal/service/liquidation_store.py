from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from liquidity_signal.models import (
    BotBacktestResponse,
    BotTrade,
    LiquidationEventPoint,
    LiquidationStreamHealth,
    PaperBotStatus,
    TrainingDatasetSummary,
    TrainingSnapshotLabel,
    TrainingSnapshotRecord,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _default_db_path() -> Path:
    configured = os.getenv("LIQUIDATION_STORE_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
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

                CREATE INDEX IF NOT EXISTS idx_liquidation_events_symbol_time
                ON liquidation_events(symbol, event_ts);

                CREATE TABLE IF NOT EXISTS liquidation_capture_heartbeats (
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    bucket_ts INTEGER NOT NULL,
                    connected INTEGER NOT NULL,
                    events_buffered INTEGER NOT NULL,
                    reconnects INTEGER NOT NULL,
                    last_error TEXT,
                    PRIMARY KEY(source, symbol, bucket_ts)
                );

                CREATE INDEX IF NOT EXISTS idx_liquidation_heartbeats_symbol_time
                ON liquidation_capture_heartbeats(symbol, bucket_ts);

                CREATE TABLE IF NOT EXISTS liquidation_capture_incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    symbol TEXT,
                    start_ts INTEGER NOT NULL,
                    end_ts INTEGER NOT NULL,
                    invalidated_rows INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS liquidation_archive_ingest (
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    period TEXT NOT NULL,
                    status TEXT NOT NULL,
                    rows_stored INTEGER NOT NULL,
                    source_paths_json TEXT NOT NULL,
                    PRIMARY KEY(source, symbol, period)
                );

                CREATE INDEX IF NOT EXISTS idx_liquidation_archive_symbol_period
                ON liquidation_archive_ingest(source, symbol, period);

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

                CREATE TABLE IF NOT EXISTS backtest_runs (
                    run_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    completed_at INTEGER NOT NULL,
                    signal_source TEXT NOT NULL,
                    initial_capital REAL NOT NULL,
                    final_capital REAL NOT NULL,
                    return_pct REAL NOT NULL,
                    total_trades INTEGER NOT NULL,
                    win_rate REAL NOT NULL,
                    fees_paid REAL NOT NULL,
                    funding_paid REAL NOT NULL,
                    max_drawdown_pct REAL NOT NULL,
                    config_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_backtest_runs_symbol_time
                ON backtest_runs(symbol, completed_at DESC);

                CREATE TABLE IF NOT EXISTS backtest_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    trade_index INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_time INTEGER NOT NULL,
                    exit_time INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    qty REAL NOT NULL,
                    duration_minutes REAL NOT NULL,
                    roi_pct REAL NOT NULL,
                    gross_pnl REAL NOT NULL,
                    fee_paid REAL NOT NULL,
                    funding_paid REAL NOT NULL,
                    net_pnl REAL NOT NULL,
                    entry_reason TEXT,
                    exit_reason TEXT,
                    entry_signal_direction TEXT,
                    entry_signal_confidence REAL,
                    entry_signal_quality TEXT,
                    signal_horizon TEXT,
                    signal_source TEXT,
                    raw_json TEXT NOT NULL,
                    UNIQUE(run_id, trade_index)
                );

                CREATE INDEX IF NOT EXISTS idx_backtest_trades_symbol_time
                ON backtest_trades(symbol, exit_time DESC);

                CREATE TABLE IF NOT EXISTS paper_bot_sessions (
                    session_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    started_at INTEGER,
                    scheduled_end_at INTEGER,
                    stopped_at INTEGER,
                    running INTEGER NOT NULL,
                    poll_interval_seconds INTEGER NOT NULL,
                    initial_capital REAL NOT NULL,
                    current_capital REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    fees_paid REAL NOT NULL,
                    total_trades INTEGER NOT NULL,
                    win_rate REAL NOT NULL,
                    signal_source TEXT NOT NULL,
                    summary_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_paper_bot_sessions_symbol_time
                ON paper_bot_sessions(symbol, COALESCE(stopped_at, started_at) DESC);

                CREATE TABLE IF NOT EXISTS paper_bot_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    trade_index INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_time INTEGER NOT NULL,
                    exit_time INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    qty REAL NOT NULL,
                    duration_minutes REAL NOT NULL,
                    roi_pct REAL NOT NULL,
                    gross_pnl REAL NOT NULL,
                    fee_paid REAL NOT NULL,
                    funding_paid REAL NOT NULL,
                    net_pnl REAL NOT NULL,
                    entry_reason TEXT,
                    exit_reason TEXT,
                    entry_signal_direction TEXT,
                    entry_signal_confidence REAL,
                    entry_signal_quality TEXT,
                    signal_horizon TEXT,
                    signal_source TEXT,
                    raw_json TEXT NOT NULL,
                    UNIQUE(session_id, trade_index)
                );

                CREATE INDEX IF NOT EXISTS idx_paper_bot_trades_symbol_time
                ON paper_bot_trades(symbol, exit_time DESC);

                CREATE TABLE IF NOT EXISTS training_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    event_ts INTEGER NOT NULL,
                    exchange_name TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    mark_price REAL NOT NULL,
                    signal_direction TEXT NOT NULL,
                    signal_confidence REAL NOT NULL,
                    signal_quality TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_training_snapshots_symbol_time
                ON training_snapshots(symbol, event_ts DESC);

                CREATE TABLE IF NOT EXISTS training_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    event_ts INTEGER NOT NULL,
                    session_id TEXT,
                    mode TEXT,
                    decision_action TEXT NOT NULL,
                    took_trade INTEGER NOT NULL,
                    ai_verdict TEXT,
                    ai_reason TEXT,
                    raw_json TEXT NOT NULL,
                    UNIQUE(snapshot_id)
                );

                CREATE INDEX IF NOT EXISTS idx_training_decisions_symbol_time
                ON training_decisions(symbol, event_ts DESC);

                CREATE TABLE IF NOT EXISTS training_labels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    event_ts INTEGER NOT NULL,
                    horizon_minutes INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    label_action TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    resolved_at INTEGER,
                    upper_barrier_price REAL NOT NULL,
                    lower_barrier_price REAL NOT NULL,
                    barrier_first_hit TEXT,
                    barrier_hit_ts INTEGER,
                    barrier_hit_price REAL,
                    barrier_ambiguous INTEGER NOT NULL DEFAULT 0,
                    horizon_close_price REAL,
                    horizon_return_bps REAL,
                    max_favorable_excursion_pct REAL,
                    max_adverse_excursion_pct REAL,
                    terminal_price REAL,
                    max_up_pct REAL NOT NULL DEFAULT 0.0,
                    max_down_pct REAL NOT NULL DEFAULT 0.0,
                    raw_json TEXT NOT NULL,
                    UNIQUE(snapshot_id, horizon_minutes)
                );

                CREATE INDEX IF NOT EXISTS idx_training_labels_symbol_expiry
                ON training_labels(symbol, status, expires_at ASC);
                """
            )
            incident_columns = {
                row[1]
                for row in self._conn.execute(
                    "PRAGMA table_info(liquidation_capture_incidents)"
                ).fetchall()
            }
            if "symbol" not in incident_columns:
                self._conn.execute(
                    "ALTER TABLE liquidation_capture_incidents ADD COLUMN symbol TEXT"
                )
            # Additive migration for stores created before label schema v2.
            existing = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(training_labels)").fetchall()
            }
            migrations = {
                "barrier_first_hit": "TEXT",
                "barrier_hit_ts": "INTEGER",
                "barrier_hit_price": "REAL",
                "barrier_ambiguous": "INTEGER NOT NULL DEFAULT 0",
                "horizon_close_price": "REAL",
                "horizon_return_bps": "REAL",
                "max_favorable_excursion_pct": "REAL",
                "max_adverse_excursion_pct": "REAL",
            }
            for name, definition in migrations.items():
                if name not in existing:
                    self._conn.execute(
                        f"ALTER TABLE training_labels ADD COLUMN {name} {definition}"
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

    def record_archive_period(
        self,
        source: str,
        symbol: str,
        period: str,
        status: str,
        rows_stored: int,
        source_paths: list[str],
    ) -> None:
        """Record archive provenance even when a published day has zero events."""
        if status not in {"complete", "unavailable", "failed"}:
            raise ValueError("Unsupported liquidation archive status.")
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO liquidation_archive_ingest
                   (source, symbol, period, status, rows_stored, source_paths_json)
                   VALUES(?,?,?,?,?,?)""",
                (
                    source,
                    symbol.upper(),
                    period,
                    status,
                    rows_stored,
                    json.dumps(source_paths),
                ),
            )

    def archive_coverage(
        self, source: str, symbol: str, start_day: str, end_day: str
    ) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*),
                          SUM(status='complete'),
                          SUM(status='unavailable'),
                          SUM(CASE WHEN status='complete' THEN rows_stored ELSE 0 END),
                          MIN(CASE WHEN status='complete' THEN period END),
                          MAX(CASE WHEN status='complete' THEN period END)
                   FROM liquidation_archive_ingest
                   WHERE source=? AND symbol=? AND period BETWEEN ? AND ?""",
                (source, symbol.upper(), start_day, end_day),
            ).fetchone()
        return {
            "audited_days": int(row[0] or 0),
            "published_days": int(row[1] or 0),
            "unavailable_days": int(row[2] or 0),
            "rows": int(row[3] or 0),
            "first_published_day": row[4],
            "last_published_day": row[5],
        }

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

    def persist_stream_health(
        self, rows: list[LiquidationStreamHealth], observed_at_ms: int
    ) -> None:
        """Persist five-minute collector heartbeats for auditable coverage."""
        bucket_ts = (observed_at_ms // 300_000) * 300_000
        values = [
            (
                row.source,
                row.symbol.upper(),
                bucket_ts,
                int(row.connected),
                row.events_buffered,
                row.reconnects,
                row.last_error,
            )
            for row in rows
        ]
        if not values:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT OR REPLACE INTO liquidation_capture_heartbeats
                   (source, symbol, bucket_ts, connected, events_buffered,
                    reconnects, last_error) VALUES(?,?,?,?,?,?,?)""",
                values,
            )

    def invalidate_capture_interval(
        self,
        source: str,
        start_ts: int,
        end_ts: int,
        reason: str,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """Fail closed on heartbeats later proven not to represent a live feed."""
        normalized_source = source.strip().lower()
        if not normalized_source:
            raise ValueError("source is required")
        if end_ts < start_ts:
            raise ValueError("end_ts must be on or after start_ts")
        if not reason.strip():
            raise ValueError("reason is required")
        normalized_symbol = symbol.strip().upper() if symbol else None
        with self._lock, self._conn:
            if normalized_symbol:
                cursor = self._conn.execute(
                    """UPDATE liquidation_capture_heartbeats
                       SET connected=0, last_error=?
                       WHERE source=? AND symbol=? AND bucket_ts BETWEEN ? AND ?""",
                    (
                        reason.strip(),
                        normalized_source,
                        normalized_symbol,
                        int(start_ts),
                        int(end_ts),
                    ),
                )
            else:
                cursor = self._conn.execute(
                    """UPDATE liquidation_capture_heartbeats
                       SET connected=0, last_error=?
                       WHERE source=? AND bucket_ts BETWEEN ? AND ?""",
                    (reason.strip(), normalized_source, int(start_ts), int(end_ts)),
                )
            invalidated = max(0, int(cursor.rowcount))
            self._conn.execute(
                """INSERT INTO liquidation_capture_incidents
                   (source, symbol, start_ts, end_ts, invalidated_rows, reason,
                    recorded_at) VALUES(?,?,?,?,?,?,?)""",
                (
                    normalized_source,
                    normalized_symbol,
                    int(start_ts),
                    int(end_ts),
                    invalidated,
                    reason.strip(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return {
            "source": normalized_source,
            "symbol": normalized_symbol,
            "start_ts": int(start_ts),
            "end_ts": int(end_ts),
            "invalidated_rows": invalidated,
            "reason": reason.strip(),
        }

    def liquidation_flow_features(
        self,
        symbol: str,
        event_ts: int,
        mark_price: float,
        windows_minutes: tuple[int, ...] = (5, 30, 60),
    ) -> dict[str, float]:
        """Aggregate only events observed at or before a model decision."""
        if not windows_minutes or mark_price <= 0:
            return {}
        max_window = max(windows_minutes)
        with self._lock:
            rows = self._conn.execute(
                """SELECT source, liquidated_side, price, notional, event_ts
                   FROM liquidation_events
                   WHERE symbol=? AND event_ts>? AND event_ts<=?
                   ORDER BY event_ts DESC""",
                (symbol.upper(), event_ts - max_window * 60_000, event_ts),
            ).fetchall()
            event_day = datetime.fromtimestamp(event_ts / 1000, timezone.utc).date().isoformat()
            archive_row = self._conn.execute(
                """SELECT 1 FROM liquidation_archive_ingest
                   WHERE source='hyperliquid_archive' AND symbol=? AND period=?
                         AND status='complete' LIMIT 1""",
                (symbol.upper(), event_day),
            ).fetchone()
        archive_available = bool(archive_row)

        features: dict[str, float] = {
            "xliq.hyperliquid_archive_available": float(archive_available)
        }
        for minutes in windows_minutes:
            cutoff = event_ts - minutes * 60_000
            selected = [row for row in rows if int(row["event_ts"]) > cutoff]
            with self._lock:
                connected_buckets = self._conn.execute(
                    """SELECT COUNT(*) FROM (
                           SELECT bucket_ts
                           FROM liquidation_capture_heartbeats
                           WHERE symbol=? AND bucket_ts>? AND bucket_ts<=? AND connected=1
                           GROUP BY bucket_ts
                           HAVING COUNT(DISTINCT source) >= 2
                       )""",
                    (symbol.upper(), cutoff, event_ts),
                ).fetchone()[0]
            expected_buckets = max(1, minutes // 5)
            capture_coverage = min(1.0, connected_buckets / expected_buckets)
            long_notional = sum(
                float(row["notional"])
                for row in selected
                if row["liquidated_side"] == "LONG"
            )
            short_notional = sum(
                float(row["notional"])
                for row in selected
                if row["liquidated_side"] == "SHORT"
            )
            total = long_notional + short_notional
            prefix = f"liqflow.{minutes}m"
            coverage_known = archive_available or capture_coverage >= 0.95 or bool(selected)
            features[f"{prefix}.capture_coverage"] = capture_coverage
            features[f"{prefix}.coverage_known"] = float(coverage_known)
            features[f"{prefix}.event_count_log"] = (
                math.log1p(len(selected)) if coverage_known else math.nan
            )
            features[f"{prefix}.notional_log"] = (
                math.log1p(total) if coverage_known else math.nan
            )
            features[f"{prefix}.notional_imbalance"] = (
                (long_notional - short_notional) / total
                if total > 0
                else (0.0 if coverage_known else math.nan)
            )
            features[f"{prefix}.source_count"] = float(
                len({str(row["source"]) for row in selected})
            )

            archive_selected = [
                row for row in selected if row["source"] == "hyperliquid_archive"
            ]
            archive_long = sum(
                float(row["notional"])
                for row in archive_selected
                if row["liquidated_side"] == "LONG"
            )
            archive_short = sum(
                float(row["notional"])
                for row in archive_selected
                if row["liquidated_side"] == "SHORT"
            )
            archive_total = archive_long + archive_short
            archive_prefix = f"xliq.hyperliquid.{minutes}m"
            features[f"{archive_prefix}.event_count_log"] = (
                math.log1p(len(archive_selected)) if archive_available else math.nan
            )
            features[f"{archive_prefix}.notional_log"] = (
                math.log1p(archive_total) if archive_available else math.nan
            )
            features[f"{archive_prefix}.notional_imbalance"] = (
                (archive_long - archive_short) / archive_total
                if archive_total > 0
                else (0.0 if archive_available else math.nan)
            )
            for side in ("LONG", "SHORT"):
                distances = [
                    ((float(row["price"]) - mark_price) / mark_price) * 10_000
                    for row in selected
                    if row["liquidated_side"] == side
                ]
                if distances:
                    features[f"{prefix}.nearest_{side.lower()}_distance_bps"] = min(
                        distances, key=abs
                    )
        return features

    def hyperliquid_archive_flow_features(
        self,
        symbol: str,
        event_ts: int,
        mark_price: float,
        windows_minutes: tuple[int, ...] = (5, 30, 60),
    ) -> dict[str, float]:
        """Return source-isolated archive features with explicit missingness."""
        features = self.liquidation_flow_features(
            symbol,
            event_ts,
            mark_price,
            windows_minutes=windows_minutes,
        )
        return {
            name: value
            for name, value in features.items()
            if name.startswith("xliq.hyperliquid")
        }

    def cryptohft_archive_flow_features(
        self,
        symbol: str,
        event_ts: int,
        mark_price: float,
        windows_minutes: tuple[int, ...] = (5, 30, 60),
    ) -> dict[str, float]:
        """Return source-isolated CryptoHFT CEX features with explicit coverage.

        Archive provenance, rather than live collector heartbeats, distinguishes a
        genuinely quiet period from a missing download. Events are still clipped at
        ``event_ts`` so a historical feature never sees a future liquidation.
        """
        if not windows_minutes or mark_price <= 0:
            return {}
        max_window = max(windows_minutes)
        earliest_ts = event_ts - max_window * 60_000

        def hour_periods(cutoff: int) -> list[str]:
            first_hour = ((cutoff + 1) // 3_600_000) * 3_600_000
            last_hour = (event_ts // 3_600_000) * 3_600_000
            periods = []
            current = first_hour
            while current <= last_hour:
                periods.append(
                    datetime.fromtimestamp(current / 1000, timezone.utc).strftime(
                        "%Y-%m-%dT%H:00:00Z"
                    )
                )
                current += 3_600_000
            return periods

        all_periods = hour_periods(earliest_ts)
        placeholders = ",".join("?" for _ in all_periods)
        with self._lock:
            rows = self._conn.execute(
                """SELECT source, liquidated_side, price, notional, event_ts
                   FROM liquidation_events
                   WHERE symbol=? AND source IN ('binance','bybit')
                         AND event_ts>? AND event_ts<=?
                   ORDER BY event_ts DESC""",
                (symbol.upper(), earliest_ts, event_ts),
            ).fetchall()
            provenance = self._conn.execute(
                f"""SELECT source, period FROM liquidation_archive_ingest
                    WHERE symbol=? AND status='complete'
                          AND source IN (
                              'cryptohft_recovery_binance',
                              'cryptohft_recovery_bybit'
                          ) AND period IN ({placeholders})""",
                (symbol.upper(), *all_periods),
            ).fetchall()
        available = {(str(row["source"]), str(row["period"])) for row in provenance}
        expected_sources = (
            "cryptohft_recovery_binance",
            "cryptohft_recovery_bybit",
        )
        features: dict[str, float] = {}
        for minutes in windows_minutes:
            cutoff = event_ts - minutes * 60_000
            required_periods = hour_periods(cutoff)
            complete = bool(required_periods) and all(
                (source, period) in available
                for source in expected_sources
                for period in required_periods
            )
            selected = [row for row in rows if int(row["event_ts"]) > cutoff]
            long_notional = sum(
                float(row["notional"])
                for row in selected
                if row["liquidated_side"] == "LONG"
            )
            short_notional = sum(
                float(row["notional"])
                for row in selected
                if row["liquidated_side"] == "SHORT"
            )
            total = long_notional + short_notional
            prefix = f"xliq.cryptohft.{minutes}m"
            features[f"{prefix}.archive_complete"] = float(complete)
            features[f"{prefix}.event_count_log"] = (
                math.log1p(len(selected)) if complete else math.nan
            )
            features[f"{prefix}.notional_log"] = (
                math.log1p(total) if complete else math.nan
            )
            features[f"{prefix}.notional_imbalance"] = (
                (long_notional - short_notional) / total
                if total > 0
                else (0.0 if complete else math.nan)
            )
            features[f"{prefix}.source_count"] = (
                float(len({str(row["source"]) for row in selected}))
                if complete
                else math.nan
            )
            for side in ("LONG", "SHORT"):
                distances = [
                    ((float(row["price"]) - mark_price) / mark_price) * 10_000
                    for row in selected
                    if row["liquidated_side"] == side
                ]
                features[f"{prefix}.nearest_{side.lower()}_distance_bps"] = (
                    min(distances, key=abs) if complete and distances else math.nan
                )
        return features

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

    def nearest_snapshot(self, symbol: str, target_ms: int, tolerance_ms: int = 10 * 60 * 1000) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT payload_json, ABS(generated_at - ?) AS distance_ms
                FROM liquidation_snapshots
                WHERE symbol = ? AND ABS(generated_at - ?) <= ?
                ORDER BY distance_ms ASC
                LIMIT 1
                """,
                (int(target_ms), symbol.upper(), int(target_ms), int(tolerance_ms)),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

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

    def persist_backtest_run(
        self,
        run_id: str,
        symbol: str,
        interval: str,
        started_at: int,
        completed_at: int,
        signal_source: str,
        config: dict[str, Any],
        response: BotBacktestResponse,
    ) -> None:
        payload = response.model_dump(mode="json")
        trade_rows = []
        for trade_index, trade in enumerate(response.trades, start=1):
            trade_rows.append(
                (
                    run_id,
                    trade_index,
                    symbol.upper(),
                    interval,
                    trade.side.value,
                    trade.entry_time,
                    trade.exit_time,
                    trade.entry_price,
                    trade.exit_price,
                    trade.qty,
                    trade.duration_minutes,
                    trade.roi_pct,
                    trade.gross_pnl,
                    trade.fee_paid,
                    trade.funding_paid,
                    trade.net_pnl,
                    trade.entry_reason,
                    trade.exit_reason or trade.reason,
                    trade.entry_signal_direction.value,
                    trade.entry_signal_confidence,
                    trade.entry_signal_quality,
                    trade.signal_horizon,
                    trade.signal_source,
                    json.dumps(trade.model_dump(mode="json")),
                )
            )

        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO backtest_runs (
                    run_id, symbol, interval, started_at, completed_at, signal_source,
                    initial_capital, final_capital, return_pct, total_trades, win_rate,
                    fees_paid, funding_paid, max_drawdown_pct, config_json, summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    symbol.upper(),
                    interval,
                    started_at,
                    completed_at,
                    signal_source,
                    response.initial_capital,
                    response.final_capital,
                    response.return_pct,
                    response.total_trades,
                    response.win_rate,
                    response.fees_paid,
                    response.funding_paid,
                    response.max_drawdown_pct,
                    json.dumps(config),
                    json.dumps(payload),
                ),
            )
            if trade_rows:
                self._conn.executemany(
                    """
                    INSERT OR REPLACE INTO backtest_trades (
                        run_id, trade_index, symbol, interval, side, entry_time, exit_time,
                        entry_price, exit_price, qty, duration_minutes, roi_pct, gross_pnl,
                        fee_paid, funding_paid, net_pnl, entry_reason, exit_reason,
                        entry_signal_direction, entry_signal_confidence, entry_signal_quality,
                        signal_horizon, signal_source, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    trade_rows,
                )

    def load_recent_backtest_trades(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT raw_json FROM backtest_trades
                WHERE symbol = ?
                ORDER BY exit_time DESC, id DESC
                LIMIT ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        return [json.loads(row["raw_json"]) for row in rows]

    def load_recent_backtest_runs(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT summary_json FROM backtest_runs
                WHERE symbol = ?
                ORDER BY completed_at DESC
                LIMIT ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        return [json.loads(row["summary_json"]) for row in rows]

    def persist_paper_bot_status(self, status: PaperBotStatus) -> None:
        if not status.session_id:
            return
        payload = status.model_dump(mode="json")
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO paper_bot_sessions (
                    session_id, symbol, started_at, scheduled_end_at, stopped_at, running,
                    poll_interval_seconds, initial_capital, current_capital, realized_pnl,
                    unrealized_pnl, fees_paid, total_trades, win_rate, signal_source, summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    status.session_id,
                    status.symbol.upper(),
                    status.started_at,
                    status.scheduled_end_at,
                    status.stopped_at,
                    1 if status.running else 0,
                    status.poll_interval_seconds,
                    status.initial_capital,
                    status.current_capital,
                    status.realized_pnl,
                    status.unrealized_pnl,
                    status.fees_paid,
                    status.total_trades,
                    status.win_rate,
                    status.signal_source,
                    json.dumps(payload),
                ),
            )

    def persist_paper_bot_trade(self, session_id: str, symbol: str, trade_index: int, trade: BotTrade) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO paper_bot_trades (
                    session_id, trade_index, symbol, side, entry_time, exit_time,
                    entry_price, exit_price, qty, duration_minutes, roi_pct, gross_pnl,
                    fee_paid, funding_paid, net_pnl, entry_reason, exit_reason,
                    entry_signal_direction, entry_signal_confidence, entry_signal_quality,
                    signal_horizon, signal_source, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    trade_index,
                    symbol.upper(),
                    trade.side.value,
                    trade.entry_time,
                    trade.exit_time,
                    trade.entry_price,
                    trade.exit_price,
                    trade.qty,
                    trade.duration_minutes,
                    trade.roi_pct,
                    trade.gross_pnl,
                    trade.fee_paid,
                    trade.funding_paid,
                    trade.net_pnl,
                    trade.entry_reason,
                    trade.exit_reason or trade.reason,
                    trade.entry_signal_direction.value,
                    trade.entry_signal_confidence,
                    trade.entry_signal_quality,
                    trade.signal_horizon,
                    trade.signal_source,
                    json.dumps(trade.model_dump(mode="json")),
                ),
            )

    def load_recent_paper_bot_trades(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT raw_json FROM paper_bot_trades
                WHERE symbol = ?
                ORDER BY exit_time DESC, id DESC
                LIMIT ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        return [json.loads(row["raw_json"]) for row in rows]

    def load_latest_paper_bot_status(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT summary_json FROM paper_bot_sessions
                WHERE symbol = ?
                ORDER BY COALESCE(stopped_at, started_at) DESC
                LIMIT 1
                """,
                (symbol.upper(),),
            ).fetchone()
        return json.loads(row["summary_json"]) if row else None

    def persist_training_snapshot(self, snapshot: TrainingSnapshotRecord, decision_payload: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO training_snapshots (
                    snapshot_id, symbol, event_ts, exchange_name, environment, mark_price,
                    signal_direction, signal_confidence, signal_quality, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.symbol.upper(),
                    snapshot.event_ts,
                    snapshot.exchange,
                    snapshot.environment,
                    snapshot.mark_price,
                    snapshot.signal_direction.value,
                    snapshot.signal_confidence,
                    snapshot.signal_quality,
                    json.dumps(snapshot.model_dump(mode="json")),
                ),
            )
            self._conn.execute(
                """
                INSERT OR REPLACE INTO training_decisions (
                    snapshot_id, symbol, event_ts, session_id, mode, decision_action, took_trade,
                    ai_verdict, ai_reason, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.symbol.upper(),
                    snapshot.event_ts,
                    decision_payload.get("session_id"),
                    decision_payload.get("mode"),
                    snapshot.decision_action,
                    1 if snapshot.took_trade else 0,
                    decision_payload.get("ai_verdict"),
                    decision_payload.get("ai_reason"),
                    json.dumps(decision_payload),
                ),
            )

    def persist_training_label(self, label: TrainingSnapshotLabel) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO training_labels (
                    snapshot_id, symbol, event_ts, horizon_minutes, status, label_action, expires_at,
                    resolved_at, upper_barrier_price, lower_barrier_price, barrier_first_hit,
                    barrier_hit_ts, barrier_hit_price, barrier_ambiguous, horizon_close_price,
                    horizon_return_bps, max_favorable_excursion_pct, max_adverse_excursion_pct,
                    terminal_price, max_up_pct, max_down_pct, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    label.snapshot_id,
                    label.symbol.upper(),
                    label.event_ts,
                    label.horizon_minutes,
                    label.status,
                    label.label_action.value,
                    label.expires_at,
                    label.resolved_at,
                    label.upper_barrier_price,
                    label.lower_barrier_price,
                    label.barrier_first_hit.value if label.barrier_first_hit else None,
                    label.barrier_hit_ts,
                    label.barrier_hit_price,
                    1 if label.barrier_ambiguous else 0,
                    label.horizon_close_price,
                    label.horizon_return_bps,
                    label.max_favorable_excursion_pct,
                    label.max_adverse_excursion_pct,
                    label.terminal_price,
                    label.max_up_pct,
                    label.max_down_pct,
                    json.dumps(label.model_dump(mode="json")),
                ),
            )

    def load_pending_training_labels(self, symbol: str, now_ms: int, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT raw_json FROM training_labels
                WHERE symbol = ? AND status = 'PENDING' AND expires_at <= ?
                ORDER BY expires_at ASC
                LIMIT ?
                """,
                (symbol.upper(), now_ms, limit),
            ).fetchall()
        return [json.loads(row["raw_json"]) for row in rows]

    def load_recent_training_snapshots(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT raw_json FROM training_snapshots
                WHERE symbol = ?
                ORDER BY event_ts DESC
                LIMIT ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        return [json.loads(row["raw_json"]) for row in rows]

    def load_recent_training_labels(self, symbol: str, limit: int = 100, resolved_only: bool = False) -> list[dict[str, Any]]:
        query = """
            SELECT raw_json FROM training_labels
            WHERE symbol = ?
        """
        params: list[Any] = [symbol.upper()]
        if resolved_only:
            query += " AND status = 'RESOLVED'"
        query += " ORDER BY event_ts DESC, horizon_minutes ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [json.loads(row["raw_json"]) for row in rows]

    def load_training_labels_for_snapshot(self, snapshot_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT raw_json FROM training_labels
                WHERE snapshot_id = ?
                ORDER BY horizon_minutes ASC
                """,
                (snapshot_id,),
            ).fetchall()
        return [json.loads(row["raw_json"]) for row in rows]

    def load_lora_training_rows(
        self,
        symbol: str,
        *,
        horizon_minutes: int,
        limit: int = 1000,
        decision_action: str | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                ts.raw_json AS snapshot_json,
                td.raw_json AS decision_json,
                tl.raw_json AS label_json
            FROM training_labels tl
            INNER JOIN training_snapshots ts
                ON ts.snapshot_id = tl.snapshot_id
            INNER JOIN training_decisions td
                ON td.snapshot_id = tl.snapshot_id
            WHERE tl.symbol = ?
              AND tl.status = 'RESOLVED'
              AND tl.horizon_minutes = ?
        """
        params: list[Any] = [symbol.upper(), horizon_minutes]
        if decision_action:
            query += " AND td.decision_action = ?"
            params.append(decision_action)
        query += " ORDER BY tl.event_ts DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()

        payload: list[dict[str, Any]] = []
        for row in rows:
            payload.append(
                {
                    "snapshot": json.loads(row["snapshot_json"]),
                    "decision": json.loads(row["decision_json"]),
                    "label": json.loads(row["label_json"]),
                }
            )
        return payload

    def training_dataset_summary(self, symbol: str) -> TrainingDatasetSummary:
        with self._lock:
            snapshot_count = self._conn.execute(
                "SELECT COUNT(*) AS count FROM training_snapshots WHERE symbol = ?",
                (symbol.upper(),),
            ).fetchone()
            decision_count = self._conn.execute(
                "SELECT COUNT(*) AS count FROM training_decisions WHERE symbol = ?",
                (symbol.upper(),),
            ).fetchone()
            label_counts = self._conn.execute(
                """
                SELECT
                    COUNT(*) AS total_labels,
                    SUM(CASE WHEN status = 'RESOLVED' THEN 1 ELSE 0 END) AS resolved_labels,
                    SUM(CASE WHEN status = 'PENDING' THEN 1 ELSE 0 END) AS pending_labels,
                    SUM(CASE WHEN label_action = 'LONG' THEN 1 ELSE 0 END) AS long_labels,
                    SUM(CASE WHEN label_action = 'SHORT' THEN 1 ELSE 0 END) AS short_labels,
                    SUM(CASE WHEN label_action = 'FLAT' THEN 1 ELSE 0 END) AS flat_labels
                FROM training_labels
                WHERE symbol = ?
                """,
                (symbol.upper(),),
            ).fetchone()

        return TrainingDatasetSummary(
            symbol=symbol.upper(),
            total_snapshots=int(snapshot_count["count"] if snapshot_count else 0),
            total_decisions=int(decision_count["count"] if decision_count else 0),
            total_labels=int(label_counts["total_labels"] if label_counts and label_counts["total_labels"] is not None else 0),
            resolved_labels=int(label_counts["resolved_labels"] if label_counts and label_counts["resolved_labels"] is not None else 0),
            pending_labels=int(label_counts["pending_labels"] if label_counts and label_counts["pending_labels"] is not None else 0),
            long_labels=int(label_counts["long_labels"] if label_counts and label_counts["long_labels"] is not None else 0),
            short_labels=int(label_counts["short_labels"] if label_counts and label_counts["short_labels"] is not None else 0),
            flat_labels=int(label_counts["flat_labels"] if label_counts and label_counts["flat_labels"] is not None else 0),
        )
