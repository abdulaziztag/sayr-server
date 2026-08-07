"""Прокси Open-Meteo с кэшем в памяти.

Прогноз в горах сильно зависит от высоты, поэтому elevation передаётся в API.
"""

import asyncio
import time
from datetime import datetime, timezone

import httpx

from ..config import settings

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
DAILY_FIELDS = (
    "temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
    "weather_code,wind_speed_10m_max"
)
# Почасовой нужен для ленты «ровно на длину похода» после нажатия «Пойду»
HOURLY_FIELDS = "temperature_2m,precipitation_probability,wind_speed_10m,weather_code"

_cache: dict[str, tuple[float, dict]] = {}
_locks: dict[str, asyncio.Lock] = {}


async def _fetch_raw(params: dict) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        return resp.json()


async def get_forecast(key: str, lat: float, lng: float, elevation_m: int | None) -> dict:
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < settings.weather_cache_ttl_sec:
        return hit[1]

    # Лок на место, а не на всё: с общим локом прогноз второго места ждал,
    # пока Open-Meteo ответит первому
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < settings.weather_cache_ttl_sec:
            return hit[1]

        params: dict = {
            "latitude": lat,
            "longitude": lng,
            "daily": DAILY_FIELDS,
            "hourly": HOURLY_FIELDS,
            "timezone": "Asia/Tashkent",
            "forecast_days": 7,
        }
        if elevation_m is not None:
            params["elevation"] = elevation_m

        raw = await _fetch_raw(params)

        daily = raw["daily"]
        hourly = raw.get("hourly") or {}
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "days": [
                {
                    "date": daily["time"][i],
                    "t_min": daily["temperature_2m_min"][i],
                    "t_max": daily["temperature_2m_max"][i],
                    "precip_prob": daily["precipitation_probability_max"][i],
                    "wind_max": daily["wind_speed_10m_max"][i],
                    "weathercode": daily["weather_code"][i],
                }
                for i in range(len(daily["time"]))
            ],
            "hours": [
                {
                    # ISO без таймзоны, местное время места: «2026-08-05T08:00»
                    "time": hourly["time"][i],
                    "t": hourly["temperature_2m"][i],
                    "precip_prob": hourly["precipitation_probability"][i],
                    "wind": hourly["wind_speed_10m"][i],
                    "weathercode": hourly["weather_code"][i],
                }
                for i in range(len(hourly.get("time", [])))
            ],
        }
        _cache[key] = (time.monotonic(), payload)
        return payload
