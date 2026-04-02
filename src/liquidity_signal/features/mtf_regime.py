from __future__ import annotations

from enum import Enum
from typing import Any

from liquidity_signal.models import Direction, Candle


class RegimeStrength(str, Enum):
    WEAK = "WEAK"
    MODERATE = "MODERATE"
    STRONG = "STRONG"


class MarketRegime(str, Enum):
    STRONG_UPTREND = "STRONG_UPTREND"
    UPTREND = "UPTREND"
    MIXED_UP = "MIXED_UP"
    BALANCED = "BALANCED"
    MIXED_DOWN = "MIXED_DOWN"
    DOWNTREND = "DOWNTREND"
    STRONG_DOWNTREND = "STRONG_DOWNTREND"


def identify_timeframe_regime(
    candles: list[Candle], lookback: int = 20
) -> tuple[Direction, RegimeStrength]:
    """Identify trend/range regime for a single timeframe.

    Returns (direction, strength) where:
    - Direction: LONG (uptrend), SHORT (downtrend), FLAT (range)
    - Strength: WEAK, MODERATE, STRONG
    """
    if len(candles) < lookback + 1:
        return Direction.FLAT, RegimeStrength.WEAK

    visible = candles[-lookback - 1 :]
    closes = [c.close for c in visible]
    start = closes[0]
    end = closes[-1]

    if start <= 0:
        return Direction.FLAT, RegimeStrength.WEAK

    # Return in bps
    ret_bps = ((end - start) / start) * 10_000

    # Volatility in bps
    diffs = [abs(closes[j] - closes[j - 1]) for j in range(1, len(closes))]
    avg_move = sum(diffs) / len(diffs) if diffs else 0.0
    vol_bps = (avg_move / end) * 10_000 if end > 0 else 0.0

    # Trend threshold: 1.2x average move
    threshold_bps = max(12.0, vol_bps * 1.2)

    # Strength: trend magnitude relative to volatility
    if vol_bps > 0:
        strength_ratio = abs(ret_bps) / vol_bps
    else:
        strength_ratio = 0.0

    if strength_ratio >= 2.0:
        strength = RegimeStrength.STRONG
    elif strength_ratio >= 1.2:
        strength = RegimeStrength.MODERATE
    else:
        strength = RegimeStrength.WEAK

    if ret_bps >= threshold_bps:
        return Direction.LONG, strength
    if ret_bps <= -threshold_bps:
        return Direction.SHORT, strength
    return Direction.FLAT, strength


def combine_mtf_regimes(
    regime_1h: tuple[Direction, RegimeStrength],
    regime_4h: tuple[Direction, RegimeStrength],
    regime_12h: tuple[Direction, RegimeStrength],
) -> tuple[MarketRegime, float]:
    """Combine 3 timeframe regimes into overall market context.

    Returns (combined_regime, bias_factor) where:
    - combined_regime: 7-level regime classification
    - bias_factor: -1.0 (bearish) to +1.0 (bullish)
    """
    dir_1h, str_1h = regime_1h
    dir_4h, str_4h = regime_4h
    dir_12h, str_12h = regime_12h

    # Map direction to numeric: LONG=+1, FLAT=0, SHORT=-1
    def dir_score(d: Direction, s: RegimeStrength) -> float:
        multiplier = {"WEAK": 0.5, "MODERATE": 1.0, "STRONG": 1.5}[s.value]
        if d == Direction.LONG:
            return 1.0 * multiplier
        if d == Direction.SHORT:
            return -1.0 * multiplier
        return 0.0

    score_1h = dir_score(dir_1h, str_1h)
    score_4h = dir_score(dir_4h, str_4h)
    score_12h = dir_score(dir_12h, str_12h)

    # Weighted average: 12h (40%) + 4h (35%) + 1h (25%)
    combined_score = (score_12h * 0.4) + (score_4h * 0.35) + (score_1h * 0.25)
    bias_factor = max(-1.0, min(1.0, combined_score / 2.0))

    # Classify based on combined score
    if combined_score >= 3.0:
        regime = MarketRegime.STRONG_UPTREND
    elif combined_score >= 1.5:
        regime = MarketRegime.UPTREND
    elif combined_score >= 0.5:
        regime = MarketRegime.MIXED_UP
    elif combined_score >= -0.5:
        regime = MarketRegime.BALANCED
    elif combined_score >= -1.5:
        regime = MarketRegime.MIXED_DOWN
    elif combined_score >= -3.0:
        regime = MarketRegime.DOWNTREND
    else:
        regime = MarketRegime.STRONG_DOWNTREND

    return regime, bias_factor


class MTFRegimeAnalyzer:
    def __init__(self) -> None:
        pass

    def analyze(
        self,
        klines_1h: list[list[Any]],
        klines_4h: list[list[Any]],
        klines_12h: list[list[Any]],
    ) -> tuple[MarketRegime, float, dict[str, Any]]:
        """Analyze multi-timeframe regime.

        Returns (combined_regime, bias_factor, breakdown_dict)
        """
        candles_1h = [
            Candle(
                open_time=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in klines_1h
        ]
        candles_4h = [
            Candle(
                open_time=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in klines_4h
        ]
        candles_12h = [
            Candle(
                open_time=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in klines_12h
        ]

        regime_1h = identify_timeframe_regime(candles_1h, lookback=20)
        regime_4h = identify_timeframe_regime(candles_4h, lookback=20)
        regime_12h = identify_timeframe_regime(candles_12h, lookback=20)

        combined, bias = combine_mtf_regimes(regime_1h, regime_4h, regime_12h)

        breakdown = {
            "regime_1h": {"direction": regime_1h[0].value, "strength": regime_1h[1].value},
            "regime_4h": {"direction": regime_4h[0].value, "strength": regime_4h[1].value},
            "regime_12h": {"direction": regime_12h[0].value, "strength": regime_12h[1].value},
        }

        return combined, bias, breakdown
