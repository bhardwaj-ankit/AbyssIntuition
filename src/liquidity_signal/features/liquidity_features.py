from __future__ import annotations

import math
from statistics import pstdev
from typing import Any, Dict, List, Sequence, Tuple

from liquidity_signal.models import LiquidityFeatures


def _safe_float(v: Any) -> float:
    return float(v)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _sum_levels(levels: Sequence[Sequence[str]], depth: int) -> float:
    return sum(_safe_float(row[1]) for row in levels[:depth])


def _imbalance(bids: Sequence[Sequence[str]], asks: Sequence[Sequence[str]], depth: int) -> float:
    bid_qty = _sum_levels(bids, depth)
    ask_qty = _sum_levels(asks, depth)
    denom = bid_qty + ask_qty
    if denom <= 0.0:
        return 0.0
    return (bid_qty - ask_qty) / denom


def _trade_flow_ratio(trades: List[Dict[str, Any]]) -> float:
    buy = 0.0
    sell = 0.0
    for t in trades:
        qty = _safe_float(t["qty"])
        # Binance `isBuyerMaker=True` means aggressive side is sell.
        if bool(t["isBuyerMaker"]):
            sell += qty
        else:
            buy += qty
    denom = buy + sell
    return 0.5 if denom <= 0.0 else buy / denom


def _candle_pressure(row: Sequence[Any]) -> float:
    open_price = _safe_float(row[1])
    high = _safe_float(row[2])
    low = _safe_float(row[3])
    close = _safe_float(row[4])
    span = max(high - low, max(close, open_price, 1.0) * 0.0001)
    body = _clamp((close - open_price) / span, -1.0, 1.0)
    close_location = _clamp((((close - low) / span) * 2.0) - 1.0, -1.0, 1.0)
    upper_wick = max(high - max(open_price, close), 0.0)
    lower_wick = max(min(open_price, close) - low, 0.0)
    wick_bias = _clamp((lower_wick - upper_wick) / span, -1.0, 1.0)
    return _clamp((0.55 * body) + (0.30 * close_location) + (0.15 * wick_bias), -1.0, 1.0)


def _directional_pressure(klines: List[List[Any]], lookback: int) -> float:
    visible = klines[-lookback:]
    if not visible:
        return 0.0

    weighted_total = 0.0
    weight_sum = 0.0
    for idx, row in enumerate(visible):
        volume = max(_safe_float(row[5]), 1.0)
        prev_close = _safe_float(visible[idx - 1][4]) if idx > 0 else _safe_float(row[1])
        close = _safe_float(row[4])
        close_to_close_bps = 0.0 if prev_close <= 0.0 else ((close - prev_close) / prev_close) * 10_000
        return_pressure = _clamp(close_to_close_bps / 8.0, -1.0, 1.0)
        recency_weight = 0.65 + (0.35 * ((idx + 1) / len(visible)))
        weight = recency_weight * math.sqrt(volume)
        combined_pressure = _clamp((_candle_pressure(row) * 0.35) + (return_pressure * 0.65), -1.0, 1.0)
        weighted_total += combined_pressure * weight
        weight_sum += weight
    if weight_sum <= 0.0:
        return 0.0
    return _clamp(weighted_total / weight_sum, -1.0, 1.0)


def _short_volatility_bps(klines: List[List[Any]], mark_price: float) -> float:
    if len(klines) < 2 or mark_price <= 0.0:
        return 0.0

    closes = [_safe_float(row[4]) for row in klines]
    diffs = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    mean_abs_move = sum(diffs) / len(diffs)
    return (mean_abs_move / mark_price) * 10_000


def _momentum_bps(klines: List[List[Any]], lookback: int) -> float:
    if len(klines) < lookback + 1:
        return 0.0

    start = _safe_float(klines[-lookback - 1][4])
    end = _safe_float(klines[-1][4])
    if start <= 0.0:
        return 0.0
    return ((end - start) / start) * 10_000


