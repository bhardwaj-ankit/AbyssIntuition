from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
import threading
import time
from typing import Any
from uuid import uuid4

from liquidity_signal.ai.openai_supervisor import OpenAISupervisor
from liquidity_signal.config import BybitDemoConfig
from liquidity_signal.data.bybit_client import BybitDemoTradingClient
from liquidity_signal.models import (
    AITradeDecision,
    BotTrade,
    DemoBotConfigStatus,
    DemoBotPerformanceResponse,
    DemoBotPerformanceSummary,
    DemoBotPosition,
    DemoBotStatus,
    Direction,
    LiquidationCluster,
    LiquidationMapAdvancedResponse,
    SignalExplainResult,
    SignalResult,
)


@dataclass(frozen=True)
class _EntryProfile:
    mode: str
    min_signal_confidence: float
    min_cumulative_confidence: float
    min_agreement_ratio: float
    allowed_qualities: set[str]
    countertrend_confidence: float
    allow_flat_cumulative: bool


@dataclass(frozen=True)
class _ExitPlan:
    strategy_mode: str
    tp_multipliers: tuple[float, ...]
    tp_allocations: tuple[float, ...]
    trailing_activation_r: float | None = None
    trailing_distance_r: float | None = None


@dataclass(frozen=True)
class _ScalpPlan:
    stale_after_seconds: int
    stale_exit_max_r: float
    stale_signal_confidence: float
    stale_agreement_ratio: float
    breakeven_r: float
    partial_take_r: float
    partial_fraction: float
    partial_stop_r: float
    lock_profit_r: float
    lock_stop_r: float
    adverse_liquidity_confidence: float
    adverse_liquidity_max_r: float
    fast_reentry_seconds: int
    stopout_reentry_seconds: int


@dataclass(frozen=True)
class _SetupAssessment:
    setup_type: str
    breakout_confirmed: bool
    entry_allowed: bool
    fakeout_risk: float
    opposite_move_close_r: float
    rationale: str


