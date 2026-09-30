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
    _walk_forward_windows,
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
            "order_book_source": "proxy",
            "trade_flow_source": "proxy",
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
            "basis_rows": 30,
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
            label_json = (
                json.dumps({"horizon_close_price": terminal, "barrier_ambiguous": False})
                if i % 2 == 0 else None
            )
            cur.execute(
                "INSERT INTO training_labels(snapshot_id,symbol,event_ts,horizon_minutes,"
                "status,label_action,terminal_price,raw_json) VALUES (?,?,?,?,?,?,?,?)",
                (sid, symbol, event_ts, 60, "RESOLVED", label, terminal, label_json),
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
    # Candle proxies are excluded from the production feature contract.
    assert not any(name.startswith("book.") for name in features)
    assert not any(name.startswith("proxy_book.") for name in features)


def test_extractor_routes_real_and_proxy_liquidity_to_distinct_columns():
    real_payload = _make_payload(20.0, "MIXED_UP", oi_present=True)
    real_payload["features"]["order_book_source"] = "real"
    real_payload["features"]["trade_flow_source"] = "real"
    real = _row_features(real_payload)
    assert real["book.spread_bps"] == 1.2
    assert real["orderflow.buy_flow_ratio"] == 0.5

    proxy = _row_features(
        _make_payload(20.0, "MIXED_UP", oi_present=True),
        include_proxy_liquidity=True,
    )
    assert proxy["proxy_book.spread_bps"] == 1.2
    assert proxy["proxy_orderflow.buy_flow_ratio"] == 0.5
    assert "book.spread_bps" not in proxy


def test_archive_profile_excludes_estimated_liquidation_proxy():
    payload = _make_payload(20.0, "MIXED_UP", oi_present=True)

    standard = _row_features(payload)
    archive_only = _row_features(payload, include_liquidation_proxy=False)

    assert standard["liq.confidence"] == 0.5
    assert not any(name.startswith("liq.") for name in archive_only)


def test_missing_derivatives_become_nan():
    absent = _row_features(_make_payload(20.0, "MIXED_UP", oi_present=False))
    assert math.isnan(absent["deriv.oi_change_pct"])
    assert math.isnan(absent["deriv.taker_buy_sell_ratio"])
    absent_payload = _make_payload(20.0, "MIXED_UP", oi_present=True)
    absent_payload["data_quality_context"]["funding_rows"] = 0
    absent_payload["data_quality_context"]["basis_rows"] = 0
    missing_rates = _row_features(absent_payload)
    assert math.isnan(missing_rates["feat.funding_rate_bps"])
    assert math.isnan(missing_rates["feat.basis_bps"])
    present = _row_features(_make_payload(20.0, "MIXED_UP", oi_present=True))
    assert not math.isnan(present["deriv.oi_change_pct"])


def test_walk_forward_windows_have_non_overlapping_tests():
    day = 86_400_000
    windows = _walk_forward_windows(
        0,
        180 * day,
        train_days=90,
        validation_days=15,
        test_days=15,
        step_days=30,
    )
    assert len(windows) == 3
    assert all(
        current["end_ts"] <= following["test_start_ts"]
        for current, following in zip(windows, windows[1:])
    )


def test_walk_forward_rejects_overlapping_test_windows():
    with pytest.raises(ValueError, match="step_days"):
        _walk_forward_windows(
            0,
            180 * 86_400_000,
            train_days=90,
            validation_days=15,
            test_days=15,
            step_days=10,
        )


def test_trainer_runs_and_writes_gated_artifacts(tmp_path):
    db = tmp_path / "training.db"
    _seed_db(db)
    out = tmp_path / "gbdt-60m"
    summary = train_gbdt_dual_head(str(db), str(out), horizon_minutes=60)

    assert (out / "model.joblib").exists()
    assert (out / "evaluation.json").exists()
    assert summary["split"] == "test"
    assert summary["rows"]["train"] > 0
    assert summary["feature_contract_version"] == 4
    assert set(summary["target_sources"]) == {
        "horizon_close_price", "legacy_terminal_price"
    }
    assert "deployment_gate" in summary
    assert set(summary["classification_test"]).issuperset(
        {"directional_precision", "directional_coverage", "balanced_accuracy"}
    )
    assert set(summary["regression_test"]).issuperset({"sign_hit_rate", "range_coverage"})
    # Chronological embargo must keep splits non-empty and ordered.
    assert summary["rows"]["validation"] > 0 and summary["rows"]["test"] > 0


def test_dataset_excludes_retired_symbols_before_parsing(tmp_path) -> None:
    from liquidity_signal.ai.gbdt_trainer import _load_dataset
    db = tmp_path / "selected.db"
    _seed_db(db, rows=8)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE training_snapshots SET symbol='PEPEUSDT', raw_json='invalid' WHERE symbol='BTCUSDT'")
        conn.execute("UPDATE training_labels SET symbol='PEPEUSDT' WHERE symbol='BTCUSDT'")
    rows = _load_dataset(str(db), 60)
    assert rows and {row["symbol"] for row in rows} == {"ETHUSDT"}
    assert _load_dataset(str(db), 60, symbols=[" ethusdt "]) == rows
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM training_snapshots WHERE symbol='PEPEUSDT'").fetchone()[0] == 8
    for invalid in ([], ["PEPEUSDT"]):
        with pytest.raises(ValueError, match="nonempty subset"):
            _load_dataset(str(db), 60, symbols=invalid)


def test_walk_forward_preserves_selected_universe(tmp_path, monkeypatch) -> None:
    from liquidity_signal.ai import gbdt_trainer as trainer
    selections = []
    def load(*args, **kwargs):
        selections.append(kwargs["symbols"])
        return [{"event_ts": day * 86_400_000} for day in range(181)]
    def train(*args, **kwargs):
        selections.append(kwargs["symbols"])
        return {"classification_test": {}, "regression_test": {}, "deployment_gate": {"passed": True}}
    monkeypatch.setattr(trainer, "_load_dataset", load)
    monkeypatch.setattr(trainer, "train_gbdt_dual_head", train)
    result = trainer.walk_forward_gbdt("unused", str(tmp_path / "folds"), symbols=["ETHUSDT"])
    assert result["fold_count"] > 0
    assert len(selections) == result["fold_count"] + 1
    assert all(selected == ("ETHUSDT",) for selected in selections)
    assert result["symbols"] == ["ETHUSDT"]
