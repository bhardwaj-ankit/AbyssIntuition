from __future__ import annotations

from dataclasses import dataclass
import inspect
import json
from pathlib import Path
import re
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
    evaluation_batch_size: int = 16
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
        dataset = Dataset.from_list(source_rows)

        def tokenize(row: dict[str, Any]) -> dict[str, Any]:
            def token_ids(value: Any) -> list[int]:
                if hasattr(value, "input_ids"):
                    value = value.input_ids
                elif isinstance(value, dict):
                    value = value["input_ids"]
                if value and isinstance(value[0], list):
                    value = value[0]
                return [int(token) for token in value]

            messages = row["messages"]
            prompt_messages = messages[:-1]
            if hasattr(tokenizer, "apply_chat_template"):
                full_ids = token_ids(tokenizer.apply_chat_template(
                    messages, tokenize=True, add_generation_prompt=False
                ))
                prompt_ids = token_ids(tokenizer.apply_chat_template(
                    prompt_messages, tokenize=True, add_generation_prompt=True
                ))
            else:
                full_ids = token_ids(tokenizer(_to_text(tokenizer, messages), add_special_tokens=True))
                prompt_ids = token_ids(tokenizer(
                    _to_text(tokenizer, prompt_messages) + "\nassistant: ",
                    add_special_tokens=True,
                ))

            completion_ids = list(full_ids[len(prompt_ids) :])
            if not completion_ids:
                completion_ids = tokenizer(
                    messages[-1]["content"],
                    add_special_tokens=False,
                )["input_ids"]
                if tokenizer.eos_token_id is not None:
                    completion_ids.append(tokenizer.eos_token_id)
            completion_ids = completion_ids[-cfg.max_seq_length :]
            prompt_budget = max(cfg.max_seq_length - len(completion_ids), 0)
            kept_prompt_ids = list(prompt_ids[-prompt_budget:]) if prompt_budget else []
            input_ids = kept_prompt_ids + completion_ids
            labels = ([-100] * len(kept_prompt_ids)) + completion_ids
            padding = cfg.max_seq_length - len(input_ids)
            input_ids += [tokenizer.pad_token_id] * padding
            labels += [-100] * padding
            attention_mask = ([1] * (cfg.max_seq_length - padding)) + ([0] * padding)
            return {"input_ids": input_ids, "attention_mask": attention_mask, "labels": labels}

        return dataset.map(tokenize, remove_columns=dataset.column_names)

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

    trainer_kwargs = {
        "model": model,
        "args": training_args,
        "train_dataset": train_dataset,
        "eval_dataset": eval_dataset,
    }
    if "processing_class" in inspect.signature(Trainer.__init__).parameters:
        trainer_kwargs["processing_class"] = tokenizer
    else:
        trainer_kwargs["tokenizer"] = tokenizer
    trainer = Trainer(
        **trainer_kwargs,
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


def evaluate_local_lora(config_path: str | Path, *, split: str = "test") -> dict[str, Any]:
    """Generate labels for a held-out split and report exact classification metrics."""
    cfg = LocalLoraTrainingConfig.from_json(config_path)
    torch, _, _, _, transformer_stack = _require_training_stack()
    AutoModelForCausalLM, AutoTokenizer, _, _ = transformer_stack
    from peft import PeftModel

    rows = [
        row for row in _load_jsonl(cfg.dataset_path)
        if row.get("metadata", {}).get("split", row.get("split", "train")) == split
    ]
    tokenizer = AutoTokenizer.from_pretrained(cfg.output_dir, use_fast=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"
    tokenizer.truncation_side = "left"
    base = AutoModelForCausalLM.from_pretrained(cfg.base_model)
    model = PeftModel.from_pretrained(base, cfg.output_dir).to(_device(torch))
    model.eval()

    labels = ("LONG", "SHORT", "FLAT")
    correct = 0
    invalid = 0
    output_samples: list[dict[str, str]] = []
    confusion = {actual: {predicted: 0 for predicted in (*labels, "INVALID")} for actual in labels}
    device = _device(torch)
    batch_size = max(1, cfg.evaluation_batch_size)
    with torch.no_grad():
        for offset in range(0, len(rows), batch_size):
            batch = rows[offset : offset + batch_size]
            expected_labels = [
                json.loads(row["messages"][-1]["content"])["prediction"] for row in batch
            ]
            prompt_texts = [
                tokenizer.apply_chat_template(
                    row["messages"][:-1], tokenize=False, add_generation_prompt=True
                )
                for row in batch
            ]
            encoded = tokenizer(
                prompt_texts,
                padding=True,
                truncation=True,
                max_length=cfg.max_seq_length,
                return_tensors="pt",
            )
            input_ids = encoded["input_ids"].to(device)
            attention_mask = encoded["attention_mask"].to(device)
            output = model.generate(
                input_ids=input_ids,
                attention_mask=attention_mask,
                max_new_tokens=24,
                do_sample=False,
                pad_token_id=tokenizer.pad_token_id,
            )
            generated_batch = tokenizer.batch_decode(
                output[:, input_ids.shape[1] :], skip_special_tokens=True
            )
            for expected, generated in zip(expected_labels, generated_batch, strict=True):
                if len(output_samples) < 5:
                    output_samples.append({"expected": expected, "generated": generated})
                match = re.search(r'"PREDICTION"\s*:\s*"(LONG|SHORT|FLAT)"', generated.upper())
                predicted = match.group(1) if match else "INVALID"
                confusion[expected][predicted] += 1
                correct += int(predicted == expected)
                invalid += int(predicted == "INVALID")

    summary = {
        "split": split,
        "examples": len(rows),
        "accuracy": round(correct / len(rows), 4) if rows else None,
        "invalid_outputs": invalid,
        "output_samples": output_samples,
        "confusion_matrix": confusion,
        "base_model": cfg.base_model,
        "adapter_path": cfg.output_dir,
    }
    Path(cfg.output_dir, f"evaluation_{split}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
