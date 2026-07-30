import httpx
import pytest

from app.services import weather

FAKE_DAILY = {
    "time": ["2026-07-30", "2026-07-31"],
    "temperature_2m_max": [34.1, 35.0],
    "temperature_2m_min": [21.0, 22.3],
    "precipitation_probability_max": [5, 10],
    "weather_code": [0, 2],
    "wind_speed_10m_max": [12.5, 14.0],
}


@pytest.fixture
def fake_open_meteo(monkeypatch):
    calls = {"count": 0}

    async def fake_get(self, url, params=None):
        calls["count"] += 1
        return httpx.Response(200, json={"daily": FAKE_DAILY}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    weather._cache.clear()
    return calls


async def test_weather_endpoint_and_cache(client, fake_open_meteo):
    resp1 = await client.get("/api/v1/places/test-peak/weather")
    assert resp1.status_code == 200
    days = resp1.json()["days"]
    assert len(days) == 2
    assert days[0]["t_max"] == 34.1
    assert days[0]["weathercode"] == 0

    resp2 = await client.get("/api/v1/places/test-peak/weather")
    assert resp2.status_code == 200
    assert fake_open_meteo["count"] == 1  # второй ответ — из кэша


async def test_weather_unknown_place(client, fake_open_meteo):
    resp = await client.get("/api/v1/places/nope/weather")
    assert resp.status_code == 404
