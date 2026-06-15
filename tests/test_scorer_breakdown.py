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
        imbalance_l10=0.25,
        weighted_depth_imbalance=0.3,
        buy_flow_ratio=0.7,
        short_volatility_bps=10.0,
        micro_momentum_bps=12.0,
        intraday_momentum_bps=20.0,
        open_interest_change_pct=2.5,
        funding_rate_bps=0.8,
        basis_bps=2.0,
        taker_buy_sell_ratio=1.05,
        global_long_short_ratio=1.03,
        top_trader_account_ratio=1.02,
        top_trader_position_ratio=1.04,
        htf_bias=0.35,
    )

    direction, confidence, reasons, breakdown = score_features_with_breakdown(features, cfg)

    assert direction in {Direction.LONG, Direction.FLAT, Direction.SHORT}
    assert 0.0 <= confidence <= 1.0
    assert reasons
    assert breakdown.microstructure_score > 0.0
    assert breakdown.momentum_score > 0.0
    assert breakdown.regime_score > 0.0
    assert breakdown.positioning_score >= 0.0
    assert breakdown.sentiment_score >= 0.0
    assert breakdown.alignment_bonus >= 0.0
    assert breakdown.execution_penalty >= 0.0
    assert breakdown.min_confidence == cfg.min_confidence
