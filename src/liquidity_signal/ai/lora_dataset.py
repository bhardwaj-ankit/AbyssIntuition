from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from liquidity_signal.models import Direction, LoraTrainingExample


SYSTEM_PROMPT = (
    "You are an elite crypto market analyst. "
    "Read the structured market snapshot and predict the next directional move for the stated horizon. "
    "Return only structured JSON. Do not invent indicators. Prefer FLAT when edge is weak."
)


@dataclass(frozen=True)
class TrainingExampleBundle:
    snapshot: dict[str, Any]
    decision: dict[str, Any]
    label: dict[str, Any]


def _round(value: Any, digits: int = 4) -> float:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return 0.0


def _compact_reasons(values: list[Any], limit: int = 4) -> list[str]:
    return [str(item).strip() for item in values[:limit] if str(item).strip()]


def _compact_number_map(values: dict[str, Any], keys: tuple[str, ...]) -> dict[str, float]:
    return {key: _round(values.get(key, 0.0), 4) for key in keys if key in values}


def _compact_liquidation_levels(values: list[Any], limit: int = 3) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in values[:limit]:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "side": item.get("side", ""),
                "leverage": int(item.get("leverage", 0) or 0),
                "level_price": _round(item.get("level_price", 0.0), 4),
                "distance_bps": _round(item.get("distance_bps", 0.0), 2),
                "strength": _round(item.get("strength", 0.0), 4),
            }
        )
    return rows


def _compact_liquidation_clusters(values: list[Any], limit: int = 3) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in values[:limit]:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "side": item.get("side", ""),
                "center_price": _round(item.get("center_price", 0.0), 4),
                "min_price": _round(item.get("min_price", 0.0), 4),
                "max_price": _round(item.get("max_price", 0.0), 4),
                "total_strength": _round(item.get("total_strength", 0.0), 4),
                "contributing_levels": int(item.get("contributing_levels", 0) or 0),
            }
        )
    return rows


