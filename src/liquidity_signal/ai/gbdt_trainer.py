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

from liquidity_signal.universe import training_symbols


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


def _row_features(
    payload: dict[str, Any], *, include_proxy_liquidity: bool = False,
    include_liquidation_proxy: bool = True,
) -> dict[str, Any]:
    features: dict[str, Any] = {}
    dq = payload.get("data_quality_context", {}) or {}
    raw_features = payload.get("features", {}) or {}

    # Kline-derived market state. These fields have identical semantics in
    # historical and live operation.
    for key in ("short_volatility_bps", "micro_momentum_bps",
                "intraday_momentum_bps", "volume_zscore"):
        value = raw_features.get(key)
        if isinstance(value, (int, float)):
            features[f"feat.{key}"] = float(value)
    for cat in ("htf_bias", "htf_regime"):
        value = raw_features.get(cat)
        if isinstance(value, (int, float)):
            features[f"feat.{cat}"] = float(value)
        elif isinstance(value, str):
            features[f"feat.{cat}"] = value

    # Historical backfills without depth/trade archives synthesize these
    # values from candles. They must not share columns with real observations,
    # otherwise an offline model sees a different distribution in production.
    order_book_source = str(raw_features.get("order_book_source") or "unknown").lower()
    trade_flow_source = str(raw_features.get("trade_flow_source") or "unknown").lower()
    book_keys = ("spread_bps", "imbalance_l1", "imbalance_l5", "imbalance_l10",
                 "weighted_depth_imbalance", "liquidity_gap_bps")
    if order_book_source == "real":
        for key in book_keys:
            value = raw_features.get(key)
            if isinstance(value, (int, float)):
                features[f"book.{key}"] = float(value)
    elif order_book_source == "proxy" and include_proxy_liquidity:
        for key in book_keys:
            value = raw_features.get(key)
            if isinstance(value, (int, float)):
                features[f"proxy_book.{key}"] = float(value)

    buy_flow_ratio = raw_features.get("buy_flow_ratio")
    if trade_flow_source == "real" and isinstance(buy_flow_ratio, (int, float)):
        features["orderflow.buy_flow_ratio"] = float(buy_flow_ratio)
    elif (
        trade_flow_source == "proxy"
        and include_proxy_liquidity
        and isinstance(buy_flow_ratio, (int, float))
    ):
        features["proxy_orderflow.buy_flow_ratio"] = float(buy_flow_ratio)

    # Funding and basis are not available across the full REST backfill range.
    # Keep them missing when no source row existed at the decision timestamp;
    # the model can branch on NaN without confusing absence with a true zero.
    for key, quality_key in (
        ("funding_rate_bps", "funding_rows"),
        ("basis_bps", "basis_rows"),
    ):
        value = raw_features.get(key)
        if int(dq.get(quality_key, 0) or 0) > 0 and isinstance(value, (int, float)):
            features[f"feat.{key}"] = float(value)
        else:
            features[f"feat.{key}"] = float("nan")

    # Derivatives with explicit missingness (30-day retention reality).
    features.update(_derivative_or_nan(
        raw_features,
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
    if include_liquidation_proxy:
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
def _attach_vision(features: dict[str, Any], store: Any, symbol: str, event_ts: int) -> None:
    """Overwrite missing derivative features with ingested Binance Vision metrics.

    On >30-day history the live API cannot serve OI/positioning, so those columns
    arrive as NaN; when a Vision store is provided we fill them from bulk data.
    """
    row = store.nearest(symbol, event_ts)
    if not row:
        return
    mapping = {
        "deriv.taker_buy_sell_ratio": row.get("taker_ratio"),
        "deriv.global_long_short_ratio": row.get("global_account_ratio"),
        "deriv.top_trader_account_ratio": row.get("top_account_ratio"),
        "deriv.top_trader_position_ratio": row.get("top_position_ratio"),
    }
    for key, value in mapping.items():
        if value is not None and (key not in features or _is_nan(features[key])):
            features[key] = float(value)
    change = store.oi_change_pct(symbol, event_ts)
    if change is not None and _is_nan(features.get("deriv.oi_change_pct", math.nan)):
        features["deriv.oi_change_pct"] = float(change)
    if row.get("oi_value"):
        features["deriv.oi_value_log"] = round(math.log1p(float(row["oi_value"])), 6)


def _attach_vision_supplemental(
    features: dict[str, Any], store: Any, symbol: str, event_ts: int
) -> None:
    depth = store.nearest_depth(symbol, event_ts)
    if depth:
        for key in ("imbalance_020", "imbalance_1", "imbalance_5", "concentration_020"):
            features[f"archive_book.{key}"] = float(depth[key])
        for key in ("total_notional_020", "total_notional_1", "total_notional_5"):
            features[f"archive_book.{key}_log"] = math.log1p(float(depth[key]))
    funding = store.nearest_funding(symbol, event_ts)
    if funding:
        features["feat.funding_rate_bps"] = float(funding["funding_rate"]) * 10_000
        features["deriv.funding_interval_hours"] = float(funding["funding_interval_hours"])
    trade_flow = store.nearest_trade_flow(symbol, event_ts)
    if trade_flow:
        buy_ratio = float(trade_flow["buy_flow_ratio"])
        sell_quote = float(trade_flow["taker_sell_quote"])
        features["orderflow.buy_flow_ratio"] = buy_ratio
        features["orderflow.taker_imbalance"] = (2.0 * buy_ratio) - 1.0
        features["orderflow.taker_buy_sell_ratio"] = (
            float(trade_flow["taker_buy_quote"]) / sell_quote
            if sell_quote > 0
            else math.nan
        )
        features["archive_flow.quote_volume_log"] = math.log1p(
            float(trade_flow["quote_volume"])
        )
        features["archive_flow.trade_count_log"] = math.log1p(
            int(trade_flow["trade_count"])
        )
        features["archive_flow.avg_trade_notional_log"] = math.log1p(
            float(trade_flow["avg_trade_notional"])
        )
        features["archive_flow.range_bps"] = float(trade_flow["range_bps"])
        features["archive_flow.return_bps"] = float(trade_flow["return_bps"])


def _attach_open_onchain(
    features: dict[str, Any], store: Any, symbol: str, event_ts: int
) -> None:
    from liquidity_signal.data.open_onchain import SYMBOL_NETWORK

    network = SYMBOL_NETWORK.get(symbol.upper())
    if not network:
        return
    row = store.nearest_completed_day(network, event_ts)
    if not row:
        return
    for key in ("active_addresses", "tx_count", "market_cap_usd", "chain_tvl_usd"):
        value = row.get(key)
        if isinstance(value, (int, float)) and value > 0:
            features[f"onchain.{key}_log"] = math.log1p(float(value))
            change = store.change_pct(network, int(row["ts"]), key, days=7)
            if change is not None:
                features[f"onchain.{key}_change_7d_pct"] = float(change)


def _attach_liquidation_flow(
    features: dict[str, Any], store: Any, symbol: str, event_ts: int, mark_price: float
) -> None:
    features.update(
        store.liquidation_flow_features(symbol, event_ts, mark_price)
    )


def _attach_hyperliquid_archive_flow(
    features: dict[str, Any], store: Any, symbol: str, event_ts: int, mark_price: float
) -> None:
    features.update(
        store.hyperliquid_archive_flow_features(symbol, event_ts, mark_price)
    )


def _attach_cryptohft_archive_flow(
    features: dict[str, Any], store: Any, symbol: str, event_ts: int, mark_price: float
) -> None:
    features.update(
        store.cryptohft_archive_flow_features(symbol, event_ts, mark_price)
    )


def _is_nan(value: Any) -> bool:
    return isinstance(value, float) and math.isnan(value)


def _load_dataset(db_path: str, horizon_minutes: int,
                  btc_symbol: str = "BTCUSDT",
                  vision_db: str | None = None,
                  onchain_db: str | None = None,
                  liquidation_db: str | None = None,
                  cross_venue_db: str | None = None,
                  data_profile: str = "standard",
                  symbols: list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    selected = training_symbols(symbols)
    archive_profile = data_profile.startswith("archive_")
    uses_hyperliquid = "hyperliquid" in data_profile
    uses_cryptohft = "cryptohft" in data_profile
    uses_cross_venue = "cross_venue" in data_profile
    if data_profile == "archive_only" and liquidation_db is not None:
        raise ValueError("archive_only profile cannot load a liquidation database")
    if uses_hyperliquid and liquidation_db is None:
        raise ValueError("archive_hyperliquid profile requires a liquidation database")
    if uses_cryptohft and liquidation_db is None:
        raise ValueError("archive_cryptohft profile requires a liquidation database")
    if uses_cross_venue and (
        cross_venue_db is None or not Path(cross_venue_db).exists()
    ):
        raise ValueError("cross-venue archive profile requires a cross-venue database")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    vision_store = None
    supplemental_store = None
    if vision_db and Path(vision_db).exists():
        from liquidity_signal.data.binance_vision import VisionMetricsStore
        from liquidity_signal.data.binance_vision_supplemental import VisionSupplementalStore
        vision_store = VisionMetricsStore(vision_db)
        supplemental_store = VisionSupplementalStore(vision_db)
    onchain_store = None
    if onchain_db and Path(onchain_db).exists():
        from liquidity_signal.data.open_onchain import OpenOnchainStore
        onchain_store = OpenOnchainStore(onchain_db)
    liquidation_store = None
    if liquidation_db and Path(liquidation_db).exists():
        from liquidity_signal.service.liquidation_store import LiquidationStore

        liquidation_store = LiquidationStore(Path(liquidation_db))
    cross_venue_store = None
    if cross_venue_db and Path(cross_venue_db).exists():
        from liquidity_signal.data.cross_venue import CrossVenueStore

        cross_venue_store = CrossVenueStore(cross_venue_db)

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
    placeholders = ",".join("?" for _ in selected)
    query = f"""
        SELECT s.snapshot_id, s.symbol, s.event_ts, s.mark_price, s.raw_json AS snap_json,
               l.label_action, l.terminal_price, l.raw_json AS label_json
        FROM training_labels l
        JOIN training_snapshots s ON s.snapshot_id = l.snapshot_id
        WHERE l.horizon_minutes = ? AND l.status = 'RESOLVED'
              AND s.mark_price > 0
              AND s.symbol IN ({placeholders})
        ORDER BY s.event_ts ASC
    """
    for row in cur.execute(query, (horizon_minutes, *selected)):
        payload = json.loads(row["snap_json"]).get("raw_payload", {})
        features = _row_features(
            payload,
            include_liquidation_proxy=not archive_profile,
        )

        label_payload: dict[str, Any] = {}
        if row["label_json"]:
            try:
                parsed_label = json.loads(row["label_json"])
                if isinstance(parsed_label, dict):
                    label_payload = parsed_label
            except (TypeError, json.JSONDecodeError):
                label_payload = {}
        if label_payload.get("barrier_ambiguous") is True:
            continue

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

        if vision_store is not None:
            _attach_vision(features, vision_store, row["symbol"], event_ts)
        if supplemental_store is not None:
            _attach_vision_supplemental(features, supplemental_store, row["symbol"], event_ts)
        if onchain_store is not None:
            _attach_open_onchain(features, onchain_store, row["symbol"], event_ts)
        if cross_venue_store is not None:
            from liquidity_signal.data.cross_venue import attach_cross_venue_features

            attach_cross_venue_features(
                features, cross_venue_store, row["symbol"], event_ts
            )
        if liquidation_store is not None:
            if uses_cryptohft:
                _attach_cryptohft_archive_flow(
                    features, liquidation_store, row["symbol"], event_ts, mark
                )
            if uses_hyperliquid:
                _attach_hyperliquid_archive_flow(
                    features, liquidation_store, row["symbol"], event_ts, mark
                )
            if not uses_cryptohft and not uses_hyperliquid:
                _attach_liquidation_flow(
                    features, liquidation_store, row["symbol"], event_ts, mark
                )

        horizon_close = label_payload.get("horizon_close_price")
        target_source = "horizon_close_price"
        if not isinstance(horizon_close, (int, float)) or horizon_close <= 0:
            # Backwards compatibility for v1 datasets. Those rows used the
            # first barrier candle close, so reports make the fallback visible.
            horizon_close = row["terminal_price"]
            target_source = "legacy_terminal_price"
        if not isinstance(horizon_close, (int, float)) or horizon_close <= 0:
            continue
        forward_return_bps = ((float(horizon_close) - mark) / mark) * 10_000
        rows.append({
            "event_ts": event_ts,
            "symbol": row["symbol"],
            "features": features,
            "direction": str(row["label_action"]),
            "return_bps": round(forward_return_bps, 4),
            "target_source": target_source,
        })
    con.close()
    if vision_store is not None:
        vision_store.close()
    if supplemental_store is not None:
        supplemental_store.close()
    if onchain_store is not None:
        onchain_store.close()
    if liquidation_store is not None:
        liquidation_store.close()
    if cross_venue_store is not None:
        cross_venue_store.close()
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


def _walk_forward_windows(
    min_ts: int,
    max_ts: int,
    *,
    train_days: int,
    validation_days: int,
    test_days: int,
    step_days: int,
) -> list[dict[str, int]]:
    """Build rolling windows whose test segments never overlap."""
    if min(train_days, validation_days, test_days, step_days) <= 0:
        raise ValueError("Walk-forward durations must all be positive.")
    if step_days < test_days:
        raise ValueError("step_days must be >= test_days so test windows do not overlap.")
    day_ms = 86_400_000
    windows: list[dict[str, int]] = []
    start = int(min_ts)
    inclusive_limit = int(max_ts) + 1
    while True:
        validation_start = start + train_days * day_ms
        test_start = validation_start + validation_days * day_ms
        end = test_start + test_days * day_ms
        if end > inclusive_limit:
            break
        windows.append({
            "start_ts": start,
            "validation_start_ts": validation_start,
            "test_start_ts": test_start,
            "end_ts": end,
        })
        start += step_days * day_ms
    return windows


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
    vision_db: str | None = None,
    onchain_db: str | None = None,
    liquidation_db: str | None = None,
    cross_venue_db: str | None = None,
    data_profile: str = "standard",
    symbols: list[str] | tuple[str, ...] | None = None,
    window_start_ts: int | None = None,
    validation_start_ts: int | None = None,
    test_start_ts: int | None = None,
    window_end_ts: int | None = None,
) -> dict[str, Any]:
    """Train and evaluate the two-head model; write artefacts + a gate report."""
    import joblib
    import numpy as np
    import pandas as pd
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor

    selected = training_symbols(symbols)
    rows = _load_dataset(
        db_path, horizon_minutes, vision_db=vision_db, onchain_db=onchain_db,
        liquidation_db=liquidation_db, cross_venue_db=cross_venue_db,
        data_profile=data_profile, symbols=selected,
    )
    if window_start_ts is not None:
        rows = [row for row in rows if row["event_ts"] >= window_start_ts]
    if window_end_ts is not None:
        rows = [row for row in rows if row["event_ts"] < window_end_ts]
    if len(rows) < 200:
        raise ValueError(f"Only {len(rows)} resolved rows for {horizon_minutes}m; need >=200.")
    explicit_split = validation_start_ts is not None or test_start_ts is not None
    if explicit_split:
        if validation_start_ts is None or test_start_ts is None:
            raise ValueError("Both validation_start_ts and test_start_ts are required.")
        if validation_start_ts >= test_start_ts:
            raise ValueError("validation_start_ts must be earlier than test_start_ts.")
        embargo_ms = horizon_minutes * 60_000
        train = [
            row for row in rows
            if row["event_ts"] + embargo_ms <= validation_start_ts
        ]
        validation = [
            row for row in rows
            if validation_start_ts <= row["event_ts"]
            and row["event_ts"] + embargo_ms <= test_start_ts
        ]
        test = [row for row in rows if row["event_ts"] >= test_start_ts]
    else:
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
            "feature_contract_version": 4,
            "data_profile": data_profile,
            "symbols": list(selected),
        },
        target / "model.joblib",
    )
    summary = {
        "symbols": list(selected),
        "db_path": db_path,
        "vision_db": vision_db,
        "onchain_db": onchain_db,
        "liquidation_db": liquidation_db,
        "cross_venue_db": cross_venue_db,
        "data_profile": data_profile,
        "horizon_minutes": horizon_minutes,
        "split": "walk_forward_fold" if explicit_split else "test",
        "base_model": "sklearn.HistGradientBoosting",
        "adapter_path": str(target),
        "rows": {"train": len(train), "validation": len(validation), "test": len(test)},
        "target_sources": {
            source: sum(row["target_source"] == source for row in rows)
            for source in sorted({row["target_source"] for row in rows})
        },
        "feature_contract_version": 4,
        "window": {
            "start_ts": window_start_ts,
            "validation_start_ts": validation_start_ts,
            "test_start_ts": test_start_ts,
            "end_ts": window_end_ts,
        } if explicit_split else None,
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


def walk_forward_gbdt(
    db_path: str,
    output_dir: str,
    *,
    horizon_minutes: int = 60,
    vision_db: str | None = None,
    onchain_db: str | None = None,
    liquidation_db: str | None = None,
    cross_venue_db: str | None = None,
    data_profile: str = "standard",
    symbols: list[str] | tuple[str, ...] | None = None,
    train_days: int = 90,
    validation_days: int = 15,
    test_days: int = 15,
    step_days: int = 30,
    random_seed: int = 42,
) -> dict[str, Any]:
    """Train rolling folds and require stable performance across all of them."""
    selected = training_symbols(symbols)
    rows = _load_dataset(
        db_path, horizon_minutes, vision_db=vision_db, onchain_db=onchain_db,
        liquidation_db=liquidation_db, cross_venue_db=cross_venue_db,
        data_profile=data_profile, symbols=selected,
    )
    if not rows:
        raise ValueError(f"No resolved rows for {horizon_minutes}m.")
    windows = _walk_forward_windows(
        min(row["event_ts"] for row in rows),
        max(row["event_ts"] for row in rows),
        train_days=train_days,
        validation_days=validation_days,
        test_days=test_days,
        step_days=step_days,
    )
    if len(windows) < 2:
        raise ValueError("Need enough history for at least two walk-forward folds.")

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    folds: list[dict[str, Any]] = []
    for index, window in enumerate(windows, start=1):
        fold = train_gbdt_dual_head(
            db_path,
            str(target / f"fold-{index}"),
            horizon_minutes=horizon_minutes,
            random_seed=random_seed,
            vision_db=vision_db,
            onchain_db=onchain_db,
            liquidation_db=liquidation_db,
            cross_venue_db=cross_venue_db,
            data_profile=data_profile,
            symbols=selected,
            window_start_ts=window["start_ts"],
            validation_start_ts=window["validation_start_ts"],
            test_start_ts=window["test_start_ts"],
            window_end_ts=window["end_ts"],
        )
        fold["fold"] = index
        folds.append(fold)

    def values(group: str, metric: str) -> list[float]:
        return [
            float(fold[group][metric])
            for fold in folds
            if fold[group].get(metric) is not None
        ]

    aggregate: dict[str, Any] = {}
    for group, metric in (
        ("classification_test", "directional_precision"),
        ("classification_test", "directional_coverage"),
        ("classification_test", "balanced_accuracy"),
        ("regression_test", "sign_hit_rate"),
        ("regression_test", "range_coverage"),
    ):
        observed = values(group, metric)
        aggregate[metric] = {
            "mean": round(sum(observed) / len(observed), 4) if observed else None,
            "min": round(min(observed), 4) if observed else None,
            "max": round(max(observed), 4) if observed else None,
        }

    summary = {
        "db_path": db_path,
        "vision_db": vision_db,
        "onchain_db": onchain_db,
        "liquidation_db": liquidation_db,
        "cross_venue_db": cross_venue_db,
        "data_profile": data_profile,
        "horizon_minutes": horizon_minutes,
        "split": "walk_forward",
        "symbols": list(selected),
        "feature_contract_version": 4,
        "configuration": {
            "train_days": train_days,
            "validation_days": validation_days,
            "test_days": test_days,
            "step_days": step_days,
        },
        "fold_count": len(folds),
        "aggregate": aggregate,
        "folds": folds,
        "deployment_gate": {
            "passed": all(fold["deployment_gate"]["passed"] for fold in folds),
            "requirement": "Every non-overlapping walk-forward fold must pass.",
        },
    }
    (target / "walk_forward_evaluation.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return summary
