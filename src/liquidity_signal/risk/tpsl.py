from __future__ import annotations

from liquidity_signal.config import SignalConfig
from liquidity_signal.models import Direction, LiquidityFeatures, RiskBreakdown


def compute_tp_sl_with_breakdown(
    direction: Direction,
    current_price: float,
    confidence: float,
    features: LiquidityFeatures,
    cfg: SignalConfig,
) -> tuple[float, float, RiskBreakdown]:
    base_stop_bps = max(features.short_volatility_bps * 1.2, cfg.min_stop_bps)
    # Widen stop for better scalp resilience, reduce false stops.
    spread_adjustment = max(0.0, features.spread_bps - 0.5)
    adjusted_stop_bps = base_stop_bps + 0.55 * spread_adjustment
    # Ensure minimum stop of 12bps for scalp entries (reduced whipsaw).
    stop_bps = max(min(adjusted_stop_bps, cfg.max_stop_bps), 12.0)
    confidence_boost = 1.0 + (confidence * 0.25)
    # Wider TP/SL ratio for scalps: 1.6-1.8x instead of 1.35x.
    tp_bps = stop_bps * max(cfg.risk_reward, 1.6) * confidence_boost

    if direction == Direction.FLAT or current_price <= 0.0:
        breakdown = RiskBreakdown(
            base_stop_bps=base_stop_bps,
            spread_adjustment_bps=spread_adjustment,
            adjusted_stop_bps=adjusted_stop_bps,
            stop_bps=stop_bps,
            confidence_boost=confidence_boost,
            tp_bps=tp_bps,
            risk_reward=cfg.risk_reward,
        )
        return current_price, current_price, breakdown

    stop_dist = current_price * (stop_bps / 10_000)
    tp_dist = current_price * (tp_bps / 10_000)

    if direction == Direction.LONG:
        tp = current_price + tp_dist
        sl = current_price - stop_dist
    else:
        tp = current_price - tp_dist
        sl = current_price + stop_dist

    breakdown = RiskBreakdown(
        base_stop_bps=base_stop_bps,
        spread_adjustment_bps=spread_adjustment,
        adjusted_stop_bps=adjusted_stop_bps,
        stop_bps=stop_bps,
        confidence_boost=confidence_boost,
        tp_bps=tp_bps,
        risk_reward=cfg.risk_reward,
    )
    return tp, sl, breakdown


def compute_tp_sl(
    direction: Direction, current_price: float, confidence: float, features: LiquidityFeatures, cfg: SignalConfig
) -> tuple[float, float]:
    tp, sl, _ = compute_tp_sl_with_breakdown(direction, current_price, confidence, features, cfg)
    return tp, sl
