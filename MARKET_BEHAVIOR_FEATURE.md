# Market Behavior Analysis Feature

## Overview

The Market Behavior Analysis feature provides comprehensive market structure analysis integrated into the Liquidity Signal dashboard. It analyzes price action patterns, trend structures, and market regimes using multi-timeframe analysis to identify key trading opportunities and market conditions.

## Components

### 1. Data Models

**File:** `src/liquidity_signal/models.py`

New `MarketBehavior` pydantic model:
```python
class MarketBehavior(BaseModel):
    symbol: str
    behavior_type: str  # 'uptrend', 'downtrend', 'ranging', 'breakout', 'volatility_spike', 'consolidation'
    strength: str  # 'Weak', 'Moderate', 'Strong'
    confidence: float
    description: str
    trading_implication: str
    key_levels: List[float]
    duration_candles: Optional[int]
    supporting_stats: Dict[str, float]
    timestamp: int
```

### 2. Behavior Analyzer Service

**File:** `src/liquidity_signal/service/market_behavior.py`

`MarketBehaviorAnalyzer` class provides:
- **Trend Detection:** Identifies uptrends (higher highs/lows) and downtrends (lower highs/lows)
- **Volatility Analysis:** Detects volatility spikes above threshold multiples
- **Range Detection:** Identifies price oscillation patterns
- **Consolidation Detection:** Recognizes squeeze patterns before breakouts

Key methods:
- `analyze()` - Main analysis method that returns detected patterns
- `_detect_trend()` - Analyzes consecutive candle structures
- `_detect_volatility()` - Compares current vs average range
- `_detect_range()` - Finds oscillating price behavior
- `_detect_consolidation()` - Identifies tight ranges

### 3. API Integration

**File:** `src/liquidity_signal/api/app.py`

Endpoint: `GET /market/behavior`
```
Query Parameters:
  - symbol: Trading pair (default: BTCUSDT)

Response:
{
  "regime": "MIXED_DOWN | STRONG_UPTREND | RANGING | etc",
  "bias": "BULLISH | BEARISH | NEUTRAL",
  "mode": "TRENDING | RANGING",
  "bias_strength": 0.0-1.0,
  "breakdown": {
    "regime_1h": {...},
    "regime_4h": {...},
    "regime_12h": {...}
  }
}
```

The endpoint leverages the existing `MTFRegimeAnalyzer` for multi-timeframe analysis across 1H, 4H, and 12H timeframes.

### 4. Frontend UI Components

**File:** `src/liquidity_signal/api/static/index.html`

#### Modal Dialog
- **ID:** `behaviorModal`
- **Trigger:** Click on "Market Behavior" card in the dashboard
- **Features:**
  - Displays detailed market structure analysis
  - Shows regime, bias, and mode information
  - Provides trading recommendations based on market conditions
  - Includes multi-timeframe breakdown visualization

#### CSS Styling
New styles added for:
- `.modal` - Container with semi-transparent overlay
- `.modal-content` - Dark-themed content box
- `.modal-header` - Title and close button
- `.behavior-section` - Content sections with borders
- `.behavior-details` - Detailed information display
- `.behavior-stat` - Key-value stat display

#### JavaScript Functions

```javascript
// Fetch market behavior data during dashboard refresh
async function loadDashboard() {
  // ... existing code ...
  const behaviorRes = await fetch(`/market/behavior?symbol=${symbol}`);
  window.marketBehaviorData = behaviorRes.ok ? await behaviorRes.json() : null;
}

// Display market behavior modal
function showMarketBehaviorModal() {
  // Fetches stored behavior data
  // Formats and displays in modal dialog
  // Includes regime, bias, strength, and recommendations
}

// Close modal when clicking outside
document.getElementById("behaviorModal").addEventListener("click", (e) => {
  if (e.target === behaviorModal) closeBehaviorModal();
});
```

## Usage

### For Traders
1. Open the Liquidity Signal dashboard
2. The dashboard automatically fetches market behavior data on refresh
3. Click the "📊 Market Behavior" card to view analysis
4. Modal displays:
   - Current market regime (trending/ranging)
   - Bias direction (bullish/bearish/neutral)
   - Trend strength percentage
   - Trading recommendations specific to the regime
   - Multi-timeframe breakdown

