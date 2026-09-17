from __future__ import annotations

from datetime import date, datetime, timezone
import sqlite3

from liquidity_signal.data.binance_vision_supplemental import (
    VisionSupplementalStore,
    build_completeness_manifest,
    normalize_depth_rows,
    normalize_funding_api_rows,
    normalize_funding_rows,
    normalize_kline_flow_rows,
)
from liquidity_signal.data.binance_vision import VisionMetricsStore
from liquidity_signal.data.open_onchain import OpenOnchainStore


def test_depth_rows_are_aggregated_at_completed_bucket_boundary() -> None:
    text = """timestamp,percentage,depth,notional
2026-02-07 00:00:08,-5.00,1,100
2026-02-07 00:00:08,-1.00,1,80
2026-02-07 00:00:08,-0.20,1,30
2026-02-07 00:00:08,0.20,1,10
2026-02-07 00:00:08,1.00,1,20
2026-02-07 00:00:08,5.00,1,100
2026-02-07 00:01:08,-5.00,1,100
2026-02-07 00:01:08,-1.00,1,40
2026-02-07 00:01:08,-0.20,1,10
2026-02-07 00:01:08,0.20,1,10
2026-02-07 00:01:08,1.00,1,40
2026-02-07 00:01:08,5.00,1,100
"""
    rows = normalize_depth_rows(text)

    assert len(rows) == 1
    assert rows[0]["ts"] == 1_770_422_700_000
    assert rows[0]["snapshots"] == 2
    assert rows[0]["imbalance_1"] == 0.3
    assert rows[0]["imbalance_020"] == 0.25


def test_funding_parser_and_supplemental_nearest_lookup(tmp_path) -> None:
    funding = normalize_funding_rows(
        "calc_time,funding_interval_hours,last_funding_rate\n"
        "1770000000000,8,-0.0001\n"
    )
    assert funding == [{
        "ts": 1_770_000_000_000,
        "funding_interval_hours": 8,
        "funding_rate": -0.0001,
    }]

    store = VisionSupplementalStore(str(tmp_path / "market.db"))
    store.upsert_funding("BTCUSDT", funding)
    store.upsert_depth("BTCUSDT", [{
        "ts": 1_770_000_000_000,
        "imbalance_020": 0.1,
        "imbalance_1": 0.2,
        "imbalance_5": 0.3,
        "total_notional_020": 10.0,
        "total_notional_1": 20.0,
        "total_notional_5": 30.0,
        "concentration_020": 1 / 3,
        "snapshots": 4,
    }])

    assert store.nearest_funding("BTCUSDT", 1_770_000_001_000)["funding_rate"] == -0.0001
    assert store.nearest_depth("BTCUSDT", 1_770_000_001_000)["imbalance_1"] == 0.2
    store.close()


def test_public_funding_normalizes_interval_and_exchange_alias(tmp_path) -> None:
    rows = normalize_funding_api_rows([
        {"fundingTime": 1_000, "fundingRate": "0.0001"},
        {"fundingTime": 14_401_000, "fundingRate": "-0.0002"},
    ])
    assert [row["funding_interval_hours"] for row in rows] == [4, 4]

    class PublicClient:
        def get_funding_rate_history(self, symbol, start_time, end_time):
            assert symbol == "1000PEPEUSDT"
            assert (start_time, end_time) == (1_000, 14_401_000)
            return [
                {"fundingTime": 1_000, "fundingRate": "0.0001"},
                {"fundingTime": 14_401_000, "fundingRate": "-0.0002"},
            ]

    store = VisionSupplementalStore(str(tmp_path / "market.db"))
    result = store.ingest_public_funding_range(
        PublicClient(), "PEPEUSDT", 1_000, 14_401_000
    )
    assert result["stored_rows"] == 2
    assert result["exchange_symbol"] == "1000PEPEUSDT"
    observed = store.nearest_funding("PEPEUSDT", 14_401_001)
    assert observed is not None
    assert observed["funding_rate"] == -0.0002
    store.close()


def test_completeness_manifest_handles_empty_forward_cohort(tmp_path) -> None:
    training_db = tmp_path / "training.db"
    liquidation_db = tmp_path / "liquidations.db"
    market_db = tmp_path / "market.db"
    onchain_db = tmp_path / "onchain.db"
    training = sqlite3.connect(training_db)
    training.execute("CREATE TABLE training_snapshots(symbol TEXT, event_ts INTEGER)")
    training.execute("CREATE TABLE training_labels(symbol TEXT DEFAULT 'BTCUSDT', raw_json TEXT)")
    training.close()
    liquidation = sqlite3.connect(liquidation_db)
    liquidation.execute(
        """CREATE TABLE liquidation_events(
            symbol TEXT, source TEXT, event_ts INTEGER
        )"""
    )
    liquidation.execute(
        """CREATE TABLE liquidation_capture_heartbeats(
            symbol TEXT, source TEXT, bucket_ts INTEGER, connected INTEGER
        )"""
    )
    liquidation.close()
    market = VisionSupplementalStore(str(market_db))
    market.close()
    sqlite3.connect(onchain_db).close()

    manifest = build_completeness_manifest(
        str(training_db),
        str(market_db),
        str(liquidation_db),
        ["BTCUSDT"],
        onchain_db=str(onchain_db),
    )

    assert manifest["complete"] is False
    assert manifest["period"]["min_ts"] is None
    assert manifest["period"]["max_ts"] is None
    assert manifest["period"]["effective_snapshot_days"] == 0.0
    assert manifest["checks"]["onchain"]["complete"] is False


