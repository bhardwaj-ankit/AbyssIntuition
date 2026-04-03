from __future__ import annotations

from datetime import datetime
from typing import Any

from liquidity_signal.config import SignalConfig
from liquidity_signal.data.binance_client import BinanceFuturesClient
from liquidity_signal.data.bybit_client import BybitPublicClient
from liquidity_signal.features.liquidation_map import build_liquidation_map_advanced, build_liquidation_map_estimate
from liquidity_signal.features.liquidity_features import build_liquidity_features
from liquidity_signal.features.mtf_regime import MTFRegimeAnalyzer, MarketRegime
from liquidity_signal.models import (
    BotBacktestResponse,
    BotTrade,
    Candle,
    CandleResponse,
    Direction,
    EquityPoint,
    LiquidationMapAdvancedResponse,
    SignalExplainResult,
    SignalResult,
)
from liquidity_signal.risk.tpsl import compute_tp_sl, compute_tp_sl_with_breakdown
from liquidity_signal.signal.ai_refiner import refine_signal_with_ai
from liquidity_signal.signal.scorer import score_features_with_breakdown


class SignalEngine:
    def __init__(
        self,
        client: BinanceFuturesClient | None = None,
        cfg: SignalConfig | None = None,
        bybit_client: BybitPublicClient | None = None,
    ) -> None:
        self.client = client or BinanceFuturesClient()
        self.cfg = cfg or SignalConfig()
        self.bybit_client = bybit_client or BybitPublicClient()

    def close(self) -> None:
        self.client.close()
        self.bybit_client.close()

    def _apply_liquidation_overlay(
        self,
        direction: Direction,
        confidence: float,
        reasons: list[str],
        liq_map: LiquidationMapAdvancedResponse,
    ) -> tuple[Direction, float, list[str]]:
        updated_reasons = list(reasons)
        liq_conf = max(0.0, min(liq_map.confidence, 1.0))

        if liq_map.quality.degraded_mode:
            updated_reasons.append("Liquidation overlay degraded: using estimate-only mode")

        if liq_map.dominant_pull == Direction.FLAT:
            updated_reasons.append("Liquidation map neutral")
            return direction, confidence, updated_reasons

        if direction == Direction.FLAT and liq_conf >= 0.60:
            new_direction = liq_map.dominant_pull
            new_confidence = max(confidence, min(0.75, 0.55 + 0.20 * liq_conf))
            updated_reasons.append(f"Liquidation map set bias to {new_direction.value}")
            return new_direction, new_confidence, updated_reasons

        if direction == liq_map.dominant_pull:
            new_confidence = min(1.0, confidence + 0.12 * liq_conf)
            updated_reasons.append("Liquidation map confirms signal direction")
        else:
            new_confidence = max(0.0, confidence - 0.15 * liq_conf)
            updated_reasons.append("Liquidation map opposes signal direction")

        if new_confidence < self.cfg.min_confidence:
            updated_reasons.append("Confidence dropped below threshold after liquidation filter")
            return Direction.FLAT, new_confidence, updated_reasons

        return direction, new_confidence, updated_reasons

    def generate_signal(self, symbol: str = "BTCUSDT") -> SignalResult:
        mark_price = self.client.get_mark_price(symbol)
        order_book = self.client.get_order_book(symbol, limit=20)
        trades = self.client.get_recent_trades(symbol, limit=100)
        klines = self.client.get_recent_klines(symbol, interval="1m", limit=20)

        features = build_liquidity_features(symbol, mark_price, order_book, trades, klines)
        direction, confidence, reasons, scoring = score_features_with_breakdown(features, self.cfg)
        ai_direction, ai_confidence, _ = refine_signal_with_ai(features, scoring, self.cfg)
        if ai_direction != Direction.FLAT:
            direction = ai_direction
            confidence = ai_confidence
            reasons = reasons + ["AI refinement active"]

        try:
            liq_map = self.generate_liquidation_map_advanced(symbol=symbol, include_events=True, event_limit=50)
            direction, confidence, reasons = self._apply_liquidation_overlay(direction, confidence, reasons, liq_map)
        except Exception:
            reasons = reasons + ["Liquidation overlay unavailable"]

        tp, sl = compute_tp_sl(direction, features.mark_price, confidence, features, self.cfg)

        return SignalResult(
            symbol=symbol,
            direction=direction,
            confidence=confidence,
            current_price=features.mark_price,
            tp=tp,
            sl=sl,
            reasons=reasons,
        )

    def generate_signal_explain(self, symbol: str = "BTCUSDT") -> SignalExplainResult:
        mark_price = self.client.get_mark_price(symbol)
        order_book = self.client.get_order_book(symbol, limit=20)
        trades = self.client.get_recent_trades(symbol, limit=100)
        klines = self.client.get_recent_klines(symbol, interval="1m", limit=20)

        features = build_liquidity_features(symbol, mark_price, order_book, trades, klines)
        direction, confidence, reasons, scoring = score_features_with_breakdown(features, self.cfg)
        ai_direction, ai_confidence, ai_refinement = refine_signal_with_ai(features, scoring, self.cfg)
        if ai_direction != Direction.FLAT:
            direction = ai_direction
            confidence = ai_confidence
            reasons = reasons + ["AI refinement active"]

        try:
            liq_map = self.generate_liquidation_map_advanced(symbol=symbol, include_events=True, event_limit=50)
            direction, confidence, reasons = self._apply_liquidation_overlay(direction, confidence, reasons, liq_map)
        except Exception:
            reasons = reasons + ["Liquidation overlay unavailable"]

        tp, sl, risk = compute_tp_sl_with_breakdown(direction, features.mark_price, confidence, features, self.cfg)

        signal = SignalResult(
            symbol=symbol,
            direction=direction,
            confidence=confidence,
            current_price=features.mark_price,
            tp=tp,
            sl=sl,
            reasons=reasons,
        )
        return SignalExplainResult(signal=signal, features=features, scoring=scoring, risk=risk, ai_refinement=ai_refinement)

    def generate_liquidation_map(self, symbol: str = "BTCUSDT") -> LiquidationMapAdvancedResponse:
        return self.generate_liquidation_map_advanced(symbol=symbol, include_events=True, event_limit=50)

    def generate_liquidation_map_advanced(
        self,
        symbol: str = "BTCUSDT",
        include_events: bool = True,
        event_limit: int = 50,
    ) -> LiquidationMapAdvancedResponse:
        mark_price = self.client.get_mark_price(symbol)
        oi_hist = self.client.get_open_interest_hist(symbol, period="5m", limit=30)
        klines = self.client.get_recent_klines(symbol, interval="1m", limit=60)
        try:
            funding_rates = self.client.get_funding_rates(symbol, limit=30)
        except Exception:
            funding_rates = []

        bybit_events: list[dict[str, Any]] | None = None
        bybit_fetched_at_ms: int | None = None
        degraded_reason: str | None = None
        if include_events:
            try:
                if hasattr(self.bybit_client, "get_recent_liquidations_with_meta"):
                    bybit_events, bybit_fetched_at_ms, degraded_reason = self.bybit_client.get_recent_liquidations_with_meta(
                        symbol=symbol, limit=event_limit
                    )
                else:
                    bybit_events = self.bybit_client.get_recent_liquidations(symbol=symbol, limit=event_limit)
                    bybit_fetched_at_ms = int(datetime.now().timestamp() * 1000)
            except Exception as exc:
                bybit_events = None
                degraded_reason = f"Bybit fetch exception: {exc}"

        return build_liquidation_map_advanced(
            symbol=symbol,
            mark_price=mark_price,
            oi_hist=oi_hist,
            klines=klines,
            funding_rates=funding_rates,
            bybit_events_raw=bybit_events,
            include_events=include_events,
            bybit_fetched_at_ms=bybit_fetched_at_ms,
            degraded_reason=degraded_reason,
        )

    def generate_candles(self, symbol: str = "BTCUSDT", interval: str = "1m", limit: int = 120) -> CandleResponse:
        rows = self.client.get_recent_klines(symbol, interval=interval, limit=limit)
        candles = [
            Candle(
                open_time=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in rows
        ]
        return CandleResponse(symbol=symbol, interval=interval, candles=candles)

    def get_market_behavior(self, symbol: str = "BTCUSDT") -> dict[str, Any]:
        """Analyze market behavior: regime, bias, strength (trend/range, bearish/bullish)."""
        try:
            mtf_analyzer = MTFRegimeAnalyzer()
            klines_1h = self.client.get_recent_klines(symbol, interval="1h", limit=100)
            klines_4h = self.client.get_recent_klines(symbol, interval="4h", limit=100)
            klines_12h = self.client.get_recent_klines(symbol, interval="12h", limit=100)
            mtf_regime, mtf_bias, breakdown = mtf_analyzer.analyze(klines_1h, klines_4h, klines_12h)

            # Determine overall bias
            if mtf_bias > 0.3:
                bias_label = "BULLISH"
            elif mtf_bias < -0.3:
                bias_label = "BEARISH"
            else:
                bias_label = "NEUTRAL"

            # Determine trend/range mode
            is_trending = mtf_regime in (
                "STRONG_UPTREND",
                "UPTREND",
                "STRONG_DOWNTREND",
                "DOWNTREND",
            )
            mode = "TRENDING" if is_trending else "RANGING"

            return {
                "regime": mtf_regime,
                "bias": bias_label,
                "mode": mode,
                "bias_strength": round(abs(mtf_bias), 2),
                "breakdown": breakdown,
            }
        except Exception as e:
            return {"error": str(e), "regime": "UNKNOWN", "bias": "NEUTRAL", "mode": "UNKNOWN"}

    def simulate_bot_backtest(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "1m",
        candle_limit: int = 300,
        initial_capital: float = 1000.0,
        fee_rate: float = 0.0004,
        leverage: float = 1.0,
        confidence_threshold: float = 0.5,
        max_trades: int = 50,
        use_liquidation_data: bool = True,
        use_htf_filter: bool = True,
        htf_interval: str = "15m",
        htf_lookback: int = 12,
        use_sentiment_data: bool = True,
        use_volume_filter: bool = True,
    ) -> BotBacktestResponse:
        rows = self.client.get_recent_klines(symbol, interval=interval, limit=candle_limit)
        if not rows:
            return BotBacktestResponse(
                symbol=symbol,
                interval=interval,
                initial_capital=initial_capital,
                final_capital=initial_capital,
                return_pct=0.0,
                total_trades=0,
                win_rate=0.0,
                fees_paid=0.0,
                funding_paid=0.0,
                max_drawdown_pct=0.0,
                trades=[],
                trade_logs=[],
                equity_curve=[],
            )

        candles = [
            Candle(
                open_time=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in rows
        ]

        htf_rows = self.client.get_recent_klines(symbol, interval=htf_interval, limit=500)
        htf_candles = [
            Candle(
                open_time=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in htf_rows
        ]

        # Fetch multi-timeframe regime (1h, 4h, 12h) for market context.
        mtf_analyzer = MTFRegimeAnalyzer()
        klines_1h = self.client.get_recent_klines(symbol, interval="1h", limit=100)
        klines_4h = self.client.get_recent_klines(symbol, interval="4h", limit=100)
        klines_12h = self.client.get_recent_klines(symbol, interval="12h", limit=100)
        mtf_regime, mtf_bias, _ = mtf_analyzer.analyze(klines_1h, klines_4h, klines_12h)

        oi_rows = sorted(
            self.client.get_open_interest_hist(symbol, period="5m", limit=500),
            key=lambda row: int(row.get("timestamp", 0)),
        )
        funding_rows = sorted(
            self.client.get_funding_rates(symbol, limit=200),
            key=lambda row: int(row.get("fundingTime", 0)),
        )
        taker_rows: list[dict[str, Any]] = []
        account_rows: list[dict[str, Any]] = []
        if use_sentiment_data:
            try:
                taker_rows = sorted(
                    self.client.get_taker_long_short_ratio(symbol, period="5m", limit=200),
                    key=lambda row: int(row.get("timestamp", 0)),
                )
            except Exception:
                taker_rows = []
            try:
                account_rows = sorted(
                    self.client.get_global_long_short_account_ratio(symbol, period="5m", limit=200),
                    key=lambda row: int(row.get("timestamp", 0)),
                )
            except Exception:
                account_rows = []

        capital = initial_capital
        fees_paid = 0.0
        funding_paid = 0.0
        trades: list[BotTrade] = []
        equity_curve: list[EquityPoint] = []
        trade_logs: list[str] = []
        funding_idx = 0
        position: dict[str, Any] | None = None
        confirm_signal = Direction.FLAT
        confirm_count = 0
        confirmation_bars = 3  # Require 3-bar confirmation to filter noise.
        min_hold_minutes = 5  # Do not close before 5 minutes (avoid scalp whipsaw).
        skip_next_entry = False  # Skip 1 entry after a loss (prevent revenge trading).

        def short_vol_bps(window_rows: list[list[Any]], mark_price: float) -> float:
            if len(window_rows) < 2 or mark_price <= 0.0:
                return 0.0
            closes = [float(row[4]) for row in window_rows]
            diffs = [abs(closes[j] - closes[j - 1]) for j in range(1, len(closes))]
            return (sum(diffs) / len(diffs) / mark_price) * 10_000

        def is_volume_spike(window_rows: list[list[Any]]) -> bool:
            """Check if recent candle has elevated volume (top 50% of recent average)."""
            if len(window_rows) < 3:
                return False
            volumes = [float(row[5]) for row in window_rows]
            avg_vol = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else 1.0
            current_vol = volumes[-1]
            return current_vol >= avg_vol * 1.5

        def ema(values: list[float], span: int) -> float:
            if not values:
                return 0.0
            alpha = 2.0 / (span + 1.0)
            result = values[0]
            for value in values[1:]:
                result = alpha * value + (1.0 - alpha) * result
            return result

        def ratio_at(rows_ratio: list[dict[str, Any]], time_ms: int, key: str) -> float:
            candidates = [row for row in rows_ratio if int(row.get("timestamp", 0)) <= time_ms]
            if not candidates:
                return 1.0
            return float(candidates[-1].get(key, 1.0))

        def sentiment_confirm(signal_side: Direction, time_ms: int, trend_regime: bool) -> bool:
            if not use_sentiment_data:
                return True
            if not taker_rows and not account_rows:
                return True

            taker_ratio = ratio_at(taker_rows, time_ms, "buySellRatio")
            account_ratio = ratio_at(account_rows, time_ms, "longShortRatio")

            if trend_regime:
                if signal_side == Direction.LONG:
                    return taker_ratio > 1.0 and account_ratio >= 0.98
                if signal_side == Direction.SHORT:
                    return taker_ratio < 1.0 and account_ratio <= 1.02
            else:
                # In range regime, avoid crowded extremes.
                if signal_side == Direction.LONG:
                    return taker_ratio < 1.08
                if signal_side == Direction.SHORT:
                    return taker_ratio > 0.92
            return True

        def htf_trend_metrics(time_ms: int) -> tuple[Direction, float]:
            if len(htf_candles) < htf_lookback + 1:
                return Direction.FLAT, 0.0

            visible = [c for c in htf_candles if c.open_time <= time_ms]
            if len(visible) < htf_lookback + 1:
                return Direction.FLAT, 0.0

            closes = [c.close for c in visible[-(htf_lookback + 1) :]]
            start = closes[0]
            end = closes[-1]
            if start <= 0.0:
                return Direction.FLAT, 0.0

            ret_bps = ((end - start) / start) * 10_000
            diffs = [abs(closes[j] - closes[j - 1]) for j in range(1, len(closes))]
            vol_bps = ((sum(diffs) / len(diffs)) / end) * 10_000 if end > 0 else 0.0
            threshold_bps = max(12.0, vol_bps * 0.8)
            strength = abs(ret_bps) / max(vol_bps, 6.0)
            if ret_bps >= threshold_bps:
                return Direction.LONG, strength
            if ret_bps <= -threshold_bps:
                return Direction.SHORT, strength
            return Direction.FLAT, strength

        def adaptive_signal(window_rows: list[list[Any]], time_ms: int) -> tuple[Direction, float, bool, Direction]:
            if len(window_rows) < 30:
                return Direction.FLAT, 0.0, False, Direction.FLAT

            closes = [float(row[4]) for row in window_rows]
            last = closes[-1]
            ltf_vol = max(short_vol_bps(window_rows, last), 4.0)
            htf_dir, htf_strength = htf_trend_metrics(time_ms)
            # Only enter trend mode if HTF strength is >1.2 (strong sustained moves, not weak noise).
            trend_regime = htf_dir != Direction.FLAT and htf_strength >= 1.2

            fast = ema(closes[-20:], 8)
            slow = ema(closes[-30:], 21)
            look = min(8, len(closes) - 1)
            mom_bps = ((last - closes[-look]) / closes[-look]) * 10_000 if closes[-look] > 0 else 0.0
            ma = sum(closes[-20:]) / 20.0
            variance = sum((value - ma) ** 2 for value in closes[-20:]) / 20.0
            std = variance ** 0.5
            zscore = (last - ma) / std if std > 0 else 0.0

            if trend_regime:
                separation = ((fast - slow) / last) * 10_000 if last > 0 else 0.0
                trend_score = max(abs(separation), abs(mom_bps))
                # For trend trades, require both strong separation AND strong momentum (not just one).
                confidence = min(trend_score / max(ltf_vol * 1.6, 10.0), 1.0)
                if confidence >= confidence_threshold:
                    # Require alignment: EMA separation AND momentum in same direction.
                    # Also require price near the fast EMA (to avoid chasing extremes).
                    price_to_fast_bps = ((last - fast) / fast) * 10_000 if fast > 0 else 100.0
                    at_fast_ma = abs(price_to_fast_bps) <= 4.0  # Price within 4bps of fast EMA.
                    # Only SHORT in trend, disable LONG (pullback/scalp trading is short-biased).
                    if separation < -8.0 and mom_bps < -6.0 and at_fast_ma:
                        return Direction.SHORT, min(confidence, 0.85), True, htf_dir
                return Direction.FLAT, confidence * 0.6, True, htf_dir

            # Range regime: mean-reversion around local mean.
            confidence = min(abs(zscore) / 2.5, 1.0)
            if confidence >= confidence_threshold:
                # Only SHORT in range mode, disable LONG (scalp trading short-biased).
                if zscore >= 1.1:
                    return Direction.SHORT, confidence, False, htf_dir
            return Direction.FLAT, confidence, False, htf_dir

        def close_position(price: float, close_time: int, reason: str) -> None:
            nonlocal capital, fees_paid, position, skip_next_entry
            if position is None:
                return

            side_sign = 1.0 if position["side"] == Direction.LONG else -1.0
            gross = position["qty"] * (price - position["entry_price"]) * side_sign
            exit_fee = position["qty"] * price * fee_rate
            capital += gross - exit_fee
            fees_paid += exit_fee

            total_fee = position["entry_fee"] + exit_fee
            net = gross - total_fee + position["funding"]
            entry_local = datetime.fromtimestamp(position["entry_time"] / 1000).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            exit_local = datetime.fromtimestamp(close_time / 1000).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            duration_minutes = max((close_time - position["entry_time"]) / 60000.0, 0.0)
            entry_notional = max(position["qty"] * position["entry_price"], 1e-9)
            roi_pct = (net / entry_notional) * 100.0
            trades.append(
                BotTrade(
                    side=position["side"],
                    entry_time=position["entry_time"],
                    exit_time=close_time,
                    entry_time_local=entry_local,
                    exit_time_local=exit_local,
                    entry_price=position["entry_price"],
                    exit_price=price,
                    qty=position["qty"],
                    duration_minutes=duration_minutes,
                    roi_pct=roi_pct,
                    gross_pnl=gross,
                    fee_paid=total_fee,
                    funding_paid=position["funding"],
                    net_pnl=net,
                    reason=reason,
                )
            )

            # Set flag to skip next entry if this trade was a loss.
            if net < 0:
                skip_next_entry = True

            trade_number = len(trades)
            trade_logs.append(
                f"#{trade_number:02d} {position['side'].value} | Entry {entry_local} @ {position['entry_price']:.4f} -> "
                f"Exit {exit_local} @ {price:.4f} | PnL {net:.4f} | ROI {roi_pct:.2f}% | {reason}"
            )
            position = None

        for i, candle in enumerate(candles):
            prev_time = candles[i - 1].open_time if i > 0 else candle.open_time - 60_000

            while funding_idx < len(funding_rows):
                funding_time = int(funding_rows[funding_idx].get("fundingTime", 0))
                if not (prev_time < funding_time <= candle.open_time):
                    break
                if position is not None:
                    rate = float(funding_rows[funding_idx].get("fundingRate", 0.0))
                    side_sign = 1.0 if position["side"] == Direction.LONG else -1.0
                    notional = position["qty"] * candle.close
                    payment = (-rate * side_sign) * notional
                    capital += payment
                    funding_paid += payment
                    position["funding"] += payment
                funding_idx += 1

            if i < 40:
                equity_curve.append(EquityPoint(time=candle.open_time, equity=capital))
                continue

            if position is not None:
                hold_time_minutes = (candle.open_time - position["entry_time"]) / 60000.0
                can_exit = hold_time_minutes >= min_hold_minutes  # Only exit after min hold time.
                # Dynamic TP: at 50% of TP target, close early to lock profits if signal proven.
                half_tp_price = position["entry_price"]
                if position["side"] == Direction.LONG:
                    half_tp_price = position["entry_price"] + (
                        (position["tp_price"] - position["entry_price"]) * 0.5
                    )
                else:
                    half_tp_price = position["entry_price"] - (
                        (position["entry_price"] - position["tp_price"]) * 0.5
                    )

                if position["side"] == Direction.LONG:
                    if candle.low <= position["sl_price"]:
                        close_position(position["sl_price"], candle.open_time, "stop_loss")
                    elif can_exit and candle.high >= half_tp_price:
                        # Take half TP at min hold time.
                        close_position(half_tp_price, candle.open_time, "take_profit_50pct")
                    elif candle.high >= position["tp_price"]:
                        # Full TP anytime.
                        close_position(position["tp_price"], candle.open_time, "take_profit_full")
                else:
                    if candle.high >= position["sl_price"]:
                        close_position(position["sl_price"], candle.open_time, "stop_loss")
                    elif can_exit and candle.low <= half_tp_price:
                        # Take half TP at min hold time.
                        close_position(half_tp_price, candle.open_time, "take_profit_50pct")
                    elif candle.low <= position["tp_price"]:
                        # Full TP anytime.
                        close_position(position["tp_price"], candle.open_time, "take_profit_full")

            kline_slice = rows[max(0, i - 60) : i + 1]
            oi_slice = [row for row in oi_rows if int(row.get("timestamp", 0)) <= candle.open_time]
            oi_slice = oi_slice[-30:]
            base_signal, base_conf, trend_regime, htf_dir = adaptive_signal(kline_slice, candle.open_time)
            signal = base_signal
            liq_conf = 0.0

            if use_liquidation_data:
                if len(oi_slice) >= 2:
                    liq = build_liquidation_map_estimate(symbol, candle.close, oi_slice, kline_slice)
                    liq_conf = liq.confidence

                    if base_signal == Direction.FLAT:
                        signal = Direction.FLAT
                    elif trend_regime:
                        if liq.dominant_pull != base_signal or liq.confidence < max(confidence_threshold, 0.52):
                            signal = Direction.FLAT
                    else:
                        # In ranging markets, liquidation is a veto only on strong opposite pressure.
                        if liq.dominant_pull != base_signal and liq.confidence >= 0.75:
                            signal = Direction.FLAT

            effective_conf = max(base_conf, liq_conf) if use_liquidation_data else base_conf

            if signal == confirm_signal and signal != Direction.FLAT:
                confirm_count += 1
            else:
                confirm_signal = signal
                confirm_count = 1 if signal != Direction.FLAT else 0

            ready_signal = signal if (signal != Direction.FLAT and confirm_count >= confirmation_bars) else Direction.FLAT

            if position is not None:
                if ready_signal == Direction.FLAT:
                    close_position(candle.close, candle.open_time, "flat_signal")
                elif ready_signal != position["side"]:
                    close_position(candle.close, candle.open_time, "signal_flip")

            # Apply MTF regime filter: adjust confidence based on market bias.
            # SHORT entries: prefer in downtrend/mixed_down regimes (bias < -0.2).
            # LONG entries: prefer in uptrend/mixed_up regimes (bias > +0.2).
            # FLAT regime: less preference for directional trades, OK for range reversals.
            mtf_regime_filter = True
            if mtf_regime in (MarketRegime.STRONG_UPTREND, MarketRegime.UPTREND):
                # Strong uptrend: SHORT OK only above 0.75 conf, LONG OK at 0.70.
                if ready_signal == Direction.SHORT:
                    mtf_regime_filter = effective_conf >= 0.75
            elif mtf_regime in (MarketRegime.STRONG_DOWNTREND, MarketRegime.DOWNTREND):
                # Strong downtrend: LONG OK only above 0.75 conf, SHORT OK at 0.70.
                if ready_signal == Direction.LONG:
                    mtf_regime_filter = effective_conf >= 0.75
            elif mtf_regime in (MarketRegime.MIXED_UP,):
                # Mixed bullish: SHORT requires higher conf (0.75), LONG at 0.70.
                if ready_signal == Direction.SHORT:
                    mtf_regime_filter = effective_conf >= 0.75
            elif mtf_regime in (MarketRegime.MIXED_DOWN,):
                # Mixed bearish: LONG requires higher conf (0.75), SHORT at 0.70.
                if ready_signal == Direction.LONG:
                    mtf_regime_filter = effective_conf >= 0.75
            # BALANCED: no directional bias, accept both at 0.70.

            # Volume check: prefer entries on volume spikes to avoid thin-volume fills.
            vol_ok = not use_volume_filter or is_volume_spike(kline_slice)

            if (
                position is None
                and ready_signal in (Direction.LONG, Direction.SHORT)
                and effective_conf >= 0.70  # Very high confidence (0.70) for scalp entries.
                and sentiment_confirm(ready_signal, candle.open_time, trend_regime)
                and (
                    (not use_htf_filter)
                    or (not trend_regime)
                    or (htf_dir == ready_signal)
                )
                and mtf_regime_filter  # MTF regime must align or be neutral.
                and vol_ok  # Only enter on volume spikes.
                and len(trades) < max_trades
                and not skip_next_entry  # Skip 1 entry after a loss (prevent revenge trading).
            ):
                skip_next_entry = False  # Reset flag after entering successfully.
                trade_notional = max(capital, 0.0) * leverage
                if trade_notional > 0:
                    qty = trade_notional / candle.close
                    entry_fee = trade_notional * fee_rate
                    capital -= entry_fee
                    fees_paid += entry_fee

                    vol_bps = max(short_vol_bps(kline_slice, candle.close), 6.0)
                    stop_bps = max(vol_bps * 1.05, 8.0)
                    tp_bps = stop_bps * 1.35
                    if ready_signal == Direction.LONG:
                        tp_price = candle.close * (1 + tp_bps / 10_000)
                        sl_price = candle.close * (1 - stop_bps / 10_000)
                    else:
                        tp_price = candle.close * (1 - tp_bps / 10_000)
                        sl_price = candle.close * (1 + stop_bps / 10_000)

                    position = {
                        "side": ready_signal,
                        "qty": qty,
                        "entry_price": candle.close,
                        "entry_time": candle.open_time,
                        "entry_fee": entry_fee,
                        "funding": 0.0,
                        "tp_price": tp_price,
                        "sl_price": sl_price,
                    }

            if len(trades) >= max_trades:
                if position is not None:
                    close_position(candle.close, candle.open_time, "max_trades_reached")
                equity_curve.append(EquityPoint(time=candle.open_time, equity=capital))
                break

            equity = capital
            if position is not None:
                side_sign = 1.0 if position["side"] == Direction.LONG else -1.0
                equity += position["qty"] * (candle.close - position["entry_price"]) * side_sign
            equity_curve.append(EquityPoint(time=candle.open_time, equity=equity))

        if position is not None:
            last_candle = candles[-1]
            close_position(last_candle.close, last_candle.open_time, "end_of_test")
            equity_curve.append(EquityPoint(time=last_candle.open_time, equity=capital))

        peak = equity_curve[0].equity if equity_curve else initial_capital
        max_dd = 0.0
        for point in equity_curve:
            peak = max(peak, point.equity)
            if peak > 0:
                dd = ((peak - point.equity) / peak) * 100.0
                max_dd = max(max_dd, dd)

        total_trades = len(trades)
        wins = len([trade for trade in trades if trade.net_pnl > 0])
        win_rate = (wins / total_trades) if total_trades > 0 else 0.0
        return_pct = ((capital - initial_capital) / initial_capital) * 100.0 if initial_capital > 0 else 0.0

        return BotBacktestResponse(
            symbol=symbol,
            interval=interval,
            initial_capital=initial_capital,
            final_capital=capital,
            return_pct=return_pct,
            total_trades=total_trades,
            win_rate=win_rate,
            fees_paid=fees_paid,
            funding_paid=funding_paid,
            max_drawdown_pct=max_dd,
            trades=trades,
            trade_logs=trade_logs,
            equity_curve=equity_curve,
        )
