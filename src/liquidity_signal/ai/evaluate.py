from __future__ import annotations

import glob
import json
from pathlib import Path
from typing import Any


def evaluate_signal_prior(pattern: str) -> dict[str, Any]:
    """Evaluate the deterministic signal stored in exported labelled snapshots.

    Results include a chronological final-15% holdout.  This is a classification
    audit, not a claim about executable PnL; fees, latency and fills belong in the
    separate trading backtest.
    """
    rows: list[dict[str, Any]] = []
    for name in glob.glob(pattern):
        with Path(name).open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                messages = row.get("messages", [])
                if len(messages) < 3:
                    continue
                prompt = json.loads(messages[1]["content"])
                completion = json.loads(messages[-1]["content"])
                signal = prompt.get("signal", {})
                rows.append(
                    {
                        "event_ts": int(row.get("metadata", {}).get("event_ts") or 0),
                        "prediction": str(signal.get("direction", "FLAT")),
                        "confidence": float(signal.get("confidence", 0.0) or 0.0),
                        "label": str(completion.get("prediction", "FLAT")),
                        "split": str(row.get("metadata", {}).get("split", row.get("split", "train"))),
                    }
                )
    rows.sort(key=lambda row: row["event_ts"])

    def metrics(sample: list[dict[str, Any]]) -> dict[str, Any]:
        count = len(sample)
        traded = [row for row in sample if row["prediction"] != "FLAT"]
        correct = sum(row["prediction"] == row["label"] for row in sample)
        traded_correct = sum(row["prediction"] == row["label"] for row in traded)
        by_threshold: dict[str, Any] = {}
        for threshold in (0.3, 0.5, 0.6, 0.7):
            selected = [row for row in traded if row["confidence"] >= threshold]
            by_threshold[f"{threshold:.1f}"] = {
                "signals": len(selected),
                "precision": round(
                    sum(row["prediction"] == row["label"] for row in selected) / len(selected), 4
                ) if selected else None,
            }
        return {
            "examples": count,
            "accuracy": round(correct / count, 4) if count else None,
            "directional_coverage": round(len(traded) / count, 4) if count else None,
            "directional_precision": round(traded_correct / len(traded), 4) if traded else None,
            "confidence_thresholds": by_threshold,
        }

    holdout_start = int(len(rows) * 0.85)
    return {
        "files": sorted(glob.glob(pattern)),
        "warning": "Classification audit only; not an executable-return estimate.",
        "all": metrics(rows),
        "exported_test": metrics([row for row in rows if row["split"] == "test"]),
        "chronological_holdout": metrics(rows[holdout_start:]),
    }
