from liquidity_signal.features.liquidation_map import build_liquidation_map_advanced, build_liquidation_map_estimate
from liquidity_signal.models import Direction


def _sample_klines(up: bool = True) -> list[list[float]]:
    closes = [100.0, 101.0, 102.0, 103.0] if up else [103.0, 102.0, 101.0, 100.0]
    rows: list[list[float]] = []
    for c in closes:
        rows.append([0.0, c, c, c, c, 0.0])
    return rows


def test_liquidation_map_outputs_levels() -> None:
    oi_hist = [
        {"sumOpenInterestValue": "1000000000"},
        {"sumOpenInterestValue": "1200000000"},
    ]
    result = build_liquidation_map_estimate("BTCUSDT", 100.0, oi_hist, _sample_klines(up=True))

    assert result.symbol == "BTCUSDT"
    assert result.is_estimated is True
    assert len(result.levels_above) == 5
    assert len(result.levels_below) == 5
    assert result.clusters_above
    assert result.clusters_below
    assert result.assumptions.leverage_buckets == [10, 25, 50, 75, 100]
    assert 0.0 <= result.confidence <= 1.0
    assert result.methodology


def test_liquidation_map_uses_funding_to_shift_crowding() -> None:
    oi_hist = [
        {"sumOpenInterestValue": "1000000000"},
        {"sumOpenInterestValue": "1200000000"},
    ]

    positive_funding = build_liquidation_map_estimate(
        "BTCUSDT",
        100.0,
        oi_hist,
        _sample_klines(up=False),
        funding_rates=[{"fundingRate": "0.0010"}],
    )
    negative_funding = build_liquidation_map_estimate(
        "BTCUSDT",
        100.0,
        oi_hist,
        _sample_klines(up=True),
        funding_rates=[{"fundingRate": "-0.0010"}],
    )

    assert positive_funding.assumptions.funding_rate_bps > 0.0
    assert positive_funding.assumptions.inferred_long_crowding > positive_funding.assumptions.inferred_short_crowding
    assert negative_funding.assumptions.funding_rate_bps < 0.0
    assert negative_funding.assumptions.inferred_short_crowding > negative_funding.assumptions.inferred_long_crowding


def test_liquidation_map_pull_direction_changes_with_trend() -> None:
    oi_hist = [
        {"sumOpenInterestValue": "1000000000"},
        {"sumOpenInterestValue": "1200000000"},
    ]

    up = build_liquidation_map_estimate("BTCUSDT", 100.0, oi_hist, _sample_klines(up=True))
    down = build_liquidation_map_estimate("BTCUSDT", 100.0, oi_hist, _sample_klines(up=False))

    assert up.assumptions.inferred_long_crowding > up.assumptions.inferred_short_crowding
    assert down.assumptions.inferred_short_crowding > down.assumptions.inferred_long_crowding
    assert up.dominant_pull in {Direction.LONG, Direction.SHORT, Direction.FLAT}
    assert down.dominant_pull in {Direction.LONG, Direction.SHORT, Direction.FLAT}


def test_advanced_map_degrades_when_bybit_unavailable() -> None:
    oi_hist = [
        {"sumOpenInterestValue": "1000000000"},
        {"sumOpenInterestValue": "1200000000"},
    ]
    result = build_liquidation_map_advanced(
        symbol="BTCUSDT",
        mark_price=100.0,
        oi_hist=oi_hist,
        klines=_sample_klines(up=True),
        bybit_events_raw=None,
        include_events=True,
        degraded_reason="Bybit test failure",
    )

    assert result.quality.degraded_mode is True
    assert result.quality.bybit_available is False
    assert result.quality.event_overlay_active is False
    assert result.quality.events_used == 0
    assert result.quality.event_weight == 0.0
    assert result.quality.degraded_reason == "Bybit test failure"
    assert result.quality.source_age_ms is None
    assert result.source == "binance_estimate_fallback"
    assert result.events == []


def test_advanced_map_uses_bybit_events_when_available() -> None:
    oi_hist = [
        {"sumOpenInterestValue": "1000000000"},
        {"sumOpenInterestValue": "1300000000"},
    ]
    bybit_events = [
        {"side": "Sell", "price": "101.0", "size": "12", "updatedTime": "1710000000001"},
        {"side": "Buy", "price": "99.0", "size": "8", "updatedTime": "1710000000002"},
    ]

    result = build_liquidation_map_advanced(
        symbol="BTCUSDT",
        mark_price=100.0,
        oi_hist=oi_hist,
        klines=_sample_klines(up=True),
        funding_rates=[{"fundingRate": "0.0001"}],
        bybit_events_raw=bybit_events,
        include_events=True,
        bybit_fetched_at_ms=1_710_000_000_000,
    )

    assert result.quality.bybit_available is True
    assert result.quality.event_overlay_active is True
    assert result.quality.degraded_mode is False
    assert result.quality.event_weight > 0.0
    assert result.quality.events_used == 2
    assert result.quality.source_age_ms is not None
    assert result.quality.source_age_ms >= 0
    assert result.source == "binance_estimate+bybit_events"
    assert len(result.events) == 2
    assert result.meta["event_count"] == 2.0


def test_advanced_map_supports_ws_style_bybit_payload_and_dedupes() -> None:
    oi_hist = [
        {"sumOpenInterestValue": "1000000000"},
        {"sumOpenInterestValue": "1300000000"},
    ]
    ws_payload = [
        {"S": "Sell", "p": "101.0", "v": "12", "T": "1710000000001"},
        {"S": "Sell", "p": "101.0", "v": "12", "T": "1710000000001"},
        {"S": "Buy", "p": "99.0", "v": "8", "T": "1710000000002"},
    ]

    result = build_liquidation_map_advanced(
        symbol="BTCUSDT",
        mark_price=100.0,
        oi_hist=oi_hist,
        klines=_sample_klines(up=True),
        bybit_events_raw=ws_payload,
        include_events=True,
        bybit_fetched_at_ms=1_710_000_000_000,
    )

    assert len(result.events) == 2
    assert result.quality.events_used == 2
    assert result.meta["event_count"] == 2.0
