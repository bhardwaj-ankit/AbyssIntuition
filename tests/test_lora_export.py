from pathlib import Path

from liquidity_signal.models import (
    CumulativeSignalResponse,
    Direction,
    LiquidityFeatures,
    RiskBreakdown,
    ScoringBreakdown,
    SignalExplainResult,
    SignalResult,
    TrainingSnapshotLabel,
    TrainingSnapshotRecord,
)
from liquidity_signal.service.engine import SignalEngine
from liquidity_signal.service.liquidation_store import LiquidationStore
from tests.test_backtest import FakeClient


def _build_explain(symbol: str, decision_ts: int) -> SignalExplainResult:
    signal = SignalResult(
        symbol=symbol,
        direction=Direction.LONG,
        confidence=0.74,
        current_price=100.0,
        tp=101.4,
        sl=99.3,
        reasons=["trend_follow", "liquidity_support"],
        horizon="MTF 5m/15m/1h",
        decision_ts=decision_ts,
        expires_at=decision_ts + (90 * 1000),
        signal_quality="HIGH",
    )
    features = LiquidityFeatures(
        symbol=symbol,
        mark_price=100.0,
        spread_bps=1.8,
        imbalance_l1=0.22,
        imbalance_l5=0.18,
        imbalance_l10=0.12,
        weighted_depth_imbalance=0.14,
        buy_flow_ratio=0.59,
        short_volatility_bps=21.0,
        micro_momentum_bps=10.0,
        intraday_momentum_bps=24.0,
        volume_zscore=1.4,
        liquidity_gap_bps=0.9,
        funding_rate_bps=0.8,
        basis_bps=1.6,
        open_interest_change_pct=1.8,
        taker_buy_sell_ratio=1.08,
        global_long_short_ratio=1.02,
        top_trader_account_ratio=1.01,
        top_trader_position_ratio=1.03,
        htf_bias=0.52,
        htf_regime="TRENDING",
    )
    scoring = ScoringBreakdown(
        spread_gate_triggered=False,
        max_spread_bps=8.0,
        imbalance_l1_weighted=0.10,
        imbalance_l5_weighted=0.06,
        imbalance_l10_weighted=0.04,
        weighted_depth_weighted=0.03,
        flow_weighted=0.04,
        microstructure_score=0.27,
        momentum_score=0.34,
        regime_score=0.48,
        positioning_score=0.16,
        sentiment_score=0.11,
        alignment_bonus=0.08,
        conflict_penalty=0.03,
        execution_penalty=0.02,
        crowding_penalty=0.01,
        raw_score=0.46,
        confidence=0.71,
        long_threshold=0.2,
        short_threshold=-0.2,
        min_confidence=0.55,
    )
    risk = RiskBreakdown(
        base_stop_bps=17.0,
        spread_adjustment_bps=1.0,
        adjusted_stop_bps=18.0,
        stop_bps=18.0,
        confidence_boost=0.05,
        tp_bps=30.0,
        risk_reward=1.66,
    )
    return SignalExplainResult(signal=signal, features=features, scoring=scoring, risk=risk)


def test_lora_export_builds_examples_from_resolved_snapshots(tmp_path: Path) -> None:
    store = LiquidationStore(db_path=tmp_path / "lora_dataset.db")
    engine = SignalEngine(client=FakeClient(), liquidation_store=store)
    decision_ts = 1_700_000_000_000
    explain = _build_explain("BTCUSDT", decision_ts)
    cumulative = CumulativeSignalResponse(
        symbol="BTCUSDT",
        generated_at=decision_ts,
        market_mode="TRENDING",
        strategy_mode="TREND_FOLLOW",
        aggregate_direction=Direction.LONG,
        aggregate_confidence=0.76,
        agreement_ratio=0.68,
        signal_quality="HIGH",
        reasons=["trend bias confirmed"],
        bot_signal=explain.signal,
        timeframes=[],
    )

    engine.capture_training_snapshot(
        symbol="BTCUSDT",
        exchange="BYBIT",
        environment="DEMO",
        session_id="session-1",
        mode="SCALPING",
        explain=explain,
        cumulative=cumulative,
        liq_map=None,
        behavior={"bias": "BULLISH", "bias_strength": 0.52, "mode": "TRENDING", "regime": "UPTREND"},
        bot_signal=explain.signal,
        open_position=None,
        ai_decision={"entry_verdict": "ALLOW", "exit_action": "HOLD", "risk_flags": []},
        decision_action="entry_submitted_long",
        took_trade=True,
    )

    export = engine.export_lora_training_dataset("BTCUSDT", horizon_minutes=15, limit=20)

    assert export.summary.total_examples == 1
    assert export.summary.long_examples == 1
    assert export.examples[0].symbol == "BTCUSDT"
    assert export.examples[0].label_action == Direction.LONG
    assert export.examples[0].messages[0]["role"] == "system"
    assert "signal" in export.examples[0].prompt
    assert "\"structure\"" in export.examples[0].prompt
    assert "\"session\"" in export.examples[0].prompt
    assert "\"data_quality\"" in export.examples[0].prompt
    assert "\"prediction\":\"LONG\"" in export.examples[0].completion

    store.close()