def _weighted_depth_imbalance(
    bids: Sequence[Sequence[str]], asks: Sequence[Sequence[str]], depth: int
) -> float:
    bid_score = 0.0
    ask_score = 0.0
    for idx, row in enumerate(bids[:depth]):
        weight = 1.0 / (idx + 1.0)
        bid_score += _safe_float(row[1]) * weight
    for idx, row in enumerate(asks[:depth]):
        weight = 1.0 / (idx + 1.0)
        ask_score += _safe_float(row[1]) * weight
    denom = bid_score + ask_score
    if denom <= 0.0:
        return 0.0
    return (bid_score - ask_score) / denom


def _volume_zscore(klines: List[List[Any]]) -> float:
    if len(klines) < 6:
        return 0.0

    volumes = [_safe_float(row[5]) for row in klines]
    baseline = volumes[:-1]
    baseline_mean = sum(baseline) / len(baseline)
    baseline_std = pstdev(baseline) if len(baseline) > 1 else 0.0
    if baseline_std <= 0.0:
        return 0.0
    return (volumes[-1] - baseline_mean) / baseline_std


def _avg_level_gap_bps(levels: Sequence[Sequence[str]], mark_price: float, depth: int) -> float:
    if len(levels) < 2 or mark_price <= 0.0:
        return 0.0
    visible = levels[:depth]
    prices = [_safe_float(row[0]) for row in visible]
    gaps = [abs(prices[idx] - prices[idx - 1]) for idx in range(1, len(prices))]
    if not gaps:
        return 0.0
    return (sum(gaps) / len(gaps) / mark_price) * 10_000


def _proxy_spread_bps(klines: List[List[Any]], mark_price: float, volume_zscore: float) -> float:
    if not klines or mark_price <= 0.0:
        return 1.5
    recent = klines[-12:]
    ranges_bps = [
        (abs(_safe_float(row[2]) - _safe_float(row[3])) / max(_safe_float(row[4]), 1e-9)) * 10_000 for row in recent
    ]
    avg_range_bps = sum(ranges_bps) / len(ranges_bps) if ranges_bps else 0.0
    return _clamp((avg_range_bps * 0.08) + max(0.0, -volume_zscore) * 0.35 + 0.7, 0.6, 6.5)


def _proxy_liquidity_gap_bps(klines: List[List[Any]], mark_price: float, volume_zscore: float) -> float:
    if not klines or mark_price <= 0.0:
        return 0.8
    recent = klines[-20:]
    ranges_bps = [
        (abs(_safe_float(row[2]) - _safe_float(row[3])) / max(_safe_float(row[4]), 1e-9)) * 10_000 for row in recent
    ]
    avg_range_bps = sum(ranges_bps) / len(ranges_bps) if ranges_bps else 0.0
    return _clamp((avg_range_bps * 0.16) + max(0.0, -volume_zscore) * 0.45 + 0.5, 0.4, 4.5)


def _last_value(rows: List[Dict[str, Any]], key: str, default: float = 0.0) -> float:
    if not rows:
        return default
    value = rows[-1].get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _open_interest_change_pct(oi_hist: List[Dict[str, Any]], lookback: int = 1) -> float:
    """Percent change in open-interest notional over the last ``lookback`` steps.

    ``lookback`` counts 5-minute buckets, so ``lookback=1`` is the most recent
    5-minute delta and ``lookback=6`` is roughly the last 30 minutes.  Using a
    small window rather than only the last two points makes the signal less
    sensitive to single-bucket noise; callers that need the raw 5-minute delta
    can keep the default.
    """
    if len(oi_hist) < 2:
        return 0.0

    steps = max(1, min(lookback, len(oi_hist) - 1))
    latest = _last_value(oi_hist, "sumOpenInterestValue", default=0.0)
    previous = _last_value(oi_hist[: -steps], "sumOpenInterestValue", default=0.0)
    if previous <= 0.0:
        return 0.0
    return ((latest - previous) / previous) * 100.0


