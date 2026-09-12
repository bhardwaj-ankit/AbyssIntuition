from __future__ import annotations

from datetime import datetime, timezone
from bisect import bisect_left, bisect_right
import threading
import time
from typing import Any
from uuid import uuid4

from liquidity_signal.config import SignalConfig
from liquidity_signal.data.binance_client import BinanceFuturesClient
from liquidity_signal.data.bybit_client import BybitPublicClient
from liquidity_signal.features.liquidation_map import (
    build_liquidation_map_advanced,
    build_liquidation_map_estimate,
    build_multi_resolution_tiles,
)
from liquidity_signal.features.liquidity_features import build_liquidity_features
from liquidity_signal.features.mtf_regime import MTFRegimeAnalyzer, MarketRegime
from liquidity_signal.models import (
    AIRefinementBreakdown,
    BotBacktestResponse,
    BotTrade,
    Candle,
    CandleResponse,
    CumulativeSignalResponse,
    CumulativeSignalTimeframe,
    MarketSymbol,
    MarketSymbolsResponse,
    Direction,
    EquityPoint,
    LiquidityFeatures,
    LiquidationEventPoint,
    LiquidationMapResponse,
    LiquidationReplaySnapshot,
    LiquidationTileResponse,
    LiquidationMapAdvancedResponse,
    LoraTrainingExportResponse,
    LoraTrainingExportSummary,
    HistoricalTrainingBackfillBatchItem,
    HistoricalTrainingBackfillBatchResponse,
    HistoricalTrainingBackfillResponse,
    PaperBotPosition,
    PaperBotStatus,
    ScoringBreakdown,
    SignalExplainResult,
    SignalResult,
    TrainingDatasetResponse,
    TrainingSnapshotLabel,
    TrainingSnapshotRecord,
)
from liquidity_signal.ai.lora_dataset import (
    TrainingExampleBundle,
    build_lora_example,
    chronological_split_examples,
    class_weights,
    rebalance_examples,
    rebalance_examples_by_split,
)
from liquidity_signal.risk.tpsl import compute_tp_sl, compute_tp_sl_with_breakdown
from liquidity_signal.service.liquidation_runtime import LiquidationRuntime
from liquidity_signal.service.liquidation_store import LiquidationStore
from liquidity_signal.signal.ai_refiner import refine_signal_with_ai
from liquidity_signal.signal.scorer import score_features_with_breakdown

SUPPORTED_MARKET_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "NEARUSDT", "PEPEUSDT", "XRPUSDT")
BINANCE_HISTORICAL_SYMBOL_ALIASES = {
    "PEPEUSDT": "1000PEPEUSDT",
}


