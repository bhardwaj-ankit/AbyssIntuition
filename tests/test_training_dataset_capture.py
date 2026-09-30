import sqlite3
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
)
from liquidity_signal.service.engine import SignalEngine
from liquidity_signal.service.liquidation_store import LiquidationStore
from tests.test_backtest import FakeClient


def _build_explain(symbol: str, decision_ts: int) -> SignalExplainResult:
    signal = SignalResult(
        symbol=symbol,
        direction=Direction.LONG,
        confidence=0.72,
        current_price=100.0,
        tp=101.0,
        sl=99.0,
        reasons=["trend_follow"],
        horizon="MTF 5m/15m/1h",
        decision_ts=decision_ts,
        expires_at=decision_ts + (90 * 1000),
        signal_quality="ELEVATED",
    )
    features = LiquidityFeatures(
        symbol=symbol,
        mark_price=100.0,
        spread_bps=2.0,
        imbalance_l1=0.2,
        imbalance_l5=0.15,
        imbalance_l10=0.10,
        weighted_depth_imbalance=0.12,
        buy_flow_ratio=0.58,
        short_volatility_bps=24.0,
        micro_momentum_bps=12.0,
        intraday_momentum_bps=26.0,
        volume_zscore=1.1,
        liquidity_gap_bps=1.0,
        funding_rate_bps=1.2,
        basis_bps=2.0,
        open_interest_change_pct=1.5,
        taker_buy_sell_ratio=1.05,
        global_long_short_ratio=1.01,
        top_trader_account_ratio=1.02,
        top_trader_position_ratio=1.03,
        htf_bias=0.45,
        htf_regime="TRENDING",
    )
    scoring = ScoringBreakdown(
        spread_gate_triggered=False,
        max_spread_bps=8.0,
        imbalance_l1_weighted=0.1,
        imbalance_l5_weighted=0.05,
        imbalance_l10_weighted=0.03,
        weighted_depth_weighted=0.02,
        flow_weighted=0.04,
        microstructure_score=0.24,
        momentum_score=0.31,
        regime_score=0.45,
        positioning_score=0.18,
        sentiment_score=0.12,
        alignment_bonus=0.08,
        conflict_penalty=0.04,
        execution_penalty=0.02,
        crowding_penalty=0.01,
        raw_score=0.42,
        confidence=0.68,
        long_threshold=0.2,
        short_threshold=-0.2,
        min_confidence=0.55,
    )
    risk = RiskBreakdown(
        base_stop_bps=18.0,
        spread_adjustment_bps=1.0,
        adjusted_stop_bps=19.0,
        stop_bps=19.0,
        confidence_boost=0.04,
        tp_bps=32.0,
        risk_reward=1.68,
    )
    return SignalExplainResult(signal=signal, features=features, scoring=scoring, risk=risk)


def test_training_dataset_capture_persists_snapshots_and_resolves_labels(tmp_path: Path) -> None:
    store = LiquidationStore(db_path=tmp_path / "training_dataset.db")
    engine = SignalEngine(client=FakeClient(), liquidation_store=store)
    decision_ts = 1_700_000_000_000
    explain = _build_explain("BTCUSDT", decision_ts=decision_ts)
    cumulative = CumulativeSignalResponse(
        symbol="BTCUSDT",
        generated_at=decision_ts,
        market_mode="TRENDING",
        strategy_mode="TREND_FOLLOW",
        aggregate_direction=Direction.LONG,
        aggregate_confidence=0.74,
        agreement_ratio=0.67,
        signal_quality="HIGH",
        reasons=["Market mode trending with trend follow bias"],
        bot_signal=explain.signal,
        timeframes=[],
    )

    engine.capture_training_snapshot(
        symbol="BTCUSDT",
        exchange="BYBIT",
        environment="DEMO",
        session_id="session-1",
        mode="BALANCED",
        explain=explain,
        cumulative=cumulative,
        liq_map=None,
        behavior={"bias": "BULLISH", "bias_strength": 0.42, "mode": "TRENDING"},
        bot_signal=explain.signal,
        open_position=None,
        ai_decision=None,
        decision_action="entry_submitted_long",
        took_trade=True,
    )

    dataset = engine.load_training_dataset("BTCUSDT", limit=20, resolved_only=False)

    assert dataset.summary.total_snapshots == 1
    assert dataset.summary.total_decisions == 1
    assert dataset.summary.total_labels == len(engine.TRAINING_LABEL_HORIZONS_MINUTES)
    assert dataset.summary.resolved_labels == len(engine.TRAINING_LABEL_HORIZONS_MINUTES)
    assert dataset.snapshots[0].decision_action == "entry_submitted_long"
    assert dataset.snapshots[0].took_trade is True
    assert all(label.status == "RESOLVED" for label in dataset.labels)
    assert any(label.label_action == Direction.LONG for label in dataset.labels)

    store.close()


def test_training_label_v2_schema_migrates_existing_store(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_training_labels.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE training_labels (
            snapshot_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            event_ts INTEGER NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            status TEXT NOT NULL,
            label_action TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            resolved_at INTEGER,
            upper_barrier_price REAL NOT NULL,
            lower_barrier_price REAL NOT NULL,
            terminal_price REAL,
            max_up_pct REAL NOT NULL DEFAULT 0.0,
            max_down_pct REAL NOT NULL DEFAULT 0.0,
            raw_json TEXT NOT NULL,
            PRIMARY KEY(snapshot_id, horizon_minutes)
        )
        """
    )
    connection.commit()
    connection.close()

    store = LiquidationStore(db_path=db_path)
    store.persist_training_label(
        TrainingSnapshotLabel(
            snapshot_id="v2-sample",
            symbol="BTCUSDT",
            event_ts=1_700_000_000_000,
            horizon_minutes=60,
            status="RESOLVED",
            label_action=Direction.LONG,
            expires_at=1_700_003_600_000,
            resolved_at=1_700_003_600_000,
            upper_barrier_price=101.0,
            lower_barrier_price=99.0,
            barrier_first_hit=Direction.LONG,
            barrier_hit_ts=1_700_000_060_000,
            barrier_hit_price=101.0,
            horizon_close_price=102.0,
            horizon_return_bps=200.0,
            max_favorable_excursion_pct=2.5,
            max_adverse_excursion_pct=0.4,
            terminal_price=102.0,
        )
    )

    loaded = store.load_training_labels_for_snapshot("v2-sample")[0]
    assert loaded["barrier_first_hit"] == "LONG"
    assert loaded["horizon_close_price"] == 102.0
    assert loaded["horizon_return_bps"] == 200.0
    store.close()
