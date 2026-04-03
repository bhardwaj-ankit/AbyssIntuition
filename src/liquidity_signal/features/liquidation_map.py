from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from liquidity_signal.models import (
    Direction,
    LiquidationAssumptions,
    LiquidationDataQuality,
    LiquidationEventPoint,
    LiquidationCluster,
    LiquidationLevel,
    LiquidationMapAdvancedResponse,
    LiquidationMapResponse,
)


def _safe_float(v: Any) -> float:
    return float(v)


def _short_volatility_bps(klines: list[list[Any]], mark_price: float) -> float:
    if len(klines) < 2 or mark_price <= 0.0:
        return 0.0

    closes = [_safe_float(row[4]) for row in klines]
    diffs = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    mean_abs_move = sum(diffs) / len(diffs)
    return (mean_abs_move / mark_price) * 10_000


def _average_funding_bps(funding_rates: list[dict[str, Any]] | None) -> float:
    if not funding_rates:
        return 0.0

    samples: list[float] = []
    for row in funding_rates:
        raw_rate = row.get("fundingRate")
        if raw_rate is None:
            continue
        try:
            samples.append(float(raw_rate) * 10_000)
        except (TypeError, ValueError):
            continue

    if not samples:
        return 0.0
    return sum(samples) / len(samples)


def _cluster_levels(
    levels: list[LiquidationLevel],
    mark_price: float,
    cluster_step_bps: float,
) -> list[LiquidationCluster]:
    if mark_price <= 0.0 or cluster_step_bps <= 0.0:
        return []

    bucketed: dict[int, list[LiquidationLevel]] = {}
    for level in levels:
        rel_bps = ((level.level_price - mark_price) / mark_price) * 10_000
        bucket = int(round(rel_bps / cluster_step_bps))
        bucketed.setdefault(bucket, []).append(level)

    clusters: list[LiquidationCluster] = []
    for bucket, rows in sorted(bucketed.items()):
        prices = [row.level_price for row in rows]
        strengths = [row.strength for row in rows]
        side = rows[0].side

        center_price = mark_price * (1.0 + (bucket * cluster_step_bps) / 10_000)
        clusters.append(
            LiquidationCluster(
                side=side,
                center_price=center_price,
                min_price=min(prices),
                max_price=max(prices),
                total_strength=sum(strengths),
                contributing_levels=len(rows),
            )
        )
    return clusters