def build_liquidity_features(
    symbol: str,
    mark_price: float,
    order_book: Dict[str, Any] | None,
    trades: List[Dict[str, Any]] | None,
    klines: List[List[Any]],
    oi_hist: List[Dict[str, Any]] | None = None,
    funding_rates: List[Dict[str, Any]] | None = None,
    basis_rows: List[Dict[str, Any]] | None = None,
    taker_volume_rows: List[Dict[str, Any]] | None = None,
    global_ratio_rows: List[Dict[str, Any]] | None = None,
    top_account_ratio_rows: List[Dict[str, Any]] | None = None,
    top_position_ratio_rows: List[Dict[str, Any]] | None = None,
    htf_bias: float = 0.0,
    htf_regime: str = "BALANCED",
) -> LiquidityFeatures:
    has_order_book = bool(order_book and order_book.get("bids") and order_book.get("asks"))
    bids: List[Tuple[str, str]] = order_book["bids"] if has_order_book else []
    asks: List[Tuple[str, str]] = order_book["asks"] if has_order_book else []
    volume_zscore = _volume_zscore(klines)

    if has_order_book:
        best_bid = _safe_float(bids[0][0])
        best_ask = _safe_float(asks[0][0])
        mid = (best_bid + best_ask) / 2.0
        spread_bps = ((best_ask - best_bid) / mid) * 10_000 if mid > 0 else 0.0
        imbalance_l1 = _imbalance(bids, asks, depth=1)
        imbalance_l5 = _imbalance(bids, asks, depth=5)
        imbalance_l10 = _imbalance(bids, asks, depth=10)
        weighted_depth_imbalance = _weighted_depth_imbalance(bids, asks, depth=10)
        liquidity_gap_bps = (
            _avg_level_gap_bps(bids, mark_price, depth=5) + _avg_level_gap_bps(asks, mark_price, depth=5)
        ) / 2.0
    else:
        imbalance_l1 = _clamp(_directional_pressure(klines, lookback=3) * 1.9, -1.0, 1.0)
        imbalance_l5 = _clamp(_directional_pressure(klines, lookback=8) * 1.55, -1.0, 1.0)
        imbalance_l10 = _clamp(_directional_pressure(klines, lookback=16) * 1.25, -1.0, 1.0)
        weighted_depth_imbalance = _clamp(
            (0.45 * imbalance_l1) + (0.35 * imbalance_l5) + (0.20 * imbalance_l10),
            -1.0,
            1.0,
        )
        spread_bps = _proxy_spread_bps(klines, mark_price, volume_zscore)
        liquidity_gap_bps = _proxy_liquidity_gap_bps(klines, mark_price, volume_zscore)

    if trades:
        buy_flow_ratio = _trade_flow_ratio(trades)
    else:
        buy_flow_ratio = _clamp(0.5 + (_directional_pressure(klines, lookback=6) * 0.45), 0.05, 0.95)

    return LiquidityFeatures(
        symbol=symbol,
        mark_price=mark_price,
        spread_bps=spread_bps,
        imbalance_l1=imbalance_l1,
        imbalance_l5=imbalance_l5,
        imbalance_l10=imbalance_l10,
        weighted_depth_imbalance=weighted_depth_imbalance,
        buy_flow_ratio=buy_flow_ratio,
        short_volatility_bps=_short_volatility_bps(klines, mark_price),
        micro_momentum_bps=_momentum_bps(klines, lookback=3),
        intraday_momentum_bps=_momentum_bps(klines, lookback=15),
        volume_zscore=volume_zscore,
        liquidity_gap_bps=liquidity_gap_bps,
        funding_rate_bps=_last_value(funding_rates or [], "fundingRate", default=0.0) * 10_000,
        basis_bps=_last_value(basis_rows or [], "basisRate", default=0.0) * 10_000,
        open_interest_change_pct=_open_interest_change_pct(oi_hist or []),
        taker_buy_sell_ratio=_last_value(taker_volume_rows or [], "buySellRatio", default=1.0),
        global_long_short_ratio=_last_value(global_ratio_rows or [], "longShortRatio", default=1.0),
        top_trader_account_ratio=_last_value(top_account_ratio_rows or [], "longShortRatio", default=1.0),
        top_trader_position_ratio=_last_value(top_position_ratio_rows or [], "longShortRatio", default=1.0),
        htf_bias=max(-1.0, min(1.0, htf_bias)),
        htf_regime=htf_regime,
    )
