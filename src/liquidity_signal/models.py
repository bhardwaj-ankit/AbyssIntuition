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
    range_pct: float
    heatmap_resolution: int
    entry_cohort_bins: int
    open_interest_change_pct: float
    price_return_pct: float
    funding_rate_bps: float
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


class LiquidationEventPoint(BaseModel):
    source: str
    symbol: str
    liquidated_side: Direction
    execution_side: str
    price: float
    quantity: float
    notional: float
    timestamp: int
    received_at: int | None = Field(default=None, ge=0)
    exchange_event_id: str | None = None


class LiquidationStreamHealth(BaseModel):
    source: str
    symbol: str
    connected: bool
    last_message_ts: int | None = Field(default=None, ge=0)
    last_event_ts: int | None = Field(default=None, ge=0)
    events_buffered: int = Field(ge=0)
    reconnects: int = Field(ge=0)
    last_error: str | None = None


class LiquidationHeatmapSlice(BaseModel):
    timestamp: int
    current_price: float
    total_intensity: List[float]
    long_liquidation_intensity: List[float]
    short_liquidation_intensity: List[float]


class LiquidationDepthBand(BaseModel):
    side: str
    price: float
    cumulative_notional: float = Field(ge=0.0)
    distance_bps: float


class LiquidationMarketMetrics(BaseModel):
    mark_price: float
    index_price: float
    basis_bps: float
    last_funding_rate_bps: float
    open_interest_value: float
    open_interest_change_pct: float
    taker_buy_sell_ratio: float
    global_long_short_ratio: float
    top_trader_account_ratio: float
    top_trader_position_ratio: float
    order_book_imbalance: float
    recent_volume: float
    realized_volatility_bps: float
    next_funding_time: int | None = Field(default=None, ge=0)


class LiquidationEventsSummary(BaseModel):
    total_events: int = Field(ge=0)
    total_notional: float = Field(ge=0.0)
    long_liquidation_notional: float = Field(ge=0.0)
    short_liquidation_notional: float = Field(ge=0.0)
    latest_event_ts: int | None = Field(default=None, ge=0)


class LiquidationCalibrationProfile(BaseModel):
    intensity_multiplier: float = Field(ge=0.0)
    long_bias_multiplier: float = Field(ge=0.0)
    short_bias_multiplier: float = Field(ge=0.0)
    historical_event_count: float = Field(ge=0.0)


class LiquidationStorageStats(BaseModel):
    persistent_event_count_1h: int = Field(ge=0)
    persistent_event_count_24h: int = Field(ge=0)
    replay_snapshots_available: int = Field(ge=0)
    tile_resolutions_available: List[int] = Field(default_factory=list)


class LiquidationTileResponse(BaseModel):
    symbol: str
    generated_at: int
    range_pct: float
    resolution: int
    time_axis: List[int]
    price_levels: List[float]
    total_intensity: List[List[float]]
    long_intensity: List[List[float]]
    short_intensity: List[List[float]]


class LiquidationReplaySnapshot(BaseModel):
    symbol: str
    generated_at: int
    current_price: float
    dominant_pull: Direction
    confidence: float = Field(ge=0.0, le=1.0)
    source: str
    price_range_low: float
    price_range_high: float


class LiquidationDataQuality(BaseModel):
    estimate_weight: float = Field(ge=0.0, le=1.0)
    event_weight: float = Field(ge=0.0, le=1.0)
    bybit_available: bool
    event_overlay_active: bool
    events_used: int = Field(ge=0)
    degraded_mode: bool
    source_age_ms: int | None = Field(default=None, ge=0)
    degraded_reason: str | None = None
    stream_health: List[LiquidationStreamHealth] = Field(default_factory=list)
    notes: List[str]


class LiquidationMapAdvancedResponse(BaseModel):
    symbol: str
    current_price: float
    dominant_pull: Direction
    confidence: float = Field(ge=0.0, le=1.0)
    methodology: str
    source: str
    generated_at: int
    price_range_low: float
    price_range_high: float
    levels_above: List[LiquidationLevel]
    levels_below: List[LiquidationLevel]
    clusters_above: List[LiquidationCluster]
    clusters_below: List[LiquidationCluster]
    heatmap_price_levels: List[float]
    heatmap: List[LiquidationHeatmapSlice]
    depth_bands: List[LiquidationDepthBand]
    market_metrics: LiquidationMarketMetrics
    assumptions: LiquidationAssumptions
    calibration: LiquidationCalibrationProfile
    quality: LiquidationDataQuality
    events_summary: LiquidationEventsSummary
    storage: LiquidationStorageStats
    events: List[LiquidationEventPoint]
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