def test_completeness_measures_availability_at_snapshot_anchors(tmp_path) -> None:
    training_db = tmp_path / "training.db"
    liquidation_db = tmp_path / "liquidations.db"
    market_db = tmp_path / "market.db"
    onchain_db = tmp_path / "onchain.db"
    event_ts = int(datetime(2026, 8, 7, 12, tzinfo=timezone.utc).timestamp() * 1000)

    training = sqlite3.connect(training_db)
    training.execute("CREATE TABLE training_snapshots(symbol TEXT, event_ts INTEGER)")
    training.execute("CREATE TABLE training_labels(symbol TEXT DEFAULT 'BTCUSDT', raw_json TEXT)")
    training.execute("INSERT INTO training_snapshots VALUES('BTCUSDT', ?)", (event_ts,))
    training.execute(
        "INSERT INTO training_labels(raw_json) VALUES(?)",
        ('{"raw_payload":{"label_version":"triple-barrier-v2"}}',),
    )
    # Retained retired records must not alter the active cohort or parse labels.
    training.execute("INSERT INTO training_snapshots VALUES('PEPEUSDT', ?)", (event_ts + 86_400_000,))
    training.execute("INSERT INTO training_labels VALUES('PEPEUSDT', 'invalid')")
    training.commit()
    training.close()

    positioning = VisionMetricsStore(str(market_db))
    positioning.upsert_many("BTCUSDT", [{
        "ts": event_ts - 300_000,
        "oi": 1.0,
        "oi_value": 1.0,
        "top_account_ratio": 1.0,
        "top_position_ratio": 1.0,
        "global_account_ratio": 1.0,
        "taker_ratio": 1.0,
    }])
    positioning.close()
    supplemental = VisionSupplementalStore(str(market_db))
    supplemental.upsert_depth("BTCUSDT", [{
        "ts": event_ts - 300_000,
        "imbalance_020": 0.0,
        "imbalance_1": 0.0,
        "imbalance_5": 0.0,
        "total_notional_020": 1.0,
        "total_notional_1": 1.0,
        "total_notional_5": 1.0,
        "concentration_020": 1.0,
        "snapshots": 1,
    }])
    supplemental.upsert_trade_flow("BTCUSDT", [{
        "ts": event_ts - 300_000,
        "buy_flow_ratio": 0.5,
        "taker_buy_quote": 1.0,
        "taker_sell_quote": 1.0,
        "quote_volume": 2.0,
        "trade_count": 1,
        "avg_trade_notional": 2.0,
        "range_bps": 1.0,
        "return_bps": 0.0,
    }])
    supplemental.upsert_funding("BTCUSDT", [{
        "ts": event_ts - 28_800_000,
        "funding_interval_hours": 8,
        "funding_rate": 0.0001,
    }])
    supplemental.close()

    liquidation = sqlite3.connect(liquidation_db)
    liquidation.execute(
        "CREATE TABLE liquidation_events(symbol TEXT, source TEXT, event_ts INTEGER)"
    )
    liquidation.execute(
        """CREATE TABLE liquidation_capture_heartbeats(
               source TEXT, symbol TEXT, bucket_ts INTEGER, connected INTEGER
           )"""
    )
    bucket = (event_ts // 300_000) * 300_000
    liquidation.executemany(
        "INSERT INTO liquidation_capture_heartbeats VALUES(?,?,?,1)",
        [(source, "BTCUSDT", bucket) for source in ("binance", "bybit")],
    )
    liquidation.commit()
    liquidation.close()

    onchain = OpenOnchainStore(str(onchain_db))
    onchain.upsert_tvl([{
        "network": "btc",
        "ts": (event_ts // 86_400_000) * 86_400_000 - 86_400_000,
        "chain_tvl_usd": 1.0,
    }])
    onchain.close()

    manifest = build_completeness_manifest(
        str(training_db),
        str(market_db),
        str(liquidation_db),
        ["BTCUSDT"],
        onchain_db=str(onchain_db),
    )

    assert manifest["checks"]["labels_v2"]["rows"] == 1
    assert manifest["period"]["max_ts"] == event_ts
    assert manifest["complete"] is True
    assert manifest["checks"]["positioning"]["minimum_coverage"] == 1.0
    assert manifest["checks"]["liquidations"]["minimum_capture_coverage"] == 1.0
    assert manifest["period"]["effective_snapshot_days"] == round(1 / 288, 4)

    liquidation = sqlite3.connect(liquidation_db)
    liquidation.execute("DELETE FROM liquidation_capture_heartbeats")
    liquidation.commit()
    liquidation.close()
    archive_manifest = build_completeness_manifest(
        str(training_db),
        str(market_db),
        str(liquidation_db),
        ["BTCUSDT"],
        onchain_db=str(onchain_db),
        require_live_liquidations=False,
        expected_snapshot_interval_minutes=60,
        data_profile="archive_only",
    )
    assert archive_manifest["complete"] is True
    assert archive_manifest["checks"]["liquidations"]["complete"] is False
    assert archive_manifest["checks"]["liquidations"]["required"] is False
    assert archive_manifest["period"]["effective_snapshot_days"] == round(1 / 24, 4)
    assert archive_manifest["data_profile"] == "archive_only"
    assert "estimated_liquidation_map" in archive_manifest["excluded_feature_families"]

    liquidation = sqlite3.connect(liquidation_db)
    liquidation.execute(
        """CREATE TABLE liquidation_archive_ingest(
               source TEXT, symbol TEXT, period TEXT, status TEXT,
               rows_stored INTEGER, source_paths_json TEXT,
               PRIMARY KEY(source, symbol, period)
           )"""
    )
    periods = {
        datetime.fromtimestamp(ts / 1000, timezone.utc).strftime(
            "%Y-%m-%dT%H:00:00Z"
        )
        for ts in (event_ts, event_ts - 3_600_000)
    }
    liquidation.executemany(
        "INSERT INTO liquidation_archive_ingest VALUES(?,?,?,?,?,?)",
        [
            (f"cryptohft_recovery_{venue}", "BTCUSDT", period, "complete", 0, "[]")
            for venue in ("binance", "bybit")
            for period in periods
        ],
    )
    liquidation.commit()
    liquidation.close()
    cryptohft_manifest = build_completeness_manifest(
        str(training_db),
        str(market_db),
        str(liquidation_db),
        ["BTCUSDT"],
        onchain_db=str(onchain_db),
        require_live_liquidations=False,
        expected_snapshot_interval_minutes=60,
        data_profile="archive_cryptohft",
    )
    assert cryptohft_manifest["complete"] is True
    assert cryptohft_manifest["checks"]["cryptohft_liquidations"][
        "minimum_archive_coverage"
    ] == 1.0
    assert "cryptohft_liquidations" in cryptohft_manifest["required_checks"]
    assert "binance_bybit_liquidation_events" not in cryptohft_manifest[
        "excluded_feature_families"
    ]


def test_kline_archive_extracts_real_taker_flow_after_candle_close(tmp_path) -> None:
    rows = normalize_kline_flow_rows(
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
        "taker_buy_volume,taker_buy_quote_volume,ignore\n"
        "1770000000000,100,102,99,101,20,1770000299999,2000,10,12,1200,0\n"
    )

    assert rows == [{
        "ts": 1_770_000_300_000,
        "buy_flow_ratio": 0.6,
        "taker_buy_quote": 1200.0,
        "taker_sell_quote": 800.0,
        "quote_volume": 2000.0,
        "trade_count": 10,
        "avg_trade_notional": 200.0,
        "range_bps": (3 / 101) * 10_000,
        "return_bps": 100.0,
    }]

    store = VisionSupplementalStore(str(tmp_path / "market.db"))
    store.upsert_trade_flow("BTCUSDT", rows)
    observed = store.nearest_trade_flow("BTCUSDT", 1_770_000_301_000)
    assert observed is not None
    assert observed["buy_flow_ratio"] == 0.6
    assert observed["trade_count"] == 10
    store.close()


def test_monthly_trade_flow_keeps_final_candle_at_next_month_boundary(tmp_path) -> None:
    final_close = normalize_kline_flow_rows(
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
        "taker_buy_volume,taker_buy_quote_volume,ignore\n"
        "1772322900000,100,101,99,100,20,1772323199999,2000,10,10,1000,0\n"
    )

    class ArchiveClient:
        def fetch_kline_month(self, symbol, month):
            return final_close if month == date(2026, 2, 1) else []

        def fetch_kline_day(self, symbol, day):
            return []

    store = VisionSupplementalStore(str(tmp_path / "market.db"))
    result = store.ingest_trade_flow_range(
        ArchiveClient(), "BTCUSDT", date(2026, 2, 1), date(2026, 2, 28)
    )

    assert result["stored_rows"] == 1
    assert store.nearest_trade_flow("BTCUSDT", 1_772_323_200_000) is not None
    store.close()
