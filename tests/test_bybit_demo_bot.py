from __future__ import annotations

import json
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx

from liquidity_signal.config import AISupervisorConfig, BybitDemoConfig
from liquidity_signal.data.bybit_client import BybitDemoTradingClient
from liquidity_signal.ai.openai_supervisor import OpenAISupervisor
from liquidity_signal.models import AISupervisorConfigStatus, AITradeDecision, DemoBotStatus, Direction, SignalResult
from liquidity_signal.service.bybit_demo_bot import BybitDemoBot


class _FakeBybitDemoClient:
    def __init__(self) -> None:
        self.orders: list[dict[str, str]] = []
        self.stop_updates: list[dict[str, str]] = []
        self.closed_pnl_rows: list[dict[str, str]] = []
        self.position_rows: list[dict[str, str]] = []
        self.leverage_updates: list[tuple[str, float, str]] = []

    def close(self) -> None:
        return None

    def get_wallet_balance(self, *, account_type: str = "UNIFIED", coin: str = "USDT") -> dict[str, str]:
        return {
            "totalEquity": "1000",
            "walletBalance": "1000",
            "availableToWithdraw": "1000",
            "totalWalletBalance": "1000",
        }

    def get_positions(self, symbol: str, category: str = "linear") -> list[dict[str, str]]:
        return self.position_rows

    def get_closed_pnl(
        self,
        symbol: str,
        *,
        category: str = "linear",
        limit: int = 50,
        start_time: int | None = None,
    ) -> list[dict[str, str]]:
        return self.closed_pnl_rows[:limit]

    def set_leverage(self, symbol: str, leverage: float, category: str = "linear") -> dict[str, str]:
        self.leverage_updates.append((symbol, leverage, category))
        return {}

    def cancel_all_orders(self, symbol: str, category: str = "linear") -> dict[str, str]:
        return {}

    def set_trading_stop(self, **kwargs: str) -> dict[str, str]:
        self.stop_updates.append(kwargs)
        return {}

    def get_instrument_info(self, symbol: str, category: str = "linear") -> dict[str, dict[str, str]]:
        return {
            "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"},
            "priceFilter": {"tickSize": "0.1"},
        }

    def get_ticker(self, symbol: str, category: str = "linear") -> dict[str, str]:
        return {"markPrice": "100"}

    def place_order(self, **kwargs: str) -> dict[str, str]:
        self.orders.append(kwargs)
        return {"orderId": "demo-order"}


class _FakeSignalEngine:
    def __init__(self) -> None:
        self.capture_calls: list[dict[str, object]] = []

    def get_market_behavior(self, symbol: str) -> dict[str, str | float]:
        return {"bias": "BULLISH", "bias_strength": 0.42, "mode": "TRENDING"}

    def _local_time_string(self, timestamp_ms: int) -> str:
        return "2026-04-12 00:00:00 UTC"

    def capture_training_snapshot(self, **kwargs: object) -> str:
        self.capture_calls.append(kwargs)
        return "snapshot-1"


class _FakeSupervisor:
    def __init__(
        self,
        *,
        active: bool = True,
        configured: bool = True,
        model: str = "gpt-4o-mini",
        entry_decision: AITradeDecision | None = None,
        position_decision: AITradeDecision | None = None,
    ) -> None:
        self.active = active
        self.configured = configured
        self.model = model
        self.entry_decision = entry_decision
        self.position_decision = position_decision

    def close(self) -> None:
        return None

    def is_active(self) -> bool:
        return self.active

    def get_config_status(self) -> AISupervisorConfigStatus:
        return AISupervisorConfigStatus(
            enabled=self.active,
            configured=self.configured,
            active=self.active and self.configured,
            provider="OPENAI",
            model=self.model,
            config_path="runtime/ai_supervisor_config.json",
            config_source="defaults",
            message="fake supervisor",
        )

    def review_entry(self, snapshot: dict[str, object]) -> AITradeDecision | None:
        return self.entry_decision

    def review_position(self, snapshot: dict[str, object]) -> AITradeDecision | None:
        return self.position_decision


def _breakout_timeframe(
    *,
    direction: Direction = Direction.LONG,
    close: float = 100.0,
    confidence: float = 0.82,
    breakout_failure_rate: float = 0.12,
    volume_ratio: float = 1.35,
    adx14: float = 24.0,
    pattern: str | None = None,
    atr_state: str = "EXPANDING",
) -> SimpleNamespace:
    if direction == Direction.LONG:
        pattern_name = pattern or "Trend continuation up"
        ema20 = close - 0.6
        ema50 = close - 1.0
        vwap = close - 0.4
    else:
        pattern_name = pattern or "Trend continuation down"
        ema20 = close + 0.6
        ema50 = close + 1.0
        vwap = close + 0.4
    return SimpleNamespace(
        timeframe="5m",
        market_mode="TRENDING",
        direction=direction,
        confidence=confidence,
        signal_quality="HIGH",
        close=close,
        ema20=ema20,
        ema50=ema50,
        vwap=vwap,
        adx14=adx14,
        atr_state=atr_state,
        breakout_failure_rate=breakout_failure_rate,
        volume_ratio=volume_ratio,
        pattern=pattern_name,
    )


def test_bybit_demo_client_signing() -> None:
    client = BybitDemoTradingClient(api_key="key123", api_secret="secret456")

    signature = client._sign(1700000000000, "category=linear&symbol=BTCUSDT")

    assert signature == "0b6ee606ccd7a4dd483092f9b22b6371157aeeb6a326d78b2b9d39dd65844835"


