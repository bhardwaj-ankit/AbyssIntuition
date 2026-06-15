from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


@dataclass
class LocalLoraTrainingConfig:
    base_model: str
    dataset_path: str
    output_dir: str
    task: str = "crypto_direction_classification"
    symbol: str = "BTCUSDT"
    horizon_minutes: int = 15
    max_seq_length: int = 2048
    learning_rate: float = 2e-4
    epochs: int = 3
    micro_batch_size: int = 2
    gradient_accumulation_steps: int = 8
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    balance_mode: str = "none"
    target_modules: list[str] | None = None
    class_weights: dict[str, float] | None = None
    notes: list[str] | None = None

    @classmethod
    def from_json(cls, path: str | Path) -> "LocalLoraTrainingConfig":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        allowed = {field for field in cls.__dataclass_fields__}
        filtered = {key: value for key, value in payload.items() if key in allowed}
        return cls(**filtered)


def _require_training_stack() -> tuple[Any, Any, Any, Any, Any]:
    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig, get_peft_model
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            Trainer,
            TrainingArguments,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Local LoRA training dependencies are not installed. "
            "Install: pip install transformers datasets peft accelerate torch"
        ) from exc
    return torch, Dataset, LoraConfig, get_peft_model, (AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments)


def _load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _device(torch: Any) -> str:
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _to_text(tokenizer: Any, messages: list[dict[str, str]]) -> str:
    if hasattr(tokenizer, "apply_chat_template"):
        return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)
    return "\n".join(f"{row['role']}: {row['content']}" for row in messages)


def train_local_lora(config_path: str | Path) -> dict[str, Any]:
    cfg = LocalLoraTrainingConfig.from_json(config_path)
    torch, Dataset, LoraConfig, get_peft_model, transformer_stack = _require_training_stack()
    AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments = transformer_stack

    rows = _load_jsonl(cfg.dataset_path)
    if not rows:
        raise RuntimeError(f"No training rows found in {cfg.dataset_path}")

    train_rows = [row for row in rows if row.get("metadata", {}).get("split", row.get("split", "train")) == "train"]
    val_rows = [row for row in rows if row.get("metadata", {}).get("split", row.get("split", "train")) == "validation"]
    if not train_rows:
        train_rows = rows

    tokenizer = AutoTokenizer.from_pretrained(cfg.base_model, use_fast=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(cfg.base_model)
    lora_config = LoraConfig(
        r=cfg.lora_r,
        lora_alpha=cfg.lora_alpha,
        lora_dropout=cfg.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=cfg.target_modules or ["q_proj", "k_proj", "v_proj", "o_proj"],
    )
    model = get_peft_model(model, lora_config)

    def build_dataset(source_rows: list[dict[str, Any]]) -> Any:
        texts = [_to_text(tokenizer, row["messages"]) for row in source_rows]
        dataset = Dataset.from_dict({"text": texts})

        def tokenize(batch: dict[str, list[str]]) -> dict[str, Any]:
            encoded = tokenizer(
                batch["text"],
                truncation=True,
                max_length=cfg.max_seq_length,
                padding="max_length",
            )
            encoded["labels"] = [list(ids) for ids in encoded["input_ids"]]
            return encoded

        return dataset.map(tokenize, batched=True, remove_columns=["text"])

    train_dataset = build_dataset(train_rows)
    eval_dataset = build_dataset(val_rows) if val_rows else None

    training_args = TrainingArguments(
        output_dir=cfg.output_dir,
        learning_rate=cfg.learning_rate,
        num_train_epochs=cfg.epochs,
        per_device_train_batch_size=cfg.micro_batch_size,
        per_device_eval_batch_size=cfg.micro_batch_size,
        gradient_accumulation_steps=cfg.gradient_accumulation_steps,
        logging_steps=10,
        save_strategy="epoch",
        eval_strategy="epoch" if eval_dataset is not None else "no",
        report_to=[],
        fp16=False,
        bf16=False,
        remove_unused_columns=False,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        tokenizer=tokenizer,
    )

    model.to(_device(torch))
    trainer.train()
    trainer.save_model(cfg.output_dir)
    tokenizer.save_pretrained(cfg.output_dir)

    summary = {
        "base_model": cfg.base_model,
        "dataset_path": cfg.dataset_path,
        "output_dir": cfg.output_dir,
        "train_rows": len(train_rows),
        "validation_rows": len(val_rows),
        "symbol": cfg.symbol,
        "horizon_minutes": cfg.horizon_minutes,
        "class_weights": cfg.class_weights or {},
    }
    Path(cfg.output_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.output_dir, "training_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
