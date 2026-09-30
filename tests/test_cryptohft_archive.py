from __future__ import annotations

from datetime import datetime, timezone

import pytest

from liquidity_signal.data.cryptohft_archive import (
    CryptoHFTArchiveClient,
    _zstd_frame_content_size,
    normalize_cryptohft_rows,
)
from liquidity_signal.models import Direction


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "received_time": 1_800_000_000_123_000_000,
        "trade_time": 1_800_000_000_000,
        "symbol": "BTCUSDT",
        "side": "SELL",
        "quantity": 2.0,
        "filled_quantity": 2.0,
        "last_filled_quantity": 2.0,
        "price": 100.0,
        "average_price": 101.0,
    }
    row.update(overrides)
    return row


def test_binance_archive_row_matches_live_event_identity() -> None:
    event = normalize_cryptohft_rows(
        [_row()],
        venue="binance",
        requested_symbol="BTCUSDT",
        start_ts=1_799_999_999_000,
        end_ts=1_800_000_001_000,
    )[0]

    assert event.liquidated_side == Direction.LONG
    assert event.execution_side == "SELL"
    assert event.price == 101.0
    assert event.quantity == 2.0
    assert event.received_at == 1_800_000_000_123
    assert event.exchange_event_id == "binance:BTCUSDT:1800000000000:SELL:2.0:101.0"


def test_bybit_archive_side_is_converted_to_raw_identity_side() -> None:
    event = normalize_cryptohft_rows(
        [_row(side="BUY")],
        venue="bybit",
        requested_symbol="BTCUSDT",
        start_ts=1_799_999_999_000,
        end_ts=1_800_000_001_000,
    )[0]

    assert event.liquidated_side == Direction.SHORT
    assert event.execution_side == "BUY"
    assert event.exchange_event_id == "bybit:BTCUSDT:1800000000000:SELL:2.0:101.0"


def test_archive_pepe_alias_rescales_contract_without_changing_notional() -> None:
    event = normalize_cryptohft_rows(
        [_row(symbol="1000PEPEUSDT", price=0.012, average_price=0.012)],
        venue="binance",
        requested_symbol="PEPEUSDT",
        start_ts=1_799_999_999_000,
        end_ts=1_800_000_001_000,
    )[0]

    assert event.price == pytest.approx(0.000012)
    assert event.quantity == pytest.approx(2_000.0)
    assert event.notional == pytest.approx(0.024)


def test_rows_outside_requested_half_open_interval_are_excluded() -> None:
    assert normalize_cryptohft_rows(
        [_row()],
        venue="binance",
        requested_symbol="BTCUSDT",
        start_ts=1_800_000_000_000,
        end_ts=1_800_000_000_000,
    ) == []


def test_remote_path_uses_exchange_and_pepe_alias() -> None:
    hour = datetime(2026, 8, 24, 3, tzinfo=timezone.utc)

    assert CryptoHFTArchiveClient.remote_path("bybit", "PEPEUSDT", hour) == (
        "bybit/2026-08-24/03/1000PEPEUSDT_liquidations.parquet"
    )


def test_archive_reader_rejects_an_unexpected_schema(tmp_path) -> None:
    import pandas as pd

    path = tmp_path / "wrong.parquet"
    pd.DataFrame([{"symbol": "BTCUSDT"}]).to_parquet(path)

    with pytest.raises(ValueError, match="Unexpected CryptoHFTData schema"):
        CryptoHFTArchiveClient._read_parquet(path)


def test_archive_reader_opens_legacy_outer_zstd_wrapper(tmp_path) -> None:
    import pyarrow as pa

    frame_path = tmp_path / "source.parquet"
    wrapped_path = tmp_path / "legacy.parquet.part"
    pd = pytest.importorskip("pandas")
    pd.DataFrame([_row()]).to_parquet(frame_path)
    parquet_bytes = frame_path.read_bytes()
    compressed = bytes(pa.Codec("zstd").compress(parquet_bytes))
    wrapped_path.write_bytes(compressed)

    assert _zstd_frame_content_size(compressed) == len(parquet_bytes)
    rows = CryptoHFTArchiveClient._read_parquet(wrapped_path)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