def test_bybit_demo_get_signature_preserves_query_order() -> None:
    params = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "limit": 20,
        "startTime": 1775950903461,
    }

    payload = BybitDemoTradingClient._build_get_payload(params)

    assert payload == "category=linear&symbol=BTCUSDT&limit=20&startTime=1775950903461"


def test_bybit_demo_post_signature_preserves_body_order() -> None:
    payload = BybitDemoTradingClient._build_post_payload(
        {
            "category": "linear",
            "symbol": "BTCUSDT",
            "buyLeverage": "2",
            "sellLeverage": "2",
        }
    )

    assert payload == '{"category":"linear","symbol":"BTCUSDT","buyLeverage":"2","sellLeverage":"2"}'


def test_bybit_demo_config_loads_from_local_file(tmp_path) -> None:
    config_path = tmp_path / "bybit_demo_config.json"
    config_path.write_text(
        json.dumps(
            {
                "api_key": "file-key",
                "api_secret": "file-secret",
                "default_symbol": "ETHUSDT",
                "default_leverage": 3,
            }
        ),
        encoding="utf-8",
    )

    cfg = BybitDemoConfig.from_sources(str(config_path))

    assert cfg.api_key == "file-key"
    assert cfg.api_secret == "file-secret"
    assert cfg.default_symbol == "ETHUSDT"
    assert cfg.default_leverage == 3
    assert cfg.config_source == "file"
    assert cfg.config_path == str(config_path)


def test_ai_supervisor_config_loads_from_local_file(tmp_path) -> None:
    config_path = tmp_path / "ai_supervisor_config.json"
    config_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "api_key": "openai-key",
                "model": "gpt-4o-mini",
            }
        ),
        encoding="utf-8",
    )

    cfg = AISupervisorConfig.from_sources(str(config_path))

    assert cfg.enabled is True
    assert cfg.api_key == "openai-key"
    assert cfg.model == "gpt-4o-mini"
    assert cfg.active is True
    assert cfg.config_source == "file"


def test_demo_bot_submits_entry_order_for_strong_aligned_setup() -> None:
    fake_client = _FakeBybitDemoClient()
    bot = BybitDemoBot(
        _FakeSignalEngine(),
        cfg=BybitDemoConfig(api_key="demo-key", api_secret="demo-secret"),
        client=fake_client,
        supervisor=_FakeSupervisor(active=False, configured=False),
    )
    bot._set_status(
        DemoBotStatus(
            session_id=uuid4().hex,
            symbol="BTCUSDT",
            running=True,
            poll_interval_seconds=30,
            mode="BALANCED",
            leverage=2.0,
            risk_per_trade_pct=0.01,
            max_margin_fraction=0.35,
            cooldown_seconds=180,
            signal_source=bot.SIGNAL_SOURCE,
        )
    )
    bot._build_live_signal_context = lambda _symbol: (
        SimpleNamespace(scoring=SimpleNamespace(conflict_penalty=0.0)),
        SimpleNamespace(
            aggregate_direction=Direction.LONG,
            agreement_ratio=0.67,
            market_mode="TRENDING",
            signal_quality="HIGH",
            reasons=["Market mode trending with trend follow bias", "5m trending LONG 0.86"],
            timeframes=[_breakout_timeframe(direction=Direction.LONG)],
            bot_signal=SignalResult(
                symbol="BTCUSDT",
                direction=Direction.LONG,
                confidence=0.86,
                current_price=100.0,
                tp=110.0,
                sl=98.0,
                reasons=["aligned_trend", "liquidation_support"],
                signal_quality="HIGH",
                horizon="MTF 5m/15m/1h",
            ),
        ),
        SimpleNamespace(
            dominant_pull=Direction.LONG,
            confidence=0.72,
            clusters_above=[SimpleNamespace(center_price=102.0, total_strength=1.0)],
            clusters_below=[],
        ),
        SignalResult(
            symbol="BTCUSDT",
            direction=Direction.LONG,
            confidence=0.86,
            current_price=100.0,
            tp=110.0,
            sl=98.0,
            reasons=["no_edge", "aligned_trend", "liquidation_support"],
            signal_quality="HIGH",
        ),
    )
    bot._run_cycle()

    assert len(fake_client.orders) == 1
    order = fake_client.orders[0]
    assert order["symbol"] == "BTCUSDT"
    assert order["side"] == "Buy"
    assert order["qty"] == "3.867"
    assert order["stop_loss"] == "98"


def test_running_status_is_returned_even_for_different_requested_symbol() -> None:
    bot = BybitDemoBot(
        _FakeSignalEngine(),
        cfg=BybitDemoConfig(api_key="demo-key", api_secret="demo-secret"),
        client=_FakeBybitDemoClient(),
        supervisor=_FakeSupervisor(active=False, configured=False),
    )
    bot._set_status(
        DemoBotStatus(
            session_id=uuid4().hex,
            symbol="BTCUSDT",
            running=True,
            poll_interval_seconds=30,
            mode="BALANCED",
            leverage=2.0,
            risk_per_trade_pct=0.01,
            max_margin_fraction=0.35,
            cooldown_seconds=180,
            signal_source=bot.SIGNAL_SOURCE,
            last_action="waiting_for_setup",
        )
    )

    status = bot.get_status("ETHUSDT")

    assert status.running is True
    assert status.symbol == "BTCUSDT"


