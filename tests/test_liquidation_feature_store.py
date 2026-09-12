from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from liquidity_signal.models import (
    Direction,
    LiquidationEventPoint,
    LiquidationStreamHealth,
)
from liquidity_signal.service.liquidation_store import LiquidationStore


def _event(
    *, timestamp: int, side: Direction, notional: float, price: float, source: str
) -> LiquidationEventPoint:
    return LiquidationEventPoint(
        source=source,
        symbol="BTCUSDT",
        liquidated_side=side,
        execution_side="SELL" if side == Direction.LONG else "BUY",
        price=price,
        quantity=notional / price,
        notional=notional,
        timestamp=timestamp,
        received_at=timestamp + 1,
        exchange_event_id=f"{source}:{timestamp}:{side.value}",
    )


def test_liquidation_flow_uses_only_pre_decision_events(tmp_path) -> None:
    store = LiquidationStore(tmp_path / "liquidations.db")
    decision_ts = 1_800_000_000_000
    store.persist_events([
        _event(
            timestamp=decision_ts - 60_000,
            side=Direction.LONG,
            notional=300.0,
            price=99.0,
            source="binance",
        ),
        _event(
            timestamp=decision_ts - 10 * 60_000,
            side=Direction.SHORT,
            notional=100.0,
            price=101.0,
            source="bybit",
        ),
        _event(
            timestamp=decision_ts + 1,
            side=Direction.SHORT,
            notional=10_000.0,
            price=102.0,
            source="bybit",
        ),
    ])

    features = store.liquidation_flow_features("BTCUSDT", decision_ts, 100.0)

    assert features["liqflow.5m.event_count_log"] == math.log1p(1)
    assert features["liqflow.5m.notional_imbalance"] == 1.0
    assert features["liqflow.30m.event_count_log"] == math.log1p(2)
    assert features["liqflow.30m.notional_imbalance"] == 0.5
    assert features["liqflow.30m.nearest_long_distance_bps"] == pytest.approx(-100.0)
    assert features["liqflow.30m.nearest_short_distance_bps"] == pytest.approx(100.0)
    store.close()


