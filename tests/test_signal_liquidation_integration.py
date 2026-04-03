from __future__ import annotations

from typing import Any

from liquidity_signal.config import SignalConfig
from liquidity_signal.models import Direction, LiquidationEventPoint, LiquidationStreamHealth
from liquidity_signal.service.engine import SignalEngine


class _FakeBinanceClient:
    def close(self) -> None:
        return None

    def get_mark_price_info(self, symbol: str) -> dict[str, Any]:
        return {
            "markPrice": "100.0",
            "indexPrice": "100.1",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 1_710_000_000_000,
        }

    def get_mark_price(self, symbol: str) -> float:
        return 100.0

    def get_order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        bids = [["99.9", "12"] for _ in range(5)]
        asks = [["100.1", "8"] for _ in range(5)]
        return {"bids": bids, "asks": asks}

    def get_recent_trades(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for i in range(20):
            rows.append({"qty": "1.0", "isBuyerMaker": bool(i % 2)})
        return rows

    def get_recent_klines(self, symbol: str, interval: str = "1m", limit: int = 20) -> list[list[Any]]:
        return [[0, 100.0, 100.2, 99.8, 100.0 + (i * 0.02), 1000.0] for i in range(limit)]

    def get_open_interest_hist(self, symbol: str, period: str = "5m", limit: int = 30) -> list[dict[str, str]]:
        return [
            {"sumOpenInterestValue": "1000000000"},
            {"sumOpenInterestValue": "1250000000"},
        ]

    def get_funding_rates(self, symbol: str, limit: int = 30) -> list[dict[str, str]]:
        return [{"fundingRate": "0.0001"}]

    def get_basis(self, symbol: str, period: str = "5m", limit: int = 30) -> list[dict[str, str]]:
        return [{"basisRate": "0.0002", "timestamp": "1710000000000"}]

    def get_taker_buy_sell_volume(self, symbol: str, period: str = "5m", limit: int = 30) -> list[dict[str, str]]:
        return [{"buySellRatio": "1.2", "timestamp": "1710000000000"}]

    def get_global_long_short_account_ratio(
        self, symbol: str, period: str = "5m", limit: int = 30
    ) -> list[dict[str, str]]:
        return [{"longShortRatio": "1.1", "timestamp": "1710000000000"}]

    def get_top_long_short_account_ratio(
        self, symbol: str, period: str = "5m", limit: int = 30
    ) -> list[dict[str, str]]:
        return [{"longShortRatio": "1.2", "timestamp": "1710000000000"}]

    def get_top_long_short_position_ratio(
        self, symbol: str, period: str = "5m", limit: int = 30
    ) -> list[dict[str, str]]:
        return [{"longShortRatio": "1.05", "timestamp": "1710000000000"}]


class _FakeRuntime:
    def __init__(self) -> None:
        self.called = 0

    def close(self) -> None:
        return None

    def ensure_symbol(self, symbol: str) -> None:
        self.called += 1

    def get_recent_events(self, symbol: str, limit: int = 50) -> list[LiquidationEventPoint]:
        return [
            LiquidationEventPoint(
                source="binance",
                symbol=symbol,
                liquidated_side=Direction.SHORT,
                execution_side="BUY",
                price=101.0,
                quantity=10.0,
                notional=1010.0,
                timestamp=1_710_000_000_001,
                received_at=1_710_000_000_100,
                exchange_event_id="binance:test",
            )
        ]

    def get_stream_health(self, symbol: str) -> list[LiquidationStreamHealth]:
        return [
            LiquidationStreamHealth(
                source="binance",
                symbol=symbol,
                connected=True,
                last_message_ts=1_710_000_000_100,
                last_event_ts=1_710_000_000_001,
                events_buffered=1,
                reconnects=0,
                last_error=None,
            )
        ]


def test_signal_uses_advanced_liquidation_overlay() -> None:
    fake_runtime = _FakeRuntime()
    engine = SignalEngine(client=_FakeBinanceClient(), cfg=SignalConfig(), liquidation_runtime=fake_runtime)

    result = engine.generate_signal("BTCUSDT")

    assert fake_runtime.called == 1
    assert result.direction in {Direction.LONG, Direction.SHORT, Direction.FLAT}
    assert any("Liquidation map" in reason for reason in result.reasons)
