# Binance Liquidity Signal Engine

Analysis-only signal engine for Binance USDT-M futures that emits:
- Direction: `LONG`, `SHORT`, or `FLAT`
- Confidence: `0.0 - 1.0`
- TP and SL around current price
- Signal metadata: horizon, TTL, model version, and feature version

Default market: `BTCUSDT`.

## What It Uses
- Binance futures depth (`/fapi/v1/depth`) for liquidity imbalance
- Binance futures recent trades (`/fapi/v1/trades`) for aggressive flow proxy
- Binance futures kline data (`/fapi/v1/klines`) for short-window volatility
- Binance futures mark price (`/fapi/v1/premiumIndex`) for current price
- Binance open interest, funding, basis, and long/short ratios for positioning context
- Multi-timeframe regime analysis (`1h`, `4h`, `12h`) for directional confirmation
- Liquidation map overlay as a second-order confirmation and veto layer

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

## Fresh Clone Setup

If you clone this repo onto a new machine and want the same local runtime data, use this order:

1. Create the virtual environment and install the package:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

2. Start the API once so the local SQLite store and runtime folders are created automatically:

```bash
uvicorn liquidity_signal.api.app:app --reload
```

3. Generate fresh liquidation history and training snapshots by using the API or CLI against live Binance data. The store is rebuilt locally under `runtime/` the first time you run the engine.

4. If you want the training datasets that were used for LoRA work, export them again from the rebuilt store:

```bash
liquidity-signal dataset-summary BTCUSDT
liquidity-signal export-lora --symbol BTCUSDT --horizon-minutes 15 --limit 2000 --balance-mode undersample_majority
liquidity-signal prepare-mlx-lora --horizon-minutes 5
liquidity-signal prepare-mlx-lora --horizon-minutes 15
```

5. If you use the Bybit demo bot, copy the local config templates before starting it:

```bash
cp runtime/bybit_demo_config.example.json runtime/bybit_demo_config.json
cp runtime/ai_supervisor_config.example.json runtime/ai_supervisor_config.json
```

6. For the Docker setup, copy the environment template and bring the stack up:

```bash
cp .env.docker.example .env.docker
docker compose up -d --build
```

What gets recreated locally:
- `runtime/liquidation_history.db` for liquidation events, snapshots, backtests, and training tables
- `runtime/lora_exports/` for ChatML and prompt-completion exports
- `runtime/mlx_lora_data/` for MLX train/validation/test splits
- `runtime/mlx_lora_runs/` for adapter checkpoints
- `runtime/training_backfill.db` for historical backfill captures

What is not automatically restored from git:
- private demo credentials
- private OpenAI keys
- your deployed `.env.docker`

If you want a fully populated local workspace, run the backfill and export commands after the first API start. That repopulates the runtime data using live market history instead of relying on checked-in artifacts.

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

The signal payload now includes:
- `horizon`, `decision_ts`, `expires_at`
- `entry_assumption`, `model_version`, `feature_version`
- `signal_quality` alongside `direction`, `confidence`, `tp`, `sl`, and `reasons`

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

## Bybit Demo Bot

The repo now includes a separate Bybit demo execution bot that places real demo orders visible in the Bybit app/web demo account.

Paste credentials into the local config file:

```bash
cp runtime/bybit_demo_config.example.json runtime/bybit_demo_config.json
```

Edit `runtime/bybit_demo_config.json` and paste:

```json
{
  "api_key": "your_demo_key",
  "api_secret": "your_demo_secret"
}
```

Optional AI supervisor config:

```bash
cp runtime/ai_supervisor_config.example.json runtime/ai_supervisor_config.json
```

Edit `runtime/ai_supervisor_config.json` and paste:

```json
{
  "enabled": true,
  "api_key": "your_openai_api_key",
  "model": "gpt-4o-mini"
}
```

You can still override with env vars if you prefer:

```bash
export BYBIT_DEMO_API_KEY="your_demo_key"
export BYBIT_DEMO_API_SECRET="your_demo_secret"
```

Optional env vars:

```bash
export BYBIT_DEMO_BASE_URL="https://api-demo.bybit.com"
export BYBIT_DEMO_CATEGORY="linear"
export BYBIT_DEMO_ACCOUNT_TYPE="UNIFIED"
```

Start the Bybit demo bot:

```bash
curl -X POST 'http://127.0.0.1:8000/bot/demo/bybit/start?symbol=BTCUSDT&poll_interval_seconds=30&leverage=2&risk_per_trade_pct=0.01&max_margin_fraction=0.35&cooldown_seconds=180&mode=BALANCED'
```