def test_demo_bot_ai_can_block_entry() -> None:
    fake_client = _FakeBybitDemoClient()
    bot = BybitDemoBot(
        _FakeSignalEngine(),
        cfg=BybitDemoConfig(api_key="demo-key", api_secret="demo-secret"),
        client=fake_client,
        supervisor=_FakeSupervisor(
            entry_decision=AITradeDecision(
                enabled=True,
                review_type="entry",
                entry_verdict="BLOCK",
                exit_action="HOLD",
                reason="AI sees weak follow-through",
            )
        ),
    )
    bot._set_status(
        DemoBotStatus(
            session_id=uuid4().hex,
            symbol="BTCUSDT",
            running=True,
            poll_interval_seconds=30,
            mode="BALANCED",
            leverage=2.0,
            risk_per_trade_pct=0.01,
            max_margin_fraction=0.35,
            cooldown_seconds=180,
            signal_source=bot.SIGNAL_SOURCE,
        )
    )
    bot._build_live_signal_context = lambda _symbol: (
        SimpleNamespace(scoring=SimpleNamespace(conflict_penalty=0.0)),
        SimpleNamespace(
            aggregate_direction=Direction.LONG,
            agreement_ratio=0.67,
            market_mode="TRENDING",
            signal_quality="HIGH",
            reasons=["Market mode trending with trend follow bias", "5m trending LONG 0.86"],
            timeframes=[_breakout_timeframe(direction=Direction.LONG)],
            bot_signal=SignalResult(
                symbol="BTCUSDT",
                direction=Direction.LONG,
                confidence=0.86,
                current_price=100.0,
                tp=110.0,
                sl=98.0,
                reasons=["aligned_trend", "liquidation_support"],
                signal_quality="HIGH",
                horizon="MTF 5m/15m/1h",
            ),
        ),
        SimpleNamespace(
            dominant_pull=Direction.LONG,
            confidence=0.72,
            clusters_above=[SimpleNamespace(center_price=102.0, total_strength=1.0)],
            clusters_below=[],
        ),
        SignalResult(
            symbol="BTCUSDT",
            direction=Direction.LONG,
            confidence=0.86,
            current_price=100.0,
            tp=110.0,
            sl=98.0,
            reasons=["no_edge", "aligned_trend", "liquidation_support"],
            signal_quality="HIGH",
        ),
    )

    bot._run_cycle()

    status = bot.get_status("BTCUSDT")
    assert fake_client.orders == []
    assert status.last_action == "ai_blocked_entry"
    assert status.ai_last_decision is not None
    assert status.ai_last_decision.reason == "AI sees weak follow-through"


def test_demo_bot_ai_can_reduce_entry_size() -> None:
    fake_client = _FakeBybitDemoClient()
    bot = BybitDemoBot(
        _FakeSignalEngine(),
        cfg=BybitDemoConfig(api_key="demo-key", api_secret="demo-secret"),
        client=fake_client,
        supervisor=_FakeSupervisor(
            entry_decision=AITradeDecision(
                enabled=True,
                review_type="entry",
                entry_verdict="ALLOW_REDUCED",
                size_multiplier=0.5,
                exit_action="HOLD",
                reason="AI wants half size",
            )
        ),
    )
    bot._set_status(
        DemoBotStatus(
            session_id=uuid4().hex,
            symbol="BTCUSDT",
            running=True,
            poll_interval_seconds=30,
            mode="BALANCED",
            leverage=2.0,
            risk_per_trade_pct=0.01,
            max_margin_fraction=0.35,
            cooldown_seconds=180,
            signal_source=bot.SIGNAL_SOURCE,
        )
    )
    bot._build_live_signal_context = lambda _symbol: (
        SimpleNamespace(scoring=SimpleNamespace(conflict_penalty=0.0)),
        SimpleNamespace(
            aggregate_direction=Direction.LONG,
            agreement_ratio=0.67,
            market_mode="TRENDING",
            signal_quality="HIGH",
            reasons=["Market mode trending with trend follow bias", "5m trending LONG 0.86"],
            timeframes=[_breakout_timeframe(direction=Direction.LONG)],
            bot_signal=SignalResult(
                symbol="BTCUSDT",
                direction=Direction.LONG,
                confidence=0.86,
                current_price=100.0,
                tp=110.0,
                sl=98.0,
                reasons=["aligned_trend", "liquidation_support"],
                signal_quality="HIGH",
                horizon="MTF 5m/15m/1h",
            ),
        ),
        SimpleNamespace(
            dominant_pull=Direction.LONG,
            confidence=0.72,
            clusters_above=[SimpleNamespace(center_price=102.0, total_strength=1.0)],
            clusters_below=[],
        ),
        SignalResult(
            symbol="BTCUSDT",
            direction=Direction.LONG,
            confidence=0.86,
            current_price=100.0,
            tp=110.0,
            sl=98.0,
            reasons=["no_edge", "aligned_trend", "liquidation_support"],
            signal_quality="HIGH",
        ),
    )

    bot._run_cycle()

    assert len(fake_client.orders) == 1
    assert fake_client.orders[0]["qty"] == "1.933"


