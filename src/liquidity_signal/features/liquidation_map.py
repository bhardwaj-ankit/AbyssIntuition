from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from liquidity_signal.models import (
    LiquidationCalibrationProfile,
    Direction,
    LiquidationAssumptions,
    LiquidationCluster,
    LiquidationDataQuality,
    LiquidationDepthBand,
    LiquidationEventPoint,
    LiquidationEventsSummary,
    LiquidationHeatmapSlice,
    LiquidationLevel,
    LiquidationMapAdvancedResponse,
    LiquidationMapResponse,
    LiquidationMarketMetrics,
    LiquidationStorageStats,
    LiquidationStreamHealth,
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def _extract_timestamp(row: dict[str, Any]) -> int:
    for key in ("timestamp", "fundingTime", "time", "T", "ts"):
        if key in row:
            return _safe_int(row[key])
    return 0


def _nearest_row(rows: list[dict[str, Any]] | None, target_ts: int) -> dict[str, Any] | None:
    if not rows:
        return None
    return min(rows, key=lambda row: abs(_extract_timestamp(row) - target_ts))


def _short_volatility_bps(klines: list[list[Any]], mark_price: float) -> float:
    if len(klines) < 2 or mark_price <= 0.0:
        return 0.0

    closes = [_safe_float(row[4]) for row in klines]
    diffs = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    mean_abs_move = sum(diffs) / len(diffs)
    return (mean_abs_move / mark_price) * 10_000


def _price_return_pct(klines: list[list[Any]]) -> float:
    if len(klines) < 2:
        return 0.0
    first_close = _safe_float(klines[0][4])
    last_close = _safe_float(klines[-1][4])
    if first_close <= 0.0:
        return 0.0
    return ((last_close - first_close) / first_close) * 100.0


def _recent_volume(klines: list[list[Any]]) -> float:
    return sum(_safe_float(row[5]) for row in klines[-20:])


def _open_interest_stats(oi_hist: list[dict[str, Any]]) -> tuple[float, float]:
    if not oi_hist:
        return 0.0, 0.0
    last = _safe_float(oi_hist[-1].get("sumOpenInterestValue"))
    if len(oi_hist) < 2:
        return last, 0.0
    first = _safe_float(oi_hist[0].get("sumOpenInterestValue"))
    if first <= 0.0:
        return last, 0.0
    return last, ((last - first) / first) * 100.0


def _average_funding_bps(funding_rates: list[dict[str, Any]] | None) -> float:
    if not funding_rates:
        return 0.0
    values = [_safe_float(row.get("fundingRate")) * 10_000 for row in funding_rates[-6:]]
    return sum(values) / len(values) if values else 0.0


def _latest_ratio(rows: list[dict[str, Any]] | None, field: str = "longShortRatio", default: float = 1.0) -> float:
    if not rows:
        return default
    return _safe_float(rows[-1].get(field), default)


def _latest_taker_ratio(rows: list[dict[str, Any]] | None) -> float:
    if not rows:
        return 1.0
    row = rows[-1]
    ratio = _safe_float(row.get("buySellRatio"))
    if ratio > 0.0:
        return ratio
    buy = _safe_float(row.get("buyVol"))
    sell = _safe_float(row.get("sellVol"))
    if sell <= 0.0:
        return 1.0
    return buy / sell


def _latest_basis_bps(rows: list[dict[str, Any]] | None, mark_price: float) -> float:
    if not rows:
        return 0.0
    row = rows[-1]
    basis_rate = _safe_float(row.get("basisRate"))
    if basis_rate != 0.0:
        return basis_rate * 10_000
    basis = _safe_float(row.get("basis"))
    if mark_price <= 0.0:
        return 0.0
    return (basis / mark_price) * 10_000


def _cluster_levels(levels: list[LiquidationLevel], mark_price: float, cluster_step_bps: float) -> list[LiquidationCluster]:
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
        clusters.append(
            LiquidationCluster(
                side=rows[0].side,
                center_price=mark_price * (1.0 + (bucket * cluster_step_bps) / 10_000),
                min_price=min(prices),
                max_price=max(prices),
                total_strength=sum(row.strength for row in rows),
                contributing_levels=len(rows),
            )
        )
    return clusters


def _normalize_legacy_event(raw: dict[str, Any], symbol: str) -> LiquidationEventPoint | None:
    side_raw = str(raw.get("side", raw.get("S", ""))).upper()
    if side_raw == "BUY":
        liquidated_side = Direction.LONG
    elif side_raw == "SELL":
        liquidated_side = Direction.SHORT
    else:
        return None

    price = _safe_float(raw.get("price", raw.get("p")))
    qty = _safe_float(raw.get("size", raw.get("qty", raw.get("v"))))
    timestamp = _safe_int(raw.get("updatedTime", raw.get("timestamp", raw.get("time", raw.get("T")))))
    event_symbol = str(raw.get("symbol", raw.get("s", symbol))).upper()
    if price <= 0.0 or qty <= 0.0 or timestamp <= 0:
        return None

    return LiquidationEventPoint(
        source=str(raw.get("source", "bybit")),
        symbol=event_symbol,
        liquidated_side=liquidated_side,
        execution_side="SELL" if liquidated_side == Direction.LONG else "BUY",
        price=price,
        quantity=qty,
        notional=price * qty,
        timestamp=timestamp,
        received_at=_safe_int(raw.get("received_at")),
        exchange_event_id=str(raw.get("exchange_event_id", "")) or None,
    )


def _coerce_events(
    symbol: str,
    live_events: list[LiquidationEventPoint] | None = None,
    legacy_events: Any | None = None,
) -> list[LiquidationEventPoint]:
    if live_events is not None:
        return sorted(live_events, key=lambda row: row.timestamp)

    if legacy_events is None:
        return []

    rows: list[LiquidationEventPoint] = []
    raw_rows: list[dict[str, Any]]
    if isinstance(legacy_events, list):
        raw_rows = [row for row in legacy_events if isinstance(row, dict)]
    elif isinstance(legacy_events, dict) and isinstance(legacy_events.get("data"), list):
        raw_rows = [row for row in legacy_events["data"] if isinstance(row, dict)]
    elif isinstance(legacy_events, dict):
        raw_rows = [legacy_events]
    else:
        raw_rows = []

    for row in raw_rows:
        event = _normalize_legacy_event(row, symbol)
        if event is not None:
            rows.append(event)

    seen: set[str] = set()
    deduped: list[LiquidationEventPoint] = []
    for event in sorted(rows, key=lambda item: item.timestamp):
        signature = event.exchange_event_id or (
            f"{event.source}:{event.symbol}:{event.timestamp}:{event.liquidated_side.value}:{event.quantity}:{event.price}"
        )
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(event)
    return deduped


def _build_depth_bands(order_book: dict[str, Any] | None, mark_price: float, max_bands: int = 12) -> list[LiquidationDepthBand]:
    if not order_book or mark_price <= 0.0:
        return []

    bands: list[LiquidationDepthBand] = []
    for side_name, key in (("BID", "bids"), ("ASK", "asks")):
        cumulative = 0.0
        for row in order_book.get(key, [])[: max_bands // 2]:
            price = _safe_float(row[0])
            qty = _safe_float(row[1])
            if price <= 0.0 or qty <= 0.0:
                continue
            cumulative += price * qty
            distance_bps = abs((price - mark_price) / mark_price) * 10_000
            bands.append(
                LiquidationDepthBand(
                    side=side_name,
                    price=price,
                    cumulative_notional=cumulative,
                    distance_bps=distance_bps,
                )
            )
    bands.sort(key=lambda row: row.distance_bps)
    return bands[:max_bands]


def _order_book_imbalance(order_book: dict[str, Any] | None) -> float:
    if not order_book:
        return 0.0
    bid_notional = sum(_safe_float(row[0]) * _safe_float(row[1]) for row in order_book.get("bids", [])[:20])
    ask_notional = sum(_safe_float(row[0]) * _safe_float(row[1]) for row in order_book.get("asks", [])[:20])
    total = bid_notional + ask_notional
    if total <= 0.0:
        return 0.0
    return (bid_notional - ask_notional) / total


def _dynamic_leverage_weights(volatility_bps: float, crowding_bias: float) -> dict[int, float]:
    base = {5: 0.16, 10: 0.22, 25: 0.24, 50: 0.22, 100: 0.16}
    high_leverage_shift = _clamp((25.0 - volatility_bps) / 80.0 + abs(crowding_bias) * 0.15, -0.12, 0.18)
    adjusted = {
        5: base[5] - high_leverage_shift * 0.45,
        10: base[10] - high_leverage_shift * 0.20,
        25: base[25],
        50: base[50] + high_leverage_shift * 0.30,
        100: base[100] + high_leverage_shift * 0.35,
    }
    total = sum(max(value, 0.02) for value in adjusted.values())
    return {key: max(value, 0.02) / total for key, value in adjusted.items()}


def _price_levels(mark_price: float, range_pct: float, resolution: int) -> list[float]:
    low = mark_price * (1.0 - range_pct / 100.0)
    high = mark_price * (1.0 + range_pct / 100.0)
    if resolution <= 1:
        return [mark_price]
    step = (high - low) / (resolution - 1)
    return [low + step * idx for idx in range(resolution)]


def _nearest_index(levels: list[float], price: float) -> int:
    return min(range(len(levels)), key=lambda idx: abs(levels[idx] - price))


def _build_entry_profiles(
    klines: list[list[Any]],
    levels: list[float],
) -> tuple[list[float], list[float]]:
    long_profile = [0.0 for _ in levels]
    short_profile = [0.0 for _ in levels]
    if not klines or not levels:
        return long_profile, short_profile

    for idx, row in enumerate(klines):
        open_price = _safe_float(row[1])
        high = _safe_float(row[2])
        low = _safe_float(row[3])
        close = _safe_float(row[4])
        volume = _safe_float(row[5])
        if volume <= 0.0:
            continue

        recency = 0.35 + 0.65 * ((idx + 1) / len(klines))
        directional_bias = 1.0 if close >= open_price else -1.0
        low_idx = _nearest_index(levels, low)
        high_idx = _nearest_index(levels, high)
        if low_idx > high_idx:
            low_idx, high_idx = high_idx, low_idx

        width = max(high_idx - low_idx + 1, 1)
        base_weight = (volume * recency) / width
        for bin_idx in range(low_idx, high_idx + 1):
            level_price = levels[bin_idx]
            proximity_to_close = 1.0 / (1.0 + abs(level_price - close) / max(close, 1e-9) * 80.0)
            long_profile[bin_idx] += base_weight * (1.12 if directional_bias >= 0.0 else 0.88) * proximity_to_close
            short_profile[bin_idx] += base_weight * (1.12 if directional_bias <= 0.0 else 0.88) * proximity_to_close

    long_total = sum(long_profile) or 1.0
    short_total = sum(short_profile) or 1.0
    return ([value / long_total for value in long_profile], [value / short_total for value in short_profile])


def _compute_side_share(
    price_return_pct: float,
    oi_change_pct: float,
    funding_bps: float,
    basis_bps: float,
    taker_ratio: float,
    global_ratio: float,
    top_account_ratio: float,
    top_position_ratio: float,
) -> tuple[float, float, float]:
    regime_score = 0.0
    if price_return_pct > 0.0 and oi_change_pct > 0.0:
        regime_score += 0.55
    elif price_return_pct < 0.0 and oi_change_pct > 0.0:
        regime_score -= 0.55
    elif price_return_pct > 0.0 and oi_change_pct < 0.0:
        regime_score += 0.15
    elif price_return_pct < 0.0 and oi_change_pct < 0.0:
        regime_score -= 0.15

    crowding_bias = (
        _clamp(price_return_pct / 2.5, -1.4, 1.4)
        + regime_score
        + _clamp(funding_bps / 8.0, -1.2, 1.2)
        + _clamp(basis_bps / 12.0, -1.2, 1.2)
        + _clamp(math.log(max(taker_ratio, 1e-6)), -1.1, 1.1)
        + _clamp(math.log(max(global_ratio, 1e-6)), -1.0, 1.0)
        + _clamp(math.log(max(top_account_ratio, 1e-6)), -1.0, 1.0)
        + _clamp(math.log(max(top_position_ratio, 1e-6)), -1.0, 1.0)
    )
    long_share = _sigmoid(crowding_bias)
    return long_share, 1.0 - long_share, crowding_bias


def _surface_from_snapshot(
    *,
    mark_price: float,
    klines: list[list[Any]],
    oi_hist: list[dict[str, Any]],
    funding_rates: list[dict[str, Any]] | None,
    basis_rows: list[dict[str, Any]] | None,
    taker_rows: list[dict[str, Any]] | None,
    global_ratio_rows: list[dict[str, Any]] | None,
    top_account_rows: list[dict[str, Any]] | None,
    top_position_rows: list[dict[str, Any]] | None,
    price_levels: list[float],
    order_book: dict[str, Any] | None = None,
    calibration: dict[str, float] | None = None,
) -> dict[str, Any]:
    volatility_bps = max(_short_volatility_bps(klines, mark_price), 5.0)
    price_return_pct = _price_return_pct(klines)
    open_interest_value, oi_change_pct = _open_interest_stats(oi_hist)
    funding_bps = _average_funding_bps(funding_rates)
    basis_bps = _latest_basis_bps(basis_rows, mark_price)
    taker_ratio = _latest_taker_ratio(taker_rows)
    global_ratio = _latest_ratio(global_ratio_rows)
    top_account_ratio = _latest_ratio(top_account_rows)
    top_position_ratio = _latest_ratio(top_position_rows)
    long_share, short_share, crowding_bias = _compute_side_share(
        price_return_pct,
        oi_change_pct,
        funding_bps,
        basis_bps,
        taker_ratio,
        global_ratio,
        top_account_ratio,
        top_position_ratio,
    )

    leverage_weights = _dynamic_leverage_weights(volatility_bps, crowding_bias)
    maintenance_margin_rate = 0.005
    notional_scale = max(open_interest_value / 1e9, 0.15)
    activity_scale = max(0.08, min(abs(oi_change_pct) / 100.0 + 0.08, 0.30))
    long_crowding = activity_scale * long_share
    short_crowding = activity_scale * short_share

    calibration = calibration or {}
    intensity_multiplier = calibration.get("intensity_multiplier", 1.0)
    long_bias_multiplier = calibration.get("long_bias_multiplier", 1.0)
    short_bias_multiplier = calibration.get("short_bias_multiplier", 1.0)
    long_crowding *= long_bias_multiplier
    short_crowding *= short_bias_multiplier

    long_profile, short_profile = _build_entry_profiles(klines, price_levels)
    long_surface = [0.0 for _ in price_levels]
    short_surface = [0.0 for _ in price_levels]

    for lev, weight in leverage_weights.items():
        liq_pct = max((1.0 / lev) - maintenance_margin_rate, volatility_bps / 10_000)
        liq_distance_bps = liq_pct * 10_000
        distance_decay = 1.0 / (1.0 + (liq_distance_bps / 250.0))
        for idx, entry_price in enumerate(price_levels):
            liq_long = entry_price * (1.0 - liq_pct)
            liq_short = entry_price * (1.0 + liq_pct)
            long_bin = _nearest_index(price_levels, liq_long)
            short_bin = _nearest_index(price_levels, liq_short)

            long_live_factor = _sigmoid(((mark_price - liq_long) / mark_price) * 35.0)
            short_live_factor = _sigmoid(((liq_short - mark_price) / mark_price) * 35.0)

            long_surface[long_bin] += (
                long_profile[idx]
                * long_crowding
                * weight
                * distance_decay
                * long_live_factor
                * notional_scale
                * intensity_multiplier
            )
            short_surface[short_bin] += (
                short_profile[idx]
                * short_crowding
                * weight
                * distance_decay
                * short_live_factor
                * notional_scale
                * intensity_multiplier
            )

    total_surface = [long_surface[idx] + short_surface[idx] for idx in range(len(price_levels))]
    order_book_imbalance = _order_book_imbalance(order_book)

    return {
        "long_surface": long_surface,
        "short_surface": short_surface,
        "total_surface": total_surface,
        "metrics": {
            "price_return_pct": price_return_pct,
            "open_interest_change_pct": oi_change_pct,
            "funding_rate_bps": funding_bps,
            "basis_bps": basis_bps,
            "taker_buy_sell_ratio": taker_ratio,
            "global_long_short_ratio": global_ratio,
            "top_trader_account_ratio": top_account_ratio,
            "top_trader_position_ratio": top_position_ratio,
            "order_book_imbalance": order_book_imbalance,
            "recent_volume": _recent_volume(klines),
            "realized_volatility_bps": volatility_bps,
            "intensity_multiplier": intensity_multiplier,
        },
        "assumptions": {
            "leverage_weights": leverage_weights,
            "maintenance_margin_rate": maintenance_margin_rate,
            "long_crowding": long_crowding,
            "short_crowding": short_crowding,
        },
    }


def _apply_events_overlay(
    price_levels: list[float],
    long_surface: list[float],
    short_surface: list[float],
    events: list[LiquidationEventPoint],
) -> tuple[list[float], list[float], float]:
    if not events:
        return long_surface, short_surface, 0.0

    total_predicted = sum(long_surface) + sum(short_surface)
    total_event_notional = sum(event.notional for event in events)
    if total_event_notional <= 0.0:
        return long_surface, short_surface, 0.0

    coverage = min(len(events) / 30.0, 1.0)
    event_weight = min(0.35, 0.12 + coverage * 0.23)
    scale = (total_predicted or 1.0) / total_event_notional

    long_out = list(long_surface)
    short_out = list(short_surface)
    for event in events:
        center_idx = _nearest_index(price_levels, event.price)
        target = long_out if event.liquidated_side == Direction.LONG else short_out
        for offset, bump in ((0, 1.0), (-1, 0.45), (1, 0.45), (-2, 0.18), (2, 0.18)):
            idx = center_idx + offset
            if 0 <= idx < len(target):
                target[idx] += event.notional * scale * event_weight * bump

    return long_out, short_out, event_weight


def _select_top_levels(
    price_levels: list[float],
    surface: list[float],
    side: Direction,
    mark_price: float,
    limit: int = 8,
) -> list[LiquidationLevel]:
    candidates: list[LiquidationLevel] = []
    for price, strength in zip(price_levels, surface, strict=False):
        if side == Direction.LONG and price >= mark_price:
            continue
        if side == Direction.SHORT and price <= mark_price:
            continue
        distance_bps = abs((price - mark_price) / mark_price) * 10_000
        approx_lev = min((5, 10, 25, 50, 100), key=lambda lev: abs(distance_bps - ((1 / lev) - 0.005) * 10_000))
        candidates.append(
            LiquidationLevel(
                side=side,
                leverage=int(approx_lev),
                level_price=price,
                distance_bps=distance_bps,
                strength=strength,
            )
        )

    candidates.sort(key=lambda row: row.strength, reverse=True)
    return candidates[:limit]


def _event_summary(events: list[LiquidationEventPoint]) -> LiquidationEventsSummary:
    long_notional = sum(event.notional for event in events if event.liquidated_side == Direction.LONG)
    short_notional = sum(event.notional for event in events if event.liquidated_side == Direction.SHORT)
    latest_event_ts = max((event.timestamp for event in events), default=None)
    return LiquidationEventsSummary(
        total_events=len(events),
        total_notional=long_notional + short_notional,
        long_liquidation_notional=long_notional,
        short_liquidation_notional=short_notional,
        latest_event_ts=latest_event_ts,
    )


def _build_heatmap_history(
    *,
    mark_price: float,
    klines: list[list[Any]],
    oi_hist: list[dict[str, Any]],
    funding_rates: list[dict[str, Any]] | None,
    basis_rows: list[dict[str, Any]] | None,
    taker_rows: list[dict[str, Any]] | None,
    global_ratio_rows: list[dict[str, Any]] | None,
    top_account_rows: list[dict[str, Any]] | None,
    top_position_rows: list[dict[str, Any]] | None,
    price_levels: list[float],
    history_points: int,
) -> list[LiquidationHeatmapSlice]:
    if not klines:
        return []

    history_points = max(1, min(history_points, len(klines)))
    step = max(1, len(klines) // history_points)
    slices: list[LiquidationHeatmapSlice] = []
    for idx in range(step - 1, len(klines), step):
        subset = klines[: idx + 1]
        ts = _safe_int(subset[-1][0])
        mark = _safe_float(subset[-1][4], mark_price)
        oi_subset = [row for row in oi_hist if _extract_timestamp(row) <= ts] or oi_hist[:1]
        funding_subset = [row for row in (funding_rates or []) if _extract_timestamp(row) <= ts] or (funding_rates or [])[:1]
        basis_subset = [row for row in (basis_rows or []) if _extract_timestamp(row) <= ts] or (basis_rows or [])[:1]
        taker_subset = [row for row in (taker_rows or []) if _extract_timestamp(row) <= ts] or (taker_rows or [])[:1]
        global_subset = [row for row in (global_ratio_rows or []) if _extract_timestamp(row) <= ts] or (global_ratio_rows or [])[:1]
        top_account_subset = [row for row in (top_account_rows or []) if _extract_timestamp(row) <= ts] or (
            top_account_rows or []
        )[:1]
        top_position_subset = [row for row in (top_position_rows or []) if _extract_timestamp(row) <= ts] or (
            top_position_rows or []
        )[:1]

        surface = _surface_from_snapshot(
            mark_price=mark,
            klines=subset,
            oi_hist=oi_subset,
            funding_rates=funding_subset,
            basis_rows=basis_subset,
            taker_rows=taker_subset,
            global_ratio_rows=global_subset,
            top_account_rows=top_account_subset,
            top_position_rows=top_position_subset,
            price_levels=price_levels,
        )
        slices.append(
            LiquidationHeatmapSlice(
                timestamp=ts,
                current_price=mark,
                total_intensity=surface["total_surface"],
                long_liquidation_intensity=surface["long_surface"],
                short_liquidation_intensity=surface["short_surface"],
            )
        )
    if slices and slices[-1].timestamp != _safe_int(klines[-1][0]):
        slices.append(
            LiquidationHeatmapSlice(
                timestamp=_safe_int(klines[-1][0]),
                current_price=_safe_float(klines[-1][4], mark_price),
                total_intensity=slices[-1].total_intensity,
                long_liquidation_intensity=slices[-1].long_liquidation_intensity,
                short_liquidation_intensity=slices[-1].short_liquidation_intensity,
            )
        )
    return slices[-history_points:]


def build_liquidation_tile(
    *,
    symbol: str,
    generated_at: int,
    range_pct: float,
    resolution: int,
    price_levels: list[float],
    heatmap: list[LiquidationHeatmapSlice],
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "generated_at": generated_at,
        "range_pct": range_pct,
        "resolution": resolution,
        "time_axis": [row.timestamp for row in heatmap],
        "price_levels": price_levels,
        "total_intensity": [row.total_intensity for row in heatmap],
        "long_intensity": [row.long_liquidation_intensity for row in heatmap],
        "short_intensity": [row.short_liquidation_intensity for row in heatmap],
    }


def build_multi_resolution_tiles(
    *,
    symbol: str,
    generated_at: int,
    range_pct: float,
    price_levels: list[float],
    heatmap: list[LiquidationHeatmapSlice],
    resolutions: list[int],
) -> list[dict[str, Any]]:
    tiles: list[dict[str, Any]] = []
    for resolution in sorted(set(resolutions)):
        if resolution <= 0:
            continue
        if resolution >= len(price_levels):
            downsampled_levels = list(price_levels)
            downsampled_heatmap = heatmap
        else:
            indices = [round(idx * (len(price_levels) - 1) / max(resolution - 1, 1)) for idx in range(resolution)]
            downsampled_levels = [price_levels[idx] for idx in indices]
            downsampled_heatmap = [
                LiquidationHeatmapSlice(
                    timestamp=row.timestamp,
                    current_price=row.current_price,
                    total_intensity=[row.total_intensity[idx] for idx in indices],
                    long_liquidation_intensity=[row.long_liquidation_intensity[idx] for idx in indices],
                    short_liquidation_intensity=[row.short_liquidation_intensity[idx] for idx in indices],
                )
                for row in heatmap
            ]

        tiles.append(
            build_liquidation_tile(
                symbol=symbol,
                generated_at=generated_at,
                range_pct=range_pct,
                resolution=len(downsampled_levels),
                price_levels=downsampled_levels,
                heatmap=downsampled_heatmap,
            )
        )
    return tiles


def build_liquidation_map_estimate(
    symbol: str,
    mark_price: float,
    oi_hist: list[dict[str, Any]],
    klines: list[list[Any]],
    funding_rates: list[dict[str, Any]] | None = None,
) -> LiquidationMapResponse:
    price_levels = _price_levels(mark_price, 10.0, 48)
    surface = _surface_from_snapshot(
        mark_price=mark_price,
        klines=klines,
        oi_hist=oi_hist,
        funding_rates=funding_rates,
        basis_rows=None,
        taker_rows=None,
        global_ratio_rows=None,
        top_account_rows=None,
        top_position_rows=None,
        price_levels=price_levels,
    )
    levels_above = _select_top_levels(price_levels, surface["short_surface"], Direction.SHORT, mark_price, limit=5)
    levels_below = _select_top_levels(price_levels, surface["long_surface"], Direction.LONG, mark_price, limit=5)
    cluster_step_bps = max(surface["metrics"]["realized_volatility_bps"] * 1.8, 18.0)
    clusters_above = _cluster_levels(levels_above, mark_price, cluster_step_bps)
    clusters_below = _cluster_levels(levels_below, mark_price, cluster_step_bps)
    pull_up = sum(level.strength for level in levels_above)
    pull_down = sum(level.strength for level in levels_below)
    denom = pull_up + pull_down
    confidence = 0.0 if denom <= 0.0 else max(pull_up, pull_down) / denom
    dominant_pull = Direction.FLAT
    if pull_up > pull_down:
        dominant_pull = Direction.LONG
    elif pull_down > pull_up:
        dominant_pull = Direction.SHORT

    leverage_weights = surface["assumptions"]["leverage_weights"]
    return LiquidationMapResponse(
        symbol=symbol,
        current_price=mark_price,
        is_estimated=True,
        methodology="Binance multi-factor estimated liquidation map",
        dominant_pull=dominant_pull,
        confidence=confidence,
        levels_above=levels_above,
        levels_below=levels_below,
        clusters_above=clusters_above,
        clusters_below=clusters_below,
        assumptions=LiquidationAssumptions(
            leverage_buckets=list(leverage_weights.keys()),
            leverage_weights=leverage_weights,
            maintenance_margin_rate=surface["assumptions"]["maintenance_margin_rate"],
            cluster_step_bps=cluster_step_bps,
            range_pct=10.0,
            heatmap_resolution=len(price_levels),
            entry_cohort_bins=len(price_levels),
            open_interest_change_pct=surface["metrics"]["open_interest_change_pct"],
            price_return_pct=surface["metrics"]["price_return_pct"],
            funding_rate_bps=surface["metrics"]["funding_rate_bps"],
            inferred_long_crowding=surface["assumptions"]["long_crowding"],
            inferred_short_crowding=surface["assumptions"]["short_crowding"],
        ),
        meta={
            "open_interest_change_pct": surface["metrics"]["open_interest_change_pct"],
            "price_return_pct": surface["metrics"]["price_return_pct"],
            "funding_rate_bps": surface["metrics"]["funding_rate_bps"],
            "short_volatility_bps": surface["metrics"]["realized_volatility_bps"],
            "long_share": surface["assumptions"]["long_crowding"]
            / max(surface["assumptions"]["long_crowding"] + surface["assumptions"]["short_crowding"], 1e-9),
            "short_share": surface["assumptions"]["short_crowding"]
            / max(surface["assumptions"]["long_crowding"] + surface["assumptions"]["short_crowding"], 1e-9),
            "pull_up": pull_up,
            "pull_down": pull_down,
        },
    )


def build_liquidation_map_advanced(
    symbol: str,
    mark_price: float,
    oi_hist: list[dict[str, Any]],
    klines: list[list[Any]],
    funding_rates: list[dict[str, Any]] | None = None,
    basis_rows: list[dict[str, Any]] | None = None,
    taker_volume_rows: list[dict[str, Any]] | None = None,
    global_ratio_rows: list[dict[str, Any]] | None = None,
    top_account_ratio_rows: list[dict[str, Any]] | None = None,
    top_position_ratio_rows: list[dict[str, Any]] | None = None,
    order_book: dict[str, Any] | None = None,
    mark_price_info: dict[str, Any] | None = None,
    live_events: list[LiquidationEventPoint] | None = None,
    bybit_events_raw: Any | None = None,
    include_events: bool = True,
    bybit_fetched_at_ms: int | None = None,
    degraded_reason: str | None = None,
    stream_health: list[LiquidationStreamHealth] | None = None,
    calibration: dict[str, float] | None = None,
    storage_stats: dict[str, Any] | None = None,
    range_pct: float = 10.0,
    resolution: int = 48,
    history_points: int = 20,
) -> LiquidationMapAdvancedResponse:
    normalized_events = _coerce_events(symbol, live_events=live_events, legacy_events=bybit_events_raw)
    stream_health = stream_health or []
    generated_at = int(datetime.now(timezone.utc).timestamp() * 1000)

    price_levels = _price_levels(mark_price, range_pct, resolution)
    base_surface = _surface_from_snapshot(
        mark_price=mark_price,
        klines=klines,
        oi_hist=oi_hist,
        funding_rates=funding_rates,
        basis_rows=basis_rows,
        taker_rows=taker_volume_rows,
        global_ratio_rows=global_ratio_rows,
        top_account_rows=top_account_ratio_rows,
        top_position_rows=top_position_ratio_rows,
        price_levels=price_levels,
        order_book=order_book,
        calibration=calibration,
    )

    event_weight = 0.0
    if include_events:
        long_surface, short_surface, event_weight = _apply_events_overlay(
            price_levels,
            base_surface["long_surface"],
            base_surface["short_surface"],
            normalized_events,
        )
    else:
        long_surface = list(base_surface["long_surface"])
        short_surface = list(base_surface["short_surface"])

    total_surface = [long_surface[idx] + short_surface[idx] for idx in range(len(price_levels))]
    levels_above = _select_top_levels(price_levels, short_surface, Direction.SHORT, mark_price)
    levels_below = _select_top_levels(price_levels, long_surface, Direction.LONG, mark_price)
    cluster_step_bps = max(base_surface["metrics"]["realized_volatility_bps"] * 1.5, 15.0)
    clusters_above = _cluster_levels(levels_above, mark_price, cluster_step_bps)
    clusters_below = _cluster_levels(levels_below, mark_price, cluster_step_bps)

    pull_up = sum(level.strength for level in levels_above)
    pull_down = sum(level.strength for level in levels_below)
    denom = pull_up + pull_down
    confidence = 0.0 if denom <= 0.0 else max(pull_up, pull_down) / denom
    dominant_pull = Direction.FLAT
    if pull_up > pull_down:
        dominant_pull = Direction.LONG
    elif pull_down > pull_up:
        dominant_pull = Direction.SHORT

    current_price = _safe_float(mark_price_info.get("markPrice"), mark_price) if mark_price_info else mark_price
    index_price = _safe_float(mark_price_info.get("indexPrice"), current_price) if mark_price_info else current_price
    next_funding_time = _safe_int(mark_price_info.get("nextFundingTime")) if mark_price_info else None
    latest_event_ts = max((event.timestamp for event in normalized_events), default=None)
    latest_received_at = max((event.received_at or 0 for event in normalized_events), default=0)
    source_age_ms = (generated_at - latest_received_at) if latest_received_at else None

    bybit_connected = any(health.source == "bybit" and health.connected for health in stream_health)
    event_overlay_active = include_events and event_weight > 0.0 and bool(normalized_events)
    degraded_mode = include_events and not event_overlay_active and not bybit_connected
    notes = ["Binance multi-factor liquidation model active"]
    if include_events:
        if event_overlay_active:
            notes.append("Live liquidation events overlaid from streaming sources")
        else:
            notes.append("No recent live liquidation events available; serving model-only surface")
    else:
        notes.append("Live liquidation overlay disabled by request")
    if degraded_reason:
        notes.append(degraded_reason)

    quality = LiquidationDataQuality(
        estimate_weight=1.0 - event_weight,
        event_weight=event_weight,
        bybit_available=bybit_connected or any(event.source == "bybit" for event in normalized_events),
        event_overlay_active=event_overlay_active,
        events_used=len(normalized_events),
        degraded_mode=degraded_mode,
        source_age_ms=source_age_ms,
        degraded_reason=degraded_reason,
        stream_health=stream_health,
        notes=notes,
    )

    heatmap = _build_heatmap_history(
        mark_price=mark_price,
        klines=klines,
        oi_hist=oi_hist,
        funding_rates=funding_rates,
        basis_rows=basis_rows,
        taker_rows=taker_volume_rows,
        global_ratio_rows=global_ratio_rows,
        top_account_rows=top_account_ratio_rows,
        top_position_rows=top_position_ratio_rows,
        price_levels=price_levels,
        history_points=history_points,
    )
    if heatmap:
        heatmap[-1] = LiquidationHeatmapSlice(
            timestamp=_safe_int(klines[-1][0]) if klines else generated_at,
            current_price=mark_price,
            total_intensity=total_surface,
            long_liquidation_intensity=long_surface,
            short_liquidation_intensity=short_surface,
        )

    leverage_weights = base_surface["assumptions"]["leverage_weights"]
    events_summary = _event_summary(normalized_events)
    calibration = calibration or {
        "intensity_multiplier": 1.0,
        "long_bias_multiplier": 1.0,
        "short_bias_multiplier": 1.0,
        "historical_event_count": 0.0,
    }
    storage_stats = storage_stats or {}
    market_metrics = LiquidationMarketMetrics(
        mark_price=current_price,
        index_price=index_price,
        basis_bps=base_surface["metrics"]["basis_bps"],
        last_funding_rate_bps=_safe_float(mark_price_info.get("lastFundingRate"), 0.0) * 10_000
        if mark_price_info
        else base_surface["metrics"]["funding_rate_bps"],
        open_interest_value=_open_interest_stats(oi_hist)[0],
        open_interest_change_pct=base_surface["metrics"]["open_interest_change_pct"],
        taker_buy_sell_ratio=base_surface["metrics"]["taker_buy_sell_ratio"],
        global_long_short_ratio=base_surface["metrics"]["global_long_short_ratio"],
        top_trader_account_ratio=base_surface["metrics"]["top_trader_account_ratio"],
        top_trader_position_ratio=base_surface["metrics"]["top_trader_position_ratio"],
        order_book_imbalance=base_surface["metrics"]["order_book_imbalance"],
        recent_volume=base_surface["metrics"]["recent_volume"],
        realized_volatility_bps=base_surface["metrics"]["realized_volatility_bps"],
        next_funding_time=next_funding_time,
    )

    return LiquidationMapAdvancedResponse(
        symbol=symbol,
        current_price=current_price,
        dominant_pull=dominant_pull,
        confidence=confidence,
        methodology="Binance multi-factor liquidation surface + live Binance/Bybit liquidation event overlay",
        source="binance_model+live_events" if event_overlay_active else "binance_model_only",
        generated_at=generated_at,
        price_range_low=price_levels[0],
        price_range_high=price_levels[-1],
        levels_above=levels_above,
        levels_below=levels_below,
        clusters_above=clusters_above,
        clusters_below=clusters_below,
        heatmap_price_levels=price_levels,
        heatmap=heatmap,
        depth_bands=_build_depth_bands(order_book, mark_price),
        market_metrics=market_metrics,
        assumptions=LiquidationAssumptions(
            leverage_buckets=list(leverage_weights.keys()),
            leverage_weights=leverage_weights,
            maintenance_margin_rate=base_surface["assumptions"]["maintenance_margin_rate"],
            cluster_step_bps=cluster_step_bps,
            range_pct=range_pct,
            heatmap_resolution=resolution,
            entry_cohort_bins=resolution,
            open_interest_change_pct=base_surface["metrics"]["open_interest_change_pct"],
            price_return_pct=base_surface["metrics"]["price_return_pct"],
            funding_rate_bps=base_surface["metrics"]["funding_rate_bps"],
            inferred_long_crowding=base_surface["assumptions"]["long_crowding"],
            inferred_short_crowding=base_surface["assumptions"]["short_crowding"],
        ),
        calibration=LiquidationCalibrationProfile(
            intensity_multiplier=calibration.get("intensity_multiplier", 1.0),
            long_bias_multiplier=calibration.get("long_bias_multiplier", 1.0),
            short_bias_multiplier=calibration.get("short_bias_multiplier", 1.0),
            historical_event_count=calibration.get("historical_event_count", 0.0),
        ),
        quality=quality,
        events_summary=events_summary,
        storage=LiquidationStorageStats(
            persistent_event_count_1h=int(storage_stats.get("persistent_event_count_1h", 0)),
            persistent_event_count_24h=int(storage_stats.get("persistent_event_count_24h", 0)),
            replay_snapshots_available=int(storage_stats.get("replay_snapshots_available", 0)),
            tile_resolutions_available=list(storage_stats.get("tile_resolutions_available", [])),
        ),
        events=sorted(normalized_events, key=lambda row: row.timestamp, reverse=True),
        meta={
            "open_interest_change_pct": base_surface["metrics"]["open_interest_change_pct"],
            "price_return_pct": base_surface["metrics"]["price_return_pct"],
            "funding_rate_bps": base_surface["metrics"]["funding_rate_bps"],
            "basis_bps": base_surface["metrics"]["basis_bps"],
            "short_volatility_bps": base_surface["metrics"]["realized_volatility_bps"],
            "intensity_multiplier": calibration.get("intensity_multiplier", 1.0),
            "long_share": base_surface["assumptions"]["long_crowding"]
            / max(base_surface["assumptions"]["long_crowding"] + base_surface["assumptions"]["short_crowding"], 1e-9),
            "short_share": base_surface["assumptions"]["short_crowding"]
            / max(base_surface["assumptions"]["long_crowding"] + base_surface["assumptions"]["short_crowding"], 1e-9),
            "pull_up": pull_up,
            "pull_down": pull_down,
            "event_weight_applied": event_weight,
            "event_count": float(len(normalized_events)),
            "latest_event_ts": float(latest_event_ts or 0),
        },
    )
