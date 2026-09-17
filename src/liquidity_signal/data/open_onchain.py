"""Free/open on-chain context from Coin Metrics Community and DefiLlama."""

from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from liquidity_signal.data.tls import httpx_verify

SYMBOL_NETWORK = {
    "BTCUSDT": "btc",
    "ETHUSDT": "eth",
    "PEPEUSDT": "eth",
    "SOLUSDT": "sol",
    "XRPUSDT": "xrp",
    "NEARUSDT": "near",
}

COINMETRICS_ASSET = {"btc": "btc", "eth": "eth", "xrp": "xrp"}
DEFILLAMA_CHAIN = {
    "btc": "Bitcoin",
    "eth": "Ethereum",
    "sol": "Solana",
    "xrp": "XRPL",
    "near": "Near",
}
METRICS = ("AdrActCnt", "TxCnt", "CapMrktCurUSD", "PriceUSD")


def _iso_day_ms(value: str) -> int:
    normalized = value.split("T", 1)[0]
    parsed = datetime.fromisoformat(normalized).replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


class OpenOnchainClient:
    COINMETRICS_URL = "https://community-api.coinmetrics.io/v4"
    DEFILLAMA_URL = "https://api.llama.fi"

    def __init__(self, timeout: float = 60.0) -> None:
        self._client = httpx.Client(timeout=timeout, verify=httpx_verify())

    def close(self) -> None:
        self._client.close()

    def coinmetrics_history(self, start_day: date, end_day: date) -> list[dict[str, Any]]:
        response = self._client.get(
            f"{self.COINMETRICS_URL}/timeseries/asset-metrics",
            params={
                "assets": ",".join(COINMETRICS_ASSET.values()),
                "metrics": ",".join(METRICS),
                "frequency": "1d",
                "start_time": start_day.isoformat(),
                "end_time": end_day.isoformat(),
                "page_size": 10_000,
            },
        )
        response.raise_for_status()
        rows: list[dict[str, Any]] = []
        payload = response.json()
        while True:
            for raw in payload.get("data", []):
                rows.append({
                    "network": str(raw["asset"]).lower(),
                    "ts": _iso_day_ms(raw["time"]),
                    "active_addresses": _float(raw.get("AdrActCnt")),
                    "tx_count": _float(raw.get("TxCnt")),
                    "market_cap_usd": _float(raw.get("CapMrktCurUSD")),
                    "price_usd": _float(raw.get("PriceUSD")),
                })
            next_url = payload.get("next_page_url")
            if not next_url:
                break
            next_response = self._client.get(next_url)
            next_response.raise_for_status()
            payload = next_response.json()
        return rows

    def defillama_tvl_history(
        self, network: str, start_day: date, end_day: date
    ) -> list[dict[str, Any]]:
        chain = DEFILLAMA_CHAIN[network]
        response = self._client.get(f"{self.DEFILLAMA_URL}/v2/historicalChainTvl/{chain}")
        response.raise_for_status()
        start_ts = int(datetime.combine(start_day, datetime.min.time(), timezone.utc).timestamp())
        end_ts = int(datetime.combine(end_day, datetime.max.time(), timezone.utc).timestamp())
        return [
            {
                "network": network,
                "ts": int(row["date"]) * 1000,
                "chain_tvl_usd": _float(row.get("tvl")),
            }
            for row in response.json()
            if start_ts <= int(row.get("date", 0)) <= end_ts
        ]


