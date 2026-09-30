from __future__ import annotations

from liquidity_signal.data.binance_client import BinanceFuturesClient


def test_funding_history_paginates_and_deduplicates(monkeypatch) -> None:
    client = BinanceFuturesClient()
    calls: list[dict[str, int | str]] = []

    def fake_get_json(path, params):
        assert path == "/fapi/v1/fundingRate"
        calls.append(params)
        if len(calls) == 1:
            return [
                {"fundingTime": 1_000, "fundingRate": "0.1"},
                {"fundingTime": 2_000, "fundingRate": "0.2"},
            ]
        return [
            {"fundingTime": 2_000, "fundingRate": "0.2"},
            {"fundingTime": 3_000, "fundingRate": "0.3"},
        ]

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    try:
        rows = client.get_funding_rate_history(
            "BTCUSDT", 1_000, 3_000, limit_per_request=2
        )
    finally:
        client.close()

    assert [row["fundingTime"] for row in rows] == [1_000, 2_000, 3_000]
    assert calls[1]["startTime"] == 2_001
