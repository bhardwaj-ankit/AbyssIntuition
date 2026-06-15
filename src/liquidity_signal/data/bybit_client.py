from __future__ import annotations

import hashlib
import hmac
import json
import random
import time
from urllib.parse import urlencode
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


class BybitDemoTradingClient:
    MAX_RETRIES = 3
    BACKOFF_SECONDS = 0.25

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = "https://api-demo.bybit.com",
        recv_window: int = 5000,
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.base_url = base_url.rstrip("/")
        self.recv_window = recv_window
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _timestamp_ms(self) -> int:
        return int(time.time() * 1000)

    def _sign(self, timestamp_ms: int, payload: str) -> str:
        raw = f"{timestamp_ms}{self.api_key}{self.recv_window}{payload}"
        return hmac.new(self.api_secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()

    def _auth_headers(self, timestamp_ms: int, payload: str) -> dict[str, str]:
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": str(timestamp_ms),
            "X-BAPI-RECV-WINDOW": str(self.recv_window),
            "X-BAPI-SIGN": self._sign(timestamp_ms, payload),
            "Content-Type": "application/json",
        }

    @staticmethod
    def _build_get_payload(params: dict[str, Any]) -> str:
        return urlencode([(key, value) for key, value in params.items() if value is not None])

    @staticmethod
    def _build_post_payload(payload: dict[str, Any]) -> str:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> Any:
        request_method = method.upper()
        body = payload or {}
        query = params or {}
        last_error: str | None = None

        for attempt in range(self.MAX_RETRIES):
            try:
                headers: dict[str, str] | None = None
                if auth:
                    timestamp_ms = self._timestamp_ms()
                    if request_method == "GET":
                        sign_payload = self._build_get_payload(query)
                    else:
                        sign_payload = self._build_post_payload(body)
                    headers = self._auth_headers(timestamp_ms, sign_payload)

                response = self._client.request(
                    request_method,
                    path,
                    params=query if request_method == "GET" else None,
                    json=body if request_method != "GET" else None,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict) and data.get("retCode") not in (0, "0", None):
                    raise RuntimeError(f"Bybit error {data.get('retCode')}: {data.get('retMsg')}")
                return data.get("result", data)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError, ValueError, RuntimeError) as exc:
                last_error = str(exc)
                if attempt < self.MAX_RETRIES - 1:
                    jitter = random.uniform(0.0, 0.05)
                    time.sleep(self.BACKOFF_SECONDS * (2**attempt) + jitter)

        raise RuntimeError(f"Bybit demo request failed after retries: {last_error or 'unknown error'}")

    def get_instrument_info(self, symbol: str, category: str = "linear") -> dict[str, Any]:
        result = self._request(
            "GET",
            "/v5/market/instruments-info",
            params={"category": category, "symbol": symbol.upper()},
        )
        rows = result.get("list", []) if isinstance(result, dict) else []
        return rows[0] if rows else {}

    def get_ticker(self, symbol: str, category: str = "linear") -> dict[str, Any]:
        result = self._request(
            "GET",
            "/v5/market/tickers",
            params={"category": category, "symbol": symbol.upper()},
        )
        rows = result.get("list", []) if isinstance(result, dict) else []
        return rows[0] if rows else {}

    def get_wallet_balance(self, *, account_type: str = "UNIFIED", coin: str = "USDT") -> dict[str, Any]:
        result = self._request(
            "GET",
            "/v5/account/wallet-balance",
            params={"accountType": account_type, "coin": coin.upper()},
            auth=True,
        )
        rows = result.get("list", []) if isinstance(result, dict) else []
        if not rows:
            return {}
        account = rows[0]
        coins = account.get("coin", []) if isinstance(account, dict) else []
        chosen = next((row for row in coins if isinstance(row, dict) and row.get("coin") == coin.upper()), {})
        return {
            "accountType": account.get("accountType"),
            "totalEquity": account.get("totalEquity"),
            "totalWalletBalance": account.get("totalWalletBalance"),
            "totalAvailableBalance": account.get("totalAvailableBalance"),
            "availableToWithdraw": chosen.get("availableToWithdraw"),
            "walletBalance": chosen.get("walletBalance"),
            "availableToBorrow": chosen.get("availableToBorrow"),
            "coin": chosen,
        }

    def get_positions(self, symbol: str, category: str = "linear") -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            "/v5/position/list",
            params={"category": category, "symbol": symbol.upper()},
            auth=True,
        )
        rows = result.get("list", []) if isinstance(result, dict) else []
        return rows if isinstance(rows, list) else []

    def get_open_orders(self, symbol: str, category: str = "linear") -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            "/v5/order/realtime",
            params={"category": category, "symbol": symbol.upper()},
            auth=True,
        )
        rows = result.get("list", []) if isinstance(result, dict) else []
        return rows if isinstance(rows, list) else []

    def get_closed_pnl(
        self,
        symbol: str,
        *,
        category: str = "linear",
        limit: int = 50,
        start_time: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": category, "symbol": symbol.upper(), "limit": max(1, min(limit, 100))}
        if start_time is not None:
            params["startTime"] = start_time
        result = self._request(
            "GET",
            "/v5/position/closed-pnl",
            params=params,
            auth=True,
        )
        rows = result.get("list", []) if isinstance(result, dict) else []
        return rows if isinstance(rows, list) else []

    def set_leverage(self, symbol: str, leverage: float, category: str = "linear") -> dict[str, Any]:
        leverage_value = self._decimal_string(leverage, places=2)
        return self._request(
            "POST",
            "/v5/position/set-leverage",
            payload={
                "category": category,
                "symbol": symbol.upper(),
                "buyLeverage": leverage_value,
                "sellLeverage": leverage_value,
            },
            auth=True,
        )

    def cancel_all_orders(self, symbol: str, category: str = "linear") -> dict[str, Any]:
        return self._request(
            "POST",
            "/v5/order/cancel-all",
            payload={"category": category, "symbol": symbol.upper()},
            auth=True,
        )

    def place_order(
        self,
        *,
        category: str,
        symbol: str,
        side: str,
        qty: str,
        order_link_id: str,
        order_type: str = "Market",
        price: str | None = None,
        time_in_force: str | None = None,
        reduce_only: bool = False,
        take_profit: str | None = None,
        stop_loss: str | None = None,
        position_idx: int = 0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "category": category,
            "symbol": symbol.upper(),
            "side": side,
            "orderType": order_type,
            "qty": qty,
            "positionIdx": position_idx,
            "reduceOnly": reduce_only,
            "orderLinkId": order_link_id,
        }
        if time_in_force is not None:
            payload["timeInForce"] = time_in_force
        elif order_type == "Market":
            payload["timeInForce"] = "IOC"
        else:
            payload["timeInForce"] = "GTC"
        if price is not None:
            payload["price"] = price
        if not reduce_only:
            payload["tpslMode"] = "Full"
            if take_profit is not None:
                payload["takeProfit"] = take_profit
                payload["tpTriggerBy"] = "MarkPrice"
            if stop_loss is not None:
                payload["stopLoss"] = stop_loss
                payload["slTriggerBy"] = "MarkPrice"
        return self._request(
            "POST",
            "/v5/order/create",
            payload=payload,
            auth=True,
        )

    def set_trading_stop(
        self,
        *,
        category: str,
        symbol: str,
        stop_loss: str | None = None,
        take_profit: str | None = None,
        trailing_stop: str | None = None,
        active_price: str | None = None,
        position_idx: int = 0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "category": category,
            "symbol": symbol.upper(),
            "tpslMode": "Full",
            "positionIdx": position_idx,
        }
        if take_profit is not None:
            payload["takeProfit"] = take_profit
            payload["tpTriggerBy"] = "MarkPrice"
        if stop_loss is not None:
            payload["stopLoss"] = stop_loss
            payload["slTriggerBy"] = "MarkPrice"
        if trailing_stop is not None:
            payload["trailingStop"] = trailing_stop
        if active_price is not None:
            payload["activePrice"] = active_price
        return self._request(
            "POST",
            "/v5/position/trading-stop",
            payload=payload,
            auth=True,
        )

    @staticmethod
    def _decimal_string(value: float, places: int = 8) -> str:
        text = f"{value:.{places}f}"
        return text.rstrip("0").rstrip(".") or "0"
