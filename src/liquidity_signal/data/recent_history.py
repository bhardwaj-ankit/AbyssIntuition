"""Append mature hourly samples and recover newly available liquidation archives."""

from __future__ import annotations

import argparse
import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from liquidity_signal.data.binance_client import BinanceFuturesClient
from liquidity_signal.data.cryptohft_archive import (
    CryptoHFTArchiveClient,
    ingest_cryptohft_range,
)
from liquidity_signal.data.daily_refresh import write_refresh_report
from liquidity_signal.service.engine import SignalEngine
from liquidity_signal.service.liquidation_store import LiquidationStore
from liquidity_signal.universe import ACTIVE_SYMBOLS

HOUR = 3_600_000


def missing_hourly_window(latest, now_ms):
    """Only append after the watermark; leave four hours for mature labels."""
    if latest is None or latest % HOUR != 60_000:
        raise ValueError("Expected an existing hourly cohort aligned at minute 01")
    last = ((now_ms - 4 * HOUR - 60_000) // HOUR) * HOUR + 60_000
    count = max(0, (last - latest) // HOUR)
    lookback = math.ceil((now_ms - (latest + HOUR - 60_000)) / HOUR)
    return {"count": count, "lookback_hours": max(1, lookback), "last_anchor": last}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.data_root.resolve()
    status_path = str(root / "recent_history_status.json")
    report = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
              "symbols": list(ACTIVE_SYMBOLS), "model_training": False, "steps": []}
    write_refresh_report(report, status_path)
    try:
        engine = SignalEngine(client=BinanceFuturesClient(timeout=60),
                              liquidation_store=LiquidationStore(root / "training_v6.db"))
        try:
            for symbol in ACTIVE_SYMBOLS:
                conn = sqlite3.connect((root / "training_v6.db").as_uri() + "?mode=ro", uri=True)
                latest = conn.execute("SELECT MAX(event_ts) FROM training_snapshots WHERE symbol=?", (symbol,)).fetchone()[0]
                conn.close()
                window = missing_hourly_window(latest, int(time.time() * 1000))
                result = None
                if window["count"]:
                    result = engine.backfill_historical_training(
                        symbol, lookback_hours=window["lookback_hours"], step_minutes=60,
                        max_samples=window["count"], include_stored_liquidation=False,
                        include_historical_positioning=True,
                    ).model_dump(mode="json")
                    if result["snapshots_created"] != window["count"] or result["resolved_labels"] != window["count"] * 5:
                        raise ValueError(f"Unexpected append counts for {symbol}: {result}")
                report["steps"].append({"symbol": symbol, "previous_last_anchor": latest,
                                        "window": window, "result": result})
                write_refresh_report(report, status_path)
                print(report["steps"][-1], flush=True)
        finally:
            engine.close()

        store = LiquidationStore(root / "liquidation_history.db")
        client = CryptoHFTArchiveClient(cache_dir=str(root / "cryptohft_liquidation_cache"),
                                       requests_per_minute=60, download_workers=8)
        try:
            watermarks = []
            for symbol in ACTIVE_SYMBOLS:
                for venue in ("binance", "bybit"):
                    source = f"cryptohft_recovery_{venue}"
                    latest = store._conn.execute(
                        "SELECT MAX(period) FROM liquidation_archive_ingest WHERE source=? AND symbol=? AND status='complete'",
                        (source, symbol),
                    ).fetchone()[0]
                    if latest is None:
                        raise ValueError(f"Missing archive baseline for {source}/{symbol}")
                    start = datetime.fromisoformat(latest.replace("Z", "+00:00")) + timedelta(hours=1)
                    failed = store._conn.execute(
                        "SELECT MIN(period) FROM liquidation_archive_ingest WHERE source=? AND symbol=? AND status!='complete'",
                        (source, symbol),
                    ).fetchone()[0]
                    if failed:
                        start = min(start, datetime.fromisoformat(failed.replace("Z", "+00:00")))
                    watermarks.append(start)
            start = min(watermarks)
            end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
            if start < end:
                result = ingest_cryptohft_range(client, store, start, end, list(ACTIVE_SYMBOLS),
                    report_path=str(root / "cryptohft_recent_status.json"))
                report["cryptohft"] = result
                if result["failed_partitions"]:
                    raise ValueError("Recent CryptoHFT archive has failed partitions")
        finally:
            client.close()
            store.close()
        report["status"] = "complete"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        raise
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_refresh_report(report, status_path)


if __name__ == "__main__":
    main()
