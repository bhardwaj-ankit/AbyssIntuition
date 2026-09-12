from __future__ import annotations

from enum import Enum
from typing import Any, List, Literal

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
    imbalance_l10: float = 0.0
    weighted_depth_imbalance: float = 0.0
    buy_flow_ratio: float
    short_volatility_bps: float
    micro_momentum_bps: float = 0.0
    intraday_momentum_bps: float = 0.0
    volume_zscore: float = 0.0
    liquidity_gap_bps: float = 0.0
    funding_rate_bps: float = 0.0
    basis_bps: float = 0.0
    open_interest_change_pct: float = 0.0
    taker_buy_sell_ratio: float = 1.0
    global_long_short_ratio: float = 1.0
    top_trader_account_ratio: float = 1.0
    top_trader_position_ratio: float = 1.0
    htf_bias: float = Field(default=0.0, ge=-1.0, le=1.0)
    htf_regime: str = "BALANCED"
    # Provenance is part of the feature contract. Historical backfills without
    # depth/trade archives use candle-derived proxies, while live requests use
    # exchange observations. Downstream models must never treat those as the
    # same feature distribution.
    order_book_source: Literal["real", "proxy", "unknown"] = "unknown"
    trade_flow_source: Literal["real", "proxy", "unknown"] = "unknown"


class SignalResult(BaseModel):
    symbol: str
    direction: Direction
    confidence: float = Field(ge=0.0, le=1.0)
    current_price: float
    tp: float
    sl: float
    reasons: List[str]
    horizon: str = "5m"
    decision_ts: int | None = Field(default=None, ge=0)
    expires_at: int | None = Field(default=None, ge=0)
    entry_assumption: str = "mark"
    model_version: str = "signal-v0.2.0"
    feature_version: str = "features-v0.2.0"
    signal_quality: str = "STANDARD"


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
    imbalance_l10_weighted: float
    weighted_depth_weighted: float
    flow_weighted: float
    microstructure_score: float
    momentum_score: float
    regime_score: float
    positioning_score: float
    sentiment_score: float
    alignment_bonus: float
    conflict_penalty: float
    execution_penalty: float
    crowding_penalty: float
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


class SignalApiResponse(BaseModel):
    symbol: str
    signal: SignalResult
    explain: SignalExplainResult
    cumulative: "CumulativeSignalResponse"


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


class MarketSymbol(BaseModel):
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    contract_type: str


class MarketSymbolsResponse(BaseModel):
    symbols: List[MarketSymbol]


class BotTrade(BaseModel):
    run_id: str | None = None
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
    entry_reason: str = ""
    exit_reason: str = ""
    entry_signal_direction: Direction = Direction.FLAT
    entry_signal_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    entry_signal_quality: str = "UNKNOWN"
    signal_horizon: str = "N/A"
    signal_source: str = "adaptive_backtest_strategy"
    reason: str


class EquityPoint(BaseModel):
    time: int
    equity: float


class BotBacktestResponse(BaseModel):
    run_id: str | None = None
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
    signal_source: str = "adaptive_backtest_strategy"
    trades: List[BotTrade]
    trade_logs: List[str]
    equity_curve: List[EquityPoint]


class PaperBotPosition(BaseModel):
    side: Direction
    entry_time: int
    entry_time_local: str
    entry_price: float
    qty: float
    leverage: float = 1.0
    tp_price: float
    sl_price: float
    liquidation_price: float | None = None
    entry_reason: str = ""
    entry_signal_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    entry_signal_quality: str = "UNKNOWN"
    signal_horizon: str = "N/A"
    signal_source: str = "signal_api_live_regime_bot"
    current_price: float
    unrealized_pnl: float = 0.0
    unrealized_roi_pct: float = 0.0


class PaperBotStatus(BaseModel):
    session_id: str | None = None
    symbol: str
    running: bool
    started_at: int | None = Field(default=None, ge=0)
    scheduled_end_at: int | None = Field(default=None, ge=0)
    stopped_at: int | None = Field(default=None, ge=0)
    poll_interval_seconds: int = Field(default=60, ge=5)
    mode: str = "BALANCED"
    leverage: float = 1.0
    initial_capital: float
    current_capital: float
    realized_pnl: float
    unrealized_pnl: float = 0.0
    fees_paid: float = 0.0
    total_trades: int = 0
    win_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    signal_source: str = "signal_api_live_regime_bot"
    open_position: PaperBotPosition | None = None
    last_signal: SignalResult | None = None
    last_action: str = "idle"
    last_updated_at: int | None = Field(default=None, ge=0)
    recent_trades: List[BotTrade] = Field(default_factory=list)


