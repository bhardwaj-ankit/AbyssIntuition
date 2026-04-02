from liquidity_signal.features.liquidation_map import build_liquidation_map_estimate
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