def build_liquidation_map_estimate(
    symbol: str,
    mark_price: float,
    oi_hist: list[dict[str, Any]],
    klines: list[list[Any]],
    funding_rates: list[dict[str, Any]] | None = None,
) -> LiquidationMapResponse:
    """Estimate liquidation magnet zones from Binance-only data.

    This is not exchange-native liquidation heatmap data. It approximates potential
    liquidation zones using open-interest trend, short-term price trend, and
    leverage-distance assumptions.
    """

    short_vol_bps = _short_volatility_bps(klines, mark_price)
    price_ret = 0.0
    if len(klines) >= 2:
        first_close = _safe_float(klines[0][4])
        last_close = _safe_float(klines[-1][4])
        if first_close > 0.0:
            price_ret = (last_close - first_close) / first_close

    oi_change = 0.0
    oi_last = 0.0
    if len(oi_hist) >= 2:
        oi_first = _safe_float(oi_hist[0]["sumOpenInterestValue"])
        oi_last = _safe_float(oi_hist[-1]["sumOpenInterestValue"])
        if oi_first > 0.0:
            oi_change = (oi_last - oi_first) / oi_first
    elif oi_hist:
        oi_last = _safe_float(oi_hist[-1]["sumOpenInterestValue"])

    oi_build_up = max(oi_change, 0.0)
    funding_bps = _average_funding_bps(funding_rates)
    crowding_bias = math.tanh((price_ret * 30.0) + (funding_bps / 8.0))
    long_share = 0.5 + 0.5 * crowding_bias
    short_share = 1.0 - long_share
    long_crowding = oi_build_up * long_share
    short_crowding = oi_build_up * short_share

    vol_floor = max(short_vol_bps, 5.0)
    leverage_buckets = [10, 25, 50, 75, 100]
    leverage_weights = {10: 0.18, 25: 0.24, 50: 0.26, 75: 0.18, 100: 0.14}
    maintenance_margin_rate = 0.005
    cluster_step_bps = max(vol_floor * 2.0, 25.0)

    levels_above: list[LiquidationLevel] = []
    levels_below: list[LiquidationLevel] = []

    for lev in leverage_buckets:
        liq_pct = max((1.0 / lev) - maintenance_margin_rate, vol_floor / 10_000)
        dist_bps = liq_pct * 10_000

        price_up = mark_price * (1.0 + dist_bps / 10_000)
        price_down = mark_price * (1.0 - dist_bps / 10_000)

        weight = leverage_weights[lev]
        distance_decay = 1.0 / (1.0 + (dist_bps / 350.0))
        notional_scale = max(oi_last / 1e9, 0.1)
        up_strength = max(short_crowding * weight * distance_decay * notional_scale, 0.0)
        down_strength = max(long_crowding * weight * distance_decay * notional_scale, 0.0)

        levels_above.append(
            LiquidationLevel(
                side=Direction.SHORT,
                leverage=lev,
                level_price=price_up,
                distance_bps=dist_bps,
                strength=up_strength,
            )
        )
        levels_below.append(
            LiquidationLevel(
                side=Direction.LONG,
                leverage=lev,
                level_price=price_down,
                distance_bps=dist_bps,
                strength=down_strength,
            )
        )

    clusters_above = _cluster_levels(levels_above, mark_price, cluster_step_bps)
    clusters_below = _cluster_levels(levels_below, mark_price, cluster_step_bps)

    pull_up = sum(cluster.total_strength for cluster in clusters_above)
    pull_down = sum(cluster.total_strength for cluster in clusters_below)
    denom = pull_up + pull_down
    confidence = 0.0 if denom <= 0.0 else max(pull_up, pull_down) / denom

    dominant_pull = Direction.FLAT
    if pull_up > pull_down:
        dominant_pull = Direction.LONG
    elif pull_down > pull_up:
        dominant_pull = Direction.SHORT

    return LiquidationMapResponse(
        symbol=symbol,
        current_price=mark_price,
        is_estimated=True,
        methodology="Binance-only estimated liquidation map (not exchange liquidation feed)",
        dominant_pull=dominant_pull,
        confidence=confidence,
        levels_above=levels_above,
        levels_below=levels_below,
        clusters_above=clusters_above,
        clusters_below=clusters_below,
        assumptions=LiquidationAssumptions(
            leverage_buckets=leverage_buckets,
            leverage_weights=leverage_weights,
            maintenance_margin_rate=maintenance_margin_rate,
            cluster_step_bps=cluster_step_bps,
            open_interest_change_pct=oi_change * 100.0,
            price_return_pct=price_ret * 100.0,
            funding_rate_bps=funding_bps,
            inferred_long_crowding=long_crowding,
            inferred_short_crowding=short_crowding,
        ),
        meta={
            "open_interest_change_pct": oi_change * 100.0,
            "price_return_pct": price_ret * 100.0,
            "funding_rate_bps": funding_bps,
            "short_volatility_bps": short_vol_bps,
            "long_share": long_share,
            "short_share": short_share,
            "pull_up": pull_up,
            "pull_down": pull_down,
        },
    )


def _coerce_bybit_event_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            return [row for row in payload["data"] if isinstance(row, dict)]
        return [payload]
    return []


def _normalize_bybit_event(event: dict[str, Any]) -> LiquidationEventPoint | None:
    side_raw = str(event.get("side", event.get("S", ""))).upper()
    if side_raw in {"BUY", "LONG"}:
        side = Direction.LONG
    elif side_raw in {"SELL", "SHORT"}:
        side = Direction.SHORT
    else:
        return None

    price_raw = event.get("price", event.get("p"))
    qty_raw = event.get("size", event.get("qty", event.get("v")))
    ts_raw = event.get("updatedTime", event.get("time", event.get("timestamp", event.get("T"))))

    if price_raw is None or qty_raw is None or ts_raw is None:
        return None

    try:
        return LiquidationEventPoint(
            source="bybit",
            side=side,
            price=float(price_raw),
            quantity=float(qty_raw),
            timestamp=int(ts_raw),
        )
    except (TypeError, ValueError):
        return None