def test_stream_health_is_bucketed_for_coverage_audits(tmp_path) -> None:
    store = LiquidationStore(tmp_path / "liquidations.db")
    observed_at = 1_800_000_123_456
    health = [
        LiquidationStreamHealth(
            source=source,
            symbol="BTCUSDT",
            connected=True,
            events_buffered=0,
            reconnects=0,
        )
        for source in ("binance", "bybit")
    ]
    store.persist_stream_health(health, observed_at)

    rows = store._conn.execute(  # noqa: SLF001 - verify persisted audit contract
        "SELECT source, bucket_ts, connected FROM liquidation_capture_heartbeats"
    ).fetchall()
    assert len(rows) == 2
    assert {row["source"] for row in rows} == {"binance", "bybit"}
    assert {row["bucket_ts"] for row in rows} == {
        (observed_at // 300_000) * 300_000
    }
    assert all(row["connected"] == 1 for row in rows)
    store.close()


def test_capture_interval_invalidation_is_audited(tmp_path) -> None:
    store = LiquidationStore(tmp_path / "liquidations.db")
    observed_at = 1_800_000_123_456
    store.persist_stream_health([
        LiquidationStreamHealth(
            source="binance",
            symbol="BTCUSDT",
            connected=True,
            events_buffered=0,
            reconnects=0,
        )
    ], observed_at)

    result = store.invalidate_capture_interval(
        "binance", observed_at - 300_000, observed_at, "retired route"
    )

    assert result["invalidated_rows"] == 1
    heartbeat = store._conn.execute(  # noqa: SLF001 - verify audit persistence
        "SELECT connected, last_error FROM liquidation_capture_heartbeats"
    ).fetchone()
    assert heartbeat["connected"] == 0
    assert heartbeat["last_error"] == "retired route"
    incident = store._conn.execute(  # noqa: SLF001 - verify incident persistence
        "SELECT source, invalidated_rows, reason FROM liquidation_capture_incidents"
    ).fetchone()
    assert tuple(incident) == ("binance", 1, "retired route")
    store.close()


def test_capture_interval_invalidation_can_target_one_symbol(tmp_path) -> None:
    store = LiquidationStore(tmp_path / "liquidations.db")
    observed_at = 1_800_000_123_456
    store.persist_stream_health(
        [
            LiquidationStreamHealth(
                source="bybit",
                symbol=symbol,
                connected=True,
                events_buffered=0,
                reconnects=0,
            )
            for symbol in ("BTCUSDT", "PEPEUSDT")
        ],
        observed_at,
    )

    result = store.invalidate_capture_interval(
        "bybit",
        observed_at - 300_000,
        observed_at,
        "wrong remote contract",
        symbol="PEPEUSDT",
    )

    assert result["symbol"] == "PEPEUSDT"
    assert result["invalidated_rows"] == 1
    rows = store._conn.execute(  # noqa: SLF001 - verify targeted audit contract
        "SELECT symbol, connected FROM liquidation_capture_heartbeats ORDER BY symbol"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("BTCUSDT", 1), ("PEPEUSDT", 0)]
    incident = store._conn.execute(  # noqa: SLF001 - verify incident symbol
        "SELECT source, symbol, reason FROM liquidation_capture_incidents"
    ).fetchone()
    assert tuple(incident) == ("bybit", "PEPEUSDT", "wrong remote contract")
    store.close()


def test_missing_archive_is_not_encoded_as_zero_liquidations(tmp_path) -> None:
    store = LiquidationStore(tmp_path / "liquidations.db")
    decision_ts = 1_800_000_000_000

    missing = store.liquidation_flow_features("BTCUSDT", decision_ts, 100.0)

    assert math.isnan(missing["liqflow.5m.event_count_log"])
    assert math.isnan(missing["xliq.hyperliquid.5m.notional_log"])
    day = datetime.fromtimestamp(decision_ts / 1000, timezone.utc).date().isoformat()
    store.record_archive_period(
        "hyperliquid_archive", "BTCUSDT", day, "complete", 0, ["day.parquet"]
    )

    observed_zero = store.liquidation_flow_features("BTCUSDT", decision_ts, 100.0)

    assert observed_zero["liqflow.5m.event_count_log"] == 0.0
    assert observed_zero["xliq.hyperliquid.5m.notional_log"] == 0.0
    assert observed_zero["xliq.hyperliquid_archive_available"] == 1.0
    store.close()


def test_hyperliquid_archive_features_never_mix_cex_events(tmp_path) -> None:
    store = LiquidationStore(tmp_path / "liquidations.db")
    decision_ts = 1_800_000_000_000
    day = datetime.fromtimestamp(decision_ts / 1000, timezone.utc).date().isoformat()
    store.record_archive_period(
        "hyperliquid_archive", "BTCUSDT", day, "complete", 1, ["day.parquet"]
    )
    store.persist_events([
        _event(
            timestamp=decision_ts - 60_000,
            side=Direction.LONG,
            notional=100.0,
            price=99.0,
            source="hyperliquid_archive",
        ),
        _event(
            timestamp=decision_ts - 30_000,
            side=Direction.SHORT,
            notional=10_000.0,
            price=101.0,
            source="binance",
        ),
    ])

    features = store.hyperliquid_archive_flow_features(
        "BTCUSDT", decision_ts, 100.0
    )

    assert features["xliq.hyperliquid.5m.event_count_log"] == math.log1p(1)
    assert features["xliq.hyperliquid.5m.notional_log"] == math.log1p(100.0)
    assert features["xliq.hyperliquid.5m.notional_imbalance"] == 1.0
    assert all(name.startswith("xliq.hyperliquid") for name in features)
    store.close()


def test_cryptohft_features_require_both_venue_partitions_and_clip_future(tmp_path) -> None:
    store = LiquidationStore(tmp_path / "liquidations.db")
    decision_ts = 1_800_003_660_000
    hours = {
        datetime.fromtimestamp(ts / 1000, timezone.utc).strftime(
            "%Y-%m-%dT%H:00:00Z"
        )
        for ts in (decision_ts, decision_ts - 5 * 60_000)
    }
    for venue in ("binance", "bybit"):
        for hour in hours:
            store.record_archive_period(
                f"cryptohft_recovery_{venue}",
                "BTCUSDT",
                hour,
                "complete",
                0,
                [f"{venue}.parquet"],
            )
    store.persist_events([
        _event(
            timestamp=decision_ts - 30_000,
            side=Direction.LONG,
            notional=100.0,
            price=99.0,
            source="binance",
        ),
        _event(
            timestamp=decision_ts + 30_000,
            side=Direction.SHORT,
            notional=10_000.0,
            price=101.0,
            source="bybit",
        ),
        _event(
            timestamp=decision_ts - 20_000,
            side=Direction.SHORT,
            notional=20_000.0,
            price=101.0,
            source="hyperliquid_archive",
        ),
    ])

    features = store.cryptohft_archive_flow_features(
        "BTCUSDT", decision_ts, 100.0, windows_minutes=(5,)
    )

    assert features["xliq.cryptohft.5m.archive_complete"] == 1.0
    assert features["xliq.cryptohft.5m.event_count_log"] == math.log1p(1)
    assert features["xliq.cryptohft.5m.notional_log"] == math.log1p(100.0)
    assert features["xliq.cryptohft.5m.notional_imbalance"] == 1.0
    assert features["xliq.cryptohft.5m.source_count"] == 1.0
    assert all(name.startswith("xliq.cryptohft") for name in features)

    store.record_archive_period(
        "cryptohft_recovery_bybit",
        "BTCUSDT",
        max(hours),
        "failed",
        0,
        [],
    )
    missing = store.cryptohft_archive_flow_features(
        "BTCUSDT", decision_ts, 100.0, windows_minutes=(5,)
    )
    assert missing["xliq.cryptohft.5m.archive_complete"] == 0.0
    assert math.isnan(missing["xliq.cryptohft.5m.notional_log"])
    store.close()
