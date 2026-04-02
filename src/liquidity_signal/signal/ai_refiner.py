from __future__ import annotations

from liquidity_signal.config import SignalConfig
from liquidity_signal.models import AIRefinementBreakdown, Direction, LiquidityFeatures, ScoringBreakdown


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + (2.718281828459045 ** (-value)))


def refine_signal_with_ai(
    features: LiquidityFeatures,
    scoring: ScoringBreakdown,
    cfg: SignalConfig,
) -> tuple[Direction, float, AIRefinementBreakdown]:
    """Model-like refinement layer over base rule score.

    This is a lightweight, deterministic meta-scorer that blends microstructure,
    model score, spread quality, and volatility quality into a refined signal.
    """
    microstructure_score = (
        (features.imbalance_l1 * 0.45)
        + (features.imbalance_l5 * 0.35)
        + ((features.buy_flow_ratio - 1.0) * 0.9)
    )

    model_score = scoring.raw_score
    spread_penalty = max(0.0, (features.spread_bps - cfg.max_spread_bps) / max(cfg.max_spread_bps, 0.1))
    volatility_penalty = max(0.0, (features.short_volatility_bps - 38.0) / 38.0)

    combined_score = (model_score * 0.6) + (microstructure_score * 0.4)
    combined_score -= (spread_penalty * 0.25) + (volatility_penalty * 0.15)

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
