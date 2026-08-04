"""Заполняет drive_minutes в seed/data/places.json через публичный OSRM.

Запуск: `uv run python -m seed.enrich_drive_times` (нужна сеть).
Значения пишутся в JSON, чтобы обычный сид оставался офлайн-способным.
"""

import json
import time
from pathlib import Path

import httpx

TASHKENT = (41.3111, 69.2797)
OSRM = "http://router.project-osrm.org/route/v1/driving/{lng1},{lat1};{lng2},{lat2}?overview=false"

DATA = Path(__file__).resolve().parent / "data" / "places.json"


def main() -> None:
    payload = json.loads(DATA.read_text())
    with httpx.Client(timeout=20) as client:
        for place in payload["places"]:
            url = OSRM.format(
                lng1=TASHKENT[1], lat1=TASHKENT[0], lng2=place["lng"], lat2=place["lat"]
            )
            resp = client.get(url)
            resp.raise_for_status()
            routes = resp.json().get("routes") or []
            if not routes:
                print(f"{place['slug']}: маршрут не найден, пропуск")
                continue
            minutes = round(routes[0]["duration"] / 60)
            place["drive_minutes"] = minutes
            print(f"{place['slug']}: {minutes} мин")
            time.sleep(1)  # публичный OSRM просит ≤1 rps

    DATA.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print("places.json обновлён")


if __name__ == "__main__":
    main()
