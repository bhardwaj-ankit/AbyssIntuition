from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

from liquidity_signal.data.daily_refresh import (
    archive_recovery_window,
    capture_safe_lookback_hours,
    quarantine_forward_cohort,
    refresh_window,
    training_readiness,
    write_refresh_report,
)


def test_refresh_window_retries_late_archive_days() -> None:
    assert refresh_window(date(2026, 8, 7), retry_days=3) == (
        date(2026, 8, 4),
        date(2026, 8, 6),
    )


def test_archive_recovery_extends_to_missing_forward_cohort_day(tmp_path) -> None:
    cohort = tmp_path / "forward.db"
    market = tmp_path / "market.db"
    onchain = tmp_path / "onchain.db"
    event_ts = int(datetime(2026, 8, 7, 12, tzinfo=timezone.utc).timestamp() * 1000)

    conn = sqlite3.connect(cohort)
    conn.execute("CREATE TABLE training_snapshots(symbol TEXT, event_ts INTEGER)")
    conn.execute("INSERT INTO training_snapshots VALUES('BTCUSDT', ?)", (event_ts,))
    conn.commit()
    conn.close()

    conn = sqlite3.connect(market)
    for table in ("vision_metrics", "vision_depth_features", "vision_trade_flow"):
        conn.execute(f"CREATE TABLE {table}(symbol TEXT, ts INTEGER)")
    conn.close()

    safe_ts = int(datetime(2026, 8, 6, tzinfo=timezone.utc).timestamp() * 1000)
    conn = sqlite3.connect(onchain)
    conn.execute(
        "CREATE TABLE onchain_daily(network TEXT, ts INTEGER, chain_tvl_usd REAL)"
    )
    conn.execute("INSERT INTO onchain_daily VALUES('btc', ?, 1.0)", (safe_ts,))
    conn.commit()
    conn.close()

    recovery = archive_recovery_window(
        date(2026, 8, 9),
        date(2026, 8, 11),
        forward_training_db=str(cohort),
        market_db=str(market),
        onchain_db=str(onchain),
        symbols=["BTCUSDT"],
    )

    assert recovery["effective_start"] == "2026-08-07"
    assert {item["family"] for item in recovery["missing_partitions"]} == {
        "vision_metrics",
        "vision_depth_features",
        "vision_trade_flow",
    }


def test_quarantine_preserves_audit_copy_before_clearing_active_rows(tmp_path) -> None:
    active = tmp_path / "forward.db"
    audit = tmp_path / "audit.db"
    conn = sqlite3.connect(active)
    conn.execute("CREATE TABLE training_snapshots(snapshot_id TEXT)")
    conn.execute("CREATE TABLE training_labels(snapshot_id TEXT)")
    conn.execute("CREATE TABLE training_decisions(snapshot_id TEXT)")
    for table in ("training_snapshots", "training_labels", "training_decisions"):
        conn.execute(f"INSERT INTO {table} VALUES('invalid-snapshot')")
    conn.commit()
    conn.close()

    result = quarantine_forward_cohort(
        str(active), str(audit), "test feed incident"
    )

    assert result["rows_quarantined"]["training_snapshots"] == 1
    active_conn = sqlite3.connect(active)
    assert active_conn.execute("SELECT COUNT(*) FROM training_snapshots").fetchone()[0] == 0
    active_conn.close()
    audit_conn = sqlite3.connect(audit)
    assert audit_conn.execute("SELECT COUNT(*) FROM training_snapshots").fetchone()[0] == 1
    assert audit_conn.execute(
        "SELECT reason FROM cohort_quarantine_audit"
    ).fetchone()[0] == "test feed incident"
    audit_conn.close()


def test_forward_lookback_starts_after_every_symbol_has_both_feeds(tmp_path) -> None:
    db_path = tmp_path / "liquidations.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE liquidation_capture_heartbeats(
            source TEXT, symbol TEXT, bucket_ts INTEGER, connected INTEGER
        )"""
    )
    now_ms = 20 * 3_600_000
    buckets = range(10 * 3_600_000, now_ms + 1, 300_000)
    conn.executemany(
        "INSERT INTO liquidation_capture_heartbeats VALUES(?,?,?,1)",
        [
            (source, symbol, bucket)
            for bucket in buckets
            for source in ("binance", "bybit")
            for symbol in ("BTCUSDT", "ETHUSDT")
        ],
    )
    conn.commit()
    conn.close()

    result = capture_safe_lookback_hours(
        str(db_path),
        ["BTCUSDT", "ETHUSDT"],
        10,
        now_ms=now_ms,
    )

    assert result["cohort_start_ts"] == 10 * 3_600_000
    assert result["hours"] == 10
    assert result["capture_coverage"] == 1.0


def test_forward_lookback_rejects_a_large_heartbeat_gap(tmp_path) -> None:
    db_path = tmp_path / "liquidations.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE liquidation_capture_heartbeats(
            source TEXT, symbol TEXT, bucket_ts INTEGER, connected INTEGER
        )"""
    )
    now_ms = 10 * 3_600_000
    conn.executemany(
        "INSERT INTO liquidation_capture_heartbeats VALUES(?,?,?,1)",
        [
            (source, "BTCUSDT", bucket)
            for bucket in (0, 300_000, now_ms)
            for source in ("binance", "bybit")
        ],
    )
    conn.commit()
    conn.close()

    result = capture_safe_lookback_hours(
        str(db_path), ["BTCUSDT"], 6, now_ms=now_ms
    )

    assert result["hours"] == 0
    assert "95%" in result["reason"]


def test_training_waits_for_complete_data_then_minimum_two_fold_span() -> None:
    incomplete = training_readiness(
        {"complete": False, "checks": {"liquidations": {"complete": False}}},
        train_days=90,
        validation_days=15,
        test_days=15,
        step_days=30,
    )
    assert incomplete["status"] == "blocked_data"
    assert incomplete["missing_checks"] == ["liquidations"]

    accumulating = training_readiness(
        {
            "complete": True,
            "period": {"min_ts": 0, "max_ts": 149 * 86_400_000},
        },
        train_days=90,
        validation_days=15,
        test_days=15,
        step_days=30,
    )
    assert accumulating["status"] == "accumulating_history"
    assert accumulating["required_days"] == 150

    sparse = training_readiness(
        {
            "complete": True,
            "period": {
                "min_ts": 0,
                "max_ts": 200 * 86_400_000,
                "effective_snapshot_days": 12.5,
            },
        },
        train_days=90,
        validation_days=15,
        test_days=15,
        step_days=30,
    )
    assert sparse["status"] == "accumulating_history"
    assert sparse["observed_days"] == 12.5


def test_refresh_report_is_written_as_valid_json(tmp_path) -> None:
    output = tmp_path / "refresh.json"
    write_refresh_report({"status": "complete"}, str(output))
    assert json.loads(output.read_text(encoding="utf-8")) == {"status": "complete"}
    assert not output.with_suffix(".json.tmp").exists()