def test_demo_bot_caps_entry_leverage_to_keep_liquidation_very_far() -> None:
    fake_client = _FakeBybitDemoClient()
    bot = BybitDemoBot(
        _FakeSignalEngine(),
        cfg=BybitDemoConfig(
            api_key="demo-key",
            api_secret="demo-secret",
            target_liquidation_move_pct=90.0,
        ),
        client=fake_client,
        supervisor=_FakeSupervisor(active=False, configured=False),
    )
    bot._set_status(
        DemoBotStatus(
            session_id=uuid4().hex,
            symbol="BTCUSDT",
            running=True,
            poll_interval_seconds=5,
            mode="BALANCED",
            leverage=5.0,
            risk_per_trade_pct=0.01,
            max_margin_fraction=0.35,
            cooldown_seconds=30,
            signal_source=bot.SIGNAL_SOURCE,
            account_equity=1000.0,
            wallet_balance=1000.0,
            available_balance=1000.0,
        )
    )
    bot._build_live_signal_context = lambda _symbol: (
        SimpleNamespace(scoring=SimpleNamespace(conflict_penalty=0.0)),
        SimpleNamespace(
            aggregate_direction=Direction.LONG,
            agreement_ratio=0.67,
            market_mode="TRENDING",
            signal_quality="HIGH",
            reasons=["confirmed breakout"],
            timeframes=[_breakout_timeframe(direction=Direction.LONG)],
            bot_signal=SignalResult(
                symbol="BTCUSDT",
                direction=Direction.LONG,
                confidence=0.86,
                current_price=100.0,
                tp=110.0,
                sl=98.0,
                reasons=["aligned_trend", "liquidation_support"],
                signal_quality="HIGH",
                horizon="MTF 5m/15m/1h",
            ),
        ),
        SimpleNamespace(
            dominant_pull=Direction.LONG,
            confidence=0.72,
            clusters_above=[SimpleNamespace(center_price=102.0, total_strength=1.0)],
            clusters_below=[],
        ),
        SignalResult(
            symbol="BTCUSDT",
            direction=Direction.LONG,
            confidence=0.86,
            current_price=100.0,
            tp=110.0,
            sl=98.0,
            reasons=["aligned_trend", "liquidation_support"],
            signal_quality="HIGH",
            horizon="MTF 5m/15m/1h",
        ),
    )

    bot._run_cycle()

    assert fake_client.orders
    assert fake_client.leverage_updates
    _, applied_leverage, _ = fake_client.leverage_updates[-1]
    assert applied_leverage < 1.11
    assert applied_leverage >= 1.0


def test_scalping_mode_allows_standard_quality_trend_continuation() -> None:
    fake_client = _FakeBybitDemoClient()
    bot = BybitDemoBot(
        _FakeSignalEngine(),
        cfg=BybitDemoConfig(api_key="demo-key", api_secret="demo-secret"),
        client=fake_client,
        supervisor=_FakeSupervisor(active=False, configured=False),
    )
    bot._set_status(
        DemoBotStatus(
            session_id=uuid4().hex,
            symbol="BTCUSDT",
            running=True,
            poll_interval_seconds=5,
            mode="SCALPING",
            leverage=2.0,
            risk_per_trade_pct=0.01,
            max_margin_fraction=0.35,
            cooldown_seconds=30,
            signal_source=bot.SIGNAL_SOURCE,
            account_equity=1000.0,
            wallet_balance=1000.0,
            available_balance=1000.0,
        )
    )
    bot._build_live_signal_context = lambda _symbol: (
        SimpleNamespace(scoring=SimpleNamespace(conflict_penalty=0.05)),
        SimpleNamespace(
            aggregate_direction=Direction.LONG,
            agreement_ratio=0.26,
            market_mode="TRENDING",
            signal_quality="STANDARD",
            reasons=["trend continuation scalp"],
            timeframes=[_breakout_timeframe(direction=Direction.LONG, confidence=0.58, volume_ratio=1.02, breakout_failure_rate=0.40, pattern="Trend continuation up")],
            bot_signal=SignalResult(
                symbol="BTCUSDT",
                direction=Direction.LONG,
                confidence=0.58,
                current_price=100.0,
                tp=103.0,
                sl=98.0,
                reasons=["trend_continuation_scalp"],
                signal_quality="STANDARD",
                horizon="MTF 5m/15m/1h",
            ),
        ),
        SimpleNamespace(
            dominant_pull=Direction.LONG,
            confidence=0.60,
            clusters_above=[SimpleNamespace(center_price=101.4, total_strength=0.8)],
            clusters_below=[],
        ),
        SignalResult(
            symbol="BTCUSDT",
            direction=Direction.LONG,
            confidence=0.58,
            current_price=100.0,
            tp=103.0,
            sl=98.0,
            reasons=["trend_continuation_scalp"],
            signal_quality="STANDARD",
            horizon="MTF 5m/15m/1h",
        ),
    )

    bot._run_cycle()

    assert len(fake_client.orders) == 1
    assert fake_client.orders[0]["side"] == "Buy"