class OpenOnchainStore:
    def __init__(self, db_path: str = "runtime/onchain_data.db") -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS onchain_daily(
                network TEXT NOT NULL,
                ts INTEGER NOT NULL,
                active_addresses REAL,
                tx_count REAL,
                market_cap_usd REAL,
                price_usd REAL,
                chain_tvl_usd REAL,
                PRIMARY KEY(network, ts)
            )"""
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def upsert_coinmetrics(self, rows: list[dict[str, Any]]) -> int:
        self._conn.executemany(
            """INSERT INTO onchain_daily(
                   network, ts, active_addresses, tx_count, market_cap_usd, price_usd
               ) VALUES(?,?,?,?,?,?)
               ON CONFLICT(network, ts) DO UPDATE SET
                   active_addresses=excluded.active_addresses,
                   tx_count=excluded.tx_count,
                   market_cap_usd=excluded.market_cap_usd,
                   price_usd=excluded.price_usd""",
            [(
                row["network"], row["ts"], row.get("active_addresses"),
                row.get("tx_count"), row.get("market_cap_usd"), row.get("price_usd"),
            ) for row in rows],
        )
        self._conn.commit()
        return len(rows)

    def upsert_tvl(self, rows: list[dict[str, Any]]) -> int:
        self._conn.executemany(
            """INSERT INTO onchain_daily(network, ts, chain_tvl_usd) VALUES(?,?,?)
               ON CONFLICT(network, ts) DO UPDATE SET chain_tvl_usd=excluded.chain_tvl_usd""",
            [(row["network"], row["ts"], row.get("chain_tvl_usd")) for row in rows],
        )
        self._conn.commit()
        return len(rows)

    def ingest(
        self, client: OpenOnchainClient, start_day: date, end_day: date
    ) -> dict[str, Any]:
        coinmetrics = client.coinmetrics_history(start_day, end_day)
        cm_rows = self.upsert_coinmetrics(coinmetrics)
        tvl_rows: dict[str, int] = {}
        for network in DEFILLAMA_CHAIN:
            rows = client.defillama_tvl_history(network, start_day, end_day)
            tvl_rows[network] = self.upsert_tvl(rows)
        return {"coinmetrics_rows": cm_rows, "defillama_rows": tvl_rows}

    def nearest_completed_day(
        self, network: str, event_ts: int, tolerance_days: int = 3
    ) -> dict[str, Any] | None:
        # A daily row is safe for intraday use only after that UTC day closes.
        current_day_start = (event_ts // 86_400_000) * 86_400_000
        safe_ts = current_day_start - 86_400_000
        row = self._conn.execute(
            """SELECT ts, active_addresses, tx_count, market_cap_usd, price_usd,
                      chain_tvl_usd
               FROM onchain_daily WHERE network=? AND ts<=? AND ts>=?
               ORDER BY ts DESC LIMIT 1""",
            (network, safe_ts, safe_ts - tolerance_days * 86_400_000),
        ).fetchone()
        if not row:
            return None
        keys = (
            "ts", "active_addresses", "tx_count", "market_cap_usd", "price_usd",
            "chain_tvl_usd",
        )
        return dict(zip(keys, row))

    def change_pct(self, network: str, ts: int, column: str, days: int = 7) -> float | None:
        if column not in {"active_addresses", "tx_count", "market_cap_usd", "chain_tvl_usd"}:
            raise ValueError("Unsupported on-chain change column.")
        previous = self._conn.execute(
            f"SELECT {column} FROM onchain_daily WHERE network=? AND ts<=? "
            f"AND {column} IS NOT NULL ORDER BY ts DESC LIMIT 1",
            (network, ts - days * 86_400_000),
        ).fetchone()
        current = self._conn.execute(
            f"SELECT {column} FROM onchain_daily WHERE network=? AND ts<=? "
            f"AND {column} IS NOT NULL ORDER BY ts DESC LIMIT 1",
            (network, ts),
        ).fetchone()
        if not current or not previous or not previous[0]:
            return None
        return ((float(current[0]) - float(previous[0])) / float(previous[0])) * 100.0

    def coverage(self, network: str, start_ts: int, end_ts: int) -> dict[str, Any]:
        row = self._conn.execute(
            """SELECT COUNT(*), MIN(ts), MAX(ts),
                      SUM(chain_tvl_usd IS NOT NULL), SUM(active_addresses IS NOT NULL),
                      SUM(tx_count IS NOT NULL)
               FROM onchain_daily WHERE network=? AND ts BETWEEN ? AND ?""",
            (network, start_ts, end_ts),
        ).fetchone()
        return {
            "rows": row[0] or 0,
            "min_ts": row[1],
            "max_ts": row[2],
            "tvl_rows": row[3] or 0,
            "active_address_rows": row[4] or 0,
            "tx_count_rows": row[5] or 0,
        }
