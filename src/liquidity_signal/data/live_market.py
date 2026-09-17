"""Persist timestamped public REST observations separately from archive features."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

import httpx

from liquidity_signal.data.binance_client import BinanceFuturesClient
from liquidity_signal.data.daily_refresh import write_refresh_report
from liquidity_signal.universe import ACTIVE_SYMBOLS


def capture_cycle(client, conn, symbols=ACTIVE_SYMBOLS):
    results = []
    for symbol in symbols:
        requests = {
            "mark_price": lambda symbol=symbol: client.get_mark_price_info(symbol),
            "order_book": lambda symbol=symbol: client.get_order_book(symbol, limit=100),
            "recent_trades": lambda symbol=symbol: client.get_recent_trades(symbol, limit=1000),
            "klines_1m": lambda symbol=symbol: client.get_klines(symbol, interval="1m", limit=10),
            "open_interest": lambda symbol=symbol: client.get_open_interest(symbol),
            "positioning": lambda symbol=symbol: client.get_global_long_short_account_ratio(symbol, limit=2),
            "top_accounts": lambda symbol=symbol: client.get_top_long_short_account_ratio(symbol, limit=2),
            "top_positions": lambda symbol=symbol: client.get_top_long_short_position_ratio(symbol, limit=2),
            "taker_flow": lambda symbol=symbol: client.get_taker_buy_sell_volume(symbol, limit=2),
        }
        for family, fetch in requests.items():
            started = int(time.time() * 1000)
            try:
                payload = fetch()
                if not payload:
                    raise ValueError("Empty source response")
                received = int(time.time() * 1000)
                with conn:
                    conn.execute(
                        "INSERT INTO live_market_observations VALUES(?,?,?,?,?,?)",
                        (symbol, family, started, received, "binance_rest", json.dumps(payload)),
                    )
                results.append({"symbol": symbol, "family": family, "received_at": received, "status": "complete"})
            except (httpx.HTTPError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
                results.append({"symbol": symbol, "family": family, "status": "failed", "error": str(exc)})
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval_seconds < 30:
        parser.error("interval must be at least 30 seconds")
    # A dedicated DB preserves raw live provenance and avoids changing archive semantics.
    conn = sqlite3.connect(args.data_root / "live_market.db", timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS live_market_observations(
        symbol TEXT, family TEXT, requested_at INTEGER, received_at INTEGER,
        source TEXT, raw_json TEXT,
        PRIMARY KEY(symbol, family, received_at))""")
    conn.commit()
    client = BinanceFuturesClient(timeout=15)
    try:
        while True:
            started = time.monotonic()
            results = capture_cycle(client, conn)
            report = {"observed_at": int(time.time() * 1000), "symbols": list(ACTIVE_SYMBOLS),
                      "status": "partial" if any(r["status"] == "failed" for r in results) else "complete",
                      "results": results, "interval_seconds": args.interval_seconds}
            write_refresh_report(report, str(args.data_root / "live_market_status.json"))
            print(json.dumps(report), flush=True)
            if args.once:
                break
            time.sleep(max(1, args.interval_seconds - (time.monotonic() - started)))
    finally:
        client.close()
        conn.close()


if __name__ == "__main__":
    main()
