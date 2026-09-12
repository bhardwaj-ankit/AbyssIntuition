from __future__ import annotations

import json

import pytest

from liquidity_signal.data.hyperliquid_archive import normalize_hyperliquid_rows
from liquidity_signal.models import Direction
from liquidity_signal.service.liquidation_store import LiquidationStore


def _row(*, user: str, direction: str, symbol: str = "BTC") -> dict[str, object]:
    return {
        "symbol": symbol,
        "recorded_at": "2026-08-02T02:20:13+00:00",
        "exchange_time": "2026-08-01T00:01:54.856000+00:00",
        "liquidation_id": "0xabc",
        "price": 100.0,
        "size": 2.0,
        "notional_usd": 200.0,
        "block_number": 123,
        "raw_json": json.dumps({
            "event": {
                "user": user,
                "dir": direction,
                "tid": 456,
                "liquidation": {"liquidatedUser": "0xliquidated"},
            }
        }),
    }


def test_hyperliquid_normalizer_removes_counterparty_and_maps_direction() -> None:
    rows = [
        _row(user="0xmaker", direction="Open Long"),
        _row(user="0xliquidated", direction="Close Long"),
    ]

    events = normalize_hyperliquid_rows(rows, {"BTCUSDT"})

    assert len(events) == 1
    assert events[0].symbol == "BTCUSDT"
    assert events[0].liquidated_side == Direction.LONG
    assert events[0].execution_side == "SELL"
    assert events[0].timestamp == 1_785_542_514_856


def test_hyperliquid_kpepe_is_rescaled_to_pepe_contract() -> None:
    row = _row(user="0xliquidated", direction="Close Short", symbol="kPEPE")

    event = normalize_hyperliquid_rows([row], {"PEPEUSDT"})[0]

    assert event.liquidated_side == Direction.SHORT
    assert event.execution_side == "BUY"
    assert event.price == pytest.approx(0.1)
    assert event.quantity == pytest.approx(2_000.0)
    assert event.notional == pytest.approx(200.0)


def test_archive_coverage_tracks_published_zero_event_days(tmp_path) -> None:
    store = LiquidationStore(tmp_path / "liquidations.db")
    store.record_archive_period(
        "hyperliquid_archive", "BTCUSDT", "2026-08-01", "complete", 0, ["one.parquet"]
    )
    store.record_archive_period(
        "hyperliquid_archive", "BTCUSDT", "2026-08-02", "unavailable", 0, []
    )

    coverage = store.archive_coverage(
        "hyperliquid_archive", "BTCUSDT", "2026-08-01", "2026-08-02"
    )

    assert coverage == {
        "audited_days": 2,
        "published_days": 1,
        "unavailable_days": 1,
        "rows": 0,
        "first_published_day": "2026-08-01",
        "last_published_day": "2026-08-01",
    }
    store.close()
