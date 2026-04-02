from liquidity_signal.config import SignalConfig
from liquidity_signal.models import Direction, LiquidityFeatures
from liquidity_signal.signal.scorer import score_features_with_breakdown


def test_breakdown_contains_weighted_components() -> None:
    cfg = SignalConfig(min_confidence=0.2)
    features = LiquidityFeatures(
        symbol="BTCUSDT",
        mark_price=60000.0,
        spread_bps=1.0,
        imbalance_l1=0.6,
        imbalance_l5=0.4,
        buy_flow_ratio=0.7,
        short_volatility_bps=10.0,
    )

    direction, confidence, reasons, breakdown = score_features_with_breakdown(features, cfg)

    assert direction in {Direction.LONG, Direction.FLAT, Direction.SHORT}
    assert 0.0 <= confidence <= 1.0
    assert reasons
    assert breakdown.raw_score == (
        breakdown.imbalance_l1_weighted + breakdown.imbalance_l5_weighted + breakdown.flow_weighted
    )
    assert breakdown.min_confidence == cfg.min_confidence
