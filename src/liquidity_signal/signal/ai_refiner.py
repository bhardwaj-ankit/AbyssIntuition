from __future__ import annotations

from liquidity_signal.config import SignalConfig
from liquidity_signal.models import AIRefinementBreakdown, Direction, LiquidityFeatures, ScoringBreakdown


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + (2.718281828459045 ** (-value)))


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def refine_signal_with_ai(
    features: LiquidityFeatures,
    scoring: ScoringBreakdown,
    cfg: SignalConfig,
) -> tuple[Direction, float, AIRefinementBreakdown]:
    """Model-like refinement layer over base rule score.

    This is a lightweight, deterministic meta-scorer that blends microstructure,
    model score, spread quality, and volatility quality into a refined signal.
    """
    microstructure_score = _clamp(
        (features.imbalance_l1 * 0.30)
        + (features.imbalance_l5 * 0.22)
        + (features.imbalance_l10 * 0.12)
        + (features.weighted_depth_imbalance * 0.18)
        + (((features.buy_flow_ratio - 0.5) * 2.0) * 0.18),
        -1.0,
        1.0,
    )

    model_score = scoring.raw_score
    spread_penalty = max(0.0, (features.spread_bps - cfg.max_spread_bps) / max(cfg.max_spread_bps, 0.1))
    volatility_penalty = max(0.0, (features.short_volatility_bps - cfg.high_volatility_bps) / max(cfg.high_volatility_bps, 1.0))
    liquidity_gap_penalty = max(
        0.0,
        (features.liquidity_gap_bps - cfg.elevated_liquidity_gap_bps) / max(cfg.elevated_liquidity_gap_bps, 0.1),
    )

    contextual_score = (
        (scoring.momentum_score * 0.32)
        + (scoring.regime_score * 0.28)
        + (scoring.positioning_score * 0.22)
        + (scoring.sentiment_score * 0.18)
    )
    combined_score = (model_score * 0.55) + (microstructure_score * 0.20) + (contextual_score * 0.25)
    combined_score -= (spread_penalty * 0.22) + (volatility_penalty * 0.14) + (liquidity_gap_penalty * 0.10)

    abs_score = abs(combined_score)
    refined_confidence = max(0.0, min(1.0, (_sigmoid(abs_score * 2.4) - 0.5) * 2.0))

    if combined_score >= cfg.long_threshold and refined_confidence >= cfg.min_confidence:
        refined_direction = Direction.LONG
    elif combined_score <= cfg.short_threshold and refined_confidence >= cfg.min_confidence:
        refined_direction = Direction.SHORT
    else:
        refined_direction = Direction.FLAT

    notes: list[str] = []
    if spread_penalty > 0.0:
        notes.append("Wide spread reduced confidence")
    if volatility_penalty > 0.0:
        notes.append("High short-term volatility reduced confidence")
    if liquidity_gap_penalty > 0.0:
        notes.append("Thin order book gaps reduced confidence")
    if features.htf_bias > 0.2 and refined_direction == Direction.LONG:
        notes.append("Higher timeframe bias supports long continuation")
    if features.htf_bias < -0.2 and refined_direction == Direction.SHORT:
        notes.append("Higher timeframe bias supports short continuation")
    if refined_direction == Direction.FLAT and abs_score > 0.08:
        notes.append("Directional edge exists but below confidence threshold")
    if not notes:
        notes.append("Microstructure and model score aligned")

    breakdown = AIRefinementBreakdown(
        enabled=True,
        refined_direction=refined_direction,
        refined_confidence=refined_confidence,
        combined_score=combined_score,
        microstructure_score=microstructure_score,
        model_score=model_score,
        volatility_penalty=volatility_penalty,
        spread_penalty=spread_penalty,
        notes=notes,
    )
    return refined_direction, refined_confidence, breakdown
