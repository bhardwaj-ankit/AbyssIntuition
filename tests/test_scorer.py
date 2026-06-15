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
        imbalance_l10=0.35,
        weighted_depth_imbalance=0.45,
        buy_flow_ratio=0.72,
        short_volatility_bps=12.0,
        micro_momentum_bps=14.0,
        intraday_momentum_bps=28.0,
        open_interest_change_pct=3.5,
        funding_rate_bps=1.1,
        basis_bps=4.0,
        taker_buy_sell_ratio=1.08,
        global_long_short_ratio=1.04,
        top_trader_account_ratio=1.03,
        top_trader_position_ratio=1.05,
        htf_bias=0.42,
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


def test_flat_when_factors_conflict_even_with_positive_book_pressure() -> None:
    cfg = SignalConfig(min_confidence=0.35)
    features = LiquidityFeatures(
        symbol="BTCUSDT",
        mark_price=60000.0,
        spread_bps=1.5,
        imbalance_l1=0.55,
        imbalance_l5=0.40,
        imbalance_l10=0.10,
        weighted_depth_imbalance=0.15,
        buy_flow_ratio=0.62,
        short_volatility_bps=18.0,
        micro_momentum_bps=-10.0,
        intraday_momentum_bps=-24.0,
        open_interest_change_pct=4.0,
        funding_rate_bps=-1.4,
        basis_bps=-5.0,
        taker_buy_sell_ratio=0.93,
        global_long_short_ratio=0.95,
        top_trader_account_ratio=0.96,
        top_trader_position_ratio=0.94,
        htf_bias=-0.55,
    )

    direction, _, reasons = score_features(features, cfg)
    assert direction == Direction.FLAT
    assert "cross_factor_conflict" in reasons or "no_edge" in reasons