def test_demo_bot_allows_strong_trend_setup_through_moderate_opposing_liquidity() -> None:
    fake_client = _FakeBybitDemoClient()
    bot = BybitDemoBot(
        _FakeSignalEngine(),
        cfg=BybitDemoConfig(api_key="demo-key", api_secret="demo-secret"),
        client=fake_client,
        supervisor=_FakeSupervisor(active=False, configured=False),
    )
    bot._set_status(
        DemoBotStatus(
            session_id=uuid4().hex,
            symbol="BTCUSDT",
            running=True,
            poll_interval_seconds=30,
            mode="BALANCED",
            leverage=2.0,
            risk_per_trade_pct=0.01,
            max_margin_fraction=0.35,
            cooldown_seconds=180,
            signal_source=bot.SIGNAL_SOURCE,
        )
    )
    bot._build_live_signal_context = lambda _symbol: (
        SimpleNamespace(scoring=SimpleNamespace(conflict_penalty=0.05)),
        SimpleNamespace(
            aggregate_direction=Direction.LONG,
            agreement_ratio=0.67,
            aggregate_confidence=0.74,
            market_mode="TRENDING",
            signal_quality="ELEVATED",
            reasons=["Market mode trending with trend follow bias", "1h trending LONG 0.81"],
            timeframes=[_breakout_timeframe(direction=Direction.LONG, confidence=0.74, breakout_failure_rate=0.22, volume_ratio=1.22)],
            bot_signal=SignalResult(
                symbol="BTCUSDT",
                direction=Direction.LONG,
                confidence=0.74,
                current_price=100.0,
                tp=110.0,
                sl=98.0,
                reasons=["trend_follow"],
                signal_quality="ELEVATED",
                horizon="MTF 5m/15m/1h",
            ),
        ),
        SimpleNamespace(
            dominant_pull=Direction.SHORT,
            confidence=0.60,
            clusters_above=[SimpleNamespace(center_price=101.0, total_strength=0.5)],
            clusters_below=[SimpleNamespace(center_price=99.0, total_strength=0.6)],
        ),
        SignalResult(
            symbol="BTCUSDT",
            direction=Direction.LONG,
            confidence=0.70,
            current_price=100.0,
            tp=110.0,
            sl=98.0,
            reasons=["trend_follow", "Liquidity magnet leans against setup (-0.03)"],
            signal_quality="ELEVATED",
            horizon="MTF 5m/15m/1h",
        ),
    )

    bot._run_cycle()

    assert len(fake_client.orders) == 1
    assert fake_client.orders[0]["side"] == "Buy"


def test_demo_bot_requires_confirmed_breakout_before_entry() -> None:
    fake_client = _FakeBybitDemoClient()
    bot = BybitDemoBot(
        _FakeSignalEngine(),
        cfg=BybitDemoConfig(api_key="demo-key", api_secret="demo-secret"),
        client=fake_client,
        supervisor=_FakeSupervisor(active=False, configured=False),
    )
    bot._set_status(
        DemoBotStatus(
            session_id=uuid4().hex,
            symbol="BTCUSDT",
            running=True,
            poll_interval_seconds=5,
            mode="BALANCED",
            leverage=2.0,
            risk_per_trade_pct=0.01,
            max_margin_fraction=0.35,
            cooldown_seconds=30,
            signal_source=bot.SIGNAL_SOURCE,
        )
    )
    bot._build_live_signal_context = lambda _symbol: (
        SimpleNamespace(scoring=SimpleNamespace(conflict_penalty=0.0)),
        SimpleNamespace(
            aggregate_direction=Direction.LONG,
            agreement_ratio=0.68,
            market_mode="TRENDING",
            signal_quality="HIGH",
            reasons=["trend setup but no breakout confirmation"],
            timeframes=[_breakout_timeframe(direction=Direction.LONG, volume_ratio=0.95, breakout_failure_rate=0.48, adx14=12.0, atr_state="COMPRESSING", pattern="Mixed Structure")],
            bot_signal=SignalResult(
                symbol="BTCUSDT",
                direction=Direction.LONG,
                confidence=0.82,
                current_price=100.0,
                tp=110.0,
                sl=98.0,
                reasons=["aligned_trend"],
                signal_quality="HIGH",
                horizon="MTF 5m/15m/1h",
            ),
        ),
        SimpleNamespace(
            dominant_pull=Direction.LONG,
            confidence=0.70,
            clusters_above=[SimpleNamespace(center_price=102.0, total_strength=1.0)],
            clusters_below=[],
        ),
        SignalResult(
            symbol="BTCUSDT",
            direction=Direction.LONG,
            confidence=0.82,
            current_price=100.0,
            tp=110.0,
            sl=98.0,
            reasons=["aligned_trend"],
            signal_quality="HIGH",
            horizon="MTF 5m/15m/1h",
        ),
    )

    bot._run_cycle()

    status = bot.get_status("BTCUSDT")
    assert fake_client.orders == []
    assert status.last_action == "waiting_for_setup"