def test_lora_export_undersamples_majority_class(tmp_path: Path) -> None:
    store = LiquidationStore(db_path=tmp_path / "lora_balance.db")
    engine = SignalEngine(client=FakeClient(), liquidation_store=store)

    def persist_example(index: int, label_action: Direction) -> None:
        snapshot_id = f"snapshot-{index}"
        event_ts = 1_700_000_000_000 + (index * 60_000)
        snapshot = TrainingSnapshotRecord(
            snapshot_id=snapshot_id,
            symbol="BTCUSDT",
            event_ts=event_ts,
            exchange="BYBIT",
            environment="DEMO",
            mark_price=100.0 + index,
            signal_direction=Direction.LONG,
            signal_confidence=0.7,
            signal_quality="HIGH",
            decision_action="waiting_for_setup",
            took_trade=False,
            raw_payload={
                "mode": "SCALPING",
                "bot_signal": {
                    "direction": "LONG",
                    "confidence": 0.7,
                    "signal_quality": "HIGH",
                    "current_price": 100.0 + index,
                    "reasons": ["test"],
                },
                "market_behavior": {"mode": "RANGING", "bias": "NEUTRAL", "regime": "MIXED", "bias_strength": 0.2},
                "features": {
                    "spread_bps": 1.0,
                    "buy_flow_ratio": 0.5,
                    "short_volatility_bps": 12.0,
                    "volume_zscore": 0.1,
                    "funding_rate_bps": 0.0,
                    "basis_bps": 0.0,
                    "open_interest_change_pct": 0.0,
                    "htf_regime": "MIXED",
                },
                "scoring": {
                    "raw_score": 0.1,
                    "confidence": 0.2,
                    "alignment_bonus": 0.0,
                    "conflict_penalty": 0.0,
                },
                "risk": {
                    "stop_bps": 10.0,
                    "tp_bps": 15.0,
                    "risk_reward": 1.5,
                },
                "timeframes": {},
                "ai_decision": {},
                "liquidation_map": {"dominant_pull": "FLAT", "confidence": 0.0, "current_price": 100.0 + index},
            },
        )
        store.persist_training_snapshot(
            snapshot,
            {
                "snapshot_id": snapshot_id,
                "symbol": "BTCUSDT",
                "event_ts": event_ts,
                "session_id": "session-1",
                "mode": "SCALPING",
                "decision_action": "waiting_for_setup",
                "took_trade": False,
                "ai_verdict": None,
                "ai_reason": None,
            },
        )
        store.persist_training_label(
            TrainingSnapshotLabel(
                snapshot_id=snapshot_id,
                symbol="BTCUSDT",
                event_ts=event_ts,
                horizon_minutes=15,
                status="RESOLVED",
                label_action=label_action,
                expires_at=event_ts + (15 * 60_000),
                resolved_at=event_ts + (15 * 60_000),
                upper_barrier_price=101.0,
                lower_barrier_price=99.0,
                terminal_price=100.5,
                max_up_pct=0.8,
                max_down_pct=0.3,
                raw_payload={"entry_price": 100.0},
            )
        )

    persist_example(1, Direction.LONG)
    persist_example(2, Direction.SHORT)
    persist_example(3, Direction.SHORT)
    persist_example(4, Direction.FLAT)
    persist_example(5, Direction.FLAT)

    raw_export = engine.export_lora_training_dataset("BTCUSDT", horizon_minutes=15, limit=20, balance_mode="none")
    balanced_export = engine.export_lora_training_dataset(
        "BTCUSDT",
        horizon_minutes=15,
        limit=20,
        balance_mode="undersample_majority",
    )

    assert raw_export.summary.raw_examples == 5
    assert raw_export.summary.total_examples == 5
    assert balanced_export.summary.raw_examples == 5
    assert balanced_export.summary.total_examples == 3
    assert balanced_export.summary.long_examples == 1
    assert balanced_export.summary.short_examples == 1
    assert balanced_export.summary.flat_examples == 1

    store.close()
