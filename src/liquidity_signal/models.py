from __future__ import annotations

from enum import Enum
from typing import List

from pydantic import BaseModel, Field


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class LiquidityFeatures(BaseModel):
    symbol: str
    mark_price: float
    spread_bps: float
    imbalance_l1: float
    imbalance_l5: float
    buy_flow_ratio: float
    short_volatility_bps: float


class SignalResult(BaseModel):
    symbol: str
    direction: Direction
    confidence: float = Field(ge=0.0, le=1.0)
    current_price: float
    tp: float
    sl: float
    reasons: List[str]


class AIRefinementBreakdown(BaseModel):
    enabled: bool
    refined_direction: Direction
    refined_confidence: float = Field(ge=0.0, le=1.0)
    combined_score: float
    microstructure_score: float
    model_score: float
    volatility_penalty: float
    spread_penalty: float
    notes: List[str]


class ScoringBreakdown(BaseModel):
    spread_gate_triggered: bool
    max_spread_bps: float
    imbalance_l1_weighted: float
    imbalance_l5_weighted: float
    flow_weighted: float
    raw_score: float
    confidence: float = Field(ge=0.0, le=1.0)
    long_threshold: float
    short_threshold: float
    min_confidence: float


class RiskBreakdown(BaseModel):
    base_stop_bps: float
    spread_adjustment_bps: float
    adjusted_stop_bps: float
    stop_bps: float
    confidence_boost: float
    tp_bps: float
    risk_reward: float


class SignalExplainResult(BaseModel):
    signal: SignalResult
    features: LiquidityFeatures
    scoring: ScoringBreakdown
    risk: RiskBreakdown
    ai_refinement: AIRefinementBreakdown | None = None


class Candle(BaseModel):
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class CandleResponse(BaseModel):
    symbol: str
    interval: str
    candles: List[Candle]


class BotTrade(BaseModel):
    side: Direction
    entry_time: int
    exit_time: int
    entry_time_local: str
    exit_time_local: str
    entry_price: float
    exit_price: float
    qty: float
    duration_minutes: float
    roi_pct: float
    gross_pnl: float
    fee_paid: float
    funding_paid: float
    net_pnl: float
    reason: str


class EquityPoint(BaseModel):
    time: int
    equity: float


class BotBacktestResponse(BaseModel):
    symbol: str
    interval: str
    initial_capital: float
    final_capital: float
    return_pct: float
    total_trades: int
    win_rate: float = Field(ge=0.0, le=1.0)
    fees_paid: float
    funding_paid: float
    max_drawdown_pct: float
    trades: List[BotTrade]
    trade_logs: List[str]
    equity_curve: List[EquityPoint]


class LiquidationLevel(BaseModel):
    side: Direction
    leverage: int
    level_price: float
    distance_bps: float
    strength: float = Field(ge=0.0)


class LiquidationCluster(BaseModel):
    side: Direction
    center_price: float
    min_price: float
    max_price: float
    total_strength: float = Field(ge=0.0)
    contributing_levels: int


class LiquidationAssumptions(BaseModel):
    leverage_buckets: List[int]
    leverage_weights: dict[int, float]
    maintenance_margin_rate: float
    cluster_step_bps: float
    open_interest_change_pct: float
    price_return_pct: float
    inferred_long_crowding: float
    inferred_short_crowding: float


class LiquidationMapResponse(BaseModel):
    symbol: str
    current_price: float
    is_estimated: bool
    methodology: str
    dominant_pull: Direction
    confidence: float = Field(ge=0.0, le=1.0)
    levels_above: List[LiquidationLevel]
    levels_below: List[LiquidationLevel]
    clusters_above: List[LiquidationCluster]
    clusters_below: List[LiquidationCluster]
    assumptions: LiquidationAssumptions
    meta: dict[str, float]


class MarketBehavior(BaseModel):
    symbol: str
    behavior_type: str  # 'uptrend', 'downtrend', 'ranging', 'breakout', 'volatility_spike', 'consolidation'
    strength: str  # 'Weak', 'Moderate', 'Strong'
    confidence: float = Field(ge=0.0, le=1.0)
    description: str
    trading_implication: str
    key_levels: List[float] = []
    duration_candles: int | None = None
    supporting_stats: dict[str, float] = Field(default_factory=dict)
    timestamp: int  # Unix timestamp
