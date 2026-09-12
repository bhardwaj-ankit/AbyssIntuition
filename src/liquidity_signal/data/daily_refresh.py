from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


DEFAULT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "NEARUSDT",
    "PEPEUSDT",
)


def archive_recovery_window(
    requested_start: date,
    requested_end: date,
    *,
    forward_training_db: str,
    market_db: str,
    onchain_db: str,
    symbols: list[str],
) -> dict[str, Any]:
    """Extend a refresh back to archive gaps needed by existing cohort rows."""
    effective_start = requested_start
    missing: list[dict[str, str]] = []
    training_path = Path(forward_training_db)
    market_path = Path(market_db)
    if not training_path.exists() or not market_path.exists():
        return {
            "requested_start": requested_start.isoformat(),
            "effective_start": effective_start.isoformat(),
            "end": requested_end.isoformat(),
            "missing_partitions": missing,
        }

    training = sqlite3.connect(training_path)
    try:
        has_snapshots = training.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='training_snapshots'"
        ).fetchone()
        if not has_snapshots:
            cohort_days: list[date] = []
        else:
            cohort_days = [
                datetime.fromtimestamp(int(row[0]) / 1000, timezone.utc).date()
                for row in training.execute(
                    """SELECT DISTINCT (event_ts / 86400000) * 86400000
                       FROM training_snapshots ORDER BY 1"""
                )
                if row[0] is not None
            ]
    finally:
        training.close()

    selected_days = [day for day in cohort_days if day <= requested_end]
    if not selected_days:
        return {
            "requested_start": requested_start.isoformat(),
            "effective_start": effective_start.isoformat(),
            "end": requested_end.isoformat(),
            "missing_partitions": missing,
        }

    market = sqlite3.connect(market_path)
    try:
        tables = ("vision_metrics", "vision_depth_features", "vision_trade_flow")
        available_tables = {
            row[0]
            for row in market.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for day in selected_days:
            day_start = int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
            day_end = day_start + 86_400_000
            for table in tables:
                for symbol in symbols:
                    count = (
                        market.execute(
                            f"SELECT COUNT(*) FROM {table} "
                            "WHERE symbol=? AND ts>=? AND ts<?",
                            (symbol.upper(), day_start, day_end),
                        ).fetchone()[0]
                        if table in available_tables
                        else 0
                    )
                    if count < 274:  # 95% of the expected 288 five-minute rows.
                        effective_start = min(effective_start, day)
                        missing.append({
                            "family": table,
                            "symbol": symbol.upper(),
                            "day": day.isoformat(),
                        })
    finally:
        market.close()

    onchain_path = Path(onchain_db)
    if onchain_path.exists():
        from liquidity_signal.data.open_onchain import SYMBOL_NETWORK

        onchain = sqlite3.connect(onchain_path)
        try:
            has_onchain = onchain.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='onchain_daily'"
            ).fetchone()
            for day in selected_days:
                safe_day = day - timedelta(days=1)
                safe_ts = int(
                    datetime.combine(
                        safe_day, datetime.min.time(), tzinfo=timezone.utc
                    ).timestamp()
                    * 1000
                )
                for network in sorted({SYMBOL_NETWORK[symbol.upper()] for symbol in symbols}):
                    exists = bool(
                        has_onchain
                        and onchain.execute(
                            """SELECT 1 FROM onchain_daily
                               WHERE network=? AND ts=? AND chain_tvl_usd IS NOT NULL""",
                            (network, safe_ts),
                        ).fetchone()
                    )
                    if not exists:
                        effective_start = min(effective_start, safe_day)
                        missing.append({
                            "family": "onchain_daily",
                            "symbol": network,
                            "day": safe_day.isoformat(),
                        })
        finally:
            onchain.close()

    return {
        "requested_start": requested_start.isoformat(),
        "effective_start": effective_start.isoformat(),
        "end": requested_end.isoformat(),
        "missing_partitions": missing,
    }


