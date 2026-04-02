# Binance Liquidity Signal Engine

Analysis-only signal engine for Binance USDT-M futures that emits:
- Direction: `LONG`, `SHORT`, or `FLAT`
- Confidence: `0.0 - 1.0`
- TP and SL around current price

Default market: `BTCUSDT`.

## What It Uses
- Binance futures depth (`/fapi/v1/depth`) for liquidity imbalance
- Binance futures recent trades (`/fapi/v1/trades`) for aggressive flow proxy
- Binance futures kline data (`/fapi/v1/klines`) for short-window volatility
- Binance futures mark price (`/fapi/v1/premiumIndex`) for current price

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

Run CLI signal:

```bash
liquidity-signal signal --symbol BTCUSDT
```

Run API:

```bash
uvicorn liquidity_signal.api.app:app --reload
```

Open web dashboard:

```bash
open http://127.0.0.1:8000/
```

Get signal:

```bash
curl 'http://127.0.0.1:8000/signal?symbol=BTCUSDT'
```

Get Binance-only estimated liquidation map:

```bash
curl 'http://127.0.0.1:8000/liquidation-map?symbol=BTCUSDT'
```

Get candles for charting:

```bash
curl 'http://127.0.0.1:8000/market/candles?symbol=BTCUSDT&interval=1m&limit=120'
```

Run bot backtest (paper trading with fees and funding, initial capital 1000 USD):

```bash
curl 'http://127.0.0.1:8000/bot/backtest?symbol=BTCUSDT&interval=1m&candle_limit=300&initial_capital=1000&fee_rate=0.0004&leverage=1&confidence_threshold=0.58&max_trades=50'
```

Toggle liquidation filter in backtest:

```bash
curl 'http://127.0.0.1:8000/bot/backtest?symbol=BTCUSDT&interval=1m&candle_limit=300&initial_capital=1000&fee_rate=0.0004&leverage=1&confidence_threshold=0.5&max_trades=50&use_liquidation_data=true&use_htf_filter=true&htf_interval=15m&htf_lookback=12&use_sentiment_data=true'
```

Adaptive default strategy notes:
- Regime-aware signal: trend mode (momentum) + range mode (mean-reversion).
- Optional liquidation confirmation filter.
- Optional higher-timeframe trend alignment (`use_htf_filter`).
- Optional sentiment confirmation using Binance taker ratio and global account long/short ratio (`use_sentiment_data`).

Dashboard note:
- Bot test starts at 1000 USD for the first run.
- Each next run starts from the previous run final capital.
- A run can be stopped manually using the Stop button, or auto-stops after `max_trades` in that run.

## Notes
- This project is analysis-only and does not place orders.
- Signals are heuristics over live liquidity and flow; they are not financial advice.
- Liquidation map output is an estimate built from Binance open interest and price/volatility behavior, not an exchange liquidation feed.