def _dedupe_events(events: list[LiquidationEventPoint]) -> list[LiquidationEventPoint]:
    seen: set[tuple[str, str, float, float, int]] = set()
    unique: list[LiquidationEventPoint] = []

    for event in sorted(events, key=lambda row: row.timestamp):
        signature = (
            event.source,
            event.side.value,
            round(event.price, 4),
            round(event.quantity, 8),
            int(event.timestamp // 1_000),
        )
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(event)

    return unique


def build_liquidation_map_advanced(
    symbol: str,
    mark_price: float,
    oi_hist: list[dict[str, Any]],
    klines: list[list[Any]],
    funding_rates: list[dict[str, Any]] | None = None,
    bybit_events_raw: Any | None = None,
    include_events: bool = True,
    bybit_fetched_at_ms: int | None = None,
    degraded_reason: str | None = None,
) -> LiquidationMapAdvancedResponse:
    base = build_liquidation_map_estimate(
        symbol=symbol,
        mark_price=mark_price,
        oi_hist=oi_hist,
        klines=klines,
        funding_rates=funding_rates,
    )

    notes: list[str] = ["Binance estimate baseline active"]
    bybit_available = bybit_events_raw is not None
    events: list[LiquidationEventPoint] = []
    event_up_pressure = 0.0
    event_down_pressure = 0.0

    if include_events and bybit_events_raw:
        for raw in _coerce_bybit_event_rows(bybit_events_raw):
            event = _normalize_bybit_event(raw)
            if event is None:
                continue
            events.append(event)
        events = _dedupe_events(events)
        for event in events:
            event_strength = event.quantity * max(event.price, 0.0)
            if event.side == Direction.SHORT:
                event_up_pressure += event_strength
            elif event.side == Direction.LONG:
                event_down_pressure += event_strength

    estimate_up = base.meta.get("pull_up", 0.0)
    estimate_down = base.meta.get("pull_down", 0.0)

    event_total = event_up_pressure + event_down_pressure
    event_scale = 0.0
    if event_total > 0.0:
        event_scale = max(estimate_up + estimate_down, 1e-9) / event_total

    normalized_event_up = event_up_pressure * event_scale
    normalized_event_down = event_down_pressure * event_scale
    generated_at = int(datetime.now(timezone.utc).timestamp() * 1000)
    source_age_ms: int | None = None
    if include_events and bybit_fetched_at_ms is not None:
        source_age_ms = max(generated_at - bybit_fetched_at_ms, 0)

    if bybit_available and include_events:
        if events:
            notes.append("Bybit liquidation event overlay active")
            event_coverage = min(len(events) / 12.0, 1.0)
            freshness_factor = 1.0 if source_age_ms is None else min(1.0, 15_000 / max(source_age_ms, 15_000))
            event_weight = 0.35 * event_coverage * freshness_factor
            estimate_weight = 1.0 - event_weight
            pull_up = estimate_up * estimate_weight + normalized_event_up * event_weight
            pull_down = estimate_down * estimate_weight + normalized_event_down * event_weight
            meta = dict(base.meta)
            meta["event_up_pressure"] = event_up_pressure
            meta["event_down_pressure"] = event_down_pressure
            meta["event_count"] = float(len(events))
            meta["event_weight_applied"] = event_weight
            if source_age_ms is not None and source_age_ms > 15_000:
                notes.append("Bybit event overlay stale; weight reduced")
        else:
            notes.append("Bybit available but returned no usable liquidation events")
            event_weight = 0.0
            estimate_weight = 1.0
            pull_up = estimate_up
            pull_down = estimate_down
            meta = dict(base.meta)
            meta["event_count"] = 0.0
            meta["event_weight_applied"] = 0.0
    elif include_events:
        fallback_reason = degraded_reason or "Bybit unavailable, fallback to estimate-only mode"
        notes.append(fallback_reason)
        event_weight = 0.0
        estimate_weight = 1.0
        pull_up = estimate_up
        pull_down = estimate_down
        meta = dict(base.meta)
        meta["event_count"] = 0.0
        meta["event_weight_applied"] = 0.0
    else:
        notes.append("Event overlay disabled by request")
        event_weight = 0.0
        estimate_weight = 1.0
        pull_up = estimate_up
        pull_down = estimate_down
        meta = dict(base.meta)
        meta["event_count"] = 0.0
        meta["event_weight_applied"] = 0.0

    denom = pull_up + pull_down
    confidence = 0.0 if denom <= 0.0 else max(pull_up, pull_down) / denom

    dominant_pull = Direction.FLAT
    if pull_up > pull_down:
        dominant_pull = Direction.LONG
    elif pull_down > pull_up:
        dominant_pull = Direction.SHORT

    methodology = "Binance estimated liquidation map + optional Bybit liquidation event overlay"
    overlay_active = include_events and event_weight > 0.0 and bool(events)
    quality_degraded = include_events and not overlay_active
    if quality_degraded and degraded_reason is None and bybit_available and not events:
        degraded_reason = "Bybit returned no usable liquidation events"

    if overlay_active:
        source = "binance_estimate+bybit_events"
    elif include_events:
        source = "binance_estimate_fallback"
    else:
        source = "binance_estimate"

    return LiquidationMapAdvancedResponse(
        symbol=symbol,
        current_price=mark_price,
        dominant_pull=dominant_pull,
        confidence=confidence,
        methodology=methodology,
        source=source,
        generated_at=generated_at,
        levels_above=base.levels_above,
        levels_below=base.levels_below,
        clusters_above=base.clusters_above,
        clusters_below=base.clusters_below,
        assumptions=base.assumptions,
        quality=LiquidationDataQuality(
            estimate_weight=estimate_weight,
            event_weight=event_weight,
            bybit_available=bybit_available,
            event_overlay_active=overlay_active,
            events_used=len(events),
            degraded_mode=quality_degraded,
            source_age_ms=source_age_ms,
            degraded_reason=degraded_reason,
            notes=notes,
        ),
        events=events,
        meta=meta,
    )