def _timeframe_lookup(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    cumulative = raw.get("cumulative", {})
    for row in cumulative.get("timeframes", []) if isinstance(cumulative, dict) else []:
        if not isinstance(row, dict):
            continue
        timeframe = str(row.get("timeframe", "")).strip()
        if timeframe:
            lookup[timeframe] = row

    compact_rows = raw.get("timeframes", {})
    if isinstance(compact_rows, dict):
        for timeframe, row in compact_rows.items():
            if isinstance(row, dict):
                lookup[str(timeframe)] = {**lookup.get(str(timeframe), {}), **row}
    return lookup


def _snapshot_view(bundle: TrainingExampleBundle) -> dict[str, Any]:
    raw = bundle.snapshot.get("raw_payload", {})
    features = raw.get("features", {})
    scoring = raw.get("scoring", {})
    risk = raw.get("risk", {})
    bot_signal = raw.get("bot_signal", {})
    liq_map = raw.get("liquidation_map", {})
    liq_metrics = liq_map.get("market_metrics", {}) if isinstance(liq_map.get("market_metrics", {}), dict) else {}
    liq_assumptions = liq_map.get("assumptions", {}) if isinstance(liq_map.get("assumptions", {}), dict) else {}
    liq_meta = liq_map.get("meta", {}) if isinstance(liq_map.get("meta", {}), dict) else {}
    behavior = raw.get("market_behavior", {})
    ai_decision = raw.get("ai_decision", {})
    timeframes = _timeframe_lookup(raw)
    whale_context = raw.get("whale_context", {})
    structure_context = raw.get("structure_context", {})
    session_context = raw.get("session_context", {})
    data_quality_context = raw.get("data_quality_context", {})

    tf_payload: dict[str, Any] = {}
    for timeframe in ("5m", "15m", "1h"):
        row = timeframes.get(timeframe, {})
        tf_payload[timeframe] = {
            "market_mode": row.get("market_mode", ""),
            "direction": row.get("direction", ""),
            "confidence": _round(row.get("confidence", 0.0), 3),
            "score": _round(row.get("score", 0.0), 3),
            "close": _round(row.get("close", 0.0), 4),
            "ema20": _round(row.get("ema20", 0.0), 4),
            "ema50": _round(row.get("ema50", 0.0), 4),
            "ema200": _round(row.get("ema200", 0.0), 4),
            "vwap": _round(row.get("vwap", 0.0), 4),
            "rsi14": _round(row.get("rsi14", 0.0), 2),
            "macd": _round(row.get("macd", 0.0), 4),
            "macd_signal": _round(row.get("macd_signal", 0.0), 4),
            "macd_histogram": _round(row.get("macd_histogram", 0.0), 4),
            "atr_pct": _round(row.get("atr_pct", 0.0), 3),
            "atr_state": row.get("atr_state", ""),
            "adx14": _round(row.get("adx14", 0.0), 2),
            "volume_ratio": _round(row.get("volume_ratio", 0.0), 3),
            "breakout_failure_rate": _round(row.get("breakout_failure_rate", 0.0), 3),
            "pattern": row.get("pattern", ""),
            "nearest_support": _round(row.get("nearest_support", 0.0), 4),
            "nearest_resistance": _round(row.get("nearest_resistance", 0.0), 4),
            "reasons": _compact_reasons(row.get("reasons", []), limit=3),
        }

    return {
        "symbol": bundle.snapshot.get("symbol", ""),
        "exchange": bundle.snapshot.get("exchange", ""),
        "environment": bundle.snapshot.get("environment", ""),
        "mode": raw.get("mode", ""),
        "decision_action": bundle.decision.get("decision_action", ""),
        "took_trade": bool(bundle.decision.get("took_trade", False)),
        "signal": {
            "direction": bot_signal.get("direction", bundle.snapshot.get("signal_direction", "")),
            "confidence": _round(bot_signal.get("confidence", bundle.snapshot.get("signal_confidence", 0.0)), 3),
            "signal_quality": bot_signal.get("signal_quality", bundle.snapshot.get("signal_quality", "")),
            "current_price": _round(bot_signal.get("current_price", bundle.snapshot.get("mark_price", 0.0)), 4),
            "reasons": _compact_reasons(bot_signal.get("reasons", []), limit=4),
        },
        "liquidity": {
            "dominant_pull": liq_map.get("dominant_pull", "FLAT"),
            "confidence": _round(liq_map.get("confidence", 0.0), 3),
            "mark_price": _round(liq_map.get("current_price", bundle.snapshot.get("mark_price", 0.0)), 4),
            "source": liq_map.get("source", ""),
            "price_range_low": _round(liq_map.get("price_range_low", 0.0), 4),
            "price_range_high": _round(liq_map.get("price_range_high", 0.0), 4),
            "pull_up": _round(liq_meta.get("pull_up", 0.0), 4),
            "pull_down": _round(liq_meta.get("pull_down", 0.0), 4),
            "long_share": _round(liq_meta.get("long_share", 0.0), 4),
            "short_share": _round(liq_meta.get("short_share", 0.0), 4),
            "event_weight_applied": _round(liq_meta.get("event_weight_applied", 0.0), 4),
            "events_used": int(liq_map.get("events_used", 0) or 0),
            "levels_above": _compact_liquidation_levels(liq_map.get("levels_above", [])),
            "levels_below": _compact_liquidation_levels(liq_map.get("levels_below", [])),
            "clusters_above": _compact_liquidation_clusters(liq_map.get("clusters_above", [])),
            "clusters_below": _compact_liquidation_clusters(liq_map.get("clusters_below", [])),
            "market_metrics": _compact_number_map(
                liq_metrics,
                (
                    "basis_bps",
                    "last_funding_rate_bps",
                    "open_interest_change_pct",
                    "taker_buy_sell_ratio",
                    "global_long_short_ratio",
                    "top_trader_account_ratio",
                    "top_trader_position_ratio",
                    "order_book_imbalance",
                    "recent_volume",
                    "realized_volatility_bps",
                ),
            ),
            "assumptions": _compact_number_map(
                liq_assumptions,
                (
                    "open_interest_change_pct",
                    "price_return_pct",
                    "funding_rate_bps",
                    "inferred_long_crowding",
                    "inferred_short_crowding",
                ),
            ),
        },
        "behavior": {
            "mode": behavior.get("mode", ""),
            "bias": behavior.get("bias", ""),
            "regime": behavior.get("regime", ""),
            "bias_strength": _round(behavior.get("bias_strength", 0.0), 3),
            "breakdown": behavior.get("breakdown", {}),
        },
        "features": {
            "spread_bps": _round(features.get("spread_bps", 0.0), 3),
            "buy_flow_ratio": _round(features.get("buy_flow_ratio", 0.0), 3),
            "short_volatility_bps": _round(features.get("short_volatility_bps", 0.0), 3),
            "volume_zscore": _round(features.get("volume_zscore", 0.0), 3),
            "funding_rate_bps": _round(features.get("funding_rate_bps", 0.0), 3),
            "basis_bps": _round(features.get("basis_bps", 0.0), 3),
            "open_interest_change_pct": _round(features.get("open_interest_change_pct", 0.0), 3),
            "htf_regime": features.get("htf_regime", ""),
        },
        "scoring": {
            "raw_score": _round(scoring.get("raw_score", 0.0), 3),
            "confidence": _round(scoring.get("confidence", 0.0), 3),
            "alignment_bonus": _round(scoring.get("alignment_bonus", 0.0), 3),
            "conflict_penalty": _round(scoring.get("conflict_penalty", 0.0), 3),
        },
        "risk": {
            "stop_bps": _round(risk.get("stop_bps", 0.0), 3),
            "tp_bps": _round(risk.get("tp_bps", 0.0), 3),
            "risk_reward": _round(risk.get("risk_reward", 0.0), 3),
        },
        "ai": {
            "entry_verdict": ai_decision.get("entry_verdict", ""),
            "exit_action": ai_decision.get("exit_action", ""),
            "risk_flags": ai_decision.get("risk_flags", []),
        },
        "whale": {
            "impulse_bps_5m": _round(whale_context.get("impulse_bps_5m", 0.0), 3),
            "volume_ratio_5m": _round(whale_context.get("volume_ratio_5m", 0.0), 3),
            "open_interest_acceleration": _round(whale_context.get("open_interest_acceleration", 0.0), 3),
            "top_trader_account_divergence": _round(whale_context.get("top_trader_account_divergence", 0.0), 3),
            "top_trader_position_divergence": _round(whale_context.get("top_trader_position_divergence", 0.0), 3),
            "taker_aggression_score": _round(whale_context.get("taker_aggression_score", 0.0), 3),
            "squeeze_risk": whale_context.get("squeeze_risk", "NEUTRAL"),
            "absorption_hint": whale_context.get("absorption_hint", "NONE"),
            "crowding_bias": whale_context.get("crowding_bias", "NEUTRAL"),
        },
        "structure": {
            "range_position_30m": _round(structure_context.get("range_position_30m", 0.5), 3),
            "range_width_pct_30m": _round(structure_context.get("range_width_pct_30m", 0.0), 3),
            "compression_ratio": _round(structure_context.get("compression_ratio", 1.0), 3),
            "breakout_state": structure_context.get("breakout_state", "UNKNOWN"),
            "trend_persistence_1m": _round(structure_context.get("trend_persistence_1m", 0.0), 3),
            "wick_skew_last_1m": _round(structure_context.get("wick_skew_last_1m", 0.0), 3),
            "distance_from_range_mid_pct": _round(structure_context.get("distance_from_range_mid_pct", 0.0), 3),
            "fakeout_risk": structure_context.get("fakeout_risk", "UNKNOWN"),
        },
        "session": {
            "utc_hour": int(session_context.get("utc_hour", 0) or 0),
            "weekday": int(session_context.get("weekday", 0) or 0),
            "session": session_context.get("session", "UNKNOWN"),
            "session_type": session_context.get("session_type", "UNKNOWN"),
        },
        "data_quality": {
            "oi_rows": int(data_quality_context.get("oi_rows", 0) or 0),
            "funding_rows": int(data_quality_context.get("funding_rows", 0) or 0),
            "basis_rows": int(data_quality_context.get("basis_rows", 0) or 0),
            "taker_rows": int(data_quality_context.get("taker_rows", 0) or 0),
            "global_ratio_rows": int(data_quality_context.get("global_ratio_rows", 0) or 0),
            "top_account_rows": int(data_quality_context.get("top_account_rows", 0) or 0),
            "top_position_rows": int(data_quality_context.get("top_position_rows", 0) or 0),
            "positioning_complete": bool(data_quality_context.get("positioning_complete", False)),
            "derivatives_complete": bool(data_quality_context.get("derivatives_complete", False)),
            "used_liquidation_snapshot": bool(data_quality_context.get("used_liquidation_snapshot", False)),
            "coverage_score": _round(data_quality_context.get("coverage_score", 0.0), 3),
        },
        "timeframes": tf_payload,
    }


def _assistant_completion(bundle: TrainingExampleBundle) -> str:
    label = bundle.label
    # Keep the supervised target to information available at inference time.  The
    # previous completion also contained realized future prices and excursions;
    # that made a small model spend capacity generating unknowable values instead
    # of learning the three-way classification task.
    return json.dumps(
        {
            "prediction": label.get("label_action", Direction.FLAT.value),
            "horizon_minutes": int(label.get("horizon_minutes", 0) or 0),
        },
        separators=(",", ":"),
    )


def chronological_split_examples(
    examples: list[LoraTrainingExample],
    *,
    train_fraction: float = 0.70,
    validation_fraction: float = 0.15,
) -> list[LoraTrainingExample]:
    """Assign leakage-resistant splits ordered by event time.

    Dense market snapshots are strongly autocorrelated, so hashing snapshot ids
    puts near-duplicates on both sides of an evaluation boundary.  Ordering by
    event time makes validation and test results representative of deployment.
    """
    if not examples:
        return examples
    ordered = sorted(
        examples,
        key=lambda row: (int(row.metadata.get("event_ts") or 0), row.snapshot_id),
    )
    count = len(ordered)
    train_end = max(1, int(count * train_fraction))
    validation_end = max(train_end, int(count * (train_fraction + validation_fraction)))
    if count >= 3:
        train_end = min(train_end, count - 2)
        validation_end = min(max(validation_end, train_end + 1), count - 1)
    for index, row in enumerate(ordered):
        row.split = "train" if index < train_end else "validation" if index < validation_end else "test"
    return ordered


def build_lora_example(bundle: TrainingExampleBundle) -> LoraTrainingExample:
    view = _snapshot_view(bundle)
    prompt = json.dumps(view, separators=(",", ":"), sort_keys=True)
    completion = _assistant_completion(bundle)
    return LoraTrainingExample(
        snapshot_id=str(bundle.snapshot.get("snapshot_id", "")),
        symbol=str(bundle.snapshot.get("symbol", "")),
        horizon_minutes=int(bundle.label.get("horizon_minutes", 0) or 0),
        label_action=Direction(str(bundle.label.get("label_action", Direction.FLAT.value))),
        decision_action=str(bundle.decision.get("decision_action", "")),
        took_trade=bool(bundle.decision.get("took_trade", False)),
        split="train",
        prompt=prompt,
        completion=completion,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": completion},
        ],
        metadata={
            "event_ts": bundle.snapshot.get("event_ts"),
            "signal_confidence": bundle.snapshot.get("signal_confidence"),
            "signal_quality": bundle.snapshot.get("signal_quality"),
            "mode": bundle.decision.get("mode"),
            "ai_verdict": bundle.decision.get("ai_verdict"),
        },
    )


