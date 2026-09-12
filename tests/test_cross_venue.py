from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from liquidity_signal.data.cross_venue import (
    CrossVenueStore,
    attach_cross_venue_features,
    bybit_archive_symbol,
    normalize_binance_spot_kline_rows,
    normalize_bybit_trade_rows,
)


def test_normalize_bybit_trade_rows_is_causal_and_uses_aggressor_side() -> None:
    lines = [
        "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,"
        "homeNotional,foreignNotional,RPI\n",
        "1770415200.001,BTCUSDT,Buy,2,100,PlusTick,id1,0,0,0,0\n",
        "1770415259.999,BTCUSDT,Sell,1,110,MinusTick,id2,0,0,0,0\n",
        "1770415500.000,BTCUSDT,Buy,1,120,PlusTick,id3,0,0,0,0\n",
    ]

    rows = normalize_bybit_trade_rows(lines)

    assert len(rows) == 2
    assert rows[0]["ts"] == 1770415500000
    assert rows[0]["quote_volume"] == pytest.approx(310)
    assert rows[0]["buy_flow_ratio"] == pytest.approx(200 / 310)
    assert rows[0]["trade_count"] == 2
    assert rows[0]["return_bps"] == pytest.approx(1000)
    assert rows[1]["ts"] == 1770415800000
    assert bybit_archive_symbol("pepeusdt") == "1000PEPEUSDT"


def test_normalize_binance_spot_kline_rows_handles_headerless_microseconds() -> None:
    text = (
        "1785542400000000,100,110,90,105,5,1785542699999999,1000,10,2,600,0\n"
    )

    rows = normalize_binance_spot_kline_rows(text)

    assert rows == [{
        "ts": 1785542700000,
        "buy_flow_ratio": 0.6,
        "taker_buy_quote": 600.0,
        "taker_sell_quote": 400.0,
        "quote_volume": 1000.0,
        "trade_count": 10,
        "avg_trade_notional": 100.0,
        "max_trade_notional": None,
        "return_bps": pytest.approx(500),
        "range_bps": pytest.approx((20 / 105) * 10_000),
    }]


def _flow(ts: int, buy_ratio: float, volume: float, return_bps: float) -> dict:
    return {
        "ts": ts,
        "buy_flow_ratio": buy_ratio,
        "taker_buy_quote": volume * buy_ratio,
        "taker_sell_quote": volume * (1 - buy_ratio),
        "quote_volume": volume,
        "trade_count": 10,
        "avg_trade_notional": volume / 10,
        "max_trade_notional": volume / 2,
        "return_bps": return_bps,
    }


def test_store_nearest_rows_and_source_isolated_features(tmp_path) -> None:
    store = CrossVenueStore(str(tmp_path / "cross.db"))
    ts = 1_770_415_500_000
    store.upsert_trade_flow("bybit", "perpetual", "BTCUSDT", [_flow(ts, 0.4, 500, -4)])
    store.upsert_trade_flow("binance", "spot", "BTCUSDT", [_flow(ts, 0.7, 1000, 6)])
    store.upsert_positioning("bybit", "BTCUSDT", [
        {"ts": ts - 3_600_000, "open_interest": 100, "long_ratio": 0.55,
         "short_ratio": 0.45},
        {"ts": ts, "open_interest": 110, "long_ratio": 0.6, "short_ratio": 0.4},
    ])
    store.upsert_funding("bybit", "BTCUSDT", [{"ts": ts, "funding_rate": 0.0001}])

    features: dict = {}
    attach_cross_venue_features(features, store, "BTCUSDT", ts)

    assert features["xvenue.bybit_perp.buy_flow_ratio"] == 0.4
    assert features["xvenue.binance_spot.buy_flow_ratio"] == 0.7
    assert features["xvenue.spot_perp.buy_ratio_spread"] == pytest.approx(0.3)
    assert features["xvenue.spot_perp.return_spread_bps"] == 10
    assert features["xvenue.bybit_positioning.oi_change_1h_pct"] == pytest.approx(10)
    assert features["xvenue.bybit_positioning.funding_rate_bps"] == 1
    assert store.nearest_trade_flow(
        "bybit", "perpetual", "BTCUSDT", ts + 600_001
    ) is None
    store.close()


class _FakeBybit:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_trade_day(self, symbol: str, day: date) -> dict:
        self.calls += 1
        return {
            "rows": [_flow(1_770_415_500_000, 0.5, 100, 0)],
            "bytes_downloaded": 123,
            "sha256": "abc",
        }


def test_ingest_ledger_skips_completed_days_and_keeps_provenance(tmp_path) -> None:
    db_path = tmp_path / "cross.db"
    store = CrossVenueStore(str(db_path))
    client = _FakeBybit()
    day = date(2026, 2, 7)

    first = store.ingest_bybit_trades_range(client, "BTCUSDT", day, day)
    second = store.ingest_bybit_trades_range(client, "BTCUSDT", day, day)

    assert first["stored_rows"] == 1
    assert second["stored_rows"] == 0
    assert second["complete_days"] == 1
    assert client.calls == 1
    ledger = store._conn.execute(
        "SELECT status, rows_stored, bytes_downloaded, sha256 FROM cross_venue_ingest"
    ).fetchone()
    assert ledger == ("complete", 1, 123, "abc")
    store.close()


def test_feature_coverage_is_measured_at_training_anchors(tmp_path) -> None:
    training_db = tmp_path / "training.db"
    con = sqlite3.connect(training_db)
    con.execute("CREATE TABLE training_snapshots(symbol TEXT, event_ts INTEGER)")
    con.executemany(
        "INSERT INTO training_snapshots VALUES(?,?)",
        [("BTCUSDT", 1_000_000), ("BTCUSDT", 1_300_000)],
    )
    con.commit()
    con.close()
    store = CrossVenueStore(str(tmp_path / "cross.db"))
    store.upsert_trade_flow("bybit", "perpetual", "BTCUSDT", [_flow(1_000_000, 0.5, 1, 0)])
    store.upsert_trade_flow("binance", "spot", "BTCUSDT", [_flow(1_000_000, 0.5, 1, 0)])
    store.upsert_positioning("bybit", "BTCUSDT", [
        {"ts": 1_000_000, "open_interest": 1, "long_ratio": 0.5, "short_ratio": 0.5}
    ])

    report = store.feature_coverage(str(training_db), ["BTCUSDT"], tolerance_ms=1)

    assert report["anchors"] == 2
    assert report["coverage"] == {
        "bybit_trades": 0.5,
        "bybit_positioning": 0.5,
        "binance_spot": 0.5,
    }
    store.close()

