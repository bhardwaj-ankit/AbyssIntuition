import sqlite3

from liquidity_signal.data.live_market import capture_cycle
from liquidity_signal.data.recent_history import HOUR, missing_hourly_window


def test_hourly_append_only_mature_missing_anchors():
    latest = 100 * HOUR + 60_000
    window = missing_hourly_window(latest, 110 * HOUR + 30 * 60_000)
    assert window == {"count": 6, "lookback_hours": 10, "last_anchor": 106 * HOUR + 60_000}
    # Before minute 01, that hour's four-hour label is not yet mature.
    assert missing_hourly_window(latest, 104 * HOUR)["count"] == 0
    assert missing_hourly_window(latest, 105 * HOUR)["count"] == 0
    assert missing_hourly_window(latest, 105 * HOUR + 60_000)["count"] == 1


def test_live_market_preserves_failures_and_observation_provenance():
    class Client:
        def __getattr__(self, name):
            def fetch(*args, **kwargs):
                if name == "get_order_book":
                    raise ValueError("unavailable")
                return {"source_timestamp": 1000, "price": "123"}
            return fetch
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE live_market_observations(
        symbol, family, requested_at, received_at, source, raw_json)""")
    results = capture_cycle(Client(), conn, ["BTCUSDT"])
    assert len(results) == 9
    assert [r["family"] for r in results if r["status"] == "failed"] == ["order_book"]
    rows = conn.execute("SELECT * FROM live_market_observations").fetchall()
    assert len(rows) == 8
    assert all(r[0] == "BTCUSDT" and r[3] >= r[2] and r[4] == "binance_rest" for r in rows)
    assert all('"source_timestamp": 1000' in r[5] for r in rows)
    conn.close()


def test_refresh_collection_policy_excludes_optional_pipelines(tmp_path, monkeypatch):
    from datetime import date

    from liquidity_signal.data import daily_refresh

    invoked = []
    monkeypatch.setattr(daily_refresh, "archive_recovery_window", lambda *a, **k: {"effective_start": "2026-09-15"})
    monkeypatch.setattr(daily_refresh, "capture_safe_lookback_hours", lambda *a, **k: {"hours": 0})
    def step(report, name, callback, output):
        invoked.append(name)
        return {}
    monkeypatch.setattr(daily_refresh, "_run_step", step)
    report = daily_refresh.run_daily_refresh(
        archive_start=date(2026, 9, 15), archive_end=date(2026, 9, 16),
        report_path=str(tmp_path / "refresh.json"), run_training_when_ready=False,
        include_cross_venue=False, include_hyperliquid=False,
    )
    assert "cross_venue" not in invoked
    assert "hyperliquid_liquidations" not in invoked
    assert "strict_walk_forward" not in invoked
    assert report["collection_policy"] == {"cross_venue": False, "hyperliquid": False, "automatic_training": False}


def test_backup_zero_retention_preserves_existing_archives(tmp_path):
    from liquidity_signal.data.data_backup import prune_backups
    for index in range(9):
        (tmp_path / f"data-backup-{index}.zip").write_bytes(b"preserved")
    assert prune_backups(str(tmp_path), 0) == []
    assert len(list(tmp_path.glob("*.zip"))) == 9


def test_online_backup_pins_snapshot_during_wal_writes(tmp_path, monkeypatch):
    from liquidity_signal.data import data_backup

    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    connect = sqlite3.connect
    with connect(source) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE records(value INTEGER)")
        conn.execute("INSERT INTO records VALUES(1)")
    conn.close()

    class SourceConnection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def backup(self, destination, **kwargs):
            with connect(source) as writer:
                writer.execute("INSERT INTO records VALUES(2)")
            writer.close()
            return self.conn.backup(destination, **kwargs)

    def wrapped(path, *args, **kwargs):
        conn = connect(path, *args, **kwargs)
        return SourceConnection(conn) if path == source else conn

    monkeypatch.setattr(data_backup.sqlite3, "connect", wrapped)
    assert data_backup._online_sqlite_backup(source, destination)["quick_check"] == "ok"
    with connect(destination) as conn:
        assert conn.execute("SELECT value FROM records").fetchall() == [(1,)]
    conn.close()
    with connect(source) as conn:
        assert conn.execute("SELECT value FROM records").fetchall() == [(1,), (2,)]
    conn.close()
