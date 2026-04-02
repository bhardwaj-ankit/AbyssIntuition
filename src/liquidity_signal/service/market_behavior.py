"""Market Behavior Analysis Module

Analyzes market conditions and price action to identify trends, ranges,
consolidations, breakouts, and volatility patterns. Provides insights for
trading strategy optimization.
"""

from typing import List, Optional
from datetime import datetime

from liquidity_signal.models import Candle, Direction, MarketBehavior


class MarketBehaviorAnalyzer:
    """Analyzes market structure and price action patterns."""
    
    def __init__(self):
        self.min_candles_for_trend = 3
        self.volatility_threshold = 2.0  # Standard deviations
        self.consolidation_threshold = 0.5  # Percentage range for consolidation
        
    def analyze(
        self,
        symbol: str,
        candles: List[Candle],
        current_price: float,
        timestamp: int,
    ) -> Optional[MarketBehavior]:
        """
        Analyze market behavior from candle data.
        
        Args:
            symbol: Trading pair symbol
            candles: List of recent candlesticks
            current_price: Current market price
            timestamp: Unix timestamp
            
        Returns:
            MarketBehavior object with analysis results
        """
        if not candles or len(candles) < 3:
            return None
            
        # Check for different market patterns
        trend_analysis = self._detect_trend(candles)
        if trend_analysis:
            return trend_analysis
            
        volatility_analysis = self._detect_volatility(candles)
        if volatility_analysis:
            return volatility_analysis
            
        range_analysis = self._detect_range(candles, current_price)
        if range_analysis:
            return range_analysis
            
        consolidation_analysis = self._detect_consolidation(candles)
        if consolidation_analysis:
            return consolidation_analysis
            
        return None
    
    def _detect_trend(self, candles: List[Candle]) -> Optional[MarketBehavior]:
        """Detect uptrend or downtrend patterns."""
        if len(candles) < self.min_candles_for_trend:
            return None
            
        closes = [c.close for c in candles[-self.min_candles_for_trend:]]
        highs = [c.high for c in candles[-self.min_candles_for_trend:]]
        lows = [c.low for c in candles[-self.min_candles_for_trend:]]
        
        # Check for higher highs and higher lows (uptrend)
        is_uptrend = all(
            highs[i] <= highs[i+1] for i in range(len(highs)-1)
        ) and all(
            lows[i] <= lows[i+1] for i in range(len(lows)-1)
        )
        
        # Check for lower highs and lower lows (downtrend)
        is_downtrend = all(
            highs[i] >= highs[i+1] for i in range(len(highs)-1)
        ) and all(
            lows[i] >= lows[i+1] for i in range(len(lows)-1)
        )
        
        if is_uptrend:
            momentum = self._calculate_momentum(closes)
            strength = self._classify_strength(momentum)
            
            return MarketBehavior(
                symbol=candles[0].symbol if hasattr(candles[0], 'symbol') else "",
                behavior_type="uptrend",
                strength=strength,
                confidence=min(0.95, 0.7 + abs(momentum) / 100),
                description="Consecutive higher highs and higher lows indicating sustained upward momentum",
                trading_implication="Favor LONG positions in pullbacks to support. Watch for resistance at recent highs.",
                key_levels=highs[-3:],
                duration_candles=len(candles),
                supporting_stats={
                    "momentum": momentum,
                    "recent_high": highs[-1],
                    "recent_low": lows[-1],
                },
                timestamp=int(candles[-1].open_time / 1000),
            )
            
        elif is_downtrend:
            momentum = self._calculate_momentum(closes)
            strength = self._classify_strength(abs(momentum))
            
            return MarketBehavior(
                symbol=candles[0].symbol if hasattr(candles[0], 'symbol') else "",
                behavior_type="downtrend",
                strength=strength,
                confidence=min(0.95, 0.7 + abs(momentum) / 100),
                description="Consecutive lower highs and lower lows indicating sustained downward pressure",
                trading_implication="Favor SHORT positions into resistance. Scalp reversals near key levels.",
                key_levels=lows[-3:],
                duration_candles=len(candles),
                supporting_stats={
                    "momentum": momentum,
                    "recent_high": highs[-1],
                    "recent_low": lows[-1],
                },
                timestamp=int(candles[-1].open_time / 1000),
            )
            
        return None
    
    def _detect_volatility(self, candles: List[Candle]) -> Optional[MarketBehavior]:
        """Detect volatility spikes."""
        if len(candles) < 5:
            return None
            
        ranges = [c.high - c.low for c in candles]
        avg_range = sum(ranges) / len(ranges)
        current_range = ranges[-1]
        
        if current_range > avg_range * self.volatility_threshold:
            volume_change = (candles[-1].volume / (sum([c.volume for c in candles[:-1]]) / len(candles[:-1])) - 1) * 100
            
            return MarketBehavior(
                symbol=candles[0].symbol if hasattr(candles[0], 'symbol') else "",
                behavior_type="volatility_spike",
                strength="Strong",
                confidence=min(0.9, current_range / (avg_range * self.volatility_threshold)),
                description="Significant increase in volatility indicating market uncertainty",
                trading_implication="Reduce position sizes or avoid trading. Use wider stops to avoid whipsaws.",
                key_levels=[candles[-1].high, candles[-1].low],
                supporting_stats={
                    "current_range": current_range,
                    "avg_range": avg_range,
                    "range_multiple": current_range / avg_range,
                    "volume_change_pct": volume_change,
                },
                timestamp=int(candles[-1].open_time / 1000),
            )
            
        return None
    
    def _detect_range(self, candles: List[Candle], current_price: float) -> Optional[MarketBehavior]:
        """Detect range-bound markets."""
        if len(candles) < 5:
            return None
            
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        
        max_high = max(highs)
        min_low = min(lows)
        range_width = max_high - min_low
        range_pct = (range_width / min_low) * 100
        
        # Check if not trending (volatility in range)
        mid_price = (max_high + min_low) / 2
        
        # If price is oscillating within this range
        if range_pct > 1.0 and range_pct < 10.0:  # Reasonable range
            return MarketBehavior(
                symbol=candles[0].symbol if hasattr(candles[0], 'symbol') else "",
                behavior_type="ranging",
                strength="Moderate",
                confidence=0.75,
                description="Price oscillating between defined support and resistance levels",
                trading_implication="Use mean reversion strategies. Buy near support, sell near resistance.",
                key_levels=[max_high, min_low, mid_price],
                duration_candles=len(candles),
                supporting_stats={
                    "range_high": max_high,
                    "range_low": min_low,
                    "range_width": range_width,
                    "range_pct": range_pct,
                    "mid_price": mid_price,
                },
                timestamp=int(candles[-1].open_time / 1000),
            )
            
        return None
    
    def _detect_consolidation(self, candles: List[Candle]) -> Optional[MarketBehavior]:
        """Detect consolidation/squeeze patterns."""
        if len(candles) < 5:
            return None
            
        ranges = [c.high - c.low for c in candles]
        avg_range = sum(ranges) / len(ranges)
        
        # Check if recent candles have low range (consolidation)
        recent_ranges = ranges[-3:]
        avg_recent = sum(recent_ranges) / len(recent_ranges)
        
        if avg_recent < avg_range * 0.5:  # Consolidation has low range
            lows = [c.low for c in candles[-5:]]
            highs = [c.high for c in candles[-5:]]
            
            return MarketBehavior(
                symbol=candles[0].symbol if hasattr(candles[0], 'symbol') else "",
                behavior_type="consolidation",
                strength="Moderate",
                confidence=0.8,
                description="Low volatility period with price stuck in tight range, often preceding breakouts",
                trading_implication="Prepare for breakout. Build small position or wait for confirmation.",
                key_levels=[min(lows), max(highs)],
                duration_candles=len(candles),
                supporting_stats={
                    "consolidation_range": max(highs) - min(lows),
                    "avg_range": avg_range,
                    "range_compression": 1 - (avg_recent / avg_range),
                },
                timestamp=int(candles[-1].open_time / 1000),
            )
            
        return None
    
    def _calculate_momentum(self, closes: List[float]) -> float:
        """Calculate price momentum as percentage change."""
        if len(closes) < 2:
            return 0.0
        return ((closes[-1] - closes[0]) / closes[0]) * 100
    
    def _classify_strength(self, value: float) -> str:
        """Classify strength based on magnitude."""
        abs_val = abs(value)
        if abs_val > 2.0:
            return "Strong"
        elif abs_val > 1.0:
            return "Moderate"
        else:
            return "Weak"