def test_demo_bot_kills_trade_after_90pct_adverse_move() -> None:
    fake_client = _FakeBybitDemoClient()
    fake_client.position_rows = [
        {
            "side": "Buy",
            "size": "1.0",
            "avgPrice": "100",
            "markPrice": "98.1",
            "positionValue": "100",
            "unrealisedPnl": "-1.9",
            "leverage": "2",
            "takeProfit": "104",
            "stopLoss": "98",
        }
    ]
    bot = BybitDemoBot(
        _FakeSignalEngine(),
        cfg=BybitDemoConfig(
            api_key="demo-key",
            api_secret="demo-secret",
            opposite_move_close_r=0.90,
        ),
        client=fake_client,
        supervisor=_FakeSupervisor(active=False, configured=False),
    )
    bot._active_entry_context = {
        "entry_time": int(time.time() * 1000) - 15_000,
        "planned_stop_loss": "98",
        "strategy_mode": "TRENDING",
        "breakeven_armed": False,
        "partial_taken": False,
        "profit_lock_armed": False,
        "pending_exit_reason": "",
    }
    bot._set_status(
        DemoBotStatus(
            session_id=uuid4().hex,
            symbol="BTCUSDT",
            running=True,
            poll_interval_seconds=5,
            mode="BALANCED",
            leverage=15.0,
            risk_per_trade_pct=0.01,
            max_margin_fraction=0.35,
            cooldown_seconds=30,
            signal_source=bot.SIGNAL_SOURCE,
            account_equity=1000.0,
            wallet_balance=1000.0,
            available_balance=1000.0,
        )
    )
    bot._build_live_signal_context = lambda _symbol: (
        SimpleNamespace(scoring=SimpleNamespace(conflict_penalty=0.0)),
        SimpleNamespace(
            aggregate_direction=Direction.LONG,
            agreement_ratio=0.60,
            market_mode="TRENDING",
            signal_quality="HIGH",
            reasons=["trend still long but price is failing"],
            timeframes=[_breakout_timeframe(direction=Direction.LONG, volume_ratio=1.45, breakout_failure_rate=0.10)],
            bot_signal=SignalResult(
                symbol="BTCUSDT",
                direction=Direction.LONG,
                confidence=0.72,
                current_price=98.1,
                tp=104.0,
                sl=99.0,
                reasons=["opposite_move_risk"],
                signal_quality="HIGH",
                horizon="MTF 5m/15m/1h",
            ),
        ),
        SimpleNamespace(
            dominant_pull=Direction.SHORT,
            confidence=0.70,
            clusters_above=[SimpleNamespace(center_price=103.0, total_strength=1.0)],
            clusters_below=[],
        ),
        SignalResult(
            symbol="BTCUSDT",
            direction=Direction.LONG,
            confidence=0.72,
            current_price=98.1,
            tp=104.0,
            sl=99.0,
            reasons=["opposite_move_risk"],
            signal_quality="HIGH",
            horizon="MTF 5m/15m/1h",
        ),
    )

    bot._run_cycle()

    status = bot.get_status("BTCUSDT")
    assert fake_client.orders
    assert fake_client.orders[-1]["reduce_only"] is True
    assert status.last_action == "opposite_move_kill_switch"


def test_demo_bot_closes_when_market_flips_against_open_trade() -> None:
    fake_client = _FakeBybitDemoClient()
    fake_client.position_rows = [
        {
            "side": "Buy",
            "size": "1.0",
            "avgPrice": "100",
            "markPrice": "100.2",
            "positionValue": "100",
            "unrealisedPnl": "0.2",
            "leverage": "2",
            "takeProfit": "104",
            "stopLoss": "98",
        }
    ]
    bot = BybitDemoBot(
        _FakeSignalEngine(),
        cfg=BybitDemoConfig(api_key="demo-key", api_secret="demo-secret"),
        client=fake_client,
        supervisor=_FakeSupervisor(active=False, configured=False),
    )
    bot._active_entry_context = {
        "entry_time": int(time.time() * 1000) - 20_000,
        "planned_stop_loss": "98",
        "strategy_mode": "TRENDING",
        "breakeven_armed": False,
        "partial_taken": False,
        "profit_lock_armed": False,
        "pending_exit_reason": "",
    }
    bot._set_status(
        DemoBotStatus(
            session_id=uuid4().hex,
            symbol="BTCUSDT",
            running=True,
            poll_interval_seconds=5,
            mode="BALANCED",
            leverage=2.0,
            risk_per_trade_pct=0.01,
            max_margin_fraction=0.35,
            cooldown_seconds=30,
            signal_source=bot.SIGNAL_SOURCE,
        )
    )
    bot._build_live_signal_context = lambda _symbol: (
        SimpleNamespace(scoring=SimpleNamespace(conflict_penalty=0.02)),
        SimpleNamespace(
            aggregate_direction=Direction.SHORT,
            agreement_ratio=0.72,
            market_mode="TRENDING",
            signal_quality="HIGH",
            reasons=["market flipped lower"],
            timeframes=[_breakout_timeframe(direction=Direction.SHORT, close=100.2, confidence=0.78)],
            bot_signal=SignalResult(
                symbol="BTCUSDT",
                direction=Direction.SHORT,
                confidence=0.78,
                current_price=100.2,
                tp=97.0,
                sl=101.4,
                reasons=["flip_confirmed"],
                signal_quality="HIGH",
                horizon="MTF 5m/15m/1h",
            ),
        ),
        SimpleNamespace(
            dominant_pull=Direction.SHORT,
            confidence=0.68,
            clusters_above=[],
            clusters_below=[SimpleNamespace(center_price=99.4, total_strength=0.8)],
        ),
        SignalResult(
            symbol="BTCUSDT",
            direction=Direction.SHORT,
            confidence=0.78,
            current_price=100.2,
            tp=97.0,
            sl=101.4,
            reasons=["flip_confirmed"],
            signal_quality="HIGH",
            horizon="MTF 5m/15m/1h",
        ),
    )

    bot._run_cycle()

    status = bot.get_status("BTCUSDT")
    assert fake_client.orders
    assert fake_client.orders[-1]["reduce_only"] is True
    assert status.last_action == "signal_flip"


