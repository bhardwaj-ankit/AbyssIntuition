from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from liquidity_signal.universe import ACTIVE_SYMBOLS_CSV, training_symbols
from liquidity_signal.ai.local_lora_trainer import evaluate_local_lora, train_local_lora
from liquidity_signal.ai.evaluate import evaluate_signal_prior
from liquidity_signal.ai.deployment import assess_lora_candidate, deployment_status
from liquidity_signal.ai.tabular_trainer import train_signal_validator, train_tabular_reviewer
from liquidity_signal.ai.gbdt_trainer import train_gbdt_dual_head, walk_forward_gbdt
from liquidity_signal.service.engine import SignalEngine

app = typer.Typer(help="Binance liquidity signal CLI")
console = Console()


def _require_complete_data(manifest_path: str, allow_incomplete_data: bool) -> None:
    if allow_incomplete_data:
        return
    path = Path(manifest_path)
    if not path.exists():
        raise typer.BadParameter(
            f"Completeness manifest {manifest_path} does not exist; run data-completeness."
        )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("complete") is not True:
        missing = [
            name for name, check in (manifest.get("checks") or {}).items()
            if check.get("complete") is not True
        ]
        raise typer.BadParameter(
            "Training data is incomplete for: " + ", ".join(missing)
            + ". Use --allow-incomplete-data only for research/shadow evaluation."
        )


def _make_engine(db_path: str | None = None) -> SignalEngine:
    if db_path:
        os.environ["LIQUIDATION_STORE_PATH"] = db_path
    return SignalEngine()


@app.command()
def signal(symbol: str = "BTCUSDT") -> None:
    engine = _make_engine()
    result = engine.generate_signal(symbol.upper())

    table = Table(title=f"Signal for {result.symbol}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Direction", result.direction.value)
    table.add_row("Confidence", f"{result.confidence:.2f}")
    table.add_row("Current Price", f"{result.current_price:.2f}")
    table.add_row("TP", f"{result.tp:.2f}")
    table.add_row("SL", f"{result.sl:.2f}")
    table.add_row("Reasons", ", ".join(result.reasons))

    console.print(table)


@app.command("dataset-summary")
def dataset_summary(symbol: str = "BTCUSDT", db_path: str | None = None) -> None:
    engine = _make_engine(db_path)
    dataset = engine.load_training_dataset(symbol.upper(), limit=50, resolved_only=False)
    summary = dataset.summary

    table = Table(title=f"Training Dataset Summary for {symbol.upper()}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Snapshots", str(summary.total_snapshots))
    table.add_row("Decisions", str(summary.total_decisions))
    table.add_row("Labels", str(summary.total_labels))
    table.add_row("Resolved", str(summary.resolved_labels))
    table.add_row("Pending", str(summary.pending_labels))
    table.add_row("Long", str(summary.long_labels))
    table.add_row("Short", str(summary.short_labels))
    table.add_row("Flat", str(summary.flat_labels))
    console.print(table)


@app.command("backfill-training")
def backfill_training(
    symbol: str = "BTCUSDT",
    lookback_hours: int = 24,
    step_minutes: int = 5,
    max_samples: int = 400,
    include_stored_liquidation: bool = True,
    include_historical_positioning: bool = True,
    db_path: str | None = None,
) -> None:
    engine = _make_engine(db_path)
    result = engine.backfill_historical_training(
        symbol.upper(),
        lookback_hours=lookback_hours,
        step_minutes=step_minutes,
        max_samples=max_samples,
        include_stored_liquidation=include_stored_liquidation,
        include_historical_positioning=include_historical_positioning,
    )
    table = Table(title=f"Historical Training Backfill for {symbol.upper()}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Lookback Hours", str(result.lookback_hours))
    table.add_row("Step Minutes", str(result.step_minutes))
    table.add_row("Samples Attempted", str(result.samples_attempted))
    table.add_row("Snapshots Created", str(result.snapshots_created))
    table.add_row("Resolved Labels", str(result.resolved_labels))
    table.add_row("Skipped Samples", str(result.skipped_samples))
    table.add_row("Used Liq Snapshots", str(result.used_liquidation_snapshots))
    console.print(table)
    for note in result.notes:
        console.print(f"- {note}")


@app.command("backfill-training-batch")
def backfill_training_batch(
    symbols: str = ACTIVE_SYMBOLS_CSV,
    lookback_hours: int = 24,
    step_minutes: int = 5,
    max_samples_per_symbol: int = 400,
    include_stored_liquidation: bool = True,
    include_historical_positioning: bool = True,
    db_path: str | None = None,
) -> None:
    engine = _make_engine(db_path)
    symbol_list = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    result = engine.backfill_historical_training_batch(
        symbol_list,
        lookback_hours=lookback_hours,
        step_minutes=step_minutes,
        max_samples_per_symbol=max_samples_per_symbol,
        include_stored_liquidation=include_stored_liquidation,
        include_historical_positioning=include_historical_positioning,
    )
    table = Table(title="Historical Training Backfill Batch")
    table.add_column("Symbol")
    table.add_column("Snapshots")
    table.add_column("Resolved")
    table.add_column("Skipped")
    table.add_column("Liq Snapshots")
    for item in result.items:
        table.add_row(
            item.symbol,
            str(item.snapshots_created),
            str(item.resolved_labels),
            str(item.skipped_samples),
            str(item.used_liquidation_snapshots),
        )
    console.print(table)


