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

    @staticmethod
    def _interval_ms(interval: str) -> int:
        units = {
            "m": 60_000,
            "h": 60 * 60_000,
            "d": 24 * 60 * 60_000,
            "w": 7 * 24 * 60 * 60_000,
        }
        value = int(interval[:-1])
        unit = interval[-1]
        return value * units[unit]

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

    def get_klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 20,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> List[List[Any]]:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)
        return self._get_json("/fapi/v1/klines", params=params)

    def get_recent_klines(self, symbol: str, interval: str = "1m", limit: int = 20) -> List[List[Any]]:
        return self.get_klines(symbol=symbol, interval=interval, limit=limit)

    def get_historical_klines(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        limit_per_request: int = 1000,
    ) -> List[List[Any]]:
        cursor = int(start_time)
        end_time = int(end_time)
        step_ms = self._interval_ms(interval)
        rows: List[List[Any]] = []

        while cursor <= end_time:
            batch = self.get_klines(
                symbol=symbol,
                interval=interval,
                limit=min(limit_per_request, 1500),
                start_time=cursor,
                end_time=end_time,
            )
            if not batch:
                break
            rows.extend(batch)
            last_open = int(batch[-1][0])
            next_cursor = last_open + step_ms
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < min(limit_per_request, 1500):
                break

        deduped: list[list[Any]] = []
        seen: set[int] = set()
        for row in rows:
            open_time = int(row[0])
            if open_time in seen:
                continue
            seen.add(open_time)
            deduped.append(row)
        return deduped

    def get_exchange_info(self) -> Dict[str, Any]:
        return self._get_json("/fapi/v1/exchangeInfo", params={})

    def get_open_interest(self, symbol: str) -> Dict[str, Any]:
        return self._get_json("/fapi/v1/openInterest", params={"symbol": symbol})

    def get_open_interest_hist(self, symbol: str, period: str = "5m", limit: int = 30) -> List[Dict[str, Any]]:
        return self._get_json("/futures/data/openInterestHist", params={"symbol": symbol, "period": period, "limit": limit})

    def _get_historical_futures_data(
        self,
        path: str,
        *,
        params: dict[str, Any],
        period: str,
        start_time: int,
        end_time: int,
        timestamp_key: str = "timestamp",
        limit_per_request: int = 500,
    ) -> List[Dict[str, Any]]:
        cursor = int(start_time)
        step_ms = self._interval_ms(period)
        rows: list[dict[str, Any]] = []
        while cursor <= end_time:
            batch = self._get_json(
                path,
                params={
                    **params,
                    "period": period,
                    "limit": min(limit_per_request, 500),
                    "startTime": cursor,
                    "endTime": int(end_time),
                },
            )
            if not batch:
                break
            rows.extend(batch)
            last_time = int(batch[-1].get(timestamp_key, 0))
            next_cursor = last_time + step_ms
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < min(limit_per_request, 500):
                break
        deduped: dict[int, dict[str, Any]] = {}
        for row in rows:
            timestamp = int(row.get(timestamp_key, 0))
            if timestamp:
                deduped[timestamp] = row
        return [deduped[key] for key in sorted(deduped)]

    def get_historical_open_interest_hist(
        self, symbol: str, period: str, start_time: int, end_time: int
    ) -> List[Dict[str, Any]]:
        return self._get_historical_futures_data(
            "/futures/data/openInterestHist",
            params={"symbol": symbol},
            period=period,
            start_time=start_time,
            end_time=end_time,
        )

    def get_funding_rates(self, symbol: str, limit: int = 100) -> List[Dict[str, Any]]:
        return self._get_json("/fapi/v1/fundingRate", params={"symbol": symbol, "limit": limit})

    def get_funding_rate_history(
        self,
        symbol: str,
        start_time: int,
        end_time: int,
        *,
        limit_per_request: int = 1000,
    ) -> List[Dict[str, Any]]:
        """Fetch the complete inclusive funding range from Binance's public API."""
        if end_time < start_time:
            raise ValueError("end_time must be greater than or equal to start_time")
        limit = max(1, min(int(limit_per_request), 1000))
        cursor = int(start_time)
        rows: list[dict[str, Any]] = []
        while cursor <= end_time:
            batch = self._get_json(
                "/fapi/v1/fundingRate",
                params={
                    "symbol": symbol,
                    "startTime": cursor,
                    "endTime": int(end_time),
                    "limit": limit,
                },
            )
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(row for row in batch if isinstance(row, dict))
            timestamps = []
            for row in batch:
                try:
                    timestamps.append(int(row["fundingTime"]))
                except (KeyError, TypeError, ValueError):
                    continue
            if not timestamps:
                break
            next_cursor = max(timestamps) + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < limit:
                break

        deduped: dict[int, dict[str, Any]] = {}
        for row in rows:
            try:
                timestamp = int(row["fundingTime"])
            except (KeyError, TypeError, ValueError):
                continue
            if start_time <= timestamp <= end_time:
                deduped[timestamp] = row
        return [deduped[timestamp] for timestamp in sorted(deduped)]

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

    def get_historical_taker_buy_sell_volume(
        self, symbol: str, period: str, start_time: int, end_time: int
    ) -> List[Dict[str, Any]]:
        return self._get_historical_futures_data(
            "/futures/data/takerlongshortRatio",
            params={"symbol": symbol},
            period=period,
            start_time=start_time,
            end_time=end_time,
        )

    def get_historical_basis(
        self, symbol: str, period: str, start_time: int, end_time: int
    ) -> List[Dict[str, Any]]:
        return self._get_historical_futures_data(
            "/futures/data/basis",
            params={"pair": symbol, "contractType": "PERPETUAL"},
            period=period,
            start_time=start_time,
            end_time=end_time,
        )

    def get_global_long_short_account_ratio(
        self, symbol: str, period: str = "5m", limit: int = 100
    ) -> List[Dict[str, Any]]:
        return self._get_json(
            "/futures/data/globalLongShortAccountRatio",
            params={"symbol": symbol, "period": period, "limit": limit},
        )

    def get_historical_global_long_short_account_ratio(
        self, symbol: str, period: str, start_time: int, end_time: int
    ) -> List[Dict[str, Any]]:
        return self._get_historical_futures_data(
            "/futures/data/globalLongShortAccountRatio",
            params={"symbol": symbol}, period=period, start_time=start_time, end_time=end_time,
        )

    def get_top_long_short_account_ratio(
        self, symbol: str, period: str = "5m", limit: int = 100
    ) -> List[Dict[str, Any]]:
        return self._get_json(
            "/futures/data/topLongShortAccountRatio",
            params={"symbol": symbol, "period": period, "limit": limit},
        )

    def get_historical_top_long_short_account_ratio(
        self, symbol: str, period: str, start_time: int, end_time: int
    ) -> List[Dict[str, Any]]:
        return self._get_historical_futures_data(
            "/futures/data/topLongShortAccountRatio",
            params={"symbol": symbol}, period=period, start_time=start_time, end_time=end_time,
        )

    def get_top_long_short_position_ratio(
        self, symbol: str, period: str = "5m", limit: int = 100
    ) -> List[Dict[str, Any]]:
        return self._get_json(
            "/futures/data/topLongShortPositionRatio",
            params={"symbol": symbol, "period": period, "limit": limit},
        )

    def get_historical_top_long_short_position_ratio(
        self, symbol: str, period: str, start_time: int, end_time: int
    ) -> List[Dict[str, Any]]:
        return self._get_historical_futures_data(
            "/futures/data/topLongShortPositionRatio",
            params={"symbol": symbol}, period=period, start_time=start_time, end_time=end_time,
        )
