from liquidity_signal.data.open_onchain import OpenOnchainStore


def test_onchain_store_uses_only_previous_completed_day(tmp_path) -> None:
    day = 86_400_000
    store = OpenOnchainStore(str(tmp_path / "onchain.db"))
    store.upsert_coinmetrics([
        {
            "network": "btc", "ts": day, "active_addresses": 100.0,
            "tx_count": 200.0, "market_cap_usd": 1_000.0, "price_usd": 10.0,
        },
        {
            "network": "btc", "ts": 2 * day, "active_addresses": 120.0,
            "tx_count": 240.0, "market_cap_usd": 1_100.0, "price_usd": 11.0,
        },
    ])
    store.upsert_tvl([
        {"network": "btc", "ts": day, "chain_tvl_usd": 50.0},
        {"network": "btc", "ts": 2 * day, "chain_tvl_usd": 55.0},
    ])

    row = store.nearest_completed_day("btc", 2 * day + 3_600_000)

    assert row["ts"] == day
    assert row["active_addresses"] == 100.0
    assert row["chain_tvl_usd"] == 50.0
    store.close()