class DemoBotPosition(BaseModel):
    symbol: str
    side: Direction
    size: float
    entry_price: float
    mark_price: float
    leverage: float = 1.0
    take_profit: float | None = None
    stop_loss: float | None = None
    liquidation_price: float | None = None
    liquidation_buffer_pct: float | None = None
    position_value: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_roi_pct: float = 0.0
    order_link_id: str | None = None
    opened_at: int | None = Field(default=None, ge=0)


class AITradeDecision(BaseModel):
    enabled: bool = False
    source: str = "OPENAI"
    model: str | None = None
    review_type: str = "entry"
    entry_verdict: str = "SKIPPED"
    size_multiplier: float = Field(default=1.0, ge=0.0, le=1.0)
    confidence_adjustment: float = Field(default=0.0, ge=-0.2, le=0.2)
    exit_action: str = "HOLD"
    stop_adjustment_pct: float = Field(default=0.0, ge=-0.2, le=0.2)
    take_profit_adjustment_pct: float = Field(default=0.0, ge=-0.2, le=0.2)
    reason: str = ""
    risk_flags: List[str] = Field(default_factory=list)
    reviewed_at: int | None = Field(default=None, ge=0)


class AISupervisorConfigStatus(BaseModel):
    enabled: bool
    configured: bool
    active: bool
    provider: str = "OPENAI"
    model: str
    config_path: str
    config_source: str = "defaults"
    message: str


class DemoBotStatus(BaseModel):
    session_id: str | None = None
    symbol: str
    running: bool
    exchange: str = "BYBIT"
    environment: str = "DEMO"
    started_at: int | None = Field(default=None, ge=0)
    stopped_at: int | None = Field(default=None, ge=0)
    poll_interval_seconds: int = Field(default=30, ge=5)
    mode: str = "BALANCED"
    leverage: float = 1.0
    risk_per_trade_pct: float = Field(default=0.01, ge=0.0, le=0.1)
    max_margin_fraction: float = Field(default=0.35, ge=0.0, le=1.0)
    cooldown_seconds: int = Field(default=180, ge=0)
    cooldown_until: int | None = Field(default=None, ge=0)
    account_equity: float = 0.0
    wallet_balance: float = 0.0
    available_balance: float = 0.0
    total_trades: int = 0
    signal_source: str = "bybit_demo_live_bot"
    ai_enabled: bool = False
    ai_model: str | None = None
    ai_last_decision: AITradeDecision | None = None
    open_position: DemoBotPosition | None = None
    last_signal: SignalResult | None = None
    last_action: str = "idle"
    last_error: str | None = None
    last_updated_at: int | None = Field(default=None, ge=0)
    recent_trades: List[BotTrade] = Field(default_factory=list)


class DemoBotConfigStatus(BaseModel):
    configured: bool
    config_path: str
    config_source: str = "defaults"
    base_url: str
    category: str
    account_type: str
    default_symbol: str
    ai_enabled: bool = False
    ai_configured: bool = False
    ai_model: str | None = None
    ai_message: str = ""
    message: str


class DemoBotPerformanceSummary(BaseModel):
    total_trades: int = 0
    win_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    net_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: float | None = None
    avg_roi_pct: float = 0.0
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0
    total_fees: float = 0.0
    total_funding: float = 0.0
    last_trade_at: int | None = Field(default=None, ge=0)


class DemoBotPerformanceResponse(BaseModel):
    symbol: str
    exchange: str = "BYBIT"
    environment: str = "DEMO"
    lookback_trades: int = 50
    summary: DemoBotPerformanceSummary
    trades: List[BotTrade] = Field(default_factory=list)


class TrainingSnapshotRecord(BaseModel):
    snapshot_id: str
    symbol: str
    event_ts: int = Field(ge=0)
    exchange: str
    environment: str
    mark_price: float
    signal_direction: Direction
    signal_confidence: float = Field(ge=0.0, le=1.0)
    signal_quality: str = "STANDARD"
    decision_action: str
    took_trade: bool = False
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class TrainingSnapshotLabel(BaseModel):
    snapshot_id: str
    symbol: str
    event_ts: int = Field(ge=0)
    horizon_minutes: int = Field(ge=1)
    status: str = "PENDING"
    label_action: Direction = Direction.FLAT
    expires_at: int = Field(ge=0)
    resolved_at: int | None = Field(default=None, ge=0)
    upper_barrier_price: float
    lower_barrier_price: float
    barrier_first_hit: Direction | None = None
    barrier_hit_ts: int | None = Field(default=None, ge=0)
    barrier_hit_price: float | None = None
    barrier_ambiguous: bool = False
    horizon_close_price: float | None = None
    horizon_return_bps: float | None = None
    max_favorable_excursion_pct: float | None = None
    max_adverse_excursion_pct: float | None = None
    # Kept for backwards compatibility with v1 datasets. New labels set this
    # to the true horizon close, not the close of the first barrier candle.
    terminal_price: float | None = None
    max_up_pct: float = 0.0
    max_down_pct: float = 0.0
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class TrainingDatasetSummary(BaseModel):
    symbol: str
    total_snapshots: int = 0
    total_decisions: int = 0
    total_labels: int = 0
    resolved_labels: int = 0
    pending_labels: int = 0
    long_labels: int = 0
    short_labels: int = 0
    flat_labels: int = 0


