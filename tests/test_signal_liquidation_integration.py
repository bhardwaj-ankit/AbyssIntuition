from __future__ import annotations

from typing import Any

from liquidity_signal.config import SignalConfig
from liquidity_signal.models import Direction
from liquidity_signal.service.engine import SignalEngine


class _FakeBinanceClient:
    def close(self) -> None:
        return None

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


class _FakeBybitClient:
    def __init__(self) -> None:
        self.called = 0

    def close(self) -> None:
        return None

    def get_recent_liquidations(self, symbol: str, limit: int = 50) -> list[dict[str, str]]:
        self.called += 1
        return [{"side": "Sell", "price": "101.0", "size": "10", "updatedTime": "1710000000001"}]

    def get_recent_liquidations_with_meta(
        self, symbol: str, limit: int = 50
    ) -> tuple[list[dict[str, str]], int, None]:
        rows = self.get_recent_liquidations(symbol=symbol, limit=limit)
        return rows, 1_710_000_000_000, None


def test_signal_uses_advanced_liquidation_overlay() -> None:
    fake_bybit = _FakeBybitClient()
    engine = SignalEngine(client=_FakeBinanceClient(), cfg=SignalConfig(), bybit_client=fake_bybit)

    result = engine.generate_signal("BTCUSDT")

    assert fake_bybit.called == 1
    assert result.direction in {Direction.LONG, Direction.SHORT, Direction.FLAT}
    assert any("Liquidation map" in reason for reason in result.reasons)
