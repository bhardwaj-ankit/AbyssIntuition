from __future__ import annotations

from typing import Any, Dict, List

import httpx


class BinanceFuturesClient:
    BASE_URL = "https://fapi.binance.com"

    def __init__(self, timeout: float = 10.0) -> None:
        self._client = httpx.Client(base_url=self.BASE_URL, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def get_mark_price(self, symbol: str) -> float:
        data = self._client.get("/fapi/v1/premiumIndex", params={"symbol": symbol}).json()
        return float(data["markPrice"])

    def get_order_book(self, symbol: str, limit: int = 20) -> Dict[str, Any]:
        return self._client.get("/fapi/v1/depth", params={"symbol": symbol, "limit": limit}).json()

    def get_recent_trades(self, symbol: str, limit: int = 100) -> List[Dict[str, Any]]:
        return self._client.get("/fapi/v1/trades", params={"symbol": symbol, "limit": limit}).json()

    def get_recent_klines(self, symbol: str, interval: str = "1m", limit: int = 20) -> List[List[Any]]:
        return self._client.get(
            "/fapi/v1/klines", params={"symbol": symbol, "interval": interval, "limit": limit}
        ).json()

    def get_open_interest(self, symbol: str) -> Dict[str, Any]:
        return self._client.get("/fapi/v1/openInterest", params={"symbol": symbol}).json()

    def get_open_interest_hist(self, symbol: str, period: str = "5m", limit: int = 30) -> List[Dict[str, Any]]:
        return self._client.get(
            "/futures/data/openInterestHist", params={"symbol": symbol, "period": period, "limit": limit}
        ).json()

    def get_funding_rates(self, symbol: str, limit: int = 100) -> List[Dict[str, Any]]:
        return self._client.get("/fapi/v1/fundingRate", params={"symbol": symbol, "limit": limit}).json()

    def get_taker_long_short_ratio(
        self, symbol: str, period: str = "5m", limit: int = 100
    ) -> List[Dict[str, Any]]:
        return self._client.get(
            "/futures/data/takerlongshortRatio", params={"symbol": symbol, "period": period, "limit": limit}
        ).json()

    def get_global_long_short_account_ratio(
        self, symbol: str, period: str = "5m", limit: int = 100
    ) -> List[Dict[str, Any]]:
        return self._client.get(
            "/futures/data/globalLongShortAccountRatio",
            params={"symbol": symbol, "period": period, "limit": limit},
        ).json()