Check bot status:

```bash
curl 'http://127.0.0.1:8000/bot/demo/bybit/status?symbol=BTCUSDT'
```

Check whether credentials were detected:

```bash
curl 'http://127.0.0.1:8000/bot/demo/bybit/config'
```

Stop the bot:

```bash
curl -X POST 'http://127.0.0.1:8000/bot/demo/bybit/stop?close_position=true'
```

Execution notes:
- Signals still come from the local multi-factor engine.
- Entries require stronger alignment than the legacy paper simulator: base signal quality, cumulative MTF confirmation, market-bias checks, and risk-based sizing from stop distance.
- Orders are submitted to Bybit demo with exchange-side stops, laddered profit targets, and regime-aware exit management, so fills and open positions should be visible in the actual Bybit demo app.
- An optional OpenAI supervisor can validate entries, reduce size, tighten stops, take partials, or request an exit without bypassing the hard deterministic risk limits.
- The dashboard now has a dedicated `Bybit Demo Bot` section that reads config status, starts/stops the bot, shows live exchange-position state, and shows the latest AI decision when enabled.

Dashboard note:
- Bot test starts at 1000 USD for the first run.
- Each next run starts from the previous run final capital.
- A run can be stopped manually using the Stop button, or auto-stops after `max_trades` in that run.

## iOS App

There is now a dedicated SwiftUI iOS client in [ios/AbyssIntuitionIOS](/Users/ankitbhardwaj/Documents/AbyssIntuition/ios/AbyssIntuitionIOS).

It includes:
- unified signal view using the single `/signal` API
- market behavior and liquidation summary
- Bybit demo bot config, status, controls, and recent performance
- local backend URL settings for simulator or physical iPhone

Generate and open the project:

```bash
cd ios/AbyssIntuitionIOS
xcodegen generate
open AbyssIntuitionIOS.xcodeproj
```

Build from terminal:

```bash
cd ios/AbyssIntuitionIOS
xcodebuild -project AbyssIntuitionIOS.xcodeproj -scheme AbyssIntuitionIOS -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO build
```

Backend URL notes:
- iOS Simulator: `http://127.0.0.1:8000`
- Physical iPhone on the same network: `http://<your-mac-lan-ip>:8000`

## Docker Deployment

You can keep the backend in Python and expose it safely from Docker. The repo now includes:
- [Dockerfile](/Users/ankitbhardwaj/Documents/AbyssIntuition/Dockerfile)
- [docker-compose.yml](/Users/ankitbhardwaj/Documents/AbyssIntuition/docker-compose.yml)
- [deploy/Caddyfile](/Users/ankitbhardwaj/Documents/AbyssIntuition/deploy/Caddyfile)
- [.env.docker.example](/Users/ankitbhardwaj/Documents/AbyssIntuition/.env.docker.example)

Quick start:

```bash
cp .env.docker.example .env.docker
docker compose up -d --build
```

What this gives you:
- Python API in Docker on port `8000`
- Caddy reverse proxy on ports `80` and `443`
- automatic HTTPS when `API_DOMAIN` is a real domain pointing at your server
- persisted runtime data from `./runtime`

If your iPhone and backend are on different networks:
- it still works as long as the API is reachable publicly
- without a domain, the iPhone can use `http://<public-ip>:8000`
- with a domain, use `https://<your-domain>` and let Caddy handle TLS

Optional token auth:
- set `API_ACCESS_TOKEN` in `.env.docker`
- the backend will then require `Authorization: Bearer <token>` or `X-API-Token`
- the iOS app now has a field for that token in Settings

Recommended production setup:
1. Put this on a VPS or always-on machine.
2. Point `API_DOMAIN` to that server.
3. Open `80/443`.
4. Set a long random `API_ACCESS_TOKEN`.
5. Keep `runtime/bybit_demo_config.json` only on the server, not in git.

Detailed internet-facing setup is documented in [deploy/INTERNET_SETUP.md](/Users/ankitbhardwaj/Documents/AbyssIntuition/deploy/INTERNET_SETUP.md).

## Notes
- This project can place Bybit demo orders when the Bybit demo bot is enabled; it does not place live production orders.
- Signals are multi-factor heuristics over liquidity, trend, positioning, and liquidation structure; they are not financial advice.
- Liquidation map output is an estimate built from Binance open interest and price/volatility behavior, not an exchange liquidation feed.
