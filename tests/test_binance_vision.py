from __future__ import annotations

from liquidity_signal.data.binance_vision import (
    VisionMetricsStore,
    normalize_metric_rows,
)


CSV_HEADER = (
    "create_time,symbol,sum_open_interest,sum_open_interest_value,"
    "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
    "count_long_short_ratio,sum_taker_long_short_vol_ratio"
)


def _csv(*rows: str) -> str:
    return "\n".join([CSV_HEADER, *rows]) + "\n"


def test_normalize_maps_columns_and_parses_utc_time():
    text = _csv(
        "2026-05-28 00:05:00,BTCUSDT,101170.37,7539610760.41,1.67,1.38,1.67,1.25",
        "2026-05-28 00:10:00,BTCUSDT,101500.00,7560000000.00,1.70,1.40,1.68,1.26",
    )
    rows = normalize_metric_rows(text)
    assert len(rows) == 2
    first = rows[0]
    from datetime import datetime, timezone
    expected_ts = int(datetime(2026, 5, 28, 0, 5, tzinfo=timezone.utc).timestamp() * 1000)
    assert first["ts"] == expected_ts
    assert first["oi_value"] == 7539610760.41
    assert first["global_account_ratio"] == 1.67
    assert first["top_position_ratio"] == 1.38
    assert first["taker_ratio"] == 1.25


def test_normalize_skips_malformed_rows():
    text = _csv(
        "not-a-date,BTCUSDT,1,2,3,4,5,6",
        "2026-05-28 00:10:00,BTCUSDT,101500.00,7560000000.00,1.70,1.40,1.68,1.26",
    )
    rows = normalize_metric_rows(text)
    assert len(rows) == 1


def test_store_upsert_nearest_and_oi_change(tmp_path):
    store = VisionMetricsStore(str(tmp_path / "vision.db"))
    base = 1_764_288_300_000
    rows = [
        {"ts": base, "oi": 100.0, "oi_value": 1000.0, "top_account_ratio": 1.6,
         "top_position_ratio": 1.4, "global_account_ratio": 1.7, "taker_ratio": 1.2},
        {"ts": base + 1_800_000, "oi": 110.0, "oi_value": 1100.0, "top_account_ratio": 1.65,
         "top_position_ratio": 1.42, "global_account_ratio": 1.72, "taker_ratio": 1.25},
    ]
    assert store.upsert_many("BTCUSDT", rows) == 2
    # Idempotent upsert.
    assert store.upsert_many("BTCUSDT", rows) == 2
    assert store.coverage("BTCUSDT")["rows"] == 2

    # nearest picks the row at/just-before the query within tolerance.
    near = store.nearest("BTCUSDT", base + 1_800_000 + 120_000)
    assert near["oi_value"] == 1100.0
    # outside tolerance -> None
    assert store.nearest("BTCUSDT", base - 10_000_000) is None

    # OI change over the 30-minute lookback: (1100-1000)/1000 = 10%.
    change = store.oi_change_pct("BTCUSDT", base + 1_800_000, lookback_ms=1_800_000)
    assert change is not None and abs(change - 10.0) < 1e-6
    store.close()