def quarantine_forward_cohort(
    db_path: str,
    audit_path: str,
    reason: str,
) -> dict[str, Any]:
    """Preserve an invalid cohort in an audit DB, then clear active training rows."""
    source_path = Path(db_path).resolve()
    target_path = Path(audit_path).resolve()
    if not reason.strip():
        raise ValueError("A non-empty quarantine reason is required.")
    if not source_path.exists():
        raise FileNotFoundError(f"Forward cohort does not exist: {source_path}")
    if target_path.exists():
        raise FileExistsError(f"Audit target already exists: {target_path}")
    if source_path == target_path:
        raise ValueError("Audit target must differ from the active cohort database.")
    target_path.parent.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(source_path)
    audit = sqlite3.connect(target_path)
    tables = ("training_snapshots", "training_labels", "training_decisions")
    try:
        before = {
            table: source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        source.backup(audit)
        audit.execute(
            """CREATE TABLE IF NOT EXISTS cohort_quarantine_audit(
                   quarantined_at TEXT NOT NULL,
                   source_path TEXT NOT NULL,
                   reason TEXT NOT NULL
               )"""
        )
        audit.execute(
            "INSERT INTO cohort_quarantine_audit VALUES(?,?,?)",
            (datetime.now(timezone.utc).isoformat(), str(source_path), reason.strip()),
        )
        audit.commit()
        audit_check = audit.execute("PRAGMA quick_check").fetchone()[0]
        if audit_check != "ok":
            raise RuntimeError(f"Audit database quick_check failed: {audit_check}")

        with source:
            source.execute("DELETE FROM training_labels")
            source.execute("DELETE FROM training_decisions")
            source.execute("DELETE FROM training_snapshots")
        active_check = source.execute("PRAGMA quick_check").fetchone()[0]
        if active_check != "ok":
            raise RuntimeError(f"Active database quick_check failed: {active_check}")
        after = {
            table: source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
    finally:
        audit.close()
        source.close()

    return {
        "status": "complete",
        "active_db": str(source_path),
        "audit_db": str(target_path),
        "reason": reason.strip(),
        "rows_quarantined": before,
        "active_rows_after": after,
        "audit_quick_check": audit_check,
        "active_quick_check": active_check,
    }


def refresh_window(
    today: date | None = None,
    *,
    retry_days: int = 3,
    publication_lag_days: int = 1,
) -> tuple[date, date]:
    """Return a UTC archive window with overlap for late-published partitions."""
    if retry_days < 1:
        raise ValueError("retry_days must be at least one")
    if publication_lag_days < 1:
        raise ValueError("publication_lag_days must be at least one")
    utc_today = today or datetime.now(timezone.utc).date()
    end_day = utc_today - timedelta(days=publication_lag_days)
    return end_day - timedelta(days=retry_days - 1), end_day


def capture_safe_lookback_hours(
    liquidation_db: str,
    symbols: list[str],
    requested_hours: int,
    *,
    now_ms: int | None = None,
    label_horizon_hours: int = 4,
    minimum_coverage: float = 0.95,
) -> dict[str, Any]:
    """Find a backfill window whose eligible anchors have dual-feed coverage."""
    if requested_hours <= label_horizon_hours:
        raise ValueError("requested_hours must exceed the maximum label horizon")
    path = Path(liquidation_db)
    if not path.exists():
        return {
            "hours": 0,
            "cohort_start_ts": None,
            "reason": "Liquidation capture database does not exist.",
        }
    conn = sqlite3.connect(path)
    try:
        table = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='liquidation_capture_heartbeats'"""
        ).fetchone()
        if not table:
            return {
                "hours": 0,
                "cohort_start_ts": None,
                "reason": "No liquidation capture heartbeats exist yet.",
            }
        placeholders = ",".join("?" for _ in symbols)
        rows = conn.execute(
            f"""SELECT bucket_ts
                FROM liquidation_capture_heartbeats
                WHERE connected=1 AND source IN ('binance','bybit')
                      AND symbol IN ({placeholders})
                GROUP BY bucket_ts
                HAVING COUNT(DISTINCT source || ':' || symbol)=?""",
            [*(symbol.upper() for symbol in symbols), len(symbols) * 2],
        ).fetchall()
    finally:
        conn.close()
    common_buckets = sorted(int(row[0]) for row in rows)
    if not common_buckets:
        return {
            "hours": 0,
            "cohort_start_ts": None,
            "reason": "Both exchange feeds have not recorded heartbeats for every symbol.",
        }
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    bucket_ms = 300_000
    current_bucket = (current_ms // bucket_ms) * bucket_ms
    if common_buckets[-1] < current_bucket - (2 * bucket_ms):
        return {
            "hours": 0,
            "cohort_start_ts": None,
            "latest_common_bucket_ts": common_buckets[-1],
            "reason": "Dual-exchange capture heartbeats are stale.",
        }
    eligible_end = current_bucket - label_horizon_hours * 3_600_000
    common = set(common_buckets)
    for hours in range(requested_hours, label_horizon_hours, -1):
        raw_start = current_ms - hours * 3_600_000
        start_bucket = ((raw_start + bucket_ms - 1) // bucket_ms) * bucket_ms
        if start_bucket > eligible_end:
            continue
        expected = ((eligible_end - start_bucket) // bucket_ms) + 1
        observed = sum(start_bucket <= bucket <= eligible_end for bucket in common)
        coverage = observed / expected
        if coverage >= minimum_coverage:
            return {
                "hours": hours,
                "cohort_start_ts": start_bucket,
                "eligible_end_ts": eligible_end,
                "eligible_buckets": expected,
                "connected_buckets": observed,
                "capture_coverage": round(coverage, 4),
                "requested_hours": requested_hours,
                "reason": None,
            }
    return {
        "hours": 0,
        "cohort_start_ts": None,
        "eligible_end_ts": eligible_end,
        "requested_hours": requested_hours,
        "reason": (
            "No label-resolved lookback window meets the dual-exchange "
            f"heartbeat threshold of {minimum_coverage:.0%}."
        ),
    }


def training_readiness(
    manifest: dict[str, Any],
    *,
    train_days: int,
    validation_days: int,
    test_days: int,
    step_days: int,
) -> dict[str, Any]:
    """Explain whether strict walk-forward evaluation may run."""
    if manifest.get("complete") is not True:
        missing = [
            name
            for name, check in (manifest.get("checks") or {}).items()
            if check.get("complete") is not True
        ]
        return {
            "ready": False,
            "status": "blocked_data",
            "missing_checks": missing,
            "reason": "The forward cohort is not complete for every required family.",
        }
    period = manifest.get("period") or {}
    min_ts = period.get("min_ts")
    max_ts = period.get("max_ts")
    required_days = train_days + validation_days + test_days + step_days
    calendar_span_days = (
        max(0.0, (int(max_ts) - int(min_ts)) / 86_400_000)
        if min_ts is not None and max_ts is not None
        else 0.0
    )
    observed_days = float(period.get("effective_snapshot_days", calendar_span_days))
    if observed_days < required_days or calendar_span_days < required_days:
        return {
            "ready": False,
            "status": "accumulating_history",
            "observed_days": round(observed_days, 2),
            "calendar_span_days": round(calendar_span_days, 2),
            "required_days": required_days,
            "reason": (
                "At least two non-overlapping walk-forward folds require both "
                "calendar span and effective snapshot coverage."
            ),
        }
    return {
        "ready": True,
        "status": "ready",
        "observed_days": round(observed_days, 2),
        "calendar_span_days": round(calendar_span_days, 2),
        "required_days": required_days,
    }


def write_refresh_report(report: dict[str, Any], output_path: str) -> None:
    """Atomically persist progress so schedulers can inspect interrupted jobs."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
    temporary.replace(path)


def _run_step(
    report: dict[str, Any],
    name: str,
    callback: Callable[[], Any],
    output_path: str,
) -> Any | None:
    started = time.monotonic()
    try:
        result = callback()
        report["steps"][name] = {
            "status": "complete",
            "duration_seconds": round(time.monotonic() - started, 3),
            "result": result,
        }
        return result
    except Exception as exc:  # keep later independent refreshes running
        report["steps"][name] = {
            "status": "failed",
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }
        return None
    finally:
        write_refresh_report(report, output_path)


def run_daily_refresh(
    *,
    symbols: list[str] | None = None,
    archive_start: date,
    archive_end: date,
    market_db: str = "runtime/vision_metrics.db",
    onchain_db: str = "runtime/onchain_data.db",
    liquidation_db: str = "runtime/liquidation_history.db",
    cross_venue_db: str = "runtime/cross_venue.db",
    forward_training_db: str = "runtime/training_forward.db",
    completeness_path: str = "runtime/data_completeness_forward.json",
    report_path: str = "runtime/daily_refresh_status.json",
    vision_cache_dir: str = "runtime/vision_cache",
    supplemental_cache_dir: str = "runtime/vision_supplemental_cache",
    hyperliquid_cache_dir: str = "runtime/hyperliquid_liquidation_cache",
    cross_venue_cache_dir: str = "runtime/cross_venue_cache",
    forward_lookback_hours: int = 36,
    train_days: int = 90,
    validation_days: int = 15,
    test_days: int = 15,
    step_days: int = 30,
    run_training_when_ready: bool = True,
) -> dict[str, Any]:
    """Refresh free sources and maintain a strict forward-only training cohort."""
    from liquidity_signal.ai.gbdt_trainer import walk_forward_gbdt
    from liquidity_signal.data.binance_client import BinanceFuturesClient
    from liquidity_signal.data.binance_vision import BinanceVisionClient, VisionMetricsStore
    from liquidity_signal.data.binance_vision_supplemental import (
        BinanceVisionSupplementalClient,
        VisionSupplementalStore,
        build_completeness_manifest,
        write_manifest,
    )
    from liquidity_signal.data.hyperliquid_archive import (
        HyperliquidArchiveClient,
        ingest_hyperliquid_range,
    )
    from liquidity_signal.data.cross_venue import (
        BinanceSpotArchiveClient,
        BybitPublicClient,
        CrossVenueStore,
        require_cross_venue_coverage,
    )
    from liquidity_signal.data.open_onchain import OpenOnchainClient, OpenOnchainStore
    from liquidity_signal.service.engine import SignalEngine
    from liquidity_signal.service.liquidation_store import LiquidationStore

    selected = [symbol.upper() for symbol in (symbols or list(DEFAULT_SYMBOLS))]
    recovery = archive_recovery_window(
        archive_start,
        archive_end,
        forward_training_db=forward_training_db,
        market_db=market_db,
        onchain_db=onchain_db,
        symbols=selected,
    )
    archive_start = date.fromisoformat(recovery["effective_start"])
    report: dict[str, Any] = {
        "version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "archive_window": {"start": archive_start.isoformat(), "end": archive_end.isoformat()},
        "archive_recovery": recovery,
        "symbols": selected,
        "forward_training_db": forward_training_db,
        "steps": {},
    }
    write_refresh_report(report, report_path)

    def refresh_positioning() -> list[dict[str, Any]]:
        client = BinanceVisionClient(cache_dir=vision_cache_dir)
        store = VisionMetricsStore(market_db)
        try:
            return [
                store.ingest(client, symbol, archive_start, archive_end)
                for symbol in selected
            ]
        finally:
            client.close()
            store.close()

    _run_step(report, "binance_positioning", refresh_positioning, report_path)

    def refresh_supplemental() -> list[dict[str, Any]]:
        client = BinanceVisionSupplementalClient(cache_dir=supplemental_cache_dir)
        store = VisionSupplementalStore(market_db)
        results = []
        current_month = archive_end.replace(day=1)
        prior_month_end = current_month - timedelta(days=1)
        prior_month_start = prior_month_end.replace(day=1)
        try:
            for symbol in selected:
                results.append({
                    "symbol": symbol,
                    "depth": store.ingest_depth_range(client, symbol, archive_start, archive_end),
                    # Recheck the prior month daily until Binance publishes it;
                    # completed ledger entries make later runs cache-only.
                    "funding": store.ingest_funding_range(
                        client, symbol, prior_month_start, prior_month_end
                    ),
                    "trade_flow": store.ingest_trade_flow_range(
                        client, symbol, archive_start, archive_end
                    ),
                })
            return results
        finally:
            client.close()
            store.close()

    _run_step(report, "binance_depth_and_flow", refresh_supplemental, report_path)

    def refresh_current_funding() -> list[dict[str, Any]]:
        client = BinanceFuturesClient(timeout=60.0)
        store = VisionSupplementalStore(market_db)
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start_ts = int(month_start.timestamp() * 1000)
        end_ts = int(now.timestamp() * 1000)
        try:
            return [
                store.ingest_public_funding_range(client, symbol, start_ts, end_ts)
                for symbol in selected
            ]
        finally:
            client.close()
            store.close()

    _run_step(report, "binance_current_funding", refresh_current_funding, report_path)

    def refresh_cross_venue() -> list[dict[str, Any]]:
        bybit = BybitPublicClient(
            cache_dir=cross_venue_cache_dir,
            download_workers=3,
        )
        binance = BinanceSpotArchiveClient(cache_dir=cross_venue_cache_dir)
        store = CrossVenueStore(cross_venue_db)
        results = []
        try:
            for symbol in selected:
                results.append({
                    "symbol": symbol,
                    "bybit_trades": store.ingest_bybit_trades_range(
                        bybit, symbol, archive_start, archive_end
                    ),
                    "bybit_positioning": store.ingest_bybit_positioning_range(
                        bybit, symbol, archive_start, archive_end
                    ),
                    "binance_spot": store.ingest_binance_spot_range(
                        binance, symbol, archive_start, archive_end
                    ),
                })
            return results
        finally:
            bybit.close()
            binance.close()
            store.close()

    _run_step(report, "cross_venue", refresh_cross_venue, report_path)

    def refresh_onchain() -> dict[str, Any]:
        client = OpenOnchainClient()
        store = OpenOnchainStore(onchain_db)
        try:
            return store.ingest(client, archive_start, archive_end)
        finally:
            client.close()
            store.close()

    _run_step(report, "open_onchain", refresh_onchain, report_path)

    def refresh_hyperliquid() -> dict[str, Any]:
        client = HyperliquidArchiveClient(cache_dir=hyperliquid_cache_dir)
        store = LiquidationStore(Path(liquidation_db))
        try:
            return ingest_hyperliquid_range(
                client, store, archive_start, archive_end, selected
            )
        finally:
            client.close()
            store.close()

    _run_step(report, "hyperliquid_liquidations", refresh_hyperliquid, report_path)

    safe_window = capture_safe_lookback_hours(
        liquidation_db, selected, forward_lookback_hours
    )
    report["capture_cohort"] = safe_window
    write_refresh_report(report, report_path)

    def refresh_forward_training() -> dict[str, Any]:
        hours = int(safe_window["hours"])
        if hours < 1:
            return {"status": "waiting_for_capture", **safe_window}
        store = LiquidationStore(Path(forward_training_db))
        engine = SignalEngine(liquidation_store=store)
        try:
            result = engine.backfill_historical_training_batch(
                selected,
                lookback_hours=hours,
                step_minutes=5,
                max_samples_per_symbol=hours * 12,
                include_stored_liquidation=False,
                include_historical_positioning=True,
            )
            return result.model_dump(mode="json")
        finally:
            engine.close()

    _run_step(report, "forward_training_cohort", refresh_forward_training, report_path)

    def audit_completeness() -> dict[str, Any]:
        # These constructors ensure additive tables exist even after a partial run.
        market_store = VisionSupplementalStore(market_db)
        market_store.close()
        liquidation_store = LiquidationStore(Path(liquidation_db))
        liquidation_store.close()
        forward_store = LiquidationStore(Path(forward_training_db))
        forward_store.close()
        manifest = build_completeness_manifest(
            forward_training_db,
            market_db,
            liquidation_db,
            selected,
            onchain_db=onchain_db,
        )
        cross_store = CrossVenueStore(cross_venue_db)
        try:
            require_cross_venue_coverage(
                manifest, cross_store, forward_training_db, selected
            )
        finally:
            cross_store.close()
        write_manifest(manifest, completeness_path)
        return manifest

    manifest = _run_step(report, "forward_completeness", audit_completeness, report_path)
    readiness = training_readiness(
        manifest or {},
        train_days=train_days,
        validation_days=validation_days,
        test_days=test_days,
        step_days=step_days,
    )
    report["strict_training"] = readiness
    if readiness["ready"] and run_training_when_ready:
        dated_output = (
            f"runtime/models/gbdt-forward-{archive_end.isoformat()}-60m"
        )

        def train() -> dict[str, Any]:
            return walk_forward_gbdt(
                forward_training_db,
                dated_output,
                horizon_minutes=60,
                vision_db=market_db,
                onchain_db=onchain_db,
                liquidation_db=liquidation_db,
                cross_venue_db=cross_venue_db,
                train_days=train_days,
                validation_days=validation_days,
                test_days=test_days,
                step_days=step_days,
            )

        evaluation = _run_step(report, "strict_walk_forward", train, report_path)
        report["strict_training"]["evaluation_path"] = (
            str(Path(dated_output) / "walk_forward_evaluation.json")
            if evaluation is not None
            else None
        )
        report["strict_training"]["deployment_gate_passed"] = bool(
            evaluation and evaluation.get("deployment_gate", {}).get("passed")
        )
    elif readiness["ready"]:
        report["strict_training"]["status"] = "ready_not_run"

    failed_steps = [
        name for name, step in report["steps"].items() if step["status"] == "failed"
    ]
    report["status"] = "failed" if failed_steps else "complete"
    report["failed_steps"] = failed_steps
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_refresh_report(report, report_path)
    return report
