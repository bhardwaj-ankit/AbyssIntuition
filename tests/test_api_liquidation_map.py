from fastapi.testclient import TestClient

from liquidity_signal.api.app import app, engine
from liquidity_signal.features.liquidation_map import build_liquidation_map_advanced


client = TestClient(app)


def _sample_klines(up: bool = True) -> list[list[float]]:
    closes = [100.0, 101.0, 102.0, 103.0] if up else [103.0, 102.0, 101.0, 100.0]
    rows: list[list[float]] = []
    for c in closes:
        rows.append([0.0, c, c, c, c, 0.0])
    return rows


def _sample_oi() -> list[dict[str, str]]:
    return [
        {"sumOpenInterestValue": "1000000000"},
        {"sumOpenInterestValue": "1200000000"},
    ]


def test_liquidation_map_default_route_uses_advanced_model(monkeypatch) -> None:
    def fake_generate(
        symbol: str,
        include_events: bool,
        event_limit: int,
        range_pct: float,
        resolution: int,
        history_points: int,
    ):
        return build_liquidation_map_advanced(
            symbol=symbol,
            mark_price=100.0,
            oi_hist=_sample_oi(),
            klines=_sample_klines(),
            funding_rates=[{"fundingRate": "0.0001"}],
            bybit_events_raw=[{"side": "Sell", "price": "101.0", "size": "9", "updatedTime": "1710000000001"}],
            include_events=include_events,
            range_pct=range_pct,
            resolution=resolution,
            history_points=history_points,
        )

    monkeypatch.setattr(engine, "generate_liquidation_map_advanced", fake_generate)

    response = client.get("/liquidation-map?symbol=BTCUSDT&include_events=true&event_limit=30")
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "BTCUSDT"
    assert "quality" in body
    assert "events" in body
    assert "levels_above" in body
    assert "heatmap" in body
    assert "market_metrics" in body
    assert body["quality"]["event_overlay_active"] is True


def test_liquidation_map_route_supports_event_toggle(monkeypatch) -> None:
    def fake_generate(
        symbol: str,
        include_events: bool,
        event_limit: int,
        range_pct: float,
        resolution: int,
        history_points: int,
    ):
        bybit_events = (
            [{"side": "Sell", "price": "101.0", "size": "9", "updatedTime": "1710000000001"}]
            if include_events
            else []
        )
        return build_liquidation_map_advanced(
            symbol=symbol,
            mark_price=100.0,
            oi_hist=_sample_oi(),
            klines=_sample_klines(),
            funding_rates=[{"fundingRate": "0.0001"}],
            bybit_events_raw=bybit_events,
            include_events=include_events,
            range_pct=range_pct,
            resolution=resolution,
            history_points=history_points,
        )

    monkeypatch.setattr(engine, "generate_liquidation_map_advanced", fake_generate)

    response = client.get("/liquidation-map?symbol=BTCUSDT&include_events=false&event_limit=30")
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "BTCUSDT"
    assert body["quality"]["event_weight"] == 0.0
    assert body["quality"]["event_overlay_active"] is False


def test_liquidation_events_route_returns_event_points(monkeypatch) -> None:
    monkeypatch.setattr(
        engine,
        "generate_liquidation_events",
        lambda symbol, limit: build_liquidation_map_advanced(
            symbol=symbol,
            mark_price=100.0,
            oi_hist=_sample_oi(),
            klines=_sample_klines(),
            funding_rates=[{"fundingRate": "0.0001"}],
            bybit_events_raw=[{"side": "Buy", "price": "99.0", "size": "9", "updatedTime": "1710000000001"}],
            include_events=True,
        ).events[:limit],
    )

    response = client.get("/liquidation/events?symbol=BTCUSDT&limit=10")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["symbol"] == "BTCUSDT"
    assert body[0]["liquidated_side"] == "LONG"
