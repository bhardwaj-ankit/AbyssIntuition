from __future__ import annotations

from pathlib import Path

from liquidity_signal.service.engine import SignalEngine
from liquidity_signal.service.liquidation_store import LiquidationStore


class FakeHistoricalClient:
    def close(self) -> None:
        return None

    @staticmethod
    def _interval_ms(interval: str) -> int:
        units = {"m": 60_000, "h": 60 * 60_000, "d": 24 * 60 * 60_000}
        return int(interval[:-1]) * units[interval[-1]]

    def get_historical_klines(self, symbol: str, interval: str, start_time: int, end_time: int, limit_per_request: int = 1000):
        step = self._interval_ms(interval)
        rows = []
        cursor = int(start_time // step * step)
        price = 100.0
        while cursor <= end_time:
            drift = 0.04 if (cursor // step) % 40 < 24 else -0.03
            price += drift
            rows.append(
                [
                    cursor,
                    f"{price - 0.10}",
                    f"{price + 0.25}",
                    f"{price - 0.25}",
                    f"{price}",
                    "12.0",
                ]
            )
            cursor += step
        return rows

    def get_open_interest_hist(self, symbol: str, period: str = "5m", limit: int = 500):
        base = 1_700_000_000_000
        step = self._interval_ms(period)
        return [
            {"timestamp": str(base + i * step), "sumOpenInterestValue": str(1_000_000_000 + i * 2_000_000)}
            for i in range(limit)
        ]

    def get_funding_rates(self, symbol: str, limit: int = 500):
        base = 1_700_000_000_000
        step = 8 * 60 * 60_000
        return [{"fundingTime": str(base + i * step), "fundingRate": "0.0001"} for i in range(limit)]

    def get_basis(self, symbol: str, period: str = "5m", limit: int = 500):
        base = 1_700_000_000_000
        step = self._interval_ms(period)
        return [{"timestamp": base + i * step, "basisRate": "0.0002"} for i in range(limit)]

    def get_taker_buy_sell_volume(self, symbol: str, period: str = "5m", limit: int = 500):
        base = 1_700_000_000_000
        step = self._interval_ms(period)
        return [{"timestamp": base + i * step, "buySellRatio": "1.03"} for i in range(limit)]

    def get_global_long_short_account_ratio(self, symbol: str, period: str = "5m", limit: int = 500):
        base = 1_700_000_000_000
        step = self._interval_ms(period)
        return [{"timestamp": base + i * step, "longShortRatio": "1.01"} for i in range(limit)]

    def get_top_long_short_account_ratio(self, symbol: str, period: str = "5m", limit: int = 500):
        base = 1_700_000_000_000
        step = self._interval_ms(period)
        return [{"timestamp": base + i * step, "longShortRatio": "1.02"} for i in range(limit)]

    def get_top_long_short_position_ratio(self, symbol: str, period: str = "5m", limit: int = 500):
        base = 1_700_000_000_000
        step = self._interval_ms(period)
        return [{"timestamp": base + i * step, "longShortRatio": "1.04"} for i in range(limit)]


def test_historical_backfill_creates_resolved_training_rows(tmp_path: Path) -> None:
    store = LiquidationStore(db_path=tmp_path / "historical_backfill.db")
    engine = SignalEngine(client=FakeHistoricalClient(), liquidation_store=store)

    result = engine.backfill_historical_training(
        "BTCUSDT",
        lookback_hours=6,
        step_minutes=30,
        max_samples=8,
        include_stored_liquidation=False,
    )

    assert result.snapshots_created > 0
    assert result.resolved_labels >= result.snapshots_created
    dataset = engine.load_training_dataset("BTCUSDT", limit=50, resolved_only=False)
    assert dataset.summary.total_snapshots == result.snapshots_created
    assert dataset.summary.resolved_labels == result.resolved_labels
    assert "whale_context" in dataset.snapshots[0].raw_payload
    assert "structure_context" in dataset.snapshots[0].raw_payload
    assert "session_context" in dataset.snapshots[0].raw_payload
    assert "data_quality_context" in dataset.snapshots[0].raw_payload

    store.close()


def test_historical_backfill_batch_runs_multiple_symbols(tmp_path: Path) -> None:
    store = LiquidationStore(db_path=tmp_path / "historical_backfill_batch.db")
    engine = SignalEngine(client=FakeHistoricalClient(), liquidation_store=store)

    result = engine.backfill_historical_training_batch(
        ["BTCUSDT", "ETHUSDT"],
        lookback_hours=6,
        step_minutes=30,
        max_samples_per_symbol=4,
        include_stored_liquidation=False,
    )

    assert len(result.items) == 2
    assert result.total_snapshots_created > 0
    assert result.total_resolved_labels >= result.total_snapshots_created

    store.close()


def test_historical_features_only_use_closed_candles(tmp_path: Path) -> None:
    store = LiquidationStore(db_path=tmp_path / "closed_candles.db")
    engine = SignalEngine(client=FakeHistoricalClient(), liquidation_store=store)
    hour = 60 * 60_000
    rows = [
        [0, "100", "101", "99", "100", "10"],
        [hour, "100", "150", "90", "140", "10"],
    ]

    visible = engine._closed_klines_at(rows, time_ms=hour + 30 * 60_000, interval_minutes=60)

    assert len(visible) == 1
    assert int(visible[0][0]) == 0
    store.close()
