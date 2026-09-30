from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from liquidity_signal.data.daily_refresh import DEFAULT_SYMBOLS, write_refresh_report


def _parse_iso_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def build_operations_health(
    *,
    liquidation_db: str = "runtime/liquidation_history.db",
    refresh_report_path: str = "runtime/daily_refresh_status.json",
    backup_status_path: str | None = "runtime/data_backup_status.json",
    symbols: list[str] | None = None,
    now_ms: int | None = None,
    stale_after_minutes: int = 10,
    continuity_window_minutes: int = 30,
    minimum_continuity: float = 0.95,
    refresh_stale_after_hours: int = 36,
    backup_stale_after_hours: int = 36,
) -> dict[str, Any]:
    """Audit collector liveness independently from model/data readiness."""
    selected = [symbol.upper() for symbol in (symbols or list(DEFAULT_SYMBOLS))]
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    source_symbols = [(source, symbol) for source in ("binance", "bybit") for symbol in selected]
    collector: dict[str, Any] = {
        "healthy": False,
        "stale_after_minutes": stale_after_minutes,
        "continuity_window_minutes": continuity_window_minutes,
        "minimum_continuity": minimum_continuity,
        "streams": {},
    }
    db_path = Path(liquidation_db)
    if not db_path.exists():
        collector["reason"] = "Liquidation database does not exist."
    else:
        conn = sqlite3.connect(db_path)
        try:
            has_heartbeats = bool(conn.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='liquidation_capture_heartbeats'"""
            ).fetchone())
            if not has_heartbeats:
                collector["reason"] = "No heartbeat table exists."
            else:
                stale_streams = []
                for source, symbol in source_symbols:
                    row = conn.execute(
                        """SELECT bucket_ts, reconnects, last_error
                           FROM liquidation_capture_heartbeats
                           WHERE source=? AND symbol=? AND connected=1
                           ORDER BY bucket_ts DESC LIMIT 1""",
                        (source, symbol),
                    ).fetchone()
                    latest_ts = row[0] if row else None
                    age_minutes = (
                        max(0.0, (current_ms - int(latest_ts)) / 60_000)
                        if latest_ts is not None
                        else None
                    )
                    stale = age_minutes is None or age_minutes > stale_after_minutes
                    key = f"{source}:{symbol}"
                    collector["streams"][key] = {
                        "latest_connected_bucket_ts": latest_ts,
                        "age_minutes": round(age_minutes, 2) if age_minutes is not None else None,
                        "stale": stale,
                        "reconnects": int(row[1] or 0) if row else 0,
                        "last_error": row[2] if row else None,
                    }
                    if stale:
                        stale_streams.append(key)

                bucket_ms = 300_000
                # Audit only completed buckets; the collector may persist the
                # current bucket seconds later without representing downtime.
                end_bucket = ((current_ms // bucket_ms) * bucket_ms) - bucket_ms
                expected = max(1, (continuity_window_minutes * 60_000) // bucket_ms)
                start_bucket = end_bucket - (expected - 1) * bucket_ms
                placeholders = ",".join("?" for _ in selected)
                connected = conn.execute(
                    f"""SELECT COUNT(*) FROM (
                            SELECT bucket_ts
                            FROM liquidation_capture_heartbeats
                            WHERE connected=1 AND source IN ('binance','bybit')
                                  AND symbol IN ({placeholders})
                                  AND bucket_ts BETWEEN ? AND ?
                            GROUP BY bucket_ts
                            HAVING COUNT(DISTINCT source || ':' || symbol)=?
                        )""",
                    [*selected, start_bucket, end_bucket, len(source_symbols)],
                ).fetchone()[0]
                coverage = min(1.0, connected / expected)
                collector.update({
                    "stale_streams": stale_streams,
                    "expected_common_buckets": expected,
                    "connected_common_buckets": connected,
                    "recent_continuity": round(coverage, 4),
                    "healthy": not stale_streams and coverage >= minimum_continuity,
                })

                has_events = bool(conn.execute(
                    """SELECT 1 FROM sqlite_master
                       WHERE type='table' AND name='liquidation_events'"""
                ).fetchone())
                if has_events:
                    collector["events_last_24h"] = {
                        source: conn.execute(
                            """SELECT COUNT(*) FROM liquidation_events
                               WHERE source=? AND event_ts>=?""",
                            (source, current_ms - 86_400_000),
                        ).fetchone()[0]
                        for source in ("binance", "bybit")
                    }
        finally:
            conn.close()

    refresh: dict[str, Any] = {"healthy": False}
    report_file = Path(refresh_report_path)
    training: dict[str, Any] = {"status": "unknown"}
    if not report_file.exists():
        refresh["reason"] = "Daily refresh report does not exist."
    else:
        try:
            report = json.loads(report_file.read_text(encoding="utf-8"))
            finished_ms = _parse_iso_ms(report.get("finished_at"))
            age_hours = (
                max(0.0, (current_ms - finished_ms) / 3_600_000)
                if finished_ms is not None
                else None
            )
            failed_steps = list(report.get("failed_steps") or [])
            refresh = {
                "healthy": (
                    report.get("status") == "complete"
                    and not failed_steps
                    and age_hours is not None
                    and age_hours <= refresh_stale_after_hours
                ),
                "status": report.get("status"),
                "finished_at": report.get("finished_at"),
                "age_hours": round(age_hours, 2) if age_hours is not None else None,
                "stale_after_hours": refresh_stale_after_hours,
                "failed_steps": failed_steps,
                "archive_window": report.get("archive_window"),
            }
            training = dict(report.get("strict_training") or {"status": "unknown"})
        except (json.JSONDecodeError, OSError) as exc:
            refresh["reason"] = f"Refresh report is unreadable: {exc}"

    backup: dict[str, Any]
    if backup_status_path is None:
        backup = {"healthy": True, "status": "not_checked"}
    else:
        backup_file = Path(backup_status_path)
        if not backup_file.exists():
            backup = {"healthy": False, "reason": "Data backup status does not exist."}
        else:
            try:
                backup_report = json.loads(backup_file.read_text(encoding="utf-8"))
                created_ms = _parse_iso_ms(backup_report.get("created_at"))
                age_hours = (
                    max(0.0, (current_ms - created_ms) / 3_600_000)
                    if created_ms is not None
                    else None
                )
                archive = Path(str(backup_report.get("archive") or ""))
                verification_valid = bool(
                    (backup_report.get("verification") or {}).get("valid")
                )
                backup = {
                    "healthy": (
                        backup_report.get("status") == "complete"
                        and verification_valid
                        and archive.is_file()
                        and age_hours is not None
                        and age_hours <= backup_stale_after_hours
                    ),
                    "status": backup_report.get("status"),
                    "created_at": backup_report.get("created_at"),
                    "age_hours": round(age_hours, 2) if age_hours is not None else None,
                    "stale_after_hours": backup_stale_after_hours,
                    "archive": str(archive),
                    "archive_exists": archive.is_file(),
                    "verification_valid": verification_valid,
                    "archive_bytes": backup_report.get("archive_bytes"),
                }
            except (json.JSONDecodeError, OSError) as exc:
                backup = {"healthy": False, "reason": f"Backup status is unreadable: {exc}"}

    if collector.get("stale_streams"):
        recommended_action = "restart_collector"
    elif not collector.get("healthy"):
        recommended_action = "record_continuity_gap"
    elif not refresh.get("healthy") or not backup.get("healthy"):
        recommended_action = "rerun_daily_refresh"
    else:
        recommended_action = "none"
    return {
        "version": 1,
        "checked_at_ms": current_ms,
        "healthy": bool(
            collector.get("healthy") and refresh.get("healthy") and backup.get("healthy")
        ),
        "recommended_action": recommended_action,
        "collector": collector,
        "daily_refresh": refresh,
        "data_backup": backup,
        # Accumulation is expected and must not make operational health red.
        "strict_training": training,
    }


def write_operations_health(health: dict[str, Any], output_path: str) -> None:
    write_refresh_report(health, output_path)
