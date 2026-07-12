from __future__ import annotations

import json
from pathlib import Path
from typing import Any


LABELS = ("FLAT", "LONG", "SHORT")
ABSOLUTE_PRICE_FIELDS = {
    "current_price", "mark_price", "price_range_low", "price_range_high",
    "close", "ema20", "ema50", "ema200", "vwap", "nearest_support",
    "nearest_resistance", "center_price", "min_price", "max_price", "level_price",
}


def _flatten(value: Any, *, prefix: str = "") -> dict[str, Any]:
    output: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "reasons" or key in ABSOLUTE_PRICE_FIELDS:
                continue
            name = f"{prefix}.{key}" if prefix else str(key)
            output.update(_flatten(item, prefix=name))
    elif isinstance(value, list):
        # Reasons are unstable prose; retain structured list values only as
        # compact membership flags.
        for index, item in enumerate(value[:5]):
            if isinstance(item, (str, int, float, bool)):
                output[f"{prefix}[{index}]"] = item
    elif value is not None:
        output[prefix] = value
    return output


def _engineer_price_distances(prompt: dict[str, Any]) -> None:
    for timeframe, row in prompt.get("timeframes", {}).items():
        if not isinstance(row, dict):
            continue
        try:
            close = float(row.get("close", 0.0))
        except (TypeError, ValueError):
            close = 0.0
        if close <= 0.0:
            continue
        for key in ("ema20", "ema50", "ema200", "vwap", "nearest_support", "nearest_resistance"):
            try:
                value = float(row.get(key, 0.0))
            except (TypeError, ValueError):
                value = 0.0
            if value > 0.0:
                row[f"{key}_distance_bps"] = round(((close - value) / close) * 10_000, 4)


def _load_rows(path: str | Path) -> dict[str, tuple[list[dict[str, Any]], list[str]]]:
    splits = {name: ([], []) for name in ("train", "validation", "test")}
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            split = row.get("metadata", {}).get("split", row.get("split", "train"))
            if split not in splits:
                continue
            prompt = json.loads(row["messages"][1]["content"])
            _engineer_price_distances(prompt)
            label = json.loads(row["messages"][-1]["content"])["prediction"]
            features = _flatten(prompt)
            features["__event_ts"] = int(row.get("metadata", {}).get("event_ts") or 0)
            # These fields describe what the old strategy decided and can make
            # a reviewer mimic it instead of learning the outcome.
            for key in list(features):
                unstable_prefixes = (
                    "decision_action", "took_trade", "data_quality", "whale",
                    "liquidity.market_metrics", "liquidity.event_", "liquidity.events_",
                    "features.funding_rate_bps", "features.basis_bps",
                    "features.open_interest_change_pct",
                )
                if key.startswith(unstable_prefixes):
                    features.pop(key, None)
            splits[split][0].append(features)
            splits[split][1].append(label)
    return splits


def _metrics(actual: list[str], predicted: list[str]) -> dict[str, Any]:
    from sklearn.metrics import accuracy_score, balanced_accuracy_score, confusion_matrix, f1_score, recall_score

    recalls = recall_score(actual, predicted, labels=list(LABELS), average=None, zero_division=0)
    directional = [index for index, value in enumerate(predicted) if value != "FLAT"]
    directional_correct = sum(predicted[index] == actual[index] for index in directional)
    return {
        "examples": len(actual),
        "accuracy": round(float(accuracy_score(actual, predicted)), 4),
        "balanced_accuracy": round(float(balanced_accuracy_score(actual, predicted)), 4),
        "macro_f1": round(float(f1_score(actual, predicted, labels=list(LABELS), average="macro", zero_division=0)), 4),
        "directional_coverage": round(len(directional) / len(actual), 4) if actual else 0.0,
        "directional_precision": round(directional_correct / len(directional), 4) if directional else None,
        "recall": {label: round(float(value), 4) for label, value in zip(LABELS, recalls)},
        "confusion_matrix": confusion_matrix(actual, predicted, labels=list(LABELS)).tolist(),
        "labels": list(LABELS),
    }


def _apply_policy(classes: list[str], probabilities: Any, threshold: float, margin: float) -> list[str]:
    predictions: list[str] = []
    for row in probabilities:
        ranked = sorted(zip(classes, row), key=lambda item: item[1], reverse=True)
        label, probability = ranked[0]
        probability_margin = probability - ranked[1][1]
        predictions.append(label if probability >= threshold and probability_margin >= margin else "FLAT")
    return predictions


