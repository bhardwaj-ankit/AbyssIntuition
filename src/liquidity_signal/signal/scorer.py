from __future__ import annotations

import math

from liquidity_signal.config import SignalConfig
from liquidity_signal.models import Direction, LiquidityFeatures, ScoringBreakdown


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _scale_signed(value: float, scale: float) -> float:
    if scale <= 0.0:
        return 0.0
    return _clamp(value / scale, -1.0, 1.0)


def _score_direction(value: float, threshold: float = 0.12) -> int:
    if value >= threshold:
        return 1
    if value <= -threshold:
        return -1
    return 0


def _confidence_curve(magnitude: float) -> float:
    return _clamp((1.0 / (1.0 + math.exp(-(magnitude * 3.6)))) * 1.75 - 0.85, 0.0, 1.0)


def score_features_with_breakdown(
    features: LiquidityFeatures, cfg: SignalConfig
) -> tuple[Direction, float, list[str], ScoringBreakdown]:
    reasons: list[str] = []

    spread_gate_triggered = features.spread_bps > cfg.max_spread_bps
    imbalance_l1_weighted = 0.45 * features.imbalance_l1
    imbalance_l5_weighted = 0.25 * features.imbalance_l5
    imbalance_l10_weighted = 0.12 * features.imbalance_l10
    weighted_depth_weighted = 0.18 * features.weighted_depth_imbalance
    flow_weighted = 0.30 * ((features.buy_flow_ratio - 0.5) * 2.0)

    microstructure_score = _clamp(
        imbalance_l1_weighted + imbalance_l5_weighted + imbalance_l10_weighted + weighted_depth_weighted + flow_weighted,
        -1.0,
        1.0,
    )
    momentum_score = _clamp(
        (0.42 * _scale_signed(features.micro_momentum_bps, 18.0))
        + (0.58 * _scale_signed(features.intraday_momentum_bps, 42.0)),
        -1.0,
        1.0,
    )
    regime_score = _clamp(features.htf_bias, -1.0, 1.0)
    positioning_score = _clamp(
        (0.55 * (_scale_signed(features.intraday_momentum_bps, 42.0) * max(_scale_signed(features.open_interest_change_pct, 6.0), 0.0)))
        + (0.25 * _scale_signed(features.funding_rate_bps, 2.5))
        + (0.20 * _scale_signed(features.basis_bps, 8.0)),
        -1.0,
        1.0,
    )
    sentiment_score = _clamp(
        (0.45 * _scale_signed((features.taker_buy_sell_ratio - 1.0) * 100.0, 12.0))
        + (0.20 * _scale_signed((features.global_long_short_ratio - 1.0) * 100.0, 10.0))
        + (0.17 * _scale_signed((features.top_trader_account_ratio - 1.0) * 100.0, 10.0))
        + (0.18 * _scale_signed((features.top_trader_position_ratio - 1.0) * 100.0, 10.0)),
        -1.0,
        1.0,
    )

    family_scores = [
        microstructure_score,
        momentum_score,
        regime_score,
        positioning_score,
        sentiment_score,
    ]
    bullish_votes = sum(1 for score in family_scores if _score_direction(score) > 0)
    bearish_votes = sum(1 for score in family_scores if _score_direction(score) < 0)
    dominant_votes = max(bullish_votes, bearish_votes)
    alignment_bonus = min(0.16, max(0, dominant_votes - 2) * 0.05)
    conflict_penalty = min(0.24, min(bullish_votes, bearish_votes) * 0.07)

    execution_penalty = min(
        0.34,
        max(0.0, (features.spread_bps - (cfg.max_spread_bps * 0.55)) / max(cfg.max_spread_bps, 0.1)) * 0.18
        + max(0.0, (features.short_volatility_bps - cfg.high_volatility_bps) / max(cfg.high_volatility_bps, 1.0)) * 0.10
        + max(0.0, (features.liquidity_gap_bps - cfg.elevated_liquidity_gap_bps) / max(cfg.elevated_liquidity_gap_bps, 0.1))
        * 0.08,
    )

    crowding_pressure = (
        abs(features.funding_rate_bps)
        + abs(features.basis_bps * 0.35)
        + abs((features.top_trader_position_ratio - 1.0) * 8.0)
    )
    crowding_penalty = min(0.16, max(0.0, crowding_pressure - 3.6) * 0.03)

    directional_score = (
        (0.36 * microstructure_score)
        + (0.21 * momentum_score)
        + (0.18 * regime_score)
        + (0.15 * positioning_score)
        + (0.10 * sentiment_score)
    )
    score_sign = 1.0 if directional_score > 0.0 else -1.0 if directional_score < 0.0 else 0.0
    magnitude = max(0.0, abs(directional_score) + alignment_bonus - conflict_penalty - execution_penalty - crowding_penalty)
    raw_score = score_sign * magnitude
    confidence = _confidence_curve(magnitude)

    if spread_gate_triggered:
        reasons.append("spread_too_wide")
        direction = Direction.FLAT
        confidence = 0.0
    elif raw_score >= cfg.long_threshold and confidence >= cfg.min_confidence and (
        bullish_votes >= 2 or microstructure_score >= 0.55
    ):
        reasons.append("buy_pressure")
        if microstructure_score > 0.18:
            reasons.append("order_book_bid_support")
        if momentum_score > 0.18:
            reasons.append("short_term_momentum_up")
        if regime_score > 0.18:
            reasons.append("higher_timeframe_trend_supportive")
        if positioning_score > 0.12:
            reasons.append("open_interest_and_funding_supportive")
        direction = Direction.LONG
    elif raw_score <= cfg.short_threshold and confidence >= cfg.min_confidence and (
        bearish_votes >= 2 or microstructure_score <= -0.55
    ):
        reasons.append("sell_pressure")
        if microstructure_score < -0.18:
            reasons.append("order_book_ask_pressure")
        if momentum_score < -0.18:
            reasons.append("short_term_momentum_down")
        if regime_score < -0.18:
            reasons.append("higher_timeframe_trend_bearish")
        if positioning_score < -0.12:
            reasons.append("open_interest_and_funding_bearish")
        direction = Direction.SHORT
    else:
        reasons.append("no_edge")
        if bullish_votes and bearish_votes:
            reasons.append("cross_factor_conflict")
        elif execution_penalty >= 0.12:
            reasons.append("execution_quality_too_low")
        elif dominant_votes >= 2 and confidence < cfg.min_confidence:
            reasons.append("edge_below_confidence_threshold")
        direction = Direction.FLAT

    if not spread_gate_triggered and crowding_penalty >= 0.08:
        reasons.append("crowded_positioning_reduced_quality")

    breakdown = ScoringBreakdown(
        spread_gate_triggered=spread_gate_triggered,
        max_spread_bps=cfg.max_spread_bps,
        imbalance_l1_weighted=imbalance_l1_weighted,
        imbalance_l5_weighted=imbalance_l5_weighted,
        imbalance_l10_weighted=imbalance_l10_weighted,
        weighted_depth_weighted=weighted_depth_weighted,
        flow_weighted=flow_weighted,
        microstructure_score=microstructure_score,
        momentum_score=momentum_score,
        regime_score=regime_score,
        positioning_score=positioning_score,
        sentiment_score=sentiment_score,
        alignment_bonus=alignment_bonus,
        conflict_penalty=conflict_penalty,
        execution_penalty=execution_penalty,
        crowding_penalty=crowding_penalty,
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
