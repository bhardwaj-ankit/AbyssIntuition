from __future__ import annotations

from typing import Any

import httpx

from liquidity_signal.data.bybit_client import BybitPublicClient


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://api.bybit.com/v5/market/liquidation")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("status error", request=request, response=response)

    def json(self) -> dict[str, Any]:
        return self._payload


class _FlakyTransport:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, url: str, params: dict[str, Any]) -> _FakeResponse:
        self.calls += 1
        if self.calls == 1:
            raise httpx.TimeoutException("timeout")
        return _FakeResponse({"result": {"list": [{"side": "Sell", "price": "100", "size": "1", "updatedTime": "1"}]}})


class _FailingTransport:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, url: str, params: dict[str, Any]) -> _FakeResponse:
        self.calls += 1
        raise httpx.NetworkError("network down")


def test_bybit_client_retries_and_succeeds(monkeypatch) -> None:
    client = BybitPublicClient()
    transport = _FlakyTransport()
    monkeypatch.setattr(client, "_client", transport)
    monkeypatch.setattr("liquidity_signal.data.bybit_client.time.sleep", lambda _: None)
    monkeypatch.setattr("liquidity_signal.data.bybit_client.random.uniform", lambda _a, _b: 0.0)

    rows, fetched_at_ms, error = client.get_recent_liquidations_with_meta("BTCUSDT", limit=10)

    assert transport.calls == 2
    assert error is None
    assert fetched_at_ms is not None
    assert rows is not None
    assert len(rows) == 1


def test_bybit_client_returns_degraded_reason_after_retries(monkeypatch) -> None:
    client = BybitPublicClient()
    transport = _FailingTransport()
    monkeypatch.setattr(client, "_client", transport)
    monkeypatch.setattr("liquidity_signal.data.bybit_client.time.sleep", lambda _: None)
    monkeypatch.setattr("liquidity_signal.data.bybit_client.random.uniform", lambda _a, _b: 0.0)

    rows, fetched_at_ms, error = client.get_recent_liquidations_with_meta("BTCUSDT", limit=10)

    assert transport.calls == client.MAX_RETRIES
    assert rows is None
    assert fetched_at_ms is None
    assert error is not None