class BybitDemoBot:
    SIGNAL_SOURCE = "bybit_demo_live_bot"

    def __init__(
        self,
        signal_engine: Any,
        *,
        cfg: BybitDemoConfig | None = None,
        client: BybitDemoTradingClient | None = None,
        supervisor: OpenAISupervisor | None = None,
    ) -> None:
        self.signal_engine = signal_engine
        self.cfg = cfg or BybitDemoConfig.from_env()
        self.client = client
        self._client_injected = client is not None
        self.supervisor = supervisor or OpenAISupervisor()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._status = DemoBotStatus(
            symbol=self.cfg.default_symbol,
            running=False,
            poll_interval_seconds=self.cfg.default_poll_interval_seconds,
            mode=self.cfg.default_mode.upper(),
            leverage=self.cfg.default_leverage,
            risk_per_trade_pct=self.cfg.default_risk_per_trade_pct,
            max_margin_fraction=self.cfg.default_max_margin_fraction,
            cooldown_seconds=self.cfg.default_cooldown_seconds,
            signal_source=self.SIGNAL_SOURCE,
            ai_enabled=self.supervisor.is_active(),
            ai_model=self.supervisor.get_config_status().model if self.supervisor.get_config_status().configured else None,
        )
        self._instrument_cache: dict[str, dict[str, Any]] = {}
        self._active_entry_context: dict[str, Any] | None = None
        self._seen_closed_trade_ids: set[str] = set()

    def close(self) -> None:
        self.stop(close_position=False)
        if self.client is not None:
            self.client.close()
        self.supervisor.close()

    def _reload_config(self) -> None:
        if self._client_injected:
            return
        next_cfg = BybitDemoConfig.from_sources(self.cfg.config_path or None)
        changed = (
            next_cfg.api_key != self.cfg.api_key
            or next_cfg.api_secret != self.cfg.api_secret
            or next_cfg.base_url != self.cfg.base_url
            or next_cfg.recv_window != self.cfg.recv_window
        )
        if changed and self.client is not None:
            self.client.close()
            self.client = None
        self.cfg = next_cfg

    def _client(self) -> BybitDemoTradingClient:
        if self.client is None:
            if not self.cfg.enabled:
                raise RuntimeError("Bybit demo credentials are not configured")
            self.client = BybitDemoTradingClient(
                api_key=self.cfg.api_key,
                api_secret=self.cfg.api_secret,
                base_url=self.cfg.base_url,
                recv_window=self.cfg.recv_window,
            )
        return self.client

    def _status_snapshot(self) -> DemoBotStatus:
        with self._lock:
            return DemoBotStatus.model_validate(self._status.model_dump(mode="json"))

    def _set_status(self, status: DemoBotStatus) -> None:
        with self._lock:
            self._status = status

    def _with_ai_metadata(self, status: DemoBotStatus) -> DemoBotStatus:
        ai_status = self.supervisor.get_config_status()
        status.ai_enabled = ai_status.active
        status.ai_model = ai_status.model if ai_status.configured else None
        return status

    def get_status(self, symbol: str | None = None) -> DemoBotStatus:
        status = self._with_ai_metadata(self._status_snapshot())
        if status.running:
            return status
        if symbol and status.symbol.upper() != symbol.upper():
            return self._with_ai_metadata(
                DemoBotStatus(
                symbol=symbol.upper(),
                running=False,
                poll_interval_seconds=self.cfg.default_poll_interval_seconds,
                mode=self.cfg.default_mode.upper(),
                leverage=self.cfg.default_leverage,
                risk_per_trade_pct=self.cfg.default_risk_per_trade_pct,
                max_margin_fraction=self.cfg.default_max_margin_fraction,
                cooldown_seconds=self.cfg.default_cooldown_seconds,
                signal_source=self.SIGNAL_SOURCE,
                )
            )
        return status

    def get_config_status(self) -> DemoBotConfigStatus:
        self._reload_config()
        ai_status = self.supervisor.get_config_status()
        message = (
            "Credentials loaded. You can start the Bybit demo bot from the dashboard."
            if self.cfg.enabled
            else f"Paste your Bybit demo API key/secret into {self.cfg.config_path} or set env vars."
        )
        return DemoBotConfigStatus(
            configured=self.cfg.enabled,
            config_path=self.cfg.config_path,
            config_source=self.cfg.config_source,
            base_url=self.cfg.base_url,
            category=self.cfg.category,
            account_type=self.cfg.account_type,
            default_symbol=self.cfg.default_symbol,
            ai_enabled=ai_status.active,
            ai_configured=ai_status.configured,
            ai_model=ai_status.model if ai_status.configured else None,
            ai_message=ai_status.message,
            message=message,
        )

    def get_performance(self, symbol: str, *, limit: int = 50) -> DemoBotPerformanceResponse:
        self._reload_config()
        if not self.cfg.enabled:
            raise RuntimeError(
                f"Paste your Bybit demo credentials into {self.cfg.config_path} or set BYBIT_DEMO_API_KEY/BYBIT_DEMO_API_SECRET"
            )

        rows = self._client().get_closed_pnl(
            symbol.upper(),
            category=self.cfg.category,
            limit=limit,
            start_time=None,
        )
        trades = [trade for trade in (self._historical_trade_to_bot_trade(symbol.upper(), row) for row in rows) if trade is not None]
        wins = [trade for trade in trades if trade.net_pnl > 0]
        losses = [trade for trade in trades if trade.net_pnl < 0]
        gross_profit = sum(trade.net_pnl for trade in wins)
        gross_loss = sum(abs(trade.net_pnl) for trade in losses)
        total_fees = sum(trade.fee_paid for trade in trades)
        total_funding = sum(trade.funding_paid for trade in trades)
        net_pnl = sum(trade.net_pnl for trade in trades)
        total_trades = len(trades)
        profit_factor = None if gross_loss <= 0 else gross_profit / gross_loss

        return DemoBotPerformanceResponse(
            symbol=symbol.upper(),
            lookback_trades=limit,
            summary=DemoBotPerformanceSummary(
                total_trades=total_trades,
                win_rate=(len(wins) / total_trades) if total_trades else 0.0,
                net_pnl=net_pnl,
                gross_profit=gross_profit,
                gross_loss=-gross_loss,
                profit_factor=profit_factor,
                avg_roi_pct=(sum(trade.roi_pct for trade in trades) / total_trades) if total_trades else 0.0,
                best_trade_pnl=max((trade.net_pnl for trade in trades), default=0.0),
                worst_trade_pnl=min((trade.net_pnl for trade in trades), default=0.0),
                total_fees=total_fees,
                total_funding=total_funding,
                last_trade_at=max((trade.exit_time for trade in trades), default=None),
            ),
            trades=trades,
        )

    def start(
        self,
        *,
        symbol: str | None = None,
        poll_interval_seconds: int | None = None,
        leverage: float | None = None,
        risk_per_trade_pct: float | None = None,
        max_margin_fraction: float | None = None,
        cooldown_seconds: int | None = None,
        mode: str | None = None,
    ) -> DemoBotStatus:
        current = self._status_snapshot()
        if current.running:
            return current

        self._reload_config()
        if not self.cfg.enabled:
            raise RuntimeError(
                f"Paste your Bybit demo credentials into {self.cfg.config_path} or set BYBIT_DEMO_API_KEY/BYBIT_DEMO_API_SECRET"
            )
        # Fail fast on invalid credentials, missing demo permissions, or geo/network errors.
        self._client().get_wallet_balance(account_type=self.cfg.account_type, coin="USDT")

        now_ms = int(time.time() * 1000)
        status = DemoBotStatus(
            session_id=uuid4().hex,
            symbol=(symbol or self.cfg.default_symbol).upper(),
            running=True,
            started_at=now_ms,
            poll_interval_seconds=poll_interval_seconds or self.cfg.default_poll_interval_seconds,
            mode=(mode or self.cfg.default_mode).upper(),
            leverage=leverage or self.cfg.default_leverage,
            risk_per_trade_pct=risk_per_trade_pct or self.cfg.default_risk_per_trade_pct,
            max_margin_fraction=max_margin_fraction or self.cfg.default_max_margin_fraction,
            cooldown_seconds=cooldown_seconds or self.cfg.default_cooldown_seconds,
            signal_source=self.SIGNAL_SOURCE,
            ai_enabled=self.supervisor.is_active(),
            ai_model=self.supervisor.get_config_status().model if self.supervisor.get_config_status().configured else None,
            last_action="started",
            last_updated_at=now_ms,
        )
        self._active_entry_context = None
        self._seen_closed_trade_ids = set()
        self._set_status(status)
        self._stop.clear()

        self._thread = threading.Thread(target=self._loop, daemon=True, name="bybit-demo-bot")
        self._thread.start()
        return self._status_snapshot()

    def stop(self, *, close_position: bool = True) -> DemoBotStatus:
        pre_stop_status = self._status_snapshot()
        if close_position and pre_stop_status.open_position is not None:
            try:
                self._close_position(pre_stop_status, reason="stopped_by_user")
            except Exception as exc:
                failed = self._status_snapshot()
                failed.last_error = str(exc)
                failed.last_action = "stop_close_failed"
                self._set_status(failed)
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

        status = self._status_snapshot()
        if status.running:
            status.running = False
            status.stopped_at = int(time.time() * 1000)
            status.last_action = "stopped"
            status.last_updated_at = status.stopped_at
            self._set_status(status)
        return self._status_snapshot()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._run_cycle()
            except Exception as exc:
                failed = self._status_snapshot()
                failed.last_error = str(exc)
                failed.last_action = "cycle_error"
                failed.last_updated_at = int(time.time() * 1000)
                self._set_status(failed)
            wait_seconds = self._status_snapshot().poll_interval_seconds
            self._stop.wait(wait_seconds)

        final_status = self._status_snapshot()
        if final_status.running:
            final_status.running = False
            final_status.stopped_at = int(time.time() * 1000)
            final_status.last_updated_at = final_status.stopped_at
            final_status.last_action = "stopped"
        self._set_status(final_status)

    def _build_live_signal_context(
        self,
        symbol: str,
    ) -> tuple[SignalExplainResult, Any, LiquidationMapAdvancedResponse | None, SignalResult]:
        explain = self.signal_engine.generate_signal_explain(symbol)
        cumulative = self.signal_engine.generate_cumulative_signal(symbol)
        liq_map: LiquidationMapAdvancedResponse | None = None
        try:
            liq_map = self.signal_engine.generate_liquidation_map_advanced(
                symbol=symbol,
                include_events=True,
                event_limit=50,
            )
        except Exception:
            liq_map = None
        live_signal = self._apply_liquidity_magnet(cumulative.bot_signal, liq_map, cumulative)
        explain_reasons = [
            reason
            for reason in explain.signal.reasons
            if reason not in {"Liquidation map opposes signal direction", "Confidence dropped below threshold after liquidation filter"}
        ]
        merged_reasons = self._merge_reasons(cumulative.reasons, explain_reasons, live_signal.reasons)
        if getattr(cumulative, "aggregate_direction", Direction.FLAT) != Direction.FLAT:
            merged_reasons = [reason for reason in merged_reasons if reason != "no_edge"]
        live_signal = live_signal.model_copy(
            update={
                "reasons": merged_reasons,
                "horizon": cumulative.bot_signal.horizon,
                "signal_quality": cumulative.signal_quality or live_signal.signal_quality,
            }
        )
        return explain, cumulative, liq_map, live_signal

    @staticmethod
    def _merge_reasons(*reason_groups: list[str]) -> list[str]:
        ordered: list[str] = []
        for group in reason_groups:
            for reason in group:
                normalized = str(reason).strip()
                if normalized and normalized not in ordered:
                    ordered.append(normalized)
        return ordered[:8]

    def _apply_liquidity_magnet(
        self,
        signal: SignalResult,
        liq_map: LiquidationMapAdvancedResponse | None,
        cumulative: Any | None = None,
    ) -> SignalResult:
        if liq_map is None or signal.direction == Direction.FLAT:
            return signal

        reasons = list(signal.reasons)
        confidence = float(signal.confidence)
        quality = signal.signal_quality
        market_mode = str(getattr(cumulative, "market_mode", "") or "").upper()
        agreement_ratio = self._safe_float(getattr(cumulative, "agreement_ratio", 0.0))
        trend_following = market_mode == "TRENDING"

        if liq_map.dominant_pull == signal.direction:
            magnet_score = self._liquidity_magnet_score(signal.direction, signal.current_price, liq_map)
            bonus_cap = 0.12 if trend_following else 0.10
            bonus = min(bonus_cap, (liq_map.confidence * 0.07) + magnet_score)
            confidence = min(0.98, confidence + bonus)
            reasons.append(f"Liquidity magnet supports {signal.direction.value.lower()} bias (+{bonus:.2f})")
        elif liq_map.dominant_pull != Direction.FLAT and liq_map.confidence >= 0.55:
            penalty_rate = 0.04 if trend_following else 0.07
            penalty_cap = 0.08 if trend_following else 0.12
            if agreement_ratio >= 0.66 and confidence >= 0.55:
                penalty_rate *= 0.55
                penalty_cap *= 0.65
            elif agreement_ratio >= 0.5 and confidence >= 0.45:
                penalty_rate *= 0.75
            penalty = min(penalty_cap, liq_map.confidence * penalty_rate)
            confidence = max(0.0, confidence - penalty)
            reasons.append(f"Liquidity magnet leans against setup (-{penalty:.2f})")

        if confidence >= 0.78:
            quality = "HIGH"
        elif confidence >= 0.64:
            quality = "ELEVATED"
        elif confidence < 0.55:
            quality = "LOW"

        return signal.model_copy(
            update={
                "confidence": confidence,
                "signal_quality": quality,
                "reasons": reasons[:8],
            }
        )

    def _liquidity_allows_entry(
        self,
        *,
        signal: SignalResult,
        liq_map: LiquidationMapAdvancedResponse,
        cumulative: Any,
        explain: SignalExplainResult,
        profile: _EntryProfile,
        behavior: dict[str, Any],
    ) -> bool:
        dominant_pull = liq_map.dominant_pull
        if dominant_pull == Direction.FLAT:
            return profile.allow_flat_cumulative or signal.confidence >= profile.min_signal_confidence
        if dominant_pull == signal.direction:
            return liq_map.confidence >= profile.min_cumulative_confidence

        cumulative_confidence = self._safe_float(getattr(cumulative, "aggregate_confidence", signal.confidence))
        agreement_ratio = self._safe_float(getattr(cumulative, "agreement_ratio", 0.0))
        market_mode = str(getattr(cumulative, "market_mode", behavior.get("mode", "UNKNOWN"))).upper()
        if market_mode != "TRENDING":
            return False
        if liq_map.confidence >= 0.68:
            return False
        if explain.scoring.conflict_penalty >= 0.14:
            return False
        if cumulative_confidence < max(profile.min_signal_confidence, 0.52):
            return False
        if agreement_ratio < max(profile.min_agreement_ratio, 0.55):
            return False
        return signal.confidence >= max(profile.min_signal_confidence - 0.06, 0.50)

    def _liquidity_magnet_score(
        self,
        direction: Direction,
        current_price: float,
        liq_map: LiquidationMapAdvancedResponse,
    ) -> float:
        clusters = liq_map.clusters_above if direction == Direction.LONG else liq_map.clusters_below
        target = self._nearest_cluster(clusters, current_price)
        if target is None or current_price <= 0:
            return 0.0

        distance_bps = abs(target.center_price - current_price) / current_price * 10_000
        strength = max(0.0, min(target.total_strength, 1.0))
        if distance_bps <= 30:
            proximity = 0.06
        elif distance_bps <= 60:
            proximity = 0.04
        elif distance_bps <= 100:
            proximity = 0.02
        else:
            proximity = 0.0
        return min(0.08, proximity + (strength * 0.02))

    @staticmethod
    def _nearest_cluster(clusters: list[LiquidationCluster], current_price: float) -> LiquidationCluster | None:
        if not clusters:
            return None
        return min(clusters, key=lambda cluster: abs(cluster.center_price - current_price))

    def _entry_review_snapshot(
        self,
        *,
        status: DemoBotStatus,
        signal: SignalResult,
        explain: SignalExplainResult,
        liq_map: LiquidationMapAdvancedResponse | None,
        behavior: dict[str, Any],
    ) -> dict[str, Any]:
        scoring = getattr(explain, "scoring", None)
        risk = getattr(explain, "risk", None)
        return {
            "symbol": status.symbol,
            "mode": status.mode,
            "market_behavior": {
                "bias": behavior.get("bias"),
                "bias_strength": behavior.get("bias_strength"),
                "mode": behavior.get("mode"),
            },
            "signal": {
                "direction": signal.direction.value,
                "confidence": signal.confidence,
                "signal_quality": signal.signal_quality,
                "horizon": signal.horizon,
                "reasons": signal.reasons[:6],
                "entry_price": signal.current_price,
                "take_profit": signal.tp,
                "stop_loss": signal.sl,
            },
            "scoring": {
                "confidence": self._safe_float(getattr(scoring, "confidence", 0.0)),
                "raw_score": self._safe_float(getattr(scoring, "raw_score", 0.0)),
                "alignment_bonus": self._safe_float(getattr(scoring, "alignment_bonus", 0.0)),
                "conflict_penalty": self._safe_float(getattr(scoring, "conflict_penalty", 0.0)),
                "execution_penalty": self._safe_float(getattr(scoring, "execution_penalty", 0.0)),
                "crowding_penalty": self._safe_float(getattr(scoring, "crowding_penalty", 0.0)),
            },
            "risk": {
                "stop_bps": self._safe_float(getattr(risk, "stop_bps", 0.0)),
                "tp_bps": self._safe_float(getattr(risk, "tp_bps", 0.0)),
                "risk_reward": self._safe_float(getattr(risk, "risk_reward", 0.0)),
            },
            "liquidity_map": {
                "dominant_pull": liq_map.dominant_pull.value if liq_map else "FLAT",
                "confidence": liq_map.confidence if liq_map else 0.0,
                "cluster_above": self._cluster_snapshot(self._nearest_cluster(liq_map.clusters_above, signal.current_price)) if liq_map else None,
                "cluster_below": self._cluster_snapshot(self._nearest_cluster(liq_map.clusters_below, signal.current_price)) if liq_map else None,
            },
            "account": {
                "available_balance": status.available_balance,
                "wallet_balance": status.wallet_balance,
                "account_equity": status.account_equity,
                "risk_per_trade_pct": status.risk_per_trade_pct,
                "max_margin_fraction": status.max_margin_fraction,
                "leverage": status.leverage,
            },
        }

    def _position_review_snapshot(
        self,
        *,
        status: DemoBotStatus,
        signal: SignalResult,
        explain: SignalExplainResult,
        liq_map: LiquidationMapAdvancedResponse | None,
        behavior: dict[str, Any],
    ) -> dict[str, Any]:
        position = status.open_position
        context = self._active_entry_context or {}
        scoring = getattr(explain, "scoring", None)
        return {
            "symbol": status.symbol,
            "mode": status.mode,
            "market_behavior": {
                "bias": behavior.get("bias"),
                "bias_strength": behavior.get("bias_strength"),
                "mode": behavior.get("mode"),
            },
            "position": {
                "side": position.side.value if position else "FLAT",
                "size": position.size if position else 0.0,
                "entry_price": position.entry_price if position else 0.0,
                "mark_price": position.mark_price if position else 0.0,
                "take_profit": position.take_profit if position else None,
                "stop_loss": position.stop_loss if position else None,
                "unrealized_pnl": position.unrealized_pnl if position else 0.0,
                "unrealized_roi_pct": position.unrealized_roi_pct if position else 0.0,
                "strategy_mode": context.get("strategy_mode"),
            },
            "signal": {
                "direction": signal.direction.value,
                "confidence": signal.confidence,
                "signal_quality": signal.signal_quality,
                "reasons": signal.reasons[:6],
            },
            "scoring": {
                "confidence": self._safe_float(getattr(scoring, "confidence", 0.0)),
                "raw_score": self._safe_float(getattr(scoring, "raw_score", 0.0)),
                "alignment_bonus": self._safe_float(getattr(scoring, "alignment_bonus", 0.0)),
                "conflict_penalty": self._safe_float(getattr(scoring, "conflict_penalty", 0.0)),
            },
            "liquidity_map": {
                "dominant_pull": liq_map.dominant_pull.value if liq_map else "FLAT",
                "confidence": liq_map.confidence if liq_map else 0.0,
            },
        }

    @staticmethod
    def _cluster_snapshot(cluster: LiquidationCluster | None) -> dict[str, float] | None:
        if cluster is None:
            return None
        center_price = float(getattr(cluster, "center_price", 0.0) or 0.0)
        return {
            "center_price": center_price,
            "min_price": float(getattr(cluster, "min_price", center_price) or center_price),
            "max_price": float(getattr(cluster, "max_price", center_price) or center_price),
            "total_strength": float(getattr(cluster, "total_strength", 0.0) or 0.0),
            "contributing_levels": float(getattr(cluster, "contributing_levels", 1) or 1),
        }

    def _capture_training_snapshot(
        self,
        *,
        status: DemoBotStatus,
        explain: SignalExplainResult,
        cumulative: Any,
        liq_map: LiquidationMapAdvancedResponse | None,
        behavior: dict[str, Any],
        signal: SignalResult,
        decision_action: str,
        took_trade: bool,
    ) -> None:
        if not hasattr(self.signal_engine, "capture_training_snapshot"):
            return
        open_position = status.open_position.model_dump(mode="json") if status.open_position is not None else None
        ai_decision = status.ai_last_decision.model_dump(mode="json") if status.ai_last_decision is not None else None
        self.signal_engine.capture_training_snapshot(
            symbol=status.symbol,
            exchange=status.exchange,
            environment=status.environment,
            session_id=status.session_id,
            mode=status.mode,
            explain=explain,
            cumulative=cumulative,
            liq_map=liq_map,
            behavior=behavior,
            bot_signal=signal,
            open_position=open_position,
            ai_decision=ai_decision,
            decision_action=decision_action,
            took_trade=took_trade,
        )

    def _apply_ai_signal_adjustment(self, signal: SignalResult, decision: AITradeDecision | None) -> SignalResult:
        if decision is None or abs(decision.confidence_adjustment) < 1e-9:
            return signal
        confidence = max(0.0, min(0.99, signal.confidence + decision.confidence_adjustment))
        reasons = list(signal.reasons)
        reasons.append(f"AI supervisor adjusted confidence by {decision.confidence_adjustment:+.2f}")
        return signal.model_copy(
            update={
                "confidence": confidence,
                "signal_quality": self._quality_for_confidence(confidence, signal.signal_quality),
                "reasons": reasons[:8],
            }
        )

    @staticmethod
    def _quality_for_confidence(confidence: float, fallback: str) -> str:
        if confidence >= 0.78:
            return "HIGH"
        if confidence >= 0.64:
            return "ELEVATED"
        if confidence >= 0.55:
            return "STANDARD"
        if confidence >= 0.40:
            return "LOW"
        return fallback

    def _review_entry_with_ai(
        self,
        *,
        status: DemoBotStatus,
        signal: SignalResult,
        explain: SignalExplainResult,
        liq_map: LiquidationMapAdvancedResponse | None,
        behavior: dict[str, Any],
    ) -> tuple[SignalResult, float, bool]:
        decision = self.supervisor.review_entry(
            self._entry_review_snapshot(
                status=status,
                signal=signal,
                explain=explain,
                liq_map=liq_map,
                behavior=behavior,
            )
        )
        if decision is None:
            status.ai_last_decision = None
            return signal, 1.0, True

        status.ai_last_decision = decision
        adjusted_signal = self._apply_ai_signal_adjustment(signal, decision)
        if decision.entry_verdict == "BLOCK":
            return adjusted_signal, 0.0, False
        if decision.entry_verdict == "ALLOW_REDUCED":
            return adjusted_signal, decision.size_multiplier, True
        return adjusted_signal, 1.0, True

    def _review_position_with_ai(
        self,
        *,
        status: DemoBotStatus,
        signal: SignalResult,
        explain: SignalExplainResult,
        liq_map: LiquidationMapAdvancedResponse | None,
        behavior: dict[str, Any],
    ) -> bool:
        decision = self.supervisor.review_position(
            self._position_review_snapshot(
                status=status,
                signal=signal,
                explain=explain,
                liq_map=liq_map,
                behavior=behavior,
            )
        )
        if decision is None:
            status.ai_last_decision = None
            return False

        status.ai_last_decision = decision
        if decision.exit_action == "EXIT_NOW":
            self._close_position(status, reason="ai_exit_now")
            return True
        if decision.exit_action == "TAKE_PARTIAL":
            self._take_partial_position(status, size_fraction=0.25, reason="ai_take_partial")
            return True
        if decision.exit_action in {"MOVE_STOP", "TRAIL_STOP"}:
            self._adjust_position_stop(status, decision)
            return True
        return False

    def _run_cycle(self) -> None:
        status = self._with_ai_metadata(self._status_snapshot())
        if not status.running:
            return

        now_ms = int(time.time() * 1000)
        client = self._client()
        explain, cumulative, liq_map, signal = self._build_live_signal_context(status.symbol)
        behavior = self.signal_engine.get_market_behavior(status.symbol)

        status.last_signal = signal
        status.last_error = None
        self._refresh_balances(status)
        self._sync_position(status)
        self._sync_closed_trades(status)

        if status.open_position is not None:
            if self._should_flip(
                mode=status.mode,
                signal=signal,
                liq_map=liq_map,
                behavior=behavior,
                position=status.open_position,
                cumulative=cumulative,
            ):
                self._capture_training_snapshot(
                    status=status,
                    explain=explain,
                    cumulative=cumulative,
                    liq_map=liq_map,
                    behavior=behavior,
                    signal=signal,
                    decision_action="signal_flip",
                    took_trade=False,
                )
                self._close_position(status, reason="signal_flip")
                refreshed = self._status_snapshot()
                refreshed.last_signal = signal
                refreshed.last_updated_at = now_ms
                self._set_status(refreshed)
            elif self._manage_open_position_dynamically(
                status=status,
                signal=signal,
                liq_map=liq_map,
                behavior=behavior,
                cumulative=cumulative,
            ):
                self._capture_training_snapshot(
                    status=status,
                    explain=explain,
                    cumulative=cumulative,
                    liq_map=liq_map,
                    behavior=behavior,
                    signal=signal,
                    decision_action=status.last_action or "dynamic_position_management",
                    took_trade=False,
                )
                refreshed = self._with_ai_metadata(self._status_snapshot())
                refreshed.last_signal = signal
                refreshed.last_updated_at = now_ms
                self._set_status(refreshed)
            elif self._review_position_with_ai(
                status=status,
                signal=signal,
                explain=explain,
                liq_map=liq_map,
                behavior=behavior,
            ):
                self._capture_training_snapshot(
                    status=status,
                    explain=explain,
                    cumulative=cumulative,
                    liq_map=liq_map,
                    behavior=behavior,
                    signal=signal,
                    decision_action=status.ai_last_decision.exit_action.lower() if status.ai_last_decision else "ai_position_action",
                    took_trade=False,
                )
                refreshed = self._with_ai_metadata(self._status_snapshot())
                refreshed.last_signal = signal
                refreshed.last_updated_at = now_ms
                self._set_status(refreshed)
            else:
                status.last_action = "monitoring_position"
                status.last_updated_at = now_ms
                self._capture_training_snapshot(
                    status=status,
                    explain=explain,
                    cumulative=cumulative,
                    liq_map=liq_map,
                    behavior=behavior,
                    signal=signal,
                    decision_action="monitoring_position",
                    took_trade=False,
                )
                self._set_status(status)
            return

        if status.cooldown_until and now_ms < status.cooldown_until:
            status.last_action = "cooldown_active"
            status.last_updated_at = now_ms
            self._capture_training_snapshot(
                status=status,
                explain=explain,
                cumulative=cumulative,
                liq_map=liq_map,
                behavior=behavior,
                signal=signal,
                decision_action="cooldown_active",
                took_trade=False,
            )
            self._set_status(status)
            return

        if not self._should_enter(
            status=status,
            signal=signal,
            liq_map=liq_map,
            behavior=behavior,
            explain=explain,
            cumulative=cumulative,
        ):
            status.last_action = "waiting_for_setup"
            status.last_updated_at = now_ms
            self._capture_training_snapshot(
                status=status,
                explain=explain,
                cumulative=cumulative,
                liq_map=liq_map,
                behavior=behavior,
                signal=signal,
                decision_action="waiting_for_setup",
                took_trade=False,
            )
            self._set_status(status)
            return

        signal, ai_size_multiplier, allowed_by_ai = self._review_entry_with_ai(
            status=status,
            signal=signal,
            explain=explain,
            liq_map=liq_map,
            behavior=behavior,
        )
        status.last_signal = signal
        if not allowed_by_ai:
            status.last_action = "ai_blocked_entry"
            status.last_updated_at = now_ms
            self._capture_training_snapshot(
                status=status,
                explain=explain,
                cumulative=cumulative,
                liq_map=liq_map,
                behavior=behavior,
                signal=signal,
                decision_action="ai_blocked_entry",
                took_trade=False,
            )
            self._set_status(status)
            return

        setup = self._assess_setup(
            signal=signal,
            cumulative=cumulative,
            behavior=behavior,
            liq_map=liq_map,
            mode=status.mode,
        )
        order_plan = self._build_entry_plan(status=status, signal=signal, size_multiplier=ai_size_multiplier, setup=setup)
        if order_plan is None:
            status.last_action = "setup_rejected_by_risk"
            status.last_updated_at = now_ms
            self._capture_training_snapshot(
                status=status,
                explain=explain,
                cumulative=cumulative,
                liq_map=liq_map,
                behavior=behavior,
                signal=signal,
                decision_action="setup_rejected_by_risk",
                took_trade=False,
            )
            self._set_status(status)
            return

        applied_leverage = self._safe_float(order_plan.get("applied_leverage")) or status.leverage
        self._ensure_leverage(status.symbol, applied_leverage)
        self._capture_training_snapshot(
            status=status,
            explain=explain,
            cumulative=cumulative,
            liq_map=liq_map,
            behavior=behavior,
            signal=signal,
            decision_action=f"entry_submitted_{signal.direction.value.lower()}",
            took_trade=True,
        )
        client.place_order(
            category=self.cfg.category,
            symbol=status.symbol,
            side="Buy" if signal.direction == Direction.LONG else "Sell",
            qty=order_plan["qty"],
            order_link_id=order_plan["order_link_id"],
            stop_loss=order_plan["stop_loss"],
        )
        self._active_entry_context = {
            "order_link_id": order_plan["order_link_id"],
            "entry_time": now_ms,
            "side": signal.direction,
            "entry_reason": order_plan["entry_reason"],
            "entry_signal_confidence": signal.confidence,
            "entry_signal_quality": signal.signal_quality,
            "signal_horizon": signal.horizon,
            "setup_type": order_plan.get("setup_type", setup.setup_type),
            "fakeout_risk": order_plan.get("fakeout_risk"),
            "opposite_move_close_r": order_plan.get("opposite_move_close_r"),
            "applied_leverage": applied_leverage,
            "target_liquidation_move_pct": self.cfg.target_liquidation_move_pct,
            "estimated_liquidation_price": order_plan.get("estimated_liquidation_price"),
            "estimated_liquidation_buffer_pct": order_plan.get("estimated_liquidation_buffer_pct"),
            "planned_take_profit": order_plan["take_profit"],
            "planned_stop_loss": order_plan["stop_loss"],
            "strategy_mode": self._strategy_mode(behavior, cumulative),
            "exit_strategy_armed": False,
            "breakeven_armed": False,
            "partial_taken": False,
            "profit_lock_armed": False,
            "pending_exit_reason": "",
            "ai_reason": status.ai_last_decision.reason if status.ai_last_decision else "",
        }
        status.last_action = f"entry_submitted_{signal.direction.value.lower()}"
        status.last_updated_at = now_ms
        self._set_status(status)
        self._sync_position(self._status_snapshot())

    def _refresh_balances(self, status: DemoBotStatus) -> None:
        payload = self._client().get_wallet_balance(account_type=self.cfg.account_type, coin="USDT")
        status.account_equity = self._safe_float(payload.get("totalEquity"))
        status.wallet_balance = self._safe_float(payload.get("walletBalance"))
        status.available_balance = self._safe_float(
            payload.get("totalAvailableBalance")
            or payload.get("availableToWithdraw")
            or payload.get("walletBalance")
            or payload.get("totalWalletBalance")
        )

    def _sync_position(self, status: DemoBotStatus) -> None:
        positions = self._client().get_positions(status.symbol, category=self.cfg.category)
        had_position = status.open_position is not None
        current = None
        for row in positions:
            size = self._safe_float(row.get("size"))
            if size > 0:
                current = row
                break

        if current is None:
            status.open_position = None
            if had_position:
                self._apply_adaptive_cooldown(status, reason=(self._active_entry_context or {}).get("pending_exit_reason"))
            self._set_status(status)
            return

        side = Direction.LONG if str(current.get("side", "")).lower() == "buy" else Direction.SHORT
        entry_price = self._safe_float(current.get("avgPrice"))
        mark_price = self._safe_float(current.get("markPrice"))
        size = self._safe_float(current.get("size"))
        position_value = self._safe_float(current.get("positionValue"))
        unrealized = self._safe_float(current.get("unrealisedPnl") or current.get("unrealizedPnl"))
        roi_pct = (unrealized / max(position_value / max(status.leverage, 1.0), 1e-9)) * 100.0 if position_value > 0 else 0.0
        context = self._active_entry_context or {}
        position_leverage = self._safe_float(current.get("leverage")) or status.leverage
        liquidation_price = self._safe_float(current.get("liqPrice")) or self._estimate_liquidation_price(side, entry_price, position_leverage)

        status.open_position = DemoBotPosition(
            symbol=status.symbol,
            side=side,
            size=size,
            entry_price=entry_price,
            mark_price=mark_price,
            leverage=position_leverage,
            take_profit=self._safe_float(current.get("takeProfit")) or None,
            stop_loss=self._safe_float(current.get("stopLoss")) or None,
            liquidation_price=liquidation_price or None,
            liquidation_buffer_pct=self._liquidation_buffer_pct(mark_price, liquidation_price),
            position_value=position_value,
            unrealized_pnl=unrealized,
            unrealized_roi_pct=roi_pct,
            order_link_id=context.get("order_link_id"),
            opened_at=context.get("entry_time"),
        )
        status.last_updated_at = int(time.time() * 1000)
        self._set_status(status)
        if self._active_entry_context and not self._active_entry_context.get("exit_strategy_armed"):
            self._arm_exit_strategy(status, status.open_position)

    def _sync_closed_trades(self, status: DemoBotStatus) -> None:
        if not status.session_id or not status.started_at:
            return
        rows = self._client().get_closed_pnl(
            status.symbol,
            category=self.cfg.category,
            limit=20,
            start_time=status.started_at,
        )
        if not rows:
            return

        recent: list[BotTrade] = []
        for row in rows:
            trade_id = self._closed_trade_id(row)
            if trade_id in self._seen_closed_trade_ids:
                continue
            self._seen_closed_trade_ids.add(trade_id)
            trade = self._closed_trade_to_bot_trade(status, row)
            if trade is None:
                continue
            recent.append(trade)

        if not recent:
            return

        merged = recent + list(status.recent_trades)
        merged.sort(key=lambda item: item.exit_time, reverse=True)
        status.recent_trades = merged[:50]
        status.total_trades = len(status.recent_trades)
        wins = len([trade for trade in status.recent_trades if trade.net_pnl > 0])
        status.last_action = "trade_history_synced"
        status.last_updated_at = int(time.time() * 1000)
        status.available_balance = max(status.available_balance, 0.0)
        status.open_position = None
        latest_reason = (self._active_entry_context or {}).get("pending_exit_reason")
        self._apply_adaptive_cooldown(status, reason=latest_reason, trade=recent[0])
        status.last_error = None
        status.wallet_balance = max(status.wallet_balance, 0.0)
        status.account_equity = max(status.account_equity, 0.0)
        status.win_rate = wins / max(status.total_trades, 1)
        self._set_status(status)
        self._active_entry_context = None

    def _closed_trade_to_bot_trade(self, status: DemoBotStatus, row: dict[str, Any]) -> BotTrade | None:
        qty = self._safe_float(row.get("qty") or row.get("closedSize") or row.get("size"))
        entry_price = self._safe_float(row.get("avgEntryPrice"))
        exit_price = self._safe_float(row.get("avgExitPrice"))
        entry_time = int(self._safe_float(row.get("createdTime")))
        exit_time = int(self._safe_float(row.get("updatedTime")))
        if qty <= 0 or entry_price <= 0 or exit_price <= 0 or exit_time <= 0:
            return None

        side = Direction.LONG if str(row.get("side", "")).lower() == "buy" else Direction.SHORT
        fee_paid = self._safe_float(row.get("openFee")) + self._safe_float(row.get("closeFee"))
        funding_paid = self._safe_float(row.get("totalFunding")) + self._safe_float(row.get("funding"))
        net_pnl = self._safe_float(row.get("closedPnl"))
        context = self._active_entry_context if self._active_entry_context and self._active_entry_context.get("side") == side else {}

        return BotTrade(
            run_id=status.session_id,
            side=side,
            entry_time=entry_time,
            exit_time=exit_time,
            entry_time_local=self.signal_engine._local_time_string(entry_time),
            exit_time_local=self.signal_engine._local_time_string(exit_time),
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
            duration_minutes=max((exit_time - entry_time) / 60000.0, 0.0),
            roi_pct=(net_pnl / max(qty * entry_price, 1e-9)) * 100.0,
            gross_pnl=net_pnl + fee_paid - funding_paid,
            fee_paid=fee_paid,
            funding_paid=funding_paid,
            net_pnl=net_pnl,
            entry_reason=" | ".join(
                part for part in [context.get("entry_reason", "bybit_demo_entry"), context.get("ai_reason", "")] if part
            ),
            exit_reason=str(row.get("execType") or row.get("closedPnlType") or "exchange_exit"),
            entry_signal_direction=side,
            entry_signal_confidence=context.get("entry_signal_confidence", 0.0),
            entry_signal_quality=context.get("entry_signal_quality", "UNKNOWN"),
            signal_horizon=context.get("signal_horizon", "N/A"),
            signal_source=self.SIGNAL_SOURCE,
            reason=str(row.get("execType") or row.get("closedPnlType") or "exchange_exit"),
        )

    def _historical_trade_to_bot_trade(self, symbol: str, row: dict[str, Any]) -> BotTrade | None:
        qty = self._safe_float(row.get("qty") or row.get("closedSize") or row.get("size"))
        entry_price = self._safe_float(row.get("avgEntryPrice"))
        exit_price = self._safe_float(row.get("avgExitPrice"))
        entry_time = int(self._safe_float(row.get("createdTime")))
        exit_time = int(self._safe_float(row.get("updatedTime")))
        if qty <= 0 or entry_price <= 0 or exit_price <= 0 or exit_time <= 0:
            return None

        side = Direction.LONG if str(row.get("side", "")).lower() == "buy" else Direction.SHORT
        fee_paid = self._safe_float(row.get("openFee")) + self._safe_float(row.get("closeFee"))
        funding_paid = self._safe_float(row.get("totalFunding")) + self._safe_float(row.get("funding"))
        net_pnl = self._safe_float(row.get("closedPnl"))
        return BotTrade(
            run_id=str(row.get("orderId") or row.get("execId") or "") or None,
            side=side,
            entry_time=entry_time,
            exit_time=exit_time,
            entry_time_local=self.signal_engine._local_time_string(entry_time),
            exit_time_local=self.signal_engine._local_time_string(exit_time),
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
            duration_minutes=max((exit_time - entry_time) / 60000.0, 0.0),
            roi_pct=(net_pnl / max(qty * entry_price, 1e-9)) * 100.0,
            gross_pnl=net_pnl + fee_paid - funding_paid,
            fee_paid=fee_paid,
            funding_paid=funding_paid,
            net_pnl=net_pnl,
            entry_reason=f"bybit_demo_history:{symbol}",
            exit_reason=str(row.get("execType") or row.get("closedPnlType") or "exchange_exit"),
            entry_signal_direction=side,
            entry_signal_confidence=0.0,
            entry_signal_quality="HISTORICAL",
            signal_horizon="N/A",
            signal_source=self.SIGNAL_SOURCE,
            reason=str(row.get("execType") or row.get("closedPnlType") or "exchange_exit"),
        )

    def _should_enter(
        self,
        *,
        status: DemoBotStatus,
        signal: SignalResult,
        liq_map: LiquidationMapAdvancedResponse | None,
        behavior: dict[str, Any],
        explain: SignalExplainResult,
        cumulative: Any,
    ) -> bool:
        profile = self._profile(status.mode)
        setup = self._assess_setup(
            signal=signal,
            cumulative=cumulative,
            behavior=behavior,
            liq_map=liq_map,
            mode=status.mode,
        )
        if signal.direction == Direction.FLAT:
            return False
        if getattr(cumulative, "aggregate_direction", Direction.FLAT) != signal.direction:
            return False
        if self._safe_float(getattr(cumulative, "agreement_ratio", 0.0)) < profile.min_agreement_ratio:
            return False
        if "no_edge" in signal.reasons and not self._allow_no_edge_override(signal=signal, liq_map=liq_map, behavior=behavior):
            return False
        if signal.signal_quality not in profile.allowed_qualities:
            return False
        if signal.confidence < profile.min_signal_confidence:
            return False
        if setup.setup_type == "BREAKOUT" and signal.confidence < self._breakout_confidence_threshold(status.mode):
            return False
        if not setup.entry_allowed:
            return False
        if liq_map is None:
            return False
        if not self._liquidity_allows_entry(
            signal=signal,
            liq_map=liq_map,
            cumulative=cumulative,
            explain=explain,
            profile=profile,
            behavior=behavior,
        ):
            return False
        if explain.scoring.conflict_penalty >= 0.18:
            return False

        bias = str(behavior.get("bias", "NEUTRAL")).upper()
        bias_strength = abs(float(behavior.get("bias_strength", 0.0) or 0.0))
        if bias == "BEARISH" and signal.direction == Direction.LONG and bias_strength >= 0.35:
            return signal.confidence >= profile.countertrend_confidence
        if bias == "BULLISH" and signal.direction == Direction.SHORT and bias_strength >= 0.35:
            return signal.confidence >= profile.countertrend_confidence
        return True

    @staticmethod
    def _breakout_confidence_threshold(mode: str) -> float:
        normalized = str(mode or "BALANCED").upper()
        if normalized == "SCALPING":
            return 0.62
        if normalized == "AGGRESSIVE":
            return 0.66
        if normalized == "CONSERVATIVE":
            return 0.74
        return 0.70

    def _breakout_is_confirmed(
        self,
        *,
        signal: SignalResult,
        cumulative: Any,
        behavior: dict[str, Any],
    ) -> bool:
        if signal.direction == Direction.FLAT:
            return False
        market_mode = str(getattr(cumulative, "market_mode", behavior.get("mode", "UNKNOWN")) or "").upper()
        if market_mode != "TRENDING":
            return False

        timeframes = list(getattr(cumulative, "timeframes", []) or [])
        aligned = [
            timeframe
            for timeframe in timeframes
            if getattr(timeframe, "direction", Direction.FLAT) == signal.direction
            and str(getattr(timeframe, "market_mode", "") or "").upper() == "TRENDING"
        ]
        if not aligned:
            return False

        for timeframe in aligned:
            volume_ratio = self._safe_float(getattr(timeframe, "volume_ratio", 0.0))
            breakout_failure_rate = self._safe_float(getattr(timeframe, "breakout_failure_rate", 1.0))
            atr_state = str(getattr(timeframe, "atr_state", "") or "").upper()
            adx14 = self._safe_float(getattr(timeframe, "adx14", 0.0))
            close = self._safe_float(getattr(timeframe, "close", signal.current_price))
            ema20 = self._safe_float(getattr(timeframe, "ema20", 0.0))
            ema50 = self._safe_float(getattr(timeframe, "ema50", 0.0))
            vwap = self._safe_float(getattr(timeframe, "vwap", 0.0))
            pattern = str(getattr(timeframe, "pattern", "") or "").lower()
            if volume_ratio < self.cfg.breakout_min_volume_ratio:
                continue
            if breakout_failure_rate > self.cfg.breakout_max_failure_rate:
                continue
            if atr_state == "COMPRESSING":
                continue
            if adx14 and adx14 < 18.0:
                continue

            if signal.direction == Direction.LONG:
                structure_ok = close > ema20 > ema50 and close >= vwap
                pattern_ok = pattern in {"trend continuation up", "bullish engulfing", "morning star"}
            else:
                structure_ok = close < ema20 < ema50 and close <= vwap
                pattern_ok = pattern in {"trend continuation down", "bearish engulfing", "evening star"}
            if structure_ok or pattern_ok:
                return True
        return False

    def _assess_setup(
        self,
        *,
        signal: SignalResult,
        cumulative: Any,
        behavior: dict[str, Any],
        liq_map: LiquidationMapAdvancedResponse | None,
        mode: str,
    ) -> _SetupAssessment:
        market_mode = str(getattr(cumulative, "market_mode", behavior.get("mode", "UNKNOWN")) or "").upper()
        primary = self._primary_timeframe(cumulative, signal.direction)
        breakout_confirmed = self._breakout_is_confirmed(signal=signal, cumulative=cumulative, behavior=behavior)
        fakeout_risk = self._fakeout_risk(signal=signal, timeframe=primary, liq_map=liq_map, market_mode=market_mode)
        setup_type = "TRANSITIONAL"
        entry_allowed = False
        rationale = "transitional_or_unclear"

        breakout_threshold, trend_threshold, range_threshold = self._setup_entry_thresholds(mode)

        if market_mode == "TRENDING":
            if breakout_confirmed:
                setup_type = "BREAKOUT"
                entry_allowed = fakeout_risk <= breakout_threshold
                rationale = "confirmed_breakout" if entry_allowed else "breakout_fakeout_risk_too_high"
            elif self._trend_continuation_confirmed(signal=signal, timeframe=primary):
                setup_type = "TREND_CONTINUATION"
                entry_allowed = fakeout_risk <= trend_threshold
                rationale = "trend_continuation" if entry_allowed else "trend_setup_too_noisy"
        elif market_mode == "RANGING":
            if self._range_reversion_confirmed(signal=signal, timeframe=primary):
                setup_type = "RANGE_REVERSION"
                entry_allowed = fakeout_risk <= range_threshold
                rationale = "range_reversion" if entry_allowed else "range_fakeout_risk_too_high"

        dynamic_close_r = self._dynamic_opposite_move_close_r(
            setup_type=setup_type,
            fakeout_risk=fakeout_risk,
            mode=mode,
        )
        return _SetupAssessment(
            setup_type=setup_type,
            breakout_confirmed=breakout_confirmed,
            entry_allowed=entry_allowed,
            fakeout_risk=fakeout_risk,
            opposite_move_close_r=dynamic_close_r,
            rationale=rationale,
        )

    @staticmethod
    def _setup_entry_thresholds(mode: str) -> tuple[float, float, float]:
        normalized = str(mode or "BALANCED").upper()
        if normalized == "SCALPING":
            return 0.72, 0.82, 0.72
        if normalized == "AGGRESSIVE":
            return 0.66, 0.76, 0.68
        if normalized == "CONSERVATIVE":
            return 0.52, 0.64, 0.56
        return 0.58, 0.70, 0.62

    def _primary_timeframe(self, cumulative: Any, direction: Direction) -> Any | None:
        timeframes = list(getattr(cumulative, "timeframes", []) or [])
        aligned = [timeframe for timeframe in timeframes if getattr(timeframe, "direction", Direction.FLAT) == direction]
        if aligned:
            return max(aligned, key=lambda timeframe: self._safe_float(getattr(timeframe, "confidence", 0.0)))
        return timeframes[0] if timeframes else None

    def _trend_continuation_confirmed(self, *, signal: SignalResult, timeframe: Any | None) -> bool:
        if timeframe is None or signal.direction == Direction.FLAT:
            return False
        close = self._safe_float(getattr(timeframe, "close", signal.current_price))
        ema20 = self._safe_float(getattr(timeframe, "ema20", 0.0))
        ema50 = self._safe_float(getattr(timeframe, "ema50", 0.0))
        vwap = self._safe_float(getattr(timeframe, "vwap", 0.0))
        adx14 = self._safe_float(getattr(timeframe, "adx14", 0.0))
        atr_state = str(getattr(timeframe, "atr_state", "") or "").upper()
        if signal.direction == Direction.LONG:
            structure_ok = close > ema20 > ema50 and close >= vwap
        else:
            structure_ok = close < ema20 < ema50 and close <= vwap
        return structure_ok and adx14 >= 18.0 and atr_state != "COMPRESSING"

    def _range_reversion_confirmed(self, *, signal: SignalResult, timeframe: Any | None) -> bool:
        if timeframe is None or signal.direction == Direction.FLAT:
            return False
        close = self._safe_float(getattr(timeframe, "close", signal.current_price))
        nearest_support = self._safe_float(getattr(timeframe, "nearest_support", 0.0))
        nearest_resistance = self._safe_float(getattr(timeframe, "nearest_resistance", 0.0))
        rsi14 = self._safe_float(getattr(timeframe, "rsi14", 0.0))
        pattern = str(getattr(timeframe, "pattern", "") or "").lower()
        if signal.direction == Direction.LONG and nearest_support > 0.0:
            distance_pct = ((close - nearest_support) / max(close, 1e-9)) * 100.0
            return 0.0 <= distance_pct <= 0.9 and rsi14 <= 44.0 and pattern in {"hammer reversal", "bullish engulfing", "morning star"}
        if signal.direction == Direction.SHORT and nearest_resistance > 0.0:
            distance_pct = ((nearest_resistance - close) / max(close, 1e-9)) * 100.0
            return 0.0 <= distance_pct <= 0.9 and rsi14 >= 56.0 and pattern in {"shooting star", "bearish engulfing", "evening star"}
        return False

    def _fakeout_risk(
        self,
        *,
        signal: SignalResult,
        timeframe: Any | None,
        liq_map: LiquidationMapAdvancedResponse | None,
        market_mode: str,
    ) -> float:
        if timeframe is None:
            return 0.75
        failure_rate = self._safe_float(getattr(timeframe, "breakout_failure_rate", 0.5))
        volume_ratio = self._safe_float(getattr(timeframe, "volume_ratio", 1.0))
        adx14 = self._safe_float(getattr(timeframe, "adx14", 18.0))
        atr_state = str(getattr(timeframe, "atr_state", "") or "").upper()
        close = self._safe_float(getattr(timeframe, "close", signal.current_price))
        vwap = self._safe_float(getattr(timeframe, "vwap", signal.current_price))
        pattern = str(getattr(timeframe, "pattern", "") or "").lower()

        risk = min(1.0, failure_rate / 0.65) * 0.38
        if volume_ratio < self.cfg.breakout_min_volume_ratio:
            risk += min(0.24, ((self.cfg.breakout_min_volume_ratio - volume_ratio) / max(self.cfg.breakout_min_volume_ratio, 1e-9)) * 0.24)
        if adx14 < 18.0:
            risk += min(0.16, ((18.0 - adx14) / 18.0) * 0.16)
        if atr_state == "COMPRESSING":
            risk += 0.14
        elif atr_state == "NEUTRAL":
            risk += 0.05
        if signal.direction == Direction.LONG and close < vwap:
            risk += 0.10
        if signal.direction == Direction.SHORT and close > vwap:
            risk += 0.10
        if "mixed structure" in pattern or "building context" in pattern:
            risk += 0.08
        if market_mode == "RANGING":
            risk += 0.06
        if liq_map is not None and liq_map.dominant_pull not in {Direction.FLAT, signal.direction}:
            risk += min(0.12, liq_map.confidence * 0.12)
        return max(0.0, min(risk, 1.0))

    def _dynamic_opposite_move_close_r(self, *, setup_type: str, fakeout_risk: float, mode: str) -> float:
        baseline = self.cfg.opposite_move_close_r
        if setup_type == "BREAKOUT":
            base = min(baseline, 0.78)
        elif setup_type == "RANGE_REVERSION":
            base = min(baseline, 0.72)
        elif setup_type == "TREND_CONTINUATION":
            base = max(baseline, 0.96)
        else:
            base = min(baseline, 0.60)

        mode_adjustment = 0.0
        normalized_mode = str(mode or "BALANCED").upper()
        if normalized_mode == "CONSERVATIVE":
            mode_adjustment = -0.05
        elif normalized_mode == "AGGRESSIVE":
            mode_adjustment = 0.05 if setup_type == "TREND_CONTINUATION" else 0.02
        elif normalized_mode == "SCALPING":
            mode_adjustment = -0.10 if setup_type in {"BREAKOUT", "RANGE_REVERSION"} else -0.04

        adjusted = base - (fakeout_risk * 0.20) + mode_adjustment
        return max(0.45, min(adjusted, 1.10))

    def _allow_no_edge_override(
        self,
        *,
        signal: SignalResult,
        liq_map: LiquidationMapAdvancedResponse | None,
        behavior: dict[str, Any],
    ) -> bool:
        if "no_edge" not in signal.reasons:
            return True
        if liq_map is None:
            return False
        if liq_map.dominant_pull != signal.direction or liq_map.confidence < 0.62:
            return False
        market_mode = str(behavior.get("mode", "UNKNOWN")).upper()
        if market_mode not in {"TRENDING", "RANGING"}:
            return False
        return signal.confidence >= 0.84 and signal.signal_quality in {"HIGH", "ELEVATED"}

    def _should_flip(
        self,
        *,
        mode: str,
        signal: SignalResult,
        liq_map: LiquidationMapAdvancedResponse | None,
        behavior: dict[str, Any],
        position: DemoBotPosition,
        cumulative: Any,
    ) -> bool:
        if signal.direction == Direction.FLAT:
            return False
        if signal.direction == position.side:
            return False
        if getattr(cumulative, "aggregate_direction", Direction.FLAT) != signal.direction:
            return False
        agreement_ratio = self._safe_float(getattr(cumulative, "agreement_ratio", 0.0))
        setup = self._assess_setup(
            signal=signal,
            cumulative=cumulative,
            behavior=behavior,
            liq_map=liq_map,
            mode=mode,
        )
        if (
            setup.setup_type == "BREAKOUT"
            and setup.breakout_confirmed
            and setup.entry_allowed
            and signal.confidence >= max(self.cfg.live_flip_min_confidence, 0.68)
            and agreement_ratio >= self.cfg.live_flip_min_agreement_ratio
        ):
            return True
        if signal.confidence < self.cfg.live_flip_min_confidence or agreement_ratio < self.cfg.live_flip_min_agreement_ratio:
            return False
        if liq_map is not None and liq_map.dominant_pull not in {Direction.FLAT, signal.direction} and liq_map.confidence >= 0.70:
            return False
        return True

    def _build_entry_plan(
        self,
        *,
        status: DemoBotStatus,
        signal: Any,
        size_multiplier: float = 1.0,
        setup: _SetupAssessment | None = None,
    ) -> dict[str, str] | None:
        instrument = self._instrument_info(status.symbol)
        ticker = self._client().get_ticker(status.symbol, category=self.cfg.category)
        mark_price = self._safe_float(ticker.get("markPrice") or signal.current_price)
        stop_price = float(signal.sl)
        tp_price = float(signal.tp)
        stop_distance = abs(mark_price - stop_price)
        if mark_price <= 0 or stop_distance <= 0:
            return None
        if signal.direction == Direction.LONG and not (stop_price < mark_price < tp_price):
            return None
        if signal.direction == Direction.SHORT and not (tp_price < mark_price < stop_price):
            return None
        applied_leverage = min(status.leverage, self._max_leverage_for_target_liquidation_move())
        estimated_liquidation_price = self._estimate_liquidation_price(signal.direction, mark_price, applied_leverage)
        liquidation_buffer_pct = self._liquidation_buffer_pct(mark_price, estimated_liquidation_price)

        risk_budget = max(status.available_balance, status.wallet_balance, status.account_equity, 0.0) * status.risk_per_trade_pct
        qty_from_risk = risk_budget / stop_distance
        max_notional = (
            max(status.available_balance, status.wallet_balance, status.account_equity, 0.0)
            * status.max_margin_fraction
            * applied_leverage
        )
        qty_from_margin = max_notional / mark_price if mark_price > 0 else 0.0
        raw_qty = min(qty_from_risk, qty_from_margin) * max(0.25, min(size_multiplier, 1.0))
        qty = self._quantize_down(raw_qty, instrument["qty_step"])
        if qty < instrument["min_qty"]:
            return None

        tp = self._quantize_down(tp_price, instrument["tick_size"])
        sl = self._quantize_down(stop_price, instrument["tick_size"])
        if tp <= 0 or sl <= 0:
            return None

        return {
            "qty": self._decimal_str(qty),
            "take_profit": self._decimal_str(tp),
            "stop_loss": self._decimal_str(sl),
            "applied_leverage": self._decimal_str(applied_leverage),
            "setup_type": setup.setup_type if setup else "UNKNOWN",
            "fakeout_risk": f"{setup.fakeout_risk:.2f}" if setup else None,
            "opposite_move_close_r": f"{setup.opposite_move_close_r:.2f}" if setup else self._decimal_str(self.cfg.opposite_move_close_r),
            "estimated_liquidation_price": self._decimal_str(estimated_liquidation_price) if estimated_liquidation_price else None,
            "estimated_liquidation_buffer_pct": f"{liquidation_buffer_pct:.2f}",
            "order_link_id": uuid4().hex[:24],
            "entry_reason": (
                f"mode={status.mode} | signal={signal.direction.value} | "
                f"confidence={signal.confidence:.2f} | quality={signal.signal_quality} | "
                f"setup={(setup.setup_type if setup else 'UNKNOWN')} | fakeout={(setup.fakeout_risk if setup else 0.0):.2f} | "
                f"breakout={'confirmed' if (setup and setup.breakout_confirmed) else 'not_confirmed'} | applied_lev={applied_leverage:.2f}x | "
                f"liq_target={self.cfg.target_liquidation_move_pct:.0f}% | liq_buffer={liquidation_buffer_pct:.2f}% | "
                f"opp_kill={(setup.opposite_move_close_r if setup else self.cfg.opposite_move_close_r):.2f}R | "
                f"ai={status.ai_last_decision.entry_verdict if status.ai_last_decision else 'OFF'} | "
                f"reasons={','.join(signal.reasons[:4])}"
            ),
        }

    def _manage_open_position_dynamically(
        self,
        *,
        status: DemoBotStatus,
        signal: SignalResult,
        liq_map: LiquidationMapAdvancedResponse | None,
        behavior: dict[str, Any],
        cumulative: Any,
    ) -> bool:
        position = status.open_position
        if position is None:
            return False

        context = self._active_entry_context or {}
        entry_time = int(self._safe_float(context.get("entry_time")))
        strategy_mode = str(context.get("strategy_mode") or self._strategy_mode(behavior, cumulative)).upper()
        plan = self._scalp_plan(status.mode, strategy_mode)
        r_multiple = self._position_r_multiple(position, context)
        holding_seconds = max(0, int(time.time()) - int(entry_time / 1000)) if entry_time > 0 else 0
        agreement_ratio = self._safe_float(getattr(cumulative, "agreement_ratio", 0.0))
        current_setup = self._assess_setup(
            signal=signal,
            cumulative=cumulative,
            behavior=behavior,
            liq_map=liq_map,
            mode=status.mode,
        )
        configured_opp_close_r = self._safe_float(context.get("opposite_move_close_r")) or self.cfg.opposite_move_close_r
        opposite_move_close_r = configured_opp_close_r

        if r_multiple <= -abs(opposite_move_close_r):
            self._close_position(status, reason="opposite_move_kill_switch")
            return True

        if (
            signal.direction != Direction.FLAT
            and signal.direction != position.side
            and current_setup.setup_type == "BREAKOUT"
            and current_setup.breakout_confirmed
            and current_setup.entry_allowed
            and signal.confidence >= max(self.cfg.live_flip_min_confidence, 0.68)
            and agreement_ratio >= self.cfg.live_flip_min_agreement_ratio
        ):
            self._close_position(status, reason="opposite_breakout_confirmed")
            return True

        if (
            holding_seconds >= plan.stale_after_seconds
            and r_multiple <= plan.stale_exit_max_r
            and signal.confidence < plan.stale_signal_confidence
            and agreement_ratio < plan.stale_agreement_ratio
        ):
            self._close_position(status, reason="stale_no_follow_through")
            return True

        if (
            liq_map is not None
            and liq_map.dominant_pull not in {Direction.FLAT, position.side}
            and liq_map.confidence >= plan.adverse_liquidity_confidence
            and r_multiple <= plan.adverse_liquidity_max_r
            and signal.direction != position.side
        ):
            self._close_position(status, reason="adverse_liquidity_shift")
            return True

        if (
            signal.direction != Direction.FLAT
            and signal.direction != position.side
            and current_setup.entry_allowed
            and signal.confidence >= self.cfg.live_flip_min_confidence
            and agreement_ratio >= self.cfg.live_flip_min_agreement_ratio
        ):
            if r_multiple <= 0.15:
                self._close_position(status, reason="live_market_flip_exit")
                return True
            if self._protect_position_at_r(status, protected_r=0.0, reason="live_flip_stop_tightened"):
                return True

        if signal.direction == Direction.FLAT and holding_seconds >= max(45, plan.stale_after_seconds // 2) and r_multiple > 0.08:
            self._close_position(status, reason="momentum_flattened_exit")
            return True

        if not context.get("breakeven_armed") and r_multiple >= plan.breakeven_r:
            if self._protect_position_at_r(status, protected_r=0.02, reason="scalp_break_even_armed"):
                context["breakeven_armed"] = True
                self._active_entry_context = context
                return True

        if not context.get("partial_taken") and r_multiple >= plan.partial_take_r:
            self._take_partial_position(status, size_fraction=plan.partial_fraction, reason="scalp_take_partial")
            context["partial_taken"] = True
            self._active_entry_context = context
            self._protect_position_at_r(status, protected_r=plan.partial_stop_r, reason="scalp_partial_protected")
            return True

        if not context.get("profit_lock_armed") and r_multiple >= plan.lock_profit_r:
            if self._protect_position_at_r(status, protected_r=plan.lock_stop_r, reason="scalp_profit_locked"):
                context["profit_lock_armed"] = True
                self._active_entry_context = context
                return True

        return False

    def _position_r_multiple(self, position: DemoBotPosition, context: dict[str, Any]) -> float:
        reference_stop = position.stop_loss or self._safe_float(context.get("planned_stop_loss"))
        if reference_stop <= 0.0:
            return 0.0
        risk_distance = abs(position.entry_price - reference_stop)
        if risk_distance <= 0.0:
            return 0.0
        if position.side == Direction.LONG:
            move = position.mark_price - position.entry_price
        else:
            move = position.entry_price - position.mark_price
        return move / risk_distance

    def _protect_position_at_r(self, status: DemoBotStatus, *, protected_r: float, reason: str) -> bool:
        position = status.open_position
        if position is None:
            return False
        instrument = self._instrument_info(status.symbol)
        context = self._active_entry_context or {}
        reference_stop = position.stop_loss or self._safe_float(context.get("planned_stop_loss"))
        risk_distance = abs(position.entry_price - reference_stop)
        if reference_stop <= 0.0 or risk_distance <= 0.0:
            return False

        if position.side == Direction.LONG:
            desired_stop = position.entry_price + (risk_distance * protected_r)
            protected_stop = min(position.mark_price - instrument["tick_size"], desired_stop)
        else:
            desired_stop = position.entry_price - (risk_distance * protected_r)
            protected_stop = max(position.mark_price + instrument["tick_size"], desired_stop)
        protected_stop = self._quantize_down(protected_stop, instrument["tick_size"])
        if protected_stop <= 0.0:
            return False

        self._client().set_trading_stop(
            category=self.cfg.category,
            symbol=status.symbol,
            stop_loss=self._decimal_str(protected_stop),
        )
        status.last_action = reason
        status.last_updated_at = int(time.time() * 1000)
        self._set_status(status)
        return True

    def _scalp_plan(self, mode: str, strategy_mode: str) -> _ScalpPlan:
        normalized = str(mode or "BALANCED").upper()
        if normalized == "CONSERVATIVE":
            plan = _ScalpPlan(
                stale_after_seconds=240,
                stale_exit_max_r=0.12,
                stale_signal_confidence=0.58,
                stale_agreement_ratio=0.48,
                breakeven_r=0.42,
                partial_take_r=0.62,
                partial_fraction=0.30,
                partial_stop_r=0.08,
                lock_profit_r=0.92,
                lock_stop_r=0.26,
                adverse_liquidity_confidence=0.72,
                adverse_liquidity_max_r=0.25,
                fast_reentry_seconds=20,
                stopout_reentry_seconds=45,
            )
        elif normalized == "SCALPING":
            plan = _ScalpPlan(
                stale_after_seconds=75,
                stale_exit_max_r=0.05,
                stale_signal_confidence=0.50,
                stale_agreement_ratio=0.20,
                breakeven_r=0.18,
                partial_take_r=0.28,
                partial_fraction=0.45,
                partial_stop_r=0.03,
                lock_profit_r=0.45,
                lock_stop_r=0.12,
                adverse_liquidity_confidence=0.56,
                adverse_liquidity_max_r=0.14,
                fast_reentry_seconds=5,
                stopout_reentry_seconds=10,
            )
        elif normalized == "AGGRESSIVE":
            plan = _ScalpPlan(
                stale_after_seconds=120,
                stale_exit_max_r=0.08,
                stale_signal_confidence=0.54,
                stale_agreement_ratio=0.30,
                breakeven_r=0.25,
                partial_take_r=0.38,
                partial_fraction=0.40,
                partial_stop_r=0.04,
                lock_profit_r=0.62,
                lock_stop_r=0.18,
                adverse_liquidity_confidence=0.64,
                adverse_liquidity_max_r=0.18,
                fast_reentry_seconds=8,
                stopout_reentry_seconds=20,
            )
        else:
            plan = _ScalpPlan(
                stale_after_seconds=180,
                stale_exit_max_r=0.10,
                stale_signal_confidence=0.56,
                stale_agreement_ratio=0.34,
                breakeven_r=0.32,
                partial_take_r=0.48,
                partial_fraction=0.35,
                partial_stop_r=0.05,
                lock_profit_r=0.74,
                lock_stop_r=0.20,
                adverse_liquidity_confidence=0.68,
                adverse_liquidity_max_r=0.22,
                fast_reentry_seconds=12,
                stopout_reentry_seconds=30,
            )

        if str(strategy_mode or "RANGING").upper() == "RANGING":
            return _ScalpPlan(
                stale_after_seconds=max(75, int(plan.stale_after_seconds * 0.75)),
                stale_exit_max_r=plan.stale_exit_max_r,
                stale_signal_confidence=plan.stale_signal_confidence,
                stale_agreement_ratio=plan.stale_agreement_ratio,
                breakeven_r=max(0.18, plan.breakeven_r - 0.05),
                partial_take_r=max(0.28, plan.partial_take_r - 0.10),
                partial_fraction=min(0.50, plan.partial_fraction + 0.05),
                partial_stop_r=plan.partial_stop_r,
                lock_profit_r=max(0.45, plan.lock_profit_r - 0.12),
                lock_stop_r=plan.lock_stop_r,
                adverse_liquidity_confidence=plan.adverse_liquidity_confidence,
                adverse_liquidity_max_r=plan.adverse_liquidity_max_r,
                fast_reentry_seconds=max(6, int(plan.fast_reentry_seconds * 0.8)),
                stopout_reentry_seconds=max(12, int(plan.stopout_reentry_seconds * 0.8)),
            )
        return plan

    def _apply_adaptive_cooldown(
        self,
        status: DemoBotStatus,
        *,
        reason: str | None = None,
        trade: BotTrade | None = None,
    ) -> None:
        context = self._active_entry_context or {}
        strategy_mode = str(context.get("strategy_mode") or "RANGING").upper()
        plan = self._scalp_plan(status.mode, strategy_mode)
        fast_reentry = min(status.cooldown_seconds, max(plan.fast_reentry_seconds, status.poll_interval_seconds))
        stopout_reentry = min(status.cooldown_seconds, max(plan.stopout_reentry_seconds, status.poll_interval_seconds))
        cooldown_seconds = status.cooldown_seconds

        if reason in {
            "signal_flip",
            "opposite_breakout_confirmed",
            "live_market_flip_exit",
            "stale_no_follow_through",
            "momentum_flattened_exit",
            "adverse_liquidity_shift",
            "opposite_move_kill_switch",
        }:
            cooldown_seconds = fast_reentry
        elif trade is not None:
            if trade.net_pnl > 0 and trade.duration_minutes <= 20:
                cooldown_seconds = fast_reentry
            elif trade.net_pnl <= 0 and trade.duration_minutes <= 20:
                cooldown_seconds = stopout_reentry

        status.cooldown_until = int(time.time() * 1000) + max(cooldown_seconds, 0) * 1000
        context["pending_exit_reason"] = reason or context.get("pending_exit_reason", "")
        self._active_entry_context = context

    def _close_position(self, status: DemoBotStatus, *, reason: str) -> None:
        if status.open_position is None:
            status.last_action = "no_position_to_close"
            status.last_updated_at = int(time.time() * 1000)
            self._set_status(status)
            return

        self._client().cancel_all_orders(status.symbol, category=self.cfg.category)
        self._client().place_order(
            category=self.cfg.category,
            symbol=status.symbol,
            side="Sell" if status.open_position.side == Direction.LONG else "Buy",
            qty=self._decimal_str(status.open_position.size),
            order_link_id=uuid4().hex[:24],
            reduce_only=True,
        )
        context = self._active_entry_context or {}
        context["pending_exit_reason"] = reason
        self._active_entry_context = context
        self._apply_adaptive_cooldown(status, reason=reason)
        status.last_action = reason
        status.last_updated_at = int(time.time() * 1000)
        self._set_status(status)

    def _take_partial_position(self, status: DemoBotStatus, *, size_fraction: float, reason: str) -> None:
        if status.open_position is None:
            return
        instrument = self._instrument_info(status.symbol)
        raw_qty = status.open_position.size * max(0.1, min(size_fraction, 0.9))
        partial_qty = self._quantize_down(raw_qty, instrument["qty_step"])
        if partial_qty < instrument["min_qty"] or partial_qty >= status.open_position.size:
            return
        self._client().place_order(
            category=self.cfg.category,
            symbol=status.symbol,
            side="Sell" if status.open_position.side == Direction.LONG else "Buy",
            qty=self._decimal_str(partial_qty),
            order_link_id=uuid4().hex[:24],
            reduce_only=True,
        )
        status.last_action = reason
        status.last_updated_at = int(time.time() * 1000)
        self._set_status(status)

    def _adjust_position_stop(self, status: DemoBotStatus, decision: AITradeDecision) -> None:
        if status.open_position is None:
            return
        position = status.open_position
        instrument = self._instrument_info(status.symbol)
        reference_stop = position.stop_loss or self._safe_float((self._active_entry_context or {}).get("planned_stop_loss"))
        if reference_stop <= 0.0:
            return
        risk_distance = abs(position.entry_price - reference_stop)
        if risk_distance <= 0.0:
            return

        adjustment = max(0.01, abs(decision.stop_adjustment_pct or 0.0))
        if position.side == Direction.LONG:
            tightened_stop = min(position.mark_price - instrument["tick_size"], reference_stop + (risk_distance * adjustment))
        else:
            tightened_stop = max(position.mark_price + instrument["tick_size"], reference_stop - (risk_distance * adjustment))
        tightened_stop = self._quantize_down(tightened_stop, instrument["tick_size"])
        if tightened_stop <= 0.0:
            return

        trailing_stop = None
        active_price = None
        if decision.exit_action == "TRAIL_STOP":
            trailing_distance = self._quantize_down(risk_distance * max(adjustment, 0.05), instrument["tick_size"])
            if trailing_distance > 0.0:
                trailing_stop = self._decimal_str(trailing_distance)
                active_price = self._decimal_str(self._quantize_down(position.mark_price, instrument["tick_size"]))

        self._client().set_trading_stop(
            category=self.cfg.category,
            symbol=status.symbol,
            stop_loss=self._decimal_str(tightened_stop),
            trailing_stop=trailing_stop,
            active_price=active_price,
        )
        status.last_action = "ai_stop_updated"
        status.last_updated_at = int(time.time() * 1000)
        self._set_status(status)

    def _ensure_leverage(self, symbol: str, leverage: float) -> None:
        try:
            self._client().set_leverage(symbol, leverage, category=self.cfg.category)
        except RuntimeError as exc:
            message = str(exc).lower()
            if "not modified" not in message and "same to the old leverage" not in message:
                raise

    def _instrument_info(self, symbol: str) -> dict[str, float]:
        cached = self._instrument_cache.get(symbol.upper())
        if cached is not None:
            return cached

        raw = self._client().get_instrument_info(symbol, category=self.cfg.category)
        lot_size = raw.get("lotSizeFilter", {}) if isinstance(raw, dict) else {}
        price_filter = raw.get("priceFilter", {}) if isinstance(raw, dict) else {}
        payload = {
            "qty_step": self._safe_float(lot_size.get("qtyStep")) or 0.001,
            "min_qty": self._safe_float(lot_size.get("minOrderQty")) or 0.001,
            "tick_size": self._safe_float(price_filter.get("tickSize")) or 0.1,
        }
        self._instrument_cache[symbol.upper()] = payload
        return payload

    def _profile(self, mode: str) -> _EntryProfile:
        normalized = str(mode or "BALANCED").upper()
        if normalized == "CONSERVATIVE":
            return _EntryProfile(
                mode=normalized,
                min_signal_confidence=0.74,
                min_cumulative_confidence=0.26,
                min_agreement_ratio=0.66,
                allowed_qualities={"HIGH", "ELEVATED"},
                countertrend_confidence=0.82,
                allow_flat_cumulative=False,
            )
        if normalized == "SCALPING":
            return _EntryProfile(
                mode=normalized,
                min_signal_confidence=0.54,
                min_cumulative_confidence=0.14,
                min_agreement_ratio=0.22,
                allowed_qualities={"HIGH", "ELEVATED", "STANDARD"},
                countertrend_confidence=0.72,
                allow_flat_cumulative=True,
            )
        if normalized == "AGGRESSIVE":
            return _EntryProfile(
                mode=normalized,
                min_signal_confidence=0.60,
                min_cumulative_confidence=0.18,
                min_agreement_ratio=0.34,
                allowed_qualities={"HIGH", "ELEVATED", "STANDARD"},
                countertrend_confidence=0.76,
                allow_flat_cumulative=True,
            )
        return _EntryProfile(
            mode="BALANCED",
            min_signal_confidence=0.66,
            min_cumulative_confidence=0.22,
            min_agreement_ratio=0.34,
            allowed_qualities={"HIGH", "ELEVATED"},
            countertrend_confidence=0.79,
            allow_flat_cumulative=False,
        )

    @staticmethod
    def _strategy_mode(behavior: dict[str, Any], cumulative: Any | None = None) -> str:
        cumulative_mode = str(getattr(cumulative, "market_mode", "") or "").upper()
        if cumulative_mode in {"TRENDING", "RANGING"}:
            return cumulative_mode
        mode = str(behavior.get("mode", "UNKNOWN")).upper()
        return "TRENDING" if mode == "TRENDING" else "RANGING"

    def _exit_plan(self, strategy_mode: str) -> _ExitPlan:
        if strategy_mode == "TRENDING":
            return _ExitPlan(
                strategy_mode="TRENDING",
                tp_multipliers=(1.0, 1.8, 2.8),
                tp_allocations=(0.30, 0.30, 0.40),
                trailing_activation_r=1.2,
                trailing_distance_r=0.6,
            )
        return _ExitPlan(
            strategy_mode="RANGING",
            tp_multipliers=(0.65, 1.15),
            tp_allocations=(0.50, 0.50),
            trailing_activation_r=None,
            trailing_distance_r=None,
        )

    def _arm_exit_strategy(self, status: DemoBotStatus, position: DemoBotPosition) -> None:
        context = self._active_entry_context or {}
        if context.get("exit_strategy_armed"):
            return

        instrument = self._instrument_info(status.symbol)
        stop_loss = self._safe_float(context.get("planned_stop_loss")) or position.stop_loss or 0.0
        if stop_loss <= 0.0:
            return

        self._client().cancel_all_orders(status.symbol, category=self.cfg.category)
        strategy_mode = str(context.get("strategy_mode") or "RANGING").upper()
        plan = self._exit_plan(strategy_mode)
        entry_price = position.entry_price
        stop_distance = abs(entry_price - stop_loss)
        if stop_distance <= 0.0:
            return

        base_stop = self._quantize_down(stop_loss, instrument["tick_size"])
        trailing_distance = None
        active_price = None
        if plan.trailing_distance_r is not None and plan.trailing_activation_r is not None:
            trailing_distance = self._quantize_down(stop_distance * plan.trailing_distance_r, instrument["tick_size"])
            trigger_price = entry_price + (stop_distance * plan.trailing_activation_r) * (1 if position.side == Direction.LONG else -1)
            active_price = self._quantize_down(trigger_price, instrument["tick_size"])

        self._client().set_trading_stop(
            category=self.cfg.category,
            symbol=status.symbol,
            stop_loss=self._decimal_str(base_stop),
            trailing_stop=self._decimal_str(trailing_distance) if trailing_distance else None,
            active_price=self._decimal_str(active_price) if active_price else None,
        )

        remaining_qty = position.size
        ladders: list[tuple[float, float]] = []
        for idx, multiplier in enumerate(plan.tp_multipliers):
            if idx == len(plan.tp_multipliers) - 1:
                ladder_qty = remaining_qty
            else:
                ladder_qty = self._quantize_down(position.size * plan.tp_allocations[idx], instrument["qty_step"])
            if ladder_qty < instrument["min_qty"]:
                continue
            remaining_qty = max(0.0, remaining_qty - ladder_qty)
            target_price = entry_price + (stop_distance * multiplier) * (1 if position.side == Direction.LONG else -1)
            target_price = self._quantize_down(target_price, instrument["tick_size"])
            ladders.append((target_price, ladder_qty))

        for idx, (target_price, ladder_qty) in enumerate(ladders, start=1):
            self._client().place_order(
                category=self.cfg.category,
                symbol=status.symbol,
                side="Sell" if position.side == Direction.LONG else "Buy",
                qty=self._decimal_str(ladder_qty),
                price=self._decimal_str(target_price),
                order_type="Limit",
                time_in_force="GTC",
                order_link_id=f"{context.get('order_link_id', uuid4().hex[:20])}-tp{idx}"[:36],
                reduce_only=True,
            )

        context["exit_strategy_armed"] = True
        context["strategy_mode"] = plan.strategy_mode
        self._active_entry_context = context
        status.last_action = f"managing_{plan.strategy_mode.lower()}_ladder"
        status.last_updated_at = int(time.time() * 1000)
        self._set_status(status)

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            if value in (None, ""):
                return 0.0
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _estimate_liquidation_price(side: Direction, entry_price: float, leverage: float) -> float | None:
        if entry_price <= 0.0 or leverage < 1.0:
            return None
        maintenance_margin_rate = 0.005
        if side == Direction.LONG:
            return entry_price * max(0.0, 1.0 - (1.0 / leverage) + maintenance_margin_rate)
        if side == Direction.SHORT:
            return entry_price * (1.0 + (1.0 / leverage) - maintenance_margin_rate)
        return None

    @staticmethod
    def _liquidation_buffer_pct(reference_price: float, liquidation_price: float | None) -> float:
        if reference_price <= 0.0 or liquidation_price is None or liquidation_price <= 0.0:
            return 0.0
        return abs(reference_price - liquidation_price) / reference_price * 100.0

    def _max_leverage_for_target_liquidation_move(self) -> float:
        maintenance_margin_rate = 0.005
        target_move_fraction = max(0.0, min(self.cfg.target_liquidation_move_pct / 100.0, 0.98))
        denominator = max(target_move_fraction + maintenance_margin_rate, 1e-6)
        max_leverage = 1.0 / denominator
        return max(1.0, min(max_leverage, 25.0))

    @staticmethod
    def _closed_trade_id(row: dict[str, Any]) -> str:
        for key in ("orderId", "execId", "updatedTime"):
            value = row.get(key)
            if value not in (None, ""):
                return str(value)
        return repr(sorted(row.items()))

    @staticmethod
    def _quantize_down(value: float, step: float) -> float:
        if value <= 0 or step <= 0:
            return 0.0
        value_dec = Decimal(str(value))
        step_dec = Decimal(str(step))
        return float((value_dec / step_dec).to_integral_value(rounding=ROUND_DOWN) * step_dec)

    @staticmethod
    def _decimal_str(value: float) -> str:
        text = f"{value:.8f}"
        return text.rstrip("0").rstrip(".") or "0"
