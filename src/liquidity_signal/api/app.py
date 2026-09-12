from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
import secrets
import sys
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from liquidity_signal.ai.deployment import deployment_status

from liquidity_signal.models import (
    BotBacktestResponse,
    CandleResponse,
    BotTrade,
    DemoBotConfigStatus,
    DemoBotPerformanceResponse,
    DemoBotStatus,
    HistoricalTrainingBackfillBatchResponse,
    HistoricalTrainingBackfillResponse,
    LiquidationEventPoint,
    LiquidationMapAdvancedResponse,
    LiquidationReplaySnapshot,
    LiquidationTileResponse,
    LoraTrainingExportResponse,
    MarketSymbolsResponse,
    SignalApiResponse,
    TrainingDatasetResponse,
)
from liquidity_signal.service.bybit_demo_bot import BybitDemoBot
from liquidity_signal.service.engine import SignalEngine


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if "pytest" not in sys.modules:
        engine.start_liquidation_watchlist(["BTCUSDT", "ETHUSDT", "SOLUSDT", "NEARUSDT", "PEPEUSDT", "XRPUSDT"], interval_seconds=45)
    yield
    bybit_demo_bot.close()
    engine.close()


app = FastAPI(title="Liquidity Signal API", version="0.1.0", lifespan=lifespan)
engine = SignalEngine()
bybit_demo_bot = BybitDemoBot(engine)
api_dir = Path(__file__).resolve().parent
static_dir = api_dir / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")
API_ACCESS_TOKEN = os.getenv("API_ACCESS_TOKEN", "").strip()


def _is_public_path(path: str) -> bool:
    if path in {"/", "/health", "/docs", "/redoc", "/openapi.json"}:
        return True
    return path.startswith("/static/")


@app.middleware("http")
async def require_api_token(request, call_next):
    if not API_ACCESS_TOKEN or _is_public_path(request.url.path):
        return await call_next(request)

    candidate = request.headers.get("x-api-token", "").strip()
    authorization = request.headers.get("authorization", "").strip()
    if not candidate and authorization.lower().startswith("bearer "):
        candidate = authorization[7:].strip()

    if not candidate or not secrets.compare_digest(candidate, API_ACCESS_TOKEN):
        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized. Provide a valid API token."},
        )

    return await call_next(request)


@app.get("/")
def home() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/model/deployment-status")
def model_deployment_status() -> dict[str, Any]:
    """Expose model readiness; missing or malformed manifests fail closed."""
    return deployment_status()


@app.get("/signal", response_model=SignalApiResponse)
def signal(symbol: str = Query(default="BTCUSDT")) -> SignalApiResponse:
    normalized = symbol.upper()
    explain = engine.generate_signal_explain(normalized)
    cumulative = engine.generate_cumulative_signal(normalized)
    return SignalApiResponse(
        symbol=normalized,
        signal=cumulative.bot_signal,
        explain=explain,
        cumulative=cumulative,
    )


@app.get("/liquidation-map", response_model=LiquidationMapAdvancedResponse)
def liquidation_map(
    symbol: str = Query(default="BTCUSDT"),
    include_events: bool = Query(default=True),
    event_limit: int = Query(default=50, ge=1, le=200),
    range_pct: float = Query(default=10.0, ge=2.0, le=25.0),
    resolution: int = Query(default=48, ge=16, le=96),
    history_points: int = Query(default=72, ge=5, le=180),
    candle_interval: str = Query(default="5m"),
    candle_limit: int = Query(default=144, ge=20, le=500),
) -> LiquidationMapAdvancedResponse:
    return engine.generate_liquidation_map_advanced(
        symbol=symbol.upper(),
        include_events=include_events,
        event_limit=event_limit,
        range_pct=range_pct,
        resolution=resolution,
        history_points=history_points,
        candle_interval=candle_interval,
        candle_limit=candle_limit,
    )