class SignalEngine:
    BACKTEST_SIGNAL_SOURCE = "signal_api_plus_regime_strategy"
    PAPER_SIGNAL_SOURCE = "signal_api_live_regime_bot"
    TRAINING_LABEL_HORIZONS_MINUTES = (5, 15, 30, 60, 240)

    def __init__(
        self,
        client: BinanceFuturesClient | None = None,
        cfg: SignalConfig | None = None,
        bybit_client: BybitPublicClient | None = None,
        liquidation_store: LiquidationStore | None = None,
        liquidation_runtime: LiquidationRuntime | None = None,
    ) -> None:
        self.client = client or BinanceFuturesClient()
        self.cfg = cfg or SignalConfig()
        self.bybit_client = bybit_client or BybitPublicClient()
        self.liquidation_store = liquidation_store or LiquidationStore()
        self.liquidation_runtime = liquidation_runtime or LiquidationRuntime(store=self.liquidation_store)
        self._watchlist_thread: threading.Thread | None = None
        self._watchlist_stop = threading.Event()
        self._paper_bot_thread: threading.Thread | None = None
        self._paper_bot_stop = threading.Event()
        self._paper_bot_lock = threading.Lock()
        self._paper_bot_position_state: dict[str, Any] | None = None
        self._paper_bot_fee_rate = 0.0004
        self._market_symbols_cache: tuple[float, list[MarketSymbol]] | None = None
        self._paper_bot_status = PaperBotStatus(
            symbol="BTCUSDT",
            running=False,
            poll_interval_seconds=60,
            initial_capital=1000.0,
            current_capital=1000.0,
            realized_pnl=0.0,
            signal_source=self.PAPER_SIGNAL_SOURCE,
        )

    def close(self) -> None:
        self.client.close()
        self.bybit_client.close()
        self.liquidation_runtime.close()
        self._watchlist_stop.set()
        if self._watchlist_thread and self._watchlist_thread.is_alive():
            self._watchlist_thread.join(timeout=1.0)
        self._paper_bot_stop.set()
        if self._paper_bot_thread and self._paper_bot_thread.is_alive():
            self._paper_bot_thread.join(timeout=2.0)
        self.liquidation_store.close()

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            if value in (None, ""):
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, value))

    @staticmethod
    def _historical_fetch_symbol(symbol: str) -> str:
        return BINANCE_HISTORICAL_SYMBOL_ALIASES.get(symbol.upper(), symbol.upper())

    def _training_barrier_pct(self, *, explain: SignalExplainResult, cumulative: CumulativeSignalResponse, horizon_minutes: int) -> float:
        timeframe_map = {row.timeframe: row for row in cumulative.timeframes}
        atr_pct = self._safe_float(getattr(timeframe_map.get("5m"), "atr_pct", 0.0))
        if horizon_minutes >= 30:
            atr_pct = max(atr_pct, self._safe_float(getattr(timeframe_map.get("15m"), "atr_pct", 0.0)))
        volatility_pct = max(atr_pct, explain.features.short_volatility_bps / 100.0, 0.12)
        roundtrip_cost_pct = 0.10
        if horizon_minutes <= 15:
            horizon_multiplier = 0.95
        elif horizon_minutes <= 30:
            horizon_multiplier = 1.25
        elif horizon_minutes <= 60:
            horizon_multiplier = 1.60
        else:
            horizon_multiplier = 2.40
        return self._clamp(max(volatility_pct * horizon_multiplier, roundtrip_cost_pct + 0.08), 0.18, 3.20)

    def _build_training_labels(
        self,
        *,
        snapshot_id: str,
        symbol: str,
        event_ts: int,
        mark_price: float,
        explain: SignalExplainResult,
        cumulative: CumulativeSignalResponse,
    ) -> list[TrainingSnapshotLabel]:
        labels: list[TrainingSnapshotLabel] = []
        for horizon_minutes in self.TRAINING_LABEL_HORIZONS_MINUTES:
            barrier_pct = self._training_barrier_pct(explain=explain, cumulative=cumulative, horizon_minutes=horizon_minutes)
            labels.append(
                TrainingSnapshotLabel(
                    snapshot_id=snapshot_id,
                    symbol=symbol.upper(),
                    event_ts=event_ts,
                    horizon_minutes=horizon_minutes,
                    status="PENDING",
                    label_action=Direction.FLAT,
                    expires_at=event_ts + (horizon_minutes * 60 * 1000),
                    upper_barrier_price=mark_price * (1.0 + (barrier_pct / 100.0)),
                    lower_barrier_price=mark_price * (1.0 - (barrier_pct / 100.0)),
                    raw_payload={
                        "entry_price": mark_price,
                        "barrier_move_pct": barrier_pct,
                        "terminal_move_pct": barrier_pct * 0.45,
                    },
                )
            )
        return labels

    @staticmethod
    def _liquidation_training_context(
        liq_map: LiquidationMapAdvancedResponse | LiquidationMapResponse | None,
        *,
        fallback_price: float,
    ) -> dict[str, Any]:
        if liq_map is None:
            return {
                "dominant_pull": Direction.FLAT.value,
                "confidence": 0.0,
                "current_price": fallback_price,
            }

        payload: dict[str, Any] = {
            "dominant_pull": liq_map.dominant_pull.value,
            "confidence": liq_map.confidence,
            "current_price": liq_map.current_price,
            "methodology": getattr(liq_map, "methodology", ""),
            "source": getattr(liq_map, "source", ""),
            "price_range_low": getattr(liq_map, "price_range_low", 0.0),
            "price_range_high": getattr(liq_map, "price_range_high", 0.0),
            "levels_above": [row.model_dump(mode="json") for row in getattr(liq_map, "levels_above", [])[:5]],
            "levels_below": [row.model_dump(mode="json") for row in getattr(liq_map, "levels_below", [])[:5]],
            "clusters_above": [row.model_dump(mode="json") for row in getattr(liq_map, "clusters_above", [])[:5]],
            "clusters_below": [row.model_dump(mode="json") for row in getattr(liq_map, "clusters_below", [])[:5]],
            "assumptions": getattr(liq_map, "assumptions", None).model_dump(mode="json")
            if getattr(liq_map, "assumptions", None)
            else {},
            "meta": getattr(liq_map, "meta", {}),
        }
        market_metrics = getattr(liq_map, "market_metrics", None)
        if market_metrics is not None:
            payload["market_metrics"] = market_metrics.model_dump(mode="json")
        quality = getattr(liq_map, "quality", None)
        if quality is not None:
            payload["quality"] = {
                "estimate_weight": quality.estimate_weight,
                "event_weight": quality.event_weight,
                "event_overlay_active": quality.event_overlay_active,
                "events_used": quality.events_used,
                "degraded_mode": quality.degraded_mode,
            }
            payload["events_used"] = quality.events_used
        events_summary = getattr(liq_map, "events_summary", None)
        if events_summary is not None:
            payload["events_summary"] = events_summary.model_dump(mode="json")
        return payload

    def capture_training_snapshot(
        self,
        *,
        symbol: str,
        exchange: str,
        environment: str,
        session_id: str | None,
        mode: str,
        explain: SignalExplainResult,
        cumulative: CumulativeSignalResponse,
        liq_map: LiquidationMapAdvancedResponse | None,
        behavior: dict[str, Any],
        bot_signal: SignalResult,
        open_position: dict[str, Any] | None,
        ai_decision: dict[str, Any] | None,
        decision_action: str,
        took_trade: bool,
        snapshot_id: str | None = None,
        auto_resolve_live: bool = True,
        whale_context: dict[str, Any] | None = None,
        structure_context: dict[str, Any] | None = None,
        session_context: dict[str, Any] | None = None,
        data_quality_context: dict[str, Any] | None = None,
    ) -> str:
        event_ts = int(bot_signal.decision_ts or time.time() * 1000)
        snapshot_id = snapshot_id or uuid4().hex
        timeframe_payload = {
            row.timeframe: {
                "market_mode": row.market_mode,
                "direction": row.direction.value,
                "confidence": row.confidence,
                "signal_quality": row.signal_quality,
                "score": row.score,
                "max_score": row.max_score,
                "close": row.close,
                "ema20": row.ema20,
                "ema50": row.ema50,
                "ema200": row.ema200,
                "vwap": row.vwap,
                "rsi14": row.rsi14,
                "macd": row.macd,
                "macd_signal": row.macd_signal,
                "macd_histogram": row.macd_histogram,
                "atr_pct": row.atr_pct,
                "atr_state": row.atr_state,
                "adx14": row.adx14,
                "volume_ratio": row.volume_ratio,
                "breakout_failure_rate": row.breakout_failure_rate,
                "pattern": row.pattern,
                "nearest_support": row.nearest_support,
                "nearest_resistance": row.nearest_resistance,
                "reasons": row.reasons[:4],
            }
            for row in cumulative.timeframes
        }
        payload = {
            "symbol": symbol.upper(),
            "event_ts": event_ts,
            "exchange": exchange,
            "environment": environment,
            "mode": mode,
            "features": explain.features.model_dump(mode="json"),
            "scoring": explain.scoring.model_dump(mode="json"),
            "risk": explain.risk.model_dump(mode="json"),
            "cumulative": cumulative.model_dump(mode="json"),
            "timeframes": timeframe_payload,
            "liquidation_map": self._liquidation_training_context(liq_map, fallback_price=explain.features.mark_price),
            "market_behavior": behavior,
            "bot_signal": bot_signal.model_dump(mode="json"),
            "open_position": open_position or {},
            "ai_decision": ai_decision or {},
            "whale_context": whale_context or {},
            "structure_context": structure_context or {},
            "session_context": session_context or {},
            "data_quality_context": data_quality_context or {},
            "decision_action": decision_action,
            "took_trade": took_trade,
        }
        snapshot = TrainingSnapshotRecord(
            snapshot_id=snapshot_id,
            symbol=symbol.upper(),
            event_ts=event_ts,
            exchange=exchange,
            environment=environment,
            mark_price=bot_signal.current_price,
            signal_direction=bot_signal.direction,
            signal_confidence=bot_signal.confidence,
            signal_quality=bot_signal.signal_quality,
            decision_action=decision_action,
            took_trade=took_trade,
            raw_payload=payload,
        )
        decision_payload = {
            "snapshot_id": snapshot_id,
            "symbol": symbol.upper(),
            "event_ts": event_ts,
            "session_id": session_id,
            "mode": mode,
            "decision_action": decision_action,
            "took_trade": took_trade,
            "ai_verdict": (ai_decision or {}).get("entry_verdict") or (ai_decision or {}).get("exit_action"),
            "ai_reason": (ai_decision or {}).get("reason"),
            "open_position": open_position or {},
        }
        self.liquidation_store.persist_training_snapshot(snapshot, decision_payload)
        for label in self._build_training_labels(
            snapshot_id=snapshot_id,
            symbol=symbol.upper(),
            event_ts=event_ts,
            mark_price=bot_signal.current_price,
            explain=explain,
            cumulative=cumulative,
        ):
            self.liquidation_store.persist_training_label(label)
        if auto_resolve_live:
            self.resolve_pending_training_labels(symbol.upper())
        return snapshot_id

    def _resolve_training_label_from_path(
        self,
        label: TrainingSnapshotLabel,
        path: list[list[Any]],
        *,
        now_ms: int,
    ) -> TrainingSnapshotLabel | None:
        entry_price = self._safe_float(label.raw_payload.get("entry_price"), default=0.0)
        if entry_price <= 0.0 or not path:
            return None

        max_up_pct = 0.0
        max_down_pct = 0.0
        resolved_action = Direction.FLAT
        barrier_first_hit: Direction | None = None
        barrier_hit_ts: int | None = None
        barrier_hit_price: float | None = None
        barrier_ambiguous = False
        horizon_close_price = self._safe_float(path[-1][4], default=entry_price)

        for candle in path:
            high_price = self._safe_float(candle[2], default=entry_price)
            low_price = self._safe_float(candle[3], default=entry_price)
            max_up_pct = max(max_up_pct, ((high_price - entry_price) / entry_price) * 100.0)
            max_down_pct = max(max_down_pct, ((entry_price - low_price) / entry_price) * 100.0)

            if barrier_first_hit is not None or barrier_ambiguous:
                continue
            upper_hit = high_price >= label.upper_barrier_price
            lower_hit = low_price <= label.lower_barrier_price
            if upper_hit and lower_hit:
                # One-minute OHLC cannot reveal which barrier was hit first.
                barrier_ambiguous = True
                barrier_hit_ts = int(candle[0])
            elif upper_hit:
                barrier_first_hit = Direction.LONG
                barrier_hit_ts = int(candle[0])
                barrier_hit_price = label.upper_barrier_price
            elif lower_hit:
                barrier_first_hit = Direction.SHORT
                barrier_hit_ts = int(candle[0])
                barrier_hit_price = label.lower_barrier_price

        if barrier_first_hit is not None:
            resolved_action = barrier_first_hit
        elif not barrier_ambiguous:
            terminal_move_pct = self._safe_float(label.raw_payload.get("terminal_move_pct"), default=0.10)
            terminal_return_pct = ((horizon_close_price - entry_price) / entry_price) * 100.0
            if terminal_return_pct >= terminal_move_pct:
                resolved_action = Direction.LONG
            elif terminal_return_pct <= -terminal_move_pct:
                resolved_action = Direction.SHORT

        horizon_return_bps = ((horizon_close_price - entry_price) / entry_price) * 10_000.0
        if resolved_action == Direction.LONG:
            max_favorable_excursion_pct = max_up_pct
            max_adverse_excursion_pct = max_down_pct
        elif resolved_action == Direction.SHORT:
            max_favorable_excursion_pct = max_down_pct
            max_adverse_excursion_pct = max_up_pct
        else:
            max_favorable_excursion_pct = None
            max_adverse_excursion_pct = None

        return label.model_copy(
            update={
                "status": "RESOLVED",
                "label_action": resolved_action,
                "resolved_at": now_ms,
                "barrier_first_hit": barrier_first_hit,
                "barrier_hit_ts": barrier_hit_ts,
                "barrier_hit_price": barrier_hit_price,
                "barrier_ambiguous": barrier_ambiguous,
                "horizon_close_price": horizon_close_price,
                "horizon_return_bps": horizon_return_bps,
                "max_favorable_excursion_pct": max_favorable_excursion_pct,
                "max_adverse_excursion_pct": max_adverse_excursion_pct,
                "terminal_price": horizon_close_price,
                "max_up_pct": max_up_pct,
                "max_down_pct": max_down_pct,
                "raw_payload": {
                    **label.raw_payload,
                    "label_version": "triple-barrier-v2",
                    "resolved_by": "triple_barrier_path_v2",
                },
            }
        )

    def resolve_pending_training_labels(self, symbol: str, now_ms: int | None = None) -> int:
        resolved = 0
        now_ms = int(now_ms or time.time() * 1000)
        pending_rows = self.liquidation_store.load_pending_training_labels(symbol.upper(), now_ms=now_ms, limit=500)
        if not pending_rows:
            return 0

        oldest_event_ts = min(int(row.get("event_ts", now_ms)) for row in pending_rows)
        lookback_minutes = max(120, int((now_ms - oldest_event_ts) / 60_000) + 10)
        klines = self.client.get_recent_klines(symbol.upper(), interval="1m", limit=min(lookback_minutes, 1000))
        if not klines:
            return 0

        latest_kline_ts = int(klines[-1][0])
        for row in pending_rows:
            label = TrainingSnapshotLabel.model_validate(row)
            if label.expires_at > latest_kline_ts + 60_000:
                continue

            path = [
                candle for candle in klines
                if label.event_ts <= int(candle[0]) < label.expires_at
            ]
            updated = self._resolve_training_label_from_path(label, path, now_ms=now_ms)
            if updated is None:
                continue
            updated.raw_payload["resolved_by"] = "triple_barrier_live"
            self.liquidation_store.persist_training_label(updated)
            resolved += 1
        return resolved

    def load_training_dataset(
        self,
        symbol: str,
        *,
        limit: int = 100,
        resolved_only: bool = False,
    ) -> TrainingDatasetResponse:
        return TrainingDatasetResponse(
            symbol=symbol.upper(),
            summary=self.liquidation_store.training_dataset_summary(symbol.upper()),
            snapshots=[
                TrainingSnapshotRecord.model_validate(row)
                for row in self.liquidation_store.load_recent_training_snapshots(symbol.upper(), limit=limit)
            ],
            labels=[
                TrainingSnapshotLabel.model_validate(row)
                for row in self.liquidation_store.load_recent_training_labels(
                    symbol.upper(),
                    limit=limit,
                    resolved_only=resolved_only,
                )
            ],
        )

    def _historical_behavior_at(
        self,
        *,
        time_ms: int,
        klines_1h: list[list[Any]],
        klines_4h: list[list[Any]],
        klines_12h: list[list[Any]],
    ) -> dict[str, Any]:
        try:
            analyzer = MTFRegimeAnalyzer()
            mtf_regime, mtf_bias = self._mtf_context_at(
                analyzer,
                time_ms=time_ms,
                klines_1h=klines_1h,
                klines_4h=klines_4h,
                klines_12h=klines_12h,
            )
            if mtf_bias > 0.3:
                bias_label = "BULLISH"
            elif mtf_bias < -0.3:
                bias_label = "BEARISH"
            else:
                bias_label = "NEUTRAL"
            is_trending = mtf_regime in (
                MarketRegime.STRONG_UPTREND,
                MarketRegime.UPTREND,
                MarketRegime.STRONG_DOWNTREND,
                MarketRegime.DOWNTREND,
            )
            return {
                "regime": mtf_regime.value if hasattr(mtf_regime, "value") else str(mtf_regime),
                "bias": bias_label,
                "mode": "TRENDING" if is_trending else "RANGING",
                "bias_strength": round(abs(mtf_bias), 2),
            }
        except Exception as exc:
            return {"error": str(exc), "regime": "UNKNOWN", "bias": "NEUTRAL", "mode": "UNKNOWN"}

    def _whale_proxy_context(
        self,
        *,
        features: LiquidityFeatures,
        visible_1m: list[list[Any]],
        liq_map: LiquidationMapAdvancedResponse | None,
    ) -> dict[str, Any]:
        last_5 = visible_1m[-5:]
        if len(last_5) >= 2:
            start_close = self._safe_float(last_5[0][4], default=features.mark_price)
            end_close = self._safe_float(last_5[-1][4], default=features.mark_price)
            impulse_bps_5m = ((end_close - start_close) / max(start_close, 1e-9)) * 10_000
            volume_now = sum(self._safe_float(row[5], default=0.0) for row in last_5)
            prev_20 = visible_1m[-25:-5]
            baseline_volume = sum(self._safe_float(row[5], default=0.0) for row in prev_20) / max(len(prev_20), 1)
            volume_ratio_5m = volume_now / max(baseline_volume * max(len(last_5), 1), 1e-9)
        else:
            impulse_bps_5m = 0.0
            volume_ratio_5m = 0.0

        open_interest_acceleration = features.open_interest_change_pct * max(abs(impulse_bps_5m) / 25.0, 0.5)
        top_trader_account_divergence = features.top_trader_account_ratio - features.global_long_short_ratio
        top_trader_position_divergence = features.top_trader_position_ratio - features.global_long_short_ratio
        taker_aggression_score = (features.taker_buy_sell_ratio - 1.0) * 100.0

        if liq_map and liq_map.dominant_pull == Direction.LONG and features.funding_rate_bps > 4.0:
            squeeze_risk = "SHORT_SQUEEZE"
        elif liq_map and liq_map.dominant_pull == Direction.SHORT and features.funding_rate_bps < -4.0:
            squeeze_risk = "LONG_SQUEEZE"
        else:
            squeeze_risk = "NEUTRAL"

        if abs(impulse_bps_5m) > 20.0 and volume_ratio_5m > 1.8 and abs(taker_aggression_score) < 1.5:
            absorption_hint = "LIKELY_ABSORPTION"
        elif abs(impulse_bps_5m) > 20.0 and volume_ratio_5m > 1.8:
            absorption_hint = "AGGRESSIVE_INITIATIVE"
        else:
            absorption_hint = "NONE"

        if top_trader_position_divergence > 0.08 and features.funding_rate_bps > 0:
            crowding_bias = "LONG_CROWDED"
        elif top_trader_position_divergence < -0.08 and features.funding_rate_bps < 0:
            crowding_bias = "SHORT_CROWDED"
        else:
            crowding_bias = "NEUTRAL"

        return {
            "impulse_bps_5m": round(impulse_bps_5m, 3),
            "volume_ratio_5m": round(volume_ratio_5m, 3),
            "open_interest_acceleration": round(open_interest_acceleration, 3),
            "top_trader_account_divergence": round(top_trader_account_divergence, 4),
            "top_trader_position_divergence": round(top_trader_position_divergence, 4),
            "taker_aggression_score": round(taker_aggression_score, 3),
            "squeeze_risk": squeeze_risk,
            "absorption_hint": absorption_hint,
            "crowding_bias": crowding_bias,
        }

    def _session_context(self, *, time_ms: int) -> dict[str, Any]:
        utc = datetime.fromtimestamp(time_ms / 1000.0, tz=timezone.utc)
        utc_hour = utc.hour
        weekday = utc.weekday()

        if 0 <= utc_hour < 7:
            session = "ASIA"
        elif 7 <= utc_hour < 12:
            session = "ASIA_EUROPE_OVERLAP"
        elif 12 <= utc_hour < 17:
            session = "EUROPE"
        elif 17 <= utc_hour < 21:
            session = "EUROPE_US_OVERLAP"
        else:
            session = "US"

        if weekday >= 5:
            session_type = "WEEKEND"
        else:
            session_type = "WEEKDAY"

        return {
            "utc_hour": utc_hour,
            "weekday": weekday,
            "session": session,
            "session_type": session_type,
        }

    def _structure_context(
        self,
        *,
        visible_1m: list[list[Any]],
        visible_5m: list[list[Any]],
        mark_price: float,
    ) -> dict[str, Any]:
        recent_30m = visible_1m[-30:]
        recent_60m = visible_1m[-60:]
        recent_5m = visible_5m[-6:]
        prior_5m = visible_5m[-18:-6]
        last_candle = visible_1m[-1] if visible_1m else None

        if not recent_30m or mark_price <= 0.0 or last_candle is None:
            return {
                "range_position_30m": 0.5,
                "range_width_pct_30m": 0.0,
                "compression_ratio": 1.0,
                "breakout_state": "UNKNOWN",
                "trend_persistence_1m": 0.0,
                "wick_skew_last_1m": 0.0,
                "distance_from_range_mid_pct": 0.0,
                "fakeout_risk": "UNKNOWN",
            }

        range_high = max(self._safe_float(row[2], default=mark_price) for row in recent_30m)
        range_low = min(self._safe_float(row[3], default=mark_price) for row in recent_30m)
        range_width_pct = ((range_high - range_low) / mark_price) * 100.0 if mark_price > 0.0 else 0.0
        if range_high > range_low:
            range_position = (mark_price - range_low) / (range_high - range_low)
        else:
            range_position = 0.5
        range_mid = (range_high + range_low) / 2.0
        distance_from_mid_pct = ((mark_price - range_mid) / mark_price) * 100.0 if mark_price > 0.0 else 0.0

        def _avg_range(rows: list[list[Any]]) -> float:
            if not rows:
                return 0.0
            values = [abs(self._safe_float(row[2], default=mark_price) - self._safe_float(row[3], default=mark_price)) for row in rows]
            return sum(values) / max(len(values), 1)

        short_range = _avg_range(recent_5m)
        baseline_range = _avg_range(prior_5m) or short_range or 1e-9
        compression_ratio = short_range / baseline_range if baseline_range > 0.0 else 1.0

        directional_moves = []
        for previous, current in zip(recent_60m[:-1], recent_60m[1:]):
            prev_close = self._safe_float(previous[4], default=mark_price)
            curr_close = self._safe_float(current[4], default=mark_price)
            if curr_close > prev_close:
                directional_moves.append(1.0)
            elif curr_close < prev_close:
                directional_moves.append(-1.0)
            else:
                directional_moves.append(0.0)
        trend_persistence = sum(directional_moves[-20:]) / max(len(directional_moves[-20:]), 1)

        open_price = self._safe_float(last_candle[1], default=mark_price)
        high_price = self._safe_float(last_candle[2], default=mark_price)
        low_price = self._safe_float(last_candle[3], default=mark_price)
        close_price = self._safe_float(last_candle[4], default=mark_price)
        candle_range = max(high_price - low_price, 1e-9)
        upper_wick = max(high_price - max(open_price, close_price), 0.0)
        lower_wick = max(min(open_price, close_price) - low_price, 0.0)
        wick_skew = (upper_wick - lower_wick) / candle_range

        prior_high = max(self._safe_float(row[2], default=mark_price) for row in recent_30m[:-1]) if len(recent_30m) > 1 else range_high
        prior_low = min(self._safe_float(row[3], default=mark_price) for row in recent_30m[:-1]) if len(recent_30m) > 1 else range_low
        breakout_state = "INSIDE_RANGE"
        if high_price > prior_high and close_price <= prior_high:
            breakout_state = "FAILED_UP_BREAK"
        elif low_price < prior_low and close_price >= prior_low:
            breakout_state = "FAILED_DOWN_BREAK"
        elif close_price > prior_high:
            breakout_state = "UP_BREAK"
        elif close_price < prior_low:
            breakout_state = "DOWN_BREAK"

        fakeout_risk = "LOW"
        if breakout_state.startswith("FAILED_"):
            fakeout_risk = "HIGH"
        elif compression_ratio < 0.72 and abs(range_position - 0.5) > 0.35:
            fakeout_risk = "MEDIUM"
        elif abs(wick_skew) > 0.45 and abs(trend_persistence) < 0.2:
            fakeout_risk = "MEDIUM"

        return {
            "range_position_30m": round(self._clamp(range_position, 0.0, 1.0), 3),
            "range_width_pct_30m": round(max(range_width_pct, 0.0), 3),
            "compression_ratio": round(max(compression_ratio, 0.0), 3),
            "breakout_state": breakout_state,
            "trend_persistence_1m": round(self._clamp(trend_persistence, -1.0, 1.0), 3),
            "wick_skew_last_1m": round(self._clamp(wick_skew, -1.0, 1.0), 3),
            "distance_from_range_mid_pct": round(distance_from_mid_pct, 3),
            "fakeout_risk": fakeout_risk,
        }

    def _data_quality_context(
        self,
        *,
        oi_rows: list[dict[str, Any]],
        funding_rows: list[dict[str, Any]],
        basis_rows: list[dict[str, Any]],
        taker_rows: list[dict[str, Any]],
        global_ratio_rows: list[dict[str, Any]],
        top_account_rows: list[dict[str, Any]],
        top_position_rows: list[dict[str, Any]],
        used_liquidation_snapshot: bool,
    ) -> dict[str, Any]:
        coverage_counts = {
            "oi_rows": len(oi_rows),
            "funding_rows": len(funding_rows),
            "basis_rows": len(basis_rows),
            "taker_rows": len(taker_rows),
            "global_ratio_rows": len(global_ratio_rows),
            "top_account_rows": len(top_account_rows),
            "top_position_rows": len(top_position_rows),
        }
        positioning_complete = min(
            coverage_counts["oi_rows"],
            coverage_counts["taker_rows"],
            coverage_counts["global_ratio_rows"],
            coverage_counts["top_account_rows"],
            coverage_counts["top_position_rows"],
        ) >= 5
        derivatives_complete = min(
            coverage_counts["funding_rows"],
            coverage_counts["basis_rows"],
        ) >= 2
        return {
            **coverage_counts,
            "positioning_complete": positioning_complete,
            "derivatives_complete": derivatives_complete,
            "used_liquidation_snapshot": used_liquidation_snapshot,
            "coverage_score": round(
                (
                    sum(1 for value in coverage_counts.values() if value > 0)
                    + (1 if used_liquidation_snapshot else 0)
                )
                / 8.0,
                3,
            ),
        }

    def _historical_cumulative_signal_at(
        self,
        *,
        symbol: str,
        time_ms: int,
        features: LiquidityFeatures,
        liq_map: LiquidationMapAdvancedResponse | None,
        klines_5m: list[list[Any]],
        klines_15m: list[list[Any]],
        klines_1h: list[list[Any]],
    ) -> CumulativeSignalResponse:
        timeframe_specs = [("5m", klines_5m, 220), ("15m", klines_15m, 220), ("1h", klines_1h, 260)]
        timeframe_signals: list[CumulativeSignalTimeframe] = []
        for timeframe, rows, limit in timeframe_specs:
            interval_minutes = {"5m": 5, "15m": 15, "1h": 60}[timeframe]
            visible_rows = self._closed_klines_at(
                rows,
                time_ms=time_ms,
                interval_minutes=interval_minutes,
                limit=limit,
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
                for row in visible_rows
            ]
            if not candles:
                continue
            timeframe_signals.append(
                self._cumulative_timeframe_signal(timeframe=timeframe, candles=candles, liq_map=liq_map)
            )

        if not timeframe_signals:
            return CumulativeSignalResponse(
                symbol=symbol,
                generated_at=time_ms,
                market_mode="TRANSITIONAL",
                strategy_mode="STAY_FLAT",
                aggregate_direction=Direction.FLAT,
                aggregate_confidence=0.0,
                agreement_ratio=0.0,
                signal_quality="LOW",
                reasons=["insufficient historical context"],
                bot_signal=SignalResult(
                    symbol=symbol,
                    direction=Direction.FLAT,
                    confidence=0.0,
                    current_price=features.mark_price,
                    tp=features.mark_price,
                    sl=features.mark_price,
                    reasons=["insufficient historical context"],
                    horizon="MTF 5m/15m/1h",
                    decision_ts=time_ms,
                    expires_at=time_ms + (self.cfg.signal_ttl_seconds * 1000),
                    signal_quality="LOW",
                ),
                timeframes=[],
            )

        weights = {"5m": 1.0, "15m": 1.35, "1h": 1.75}
        weighted_score = sum(signal.score * weights.get(signal.timeframe, 1.0) for signal in timeframe_signals)
        weighted_max = sum(signal.max_score * weights.get(signal.timeframe, 1.0) for signal in timeframe_signals)
        mode_weights = {"TRENDING": 1.0, "RANGING": -1.0, "TRANSITIONAL": 0.0}
        weighted_mode_score = sum(
            mode_weights.get(signal.market_mode, 0.0) * weights.get(signal.timeframe, 1.0)
            for signal in timeframe_signals
        )
        if weighted_mode_score >= 1.0:
            aggregate_market_mode = "TRENDING"
            strategy_mode = "TREND_FOLLOW"
        elif weighted_mode_score <= -1.0:
            aggregate_market_mode = "RANGING"
            strategy_mode = "MEAN_REVERT"
        else:
            aggregate_market_mode = "TRANSITIONAL"
            strategy_mode = "STAY_FLAT"

        if aggregate_market_mode == "RANGING":
            weighted_score *= 0.82
        elif aggregate_market_mode == "TRANSITIONAL":
            weighted_score *= 0.65

        aggregate_confidence = min(1.0, abs(weighted_score) / max(weighted_max, 1e-9))
        if aggregate_market_mode == "TRANSITIONAL":
            aggregate_confidence = min(aggregate_confidence, 0.42)

        if aggregate_confidence < 0.18 or aggregate_market_mode == "TRANSITIONAL":
            aggregate_direction = Direction.FLAT
        else:
            aggregate_direction = Direction.LONG if weighted_score > 0 else Direction.SHORT

        aligned = sum(
            1 for signal in timeframe_signals if signal.direction == aggregate_direction and signal.direction != Direction.FLAT
        )
        agreement_ratio = aligned / max(len(timeframe_signals), 1)
        aggregate_reasons = [
            f"Market mode {aggregate_market_mode.lower()} with {strategy_mode.lower().replace('_', ' ')} bias"
        ]
        for signal in sorted(timeframe_signals, key=lambda row: weights.get(row.timeframe, 1.0), reverse=True):
            if signal.direction == aggregate_direction and signal.direction != Direction.FLAT:
                aggregate_reasons.append(
                    f"{signal.timeframe} {signal.market_mode.lower()} {signal.direction.value} {signal.confidence:.2f}: {', '.join(signal.reasons[:2])}"
                )
        if aggregate_market_mode == "RANGING":
            ranging_signals = [signal for signal in timeframe_signals if signal.market_mode == "RANGING"]
            if not ranging_signals or agreement_ratio < 0.34:
                aggregate_direction = Direction.FLAT
                aggregate_confidence = min(aggregate_confidence, 0.3)
                aggregate_reasons.append("Range regime lacks clean reversal alignment, so bot stays flat")

        bot_tp, bot_sl = compute_tp_sl(aggregate_direction, features.mark_price, aggregate_confidence, features, self.cfg)
        bot_signal = SignalResult(
            symbol=symbol,
            direction=aggregate_direction,
            confidence=aggregate_confidence,
            current_price=features.mark_price,
            tp=bot_tp,
            sl=bot_sl,
            reasons=aggregate_reasons[:6],
            horizon="MTF 5m/15m/1h",
            decision_ts=time_ms,
            expires_at=time_ms + (self.cfg.signal_ttl_seconds * 1000),
            entry_assumption="mark",
            model_version=f"{self.cfg.model_version}+cumulative-mtf",
            feature_version=self.cfg.feature_version,
            signal_quality=self._signal_quality(aggregate_direction, aggregate_confidence, aggregate_reasons[:6]),
        )
        return CumulativeSignalResponse(
            symbol=symbol,
            generated_at=time_ms,
            market_mode=aggregate_market_mode,
            strategy_mode=strategy_mode,
            aggregate_direction=aggregate_direction,
            aggregate_confidence=aggregate_confidence,
            agreement_ratio=agreement_ratio,
            signal_quality=bot_signal.signal_quality,
            reasons=aggregate_reasons[:6],
            bot_signal=bot_signal,
            timeframes=timeframe_signals,
        )

    def backfill_historical_training(
        self,
        symbol: str,
        *,
        lookback_hours: int = 24,
        step_minutes: int = 5,
        max_samples: int = 400,
        include_stored_liquidation: bool = True,
        include_historical_positioning: bool = True,
    ) -> HistoricalTrainingBackfillResponse:
        fetch_symbol = self._historical_fetch_symbol(symbol.upper())
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - (lookback_hours * 60 * 60 * 1000)
        notes: list[str] = []
        if fetch_symbol != symbol.upper():
            notes.append(f"Historical fetch used exchange alias {fetch_symbol}.")

        warmup_1m_start = start_ms - max(self.cfg.short_kline_limit + 40, 180) * 60_000
        warmup_5m_start = start_ms - (220 * 5 * 60_000)
        warmup_15m_start = start_ms - (220 * 15 * 60_000)
        warmup_1h_start = start_ms - (260 * 60 * 60_000)
        warmup_4h_start = start_ms - (100 * 4 * 60 * 60_000)
        warmup_12h_start = start_ms - (100 * 12 * 60 * 60_000)
        # Binance futures-data endpoints retain only the latest 30 days and can
        # return an empty page when the cursor lands a few milliseconds outside
        # that rolling boundary. Keep a one-hour safety margin.
        metrics_start_ms = max(start_ms, now_ms - (719 * 60 * 60 * 1000))

        klines_1m = self.client.get_historical_klines(fetch_symbol, "1m", warmup_1m_start, now_ms)
        klines_5m = self.client.get_historical_klines(fetch_symbol, "5m", warmup_5m_start, now_ms)
        klines_15m = self.client.get_historical_klines(fetch_symbol, "15m", warmup_15m_start, now_ms)
        klines_1h = self.client.get_historical_klines(fetch_symbol, "1h", warmup_1h_start, now_ms)
        klines_4h = self.client.get_historical_klines(fetch_symbol, "4h", warmup_4h_start, now_ms)
        klines_12h = self.client.get_historical_klines(fetch_symbol, "12h", warmup_12h_start, now_ms)

        try:
            historical = getattr(self.client, "get_historical_open_interest_hist", None) if include_historical_positioning else None
            oi_hist = historical(fetch_symbol, "5m", metrics_start_ms, now_ms) if historical else self.client.get_open_interest_hist(fetch_symbol, period="5m", limit=240)
        except Exception:
            oi_hist = []
            notes.append("Open interest history was unavailable for part of the backfill.")
        try:
            funding_rates = self.client.get_funding_rates(fetch_symbol, limit=240)
        except Exception:
            funding_rates = []
            notes.append("Funding history was unavailable for part of the backfill.")
        try:
            historical = getattr(self.client, "get_historical_basis", None) if include_historical_positioning else None
            if historical:
                try:
                    basis_rows = historical(fetch_symbol, "5m", metrics_start_ms, now_ms)
                except Exception:
                    basis_rows = self.client.get_basis(fetch_symbol, period="5m", limit=240)
                    notes.append("Historical basis pagination was unavailable; recent basis was retained.")
            else:
                basis_rows = self.client.get_basis(fetch_symbol, period="5m", limit=240)
        except Exception:
            basis_rows = []
            notes.append("Basis history hit rate limits or was unavailable.")
        try:
            historical = getattr(self.client, "get_historical_taker_buy_sell_volume", None) if include_historical_positioning else None
            taker_volume_rows = historical(fetch_symbol, "5m", metrics_start_ms, now_ms) if historical else self.client.get_taker_buy_sell_volume(fetch_symbol, period="5m", limit=240)
        except Exception:
            taker_volume_rows = []
            notes.append("Taker buy/sell history hit rate limits or was unavailable.")
        try:
            historical = getattr(self.client, "get_historical_global_long_short_account_ratio", None) if include_historical_positioning else None
            global_ratio_rows = historical(fetch_symbol, "5m", metrics_start_ms, now_ms) if historical else self.client.get_global_long_short_account_ratio(fetch_symbol, period="5m", limit=240)
        except Exception:
            global_ratio_rows = []
            notes.append("Global long/short ratio history hit rate limits or was unavailable.")
        try:
            historical = getattr(self.client, "get_historical_top_long_short_account_ratio", None) if include_historical_positioning else None
            top_account_ratio_rows = historical(fetch_symbol, "5m", metrics_start_ms, now_ms) if historical else self.client.get_top_long_short_account_ratio(fetch_symbol, period="5m", limit=240)
        except Exception:
            top_account_ratio_rows = []
            notes.append("Top account ratio history hit rate limits or was unavailable.")
        try:
            historical = getattr(self.client, "get_historical_top_long_short_position_ratio", None) if include_historical_positioning else None
            top_position_ratio_rows = historical(fetch_symbol, "5m", metrics_start_ms, now_ms) if historical else self.client.get_top_long_short_position_ratio(fetch_symbol, period="5m", limit=240)
        except Exception:
            top_position_ratio_rows = []
            notes.append("Top position ratio history hit rate limits or was unavailable.")

        if oi_hist:
            oldest_oi_ts = min(self._row_timestamp(row) for row in oi_hist if self._row_timestamp(row) > 0)
            if oldest_oi_ts > start_ms:
                notes.append("Positioning/funding context is only partially available for the earliest samples.")

        samples_attempted = 0
        snapshots_created = 0
        resolved_labels = 0
        skipped_samples = 0
        used_liquidation_snapshots = 0
        step_ms = step_minutes * 60_000

        anchor_cutoff_ms = now_ms - (max(self.TRAINING_LABEL_HORIZONS_MINUTES) * 60_000)
        candidate_rows = [
            row for row in klines_1m
            if start_ms <= int(row[0]) <= anchor_cutoff_ms
            and int(row[0]) % step_ms == 0
        ]
        anchors = [
            self._kline_close_time(row, 1)
            for row in candidate_rows[:max_samples]
        ]

        analyzer = MTFRegimeAnalyzer()
        for anchor_ms in anchors:
            samples_attempted += 1
            visible_1m = self._closed_klines_at(
                klines_1m,
                time_ms=anchor_ms,
                interval_minutes=1,
                limit=self.cfg.short_kline_limit,
            )
            if len(visible_1m) < min(self.cfg.short_kline_limit, 40):
                skipped_samples += 1
                continue

            mtf_regime, htf_bias = self._mtf_context_at(
                analyzer,
                time_ms=anchor_ms,
                klines_1h=klines_1h,
                klines_4h=klines_4h,
                klines_12h=klines_12h,
            )
            features = build_liquidity_features(
                symbol=symbol.upper(),
                mark_price=float(visible_1m[-1][4]),
                order_book=None,
                trades=None,
                klines=visible_1m,
                oi_hist=(oi_slice := self._rows_through_time(oi_hist, anchor_ms, 30)),
                funding_rates=(funding_slice := self._rows_through_time(funding_rates, anchor_ms, 30)),
                basis_rows=(basis_slice := self._rows_through_time(basis_rows, anchor_ms, 30)),
                taker_volume_rows=(taker_slice := self._rows_through_time(taker_volume_rows, anchor_ms, 30)),
                global_ratio_rows=(global_ratio_slice := self._rows_through_time(global_ratio_rows, anchor_ms, 30)),
                top_account_ratio_rows=(top_account_slice := self._rows_through_time(top_account_ratio_rows, anchor_ms, 30)),
                top_position_ratio_rows=(top_position_slice := self._rows_through_time(top_position_ratio_rows, anchor_ms, 30)),
                htf_bias=htf_bias,
                htf_regime=mtf_regime.value if hasattr(mtf_regime, "value") else str(mtf_regime),
            )

            liq_map = None
            if include_stored_liquidation:
                payload = self.liquidation_store.nearest_snapshot(symbol.upper(), anchor_ms)
                if payload:
                    try:
                        liq_map = LiquidationMapAdvancedResponse.model_validate(payload)
                        used_liquidation_snapshots += 1
                    except Exception:
                        liq_map = None

            signal, scoring, ai_refinement = self._build_signal_from_features(
                symbol=symbol.upper(),
                features=features,
                decision_ts=anchor_ms,
                liquidation_map=liq_map,
            )
            tp, sl, risk = compute_tp_sl_with_breakdown(signal.direction, features.mark_price, signal.confidence, features, self.cfg)
            signal.tp = tp
            signal.sl = sl
            explain = SignalExplainResult(
                signal=signal,
                features=features,
                scoring=scoring,
                risk=risk,
                ai_refinement=ai_refinement,
            )
            cumulative = self._historical_cumulative_signal_at(
                symbol=symbol.upper(),
                time_ms=anchor_ms,
                features=features,
                liq_map=liq_map,
                klines_5m=klines_5m,
                klines_15m=klines_15m,
                klines_1h=klines_1h,
            )
            behavior = self._historical_behavior_at(
                time_ms=anchor_ms,
                klines_1h=klines_1h,
                klines_4h=klines_4h,
                klines_12h=klines_12h,
            )
            whale_context = self._whale_proxy_context(
                features=features,
                visible_1m=visible_1m,
                liq_map=liq_map,
            )
            visible_5m = self._closed_klines_at(
                klines_5m,
                time_ms=anchor_ms,
                interval_minutes=5,
                limit=24,
            )
            structure_context = self._structure_context(
                visible_1m=visible_1m,
                visible_5m=visible_5m,
                mark_price=features.mark_price,
            )
            session_context = self._session_context(time_ms=anchor_ms)
            data_quality_context = self._data_quality_context(
                oi_rows=oi_slice,
                funding_rows=funding_slice,
                basis_rows=basis_slice,
                taker_rows=taker_slice,
                global_ratio_rows=global_ratio_slice,
                top_account_rows=top_account_slice,
                top_position_rows=top_position_slice,
                used_liquidation_snapshot=liq_map is not None,
            )
            took_trade = (
                cumulative.bot_signal.direction != Direction.FLAT
                and cumulative.aggregate_confidence >= self.cfg.min_confidence
                and cumulative.bot_signal.signal_quality != "LOW"
            )
            decision_action = (
                f"historical_entry_candidate_{cumulative.bot_signal.direction.value.lower()}"
                if took_trade
                else "historical_wait"
            )

            snapshot_id = self.capture_training_snapshot(
                symbol=symbol.upper(),
                exchange="BINANCE",
                environment="HISTORICAL",
                session_id="historical-backfill",
                mode=cumulative.strategy_mode,
                explain=explain,
                cumulative=cumulative,
                liq_map=liq_map,
                behavior=behavior,
                bot_signal=cumulative.bot_signal,
                open_position=None,
                ai_decision=None,
                decision_action=decision_action,
                took_trade=took_trade,
                snapshot_id=f"hist_{symbol.lower()}_{anchor_ms}",
                auto_resolve_live=False,
                whale_context=whale_context,
                structure_context=structure_context,
                session_context=session_context,
                data_quality_context=data_quality_context,
            )

            for raw_label in self.liquidation_store.load_training_labels_for_snapshot(snapshot_id):
                label = TrainingSnapshotLabel.model_validate(raw_label)
                path = self._klines_in_open_time_range(
                    klines_1m, label.event_ts, label.expires_at
                )
                updated = self._resolve_training_label_from_path(label, path, now_ms=min(now_ms, label.expires_at))
                if updated is None:
                    continue
                updated.raw_payload["resolved_by"] = "triple_barrier_historical_backfill"
                self.liquidation_store.persist_training_label(updated)
                resolved_labels += 1

            snapshots_created += 1

        if not anchors:
            notes.append("No eligible anchors were found in the selected historical window.")
        return HistoricalTrainingBackfillResponse(
            symbol=symbol.upper(),
            lookback_hours=lookback_hours,
            step_minutes=step_minutes,
            samples_attempted=samples_attempted,
            snapshots_created=snapshots_created,
            resolved_labels=resolved_labels,
            skipped_samples=skipped_samples,
            used_liquidation_snapshots=used_liquidation_snapshots,
            notes=notes,
        )

    def backfill_historical_training_batch(
        self,
        symbols: list[str],
        *,
        lookback_hours: int = 24,
        step_minutes: int = 5,
        max_samples_per_symbol: int = 400,
        include_stored_liquidation: bool = True,
        include_historical_positioning: bool = True,
    ) -> HistoricalTrainingBackfillBatchResponse:
        normalized = [symbol.upper() for symbol in symbols if str(symbol).strip()]
        items: list[HistoricalTrainingBackfillBatchItem] = []
        for symbol in normalized:
            try:
                result = self.backfill_historical_training(
                    symbol,
                    lookback_hours=lookback_hours,
                    step_minutes=step_minutes,
                    max_samples=max_samples_per_symbol,
                    include_stored_liquidation=include_stored_liquidation,
                    include_historical_positioning=include_historical_positioning,
                )
                items.append(
                    HistoricalTrainingBackfillBatchItem(
                        symbol=symbol,
                        samples_attempted=result.samples_attempted,
                        snapshots_created=result.snapshots_created,
                        resolved_labels=result.resolved_labels,
                        skipped_samples=result.skipped_samples,
                        used_liquidation_snapshots=result.used_liquidation_snapshots,
                        notes=result.notes,
                    )
                )
            except Exception as exc:
                items.append(
                    HistoricalTrainingBackfillBatchItem(
                        symbol=symbol,
                        samples_attempted=0,
                        snapshots_created=0,
                        resolved_labels=0,
                        skipped_samples=0,
                        used_liquidation_snapshots=0,
                        notes=[f"backfill failed: {exc}"],
                    )
                )
        return HistoricalTrainingBackfillBatchResponse(
            symbols=normalized,
            lookback_hours=lookback_hours,
            step_minutes=step_minutes,
            total_samples_attempted=sum(item.samples_attempted for item in items),
            total_snapshots_created=sum(item.snapshots_created for item in items),
            total_resolved_labels=sum(item.resolved_labels for item in items),
            total_skipped_samples=sum(item.skipped_samples for item in items),
            total_used_liquidation_snapshots=sum(item.used_liquidation_snapshots for item in items),
            items=items,
        )

    def export_lora_training_dataset(
        self,
        symbol: str,
        *,
        horizon_minutes: int = 15,
        limit: int = 1000,
        decision_action: str | None = None,
        balance_mode: str = "none",
    ) -> LoraTrainingExportResponse:
        rows = self.liquidation_store.load_lora_training_rows(
            symbol.upper(),
            horizon_minutes=horizon_minutes,
            limit=limit,
            decision_action=decision_action,
        )
        raw_examples = [
            build_lora_example(
                TrainingExampleBundle(
                    snapshot=row["snapshot"],
                    decision=row["decision"],
                    label=row["label"],
                )
            )
            for row in rows
        ]
        split_examples = chronological_split_examples(raw_examples)
        examples = (
            rebalance_examples_by_split(split_examples, balance_mode=balance_mode)
            if len(split_examples) >= 30
            else rebalance_examples(split_examples, balance_mode=balance_mode)
        )
        summary = LoraTrainingExportSummary(
            symbol=symbol.upper(),
            horizon_minutes=horizon_minutes,
            balance_mode=balance_mode,
            raw_examples=len(raw_examples),
            raw_long_examples=sum(1 for row in raw_examples if row.label_action == Direction.LONG),
            raw_short_examples=sum(1 for row in raw_examples if row.label_action == Direction.SHORT),
            raw_flat_examples=sum(1 for row in raw_examples if row.label_action == Direction.FLAT),
            total_examples=len(examples),
            long_examples=sum(1 for row in examples if row.label_action == Direction.LONG),
            short_examples=sum(1 for row in examples if row.label_action == Direction.SHORT),
            flat_examples=sum(1 for row in examples if row.label_action == Direction.FLAT),
            train_examples=sum(1 for row in examples if row.split == "train"),
            validation_examples=sum(1 for row in examples if row.split == "validation"),
            test_examples=sum(1 for row in examples if row.split == "test"),
            class_weights=class_weights(examples),
        )
        return LoraTrainingExportResponse(
            symbol=symbol.upper(),
            horizon_minutes=horizon_minutes,
            summary=summary,
            examples=examples,
        )

    def start_liquidation_watchlist(self, symbols: list[str], interval_seconds: int = 60) -> None:
        normalized = [symbol.upper() for symbol in symbols]
        for symbol in normalized:
            self.liquidation_runtime.ensure_symbol(symbol)

        if self._watchlist_thread and self._watchlist_thread.is_alive():
            return

        def _loop() -> None:
            while not self._watchlist_stop.is_set():
                for symbol in normalized:
                    if self._watchlist_stop.is_set():
                        break
                    try:
                        self.generate_liquidation_map_advanced(
                            symbol=symbol,
                            include_events=True,
                            event_limit=100,
                            range_pct=12.0,
                            resolution=48,
                            history_points=20,
                        )
                    except Exception:
                        continue
                self._watchlist_stop.wait(interval_seconds)

        self._watchlist_stop.clear()
        self._watchlist_thread = threading.Thread(target=_loop, daemon=True, name="liquidation-watchlist")
        self._watchlist_thread.start()

    def _apply_liquidation_overlay(
        self,
        direction: Direction,
        confidence: float,
        reasons: list[str],
        liq_map: LiquidationMapAdvancedResponse,
    ) -> tuple[Direction, float, list[str]]:
        updated_reasons = list(reasons)
        liq_conf = max(0.0, min(liq_map.confidence, 1.0))

        quality = getattr(liq_map, "quality", None)
        if quality is not None and getattr(quality, "degraded_mode", False):
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

    def _apply_ai_refinement(
        self,
        direction: Direction,
        confidence: float,
        reasons: list[str],
        ai_direction: Direction,
        ai_confidence: float,
    ) -> tuple[Direction, float, list[str]]:
        updated_reasons = list(reasons)

        if ai_direction == Direction.FLAT:
            if direction != Direction.FLAT and ai_confidence + 0.05 < confidence:
                reduced_confidence = max(0.0, confidence - 0.08)
                updated_reasons.append("AI refinement reduced conviction")
                if reduced_confidence < self.cfg.min_confidence:
                    updated_reasons.append("Signal flattened after AI confidence review")
                    return Direction.FLAT, reduced_confidence, updated_reasons
                return direction, reduced_confidence, updated_reasons
            return direction, confidence, updated_reasons

        if direction == Direction.FLAT:
            if ai_confidence >= max(self.cfg.min_confidence, 0.62):
                updated_reasons.append("AI refinement surfaced aligned multi-factor setup")
                return ai_direction, ai_confidence, updated_reasons
            return direction, confidence, updated_reasons

        if ai_direction == direction:
            updated_reasons.append("AI refinement confirmed signal quality")
            boosted_confidence = max(confidence, min(1.0, (confidence * 0.6) + (ai_confidence * 0.4) + 0.03))
            return direction, boosted_confidence, updated_reasons

        updated_reasons.append("AI refinement found conflicting cross-factor context")
        reduced_confidence = max(0.0, confidence - 0.12)
        if reduced_confidence < max(self.cfg.min_confidence - 0.05, 0.45):
            updated_reasons.append("Signal flattened after AI conflict check")
            return Direction.FLAT, reduced_confidence, updated_reasons
        return direction, reduced_confidence, updated_reasons

    def _signal_quality(self, direction: Direction, confidence: float, reasons: list[str]) -> str:
        if direction == Direction.FLAT or confidence < self.cfg.min_confidence:
            return "LOW"
        if confidence >= 0.75 and len(reasons) >= 4:
            return "HIGH"
        if confidence >= 0.65:
            return "ELEVATED"
        return "STANDARD"

    def _build_signal_features(self, symbol: str) -> tuple[LiquidityFeatures, int]:
        decision_ts = int(time.time() * 1000)
        mark_info = self.client.get_mark_price_info(symbol)
        mark_price = float(mark_info["markPrice"])
        order_book = self.client.get_order_book(symbol, limit=self.cfg.order_book_levels)
        trades = self.client.get_recent_trades(symbol, limit=self.cfg.recent_trade_limit)
        klines = self.client.get_recent_klines(symbol, interval="1m", limit=self.cfg.short_kline_limit)

        try:
            oi_hist = self.client.get_open_interest_hist(symbol, period="5m", limit=30)
        except Exception:
            oi_hist = []
        try:
            funding_rates = self.client.get_funding_rates(symbol, limit=30)
        except Exception:
            funding_rates = []
        try:
            basis_rows = self.client.get_basis(symbol, period="5m", limit=30)
        except Exception:
            basis_rows = []
        try:
            taker_volume_rows = self.client.get_taker_buy_sell_volume(symbol, period="5m", limit=30)
        except Exception:
            taker_volume_rows = []
        try:
            global_ratio_rows = self.client.get_global_long_short_account_ratio(symbol, period="5m", limit=30)
        except Exception:
            global_ratio_rows = []
        try:
            top_account_ratio_rows = self.client.get_top_long_short_account_ratio(symbol, period="5m", limit=30)
        except Exception:
            top_account_ratio_rows = []
        try:
            top_position_ratio_rows = self.client.get_top_long_short_position_ratio(symbol, period="5m", limit=30)
        except Exception:
            top_position_ratio_rows = []

        htf_bias = 0.0
        htf_regime = MarketRegime.BALANCED.value
        try:
            mtf_analyzer = MTFRegimeAnalyzer()
            klines_1h = self.client.get_recent_klines(symbol, interval="1h", limit=100)
            klines_4h = self.client.get_recent_klines(symbol, interval="4h", limit=100)
            klines_12h = self.client.get_recent_klines(symbol, interval="12h", limit=100)
            mtf_regime, htf_bias, _ = mtf_analyzer.analyze(klines_1h, klines_4h, klines_12h)
            htf_regime = mtf_regime.value if hasattr(mtf_regime, "value") else str(mtf_regime)
        except Exception:
            htf_bias = 0.0
            htf_regime = MarketRegime.BALANCED.value

        features = build_liquidity_features(
            symbol=symbol,
            mark_price=mark_price,
            order_book=order_book,
            trades=trades,
            klines=klines,
            oi_hist=oi_hist,
            funding_rates=funding_rates,
            basis_rows=basis_rows,
            taker_volume_rows=taker_volume_rows,
            global_ratio_rows=global_ratio_rows,
            top_account_ratio_rows=top_account_ratio_rows,
            top_position_ratio_rows=top_position_ratio_rows,
            htf_bias=htf_bias,
            htf_regime=htf_regime,
        )
        return features, decision_ts

    def _build_signal_from_features(
        self,
        *,
        symbol: str,
        features: LiquidityFeatures,
        decision_ts: int,
        liquidation_map: LiquidationMapResponse | LiquidationMapAdvancedResponse | None = None,
    ) -> tuple[SignalResult, ScoringBreakdown, AIRefinementBreakdown]:
        direction, confidence, reasons, scoring = score_features_with_breakdown(features, self.cfg)
        ai_direction, ai_confidence, ai_refinement = refine_signal_with_ai(features, scoring, self.cfg)
        direction, confidence, reasons = self._apply_ai_refinement(
            direction, confidence, reasons, ai_direction, ai_confidence
        )

        if liquidation_map is not None:
            direction, confidence, reasons = self._apply_liquidation_overlay(
                direction,
                confidence,
                reasons,
                liquidation_map,
            )

        tp, sl = compute_tp_sl(direction, features.mark_price, confidence, features, self.cfg)
        signal = SignalResult(
            symbol=symbol,
            direction=direction,
            confidence=confidence,
            current_price=features.mark_price,
            tp=tp,
            sl=sl,
            reasons=reasons,
            horizon=self.cfg.signal_horizon,
            decision_ts=decision_ts,
            expires_at=decision_ts + (self.cfg.signal_ttl_seconds * 1000),
            entry_assumption="mark",
            model_version=self.cfg.model_version,
            feature_version=self.cfg.feature_version,
            signal_quality=self._signal_quality(direction, confidence, reasons),
        )
        return signal, scoring, ai_refinement

    def _row_timestamp(self, row: dict[str, Any]) -> int:
        for key in ("timestamp", "fundingTime", "time", "T", "ts"):
            if key in row:
                try:
                    return int(row[key])
                except (TypeError, ValueError):
                    return 0
        return 0

    @staticmethod
    def _kline_close_time(row: list[Any], interval_minutes: int) -> int:
        """Return the first millisecond after a candle is fully known."""
        if len(row) > 6:
            try:
                return int(row[6]) + 1
            except (TypeError, ValueError):
                pass
        return int(row[0]) + (interval_minutes * 60_000)

    def _closed_klines_at(
        self,
        rows: list[list[Any]],
        *,
        time_ms: int,
        interval_minutes: int,
        limit: int | None = None,
    ) -> list[list[Any]]:
        if not rows:
            return []
        latest_open_time = time_ms - (interval_minutes * 60_000)
        end = bisect_right(rows, latest_open_time, key=lambda row: int(row[0]))
        start = max(0, end - limit) if limit else 0
        return rows[start:end]

    @staticmethod
    def _klines_in_open_time_range(
        rows: list[list[Any]], start_ms: int, end_ms: int
    ) -> list[list[Any]]:
        start = bisect_left(rows, start_ms, key=lambda row: int(row[0]))
        # end_ms is the decision horizon boundary. A candle opening exactly at
        # that timestamp closes after the horizon and must not enter the label.
        end = bisect_left(rows, end_ms, key=lambda row: int(row[0]))
        return rows[start:end]

    def _rows_through_time(self, rows: list[dict[str, Any]], time_ms: int, limit: int) -> list[dict[str, Any]]:
        visible = [row for row in rows if self._row_timestamp(row) <= time_ms]
        if limit <= 0:
            return visible
        return visible[-limit:]

    def _mtf_context_at(
        self,
        analyzer: MTFRegimeAnalyzer,
        *,
        time_ms: int,
        klines_1h: list[list[Any]],
        klines_4h: list[list[Any]],
        klines_12h: list[list[Any]],
    ) -> tuple[MarketRegime, float]:
        visible_1h = self._closed_klines_at(klines_1h, time_ms=time_ms, interval_minutes=60)
        visible_4h = self._closed_klines_at(klines_4h, time_ms=time_ms, interval_minutes=240)
        visible_12h = self._closed_klines_at(klines_12h, time_ms=time_ms, interval_minutes=720)
        if len(visible_1h) < 21 or len(visible_4h) < 21 or len(visible_12h) < 21:
            return MarketRegime.BALANCED, 0.0
        regime, bias, _ = analyzer.analyze(visible_1h[-100:], visible_4h[-100:], visible_12h[-100:])
        return regime, bias

    def _local_time_string(self, timestamp_ms: int) -> str:
        return datetime.fromtimestamp(timestamp_ms / 1000).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    def _is_volume_spike_from_klines(self, klines: list[list[Any]]) -> bool:
        if len(klines) < 3:
            return False
        volumes = [float(row[5]) for row in klines]
        avg_vol = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else 1.0
        return volumes[-1] >= avg_vol * 1.5

    def _avg_candle_range_bps(self, klines: list[list[Any]], mark_price: float) -> float:
        if not klines or mark_price <= 0.0:
            return 0.0
        ranges = [abs(float(row[2]) - float(row[3])) for row in klines[-20:]]
        if not ranges:
            return 0.0
        return (sum(ranges) / len(ranges) / mark_price) * 10_000

    def _execution_levels(
        self,
        signal: SignalResult,
        market_regime: MarketRegime,
        regime_bias: float,
        klines: list[list[Any]],
    ) -> tuple[float, float]:
        mark_price = max(signal.current_price, 1e-9)
        signal_stop_bps = abs(mark_price - signal.sl) / mark_price * 10_000
        signal_tp_bps = abs(signal.tp - mark_price) / mark_price * 10_000
        range_bps = max(self._avg_candle_range_bps(klines, mark_price), 10.0)
        aligned_with_regime = (
            (signal.direction == Direction.LONG and regime_bias >= 0.2)
            or (signal.direction == Direction.SHORT and regime_bias <= -0.2)
            or market_regime in (MarketRegime.BALANCED, MarketRegime.MIXED_UP, MarketRegime.MIXED_DOWN)
        )
        rr = 1.45 if aligned_with_regime else 1.20
        stop_bps = max(signal_stop_bps, range_bps * 0.90, 20.0)
        tp_bps = max(signal_tp_bps, stop_bps * rr)

        if signal.direction == Direction.LONG:
            return mark_price * (1.0 + tp_bps / 10_000), mark_price * (1.0 - stop_bps / 10_000)
        return mark_price * (1.0 - tp_bps / 10_000), mark_price * (1.0 + stop_bps / 10_000)

    def _paper_status_snapshot(self) -> PaperBotStatus:
        with self._paper_bot_lock:
            return PaperBotStatus.model_validate(self._paper_bot_status.model_dump(mode="json"))

    def _set_paper_status(self, status: PaperBotStatus) -> None:
        with self._paper_bot_lock:
            self._paper_bot_status = status
        self.liquidation_store.persist_paper_bot_status(status)

    def get_paper_bot_status(self, symbol: str = "BTCUSDT") -> PaperBotStatus:
        current = self._paper_status_snapshot()
        if current.symbol.upper() == symbol.upper():
            current.recent_trades = [
                BotTrade.model_validate(trade)
                for trade in self.liquidation_store.load_recent_paper_bot_trades(symbol.upper(), limit=50)
            ]
            return current

        stored = self.liquidation_store.load_latest_paper_bot_status(symbol.upper())
        if stored:
            status = PaperBotStatus.model_validate(stored)
            status.recent_trades = [
                BotTrade.model_validate(trade)
                for trade in self.liquidation_store.load_recent_paper_bot_trades(symbol.upper(), limit=50)
            ]
            return status

        return PaperBotStatus(
            symbol=symbol.upper(),
            running=False,
            poll_interval_seconds=60,
            mode="BALANCED",
            leverage=1.0,
            initial_capital=1000.0,
            current_capital=1000.0,
            realized_pnl=0.0,
            signal_source=self.PAPER_SIGNAL_SOURCE,
            recent_trades=[],
        )

    def _paper_mode_profile(self, mode: str) -> dict[str, Any]:
        normalized = str(mode or "BALANCED").upper()
        if normalized == "CONSERVATIVE":
            return {
                "mode": normalized,
                "min_confidence": 0.70,
                "allowed_qualities": {"HIGH", "ELEVATED"},
                "countertrend_confidence": 0.84,
                "require_htf_alignment": True,
            }
        if normalized == "AGGRESSIVE":
            return {
                "mode": normalized,
                "min_confidence": 0.56,
                "allowed_qualities": {"HIGH", "ELEVATED", "STANDARD"},
                "countertrend_confidence": 0.74,
                "require_htf_alignment": False,
            }
        return {
            "mode": "BALANCED",
            "min_confidence": 0.60,
            "allowed_qualities": {"HIGH", "ELEVATED"},
            "countertrend_confidence": 0.78,
            "require_htf_alignment": False,
        }

    def _paper_liquidation_price(self, side: Direction, entry_price: float, leverage: float) -> float | None:
        if entry_price <= 0.0 or leverage <= 1.0:
            return None
        maintenance_margin_rate = 0.005
        if side == Direction.LONG:
            return entry_price * max(0.0, 1.0 - (1.0 / leverage) + maintenance_margin_rate)
        if side == Direction.SHORT:
            return entry_price * (1.0 + (1.0 / leverage) - maintenance_margin_rate)
        return None

    def _paper_should_enter(
        self,
        signal: SignalResult,
        regime: MarketRegime,
        regime_bias: float,
        htf_dir: Direction,
        use_htf_filter: bool,
        mode: str,
    ) -> bool:
        profile = self._paper_mode_profile(mode)
        if signal.direction == Direction.FLAT:
            return False
        if "no_edge" in signal.reasons:
            return False
        if signal.signal_quality not in profile["allowed_qualities"]:
            return False
        if signal.confidence < max(self.cfg.min_confidence, profile["min_confidence"]):
            return False
        if (
            use_htf_filter
            and profile["require_htf_alignment"]
            and htf_dir != Direction.FLAT
            and htf_dir != signal.direction
        ):
            return False
        if use_htf_filter and htf_dir != Direction.FLAT and htf_dir != signal.direction and abs(regime_bias) >= 0.25:
            return False
        if regime in (MarketRegime.STRONG_UPTREND, MarketRegime.UPTREND) and signal.direction == Direction.SHORT:
            return signal.confidence >= profile["countertrend_confidence"]
        if regime in (MarketRegime.STRONG_DOWNTREND, MarketRegime.DOWNTREND) and signal.direction == Direction.LONG:
            return signal.confidence >= profile["countertrend_confidence"]
        return True

    def _paper_mark_to_market(self, position: dict[str, Any], current_price: float) -> tuple[float, float]:
        side_sign = 1.0 if position["side"] == Direction.LONG else -1.0
        unrealized = position["qty"] * (current_price - position["entry_price"]) * side_sign
        notional = max(position["qty"] * position["entry_price"], 1e-9)
        roi_pct = (unrealized / notional) * 100.0
        return unrealized, roi_pct

    def _close_paper_position(
        self,
        *,
        status: PaperBotStatus,
        position: dict[str, Any],
        current_price: float,
        fee_rate: float,
        reason: str,
        symbol: str,
        now_ms: int,
    ) -> PaperBotStatus:
        side_sign = 1.0 if position["side"] == Direction.LONG else -1.0
        gross = position["qty"] * (current_price - position["entry_price"]) * side_sign
        exit_fee = position["qty"] * current_price * fee_rate
        status.current_capital += gross - exit_fee
        status.fees_paid += exit_fee
        net = gross - position.get("entry_fee", 0.0) - exit_fee + position.get("funding", 0.0)
        trade = BotTrade(
            run_id=status.session_id,
            side=position["side"],
            entry_time=position["entry_time"],
            exit_time=now_ms,
            entry_time_local=self._local_time_string(position["entry_time"]),
            exit_time_local=self._local_time_string(now_ms),
            entry_price=position["entry_price"],
            exit_price=current_price,
            qty=position["qty"],
            duration_minutes=max((now_ms - position["entry_time"]) / 60000.0, 0.0),
            roi_pct=(net / max(position["qty"] * position["entry_price"], 1e-9)) * 100.0,
            gross_pnl=gross,
            fee_paid=position.get("entry_fee", 0.0) + exit_fee,
            funding_paid=position.get("funding", 0.0),
            net_pnl=net,
            entry_reason=position.get("entry_reason", ""),
            exit_reason=reason,
            entry_signal_direction=position["side"],
            entry_signal_confidence=position.get("entry_signal_confidence", 0.0),
            entry_signal_quality=position.get("entry_signal_quality", "UNKNOWN"),
            signal_horizon=position.get("signal_horizon", "N/A"),
            signal_source=position.get("signal_source", self.PAPER_SIGNAL_SOURCE),
            reason=reason,
        )
        status.recent_trades = [trade] + list(status.recent_trades[:49])
        status.realized_pnl += net
        status.total_trades += 1
        wins = len([row for row in status.recent_trades if row.net_pnl > 0])
        status.win_rate = wins / max(status.total_trades, 1)
        status.unrealized_pnl = 0.0
        status.open_position = None
        status.last_action = f"closed_{reason}"
        self.liquidation_store.persist_paper_bot_trade(
            status.session_id or "",
            symbol.upper(),
            status.total_trades,
            trade,
        )
        self._paper_bot_position_state = None
        return status

    def _run_paper_bot_cycle(
        self,
        *,
        symbol: str,
        fee_rate: float,
        leverage: float,
        use_cumulative_signal: bool,
        use_htf_filter: bool,
        use_volume_filter: bool,
        mode: str,
    ) -> None:
        status = self._paper_status_snapshot()
        if not status.running or status.symbol.upper() != symbol.upper():
            return

        now_ms = int(time.time() * 1000)
        if status.scheduled_end_at and now_ms >= status.scheduled_end_at:
            status.running = False
            status.stopped_at = now_ms
            status.last_action = "scheduled_end_reached"
            self._set_paper_status(status)
            return

        if use_cumulative_signal:
            cumulative_signal = self.generate_cumulative_signal(symbol.upper())
            signal = cumulative_signal.bot_signal
        else:
            signal = self.generate_signal(symbol.upper())
        behavior = self.get_market_behavior(symbol.upper())
        regime_name = behavior.get("regime", MarketRegime.BALANCED.value)
        try:
            regime = MarketRegime(regime_name)
        except Exception:
            regime = MarketRegime.BALANCED
        regime_bias = float(behavior.get("bias_strength", 0.0) or 0.0)
        if str(behavior.get("bias", "NEUTRAL")).upper() == "BEARISH":
            regime_bias *= -1.0

        klines = self.client.get_recent_klines(symbol.upper(), interval="1m", limit=60)
        htf_rows = self.client.get_recent_klines(symbol.upper(), interval="15m", limit=40)
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
        htf_dir = Direction.FLAT
        if len(htf_candles) >= 13:
            closes = [c.close for c in htf_candles[-13:]]
            start = closes[0]
            end = closes[-1]
            if start > 0:
                ret_bps = ((end - start) / start) * 10_000
                if ret_bps >= 12.0:
                    htf_dir = Direction.LONG
                elif ret_bps <= -12.0:
                    htf_dir = Direction.SHORT

        current_price = float(self.client.get_mark_price_info(symbol.upper())["markPrice"])
        status.last_signal = signal
        status.last_updated_at = now_ms

        position = self._paper_bot_position_state
        if position is None and status.open_position is not None:
            position = {
                "side": status.open_position.side,
                "entry_time": status.open_position.entry_time,
                "entry_price": status.open_position.entry_price,
                "qty": status.open_position.qty,
                "leverage": status.open_position.leverage,
                "tp_price": status.open_position.tp_price,
                "sl_price": status.open_position.sl_price,
                "entry_reason": status.open_position.entry_reason,
                "entry_signal_confidence": status.open_position.entry_signal_confidence,
                "entry_signal_quality": status.open_position.entry_signal_quality,
                "signal_horizon": status.open_position.signal_horizon,
                "signal_source": status.open_position.signal_source,
                "entry_fee": 0.0,
                "funding": 0.0,
            }

        if position is not None:
            unrealized, unrealized_roi_pct = self._paper_mark_to_market(position, current_price)
            status.unrealized_pnl = unrealized
            status.open_position = PaperBotPosition(
                side=position["side"],
                entry_time=position["entry_time"],
                entry_time_local=self._local_time_string(position["entry_time"]),
                entry_price=position["entry_price"],
                qty=position["qty"],
                leverage=position.get("leverage", status.leverage),
                tp_price=position["tp_price"],
                sl_price=position["sl_price"],
                liquidation_price=self._paper_liquidation_price(
                    position["side"],
                    position["entry_price"],
                    position.get("leverage", status.leverage),
                ),
                entry_reason=position.get("entry_reason", ""),
                entry_signal_confidence=position.get("entry_signal_confidence", 0.0),
                entry_signal_quality=position.get("entry_signal_quality", "UNKNOWN"),
                signal_horizon=position.get("signal_horizon", "N/A"),
                signal_source=position.get("signal_source", self.PAPER_SIGNAL_SOURCE),
                current_price=current_price,
                unrealized_pnl=unrealized,
                unrealized_roi_pct=unrealized_roi_pct,
            )
            if position["side"] == Direction.LONG and current_price <= position["sl_price"]:
                status = self._close_paper_position(
                    status=status,
                    position=position,
                    current_price=current_price,
                    fee_rate=fee_rate,
                    reason="stop_loss",
                    symbol=symbol,
                    now_ms=now_ms,
                )
                position = None
            elif position["side"] == Direction.LONG and current_price >= position["tp_price"]:
                status = self._close_paper_position(
                    status=status,
                    position=position,
                    current_price=current_price,
                    fee_rate=fee_rate,
                    reason="take_profit",
                    symbol=symbol,
                    now_ms=now_ms,
                )
                position = None
            elif position["side"] == Direction.SHORT and current_price >= position["sl_price"]:
                status = self._close_paper_position(
                    status=status,
                    position=position,
                    current_price=current_price,
                    fee_rate=fee_rate,
                    reason="stop_loss",
                    symbol=symbol,
                    now_ms=now_ms,
                )
                position = None
            elif position["side"] == Direction.SHORT and current_price <= position["tp_price"]:
                status = self._close_paper_position(
                    status=status,
                    position=position,
                    current_price=current_price,
                    fee_rate=fee_rate,
                    reason="take_profit",
                    symbol=symbol,
                    now_ms=now_ms,
                )
                position = None
            elif signal.direction != Direction.FLAT and signal.direction != position["side"] and signal.confidence >= 0.60:
                status = self._close_paper_position(
                    status=status,
                    position=position,
                    current_price=current_price,
                    fee_rate=fee_rate,
                    reason="signal_flip",
                    symbol=symbol,
                    now_ms=now_ms,
                )
                position = None

        if (
            position is None
            and self._paper_should_enter(signal, regime, regime_bias, htf_dir, use_htf_filter, mode)
            and (not use_volume_filter or self._is_volume_spike_from_klines(klines))
        ):
            trade_notional = max(status.current_capital, 0.0) * leverage
            if trade_notional > 0:
                qty = trade_notional / current_price
                entry_fee = trade_notional * fee_rate
                status.current_capital -= entry_fee
                status.fees_paid += entry_fee
                tp_price, sl_price = self._execution_levels(signal, regime, regime_bias, klines)
                position = {
                    "side": signal.direction,
                    "entry_time": now_ms,
                    "entry_price": current_price,
                    "qty": qty,
                    "leverage": leverage,
                    "tp_price": tp_price,
                    "sl_price": sl_price,
                    "entry_reason": f"mode={mode.upper()} | signal={signal.direction.value} | confidence={signal.confidence:.2f} | quality={signal.signal_quality} | regime={regime.value} | reasons={','.join(signal.reasons[:4])}",
                    "entry_signal_confidence": signal.confidence,
                    "entry_signal_quality": signal.signal_quality,
                    "signal_horizon": signal.horizon,
                    "signal_source": self.PAPER_SIGNAL_SOURCE,
                    "entry_fee": entry_fee,
                    "funding": 0.0,
                }
                status.open_position = PaperBotPosition(
                    side=signal.direction,
                    entry_time=now_ms,
                    entry_time_local=self._local_time_string(now_ms),
                    entry_price=current_price,
                    qty=qty,
                    leverage=leverage,
                    tp_price=tp_price,
                    sl_price=sl_price,
                    liquidation_price=self._paper_liquidation_price(signal.direction, current_price, leverage),
                    entry_reason=position["entry_reason"],
                    entry_signal_confidence=signal.confidence,
                    entry_signal_quality=signal.signal_quality,
                    signal_horizon=signal.horizon,
                    signal_source=self.PAPER_SIGNAL_SOURCE,
                    current_price=current_price,
                    unrealized_pnl=0.0,
                    unrealized_roi_pct=0.0,
                )
                status.last_action = f"opened_{signal.direction.value.lower()}"
        elif position is None and status.last_action.startswith("closed_"):
            pass
        else:
            status.last_action = "monitoring"

        self._paper_bot_position_state = position
        self._set_paper_status(status)

    def start_paper_bot(
        self,
        symbol: str = "BTCUSDT",
        run_minutes: int = 60,
        poll_interval_seconds: int = 60,
        initial_capital: float = 1000.0,
        fee_rate: float = 0.0004,
        leverage: float = 1.0,
        use_cumulative_signal: bool = False,
        use_htf_filter: bool = True,
        use_volume_filter: bool = False,
        mode: str = "BALANCED",
    ) -> PaperBotStatus:
        current = self._paper_status_snapshot()
        if current.running:
            return current

        session_id = uuid4().hex
        now_ms = int(time.time() * 1000)
        scheduled_end_at = now_ms + (run_minutes * 60 * 1000) if run_minutes > 0 else None
        status = PaperBotStatus(
            session_id=session_id,
            symbol=symbol.upper(),
            running=True,
            started_at=now_ms,
            scheduled_end_at=scheduled_end_at,
            poll_interval_seconds=poll_interval_seconds,
            mode=self._paper_mode_profile(mode)["mode"],
            leverage=leverage,
            initial_capital=initial_capital,
            current_capital=initial_capital,
            realized_pnl=0.0,
            signal_source=self.PAPER_SIGNAL_SOURCE,
            last_action="started",
            last_updated_at=now_ms,
            recent_trades=[],
        )
        self._paper_bot_position_state = None
        self._paper_bot_stop.clear()
        self._paper_bot_fee_rate = fee_rate
        self._set_paper_status(status)

        def _loop() -> None:
            while not self._paper_bot_stop.is_set():
                try:
                    self._run_paper_bot_cycle(
                        symbol=symbol.upper(),
                        fee_rate=fee_rate,
                        leverage=leverage,
                        use_cumulative_signal=use_cumulative_signal,
                        use_htf_filter=use_htf_filter,
                        use_volume_filter=use_volume_filter,
                        mode=status.mode,
                    )
                    current_status = self._paper_status_snapshot()
                    if not current_status.running:
                        break
                except Exception as exc:
                    failed = self._paper_status_snapshot()
                    failed.last_action = f"error:{exc}"
                    failed.last_updated_at = int(time.time() * 1000)
                    self._set_paper_status(failed)
                self._paper_bot_stop.wait(poll_interval_seconds)

            final_status = self._paper_status_snapshot()
            if self._paper_bot_position_state is not None:
                try:
                    close_price = float(self.client.get_mark_price_info(symbol.upper())["markPrice"])
                    final_status = self._close_paper_position(
                        status=final_status,
                        position=self._paper_bot_position_state,
                        current_price=close_price,
                        fee_rate=fee_rate,
                        reason="session_stop",
                        symbol=symbol.upper(),
                        now_ms=int(time.time() * 1000),
                    )
                except Exception:
                    pass
            if final_status.running:
                final_status.running = False
                final_status.stopped_at = int(time.time() * 1000)
                final_status.last_action = "stopped"
                self._set_paper_status(final_status)

        self._paper_bot_thread = threading.Thread(target=_loop, daemon=True, name="paper-bot-runner")
        self._paper_bot_thread.start()
        return self._paper_status_snapshot()

    def stop_paper_bot(self) -> PaperBotStatus:
        self._paper_bot_stop.set()
        if self._paper_bot_thread and self._paper_bot_thread.is_alive():
            self._paper_bot_thread.join(timeout=2.0)
        status = self._paper_status_snapshot()
        if self._paper_bot_position_state is not None:
            try:
                close_price = float(self.client.get_mark_price_info(status.symbol.upper())["markPrice"])
                status = self._close_paper_position(
                    status=status,
                    position=self._paper_bot_position_state,
                    current_price=close_price,
                    fee_rate=self._paper_bot_fee_rate,
                    reason="session_stop",
                    symbol=status.symbol.upper(),
                    now_ms=int(time.time() * 1000),
                )
            except Exception:
                pass
        if status.running:
            status.running = False
            status.stopped_at = int(time.time() * 1000)
            status.last_action = "stopped_by_user"
            self._set_paper_status(status)
        return self._paper_status_snapshot()

    def generate_signal(self, symbol: str = "BTCUSDT") -> SignalResult:
        features, decision_ts = self._build_signal_features(symbol)
        liq_map: LiquidationMapAdvancedResponse | None = None
        try:
            liq_map = self.generate_liquidation_map_advanced(symbol=symbol, include_events=True, event_limit=50)
        except Exception:
            liq_map = None

        signal, _, _ = self._build_signal_from_features(
            symbol=symbol,
            features=features,
            decision_ts=decision_ts,
            liquidation_map=liq_map,
        )
        if liq_map is None:
            signal.reasons = signal.reasons + ["Liquidation overlay unavailable"]
            signal.signal_quality = self._signal_quality(signal.direction, signal.confidence, signal.reasons)
        return signal

    def generate_signal_explain(self, symbol: str = "BTCUSDT") -> SignalExplainResult:
        features, decision_ts = self._build_signal_features(symbol)
        liq_map: LiquidationMapAdvancedResponse | None = None
        try:
            liq_map = self.generate_liquidation_map_advanced(symbol=symbol, include_events=True, event_limit=50)
        except Exception:
            liq_map = None

        signal, scoring, ai_refinement = self._build_signal_from_features(
            symbol=symbol,
            features=features,
            decision_ts=decision_ts,
            liquidation_map=liq_map,
        )
        if liq_map is None:
            signal.reasons = signal.reasons + ["Liquidation overlay unavailable"]
            signal.signal_quality = self._signal_quality(signal.direction, signal.confidence, signal.reasons)
        tp, sl, risk = compute_tp_sl_with_breakdown(
            signal.direction,
            features.mark_price,
            signal.confidence,
            features,
            self.cfg,
        )
        signal.tp = tp
        signal.sl = sl
        return SignalExplainResult(signal=signal, features=features, scoring=scoring, risk=risk, ai_refinement=ai_refinement)

    def generate_liquidation_map(self, symbol: str = "BTCUSDT") -> LiquidationMapAdvancedResponse:
        return self.generate_liquidation_map_advanced(symbol=symbol, include_events=True, event_limit=50)

    def generate_liquidation_map_advanced(
        self,
        symbol: str = "BTCUSDT",
        include_events: bool = True,
        event_limit: int = 50,
        range_pct: float = 10.0,
        resolution: int = 48,
        history_points: int = 20,
        candle_interval: str = "1m",
        candle_limit: int = 120,
    ) -> LiquidationMapAdvancedResponse:
        mark_info = self.client.get_mark_price_info(symbol)
        mark_price = float(mark_info["markPrice"])
        order_book = self.client.get_order_book(symbol, limit=100)
        candle_interval = str(candle_interval or "1m")
        candle_limit = max(20, min(int(candle_limit), 500))
        data_period = candle_interval if candle_interval in {"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"} else "5m"
        stats_limit = max(30, min(candle_limit, 500))

        oi_hist = self.client.get_open_interest_hist(symbol, period=data_period, limit=stats_limit)
        klines = self.client.get_recent_klines(symbol, interval=candle_interval, limit=candle_limit)
        try:
            funding_rates = self.client.get_funding_rates(symbol, limit=max(30, min(candle_limit // 2, 100)))
        except Exception:
            funding_rates = []

        try:
            basis_rows = self.client.get_basis(symbol, period=data_period, limit=stats_limit)
        except Exception:
            basis_rows = []
        try:
            taker_volume_rows = self.client.get_taker_buy_sell_volume(symbol, period=data_period, limit=stats_limit)
        except Exception:
            taker_volume_rows = []
        try:
            global_ratio_rows = self.client.get_global_long_short_account_ratio(symbol, period=data_period, limit=stats_limit)
        except Exception:
            global_ratio_rows = []
        try:
            top_account_ratio_rows = self.client.get_top_long_short_account_ratio(symbol, period=data_period, limit=stats_limit)
        except Exception:
            top_account_ratio_rows = []
        try:
            top_position_ratio_rows = self.client.get_top_long_short_position_ratio(symbol, period=data_period, limit=stats_limit)
        except Exception:
            top_position_ratio_rows = []

        live_events: list[LiquidationEventPoint] = []
        stream_health = []
        degraded_reason: str | None = None
        if include_events:
            try:
                self.liquidation_runtime.ensure_symbol(symbol)
                live_events = self.liquidation_runtime.get_recent_events(symbol=symbol, limit=event_limit)
                stream_health = self.liquidation_runtime.get_stream_health(symbol=symbol)
            except Exception as exc:
                degraded_reason = f"Liquidation stream runtime exception: {exc}"

        now_ms = int(time.time() * 1000)
        calibration = self.liquidation_store.calibration_profile(symbol)
        storage_stats = {
            "persistent_event_count_1h": self.liquidation_store.count_events_since(symbol, now_ms - 60 * 60 * 1000),
            "persistent_event_count_24h": self.liquidation_store.count_events_since(symbol, now_ms - 24 * 60 * 60 * 1000),
            "replay_snapshots_available": len(self.liquidation_store.replay_snapshots(symbol, limit=200)),
            "tile_resolutions_available": [24, 48, 96],
        }

        response = build_liquidation_map_advanced(
            symbol=symbol,
            mark_price=mark_price,
            oi_hist=oi_hist,
            klines=klines,
            funding_rates=funding_rates,
            basis_rows=basis_rows,
            taker_volume_rows=taker_volume_rows,
            global_ratio_rows=global_ratio_rows,
            top_account_ratio_rows=top_account_ratio_rows,
            top_position_ratio_rows=top_position_ratio_rows,
            order_book=order_book,
            mark_price_info=mark_info,
            live_events=live_events,
            include_events=include_events,
            degraded_reason=degraded_reason,
            stream_health=stream_health,
            calibration=calibration,
            storage_stats=storage_stats,
            range_pct=range_pct,
            resolution=resolution,
            history_points=history_points,
        )
        payload = response.model_dump(mode="json")
        self.liquidation_store.persist_snapshot(
            symbol=symbol,
            generated_at=response.generated_at,
            source=response.source,
            range_pct=range_pct,
            resolution=resolution,
            payload=payload,
        )
        for tile in build_multi_resolution_tiles(
            symbol=symbol,
            generated_at=response.generated_at,
            range_pct=range_pct,
            price_levels=response.heatmap_price_levels,
            heatmap=response.heatmap,
            resolutions=[24, 48, 96, resolution],
        ):
            self.liquidation_store.persist_tile(
                symbol=symbol,
                generated_at=response.generated_at,
                range_pct=range_pct,
                resolution=tile["resolution"],
                tile=tile,
            )
        return response

    def generate_liquidation_events(self, symbol: str = "BTCUSDT", limit: int = 50) -> list[LiquidationEventPoint]:
        self.liquidation_runtime.ensure_symbol(symbol)
        live = self.liquidation_runtime.get_recent_events(symbol=symbol, limit=limit)
        if live:
            return live
        return self.liquidation_store.load_recent_events(symbol=symbol, limit=limit)

    def generate_liquidation_tile(
        self,
        symbol: str = "BTCUSDT",
        resolution: int = 48,
        range_pct: float = 12.0,
    ) -> LiquidationTileResponse | None:
        tile = self.liquidation_store.get_latest_tile(symbol=symbol, resolution=resolution, range_pct=range_pct)
        if tile is None:
            self.generate_liquidation_map_advanced(
                symbol=symbol,
                include_events=True,
                event_limit=100,
                range_pct=range_pct,
                resolution=resolution,
                history_points=20,
            )
            tile = self.liquidation_store.get_latest_tile(symbol=symbol, resolution=resolution, range_pct=range_pct)
        return LiquidationTileResponse.model_validate(tile) if tile else None

    def replay_liquidation_map(self, symbol: str = "BTCUSDT", limit: int = 20) -> list[LiquidationReplaySnapshot]:
        snapshots = self.liquidation_store.replay_snapshots(symbol=symbol, limit=limit)
        rows: list[LiquidationReplaySnapshot] = []
        for payload in snapshots:
            rows.append(
                LiquidationReplaySnapshot(
                    symbol=payload["symbol"],
                    generated_at=payload["generated_at"],
                    current_price=payload["current_price"],
                    dominant_pull=payload["dominant_pull"],
                    confidence=payload["confidence"],
                    source=payload["source"],
                    price_range_low=payload["price_range_low"],
                    price_range_high=payload["price_range_high"],
                )
            )
        return rows

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

    def _ema_series(self, values: list[float], period: int) -> list[float]:
        if not values:
            return []
        multiplier = 2.0 / (period + 1.0)
        ema = values[0]
        output: list[float] = []
        for index, value in enumerate(values):
            if index == 0:
                ema = value
            else:
                ema = ((value - ema) * multiplier) + ema
            output.append(float(ema))
        return output

    def _vwap_series(self, candles: list[Candle]) -> list[float]:
        output: list[float] = []
        cumulative_tpv = 0.0
        cumulative_volume = 0.0
        for candle in candles:
            typical_price = (candle.high + candle.low + candle.close) / 3.0
            cumulative_tpv += typical_price * candle.volume
            cumulative_volume += candle.volume
            output.append(cumulative_tpv / max(cumulative_volume, 1e-9))
        return output

    def _rsi_series(self, candles: list[Candle], period: int = 14) -> list[float | None]:
        closes = [candle.close for candle in candles]
        values: list[float | None] = [None for _ in closes]
        if len(closes) <= period:
            return values

        gain_sum = 0.0
        loss_sum = 0.0
        for index in range(1, period + 1):
            delta = closes[index] - closes[index - 1]
            gain_sum += max(delta, 0.0)
            loss_sum += max(-delta, 0.0)

        avg_gain = gain_sum / period
        avg_loss = loss_sum / period
        values[period] = 100.0 if avg_loss == 0.0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))

        for index in range(period + 1, len(closes)):
            delta = closes[index] - closes[index - 1]
            gain = max(delta, 0.0)
            loss = max(-delta, 0.0)
            avg_gain = ((avg_gain * (period - 1)) + gain) / period
            avg_loss = ((avg_loss * (period - 1)) + loss) / period
            values[index] = 100.0 if avg_loss == 0.0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))
        return values

    def _macd_series(self, candles: list[Candle]) -> tuple[list[float], list[float], list[float]]:
        closes = [candle.close for candle in candles]
        ema12 = self._ema_series(closes, 12)
        ema26 = self._ema_series(closes, 26)
        macd = [ema12[index] - ema26[index] for index in range(len(closes))]
        signal = self._ema_series(macd, 9)
        histogram = [macd[index] - signal[index] for index in range(len(macd))]
        return macd, signal, histogram

    def _atr_series(self, candles: list[Candle], period: int = 14) -> list[float]:
        if not candles:
            return []
        true_ranges: list[float] = []
        for index, candle in enumerate(candles):
            prev_close = candles[index - 1].close if index > 0 else candle.close
            tr = max(
                candle.high - candle.low,
                abs(candle.high - prev_close),
                abs(candle.low - prev_close),
            )
            true_ranges.append(tr)

        atr_values: list[float] = []
        atr = true_ranges[0]
        for index, tr in enumerate(true_ranges):
            if index == 0:
                atr = tr
            elif index < period:
                atr = ((atr * index) + tr) / (index + 1)
            else:
                atr = ((atr * (period - 1)) + tr) / period
            atr_values.append(float(atr))
        return atr_values

    def _adx_series(self, candles: list[Candle], period: int = 14) -> list[float | None]:
        if len(candles) < period + 2:
            return [None for _ in candles]

        trs: list[float] = []
        plus_dm: list[float] = []
        minus_dm: list[float] = []
        for index in range(1, len(candles)):
            current = candles[index]
            prev = candles[index - 1]
            up_move = current.high - prev.high
            down_move = prev.low - current.low
            trs.append(
                max(
                    current.high - current.low,
                    abs(current.high - prev.close),
                    abs(current.low - prev.close),
                )
            )
            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)

        smoothed_tr = sum(trs[:period])
        smoothed_plus_dm = sum(plus_dm[:period])
        smoothed_minus_dm = sum(minus_dm[:period])

        dx_values: list[float] = []
        for index in range(period, len(trs)):
            if index > period:
                smoothed_tr = smoothed_tr - (smoothed_tr / period) + trs[index]
                smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dm[index]
                smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dm[index]

            plus_di = 100.0 * (smoothed_plus_dm / max(smoothed_tr, 1e-9))
            minus_di = 100.0 * (smoothed_minus_dm / max(smoothed_tr, 1e-9))
            di_sum = plus_di + minus_di
            dx = 0.0 if di_sum == 0.0 else 100.0 * abs(plus_di - minus_di) / di_sum
            dx_values.append(dx)

        adx_values: list[float | None] = [None for _ in candles]
        if not dx_values:
            return adx_values

        if len(dx_values) >= period:
            adx = sum(dx_values[:period]) / period
            adx_index = (period * 2)
            if adx_index < len(adx_values):
                adx_values[adx_index] = adx
            for index in range(period, len(dx_values)):
                adx = ((adx * (period - 1)) + dx_values[index]) / period
                target_index = index + period + 1
                if target_index < len(adx_values):
                    adx_values[target_index] = adx
        return adx_values

    def _breakout_failure_rate(self, candles: list[Candle], window: int = 24) -> float:
        if len(candles) < window + 3:
            return 0.0
        recent = candles[-(window + 8):]
        attempts = 0
        failures = 0
        for index in range(5, len(recent) - 1):
            prior = recent[max(0, index - 5):index]
            if len(prior) < 5:
                continue
            prior_high = max(candle.high for candle in prior)
            prior_low = min(candle.low for candle in prior)
            candle = recent[index]
            next_close = recent[index + 1].close
            if candle.close > prior_high:
                attempts += 1
                if next_close < prior_high:
                    failures += 1
            elif candle.close < prior_low:
                attempts += 1
                if next_close > prior_low:
                    failures += 1
        return failures / attempts if attempts > 0 else 0.0

    def _classify_market_mode(
        self,
        *,
        close: float,
        ema20: float,
        ema50: float,
        ema200: float,
        vwap: float,
        adx14: float | None,
        atr_pct: float,
        atr_ratio: float,
        breakout_failure_rate: float,
    ) -> tuple[str, float, str]:
        ema_spread_pct = ((abs(ema20 - ema50) + abs(ema50 - ema200)) / max(close, 1e-9)) * 100.0
        vwap_distance_pct = abs(close - vwap) / max(close, 1e-9) * 100.0

        trend_score = 0.0
        range_score = 0.0

        if (ema20 > ema50 > ema200) or (ema20 < ema50 < ema200):
            trend_score += 1.0
        elif ema_spread_pct <= 0.35:
            range_score += 1.0

        if adx14 is not None:
            if adx14 >= 25.0:
                trend_score += 1.2
            elif adx14 <= 18.0:
                range_score += 1.2
            else:
                trend_score += 0.35
                range_score += 0.35

        if atr_ratio >= 1.08:
            trend_score += 0.7
        elif atr_ratio <= 0.94:
            range_score += 0.7

        if vwap_distance_pct >= 0.35:
            trend_score += 0.5
        elif vwap_distance_pct <= 0.16:
            range_score += 0.5

        if breakout_failure_rate >= 0.55:
            range_score += 1.1
        elif breakout_failure_rate <= 0.25:
            trend_score += 1.1

        if atr_pct >= 1.0:
            trend_score += 0.35
        elif atr_pct <= 0.45:
            range_score += 0.35

        if trend_score >= range_score + 0.8:
            return "TRENDING", min(1.0, trend_score / 4.5), "trend-following"
        if range_score >= trend_score + 0.8:
            return "RANGING", min(1.0, range_score / 4.5), "mean-reversion"
        return "TRANSITIONAL", 0.45, "stay-flat"

    def _support_resistance_levels(self, candles: list[Candle]) -> tuple[list[float], list[float]]:
        if len(candles) < 12:
            return [], []
        recent = candles[-90:]
        pivot_highs: list[float] = []
        pivot_lows: list[float] = []

        for index in range(2, len(recent) - 2):
            candle = recent[index]
            prev1 = recent[index - 1]
            prev2 = recent[index - 2]
            next1 = recent[index + 1]
            next2 = recent[index + 2]
            if (
                candle.high >= prev1.high
                and candle.high >= prev2.high
                and candle.high >= next1.high
                and candle.high >= next2.high
            ):
                pivot_highs.append(candle.high)
            if (
                candle.low <= prev1.low
                and candle.low <= prev2.low
                and candle.low <= next1.low
                and candle.low <= next2.low
            ):
                pivot_lows.append(candle.low)

        def _dedupe(levels: list[float], ascending: bool) -> list[float]:
            ordered = sorted(levels, reverse=not ascending)
            deduped: list[float] = []
            for level in ordered:
                too_close = any(abs(level - existing) / max(level, 1e-9) < 0.0035 for existing in deduped)
                if not too_close:
                    deduped.append(level)
            return deduped

        last_close = candles[-1].close
        supports = _dedupe([level for level in pivot_lows if level <= last_close], ascending=False)[:3]
        resistances = _dedupe([level for level in pivot_highs if level >= last_close], ascending=True)[:3]
        return supports, resistances

    def _recent_pattern_signal(self, candles: list[Candle]) -> tuple[Direction, float, str]:
        if len(candles) < 5:
            return Direction.FLAT, 0.0, "Building context"

        source = candles[-40:]
        last = source[-1]
        prev = source[-2]
        third = source[-3]
        last_range = max(last.high - last.low, 1e-9)
        last_body = abs(last.close - last.open)
        baseline_body = sum(abs(candle.close - candle.open) for candle in source[-12:]) / max(len(source[-12:]), 1)

        if (
            prev.close < prev.open
            and last.close > last.open
            and last.open <= prev.close
            and last.close >= prev.open
            and last_body >= baseline_body * 0.9
        ):
            return Direction.LONG, 0.82, "Bullish engulfing"

        if (
            prev.close > prev.open
            and last.close < last.open
            and last.open >= prev.close
            and last.close <= prev.open
            and last_body >= baseline_body * 0.9
        ):
            return Direction.SHORT, 0.82, "Bearish engulfing"

        if (
            (min(last.open, last.close) - last.low) >= last_body * 2.4
            and (last.high - max(last.open, last.close)) <= max(last_body * 0.65, last_range * 0.18)
        ):
            return Direction.LONG, 0.74, "Hammer reversal"

        if (
            (last.high - max(last.open, last.close)) >= last_body * 2.4
            and (min(last.open, last.close) - last.low) <= max(last_body * 0.65, last_range * 0.18)
        ):
            return Direction.SHORT, 0.74, "Shooting star"

        if (
            third.close < third.open
            and abs(prev.close - prev.open) <= baseline_body * 0.75
            and last.close > last.open
            and last.close > ((third.open + third.close) / 2.0)
        ):
            return Direction.LONG, 0.78, "Morning star"

        if (
            third.close > third.open
            and abs(prev.close - prev.open) <= baseline_body * 0.75
            and last.close < last.open
            and last.close < ((third.open + third.close) / 2.0)
        ):
            return Direction.SHORT, 0.78, "Evening star"

        move_window = source[-8:]
        start = move_window[0].close
        move_pct = (((move_window[-1].close - start) / start) * 100.0) if start > 0 else 0.0
        if move_pct >= 1.2:
            return Direction.LONG, min(0.76, 0.56 + (abs(move_pct) / 8.0)), "Trend continuation up"
        if move_pct <= -1.2:
            return Direction.SHORT, min(0.76, 0.56 + (abs(move_pct) / 8.0)), "Trend continuation down"
        return Direction.FLAT, 0.52, "Mixed structure"

    def _cumulative_timeframe_signal(
        self,
        *,
        timeframe: str,
        candles: list[Candle],
        liq_map: LiquidationMapAdvancedResponse | None,
    ) -> CumulativeSignalTimeframe:
        closes = [candle.close for candle in candles]
        ema20 = self._ema_series(closes, 20)
        ema50 = self._ema_series(closes, 50)
        ema200 = self._ema_series(closes, 200)
        vwap = self._vwap_series(candles)
        rsi = self._rsi_series(candles, 14)
        macd, macd_signal, macd_hist = self._macd_series(candles)
        atr = self._atr_series(candles, 14)
        adx = self._adx_series(candles, 14)
        supports, resistances = self._support_resistance_levels(candles)
        pattern_direction, pattern_confidence, pattern_name = self._recent_pattern_signal(candles)

        last = candles[-1]
        avg_volume = sum(candle.volume for candle in candles[-20:]) / max(len(candles[-20:]), 1)
        volume_ratio = last.volume / max(avg_volume, 1e-9)
        latest_rsi = rsi[-1]
        latest_macd = macd[-1]
        latest_macd_signal = macd_signal[-1]
        latest_hist = macd_hist[-1]
        latest_ema20 = ema20[-1]
        latest_ema50 = ema50[-1]
        latest_ema200 = ema200[-1]
        latest_vwap = vwap[-1]
        latest_atr = atr[-1] if atr else 0.0
        latest_adx = adx[-1] if adx else None
        avg_atr = sum(atr[-30:]) / max(len(atr[-30:]), 1) if atr else 0.0
        atr_ratio = latest_atr / max(avg_atr, 1e-9) if avg_atr > 0.0 else 1.0
        atr_pct = (latest_atr / max(last.close, 1e-9)) * 100.0
        breakout_failure_rate = self._breakout_failure_rate(candles, window=24)
        market_mode, regime_strength, strategy_mode = self._classify_market_mode(
            close=last.close,
            ema20=latest_ema20,
            ema50=latest_ema50,
            ema200=latest_ema200,
            vwap=latest_vwap,
            adx14=latest_adx,
            atr_pct=atr_pct,
            atr_ratio=atr_ratio,
            breakout_failure_rate=breakout_failure_rate,
        )

        long_score = 0.0
        short_score = 0.0
        reasons: list[str] = []
        max_score = 8.75

        if last.close > latest_ema20 > latest_ema50:
            long_score += 1.35
            reasons.append("price above EMA 20/50")
        elif last.close < latest_ema20 < latest_ema50:
            short_score += 1.35
            reasons.append("price below EMA 20/50")

        if last.close > latest_ema200 and latest_ema50 >= latest_ema200:
            long_score += 0.85
            reasons.append("higher timeframe trend above EMA 200")
        elif last.close < latest_ema200 and latest_ema50 <= latest_ema200:
            short_score += 0.85
            reasons.append("higher timeframe trend below EMA 200")

        if last.close > latest_vwap:
            long_score += 0.75
            reasons.append("price above VWAP")
        elif last.close < latest_vwap:
            short_score += 0.75
            reasons.append("price below VWAP")

        if latest_rsi is not None:
            if 55.0 <= latest_rsi <= 72.0:
                long_score += 0.85
                reasons.append("RSI supports bullish momentum")
            elif 28.0 <= latest_rsi <= 45.0:
                short_score += 0.85
                reasons.append("RSI supports bearish momentum")
            elif latest_rsi >= 74.0:
                short_score += 0.35
                reasons.append("RSI overbought risk")
            elif latest_rsi <= 26.0:
                long_score += 0.35
                reasons.append("RSI oversold rebound risk")

        if latest_macd > latest_macd_signal and latest_hist > 0.0:
            long_score += 1.0
            reasons.append("MACD bullish crossover")
        elif latest_macd < latest_macd_signal and latest_hist < 0.0:
            short_score += 1.0
            reasons.append("MACD bearish crossover")

        if volume_ratio >= 1.2:
            if last.close >= last.open:
                long_score += 0.55
                reasons.append("volume expansion on green candle")
            else:
                short_score += 0.55
                reasons.append("volume expansion on red candle")

        if pattern_direction == Direction.LONG:
            long_score += 0.9 * pattern_confidence
            reasons.append(pattern_name)
        elif pattern_direction == Direction.SHORT:
            short_score += 0.9 * pattern_confidence
            reasons.append(pattern_name)

        nearest_support = supports[0] if supports else None
        nearest_resistance = resistances[0] if resistances else None
        if nearest_support is not None:
            distance_support_pct = ((last.close - nearest_support) / max(last.close, 1e-9)) * 100.0
            if 0.0 <= distance_support_pct <= 0.8 and last.close >= last.open:
                long_score += 0.5
                reasons.append("holding near support")
        if nearest_resistance is not None:
            distance_resistance_pct = ((nearest_resistance - last.close) / max(last.close, 1e-9)) * 100.0
            if 0.0 <= distance_resistance_pct <= 0.8 and last.close <= last.open:
                short_score += 0.5
                reasons.append("rejecting near resistance")

        if market_mode == "TRENDING":
            if latest_adx is not None and latest_adx >= 25.0:
                if latest_ema20 > latest_ema50 > latest_ema200 and latest_macd > latest_macd_signal:
                    long_score += 1.1
                    reasons.append("trend regime boosts long continuation")
                elif latest_ema20 < latest_ema50 < latest_ema200 and latest_macd < latest_macd_signal:
                    short_score += 1.1
                    reasons.append("trend regime boosts short continuation")
        elif market_mode == "RANGING":
            long_score *= 0.72
            short_score *= 0.72
            if (
                nearest_support is not None
                and latest_rsi is not None
                and latest_rsi <= 42.0
                and 0.0 <= ((last.close - nearest_support) / max(last.close, 1e-9)) * 100.0 <= 0.7
            ):
                long_score += 1.0
                reasons.append("range mean reversion long at support")
            if (
                nearest_resistance is not None
                and latest_rsi is not None
                and latest_rsi >= 58.0
                and 0.0 <= ((nearest_resistance - last.close) / max(last.close, 1e-9)) * 100.0 <= 0.7
            ):
                short_score += 1.0
                reasons.append("range mean reversion short at resistance")
            if breakout_failure_rate >= 0.55:
                reasons.append("breakouts are failing frequently")
        else:
            long_score *= 0.88
            short_score *= 0.88
            reasons.append("market regime is transitional")

        if liq_map is not None:
            metrics = liq_map.market_metrics
            if liq_map.dominant_pull == Direction.LONG:
                long_score += 0.8
                reasons.append("liquidation map favors longs")
            elif liq_map.dominant_pull == Direction.SHORT:
                short_score += 0.8
                reasons.append("liquidation map favors shorts")

            oi_change = float(metrics.open_interest_change_pct or 0.0)
            funding_bps = float(metrics.last_funding_rate_bps or 0.0)
            taker_ratio = float(metrics.taker_buy_sell_ratio or 1.0)
            if oi_change > 0.5 and 0.0 <= funding_bps <= 3.5 and taker_ratio >= 1.01:
                long_score += 0.7
                reasons.append("OI and funding support long continuation")
            elif oi_change > 0.5 and -3.5 <= funding_bps <= 0.0 and taker_ratio <= 0.99:
                short_score += 0.7
                reasons.append("OI and funding support short continuation")
            elif funding_bps >= 6.0:
                short_score += 0.25
                reasons.append("funding crowded on the long side")
            elif funding_bps <= -6.0:
                long_score += 0.25
                reasons.append("funding crowded on the short side")

        net_score = long_score - short_score
        confidence = min(1.0, abs(net_score) / max_score)
        if confidence < 0.18:
            direction = Direction.FLAT
        else:
            direction = Direction.LONG if net_score > 0 else Direction.SHORT
        ordered_reasons = reasons[:6]
        signal_quality = self._signal_quality(direction, confidence, ordered_reasons)

        return CumulativeSignalTimeframe(
            timeframe=timeframe,
            market_mode=market_mode,
            regime_strength=regime_strength,
            direction=direction,
            confidence=confidence,
            signal_quality=signal_quality,
            score=net_score,
            max_score=max_score,
            close=last.close,
            ema20=latest_ema20,
            ema50=latest_ema50,
            ema200=latest_ema200,
            vwap=latest_vwap,
            rsi14=latest_rsi,
            macd=latest_macd,
            macd_signal=latest_macd_signal,
            macd_histogram=latest_hist,
            adx14=latest_adx,
            atr_pct=atr_pct,
            atr_state="EXPANDING" if atr_ratio >= 1.08 else "COMPRESSING" if atr_ratio <= 0.94 else "NEUTRAL",
            breakout_failure_rate=breakout_failure_rate,
            volume_ratio=volume_ratio,
            pattern=pattern_name,
            nearest_support=nearest_support,
            nearest_resistance=nearest_resistance,
            reasons=ordered_reasons,
        )

    def generate_cumulative_signal(self, symbol: str = "BTCUSDT") -> CumulativeSignalResponse:
        decision_ts = int(time.time() * 1000)
        features, _ = self._build_signal_features(symbol)
        try:
            liq_map = self.generate_liquidation_map_advanced(symbol=symbol, include_events=True, event_limit=50)
        except Exception:
            liq_map = None

        timeframe_specs = [("5m", 220), ("15m", 220), ("1h", 260)]
        timeframe_signals: list[CumulativeSignalTimeframe] = []
        for timeframe, limit in timeframe_specs:
            rows = self.client.get_recent_klines(symbol, interval=timeframe, limit=limit)
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
            if not candles:
                continue
            timeframe_signals.append(
                self._cumulative_timeframe_signal(timeframe=timeframe, candles=candles, liq_map=liq_map)
            )

        weights = {"5m": 1.0, "15m": 1.35, "1h": 1.75}
        weighted_score = sum(signal.score * weights.get(signal.timeframe, 1.0) for signal in timeframe_signals)
        weighted_max = sum(signal.max_score * weights.get(signal.timeframe, 1.0) for signal in timeframe_signals)
        mode_weights = {"TRENDING": 1.0, "RANGING": -1.0, "TRANSITIONAL": 0.0}
        weighted_mode_score = sum(mode_weights.get(signal.market_mode, 0.0) * weights.get(signal.timeframe, 1.0) for signal in timeframe_signals)
        if weighted_mode_score >= 1.0:
            aggregate_market_mode = "TRENDING"
            strategy_mode = "TREND_FOLLOW"
        elif weighted_mode_score <= -1.0:
            aggregate_market_mode = "RANGING"
            strategy_mode = "MEAN_REVERT"
        else:
            aggregate_market_mode = "TRANSITIONAL"
            strategy_mode = "STAY_FLAT"

        if aggregate_market_mode == "RANGING":
            weighted_score *= 0.82
        elif aggregate_market_mode == "TRANSITIONAL":
            weighted_score *= 0.65

        aggregate_confidence = min(1.0, abs(weighted_score) / max(weighted_max, 1e-9))
        if aggregate_market_mode == "TRANSITIONAL":
            aggregate_confidence = min(aggregate_confidence, 0.42)

        if aggregate_confidence < 0.18 or aggregate_market_mode == "TRANSITIONAL":
            aggregate_direction = Direction.FLAT
        else:
            aggregate_direction = Direction.LONG if weighted_score > 0 else Direction.SHORT

        aligned = sum(1 for signal in timeframe_signals if signal.direction == aggregate_direction and signal.direction != Direction.FLAT)
        agreement_ratio = aligned / max(len(timeframe_signals), 1)
        aggregate_reasons: list[str] = []
        aggregate_reasons.append(f"Market mode {aggregate_market_mode.lower()} with {strategy_mode.lower().replace('_', ' ')} bias")
        for signal in sorted(timeframe_signals, key=lambda row: weights.get(row.timeframe, 1.0), reverse=True):
            if signal.direction == aggregate_direction and signal.direction != Direction.FLAT:
                aggregate_reasons.append(
                    f"{signal.timeframe} {signal.market_mode.lower()} {signal.direction.value} {signal.confidence:.2f}: {', '.join(signal.reasons[:2])}"
                )
        if not aggregate_reasons:
            aggregate_reasons.append("Timeframes are mixed and no clear alignment is present")

        if aggregate_market_mode == "RANGING":
            ranging_signals = [signal for signal in timeframe_signals if signal.market_mode == "RANGING"]
            if not ranging_signals:
                aggregate_direction = Direction.FLAT
                aggregate_confidence = min(aggregate_confidence, 0.3)
            elif agreement_ratio < 0.34:
                aggregate_direction = Direction.FLAT
                aggregate_confidence = min(aggregate_confidence, 0.3)
                aggregate_reasons.append("Range regime lacks clean reversal alignment, so bot stays flat")

        bot_tp, bot_sl = compute_tp_sl(aggregate_direction, features.mark_price, aggregate_confidence, features, self.cfg)
        bot_signal = SignalResult(
            symbol=symbol,
            direction=aggregate_direction,
            confidence=aggregate_confidence,
            current_price=features.mark_price,
            tp=bot_tp,
            sl=bot_sl,
            reasons=aggregate_reasons[:6],
            horizon="MTF 5m/15m/1h",
            decision_ts=decision_ts,
            expires_at=decision_ts + (self.cfg.signal_ttl_seconds * 1000),
            entry_assumption="mark",
            model_version=f"{self.cfg.model_version}+cumulative-mtf",
            feature_version=self.cfg.feature_version,
            signal_quality=self._signal_quality(aggregate_direction, aggregate_confidence, aggregate_reasons[:6]),
        )
        return CumulativeSignalResponse(
            symbol=symbol,
            generated_at=decision_ts,
            market_mode=aggregate_market_mode,
            strategy_mode=strategy_mode,
            aggregate_direction=aggregate_direction,
            aggregate_confidence=aggregate_confidence,
            agreement_ratio=agreement_ratio,
            signal_quality=bot_signal.signal_quality,
            reasons=aggregate_reasons[:6],
            bot_signal=bot_signal,
            timeframes=timeframe_signals,
        )

    def list_market_symbols(
        self,
        quote_asset: str | None = None,
        search: str | None = None,
        limit: int = 600,
    ) -> MarketSymbolsResponse:
        filtered = [
            MarketSymbol(
                symbol=symbol,
                base_asset=symbol.removesuffix("USDT"),
                quote_asset="USDT",
                status="TRADING",
                contract_type="PERPETUAL",
            )
            for symbol in SUPPORTED_MARKET_SYMBOLS
        ]
        normalized_quote = str(quote_asset or "").upper().strip()
        if normalized_quote:
            filtered = [row for row in filtered if row.quote_asset == normalized_quote]

        normalized_search = str(search or "").upper().strip()
        if normalized_search:
            filtered = [
                row
                for row in filtered
                if normalized_search in row.symbol
                or normalized_search in row.base_asset
                or normalized_search in row.quote_asset
            ]

        preferred_symbols = {symbol: idx for idx, symbol in enumerate(SUPPORTED_MARKET_SYMBOLS)}
        filtered.sort(key=lambda row: (preferred_symbols.get(row.symbol, 9999), row.quote_asset, row.symbol))
        return MarketSymbolsResponse(symbols=filtered[: max(1, limit)])

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
        run_id = uuid4().hex
        run_started_at = int(time.time() * 1000)
        rows = self.client.get_recent_klines(symbol, interval=interval, limit=candle_limit)
        if not rows:
            response = BotBacktestResponse(
                run_id=run_id,
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
                signal_source=self.BACKTEST_SIGNAL_SOURCE,
                trades=[],
                trade_logs=[],
                equity_curve=[],
            )
            self.liquidation_store.persist_backtest_run(
                run_id=run_id,
                symbol=symbol,
                interval=interval,
                started_at=run_started_at,
                completed_at=int(time.time() * 1000),
                signal_source=self.BACKTEST_SIGNAL_SOURCE,
                config={
                    "interval": interval,
                    "candle_limit": candle_limit,
                    "initial_capital": initial_capital,
                    "fee_rate": fee_rate,
                    "leverage": leverage,
                    "confidence_threshold": confidence_threshold,
                    "max_trades": max_trades,
                    "use_liquidation_data": use_liquidation_data,
                    "use_htf_filter": use_htf_filter,
                    "htf_interval": htf_interval,
                    "htf_lookback": htf_lookback,
                    "use_sentiment_data": use_sentiment_data,
                    "use_volume_filter": use_volume_filter,
                },
                response=response,
            )
            return response

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

        mtf_analyzer = MTFRegimeAnalyzer()
        klines_1h = self.client.get_recent_klines(symbol, interval="1h", limit=100)
        klines_4h = self.client.get_recent_klines(symbol, interval="4h", limit=100)
        klines_12h = self.client.get_recent_klines(symbol, interval="12h", limit=100)

        def sorted_history(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return sorted(rows, key=self._row_timestamp)

        try:
            oi_rows = sorted_history(self.client.get_open_interest_hist(symbol, period="5m", limit=500))
        except Exception:
            oi_rows = []
        try:
            funding_rows = sorted_history(self.client.get_funding_rates(symbol, limit=200))
        except Exception:
            funding_rows = []
        try:
            basis_rows = sorted_history(self.client.get_basis(symbol, period="5m", limit=200))
        except Exception:
            basis_rows = []
        try:
            taker_volume_rows = sorted_history(self.client.get_taker_buy_sell_volume(symbol, period="5m", limit=200))
        except Exception:
            taker_volume_rows = []
        try:
            global_ratio_rows = sorted_history(
                self.client.get_global_long_short_account_ratio(symbol, period="5m", limit=200)
            )
        except Exception:
            global_ratio_rows = []
        try:
            top_account_ratio_rows = sorted_history(
                self.client.get_top_long_short_account_ratio(symbol, period="5m", limit=200)
            )
        except Exception:
            top_account_ratio_rows = []
        try:
            top_position_ratio_rows = sorted_history(
                self.client.get_top_long_short_position_ratio(symbol, period="5m", limit=200)
            )
        except Exception:
            top_position_ratio_rows = []

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
        confirmation_bars = 2  # Require 2-bar confirmation to filter noise without stalling.
        min_hold_minutes = 3  # Do not close immediately; still allow intraday exits.
        skip_entries_remaining = 0  # Skip the next candidate only after a loss.
        entry_conf_threshold = max(confidence_threshold, self.cfg.min_confidence)

        def build_entry_reason(
            signal: SignalResult,
            market_regime: MarketRegime,
            regime_bias: float,
            htf_dir: Direction,
            liq_conf: float,
            vol_ok: bool,
        ) -> str:
            parts = [
                f"signal={signal.direction.value}",
                f"confidence={signal.confidence:.2f}",
                f"quality={signal.signal_quality}",
                f"horizon={signal.horizon}",
                f"regime={market_regime.value}",
                f"regime_bias={regime_bias:.2f}",
            ]
            if use_htf_filter:
                parts.append(f"htf={htf_dir.value}")
            if use_liquidation_data and liq_conf > 0.0:
                parts.append(f"liquidation_conf={liq_conf:.2f}")
            parts.append("reasons=" + ",".join(signal.reasons[:4]))
            if use_sentiment_data:
                parts.append("sentiment=confirmed")
            if use_volume_filter:
                parts.append("volume=confirmed" if vol_ok else "volume=not_required")
            return " | ".join(parts)

        def is_volume_spike(window_rows: list[list[Any]]) -> bool:
            """Check if recent candle has elevated volume (top 50% of recent average)."""
            if len(window_rows) < 3:
                return False
            volumes = [float(row[5]) for row in window_rows]
            avg_vol = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else 1.0
            current_vol = volumes[-1]
            return current_vol >= avg_vol * 1.5

        def avg_candle_range_bps(window_rows: list[list[Any]], mark_price: float) -> float:
            if not window_rows or mark_price <= 0.0:
                return 0.0
            ranges = [abs(float(row[2]) - float(row[3])) for row in window_rows[-20:]]
            if not ranges:
                return 0.0
            return (sum(ranges) / len(ranges) / mark_price) * 10_000

        def ratio_at(rows_ratio: list[dict[str, Any]], time_ms: int, key: str) -> float:
            candidates = self._rows_through_time(rows_ratio, time_ms, limit=1)
            if not candidates:
                return 1.0
            return float(candidates[-1].get(key, 1.0))

        def sentiment_confirm(signal_side: Direction, time_ms: int, market_regime: MarketRegime) -> bool:
            if not use_sentiment_data:
                return True
            if not taker_volume_rows and not global_ratio_rows:
                return True

            taker_ratio = ratio_at(taker_volume_rows, time_ms, "buySellRatio")
            account_ratio = ratio_at(global_ratio_rows, time_ms, "longShortRatio")
            trend_regime = market_regime in (
                MarketRegime.STRONG_UPTREND,
                MarketRegime.UPTREND,
                MarketRegime.STRONG_DOWNTREND,
                MarketRegime.DOWNTREND,
            )

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

        def regime_allows_signal(
            signal: SignalResult,
            market_regime: MarketRegime,
            regime_bias: float,
            htf_dir: Direction,
        ) -> bool:
            if signal.direction == Direction.FLAT:
                return False

            required_conf = entry_conf_threshold
            if market_regime in (MarketRegime.STRONG_UPTREND, MarketRegime.UPTREND):
                if signal.direction == Direction.SHORT:
                    required_conf = max(required_conf, 0.72)
            elif market_regime in (MarketRegime.STRONG_DOWNTREND, MarketRegime.DOWNTREND):
                if signal.direction == Direction.LONG:
                    required_conf = max(required_conf, 0.72)
            elif market_regime == MarketRegime.MIXED_UP:
                if signal.direction == Direction.SHORT:
                    required_conf = max(required_conf, 0.68)
            elif market_regime == MarketRegime.MIXED_DOWN:
                if signal.direction == Direction.LONG:
                    required_conf = max(required_conf, 0.68)

            if signal.confidence < required_conf:
                return False
            if use_htf_filter and htf_dir != Direction.FLAT and htf_dir != signal.direction and abs(regime_bias) >= 0.25:
                return False
            return True

        def execution_levels(
            signal: SignalResult,
            market_regime: MarketRegime,
            regime_bias: float,
            window_rows: list[list[Any]],
        ) -> tuple[float, float]:
            mark_price = max(signal.current_price, 1e-9)
            signal_stop_bps = abs(mark_price - signal.sl) / mark_price * 10_000
            signal_tp_bps = abs(signal.tp - mark_price) / mark_price * 10_000
            range_bps = max(avg_candle_range_bps(window_rows, mark_price), 10.0)

            aligned_with_regime = (
                (signal.direction == Direction.LONG and regime_bias >= 0.2)
                or (signal.direction == Direction.SHORT and regime_bias <= -0.2)
                or market_regime in (MarketRegime.BALANCED, MarketRegime.MIXED_UP, MarketRegime.MIXED_DOWN)
            )
            rr = 1.45 if aligned_with_regime else 1.20
            stop_bps = max(signal_stop_bps, range_bps * 0.90, 20.0)
            tp_bps = max(signal_tp_bps, stop_bps * rr)

            if signal.direction == Direction.LONG:
                return (
                    mark_price * (1.0 + tp_bps / 10_000),
                    mark_price * (1.0 - stop_bps / 10_000),
                )
            return (
                mark_price * (1.0 - tp_bps / 10_000),
                mark_price * (1.0 + stop_bps / 10_000),
            )

        def close_position(price: float, close_time: int, reason: str) -> None:
            nonlocal capital, fees_paid, position, skip_entries_remaining
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
                    run_id=run_id,
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
                    entry_reason=position.get("entry_reason", ""),
                    exit_reason=reason,
                    entry_signal_direction=position.get("entry_signal_direction", Direction.FLAT),
                    entry_signal_confidence=position.get("entry_signal_confidence", 0.0),
                    entry_signal_quality=position.get("entry_signal_quality", "UNKNOWN"),
                    signal_horizon=position.get("signal_horizon", interval),
                    signal_source=position.get("signal_source", self.BACKTEST_SIGNAL_SOURCE),
                    reason=reason,
                )
            )

            # Set flag to skip next entry if this trade was a loss.
            if net < 0:
                skip_entries_remaining = 1

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
            oi_slice = self._rows_through_time(oi_rows, candle.open_time, limit=30)
            funding_slice = self._rows_through_time(funding_rows, candle.open_time, limit=30)
            basis_slice = self._rows_through_time(basis_rows, candle.open_time, limit=30)
            taker_volume_slice = (
                self._rows_through_time(taker_volume_rows, candle.open_time, limit=30) if use_sentiment_data else []
            )
            global_ratio_slice = (
                self._rows_through_time(global_ratio_rows, candle.open_time, limit=30) if use_sentiment_data else []
            )
            top_account_ratio_slice = (
                self._rows_through_time(top_account_ratio_rows, candle.open_time, limit=30) if use_sentiment_data else []
            )
            top_position_ratio_slice = (
                self._rows_through_time(top_position_ratio_rows, candle.open_time, limit=30)
                if use_sentiment_data
                else []
            )
            market_regime, regime_bias = self._mtf_context_at(
                mtf_analyzer,
                time_ms=candle.open_time,
                klines_1h=klines_1h,
                klines_4h=klines_4h,
                klines_12h=klines_12h,
            )
            htf_dir, _ = htf_trend_metrics(candle.open_time)
            features = build_liquidity_features(
                symbol=symbol,
                mark_price=candle.close,
                order_book=None,
                trades=[],
                klines=kline_slice,
                oi_hist=oi_slice,
                funding_rates=funding_slice,
                basis_rows=basis_slice,
                taker_volume_rows=taker_volume_slice,
                global_ratio_rows=global_ratio_slice,
                top_account_ratio_rows=top_account_ratio_slice,
                top_position_ratio_rows=top_position_ratio_slice,
                htf_bias=regime_bias,
                htf_regime=market_regime.value,
            )

            liq_map: LiquidationMapResponse | None = None
            liq_conf = 0.0
            if use_liquidation_data and len(oi_slice) >= 2:
                liq_map = build_liquidation_map_estimate(
                    symbol,
                    candle.close,
                    oi_slice,
                    kline_slice,
                    funding_rates=funding_slice,
                )
                liq_conf = liq_map.confidence

            base_signal_result, _, _ = self._build_signal_from_features(
                symbol=symbol,
                features=features,
                decision_ts=candle.open_time,
                liquidation_map=None,
            )
            if liq_map is not None and base_signal_result.direction != Direction.FLAT:
                signal_result, _, _ = self._build_signal_from_features(
                    symbol=symbol,
                    features=features,
                    decision_ts=candle.open_time,
                    liquidation_map=liq_map,
                )
            else:
                signal_result = base_signal_result

            if signal_result.direction == confirm_signal and signal_result.direction != Direction.FLAT:
                confirm_count += 1
            else:
                confirm_signal = signal_result.direction
                confirm_count = 1 if signal_result.direction != Direction.FLAT else 0

            ready_signal = (
                signal_result.direction
                if (signal_result.direction != Direction.FLAT and confirm_count >= confirmation_bars)
                else Direction.FLAT
            )

            if position is not None:
                if ready_signal != Direction.FLAT and ready_signal != position["side"]:
                    close_position(candle.close, candle.open_time, "signal_flip")

            vol_ok = not use_volume_filter or is_volume_spike(kline_slice)
            tp_ok = (
                (ready_signal == Direction.LONG and signal_result.tp > candle.close and signal_result.sl < candle.close)
                or (ready_signal == Direction.SHORT and signal_result.tp < candle.close and signal_result.sl > candle.close)
            )

            if (
                position is None
                and ready_signal in (Direction.LONG, Direction.SHORT)
                and base_signal_result.direction != Direction.FLAT
                and "no_edge" not in base_signal_result.reasons
                and regime_allows_signal(signal_result, market_regime, regime_bias, htf_dir)
                and sentiment_confirm(ready_signal, candle.open_time, market_regime)
                and vol_ok
                and tp_ok
                and len(trades) < max_trades
            ):
                if skip_entries_remaining > 0:
                    skip_entries_remaining -= 1
                    trade_logs.append(
                        f"Skipped {ready_signal.value} entry at "
                        f"{datetime.fromtimestamp(candle.open_time / 1000).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')} "
                        "after previous loss"
                    )
                    equity_curve.append(EquityPoint(time=candle.open_time, equity=capital))
                    continue

                trade_notional = max(capital, 0.0) * leverage
                if trade_notional > 0:
                    qty = trade_notional / candle.close
                    entry_fee = trade_notional * fee_rate
                    capital -= entry_fee
                    fees_paid += entry_fee
                    tp_price, sl_price = execution_levels(signal_result, market_regime, regime_bias, kline_slice)

                    position = {
                        "side": ready_signal,
                        "qty": qty,
                        "entry_price": candle.close,
                        "entry_time": candle.open_time,
                        "entry_fee": entry_fee,
                        "funding": 0.0,
                        "tp_price": tp_price,
                        "sl_price": sl_price,
                        "entry_reason": build_entry_reason(
                            signal=signal_result,
                            market_regime=market_regime,
                            regime_bias=regime_bias,
                            htf_dir=htf_dir,
                            liq_conf=liq_conf,
                            vol_ok=vol_ok,
                        ),
                        "entry_signal_direction": ready_signal,
                        "entry_signal_confidence": signal_result.confidence,
                        "entry_signal_quality": signal_result.signal_quality,
                        "signal_horizon": signal_result.horizon,
                        "signal_source": self.BACKTEST_SIGNAL_SOURCE,
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

        response = BotBacktestResponse(
            run_id=run_id,
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
            signal_source=self.BACKTEST_SIGNAL_SOURCE,
            trades=trades,
            trade_logs=trade_logs,
            equity_curve=equity_curve,
        )
        self.liquidation_store.persist_backtest_run(
            run_id=run_id,
            symbol=symbol,
            interval=interval,
            started_at=run_started_at,
            completed_at=int(time.time() * 1000),
            signal_source=self.BACKTEST_SIGNAL_SOURCE,
            config={
                "interval": interval,
                "candle_limit": candle_limit,
                "initial_capital": initial_capital,
                "fee_rate": fee_rate,
                "leverage": leverage,
                "confidence_threshold": confidence_threshold,
                "max_trades": max_trades,
                "use_liquidation_data": use_liquidation_data,
                "use_htf_filter": use_htf_filter,
                "htf_interval": htf_interval,
                "htf_lookback": htf_lookback,
                "use_sentiment_data": use_sentiment_data,
                "use_volume_filter": use_volume_filter,
            },
            response=response,
        )
        return response
