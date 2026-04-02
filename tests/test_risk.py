from liquidity_signal.config import SignalConfig
from liquidity_signal.models import Direction, LiquidityFeatures
from liquidity_signal.risk.tpsl import compute_tp_sl


def _features() -> LiquidityFeatures:
    return LiquidityFeatures(
        symbol="BTCUSDT",
        mark_price=60000.0,
        spread_bps=1.5,
        imbalance_l1=0.2,
        imbalance_l5=0.2,
        buy_flow_ratio=0.6,
        short_volatility_bps=15.0,
    )


def test_long_tp_above_and_sl_below_price() -> None:
    cfg = SignalConfig()
    f = _features()

    tp, sl = compute_tp_sl(Direction.LONG, f.mark_price, 0.8, f, cfg)
    assert tp > f.mark_price
    assert sl < f.mark_price


def test_short_tp_below_and_sl_above_price() -> None:
    cfg = SignalConfig()
    f = _features()

    tp, sl = compute_tp_sl(Direction.SHORT, f.mark_price, 0.8, f, cfg)
    assert tp < f.mark_price
    assert sl > f.mark_price
