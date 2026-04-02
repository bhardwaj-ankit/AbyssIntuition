from __future__ import annotations

from liquidity_signal.config import SignalConfig
from liquidity_signal.models import Direction, LiquidityFeatures, ScoringBreakdown


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def score_features_with_breakdown(
    features: LiquidityFeatures, cfg: SignalConfig
) -> tuple[Direction, float, list[str], ScoringBreakdown]:
    reasons: list[str] = []

    spread_gate_triggered = features.spread_bps > cfg.max_spread_bps
    imbalance_l1_weighted = 0.45 * features.imbalance_l1
    imbalance_l5_weighted = 0.25 * features.imbalance_l5
    flow_weighted = 0.30 * ((features.buy_flow_ratio - 0.5) * 2.0)
    raw_score = imbalance_l1_weighted + imbalance_l5_weighted + flow_weighted
    confidence = _clamp(abs(raw_score), 0.0, 1.0)

    if spread_gate_triggered:
        reasons.append("spread_too_wide")
        direction = Direction.FLAT
        confidence = 0.0
    elif raw_score >= cfg.long_threshold and confidence >= cfg.min_confidence:
        reasons.append("buy_pressure")
        direction = Direction.LONG
    elif raw_score <= cfg.short_threshold and confidence >= cfg.min_confidence:
        reasons.append("sell_pressure")
        direction = Direction.SHORT
    else:
        reasons.append("no_edge")
        direction = Direction.FLAT

    breakdown = ScoringBreakdown(
        spread_gate_triggered=spread_gate_triggered,
        max_spread_bps=cfg.max_spread_bps,
        imbalance_l1_weighted=imbalance_l1_weighted,
        imbalance_l5_weighted=imbalance_l5_weighted,
        flow_weighted=flow_weighted,
        raw_score=raw_score,
        confidence=confidence,
        long_threshold=cfg.long_threshold,
        short_threshold=cfg.short_threshold,
        min_confidence=cfg.min_confidence,
    )
    return direction, confidence, reasons, breakdown


def score_features(features: LiquidityFeatures, cfg: SignalConfig) -> tuple[Direction, float, list[str]]:
    direction, confidence, reasons, _ = score_features_with_breakdown(features, cfg)
    return direction, confidence, reasons
