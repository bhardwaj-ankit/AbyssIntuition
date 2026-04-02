from __future__ import annotations

import math
from typing import Any

from liquidity_signal.models import (
    Direction,
    LiquidationAssumptions,
    LiquidationCluster,
    LiquidationLevel,
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
    trend_bias = math.tanh(price_ret * 35.0)
    long_share = 0.5 + 0.5 * trend_bias
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
            inferred_long_crowding=long_crowding,
            inferred_short_crowding=short_crowding,
        ),
        meta={
            "open_interest_change_pct": oi_change * 100.0,
            "price_return_pct": price_ret * 100.0,
            "short_volatility_bps": short_vol_bps,
            "long_share": long_share,
            "short_share": short_share,
            "pull_up": pull_up,
            "pull_down": pull_down,
        },
    )