@app.get("/liquidation/events", response_model=list[LiquidationEventPoint])
def liquidation_events(
    symbol: str = Query(default="BTCUSDT"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[LiquidationEventPoint]:
    return engine.generate_liquidation_events(symbol.upper(), limit=limit)


@app.get("/liquidation/tiles", response_model=LiquidationTileResponse | None)
def liquidation_tiles(
    symbol: str = Query(default="BTCUSDT"),
    resolution: int = Query(default=48, ge=16, le=96),
    range_pct: float = Query(default=12.0, ge=2.0, le=25.0),
) -> LiquidationTileResponse | None:
    return engine.generate_liquidation_tile(symbol.upper(), resolution=resolution, range_pct=range_pct)


@app.get("/liquidation/replay", response_model=list[LiquidationReplaySnapshot])
def liquidation_replay(
    symbol: str = Query(default="BTCUSDT"),
    limit: int = Query(default=20, ge=1, le=200),
) -> list[LiquidationReplaySnapshot]:
    return engine.replay_liquidation_map(symbol.upper(), limit=limit)


@app.get("/market/candles", response_model=CandleResponse)
def market_candles(
    symbol: str = Query(default="BTCUSDT"),
    interval: str = Query(default="1m"),
    limit: int = Query(default=120, ge=20, le=500),
) -> CandleResponse:
    return engine.generate_candles(symbol.upper(), interval=interval, limit=limit)


@app.get("/market/symbols", response_model=MarketSymbolsResponse)
def market_symbols(
    quote_asset: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=6, ge=1, le=50),
) -> MarketSymbolsResponse:
    return engine.list_market_symbols(
        quote_asset=quote_asset,
        search=search,
        limit=limit,
    )


@app.get("/market/behavior")
def market_behavior(symbol: str = Query(default="BTCUSDT")) -> dict[str, Any]:
    """Analyze market behavior: regime, trend/range, bearish/bullish."""
    return engine.get_market_behavior(symbol.upper())


@app.get("/dataset/training", response_model=TrainingDatasetResponse)
def training_dataset(
    symbol: str = Query(default="BTCUSDT"),
    limit: int = Query(default=100, ge=1, le=500),
    resolved_only: bool = Query(default=False),
) -> TrainingDatasetResponse:
    return engine.load_training_dataset(symbol.upper(), limit=limit, resolved_only=resolved_only)


@app.get("/dataset/training/lora", response_model=LoraTrainingExportResponse)
def training_lora_dataset(
    symbol: str = Query(default="BTCUSDT"),
    horizon_minutes: int = Query(default=15, ge=1, le=240),
    limit: int = Query(default=1000, ge=1, le=5000),
    decision_action: str | None = Query(default=None),
    balance_mode: str = Query(default="none"),
) -> LoraTrainingExportResponse:
    return engine.export_lora_training_dataset(
        symbol.upper(),
        horizon_minutes=horizon_minutes,
        limit=limit,
        decision_action=decision_action,
        balance_mode=balance_mode,
    )


@app.post("/dataset/training/backfill", response_model=HistoricalTrainingBackfillResponse)
def training_backfill(
    symbol: str = Query(default="BTCUSDT"),
    lookback_hours: int = Query(default=24, ge=1, le=168),
    step_minutes: int = Query(default=5, ge=1, le=60),
    max_samples: int = Query(default=400, ge=10, le=5000),
    include_stored_liquidation: bool = Query(default=True),
) -> HistoricalTrainingBackfillResponse:
    return engine.backfill_historical_training(
        symbol.upper(),
        lookback_hours=lookback_hours,
        step_minutes=step_minutes,
        max_samples=max_samples,
        include_stored_liquidation=include_stored_liquidation,
    )


@app.post("/dataset/training/backfill/batch", response_model=HistoricalTrainingBackfillBatchResponse)
def training_backfill_batch(
    symbols: str = Query(default="BTCUSDT,ETHUSDT,SOLUSDT"),
    lookback_hours: int = Query(default=24, ge=1, le=168),
    step_minutes: int = Query(default=5, ge=1, le=60),
    max_samples_per_symbol: int = Query(default=400, ge=10, le=5000),
    include_stored_liquidation: bool = Query(default=True),
) -> HistoricalTrainingBackfillBatchResponse:
    symbol_list = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    return engine.backfill_historical_training_batch(
        symbol_list,
        lookback_hours=lookback_hours,
        step_minutes=step_minutes,
        max_samples_per_symbol=max_samples_per_symbol,
        include_stored_liquidation=include_stored_liquidation,
    )


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


@app.get("/bot/backtest/trades", response_model=list[BotTrade])
def bot_backtest_trades(
    symbol: str = Query(default="BTCUSDT"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[BotTrade]:
    return [
        BotTrade.model_validate(row)
        for row in engine.liquidation_store.load_recent_backtest_trades(symbol.upper(), limit=limit)
    ]


@app.post("/bot/demo/bybit/start", response_model=DemoBotStatus)
def start_bybit_demo_bot(
    symbol: str = Query(default="BTCUSDT"),
    poll_interval_seconds: int = Query(default=5, ge=5, le=600),
    leverage: float = Query(default=2.0, ge=1.0, le=25.0),
    risk_per_trade_pct: float = Query(default=0.01, ge=0.001, le=0.05),
    max_margin_fraction: float = Query(default=0.35, ge=0.05, le=1.0),
    cooldown_seconds: int = Query(default=30, ge=0, le=7200),
    mode: str = Query(default="BALANCED"),
) -> DemoBotStatus:
    return bybit_demo_bot.start(
        symbol=symbol.upper(),
        poll_interval_seconds=poll_interval_seconds,
        leverage=leverage,
        risk_per_trade_pct=risk_per_trade_pct,
        max_margin_fraction=max_margin_fraction,
        cooldown_seconds=cooldown_seconds,
        mode=mode,
    )


@app.get("/bot/demo/bybit/config", response_model=DemoBotConfigStatus)
def bybit_demo_bot_config() -> DemoBotConfigStatus:
    return bybit_demo_bot.get_config_status()


@app.get("/bot/demo/bybit/performance", response_model=DemoBotPerformanceResponse)
def bybit_demo_bot_performance(
    symbol: str = Query(default="BTCUSDT"),
    limit: int = Query(default=50, ge=1, le=200),
) -> DemoBotPerformanceResponse:
    return bybit_demo_bot.get_performance(symbol.upper(), limit=limit)


@app.post("/bot/demo/bybit/stop", response_model=DemoBotStatus)
def stop_bybit_demo_bot(close_position: bool = Query(default=True)) -> DemoBotStatus:
    return bybit_demo_bot.stop(close_position=close_position)


@app.get("/bot/demo/bybit/status", response_model=DemoBotStatus)
def bybit_demo_bot_status(symbol: str = Query(default="BTCUSDT")) -> DemoBotStatus:
    return bybit_demo_bot.get_status(symbol.upper())
