import Foundation

enum Direction: String, Codable, CaseIterable, Identifiable {
    case long = "LONG"
    case short = "SHORT"
    case flat = "FLAT"

    var id: String { rawValue }

    var displayName: String { rawValue.capitalized }
}

struct SignalAPIResponse: Decodable {
    let symbol: String
    let signal: SignalResult
    let explain: SignalExplainResult
    let cumulative: CumulativeSignalResponse
}

struct SignalResult: Decodable {
    let symbol: String
    let direction: Direction
    let confidence: Double
    let currentPrice: Double
    let tp: Double
    let sl: Double
    let reasons: [String]
    let horizon: String
    let signalQuality: String

    enum CodingKeys: String, CodingKey {
        case symbol
        case direction
        case confidence
        case currentPrice = "current_price"
        case tp
        case sl
        case reasons
        case horizon
        case signalQuality = "signal_quality"
    }
}

struct SignalExplainResult: Decodable {
    let signal: SignalResult
    let features: LiquidityFeatures
    let scoring: ScoringBreakdown
    let risk: RiskBreakdown
}

struct LiquidityFeatures: Decodable {
    let markPrice: Double
    let spreadBps: Double
    let buyFlowRatio: Double
    let shortVolatilityBps: Double
    let volumeZscore: Double
    let fundingRateBps: Double
    let basisBps: Double
    let openInterestChangePct: Double
    let htfRegime: String

    enum CodingKeys: String, CodingKey {
        case markPrice = "mark_price"
        case spreadBps = "spread_bps"
        case buyFlowRatio = "buy_flow_ratio"
        case shortVolatilityBps = "short_volatility_bps"
        case volumeZscore = "volume_zscore"
        case fundingRateBps = "funding_rate_bps"
        case basisBps = "basis_bps"
        case openInterestChangePct = "open_interest_change_pct"
        case htfRegime = "htf_regime"
    }
}

struct ScoringBreakdown: Decodable {
    let rawScore: Double
    let confidence: Double
    let alignmentBonus: Double
    let conflictPenalty: Double

    enum CodingKeys: String, CodingKey {
        case rawScore = "raw_score"
        case confidence
        case alignmentBonus = "alignment_bonus"
        case conflictPenalty = "conflict_penalty"
    }
}

struct RiskBreakdown: Decodable {
    let stopBps: Double
    let tpBps: Double
    let riskReward: Double

    enum CodingKeys: String, CodingKey {
        case stopBps = "stop_bps"
        case tpBps = "tp_bps"
        case riskReward = "risk_reward"
    }
}

struct CumulativeSignalResponse: Decodable {
    let symbol: String
    let generatedAt: Int
    let marketMode: String
    let strategyMode: String
    let aggregateDirection: Direction
    let aggregateConfidence: Double
    let agreementRatio: Double
    let signalQuality: String
    let reasons: [String]
    let timeframes: [CumulativeSignalTimeframe]

    enum CodingKeys: String, CodingKey {
        case symbol
        case generatedAt = "generated_at"
        case marketMode = "market_mode"
        case strategyMode = "strategy_mode"
        case aggregateDirection = "aggregate_direction"
        case aggregateConfidence = "aggregate_confidence"
        case agreementRatio = "agreement_ratio"
        case signalQuality = "signal_quality"
        case reasons
        case timeframes
    }
}

struct CumulativeSignalTimeframe: Decodable, Identifiable {
    let timeframe: String
    let marketMode: String
    let direction: Direction
    let confidence: Double
    let signalQuality: String
    let atrState: String
    let breakoutFailureRate: Double
    let volumeRatio: Double
    let pattern: String

    var id: String { timeframe }

    enum CodingKeys: String, CodingKey {
        case timeframe
        case marketMode = "market_mode"
        case direction
        case confidence
        case signalQuality = "signal_quality"
        case atrState = "atr_state"
        case breakoutFailureRate = "breakout_failure_rate"
        case volumeRatio = "volume_ratio"
        case pattern
    }
}

struct MarketBehaviorResponse: Decodable {
    let regime: String
    let bias: String
    let mode: String
    let biasStrength: Double?
    let breakdown: MarketBehaviorBreakdown?
    let error: String?
}

struct MarketBehaviorBreakdown: Decodable {
    let regime1h: MarketBehaviorLeg?
    let regime4h: MarketBehaviorLeg?
    let regime12h: MarketBehaviorLeg?

    enum CodingKeys: String, CodingKey {
        case regime1h = "regime_1h"
        case regime4h = "regime_4h"
        case regime12h = "regime_12h"
    }
}

