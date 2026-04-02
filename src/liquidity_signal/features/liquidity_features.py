from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from liquidity_signal.models import LiquidityFeatures


def _safe_float(v: Any) -> float:
    return float(v)


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


def _short_volatility_bps(klines: List[List[Any]], mark_price: float) -> float:
    if len(klines) < 2 or mark_price <= 0.0:
        return 0.0

    closes = [_safe_float(row[4]) for row in klines]
    diffs = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    mean_abs_move = sum(diffs) / len(diffs)
    return (mean_abs_move / mark_price) * 10_000


def build_liquidity_features(
    symbol: str,
    mark_price: float,
    order_book: Dict[str, Any],
    trades: List[Dict[str, Any]],
    klines: List[List[Any]],
) -> LiquidityFeatures:
    bids: List[Tuple[str, str]] = order_book["bids"]
    asks: List[Tuple[str, str]] = order_book["asks"]

    best_bid = _safe_float(bids[0][0])
    best_ask = _safe_float(asks[0][0])
    mid = (best_bid + best_ask) / 2.0
    spread_bps = ((best_ask - best_bid) / mid) * 10_000 if mid > 0 else 0.0

    return LiquidityFeatures(
        symbol=symbol,
        mark_price=mark_price,
        spread_bps=spread_bps,
        imbalance_l1=_imbalance(bids, asks, depth=1),
        imbalance_l5=_imbalance(bids, asks, depth=5),
        buy_flow_ratio=_trade_flow_ratio(trades),
        short_volatility_bps=_short_volatility_bps(klines, mark_price),
    )
