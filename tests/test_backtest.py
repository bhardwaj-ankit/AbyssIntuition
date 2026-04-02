from liquidity_signal.service.engine import SignalEngine


class FakeClient:
    def get_recent_klines(self, symbol: str, interval: str = "1m", limit: int = 300):
        rows = []
        price = 100.0
        base_time = 1_700_000_000_000
        for i in range(limit):
            drift = 0.08 if i < limit // 2 else -0.05
            price = price + drift
            rows.append(
                [
                    base_time + i * 60_000,
                    f"{price - 0.1}",
                    f"{price + 0.3}",
                    f"{price - 0.3}",
                    f"{price}",
                    "10.0",
                ]
            )
        return rows

    def get_open_interest_hist(self, symbol: str, period: str = "5m", limit: int = 500):
        base_time = 1_700_000_000_000
        return [
            {
                "timestamp": str(base_time + i * 300_000),
                "sumOpenInterestValue": str(1_000_000_000 + i * 1_000_000),
            }
            for i in range(min(limit, 120))
        ]

    def get_funding_rates(self, symbol: str, limit: int = 200):
        base_time = 1_700_000_000_000
        return [
            {"fundingTime": str(base_time + i * 480 * 60_000), "fundingRate": "0.0001"}
            for i in range(min(limit, 5))
        ]


def test_bot_backtest_response_shape() -> None:
    engine = SignalEngine(client=FakeClient())
    result = engine.simulate_bot_backtest(
        symbol="BTCUSDT",
        interval="1m",
        candle_limit=180,
        initial_capital=1000.0,
    )

    assert result.symbol == "BTCUSDT"
    assert result.initial_capital == 1000.0
    assert result.final_capital > 0
    assert result.total_trades == len(result.trades)
    assert 0.0 <= result.win_rate <= 1.0
