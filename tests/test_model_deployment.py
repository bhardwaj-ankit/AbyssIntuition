from __future__ import annotations

import json

from liquidity_signal.ai.deployment import assess_lora_candidate, deployment_status


def test_missing_manifest_fails_closed(tmp_path):
    status = deployment_status(tmp_path / "missing.json")
    assert status["enabled"] is False
    assert status["blocking_reasons"] == ["deployment_manifest_missing"]


def test_lora_accuracy_alone_cannot_enable_live_trading(tmp_path):
    evaluation_path = tmp_path / "evaluation_test.json"
    evaluation_path.write_text(json.dumps({
        "split": "test",
        "examples": 1000,
        "accuracy": 0.90,
        "invalid_outputs": 0,
        "base_model": "example/model",
        "adapter_path": "runtime/adapter",
    }), encoding="utf-8")

    manifest_path = tmp_path / "manifest.json"
    result = assess_lora_candidate(evaluation_path, manifest_path=manifest_path)

    assert result["enabled"] is False
    assert result["mode"] == "shadow_only"
    assert result["checks"]["strategy_gate_passed"] is False
    assert deployment_status(manifest_path)["enabled"] is False