def test_demo_bot_captures_training_snapshot_for_each_entry_cycle() -> None:
    fake_client = _FakeBybitDemoClient()
    fake_engine = _FakeSignalEngine()
    bot = BybitDemoBot(
        fake_engine,
        cfg=BybitDemoConfig(api_key="demo-key", api_secret="demo-secret"),
        client=fake_client,
        supervisor=_FakeSupervisor(active=False, configured=False),
    )
    bot._set_status(
        DemoBotStatus(
            session_id=uuid4().hex,
            symbol="BTCUSDT",
            running=True,
            poll_interval_seconds=30,
            mode="BALANCED",
            leverage=2.0,
            risk_per_trade_pct=0.01,
            max_margin_fraction=0.35,
            cooldown_seconds=180,
            signal_source=bot.SIGNAL_SOURCE,
        )
    )
    bot._build_live_signal_context = lambda _symbol: (
        SimpleNamespace(scoring=SimpleNamespace(conflict_penalty=0.0)),
        SimpleNamespace(
            aggregate_direction=Direction.LONG,
            agreement_ratio=0.67,
            market_mode="TRENDING",
            signal_quality="HIGH",
            reasons=["Market mode trending with trend follow bias"],
            timeframes=[_breakout_timeframe(direction=Direction.LONG)],
            bot_signal=SignalResult(
                symbol="BTCUSDT",
                direction=Direction.LONG,
                confidence=0.86,
                current_price=100.0,
                tp=110.0,
                sl=98.0,
                reasons=["aligned_trend"],
                signal_quality="HIGH",
                horizon="MTF 5m/15m/1h",
            ),
            model_dump=lambda mode="json": {"aggregate_direction": "LONG"},
        ),
        SimpleNamespace(
            dominant_pull=Direction.LONG,
            confidence=0.72,
            clusters_above=[SimpleNamespace(center_price=102.0, total_strength=1.0)],
            clusters_below=[],
        ),
        SignalResult(
            symbol="BTCUSDT",
            direction=Direction.LONG,
            confidence=0.86,
            current_price=100.0,
            tp=110.0,
            sl=98.0,
            reasons=["aligned_trend", "liquidation_support"],
            signal_quality="HIGH",
            horizon="MTF 5m/15m/1h",
        ),
    )

    bot._run_cycle()

    assert fake_engine.capture_calls
    assert fake_engine.capture_calls[-1]["decision_action"] == "entry_submitted_long"


def test_demo_bot_exits_stale_position_quickly_for_scalping() -> None:
    fake_client = _FakeBybitDemoClient()
    fake_client.position_rows = [
        {
            "side": "Buy",
            "size": "1.0",
            "avgPrice": "100",
            "markPrice": "100.1",
            "positionValue": "100",
            "unrealisedPnl": "0.1",
            "leverage": "2",
            "takeProfit": "110",
            "stopLoss": "98",
        }
    ]
    bot = BybitDemoBot(
        _FakeSignalEngine(),
        cfg=BybitDemoConfig(api_key="demo-key", api_secret="demo-secret"),
        client=fake_client,
        supervisor=_FakeSupervisor(active=False, configured=False),
    )
    bot._active_entry_context = {
        "entry_time": int(time.time() * 1000) - 240_000,
        "planned_stop_loss": "98",
        "strategy_mode": "TRENDING",
        "breakeven_armed": False,
        "partial_taken": False,
        "profit_lock_armed": False,
        "pending_exit_reason": "",
    }
    bot._set_status(
        DemoBotStatus(
            session_id=uuid4().hex,
            symbol="BTCUSDT",
            running=True,
            poll_interval_seconds=5,
            mode="BALANCED",
            leverage=2.0,
            risk_per_trade_pct=0.01,
            max_margin_fraction=0.35,
            cooldown_seconds=30,
            signal_source=bot.SIGNAL_SOURCE,
        )
    )
    bot._build_live_signal_context = lambda _symbol: (
        SimpleNamespace(scoring=SimpleNamespace(conflict_penalty=0.05)),
        SimpleNamespace(
            aggregate_direction=Direction.LONG,
            agreement_ratio=0.20,
            market_mode="TRENDING",
            signal_quality="LOW",
            reasons=["market_weakening"],
            bot_signal=SignalResult(
                symbol="BTCUSDT",
                direction=Direction.LONG,
                confidence=0.50,
                current_price=100.1,
                tp=101.5,
                sl=98.0,
                reasons=["weak_follow_through"],
                signal_quality="LOW",
                horizon="MTF 5m/15m/1h",
            ),
        ),
        SimpleNamespace(
            dominant_pull=Direction.FLAT,
            confidence=0.20,
            clusters_above=[],
            clusters_below=[],
        ),
        SignalResult(
            symbol="BTCUSDT",
            direction=Direction.LONG,
            confidence=0.50,
            current_price=100.1,
            tp=101.5,
            sl=98.0,
            reasons=["weak_follow_through"],
            signal_quality="LOW",
            horizon="MTF 5m/15m/1h",
        ),
    )

    bot._run_cycle()

    status = bot.get_status("BTCUSDT")
    assert fake_client.orders
    assert fake_client.orders[-1]["reduce_only"] is True
    assert fake_client.orders[-1]["side"] == "Sell"
    assert status.last_action == "stale_no_follow_through"
    assert status.cooldown_until is not None
    assert status.cooldown_until - int(time.time() * 1000) <= 15_000


