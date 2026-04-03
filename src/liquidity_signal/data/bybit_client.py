from __future__ import annotations

import random
import time
from typing import Any

import httpx


class BybitPublicClient:
    BASE_URL = "https://api.bybit.com"
    MAX_RETRIES = 3
    BACKOFF_SECONDS = 0.25

    def __init__(self, timeout: float = 8.0) -> None:
        self._client = httpx.Client(base_url=self.BASE_URL, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _extract_rows(self, data: Any) -> list[dict[str, Any]]:
        result = data.get("result", {}) if isinstance(data, dict) else {}
        rows = result.get("list", []) if isinstance(result, dict) else []
        return rows if isinstance(rows, list) else []

    def get_recent_liquidations_with_meta(
        self,
        symbol: str,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]] | None, int | None, str | None]:
        """Fetch recent liquidation events from Bybit public market API.

        Returns (rows, fetched_at_ms, error).
        On repeated failure, rows is None and error contains a degraded reason.
        """
        params = {"category": "linear", "symbol": symbol, "limit": max(1, min(limit, 200))}
        last_error: str | None = None

        for attempt in range(self.MAX_RETRIES):
            try:
                response = self._client.get("/v5/market/liquidation", params=params)
                response.raise_for_status()
                rows = self._extract_rows(response.json())
                fetched_at_ms = int(time.time() * 1000)
                return rows, fetched_at_ms, None
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError, ValueError) as exc:
                last_error = str(exc)
                if attempt < self.MAX_RETRIES - 1:
                    jitter = random.uniform(0.0, 0.05)
                    time.sleep(self.BACKOFF_SECONDS * (2**attempt) + jitter)

        return None, None, f"Bybit liquidation fetch failed after retries: {last_error or 'unknown error'}"

    def get_recent_liquidations(self, symbol: str, limit: int = 50) -> list[dict[str, Any]]:
        rows, _, _ = self.get_recent_liquidations_with_meta(symbol=symbol, limit=limit)
        return rows or []
