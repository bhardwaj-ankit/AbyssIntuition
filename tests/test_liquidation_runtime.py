from __future__ import annotations

from liquidity_signal.models import LiquidationStreamHealth
from liquidity_signal.service.liquidation_runtime import (
    LiquidationRuntime,
    _normalize_binance_force_order,
    _normalize_bybit_liquidation,
    qualify_capture_health,
)


def test_successful_reconnect_clears_previous_stream_error() -> None:
    runtime = LiquidationRuntime()
    runtime._set_health("binance", "BTCUSDT", last_error="timeout")
    runtime._set_health("binance", "BTCUSDT", connected=True, last_error=None)

    health = runtime.get_stream_health("BTCUSDT")

    assert len(health) == 1
    assert health[0].connected is True
    assert health[0].last_error is None
    runtime.close()


def test_binance_requires_recent_companion_market_heartbeat() -> None:
    stale = LiquidationStreamHealth(
        source="binance",
        symbol="BTCUSDT",
        connected=True,
        last_message_ts=1_000,
        events_buffered=0,
        reconnects=0,
    )
    bybit = stale.model_copy(update={"source": "bybit"})

    qualified = qualify_capture_health([stale, bybit], 20_000)

    assert qualified[0].connected is False
    assert "heartbeat is silent" in str(qualified[0].last_error)
    assert qualified[1].connected is True


def test_binance_scaled_pepe_alias_is_normalized_to_base_units() -> None:
    event = _normalize_binance_force_order({
        "E": 1_800_000_000_000,
        "o": {
            "s": "1000PEPEUSDT",
            "S": "SELL",
            "T": 1_800_000_000_000,
            "z": "2",
            "ap": "0.012",
        },
    }, "PEPEUSDT")

    assert event is not None
    assert event.symbol == "PEPEUSDT"
    assert event.price == 0.000012
    assert event.quantity == 2000.0
    assert event.notional == 0.024


def test_bybit_scaled_pepe_alias_is_normalized_to_base_units() -> None:
    events = _normalize_bybit_liquidation(
        {
            "data": [
                {
                    "s": "1000PEPEUSDT",
                    "S": "Buy",
                    "T": 1_800_000_000_000,
                    "v": "2",
                    "p": "0.012",
                }
            ]
        },
        "PEPEUSDT",
    )

    assert len(events) == 1
    event = events[0]
    assert event.symbol == "PEPEUSDT"
    assert event.price == 0.000012
    assert event.quantity == 2000.0
    assert event.notional == 0.024
    assert event.exchange_event_id == (
        "bybit:1000PEPEUSDT:1800000000000:BUY:2000.0:1.2e-05"
    )


def test_bybit_requested_symbol_rejects_an_unrelated_stream_row() -> None:
    events = _normalize_bybit_liquidation(
        {
            "data": [
                {
                    "s": "BTCUSDT",
                    "S": "Sell",
                    "T": 1_800_000_000_000,
                    "v": "2",
                    "p": "100",
                }
            ]
        },
        "ETHUSDT",
    )

    assert events == []