def test_demo_bot_scales_out_and_protects_fast_winner() -> None:
    fake_client = _FakeBybitDemoClient()
    fake_client.position_rows = [
        {
            "side": "Buy",
            "size": "1.0",
            "avgPrice": "100",
            "markPrice": "101.2",
            "positionValue": "100",
            "unrealisedPnl": "1.2",
            "leverage": "2",
            "takeProfit": "110",
            "stopLoss": "98",
        }
    ]
    bot = BybitDemoBot(
        _FakeSignalEngine(),
        cfg=BybitDemoConfig(api_key="demo-key", api_secret="demo-secret"),
        client=fake_client,
        supervisor=_FakeSupervisor(active=False, configured=False),
    )
    bot._active_entry_context = {
        "entry_time": int(time.time() * 1000) - 60_000,
        "planned_stop_loss": "98",
        "strategy_mode": "TRENDING",
        "breakeven_armed": True,
        "partial_taken": False,
        "profit_lock_armed": False,
        "pending_exit_reason": "",
    }
    bot._set_status(
        DemoBotStatus(
            session_id=uuid4().hex,
            symbol="BTCUSDT",
            running=True,
            poll_interval_seconds=5,
            mode="BALANCED",
            leverage=2.0,
            risk_per_trade_pct=0.01,
            max_margin_fraction=0.35,
            cooldown_seconds=30,
            signal_source=bot.SIGNAL_SOURCE,
        )
    )
    bot._build_live_signal_context = lambda _symbol: (
        SimpleNamespace(scoring=SimpleNamespace(conflict_penalty=0.03)),
        SimpleNamespace(
            aggregate_direction=Direction.LONG,
            agreement_ratio=0.70,
            market_mode="TRENDING",
            signal_quality="HIGH",
            reasons=["trend_intact"],
            bot_signal=SignalResult(
                symbol="BTCUSDT",
                direction=Direction.LONG,
                confidence=0.83,
                current_price=101.2,
                tp=103.5,
                sl=98.0,
                reasons=["trend_intact"],
                signal_quality="HIGH",
                horizon="MTF 5m/15m/1h",
            ),
        ),
        SimpleNamespace(
            dominant_pull=Direction.LONG,
            confidence=0.62,
            clusters_above=[],
            clusters_below=[],
        ),
        SignalResult(
            symbol="BTCUSDT",
            direction=Direction.LONG,
            confidence=0.83,
            current_price=101.2,
            tp=103.5,
            sl=98.0,
            reasons=["trend_intact"],
            signal_quality="HIGH",
            horizon="MTF 5m/15m/1h",
        ),
    )

    bot._run_cycle()

    status = bot.get_status("BTCUSDT")
    assert fake_client.orders
    assert fake_client.orders[-1]["reduce_only"] is True
    assert fake_client.orders[-1]["qty"] == "0.35"
    assert fake_client.stop_updates
    assert fake_client.stop_updates[-1]["stop_loss"] == "100.1"
    assert status.last_action == "scalp_partial_protected"
    assert bot._active_entry_context is not None
    assert bot._active_entry_context["partial_taken"] is True


def test_openai_supervisor_returns_skipped_decision_on_http_error() -> None:
    supervisor = OpenAISupervisor(
        cfg=AISupervisorConfig(enabled=True, api_key="key", model="gpt-4o-mini"),
        client=httpx.Client(
            base_url="https://api.openai.com/v1",
            transport=httpx.MockTransport(lambda request: httpx.Response(500, json={"error": "boom"})),
        ),
    )

    decision = supervisor.review_entry({"symbol": "BTCUSDT"})

    assert decision is not None
    assert decision.entry_verdict == "SKIPPED"
    assert "AI supervisor unavailable" in decision.reason


def test_demo_bot_performance_summarizes_historical_closed_trades() -> None:
    fake_client = _FakeBybitDemoClient()
    fake_client.closed_pnl_rows = [
        {
            "orderId": "order-1",
            "side": "Buy",
            "qty": "1.0",
            "avgEntryPrice": "100",
            "avgExitPrice": "105",
            "createdTime": "1710000000000",
            "updatedTime": "1710000300000",
            "openFee": "0.5",
            "closeFee": "0.5",
            "totalFunding": "0.1",
            "closedPnl": "4.0",
            "execType": "Trade",
        },
        {
            "orderId": "order-2",
            "side": "Sell",
            "qty": "1.0",
            "avgEntryPrice": "102",
            "avgExitPrice": "104",
            "createdTime": "1710000600000",
            "updatedTime": "1710000900000",
            "openFee": "0.4",
            "closeFee": "0.4",
            "totalFunding": "0.0",
            "closedPnl": "-2.0",
            "execType": "Trade",
        },
    ]
    bot = BybitDemoBot(
        _FakeSignalEngine(),
        cfg=BybitDemoConfig(api_key="demo-key", api_secret="demo-secret"),
        client=fake_client,
        supervisor=_FakeSupervisor(active=False, configured=False),
    )

    performance = bot.get_performance("BTCUSDT", limit=50)

    assert performance.symbol == "BTCUSDT"
    assert performance.summary.total_trades == 2
    assert performance.summary.net_pnl == 2.0
    assert performance.summary.win_rate == 0.5
    assert performance.summary.best_trade_pnl == 4.0
    assert performance.summary.worst_trade_pnl == -2.0
    assert performance.summary.profit_factor == 2.0
