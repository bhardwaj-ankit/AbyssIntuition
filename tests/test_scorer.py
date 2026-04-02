from liquidity_signal.config import SignalConfig
from liquidity_signal.models import Direction, LiquidityFeatures
from liquidity_signal.signal.scorer import score_features


def test_long_signal_when_buy_pressure_is_strong() -> None:
    cfg = SignalConfig(min_confidence=0.2)
    features = LiquidityFeatures(
        symbol="BTCUSDT",
        mark_price=60000.0,
        spread_bps=1.0,
        imbalance_l1=0.7,
        imbalance_l5=0.5,
        buy_flow_ratio=0.72,
        short_volatility_bps=12.0,
    )

    direction, confidence, _ = score_features(features, cfg)
    assert direction == Direction.LONG
    assert confidence > 0.2


def test_flat_when_spread_too_wide() -> None:
    cfg = SignalConfig(max_spread_bps=3.0)
    features = LiquidityFeatures(
        symbol="BTCUSDT",
        mark_price=60000.0,
        spread_bps=10.0,
        imbalance_l1=0.9,
        imbalance_l5=0.9,
        buy_flow_ratio=0.9,
        short_volatility_bps=10.0,
    )

    direction, confidence, reasons = score_features(features, cfg)
    assert direction == Direction.FLAT
    assert confidence == 0.0
    assert "spread_too_wide" in reasons
