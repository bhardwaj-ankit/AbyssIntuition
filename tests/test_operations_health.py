from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from liquidity_signal.data.operations_health import build_operations_health


def _seed_heartbeats(path, now_ms: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE liquidation_capture_heartbeats(
            source TEXT, symbol TEXT, bucket_ts INTEGER, connected INTEGER,
            reconnects INTEGER, last_error TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE liquidation_events(
            source TEXT, event_ts INTEGER
        )"""
    )
    end_bucket = (now_ms // 300_000) * 300_000
    conn.executemany(
        "INSERT INTO liquidation_capture_heartbeats VALUES(?,?,?,1,0,NULL)",
        [
            (source, symbol, bucket)
            for bucket in range(end_bucket - 1_800_000, end_bucket + 1, 300_000)
            for source in ("binance", "bybit")
            for symbol in ("BTCUSDT", "ETHUSDT")
        ],
    )
    conn.commit()
    conn.close()


def test_operations_health_separates_liveness_from_training_readiness(tmp_path) -> None:
    now_ms = 1_800_000_000_000
    db_path = tmp_path / "liquidations.db"
    report_path = tmp_path / "refresh.json"
    _seed_heartbeats(db_path, now_ms)
    report_path.write_text(json.dumps({
        "status": "complete",
        "failed_steps": [],
        "finished_at": datetime.fromtimestamp(
            (now_ms - 3_600_000) / 1000, timezone.utc
        ).isoformat(),
        "strict_training": {"status": "accumulating_history", "ready": False},
    }), encoding="utf-8")

    health = build_operations_health(
        liquidation_db=str(db_path),
        refresh_report_path=str(report_path),
        backup_status_path=None,
        symbols=["BTCUSDT", "ETHUSDT"],
        now_ms=now_ms,
    )

    assert health["healthy"] is True
    assert health["recommended_action"] == "none"
    assert health["collector"]["recent_continuity"] == 1.0
    assert health["strict_training"]["status"] == "accumulating_history"


def test_operations_health_fails_closed_for_stale_collector(tmp_path) -> None:
    seeded_ms = 1_800_000_000_000
    db_path = tmp_path / "liquidations.db"
    report_path = tmp_path / "refresh.json"
    _seed_heartbeats(db_path, seeded_ms)
    report_path.write_text(json.dumps({
        "status": "complete",
        "failed_steps": [],
        "finished_at": datetime.fromtimestamp(seeded_ms / 1000, timezone.utc).isoformat(),
    }), encoding="utf-8")

    health = build_operations_health(
        liquidation_db=str(db_path),
        refresh_report_path=str(report_path),
        backup_status_path=None,
        symbols=["BTCUSDT", "ETHUSDT"],
        now_ms=seeded_ms + 45 * 60_000,
    )

    assert health["healthy"] is False
    assert health["recommended_action"] == "restart_collector"
    assert health["collector"]["stale_streams"]


def test_operations_health_recommends_refresh_without_restarting_fresh_feeds(tmp_path) -> None:
    now_ms = 1_800_000_000_000
    db_path = tmp_path / "liquidations.db"
    report_path = tmp_path / "refresh.json"
    _seed_heartbeats(db_path, now_ms)
    report_path.write_text(json.dumps({
        "status": "failed",
        "failed_steps": ["open_onchain"],
        "finished_at": datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(),
    }), encoding="utf-8")

    health = build_operations_health(
        liquidation_db=str(db_path),
        refresh_report_path=str(report_path),
        backup_status_path=None,
        symbols=["BTCUSDT", "ETHUSDT"],
        now_ms=now_ms,
    )

    assert health["collector"]["healthy"] is True
    assert health["recommended_action"] == "rerun_daily_refresh"