def rebalance_examples(
    examples: list[LoraTrainingExample],
    *,
    balance_mode: str = "none",
) -> list[LoraTrainingExample]:
    normalized = balance_mode.strip().lower()
    if normalized in {"", "none"}:
        return examples

    buckets: dict[Direction, list[LoraTrainingExample]] = {
        Direction.LONG: [],
        Direction.SHORT: [],
        Direction.FLAT: [],
    }
    for row in examples:
        buckets[row.label_action].append(row)

    for key in buckets:
        buckets[key].sort(key=lambda item: (item.snapshot_id, item.horizon_minutes, item.decision_action))

    non_empty = [rows for rows in buckets.values() if rows]
    if not non_empty:
        return examples

    if normalized == "undersample_majority":
        target = min(len(rows) for rows in non_empty)
        selected = [row for rows in buckets.values() for row in rows[:target]]
    elif normalized == "drop_flat":
        target = min(len(buckets[Direction.LONG]), len(buckets[Direction.SHORT])) if buckets[Direction.LONG] and buckets[Direction.SHORT] else 0
        selected = buckets[Direction.LONG][:target] + buckets[Direction.SHORT][:target]
    elif normalized == "cap_flat":
        directional_target = max(len(buckets[Direction.LONG]), len(buckets[Direction.SHORT]), 1)
        flat_cap = min(len(buckets[Direction.FLAT]), directional_target)
        selected = (
            buckets[Direction.LONG]
            + buckets[Direction.SHORT]
            + buckets[Direction.FLAT][:flat_cap]
        )
    else:
        raise ValueError(f"Unsupported balance mode: {balance_mode}")

    selected.sort(key=lambda item: (item.split, item.snapshot_id, item.horizon_minutes))
    return selected


