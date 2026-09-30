import json

from liquidity_signal.ai.lora_dataset import chronological_split_examples, rebalance_examples_by_split
from liquidity_signal.models import Direction, LoraTrainingExample


def _example(index: int) -> LoraTrainingExample:
    completion = json.dumps({"prediction": "LONG", "horizon_minutes": 15})
    return LoraTrainingExample(
        snapshot_id=f"row-{index}",
        symbol="BTCUSDT",
        horizon_minutes=15,
        label_action=Direction.LONG,
        decision_action="wait",
        prompt="{}",
        completion=completion,
        messages=[],
        metadata={"event_ts": index},
    )


def test_chronological_split_never_puts_future_rows_in_training() -> None:
    rows = chronological_split_examples([_example(index) for index in reversed(range(20))])
    train_times = [row.metadata["event_ts"] for row in rows if row.split == "train"]
    validation_times = [row.metadata["event_ts"] for row in rows if row.split == "validation"]
    test_times = [row.metadata["event_ts"] for row in rows if row.split == "test"]

    assert max(train_times) < min(validation_times)
    assert max(validation_times) < min(test_times)


def test_rebalancing_is_independent_per_split() -> None:
    rows = [_example(index) for index in range(18)]
    for index, row in enumerate(rows):
        row.split = "train" if index < 9 else "validation" if index < 15 else "test"
        row.label_action = (Direction.LONG, Direction.SHORT, Direction.FLAT)[index % 3]

    balanced = rebalance_examples_by_split(rows, balance_mode="undersample_majority")

    for split in ("train", "validation", "test"):
        counts = [sum(row.split == split and row.label_action == label for row in balanced) for label in Direction]
        assert len(set(counts)) == 1
