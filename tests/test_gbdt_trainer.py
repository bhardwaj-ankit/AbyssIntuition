from __future__ import annotations

import json
import math
import random
import sqlite3

import pytest

pytest.importorskip("sklearn")
pytest.importorskip("pandas")

from liquidity_signal.ai.gbdt_trainer import (  # noqa: E402
    _row_features,
    train_gbdt_dual_head,
)


def _make_payload(momentum_bps: float, regime: str, oi_present: bool) -> dict:
    return {
        "features": {
            "spread_bps": 1.2,
            "imbalance_l5": momentum_bps / 100.0,
            "buy_flow_ratio": 0.5,
            "short_volatility_bps": 5.0,
            "intraday_momentum_bps": momentum_bps,
            "volume_zscore": 0.1,
            "funding_rate_bps": 0.3,
            "basis_bps": -2.0,
            "open_interest_change_pct": 0.4 if oi_present else 0.0,
            "taker_buy_sell_ratio": 1.05 if oi_present else 1.0,
            "global_long_short_ratio": 2.0 if oi_present else 1.0,
            "htf_regime": regime,
            "htf_bias": 0.2,
        },
        # Leakage groups that must be excluded by the extractor.
        "scoring": {"confidence": 0.9, "raw_score": 3.2},
        "risk": {"stop_bps": 40.0, "risk_reward": 1.5},
        "bot_signal": {"direction": "LONG", "confidence": 0.8},
        "decision_action": "historical_wait",
        "took_trade": True,
        "timeframes": {
            "15m": {"rsi14": 55.0, "macd_histogram": 0.1, "atr_pct": 0.4,
                    "adx14": 20.0, "volume_ratio": 1.1, "close": 100.0,
                    "ema20": 99.5, "vwap": 99.0, "market_mode": "TRENDING"},
        },
        "whale_context": {"impulse_bps_5m": momentum_bps, "squeeze_risk": 0.2},
        "structure_context": {"range_position_30m": 0.5, "compression_ratio": 0.8},
        "market_behavior": {"regime": regime, "bias": "BULLISH", "mode": "TRENDING",
                            "bias_strength": 0.6},
        "liquidation_map": {"dominant_pull": "LONG", "confidence": 0.5},
        "session_context": {"utc_hour": 12, "weekday": 2, "session": "US",
                            "session_type": "PEAK"},
        "data_quality_context": {
            "oi_rows": 30 if oi_present else 0,
            "taker_rows": 30 if oi_present else 0,
            "global_ratio_rows": 30 if oi_present else 0,
            "funding_rows": 30,
        },
    }


def _seed_db(path, rows: int = 600) -> None:
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute("""CREATE TABLE training_snapshots(
        snapshot_id TEXT, symbol TEXT, event_ts INTEGER, exchange_name TEXT,
        environment TEXT, mark_price REAL, signal_direction TEXT,
        signal_confidence REAL, signal_quality TEXT, raw_json TEXT)""")
    cur.execute("""CREATE TABLE training_labels(
        id INTEGER PRIMARY KEY, snapshot_id TEXT, symbol TEXT, event_ts INTEGER,
        horizon_minutes INTEGER, status TEXT, label_action TEXT, expires_at INTEGER,
        resolved_at INTEGER, upper_barrier_price REAL, lower_barrier_price REAL,
        terminal_price REAL, max_up_pct REAL, max_down_pct REAL, raw_json TEXT)""")

    rng = random.Random(7)
    base_ts = 1_700_000_000_000
    for symbol in ("BTCUSDT", "ETHUSDT"):
        for i in range(rows):
            event_ts = base_ts + i * 3_600_000
            momentum = rng.uniform(-40, 40)
            # A learnable relationship: strong positive momentum tends up.
            drift = momentum * 0.4 + rng.gauss(0, 15)
            regime = "MIXED_UP" if momentum >= 0 else "MIXED_DOWN"
            oi_present = i > rows - 40  # only most-recent rows have positioning
            mark = 100.0
            terminal = mark * (1 + drift / 10_000)
            if drift > 8:
                label = "LONG"
            elif drift < -8:
                label = "SHORT"
            else:
                label = "FLAT"
            sid = f"{symbol}_{event_ts}"
            payload = {"raw_payload": _make_payload(momentum, regime, oi_present)}
            cur.execute(
                "INSERT INTO training_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, symbol, event_ts, "BINANCE", "HISTORICAL", mark,
                 "LONG", 0.7, "HIGH", json.dumps(payload)),
            )
            cur.execute(
                "INSERT INTO training_labels(snapshot_id,symbol,event_ts,horizon_minutes,"
                "status,label_action,terminal_price) VALUES (?,?,?,?,?,?,?)",
                (sid, symbol, event_ts, 60, "RESOLVED", label, terminal),
            )
    con.commit()
    con.close()


def test_extractor_excludes_leakage_and_absolute_prices():
    features = _row_features(_make_payload(20.0, "MIXED_UP", oi_present=True))
    # Engine-decision / scoring / risk fields must never appear.
    for banned in ("scoring.confidence", "scoring.raw_score", "risk.stop_bps",
                   "bot_signal.direction", "decision_action", "took_trade",
                   "liquidation_map.dominant_pull"):
        assert banned not in features
    # Absolute prices become relative distances, not raw levels.
    assert "tf.15m.close" not in features
    assert "tf.15m.ema20_dist_bps" in features
    # Market features and cyclical session encodings survive.
    assert "feat.intraday_momentum_bps" in features
    assert "session.hour_sin" in features


def test_missing_derivatives_become_nan():
    absent = _row_features(_make_payload(20.0, "MIXED_UP", oi_present=False))
    assert math.isnan(absent["deriv.oi_change_pct"])
    assert math.isnan(absent["deriv.taker_buy_sell_ratio"])
    present = _row_features(_make_payload(20.0, "MIXED_UP", oi_present=True))
    assert not math.isnan(present["deriv.oi_change_pct"])


def test_trainer_runs_and_writes_gated_artifacts(tmp_path):
    db = tmp_path / "training.db"
    _seed_db(db)
    out = tmp_path / "gbdt-60m"
    summary = train_gbdt_dual_head(str(db), str(out), horizon_minutes=60)

    assert (out / "model.joblib").exists()
    assert (out / "evaluation.json").exists()
    assert summary["split"] == "test"
    assert summary["rows"]["train"] > 0
    assert "deployment_gate" in summary
    assert set(summary["classification_test"]).issuperset(
        {"directional_precision", "directional_coverage", "balanced_accuracy"}
    )
    assert set(summary["regression_test"]).issuperset({"sign_hit_rate", "range_coverage"})
    # Chronological embargo must keep splits non-empty and ordered.
    assert summary["rows"]["validation"] > 0 and summary["rows"]["test"] > 0