def rebalance_examples_by_split(
    examples: list[LoraTrainingExample],
    *,
    balance_mode: str = "none",
) -> list[LoraTrainingExample]:
    """Balance each chronological partition without moving rows across time."""
    selected: list[LoraTrainingExample] = []
    for split in ("train", "validation", "test"):
        partition = [row for row in examples if row.split == split]
        selected.extend(rebalance_examples(partition, balance_mode=balance_mode))
    selected.sort(
        key=lambda row: (
            {"train": 0, "validation": 1, "test": 2}.get(row.split, 3),
            int(row.metadata.get("event_ts") or 0),
            row.snapshot_id,
        )
    )
    return selected


def class_weights(examples: list[LoraTrainingExample]) -> dict[str, float]:
    counts = {
        Direction.LONG.value: 0,
        Direction.SHORT.value: 0,
        Direction.FLAT.value: 0,
    }
    for row in examples:
        counts[row.label_action.value] += 1

    non_zero = [count for count in counts.values() if count > 0]
    if not non_zero:
        return {label: 1.0 for label in counts}

    total = sum(non_zero)
    classes = len(non_zero)
    weights: dict[str, float] = {}
    for label, count in counts.items():
        if count <= 0:
            weights[label] = 0.0
        else:
            weights[label] = round(total / (classes * count), 4)
    return weights