### Example Outputs

**Bullish Regime:**
```
Regime: STRONG_UPTREND
Bias: BULLISH (78% strength)
Mode: TRENDING

Recommendations:
- Long Setup: Buy on pullbacks to support
- Watch for resistance at recent highs
- Be ready for reversals near resistance
```

**Bearish Regime:**
```
Regime: STRONG_DOWNTREND
Bias: BEARISH (82% strength)
Mode: TRENDING

Recommendations:
- Short Setup: Sell into resistance zones
- Watch for breaks below recent lows
- Be ready for reversals near support
```

**Ranging Market:**
```
Regime: RANGING
Bias: NEUTRAL
Mode: RANGING

Recommendations:
- Use mean reversion strategies
- Buy near support, sell near resistance
- Watch for breakouts that reverse momentum
```

## Technical Details

### Multi-Timeframe Analysis
Market behavior uses data across three timeframes:
- **1H:** Intraday trend confirmation
- **4H:** Session-level structure
- **12H:** Macro trend direction

Each timeframe contributes to overall regime determination and confidence scores.

### Confidence Calculation
- Based on pattern consistency across timeframes
- Adjusted by regime strength metrics
- Accounts for volatility conditions

### Key Levels
- Support/resistance zones extracted from:
  - Recent candle highs/lows
  - Identified consolidation boundaries
  - Multi-timeframe structure pivots

## Future Enhancements

1. **Pattern Recognition**
   - Head and shoulders, triangles, flags
   - Fibonacci retracement levels
   - Support/resistance cluster detection

2. **Volume Analysis**
   - Volume profile analysis
   - Confirmation of breakouts
   - Divergence detection

3. **Machine Learning**
   - Pattern classification with neural networks
   - Regime probability predictions
   - Anomaly detection for unusual setups

4. **Alert System**
   - Regime change notifications
   - Breakout alerts
   - Confluence zone alerts

5. **Historical Analysis**
   - Win rate by regime type
   - Performance stats by timeframe
   - Seasonal patterns

## Performance Considerations

- Behavior analysis runs on dashboard refresh (~30 second default interval)
- Multi-timeframe analysis caches results to minimize API calls
- Modal rendering is client-side only (no additional server load)
- Data payload is minimal (~500 bytes typical)

## Error Handling

- API failures gracefully return error messages in modal
- Missing data doesn't block dashboard refresh
- Fallback to "Analysis unavailable" if endpoint fails
- Invalid regime data handled with defensive defaults

## Integration with Other Features

### Liquidity Signal Integration
- Market behavior bias incorporated into confidence calculations
- Regime confirms/conflicts with liquidity-based signals
- Combination improves win rate on consolidated setups

### Backtesting Integration
- Backtest engine can filter trades by regime
- Future: Regime-specific performance metrics
- Strategy optimization per market condition

### AI Refinement
- Regime analysis feeds into signal refinement
- Adds confirmation signals for borderline trades
- Reduces false signals in choppy markets

## Testing

Run the API and navigate to `http://127.0.0.1:8000/`:
1. Dashboard loads and fetches market behavior
2. Click "Market Behavior" card opens modal
3. Modal displays current regime analysis
4. Close button and overlay click dismiss modal
5. Data persists across dashboard refreshes

## Files Modified/Created

### Created:
- `src/liquidity_signal/service/market_behavior.py` - Behavior analyzer
- `MARKET_BEHAVIOR_FEATURE.md` - This documentation

### Modified:
- `src/liquidity_signal/models.py` - Added MarketBehavior model
- `src/liquidity_signal/api/static/index.html` - Added modal UI and JS
- Existing `src/liquidity_signal/api/app.py` - Already has `/market/behavior` endpoint
- Existing `src/liquidity_signal/service/engine.py` - Already has get_market_behavior() method

## Quick Reference

| Component | Location | Purpose |
|-----------|----------|---------|
| Data Model | `models.py` | Define market behavior structure |
| Analyzer | `service/market_behavior.py` | Pattern analysis algorithms |
| API | `api/app.py` | HTTP endpoint `/market/behavior` |
| UI Modal | `static/index.html` | Display analysis to users |
| Styling | `static/index.html` | CSS for modal appearance |
| JS Logic | `static/index.html` | Fetch and render behavior data |

