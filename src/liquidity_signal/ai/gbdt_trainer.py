"""Gradient-boosted two-head trainer for crypto signal + price prediction.

This is the re-architected modelling path described in
``project-brief-solution-intent.md``.  It replaces the "validate the engine's
own signal" approach (which trained models to imitate a directional signal that
measured *worse than a coin flip* on resolved outcomes) with a model that learns
directly from triple-barrier outcomes and realised forward returns.

Design decisions, and why they matter for crypto specifically:

* **Two heads, one feature set.** A classification head predicts the resolved
  ``LONG`` / ``SHORT`` / ``FLAT`` barrier label; a regression head predicts the
  realised forward return in basis points and, via quantile losses, a
  ``[q10, q90]`` price range.  Direction answers "which way", the range answers
  "how far / how confident".
* **Leakage-free by construction.** Every field that encodes the old engine's
  decision, score, confidence or risk framing is excluded.  Those fields let a
  model mimic the deterministic engine instead of learning the market, which is
  exactly how the previous experiments failed.
* **Missing is not zero.** Binance ``/futures/data/*`` endpoints (open interest,
  taker ratio, long/short ratios) only retain ~30 days, so on a longer history
  those columns are mostly absent.  We map absent derivatives to ``NaN`` and use
  :class:`~sklearn.ensemble.HistGradientBoostingClassifier`, which handles
  missing values natively, instead of pretending "no data" means "no change".
* **Cross-asset context.** Alt-coins are largely BTC-beta at short horizons, so
  each row is augmented with the contemporaneous BTC return / volatility / bias
  and the symbol's move *relative* to BTC.
* **Purged, embargoed, chronological split.** Splits are by time, never random,
  and training rows whose label window overlaps the validation start are dropped
  so a 240-minute label cannot leak across the boundary.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Leakage control
# ---------------------------------------------------------------------------
# Prefixes of flattened feature names that describe what the *old engine* decided
# rather than what the *market* did.  Including any of these lets the model learn
# to reproduce the deterministic signal (measured directional precision ~0.39 on
# resolved 15m labels) instead of the outcome.
_LEAKAGE_PREFIXES = (
    "scoring.", "risk.", "bot_signal.", "cumulative.aggregate", "cumulative.bot",
    "cumulative.signal_quality", "cumulative.reasons", "ai_decision", "open_position",
    "signal_direction", "signal_confidence", "signal_quality", "decision_action",
    "took_trade", "liquidation_map.dominant_pull", "data_quality_context",
)

# Absolute price levels must never be fed raw; they are converted to relative
# distances (bps from close) so the model generalises across price regimes.
_ABSOLUTE_PRICE_KEYS = {
    "close", "ema20", "ema50", "ema200", "vwap", "nearest_support",
    "nearest_resistance", "mark_price", "current_price", "price_range_low",
    "price_range_high",
}

_DISTANCE_KEYS = ("ema20", "ema50", "ema200", "vwap", "nearest_support", "nearest_resistance")

LABELS = ("FLAT", "LONG", "SHORT")


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------
def _flatten(value: Any, prefix: str, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            _flatten(item, name, out)
    elif isinstance(value, list):
        for index, item in enumerate(value[:4]):
            if isinstance(item, (int, float, bool)):
                out[f"{prefix}[{index}]"] = item
    elif isinstance(value, bool):
        out[prefix] = int(value)
    elif isinstance(value, (int, float)):
        out[prefix] = float(value)
    elif isinstance(value, str):
        # Short categorical strings become one feature; long prose is dropped.
        if len(value) <= 24:
            out[prefix] = value


def _timeframe_features(timeframes: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tf, row in timeframes.items():
        if not isinstance(row, dict):
            continue
        try:
            close = float(row.get("close", 0.0) or 0.0)
        except (TypeError, ValueError):
            close = 0.0
        for key in ("rsi14", "macd", "macd_signal", "macd_histogram", "atr_pct",
                    "adx14", "volume_ratio", "breakout_failure_rate", "score",
                    "max_score", "confidence"):
            if key in row and isinstance(row[key], (int, float)):
                out[f"tf.{tf}.{key}"] = float(row[key])
        for cat in ("market_mode", "atr_state", "pattern"):
            if isinstance(row.get(cat), str) and len(row[cat]) <= 24:
                out[f"tf.{tf}.{cat}"] = row[cat]
        # Price levels -> signed distance in bps from close (regime-invariant).
        if close > 0.0:
            for key in _DISTANCE_KEYS:
                try:
                    level = float(row.get(key, 0.0) or 0.0)
                except (TypeError, ValueError):
                    level = 0.0
                if level > 0.0:
                    out[f"tf.{tf}.{key}_dist_bps"] = round(((close - level) / close) * 10_000, 4)
    return out


def _derivative_or_nan(features: dict[str, Any], oi_rows: int, taker_rows: int,
                       global_rows: int) -> dict[str, Any]:
    """Return positioning features, substituting NaN where the source was absent.

    ``0.0`` from an empty slice is indistinguishable from a genuine "no change";
    NaN lets the GBDT branch on missingness instead of learning a false zero.
    """
    out: dict[str, Any] = {}
    oi = features.get("open_interest_change_pct")
    out["deriv.oi_change_pct"] = float(oi) if oi_rows >= 2 and oi is not None else math.nan
    for key, present in (
        ("taker_buy_sell_ratio", taker_rows > 0),
        ("global_long_short_ratio", global_rows > 0),
        ("top_trader_account_ratio", global_rows > 0),
        ("top_trader_position_ratio", global_rows > 0),
    ):
        value = features.get(key)
        out[f"deriv.{key}"] = float(value) if present and value is not None else math.nan
    return out


def _row_features(payload: dict[str, Any]) -> dict[str, Any]:
    features: dict[str, Any] = {}
    dq = payload.get("data_quality_context", {}) or {}

    # Core market microstructure / momentum (kline-derived: fully available).
    for key in ("spread_bps", "imbalance_l1", "imbalance_l5", "imbalance_l10",
                "weighted_depth_imbalance", "buy_flow_ratio", "short_volatility_bps",
                "micro_momentum_bps", "intraday_momentum_bps", "volume_zscore",
                "liquidity_gap_bps", "funding_rate_bps", "basis_bps"):
        value = payload.get("features", {}).get(key)
        if isinstance(value, (int, float)):
            features[f"feat.{key}"] = float(value)
    for cat in ("htf_bias", "htf_regime"):
        value = payload.get("features", {}).get(cat)
        if isinstance(value, (int, float)):
            features[f"feat.{cat}"] = float(value)
        elif isinstance(value, str):
            features[f"feat.{cat}"] = value

    # Derivatives with explicit missingness (30-day retention reality).
    features.update(_derivative_or_nan(
        payload.get("features", {}),
        int(dq.get("oi_rows", 0) or 0),
        int(dq.get("taker_rows", 0) or 0),
        int(dq.get("global_ratio_rows", 0) or 0),
    ))

    features.update(_timeframe_features(payload.get("timeframes", {}) or {}))

    for group in ("whale_context", "structure_context"):
        _flatten(payload.get(group, {}) or {}, group, features)

    behavior = payload.get("market_behavior", {}) or {}
    for key in ("regime", "bias", "mode"):
        if isinstance(behavior.get(key), str):
            features[f"behavior.{key}"] = behavior[key]
    if isinstance(behavior.get("bias_strength"), (int, float)):
        features["behavior.bias_strength"] = float(behavior["bias_strength"])

    # Liquidation map: keep only the confidence scalar (dominant_pull is an
    # engine verdict and is excluded as leakage).
    liq = payload.get("liquidation_map", {}) or {}
    if isinstance(liq.get("confidence"), (int, float)):
        features["liq.confidence"] = float(liq["confidence"])

    # Session as cyclical encodings so 23:00 and 00:00 are neighbours.
    session = payload.get("session_context", {}) or {}
    hour = session.get("utc_hour")
    if isinstance(hour, (int, float)):
        features["session.hour_sin"] = round(math.sin(2 * math.pi * hour / 24), 5)
        features["session.hour_cos"] = round(math.cos(2 * math.pi * hour / 24), 5)
    for cat in ("session", "session_type"):
        if isinstance(session.get(cat), str):
            features[f"session.{cat}"] = session[cat]
    weekday = session.get("weekday")
    if isinstance(weekday, (int, float)):
        features["session.weekday"] = float(weekday)

    # Strip anything that slipped through matching a leakage prefix or a raw
    # absolute price key.
    for name in list(features):
        leaf = name.split(".")[-1]
        if name.startswith(_LEAKAGE_PREFIXES) or leaf in _ABSOLUTE_PRICE_KEYS:
            features.pop(name, None)
    return features


# ---------------------------------------------------------------------------
# Data loading + cross-asset alignment
# ---------------------------------------------------------------------------
def _load_dataset(db_path: str, horizon_minutes: int,
                  btc_symbol: str = "BTCUSDT") -> list[dict[str, Any]]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Cross-asset lookup: BTC market state indexed by event_ts.
    btc_by_ts: dict[int, dict[str, Any]] = {}
    for row in cur.execute(
        "SELECT event_ts, mark_price, raw_json FROM training_snapshots WHERE symbol=?",
        (btc_symbol,),
    ):
        payload = json.loads(row["raw_json"]).get("raw_payload", {})
        feats = payload.get("features", {}) or {}
        btc_by_ts[int(row["event_ts"])] = {
            "mark_price": float(row["mark_price"] or 0.0),
            "intraday_momentum_bps": float(feats.get("intraday_momentum_bps", 0.0) or 0.0),
            "short_volatility_bps": float(feats.get("short_volatility_bps", 0.0) or 0.0),
            "htf_bias": float(feats.get("htf_bias", 0.0) or 0.0),
        }

    rows: list[dict[str, Any]] = []
    query = """
        SELECT s.snapshot_id, s.symbol, s.event_ts, s.mark_price, s.raw_json AS snap_json,
               l.label_action, l.terminal_price
        FROM training_labels l
        JOIN training_snapshots s ON s.snapshot_id = l.snapshot_id
        WHERE l.horizon_minutes = ? AND l.status = 'RESOLVED'
              AND l.terminal_price > 0 AND s.mark_price > 0
        ORDER BY s.event_ts ASC
    """
    for row in cur.execute(query, (horizon_minutes,)):
        payload = json.loads(row["snap_json"]).get("raw_payload", {})
        features = _row_features(payload)

        mark = float(row["mark_price"])
        event_ts = int(row["event_ts"])
        btc = btc_by_ts.get(event_ts)
        if btc and btc["mark_price"] > 0:
            features["xasset.btc_momentum_bps"] = btc["intraday_momentum_bps"]
            features["xasset.btc_volatility_bps"] = btc["short_volatility_bps"]
            features["xasset.btc_htf_bias"] = btc["htf_bias"]
            own_mom = features.get("feat.intraday_momentum_bps", 0.0)
            features["xasset.rel_momentum_bps"] = own_mom - btc["intraday_momentum_bps"]
        else:
            for key in ("btc_momentum_bps", "btc_volatility_bps", "btc_htf_bias", "rel_momentum_bps"):
                features[f"xasset.{key}"] = math.nan

        forward_return_bps = ((float(row["terminal_price"]) - mark) / mark) * 10_000
        rows.append({
            "event_ts": event_ts,
            "symbol": row["symbol"],
            "features": features,
            "direction": str(row["label_action"]),
            "return_bps": round(forward_return_bps, 4),
        })
    con.close()
    return rows


def _chronological_split(rows: list[dict[str, Any]], horizon_minutes: int,
                         val_frac: float = 0.15, test_frac: float = 0.15):
    """Time-ordered split with an embargo so label windows cannot cross a boundary."""
    ordered = sorted(rows, key=lambda item: item["event_ts"])
    n = len(ordered)
    test_start = int(n * (1 - test_frac))
    val_start = int(n * (1 - test_frac - val_frac))
    embargo_ms = horizon_minutes * 60_000

    val_start_ts = ordered[val_start]["event_ts"] if val_start < n else math.inf
    test_start_ts = ordered[test_start]["event_ts"] if test_start < n else math.inf

    train = [r for r in ordered[:val_start] if r["event_ts"] + embargo_ms <= val_start_ts]
    validation = [r for r in ordered[val_start:test_start]
                  if r["event_ts"] + embargo_ms <= test_start_ts]
    test = ordered[test_start:]
    return train, validation, test


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def _classification_metrics(actual: list[str], predicted: list[str]) -> dict[str, Any]:
    from sklearn.metrics import (accuracy_score, balanced_accuracy_score,
                                 confusion_matrix, f1_score, recall_score)
    recalls = recall_score(actual, predicted, labels=list(LABELS), average=None, zero_division=0)
    directional = [i for i, v in enumerate(predicted) if v != "FLAT"]
    directional_correct = sum(predicted[i] == actual[i] for i in directional)
    return {
        "examples": len(actual),
        "accuracy": round(float(accuracy_score(actual, predicted)), 4) if actual else None,
        "balanced_accuracy": round(float(balanced_accuracy_score(actual, predicted)), 4) if actual else None,
        "macro_f1": round(float(f1_score(actual, predicted, labels=list(LABELS), average="macro", zero_division=0)), 4) if actual else None,
        "directional_coverage": round(len(directional) / len(actual), 4) if actual else 0.0,
        "directional_precision": round(directional_correct / len(directional), 4) if directional else None,
        "recall": {label: round(float(v), 4) for label, v in zip(LABELS, recalls)},
        "confusion_matrix": confusion_matrix(actual, predicted, labels=list(LABELS)).tolist() if actual else [],
        "labels": list(LABELS),
    }


def _apply_policy(classes: list[str], probabilities, threshold: float, margin: float) -> list[str]:
    predictions: list[str] = []
    for row in probabilities:
        ranked = sorted(zip(classes, row), key=lambda item: item[1], reverse=True)
        label, top = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        predictions.append(label if top >= threshold and (top - second) >= margin else "FLAT")
    return predictions


def _regression_metrics(actual_bps: list[float], q10, q50, q90) -> dict[str, Any]:
    import numpy as np
    actual = np.asarray(actual_bps, dtype=float)
    if actual.size == 0:
        return {"examples": 0}
    q10, q50, q90 = map(lambda a: np.asarray(a, dtype=float), (q10, q50, q90))
    directional_hit = float(np.mean((np.sign(q50) == np.sign(actual))[actual != 0])) if np.any(actual != 0) else None
    return {
        "examples": int(actual.size),
        "mae_bps": round(float(np.mean(np.abs(q50 - actual))), 3),
        "rmse_bps": round(float(np.sqrt(np.mean((q50 - actual) ** 2))), 3),
        "sign_hit_rate": round(directional_hit, 4) if directional_hit is not None else None,
        "range_coverage": round(float(np.mean((actual >= q10) & (actual <= q90))), 4),
        "median_range_width_bps": round(float(np.median(q90 - q10)), 3),
    }


# ---------------------------------------------------------------------------
# Training entry point
# ---------------------------------------------------------------------------
def train_gbdt_dual_head(
    db_path: str,
    output_dir: str,
    *,
    horizon_minutes: int = 60,
    random_seed: int = 42,
) -> dict[str, Any]:
    """Train and evaluate the two-head model; write artefacts + a gate report."""
    import joblib
    import numpy as np
    import pandas as pd
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor

    rows = _load_dataset(db_path, horizon_minutes)
    if len(rows) < 200:
        raise ValueError(f"Only {len(rows)} resolved rows for {horizon_minutes}m; need >=200.")
    train, validation, test = _chronological_split(rows, horizon_minutes)
    if not validation or not test:
        raise ValueError("Empty validation/test split; need a longer history.")

    def frame(part: list[dict[str, Any]]) -> pd.DataFrame:
        return pd.DataFrame([r["features"] for r in part])

    train_x, validation_x, test_x = frame(train), frame(validation), frame(test)
    # Align columns across splits (union), preserving order from train.
    columns = list(train_x.columns)
    for extra in (validation_x, test_x):
        for col in extra.columns:
            if col not in columns:
                columns.append(col)
    train_x = train_x.reindex(columns=columns)
    validation_x = validation_x.reindex(columns=columns)
    test_x = test_x.reindex(columns=columns)

    # Drop numeric columns with too few observed values to bin (e.g. the
    # OI/taker derivatives that are ~99% missing on >30-day history) or that are
    # constant.  These carry no signal here and can only be earned back by
    # forward capture; keeping them just destabilises the binning.
    keep: list[str] = []
    for col in columns:
        series = train_x[col]
        if pd.api.types.is_numeric_dtype(series):
            observed = series.dropna()
            if observed.size >= 20 and observed.nunique() >= 2:
                keep.append(col)
        elif series.dropna().nunique() >= 2:
            keep.append(col)
    columns = keep
    train_x = train_x.reindex(columns=columns)
    validation_x = validation_x.reindex(columns=columns)
    test_x = test_x.reindex(columns=columns)

    # pandas>=3 stores text as StringDtype (not object), so detect categoricals
    # by "not numeric" rather than by an object-dtype check.
    categorical = [c for c in columns if not pd.api.types.is_numeric_dtype(train_x[c])]
    for col in categorical:
        for df in (train_x, validation_x, test_x):
            df[col] = df[col].astype("category")

    train_dir = [r["direction"] for r in train]
    validation_dir = [r["direction"] for r in validation]
    test_dir = [r["direction"] for r in test]

    classifier = HistGradientBoostingClassifier(
        loss="log_loss",
        learning_rate=0.05,
        max_iter=400,
        max_leaf_nodes=31,
        min_samples_leaf=25,
        l2_regularization=1.0,
        class_weight="balanced",
        categorical_features=categorical or None,
        early_stopping=True,
        validation_fraction=0.15,
        random_state=random_seed,
    )
    classifier.fit(train_x, train_dir)
    classes = [str(c) for c in classifier.classes_]

    # Select decision policy (threshold + margin) on the validation split.
    validation_proba = classifier.predict_proba(validation_x)
    best = None
    for threshold_int in range(34, 71, 2):
        for margin_int in range(0, 17, 2):
            threshold, margin = threshold_int / 100.0, margin_int / 100.0
            predicted = _apply_policy(classes, validation_proba, threshold, margin)
            metrics = _classification_metrics(validation_dir, predicted)
            precision = metrics["directional_precision"] or 0.0
            coverage = metrics["directional_coverage"]
            score = precision * min(coverage / 0.15, 1.0) + 0.2 * (metrics["balanced_accuracy"] or 0.0)
            candidate = (score, coverage, threshold, margin, metrics)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    _, _, sel_threshold, sel_margin, validation_metrics = best

    test_proba = classifier.predict_proba(test_x)
    test_predicted = _apply_policy(classes, test_proba, sel_threshold, sel_margin)
    classification_test = _classification_metrics(test_dir, test_predicted)

    # Regression head: three quantile models -> predicted return range.
    train_ret = np.asarray([r["return_bps"] for r in train], dtype=float)
    test_ret = [r["return_bps"] for r in test]
    quantile_models = {}
    predictions = {}
    for tag, quantile in (("q10", 0.1), ("q50", 0.5), ("q90", 0.9)):
        model = HistGradientBoostingRegressor(
            loss="quantile", quantile=quantile,
            learning_rate=0.05, max_iter=400, max_leaf_nodes=31,
            min_samples_leaf=25, l2_regularization=1.0,
            categorical_features=categorical or None,
            early_stopping=True, validation_fraction=0.15,
            random_state=random_seed,
        )
        model.fit(train_x, train_ret)
        quantile_models[tag] = model
        predictions[tag] = model.predict(test_x)
    regression_test = _regression_metrics(test_ret, predictions["q10"], predictions["q50"], predictions["q90"])

    directional_precision = classification_test["directional_precision"] or 0.0
    validation_precision = validation_metrics["directional_precision"] or 0.0
    deployment_gate = {
        "passed": bool(
            directional_precision >= 0.52
            and classification_test["directional_coverage"] >= 0.10
            and (classification_test["balanced_accuracy"] or 0.0) >= 0.40
            and abs(validation_precision - directional_precision) <= 0.10
            and (regression_test.get("sign_hit_rate") or 0.0) >= 0.50
            and (regression_test.get("range_coverage") or 0.0) >= 0.70
        ),
        "requirements": {
            "directional_precision": 0.52,
            "directional_coverage": 0.10,
            "balanced_accuracy": 0.40,
            "max_validation_test_precision_gap": 0.10,
            "regression_sign_hit_rate": 0.50,
            "regression_range_coverage": 0.70,
        },
    }

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "classifier": classifier,
            "quantile_models": quantile_models,
            "columns": columns,
            "categorical": categorical,
            "classes": classes,
            "threshold": sel_threshold,
            "margin": sel_margin,
            "horizon_minutes": horizon_minutes,
            "task": "gbdt_dual_head",
        },
        target / "model.joblib",
    )
    summary = {
        "db_path": db_path,
        "horizon_minutes": horizon_minutes,
        "split": "test",
        "base_model": "sklearn.HistGradientBoosting",
        "adapter_path": str(target),
        "rows": {"train": len(train), "validation": len(validation), "test": len(test)},
        "features": len(columns),
        "categorical_features": len(categorical),
        "policy": {"threshold": sel_threshold, "margin": sel_margin},
        "examples": classification_test["examples"],
        "accuracy": classification_test["accuracy"],
        "invalid_outputs": 0,
        "classification_validation": validation_metrics,
        "classification_test": classification_test,
        "regression_test": regression_test,
        "deployment_gate": deployment_gate,
        "note": (
            "Classification+regression audit on a purged chronological test split. "
            "Live promotion still requires the strategy-level PnL backtest (fees, "
            "slippage, drawdown) enforced by ai.deployment."
        ),
    }
    (target / "evaluation.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
