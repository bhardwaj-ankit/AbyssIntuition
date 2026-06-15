from pathlib import Path

from liquidity_signal.service.engine import SignalEngine
from liquidity_signal.service.liquidation_store import LiquidationStore
from tests.test_backtest import FakeClient


def test_backtest_persists_runs_and_trades_with_signal_metadata(tmp_path: Path) -> None:
    store = LiquidationStore(db_path=tmp_path / "backtest_history.db")
    engine = SignalEngine(client=FakeClient(), liquidation_store=store)

    result = engine.simulate_bot_backtest(
        symbol="BTCUSDT",
        interval="1m",
        candle_limit=220,
        initial_capital=1000.0,
        confidence_threshold=0.5,
        use_liquidation_data=False,
        use_sentiment_data=False,
        use_volume_filter=False,
        use_htf_filter=False,
        max_trades=10,
    )

    runs = store.load_recent_backtest_runs("BTCUSDT", limit=5)
    trades = store.load_recent_backtest_trades("BTCUSDT", limit=20)

    assert runs
    assert runs[0]["run_id"] == result.run_id
    assert runs[0]["signal_source"] == engine.BACKTEST_SIGNAL_SOURCE

    assert trades
    assert all(trade["run_id"] == result.run_id for trade in trades)
    assert all("confidence=" in trade["entry_reason"] for trade in trades)
    assert all(trade["exit_reason"] for trade in trades)
    assert all(trade["signal_source"] == engine.BACKTEST_SIGNAL_SOURCE for trade in trades)

    store.close()
