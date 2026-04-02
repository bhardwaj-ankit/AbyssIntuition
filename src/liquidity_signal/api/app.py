from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from liquidity_signal.models import BotBacktestResponse, CandleResponse, LiquidationMapResponse, SignalExplainResult, SignalResult
from liquidity_signal.service.engine import SignalEngine

app = FastAPI(title="Liquidity Signal API", version="0.1.0")
engine = SignalEngine()
api_dir = Path(__file__).resolve().parent
static_dir = api_dir / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
def home() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/signal")
def signal(symbol: str = Query(default="BTCUSDT")) -> SignalResult:
    return engine.generate_signal(symbol.upper())


@app.get("/signal/explain", response_model=SignalExplainResult)
def signal_explain(symbol: str = Query(default="BTCUSDT")) -> SignalExplainResult:
    return engine.generate_signal_explain(symbol.upper())


@app.get("/liquidation-map", response_model=LiquidationMapResponse)
def liquidation_map(symbol: str = Query(default="BTCUSDT")) -> LiquidationMapResponse:
    return engine.generate_liquidation_map(symbol.upper())


@app.get("/market/candles", response_model=CandleResponse)
def market_candles(
    symbol: str = Query(default="BTCUSDT"),
    interval: str = Query(default="1m"),
    limit: int = Query(default=120, ge=20, le=500),
) -> CandleResponse:
    return engine.generate_candles(symbol.upper(), interval=interval, limit=limit)


@app.get("/market/behavior")
def market_behavior(symbol: str = Query(default="BTCUSDT")) -> dict[str, Any]:
    """Analyze market behavior: regime, trend/range, bearish/bullish."""
    return engine.get_market_behavior(symbol.upper())


@app.get("/bot/backtest", response_model=BotBacktestResponse)
def bot_backtest(
    symbol: str = Query(default="BTCUSDT"),
    interval: str = Query(default="1m"),
    candle_limit: int = Query(default=300, ge=120, le=500),
    initial_capital: float = Query(default=1000.0, ge=100.0),
    fee_rate: float = Query(default=0.0004, ge=0.0, le=0.005),
    leverage: float = Query(default=1.0, ge=1.0, le=20.0),
    confidence_threshold: float = Query(default=0.5, ge=0.5, le=0.95),
    max_trades: int = Query(default=50, ge=1, le=200),
    use_liquidation_data: bool = Query(default=True),
    use_htf_filter: bool = Query(default=True),
    htf_interval: str = Query(default="15m"),
    htf_lookback: int = Query(default=12, ge=5, le=120),
    use_sentiment_data: bool = Query(default=True),
    use_volume_filter: bool = Query(default=True),
) -> BotBacktestResponse:
    return engine.simulate_bot_backtest(
        symbol=symbol.upper(),
        interval=interval,
        candle_limit=candle_limit,
        initial_capital=initial_capital,
        fee_rate=fee_rate,
        leverage=leverage,
        confidence_threshold=confidence_threshold,
        max_trades=max_trades,
        use_liquidation_data=use_liquidation_data,
        use_htf_filter=use_htf_filter,
        htf_interval=htf_interval,
        htf_lookback=htf_lookback,
        use_sentiment_data=use_sentiment_data,
        use_volume_filter=use_volume_filter,
    )
