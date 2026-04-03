from __future__ import annotations

from typing import Any, Dict, List

import httpx


class BinanceFuturesClient:
    BASE_URL = "https://fapi.binance.com"

    def __init__(self, timeout: float = 10.0) -> None:
        self._client = httpx.Client(base_url=self.BASE_URL, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        response = self._client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def get_mark_price_info(self, symbol: str) -> Dict[str, Any]:
        data = self._get_json("/fapi/v1/premiumIndex", params={"symbol": symbol})
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict) and row.get("symbol") == symbol:
                    return row
            return {}
        return data

    def get_mark_price(self, symbol: str) -> float:
        data = self.get_mark_price_info(symbol)
        return float(data["markPrice"])

    def get_order_book(self, symbol: str, limit: int = 20) -> Dict[str, Any]:
        return self._get_json("/fapi/v1/depth", params={"symbol": symbol, "limit": limit})

    def get_recent_trades(self, symbol: str, limit: int = 100) -> List[Dict[str, Any]]:
        return self._get_json("/fapi/v1/trades", params={"symbol": symbol, "limit": limit})

    def get_recent_klines(self, symbol: str, interval: str = "1m", limit: int = 20) -> List[List[Any]]:
        return self._get_json("/fapi/v1/klines", params={"symbol": symbol, "interval": interval, "limit": limit})

    def get_open_interest(self, symbol: str) -> Dict[str, Any]:
        return self._get_json("/fapi/v1/openInterest", params={"symbol": symbol})

    def get_open_interest_hist(self, symbol: str, period: str = "5m", limit: int = 30) -> List[Dict[str, Any]]:
        return self._get_json("/futures/data/openInterestHist", params={"symbol": symbol, "period": period, "limit": limit})

    def get_funding_rates(self, symbol: str, limit: int = 100) -> List[Dict[str, Any]]:
        return self._get_json("/fapi/v1/fundingRate", params={"symbol": symbol, "limit": limit})

    def get_basis(self, symbol: str, period: str = "5m", limit: int = 30) -> List[Dict[str, Any]]:
        return self._get_json(
            "/futures/data/basis",
            params={"pair": symbol, "contractType": "PERPETUAL", "period": period, "limit": limit},
        )

    def get_taker_buy_sell_volume(
        self, symbol: str, period: str = "5m", limit: int = 100
    ) -> List[Dict[str, Any]]:
        return self._get_json(
            "/futures/data/takerlongshortRatio", params={"symbol": symbol, "period": period, "limit": limit}
        )

    def get_taker_long_short_ratio(
        self, symbol: str, period: str = "5m", limit: int = 100
    ) -> List[Dict[str, Any]]:
        return self.get_taker_buy_sell_volume(symbol, period=period, limit=limit)

    def get_global_long_short_account_ratio(
        self, symbol: str, period: str = "5m", limit: int = 100
    ) -> List[Dict[str, Any]]:
        return self._get_json(
            "/futures/data/globalLongShortAccountRatio",
            params={"symbol": symbol, "period": period, "limit": limit},
        )

    def get_top_long_short_account_ratio(
        self, symbol: str, period: str = "5m", limit: int = 100
    ) -> List[Dict[str, Any]]:
        return self._get_json(
            "/futures/data/topLongShortAccountRatio",
            params={"symbol": symbol, "period": period, "limit": limit},
        )

    def get_top_long_short_position_ratio(
        self, symbol: str, period: str = "5m", limit: int = 100
    ) -> List[Dict[str, Any]]:
        return self._get_json(
            "/futures/data/topLongShortPositionRatio",
            params={"symbol": symbol, "period": period, "limit": limit},
        )
