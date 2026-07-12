from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from liquidity_signal.ai.local_lora_trainer import evaluate_local_lora, train_local_lora
from liquidity_signal.ai.evaluate import evaluate_signal_prior
from liquidity_signal.ai.deployment import assess_lora_candidate, deployment_status
from liquidity_signal.ai.tabular_trainer import train_signal_validator, train_tabular_reviewer
from liquidity_signal.ai.gbdt_trainer import train_gbdt_dual_head
from liquidity_signal.service.engine import SignalEngine

app = typer.Typer(help="Binance liquidity signal CLI")
console = Console()


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
    symbols: str = "BTCUSDT,ETHUSDT,SOLUSDT,NEARUSDT,PEPEUSDT,XRPUSDT",
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
    symbols: str = "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,NEARUSDT,PEPEUSDT",
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
    symbols: str = "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,NEARUSDT,PEPEUSDT",
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
    db_path: str = "runtime/training_v5.db",
    horizon_minutes: int = 60,
    output_dir: str | None = None,
) -> None:
    """Train the leakage-free two-head GBDT (direction + return range) on outcomes."""
    target = output_dir or f"runtime/models/gbdt-dual-{horizon_minutes}m-v1"
    console.print_json(data=train_gbdt_dual_head(
        db_path, target, horizon_minutes=horizon_minutes
    ))


if __name__ == "__main__":
    app()