struct MarketBehaviorLeg: Decodable {
    let direction: Direction?
    let strength: String
}

struct LiquidationMapResponse: Decodable {
    let symbol: String
    let currentPrice: Double
    let dominantPull: Direction
    let confidence: Double
    let source: String
    let quality: LiquidationDataQuality
    let eventsSummary: LiquidationEventsSummary
    let marketMetrics: LiquidationMarketMetrics
    let clustersAbove: [LiquidationCluster]
    let clustersBelow: [LiquidationCluster]

    enum CodingKeys: String, CodingKey {
        case symbol
        case currentPrice = "current_price"
        case dominantPull = "dominant_pull"
        case confidence
        case source
        case quality
        case eventsSummary = "events_summary"
        case marketMetrics = "market_metrics"
        case clustersAbove = "clusters_above"
        case clustersBelow = "clusters_below"
    }
}

struct LiquidationDataQuality: Decodable {
    let estimateWeight: Double
    let eventWeight: Double
    let degradedMode: Bool
    let degradedReason: String?
    let notes: [String]

    enum CodingKeys: String, CodingKey {
        case estimateWeight = "estimate_weight"
        case eventWeight = "event_weight"
        case degradedMode = "degraded_mode"
        case degradedReason = "degraded_reason"
        case notes
    }
}

struct LiquidationEventsSummary: Decodable {
    let totalEvents: Int
    let totalNotional: Double

    enum CodingKeys: String, CodingKey {
        case totalEvents = "total_events"
        case totalNotional = "total_notional"
    }
}

struct LiquidationMarketMetrics: Decodable {
    let basisBps: Double
    let lastFundingRateBps: Double
    let openInterestChangePct: Double
    let takerBuySellRatio: Double
    let globalLongShortRatio: Double
    let orderBookImbalance: Double
    let realizedVolatilityBps: Double

    enum CodingKeys: String, CodingKey {
        case basisBps = "basis_bps"
        case lastFundingRateBps = "last_funding_rate_bps"
        case openInterestChangePct = "open_interest_change_pct"
        case takerBuySellRatio = "taker_buy_sell_ratio"
        case globalLongShortRatio = "global_long_short_ratio"
        case orderBookImbalance = "order_book_imbalance"
        case realizedVolatilityBps = "realized_volatility_bps"
    }
}

struct LiquidationCluster: Decodable, Identifiable {
    let side: Direction
    let centerPrice: Double
    let totalStrength: Double
    let contributingLevels: Int

    var id: String { "\(side.rawValue)-\(centerPrice)" }

    enum CodingKeys: String, CodingKey {
        case side
        case centerPrice = "center_price"
        case totalStrength = "total_strength"
        case contributingLevels = "contributing_levels"
    }
}

struct MarketSymbolsResponse: Decodable {
    let symbols: [MarketSymbol]
}

struct MarketSymbol: Decodable, Identifiable, Hashable {
    let symbol: String
    let baseAsset: String
    let quoteAsset: String
    let status: String

    var id: String { symbol }

    enum CodingKeys: String, CodingKey {
        case symbol
        case baseAsset = "base_asset"
        case quoteAsset = "quote_asset"
        case status
    }
}

struct DemoBotConfigStatus: Decodable {
    let configured: Bool
    let configPath: String
    let configSource: String
    let baseURL: String
    let category: String
    let accountType: String
    let defaultSymbol: String
    let aiEnabled: Bool
    let aiConfigured: Bool
    let aiModel: String?
    let aiMessage: String
    let message: String

    enum CodingKeys: String, CodingKey {
        case configured
        case configPath = "config_path"
        case configSource = "config_source"
        case baseURL = "base_url"
        case category
        case accountType = "account_type"
        case defaultSymbol = "default_symbol"
        case aiEnabled = "ai_enabled"
        case aiConfigured = "ai_configured"
        case aiModel = "ai_model"
        case aiMessage = "ai_message"
        case message
    }
}

struct DemoBotStatus: Decodable {
    let sessionID: String?
    let symbol: String
    let running: Bool
    let exchange: String
    let environment: String
    let pollIntervalSeconds: Int
    let mode: String
    let leverage: Double
    let riskPerTradePct: Double
    let maxMarginFraction: Double
    let cooldownSeconds: Int
    let accountEquity: Double
    let walletBalance: Double
    let availableBalance: Double
    let totalTrades: Int
    let aiEnabled: Bool
    let aiModel: String?
    let aiLastDecision: AITradeDecision?
    let openPosition: DemoBotPosition?
    let lastSignal: SignalResult?
    let lastAction: String
    let lastError: String?
    let lastUpdatedAt: Int?

    enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case symbol
        case running
        case exchange
        case environment
        case pollIntervalSeconds = "poll_interval_seconds"
        case mode
        case leverage
        case riskPerTradePct = "risk_per_trade_pct"
        case maxMarginFraction = "max_margin_fraction"
        case cooldownSeconds = "cooldown_seconds"
        case accountEquity = "account_equity"
        case walletBalance = "wallet_balance"
        case availableBalance = "available_balance"
        case totalTrades = "total_trades"
        case aiEnabled = "ai_enabled"
        case aiModel = "ai_model"
        case aiLastDecision = "ai_last_decision"
        case openPosition = "open_position"
        case lastSignal = "last_signal"
        case lastAction = "last_action"
        case lastError = "last_error"
        case lastUpdatedAt = "last_updated_at"
    }
}

struct AITradeDecision: Decodable {
    let enabled: Bool
    let model: String?
    let reviewType: String
    let entryVerdict: String
    let sizeMultiplier: Double
    let confidenceAdjustment: Double
    let exitAction: String
    let reason: String

    enum CodingKeys: String, CodingKey {
        case enabled
        case model
        case reviewType = "review_type"
        case entryVerdict = "entry_verdict"
        case sizeMultiplier = "size_multiplier"
        case confidenceAdjustment = "confidence_adjustment"
        case exitAction = "exit_action"
        case reason
    }
}

struct DemoBotPosition: Decodable {
    let symbol: String
    let side: Direction
    let size: Double
    let entryPrice: Double
    let markPrice: Double
    let leverage: Double
    let takeProfit: Double?
    let stopLoss: Double?
    let liquidationPrice: Double?
    let liquidationBufferPct: Double?
    let positionValue: Double
    let unrealizedPnL: Double
    let unrealizedROIPct: Double

    enum CodingKeys: String, CodingKey {
        case symbol
        case side
        case size
        case entryPrice = "entry_price"
        case markPrice = "mark_price"
        case leverage
        case takeProfit = "take_profit"
        case stopLoss = "stop_loss"
        case liquidationPrice = "liquidation_price"
        case liquidationBufferPct = "liquidation_buffer_pct"
        case positionValue = "position_value"
        case unrealizedPnL = "unrealized_pnl"
        case unrealizedROIPct = "unrealized_roi_pct"
    }
}

struct DemoBotPerformanceResponse: Decodable {
    let symbol: String
    let exchange: String
    let environment: String
    let lookbackTrades: Int
    let summary: DemoBotPerformanceSummary
    let trades: [BotTrade]

    enum CodingKeys: String, CodingKey {
        case symbol
        case exchange
        case environment
        case lookbackTrades = "lookback_trades"
        case summary
        case trades
    }
}

struct DemoBotPerformanceSummary: Decodable {
    let totalTrades: Int
    let winRate: Double
    let netPnL: Double
    let profitFactor: Double?
    let avgROIPct: Double
    let bestTradePnL: Double
    let worstTradePnL: Double
    let totalFees: Double
    let totalFunding: Double
    let lastTradeAt: Int?

    enum CodingKeys: String, CodingKey {
        case totalTrades = "total_trades"
        case winRate = "win_rate"
        case netPnL = "net_pnl"
        case profitFactor = "profit_factor"
        case avgROIPct = "avg_roi_pct"
        case bestTradePnL = "best_trade_pnl"
        case worstTradePnL = "worst_trade_pnl"
        case totalFees = "total_fees"
        case totalFunding = "total_funding"
        case lastTradeAt = "last_trade_at"
    }
}

struct BotTrade: Decodable, Identifiable {
    let runID: String?
    let side: Direction
    let entryTime: Int
    let exitTime: Int
    let entryPrice: Double
    let exitPrice: Double
    let qty: Double
    let roiPct: Double
    let netPnL: Double
    let entryReason: String
    let exitReason: String

    var id: String { "\(entryTime)-\(exitTime)-\(side.rawValue)" }

    enum CodingKeys: String, CodingKey {
        case runID = "run_id"
        case side
        case entryTime = "entry_time"
        case exitTime = "exit_time"
        case entryPrice = "entry_price"
        case exitPrice = "exit_price"
        case qty
        case roiPct = "roi_pct"
        case netPnL = "net_pnl"
        case entryReason = "entry_reason"
        case exitReason = "exit_reason"
    }
}

struct HealthResponse: Decodable {
    let status: String
}

struct BotStartRequest {
    var symbol: String
    var pollIntervalSeconds: Int
    var leverage: Double
    var riskPerTradePct: Double
    var maxMarginFraction: Double
    var cooldownSeconds: Int
    var mode: String
}
