from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MANIFEST_PATH = Path("runtime/models/deployment_manifest.json")


def assess_lora_candidate(
    evaluation_path: str | Path,
    *,
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
    minimum_accuracy: float = 0.45,
    minimum_examples: int = 500,
) -> dict[str, Any]:
    """Create a fail-closed deployment manifest from held-out LoRA metrics.

    Classification quality is necessary but not sufficient for live trading. A
    candidate therefore remains shadow-only until a separate strategy gate has
    demonstrated acceptable directional precision and coverage.
    """
    source = Path(evaluation_path)
    evaluation = json.loads(source.read_text(encoding="utf-8"))
    accuracy = evaluation.get("accuracy")
    examples = int(evaluation.get("examples") or 0)
    invalid_outputs = int(evaluation.get("invalid_outputs") or 0)
    split = str(evaluation.get("split") or "")

    checks = {
        "held_out_test_split": split == "test",
        "minimum_examples": examples >= minimum_examples,
        "minimum_accuracy": accuracy is not None and float(accuracy) >= minimum_accuracy,
        "valid_structured_outputs": invalid_outputs == 0,
        # Accuracy against barrier labels does not establish net profitability.
        "strategy_gate_passed": False,
    }
    reasons = [name for name, passed in checks.items() if not passed]
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "enabled": all(checks.values()),
        "mode": "live" if all(checks.values()) else "shadow_only",
        "candidate": {
            "type": "lora_classifier",
            "base_model": evaluation.get("base_model"),
            "artifact_path": evaluation.get("adapter_path"),
            "evaluation_path": str(source),
        },
        "metrics": {
            "split": split,
            "examples": examples,
            "accuracy": accuracy,
            "invalid_outputs": invalid_outputs,
        },
        "thresholds": {
            "minimum_examples": minimum_examples,
            "minimum_accuracy": minimum_accuracy,
            "maximum_invalid_outputs": 0,
        },
        "checks": checks,
        "blocking_reasons": reasons,
        "note": (
            "Live promotion requires a leakage-free strategy evaluation with fees, "
            "slippage, directional precision, coverage, and drawdown checks."
        ),
    }
    target = Path(manifest_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def deployment_status(manifest_path: str | Path | None = None) -> dict[str, Any]:
    """Read deployment state without ever defaulting to live-enabled."""
    configured = manifest_path or os.getenv("MODEL_DEPLOYMENT_MANIFEST") or DEFAULT_MANIFEST_PATH
    path = Path(configured)
    if not path.exists():
        return {
            "enabled": False,
            "mode": "disabled",
            "manifest_path": str(path),
            "blocking_reasons": ["deployment_manifest_missing"],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "enabled": False,
            "mode": "disabled",
            "manifest_path": str(path),
            "blocking_reasons": ["deployment_manifest_invalid"],
            "error": str(exc),
        }
    payload["enabled"] = payload.get("enabled") is True
    payload["mode"] = "live" if payload["enabled"] else payload.get("mode", "disabled")
    payload["manifest_path"] = str(path)
    return payload