def train_tabular_reviewer(
    dataset_path: str,
    output_dir: str,
    *,
    random_seed: int = 42,
) -> dict[str, Any]:
    import joblib
    from sklearn.ensemble import ExtraTreesClassifier
    from sklearn.feature_extraction import DictVectorizer

    splits = _load_rows(dataset_path)
    train_x, train_y = splits["train"]
    validation_x, validation_y = splits["validation"]
    test_x, test_y = splits["test"]
    for partition in (train_x, validation_x, test_x):
        for row in partition:
            row.pop("__event_ts", None)
    vectorizer = DictVectorizer(sparse=True)
    encoded_train = vectorizer.fit_transform(train_x)
    model = ExtraTreesClassifier(
        n_estimators=600,
        min_samples_leaf=5,
        max_features="sqrt",
        class_weight="balanced_subsample",
        n_jobs=-1,
        random_state=random_seed,
    )
    model.fit(encoded_train, train_y)
    classes = [str(value) for value in model.classes_]

    validation_probabilities = model.predict_proba(vectorizer.transform(validation_x))
    candidates: list[tuple[float, float, dict[str, Any]]] = []
    for threshold_int in range(34, 76, 2):
        for margin_int in range(0, 21, 2):
            threshold = threshold_int / 100.0
            margin = margin_int / 100.0
            predictions = _apply_policy(classes, validation_probabilities, threshold, margin)
            result = _metrics(validation_y, predictions)
            precision = result["directional_precision"] or 0.0
            coverage = result["directional_coverage"]
            # Prefer useful precision, then coverage and balanced accuracy.
            score = (precision * min(coverage / 0.15, 1.0)) + (0.20 * result["balanced_accuracy"])
            candidates.append((score, coverage, {"threshold": threshold, "margin": margin, **result}))
    eligible = [item for item in candidates if item[2]["directional_coverage"] >= 0.10]
    selected = max(eligible or candidates, key=lambda item: (item[0], item[1]))[2]

    test_probabilities = model.predict_proba(vectorizer.transform(test_x))
    test_predictions = _apply_policy(classes, test_probabilities, selected["threshold"], selected["margin"])
    test_metrics = _metrics(test_y, test_predictions)
    deployment_gate = {
        "passed": bool(
            test_metrics["balanced_accuracy"] >= 0.40
            and test_metrics["macro_f1"] >= 0.40
            and (test_metrics["directional_precision"] or 0.0) >= 0.52
            and test_metrics["directional_coverage"] >= 0.10
            and min(test_metrics["recall"].values()) >= 0.25
        ),
        "requirements": {
            "balanced_accuracy": 0.40,
            "macro_f1": 0.40,
            "directional_precision": 0.52,
            "directional_coverage": 0.10,
            "minimum_class_recall": 0.25,
        },
    }
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "model": model,
            "vectorizer": vectorizer,
            "classes": classes,
            "threshold": selected["threshold"],
            "margin": selected["margin"],
        },
        target / "reviewer.joblib",
    )
    summary = {
        "dataset_path": dataset_path,
        "rows": {name: len(values[1]) for name, values in splits.items()},
        "features": len(vectorizer.feature_names_),
        "validation_policy": selected,
        "test": test_metrics,
        "deployment_gate": deployment_gate,
    }
    (target / "evaluation.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def train_signal_validator(
    dataset_path: str,
    output_dir: str,
    *,
    random_seed: int = 42,
    train_window_days: int | None = None,
) -> dict[str, Any]:
    """Learn whether to accept an existing directional signal, not its direction."""
    import joblib
    from sklearn.ensemble import ExtraTreesClassifier
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.metrics import average_precision_score, roc_auc_score

    raw_splits = _load_rows(dataset_path)
    splits: dict[str, tuple[list[dict[str, Any]], list[int], list[str]]] = {}
    for split, (features, outcomes) in raw_splits.items():
        selected_x: list[dict[str, Any]] = []
        selected_y: list[int] = []
        sides: list[str] = []
        for row, outcome in zip(features, outcomes):
            event_ts = int(row.pop("__event_ts", 0))
            row["__event_ts_control"] = event_ts
            side = str(row.get("signal.direction", "FLAT"))
            if side not in {"LONG", "SHORT"}:
                continue
            selected_x.append(row)
            selected_y.append(int(side == outcome))
            sides.append(side)
        splits[split] = (selected_x, selected_y, sides)

    if train_window_days:
        train_x_raw, train_y_raw, train_sides_raw = splits["train"]
        newest = max((int(row.get("__event_ts_control", 0)) for row in train_x_raw), default=0)
        cutoff = newest - (train_window_days * 24 * 60 * 60 * 1000)
        retained = [
            (row, label, side)
            for row, label, side in zip(train_x_raw, train_y_raw, train_sides_raw)
            if int(row.get("__event_ts_control", 0)) >= cutoff
        ]
        splits["train"] = (
            [item[0] for item in retained],
            [item[1] for item in retained],
            [item[2] for item in retained],
        )

    for features, _, _ in splits.values():
        for row in features:
            row.pop("__event_ts_control", None)

    train_x, train_y, _ = splits["train"]
    validation_x, validation_y, validation_sides = splits["validation"]
    test_x, test_y, test_sides = splits["test"]
    vectorizer = DictVectorizer(sparse=True)
    encoded_train = vectorizer.fit_transform(train_x)
    model = ExtraTreesClassifier(
        n_estimators=800,
        min_samples_leaf=8,
        max_features=0.6,
        class_weight="balanced_subsample",
        n_jobs=-1,
        random_state=random_seed,
    )
    model.fit(encoded_train, train_y)
    positive_index = list(model.classes_).index(1)
    validation_probabilities = model.predict_proba(vectorizer.transform(validation_x))[:, positive_index]

    def policy_metrics(actual: list[int], sides: list[str], probabilities: Any, threshold: float) -> dict[str, Any]:
        accepted = [index for index, value in enumerate(probabilities) if value >= threshold]
        correct = sum(actual[index] for index in accepted)
        per_side: dict[str, Any] = {}
        for side in ("LONG", "SHORT"):
            indices = [index for index in accepted if sides[index] == side]
            per_side[side] = {
                "accepted": len(indices),
                "precision": round(sum(actual[index] for index in indices) / len(indices), 4) if indices else None,
            }
        return {
            "candidates": len(actual),
            "accepted": len(accepted),
            "coverage": round(len(accepted) / len(actual), 4) if actual else 0.0,
            "precision": round(correct / len(accepted), 4) if accepted else None,
            "baseline_precision": round(sum(actual) / len(actual), 4) if actual else None,
            "per_side": per_side,
        }

    candidates = []
    for threshold_int in range(30, 91):
        threshold = threshold_int / 100.0
        metrics = policy_metrics(validation_y, validation_sides, validation_probabilities, threshold)
        precision = metrics["precision"] or 0.0
        coverage = metrics["coverage"]
        score = precision * min(coverage / 0.10, 1.0)
        candidates.append((score, precision, coverage, threshold, metrics))
    eligible = [item for item in candidates if item[2] >= 0.10 and item[4]["accepted"] >= 100]
    selected = max(eligible or candidates, key=lambda item: (item[0], item[1], item[2]))
    threshold = selected[3]
    validation_metrics = selected[4]

    test_probabilities = model.predict_proba(vectorizer.transform(test_x))[:, positive_index]
    test_metrics = policy_metrics(test_y, test_sides, test_probabilities, threshold)
    try:
        test_metrics["roc_auc"] = round(float(roc_auc_score(test_y, test_probabilities)), 4)
        test_metrics["average_precision"] = round(float(average_precision_score(test_y, test_probabilities)), 4)
    except ValueError:
        test_metrics["roc_auc"] = None
        test_metrics["average_precision"] = None

    validation_precision = validation_metrics["precision"] or 0.0
    test_precision = test_metrics["precision"] or 0.0
    deployment_gate = {
        "passed": bool(
            test_precision >= 0.52
            and test_metrics["coverage"] >= 0.10
            and test_metrics["accepted"] >= 100
            and abs(validation_precision - test_precision) <= 0.10
            and all((test_metrics["per_side"][side]["precision"] or 0.0) >= 0.48 for side in ("LONG", "SHORT"))
        ),
        "requirements": {
            "precision": 0.52,
            "coverage": 0.10,
            "accepted_samples": 100,
            "max_validation_test_precision_gap": 0.10,
            "minimum_each_side_precision": 0.48,
        },
    }
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {"model": model, "vectorizer": vectorizer, "threshold": threshold, "task": "signal_validation"},
        target / "validator.joblib",
    )
    summary = {
        "dataset_path": dataset_path,
        "rows": {name: len(values[1]) for name, values in splits.items()},
        "features": len(vectorizer.feature_names_),
        "threshold": threshold,
        "train_window_days": train_window_days,
        "validation": validation_metrics,
        "test": test_metrics,
        "deployment_gate": deployment_gate,
    }
    (target / "evaluation.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