class TrainingDatasetResponse(BaseModel):
    symbol: str
    summary: TrainingDatasetSummary
    snapshots: List[TrainingSnapshotRecord] = Field(default_factory=list)
    labels: List[TrainingSnapshotLabel] = Field(default_factory=list)


class LoraTrainingExample(BaseModel):
    snapshot_id: str
    symbol: str
    horizon_minutes: int = Field(ge=1)
    label_action: Direction
    decision_action: str
    took_trade: bool = False
    split: str = "train"
    prompt: str
    completion: str
    messages: List[dict[str, str]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LoraTrainingExportSummary(BaseModel):
    symbol: str
    total_examples: int = 0
    horizon_minutes: int = 15
    balance_mode: str = "none"
    raw_examples: int = 0
    raw_long_examples: int = 0
    raw_short_examples: int = 0
    raw_flat_examples: int = 0
    long_examples: int = 0
    short_examples: int = 0
    flat_examples: int = 0
    train_examples: int = 0
    validation_examples: int = 0
    test_examples: int = 0
    class_weights: dict[str, float] = Field(default_factory=dict)


class LoraTrainingExportResponse(BaseModel):
    symbol: str
    horizon_minutes: int = 15
    summary: LoraTrainingExportSummary
    examples: List[LoraTrainingExample] = Field(default_factory=list)


class HistoricalTrainingBackfillResponse(BaseModel):
    symbol: str
    lookback_hours: int = Field(ge=1)
    step_minutes: int = Field(ge=1)
    samples_attempted: int = Field(ge=0)
    snapshots_created: int = Field(ge=0)
    resolved_labels: int = Field(ge=0)
    skipped_samples: int = Field(ge=0)
    used_liquidation_snapshots: int = Field(ge=0)
    notes: List[str] = Field(default_factory=list)


class HistoricalTrainingBackfillBatchItem(BaseModel):
    symbol: str
    samples_attempted: int = Field(ge=0)
    snapshots_created: int = Field(ge=0)
    resolved_labels: int = Field(ge=0)
    skipped_samples: int = Field(ge=0)
    used_liquidation_snapshots: int = Field(ge=0)
    notes: List[str] = Field(default_factory=list)


class HistoricalTrainingBackfillBatchResponse(BaseModel):
    symbols: List[str] = Field(default_factory=list)
    lookback_hours: int = Field(ge=1)
    step_minutes: int = Field(ge=1)
    total_samples_attempted: int = Field(ge=0)
    total_snapshots_created: int = Field(ge=0)
    total_resolved_labels: int = Field(ge=0)
    total_skipped_samples: int = Field(ge=0)
    total_used_liquidation_snapshots: int = Field(ge=0)
    items: List[HistoricalTrainingBackfillBatchItem] = Field(default_factory=list)


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


class CumulativeSignalTimeframe(BaseModel):
    timeframe: str
    market_mode: str = "TRANSITIONAL"
    regime_strength: float = Field(default=0.0, ge=0.0, le=1.0)
    direction: Direction
    confidence: float = Field(ge=0.0, le=1.0)
    signal_quality: str = "STANDARD"
    score: float
    max_score: float
    close: float
    ema20: float
    ema50: float
    ema200: float
    vwap: float
    rsi14: float | None = None
    macd: float
    macd_signal: float
    macd_histogram: float
    adx14: float | None = None
    atr_pct: float = 0.0
    atr_state: str = "NEUTRAL"
    breakout_failure_rate: float = 0.0
    volume_ratio: float
    pattern: str = "Mixed Structure"
    nearest_support: float | None = None
    nearest_resistance: float | None = None
    reasons: List[str] = Field(default_factory=list)


class CumulativeSignalResponse(BaseModel):
    symbol: str
    generated_at: int
    market_mode: str = "TRANSITIONAL"
    strategy_mode: str = "STAY_FLAT"
    aggregate_direction: Direction
    aggregate_confidence: float = Field(ge=0.0, le=1.0)
    agreement_ratio: float = Field(ge=0.0, le=1.0)
    signal_quality: str = "STANDARD"
    reasons: List[str] = Field(default_factory=list)
    bot_signal: SignalResult
    timeframes: List[CumulativeSignalTimeframe] = Field(default_factory=list)


SignalApiResponse.model_rebuild()