@app.command("export-lora")
def export_lora(
    symbol: str = "BTCUSDT",
    horizon_minutes: int = 15,
    limit: int = 1000,
    output_dir: str = "runtime/lora_exports",
    decision_action: str | None = None,
    balance_mode: str = "none",
    db_path: str | None = None,
) -> None:
    engine = _make_engine(db_path)
    export = engine.export_lora_training_dataset(
        symbol.upper(),
        horizon_minutes=horizon_minutes,
        limit=limit,
        decision_action=decision_action,
        balance_mode=balance_mode,
    )
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    balance_suffix = "" if balance_mode == "none" else f"_{balance_mode}"
    stem = f"{symbol.upper().lower()}_{horizon_minutes}m{balance_suffix}"
    manifest_path = target / f"{stem}_manifest.json"
    chatml_path = target / f"{stem}_chatml.jsonl"
    prompt_path = target / f"{stem}_prompt_completion.jsonl"

    manifest_path.write_text(export.model_dump_json(indent=2), encoding="utf-8")
    with chatml_path.open("w", encoding="utf-8") as handle:
        for row in export.examples:
            handle.write(
                json.dumps(
                    {
                        "messages": row.messages,
                        "split": row.split,
                        "metadata": {
                            **row.metadata,
                            "split": row.split,
                            "label_action": row.label_action.value,
                        },
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
    with prompt_path.open("w", encoding="utf-8") as handle:
        for row in export.examples:
            handle.write(
                json.dumps(
                    {
                        "prompt": row.prompt,
                        "completion": row.completion,
                        "split": row.split,
                        "metadata": row.metadata,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )

    console.print(
        f"Exported {export.summary.total_examples} examples to {chatml_path} and {prompt_path}"
    )


@app.command("prepare-mlx-lora")
def prepare_mlx_lora(
    horizon_minutes: int = 5,
    symbols: str = ACTIVE_SYMBOLS_CSV,
    input_dir: str = "runtime/lora_exports",
    output_dir: str = "runtime/mlx_lora_data",
    balance_mode: str = "undersample_majority",
) -> None:
    symbol_list = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    target = Path(output_dir) / f"crypto_{horizon_minutes}m_{balance_mode}"
    target.mkdir(parents=True, exist_ok=True)
    handles = {
        "train": (target / "train.jsonl").open("w", encoding="utf-8"),
        "validation": (target / "valid.jsonl").open("w", encoding="utf-8"),
        "test": (target / "test.jsonl").open("w", encoding="utf-8"),
    }
    counts = {"train": 0, "validation": 0, "test": 0}
    try:
        for symbol in symbol_list:
            stem = f"{symbol.lower()}_{horizon_minutes}m_{balance_mode}_chatml.jsonl"
            source = Path(input_dir) / stem
            if not source.exists():
                raise typer.BadParameter(f"Missing export file: {source}")
            with source.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    split = row.get("metadata", {}).get("split", row.get("split", "train"))
                    if split not in handles:
                        split = "train"
                    handles[split].write(json.dumps({"messages": row["messages"]}, separators=(",", ":")) + "\n")
                    counts[split] += 1
    finally:
        for handle in handles.values():
            handle.close()

    table = Table(title=f"MLX LoRA Data: {target}")
    table.add_column("Split")
    table.add_column("Rows")
    table.add_row("train.jsonl", str(counts["train"]))
    table.add_row("valid.jsonl", str(counts["validation"]))
    table.add_row("test.jsonl", str(counts["test"]))
    console.print(table)


@app.command("combine-lora-exports")
def combine_lora_exports(
    horizon_minutes: int = 15,
    symbols: str = ACTIVE_SYMBOLS_CSV,
    input_dir: str = "runtime/lora_exports",
    output_path: str = "runtime/lora_exports/crypto_combined_chatml.jsonl",
    balance_mode: str = "undersample_majority",
) -> None:
    """Combine per-symbol exports while retaining split metadata for Trainer."""
    symbol_list = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    counts = {"train": 0, "validation": 0, "test": 0}
    with target.open("w", encoding="utf-8") as output:
        for symbol in symbol_list:
            balance_suffix = "" if balance_mode == "none" else f"_{balance_mode}"
            source = Path(input_dir) / (
                f"{symbol.lower()}_{horizon_minutes}m{balance_suffix}_chatml.jsonl"
            )
            if not source.exists():
                raise typer.BadParameter(f"Missing export file: {source}")
            with source.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    split = row.get("metadata", {}).get("split", row.get("split", "train"))
                    counts[split if split in counts else "train"] += 1
                    output.write(json.dumps(row, separators=(",", ":")) + "\n")
    console.print(f"Combined {sum(counts.values())} rows into {target}: {counts}")


@app.command("train-lora")
def train_lora(
    config_path: str = "runtime/lora_training_config.example.json",
) -> None:
    summary = train_local_lora(config_path)
    console.print(
        f"Training complete for {summary['symbol']} {summary['horizon_minutes']}m with {summary['train_rows']} train rows"
    )


@app.command("evaluate-lora")
def evaluate_lora(
    config_path: str = "runtime/lora_training_config.example.json",
    split: str = "test",
) -> None:
    console.print_json(data=evaluate_local_lora(config_path, split=split))


@app.command("assess-lora-deployment")
def assess_lora_deployment(
    evaluation_path: str,
    manifest_path: str = "runtime/models/deployment_manifest.json",
    minimum_accuracy: float = 0.45,
    minimum_examples: int = 500,
) -> None:
    """Assess a held-out result and write a fail-closed deployment manifest."""
    console.print_json(data=assess_lora_candidate(
        evaluation_path,
        manifest_path=manifest_path,
        minimum_accuracy=minimum_accuracy,
        minimum_examples=minimum_examples,
    ))


@app.command("model-deployment-status")
def model_deployment_status(
    manifest_path: str = "runtime/models/deployment_manifest.json",
) -> None:
    console.print_json(data=deployment_status(manifest_path))


@app.command("evaluate-strategy-dataset")
def evaluate_strategy_dataset(
    pattern: str = "runtime/lora_exports/*_undersample_majority_chatml.jsonl",
) -> None:
    """Audit stored signal directions against resolved labels."""
    console.print_json(data=evaluate_signal_prior(pattern))


@app.command("train-tabular-reviewer")
def train_tabular_reviewer_command(
    dataset_path: str = "runtime/lora_exports_v2/crypto_15m_raw_chatml.jsonl",
    output_dir: str = "runtime/models/tabular-reviewer-15m-v2",
) -> None:
    console.print_json(data=train_tabular_reviewer(dataset_path, output_dir))


@app.command("train-signal-validator")
def train_signal_validator_command(
    dataset_path: str = "runtime/lora_exports_v2/crypto_15m_raw_chatml.jsonl",
    output_dir: str = "runtime/models/signal-validator-15m-v2",
    train_window_days: int | None = None,
) -> None:
    console.print_json(data=train_signal_validator(
        dataset_path, output_dir, train_window_days=train_window_days
    ))


@app.command("train-gbdt")
def train_gbdt_command(
    db_path: str = "runtime/training_v6.db",
    horizon_minutes: int = 60,
    output_dir: str | None = None,
    vision_db: str | None = None,
    onchain_db: str | None = "runtime/onchain_data.db",
    liquidation_db: str | None = "runtime/liquidation_history.db",
    completeness_manifest: str = "runtime/data_completeness.json",
    allow_incomplete_data: bool = False,
) -> None:
    """Train the leakage-free two-head GBDT (direction + return range) on outcomes.

    Pass --vision-db to fill OI/positioning features from ingested Binance Vision
    metrics (see the ingest-vision-metrics command).
    """
    _require_complete_data(completeness_manifest, allow_incomplete_data)
    target = output_dir or f"runtime/models/gbdt-dual-{horizon_minutes}m-v2"
    console.print_json(data=train_gbdt_dual_head(
        db_path, target, horizon_minutes=horizon_minutes, vision_db=vision_db,
        onchain_db=onchain_db, liquidation_db=liquidation_db,
    ))


@app.command("walk-forward-gbdt")
def walk_forward_gbdt_command(
    db_path: str = "runtime/training_v6.db",
    horizon_minutes: int = 60,
    output_dir: str | None = None,
    vision_db: str | None = "runtime/vision_metrics.db",
    onchain_db: str | None = "runtime/onchain_data.db",
    liquidation_db: str | None = "runtime/liquidation_history.db",
    train_days: int = 90,
    validation_days: int = 15,
    test_days: int = 15,
    step_days: int = 30,
    completeness_manifest: str = "runtime/data_completeness.json",
    allow_incomplete_data: bool = False,
) -> None:
    """Evaluate the GBDT over rolling, non-overlapping chronological tests."""
    _require_complete_data(completeness_manifest, allow_incomplete_data)
    target = output_dir or f"runtime/models/gbdt-walk-forward-{horizon_minutes}m-v2"
    console.print_json(data=walk_forward_gbdt(
        db_path,
        target,
        horizon_minutes=horizon_minutes,
        vision_db=vision_db,
        onchain_db=onchain_db,
        liquidation_db=liquidation_db,
        train_days=train_days,
        validation_days=validation_days,
        test_days=test_days,
        step_days=step_days,
    ))


@app.command("train-archive-baseline")
def train_archive_baseline_command(
    db_path: str = "runtime/training_v6.db",
    horizon_minutes: int = 60,
    output_dir: str | None = None,
    vision_db: str = "runtime/vision_metrics.db",
    onchain_db: str = "runtime/onchain_data.db",
    liquidation_audit_db: str = "runtime/liquidation_history.db",
    cross_venue_db: str = "runtime/cross_venue.db",
    completeness_manifest: str | None = None,
    symbols: str = ACTIVE_SYMBOLS_CSV,
    snapshot_interval_minutes: int = 60,
    train_days: int = 90,
    validation_days: int = 15,
    test_days: int = 15,
    step_days: int = 30,
    include_cryptohft: bool = False,
    include_hyperliquid: bool = False,
    include_cross_venue: bool = False,
    minimum_cross_venue_coverage: float = typer.Option(0.95, min=0.0, max=1.0),
) -> None:
    """Audit and evaluate a leakage-safe, backfillable archive model."""
    from liquidity_signal.data.binance_vision_supplemental import (
        VisionSupplementalStore,
        build_completeness_manifest,
        write_manifest,
    )
    from liquidity_signal.data.cross_venue import (
        CrossVenueStore,
        require_cross_venue_coverage,
    )
    from liquidity_signal.data.daily_refresh import training_readiness
    from liquidity_signal.service.liquidation_store import LiquidationStore

    profile_parts = ["archive"]
    if include_cross_venue:
        profile_parts.append("cross_venue")
    if include_cryptohft:
        profile_parts.append("cryptohft")
    if include_hyperliquid:
        profile_parts.append("hyperliquid")
    if len(profile_parts) == 1:
        profile_parts.append("only")
    data_profile = "_".join(profile_parts)
    target = output_dir or (
        f"runtime/models/gbdt-{data_profile.replace('_', '-')}-{horizon_minutes}m-v1"
    )
    manifest_path = completeness_manifest or (
        f"runtime/data_completeness_{data_profile}.json"
    )
    selected = list(training_symbols([item for item in symbols.split(",") if item.strip()]))
    market_store = VisionSupplementalStore(vision_db)
    market_store.close()
    audit_store = LiquidationStore(Path(liquidation_audit_db))
    audit_store.close()
    manifest = build_completeness_manifest(
        db_path,
        vision_db,
        liquidation_audit_db,
        selected,
        onchain_db=onchain_db,
        require_live_liquidations=False,
        expected_snapshot_interval_minutes=snapshot_interval_minutes,
        data_profile=data_profile,
    )
    if include_cross_venue:
        cross_store = CrossVenueStore(cross_venue_db)
        try:
            require_cross_venue_coverage(
                manifest,
                cross_store,
                db_path,
                selected,
                minimum_coverage=minimum_cross_venue_coverage,
            )
        finally:
            cross_store.close()
    readiness = training_readiness(
        manifest,
        train_days=train_days,
        validation_days=validation_days,
        test_days=test_days,
        step_days=step_days,
    )
    manifest["training_readiness"] = readiness
    write_manifest(manifest, manifest_path)
    if readiness.get("ready") is not True:
        raise typer.BadParameter(
            f"Archive-only data is not ready: {readiness.get('reason', 'unknown reason')}"
        )
    evaluation = walk_forward_gbdt(
        db_path,
        target,
        horizon_minutes=horizon_minutes,
        vision_db=vision_db,
        onchain_db=onchain_db,
        liquidation_db=(
            liquidation_audit_db
            if include_cryptohft or include_hyperliquid
            else None
        ),
        cross_venue_db=cross_venue_db if include_cross_venue else None,
        data_profile=data_profile,
        symbols=selected,
        train_days=train_days,
        validation_days=validation_days,
        test_days=test_days,
        step_days=step_days,
    )
    console.print_json(data={
        "data_profile": data_profile,
        "completeness_manifest": manifest_path,
        "readiness": readiness,
        "evaluation": evaluation,
    })


@app.command("ingest-cross-venue")
def ingest_cross_venue_command(
    symbols: str = ACTIVE_SYMBOLS_CSV,
    start: str = typer.Option(..., help="Start date YYYY-MM-DD (UTC)."),
    end: str = typer.Option(..., help="End date YYYY-MM-DD (UTC, inclusive)."),
    db_path: str = "runtime/cross_venue.db",
    cache_dir: str = "runtime/cross_venue_cache",
    include_bybit_trades: bool = True,
    include_bybit_positioning: bool = True,
    include_binance_spot: bool = True,
    retain_trade_archives: bool = False,
    download_workers: int = typer.Option(4, min=1, max=8),
) -> None:
    """Backfill free Bybit derivatives and Binance spot flow with provenance."""
    from datetime import date

    from liquidity_signal.data.cross_venue import (
        BinanceSpotArchiveClient,
        BybitPublicClient,
        CrossVenueStore,
    )

    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    if end_day < start_day:
        raise typer.BadParameter("end must be on or after start")
    selected = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    bybit = BybitPublicClient(
        cache_dir=cache_dir,
        retain_trade_archives=retain_trade_archives,
        download_workers=download_workers,
    )
    binance = BinanceSpotArchiveClient(cache_dir=cache_dir)
    store = CrossVenueStore(db_path)
    results = []
    try:
        for symbol in selected:
            summary: dict[str, object] = {"symbol": symbol}
            if include_bybit_trades:
                summary["bybit_trades"] = store.ingest_bybit_trades_range(
                    bybit, symbol, start_day, end_day
                )
            if include_bybit_positioning:
                summary["bybit_positioning"] = store.ingest_bybit_positioning_range(
                    bybit, symbol, start_day, end_day
                )
            if include_binance_spot:
                summary["binance_spot"] = store.ingest_binance_spot_range(
                    binance, symbol, start_day, end_day
                )
            summary["coverage"] = {
                "bybit_trades": store.coverage(
                    "bybit", "perpetual_trades", symbol
                ),
                "bybit_positioning": store.coverage(
                    "bybit", "positioning", symbol
                ),
                "binance_spot_month": store.coverage(
                    "binance", "spot_kline5m_month", symbol
                ),
                "binance_spot_day": store.coverage(
                    "binance", "spot_kline5m_day", symbol
                ),
            }
            results.append(summary)
            console.print(f"{symbol}: cross-venue archives processed")
    finally:
        bybit.close()
        binance.close()
        store.close()
    console.print_json(data={
        "db_path": db_path,
        "start": start,
        "end": end,
        "results": results,
    })


@app.command("ingest-vision-metrics")
def ingest_vision_metrics(
    symbols: str = ACTIVE_SYMBOLS_CSV,
    start: str = typer.Option(..., help="Start date YYYY-MM-DD (UTC)."),
    end: str = typer.Option(..., help="End date YYYY-MM-DD (UTC, inclusive)."),
    db_path: str = "runtime/vision_metrics.db",
    cache_dir: str = "runtime/vision_cache",
) -> None:
    """Backfill OI/positioning metrics from Binance Vision daily bulk CSVs.

    These endpoints retain years of history, unlike the 30-day futures-data API,
    so this is how the model earns back the missing positioning signals.
    """
    from datetime import date

    from liquidity_signal.data.binance_vision import BinanceVisionClient, VisionMetricsStore

    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    client = BinanceVisionClient(cache_dir=cache_dir)
    store = VisionMetricsStore(db_path)
    results = []
    try:
        for symbol in [s.strip().upper() for s in symbols.split(",") if s.strip()]:
            summary = store.ingest(client, symbol, start_day, end_day)
            summary["coverage"] = store.coverage(symbol)
            results.append(summary)
            console.print(f"{symbol}: {summary['rows_stored']} rows across {summary['days_with_data']} days")
    finally:
        client.close()
        store.close()
    console.print_json(data={"db_path": db_path, "results": results})


@app.command("ingest-vision-supplemental")
def ingest_vision_supplemental(
    symbols: str = ACTIVE_SYMBOLS_CSV,
    start: str = typer.Option(..., help="Start date YYYY-MM-DD (UTC)."),
    end: str = typer.Option(..., help="End date YYYY-MM-DD (UTC, inclusive)."),
    db_path: str = "runtime/vision_metrics.db",
    cache_dir: str = "runtime/vision_supplemental_cache",
    include_depth: bool = True,
    include_funding: bool = True,
    include_trade_flow: bool = True,
) -> None:
    """Backfill free Binance depth, funding, and observed taker-flow archives."""
    from datetime import date

    from liquidity_signal.data.binance_vision_supplemental import (
        BinanceVisionSupplementalClient,
        VisionSupplementalStore,
    )

    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    client = BinanceVisionSupplementalClient(cache_dir=cache_dir)
    store = VisionSupplementalStore(db_path)
    results = []
    try:
        for symbol in [item.strip().upper() for item in symbols.split(",") if item.strip()]:
            summary: dict[str, object] = {"symbol": symbol}
            if include_depth:
                summary["depth"] = store.ingest_depth_range(
                    client, symbol, start_day, end_day
                )
            if include_funding:
                summary["funding"] = store.ingest_funding_range(
                    client, symbol, start_day, end_day
                )
            if include_trade_flow:
                summary["trade_flow"] = store.ingest_trade_flow_range(
                    client, symbol, start_day, end_day
                )
            summary["depth_coverage"] = store.coverage("vision_depth_features", symbol)
            summary["funding_coverage"] = store.coverage("vision_funding", symbol)
            summary["trade_flow_coverage"] = store.coverage("vision_trade_flow", symbol)
            results.append(summary)
            console.print(f"{symbol}: supplemental archives processed")
    finally:
        client.close()
        store.close()
    console.print_json(data={"db_path": db_path, "results": results})


@app.command("ingest-public-funding")
def ingest_public_funding(
    symbols: str = ACTIVE_SYMBOLS_CSV,
    start: str = typer.Option(..., help="Start date YYYY-MM-DD (UTC)."),
    end: str = typer.Option(..., help="End date YYYY-MM-DD (UTC, inclusive)."),
    db_path: str = "runtime/vision_metrics.db",
) -> None:
    """Fill current funding history from Binance's free public REST API."""
    from datetime import date, datetime, timedelta, timezone

    from liquidity_signal.data.binance_client import BinanceFuturesClient
    from liquidity_signal.data.binance_vision_supplemental import VisionSupplementalStore

    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    if end_day < start_day:
        raise typer.BadParameter("end must be on or after start")
    start_dt = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(
        end_day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    ) - timedelta(milliseconds=1)
    now = datetime.now(timezone.utc)
    if start_dt > now:
        raise typer.BadParameter("start cannot be in the future")
    end_dt = min(end_dt, now)

    client = BinanceFuturesClient(timeout=60.0)
    store = VisionSupplementalStore(db_path)
    results = []
    try:
        for symbol in [item.strip().upper() for item in symbols.split(",") if item.strip()]:
            result = store.ingest_public_funding_range(
                client,
                symbol,
                int(start_dt.timestamp() * 1000),
                int(end_dt.timestamp() * 1000),
            )
            results.append(result)
            console.print(f"{symbol}: {result['stored_rows']} funding rows stored")
    finally:
        client.close()
        store.close()
    console.print_json(data={"db_path": db_path, "results": results})


@app.command("data-completeness")
def data_completeness_command(
    training_db: str = "runtime/training_v6.db",
    market_db: str = "runtime/vision_metrics.db",
    liquidation_db: str = "runtime/liquidation_history.db",
    cross_venue_db: str = "runtime/cross_venue.db",
    onchain_db: str = "runtime/onchain_data.db",
    symbols: str = ACTIVE_SYMBOLS_CSV,
    output_path: str = "runtime/data_completeness.json",
) -> None:
    """Write a fail-closed manifest for every required historical data family."""
    from liquidity_signal.data.binance_vision_supplemental import (
        VisionSupplementalStore,
        build_completeness_manifest,
        write_manifest,
    )
    from liquidity_signal.data.cross_venue import (
        CrossVenueStore,
        require_cross_venue_coverage,
    )
    from liquidity_signal.service.liquidation_store import LiquidationStore

    # Ensure additive supplemental tables exist before auditing them.
    store = VisionSupplementalStore(market_db)
    store.close()
    liquidation_store = LiquidationStore(Path(liquidation_db))
    liquidation_store.close()
    manifest = build_completeness_manifest(
        training_db,
        market_db,
        liquidation_db,
        [item.strip().upper() for item in symbols.split(",") if item.strip()],
        onchain_db=onchain_db,
    )
    cross_store = CrossVenueStore(cross_venue_db)
    try:
        require_cross_venue_coverage(
            manifest,
            cross_store,
            training_db,
            [item.strip().upper() for item in symbols.split(",") if item.strip()],
        )
    finally:
        cross_store.close()
    write_manifest(manifest, output_path)
    console.print_json(data=manifest)


@app.command("daily-data-refresh")
def daily_data_refresh_command(
    symbols: str = ACTIVE_SYMBOLS_CSV,
    through: str | None = typer.Option(
        None, help="Last archive date YYYY-MM-DD; defaults to yesterday UTC."
    ),
    retry_days: int = typer.Option(
        3, min=1, help="Recheck this many archive days to recover late files."
    ),
    market_db: str = "runtime/vision_metrics.db",
    onchain_db: str = "runtime/onchain_data.db",
    liquidation_db: str = "runtime/liquidation_history.db",
    cross_venue_db: str = "runtime/cross_venue.db",
    forward_training_db: str = "runtime/training_forward.db",
    completeness_path: str = "runtime/data_completeness_forward.json",
    report_path: str = "runtime/daily_refresh_status.json",
    forward_lookback_hours: int = typer.Option(36, min=5),
    run_training_when_ready: bool = True,
    include_cross_venue: bool = True,
    include_hyperliquid: bool = True,
    cache_root: str | None = None,
) -> None:
    """Refresh every free source and grow the strict forward-only cohort."""
    from datetime import date, timedelta

    from liquidity_signal.data.daily_refresh import refresh_window, run_daily_refresh

    selected = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    if through:
        end_day = date.fromisoformat(through)
        start_day = end_day - timedelta(days=retry_days - 1)
    else:
        start_day, end_day = refresh_window(retry_days=retry_days)
    report = run_daily_refresh(
        symbols=selected,
        archive_start=start_day,
        archive_end=end_day,
        market_db=market_db,
        onchain_db=onchain_db,
        liquidation_db=liquidation_db,
        cross_venue_db=cross_venue_db,
        forward_training_db=forward_training_db,
        completeness_path=completeness_path,
        report_path=report_path,
        forward_lookback_hours=forward_lookback_hours,
        run_training_when_ready=run_training_when_ready,
        include_cross_venue=include_cross_venue,
        include_hyperliquid=include_hyperliquid,
        **({name: str(Path(cache_root) / folder) for name, folder in {
            "vision_cache_dir": "vision_cache",
            "supplemental_cache_dir": "vision_supplemental_cache",
            "hyperliquid_cache_dir": "hyperliquid_liquidation_cache",
            "cross_venue_cache_dir": "cross_venue_cache",
        }.items()} if cache_root else {}),
    )
    console.print_json(data=report)
    if report["status"] != "complete":
        raise typer.Exit(code=1)


@app.command("quarantine-forward-cohort")
def quarantine_forward_cohort_command(
    audit_path: str = typer.Option(..., help="New SQLite path for the preserved cohort."),
    reason: str = typer.Option(..., help="Audit reason recorded in the preserved database."),
    db_path: str = "runtime/training_forward.db",
    confirm: bool = typer.Option(
        False, "--confirm", help="Required acknowledgment before clearing active rows."
    ),
) -> None:
    """Preserve and clear an invalid forward cohort after a verified feed incident."""
    from liquidity_signal.data.daily_refresh import quarantine_forward_cohort

    if not confirm:
        raise typer.BadParameter(
            "Pass --confirm after reviewing the audit path and quarantine reason."
        )
    console.print_json(data=quarantine_forward_cohort(db_path, audit_path, reason))


@app.command("data-operations-health")
def data_operations_health_command(
    liquidation_db: str = "runtime/liquidation_history.db",
    refresh_report_path: str = "runtime/daily_refresh_status.json",
    backup_status_path: str = "runtime/data_backup_status.json",
    output_path: str = "runtime/data_operations_health.json",
    symbols: str = ACTIVE_SYMBOLS_CSV,
    stale_after_minutes: int = typer.Option(10, min=5),
    continuity_window_minutes: int = typer.Option(30, min=10),
    refresh_stale_after_hours: int = typer.Option(36, min=1),
    backup_stale_after_hours: int = typer.Option(36, min=1),
    fail_on_unhealthy: bool = True,
) -> None:
    """Fail closed on stale feeds or a failed/overdue daily refresh."""
    from liquidity_signal.data.operations_health import (
        build_operations_health,
        write_operations_health,
    )

    health = build_operations_health(
        liquidation_db=liquidation_db,
        refresh_report_path=refresh_report_path,
        backup_status_path=backup_status_path,
        symbols=[item.strip().upper() for item in symbols.split(",") if item.strip()],
        stale_after_minutes=stale_after_minutes,
        continuity_window_minutes=continuity_window_minutes,
        refresh_stale_after_hours=refresh_stale_after_hours,
        backup_stale_after_hours=backup_stale_after_hours,
    )
    write_operations_health(health, output_path)
    console.print_json(data=health)
    if fail_on_unhealthy and not health["healthy"]:
        raise typer.Exit(code=1)


@app.command("backup-data")
def backup_data_command(
    databases: str = (
        "runtime/liquidation_history.db,runtime/training_forward.db,"
        "runtime/vision_metrics.db,runtime/onchain_data.db,"
        "runtime/cross_venue.db,"
        "runtime/training_forward_legacy_binance_route_audit_2026-08-12.db"
    ),
    artifacts: str = (
        "runtime/daily_refresh_status.json,runtime/data_completeness_forward.json,"
        "runtime/data_completeness_archive.json,"
        "runtime/data_completeness_archive_hyperliquid.json,"
        "runtime/data_completeness_archive_cross_venue.json,"
        "runtime/data_completeness_archive_cross_venue_hyperliquid.json,"
        "runtime/data_operations_health.json"
    ),
    output_dir: str = "runtime/backups",
    status_path: str = "runtime/data_backup_status.json",
    retain: int = typer.Option(7, min=0, help="Backup generations to keep; zero disables pruning."),
) -> None:
    """Create and restore-verify a consistent compressed SQLite backup."""
    from liquidity_signal.data.daily_refresh import write_refresh_report
    from liquidity_signal.data.data_backup import create_data_backup

    result = create_data_backup(
        database_paths=[item.strip() for item in databases.split(",") if item.strip()],
        artifact_paths=[item.strip() for item in artifacts.split(",") if item.strip()],
        output_dir=output_dir,
        retain=retain,
    )
    write_refresh_report(result, status_path)
    console.print_json(data=result)


@app.command("verify-data-backup")
def verify_data_backup_command(archive_path: str) -> None:
    """Restore a backup into temporary files and verify hashes/integrity."""
    from liquidity_signal.data.data_backup import verify_data_backup

    result = verify_data_backup(archive_path)
    console.print_json(data=result)
    if not result["valid"]:
        raise typer.Exit(code=1)


@app.command("restore-data-backup")
def restore_data_backup_command(archive_path: str, target_dir: str) -> None:
    """Restore a verified backup to a new isolated directory without overwrites."""
    from liquidity_signal.data.data_backup import restore_data_backup

    console.print_json(data=restore_data_backup(archive_path, target_dir))


@app.command("ingest-open-onchain")
def ingest_open_onchain_command(
    start: str = typer.Option(..., help="Start date YYYY-MM-DD (UTC)."),
    end: str = typer.Option(..., help="End date YYYY-MM-DD (UTC, inclusive)."),
    db_path: str = "runtime/onchain_data.db",
) -> None:
    """Backfill free daily chain activity and TVL without API credentials."""
    from datetime import date

    from liquidity_signal.data.open_onchain import OpenOnchainClient, OpenOnchainStore

    client = OpenOnchainClient()
    store = OpenOnchainStore(db_path)
    try:
        result = store.ingest(client, date.fromisoformat(start), date.fromisoformat(end))
    finally:
        client.close()
        store.close()
    console.print_json(data={"db_path": db_path, **result})


@app.command("ingest-hyperliquid-liquidations")
def ingest_hyperliquid_liquidations_command(
    start: str = typer.Option(..., help="Start date YYYY-MM-DD (UTC)."),
    end: str = typer.Option(..., help="End date YYYY-MM-DD (UTC, inclusive)."),
    symbols: str = ACTIVE_SYMBOLS_CSV,
    db_path: str = "runtime/liquidation_history.db",
    cache_dir: str = "runtime/hyperliquid_liquidation_cache",
) -> None:
    """Backfill free observed Hyperliquid liquidations as cross-venue context."""
    from datetime import date

    from liquidity_signal.data.hyperliquid_archive import (
        HyperliquidArchiveClient,
        ingest_hyperliquid_range,
    )
    from liquidity_signal.service.liquidation_store import LiquidationStore

    selected = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    client = HyperliquidArchiveClient(cache_dir=cache_dir)
    store = LiquidationStore(Path(db_path))
    try:
        result = ingest_hyperliquid_range(
            client,
            store,
            date.fromisoformat(start),
            date.fromisoformat(end),
            selected,
        )
    finally:
        client.close()
        store.close()
    console.print_json(data={"db_path": db_path, **result})


@app.command("ingest-cryptohft-liquidations")
def ingest_cryptohft_liquidations_command(
    start: str = typer.Option(..., help="Inclusive ISO-8601 UTC timestamp."),
    end: str = typer.Option(..., help="Exclusive ISO-8601 UTC timestamp."),
    symbols: str = ACTIVE_SYMBOLS_CSV,
    venues: str = "binance,bybit",
    db_path: str = "runtime/liquidation_history.db",
    cache_dir: str = "runtime/cryptohft_liquidation_cache",
    report_path: str = "runtime/cryptohft_recovery_status.json",
    requests_per_minute: int = 55,
    download_workers: int = typer.Option(8, min=1, max=16),
) -> None:
    """Recover Binance/Bybit liquidations from a free independent archive."""
    from datetime import datetime

    from liquidity_signal.data.cryptohft_archive import (
        CryptoHFTArchiveClient,
        ingest_cryptohft_range,
    )
    from liquidity_signal.service.liquidation_store import LiquidationStore

    selected_symbols = [
        item.strip().upper() for item in symbols.split(",") if item.strip()
    ]
    selected_venues = [
        item.strip().lower() for item in venues.split(",") if item.strip()
    ]
    def parse_iso(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    client = CryptoHFTArchiveClient(
        cache_dir=cache_dir,
        requests_per_minute=requests_per_minute,
        download_workers=download_workers,
    )
    store = LiquidationStore(Path(db_path))
    try:
        result = ingest_cryptohft_range(
            client,
            store,
            parse_iso(start),
            parse_iso(end),
            selected_symbols,
            venues=selected_venues,
            report_path=report_path,
        )
    finally:
        client.close()
        store.close()
    console.print_json(data={"db_path": db_path, "report_path": report_path, **result})
    if result["status"] != "complete":
        raise typer.Exit(code=1)


@app.command("invalidate-capture-interval")
def invalidate_capture_interval_command(
    source: str = typer.Option(..., help="Exchange source to invalidate."),
    symbol: str | None = typer.Option(
        None, help="Optional single symbol; omit to invalidate the whole source."
    ),
    start_ts: int = typer.Option(..., help="Inclusive UTC epoch milliseconds."),
    end_ts: int = typer.Option(..., help="Inclusive UTC epoch milliseconds."),
    reason: str = typer.Option(..., help="Audit reason stored with the incident."),
    db_path: str = "runtime/liquidation_history.db",
    confirm: bool = typer.Option(
        False, "--confirm", help="Required acknowledgment before invalidation."
    ),
) -> None:
    """Mark a proven-bad heartbeat interval ineligible for model completeness."""
    from liquidity_signal.service.liquidation_store import LiquidationStore

    if not confirm:
        raise typer.BadParameter(
            "Pass --confirm after reviewing source, interval, and reason."
        )
    store = LiquidationStore(Path(db_path))
    try:
        result = store.invalidate_capture_interval(
            source, start_ts, end_ts, reason, symbol=symbol
        )
    finally:
        store.close()
    console.print_json(data={"db_path": db_path, **result})


@app.command("capture-liquidations")
def capture_liquidations_command(
    symbols: str = ACTIVE_SYMBOLS_CSV,
    db_path: str = "runtime/liquidation_history.db",
    duration_seconds: int = typer.Option(
        0, help="Stop after N seconds; zero runs continuously."
    ),
    status_interval_seconds: int = 30,
) -> None:
    """Persist free Binance and Bybit liquidation WebSocket events."""
    import time

    from liquidity_signal.service.liquidation_runtime import (
        LiquidationRuntime,
        qualify_capture_health,
    )
    from liquidity_signal.service.liquidation_store import LiquidationStore

    selected = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    store = LiquidationStore(db_path=Path(db_path))
    runtime = LiquidationRuntime(store=store)
    started = time.monotonic()
    try:
        for symbol in selected:
            runtime.ensure_symbol(symbol)
        while duration_seconds <= 0 or time.monotonic() - started < duration_seconds:
            interval = max(1, status_interval_seconds)
            if duration_seconds > 0:
                remaining = duration_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    break
                interval = min(interval, remaining)
            time.sleep(interval)
            observed_at_ms = int(time.time() * 1000)
            health_models = qualify_capture_health([
                item
                for symbol in selected
                for item in runtime.get_stream_health(symbol)
            ], observed_at_ms)
            store.persist_stream_health(health_models, observed_at_ms)
            health = [item.model_dump(mode="json") for item in health_models]
            errors = [
                {
                    "source": item["source"],
                    "symbol": item["symbol"],
                    "error": item["last_error"],
                }
                for item in health
                if item.get("last_error")
            ]
            console.print_json(data={
                "streams": len(health),
                "connected": sum(bool(item["connected"]) for item in health),
                "events_buffered": sum(int(item["events_buffered"]) for item in health),
                "errors": errors,
            })
    except KeyboardInterrupt:
        pass
    finally:
        runtime.close()
        store.close()


if __name__ == "__main__":
    app()
