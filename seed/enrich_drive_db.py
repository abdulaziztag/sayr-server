"""Считает время в дороге от Ташкента для мест, где его нет.

    uv run python -m seed.enrich_drive_db            # показать
    uv run python -m seed.enrich_drive_db --apply    # записать

Зачем это не косметика. Окно выезда считается формулой
«закат − (дорога×2 + ход×1,5 + запас)», и когда времени в дороге нет,
клиент подставляет полтора часа по умолчанию (ios TripPlan.swift). Для
Чимгана это почти правда, а для Сангардака, куда ехать восемь часов, —
обещание, по которому человек выедет затемно и приедет к ночи.

Маршруты берём у публичного OSRM, по одному запросу на место, с паузой:
чужой сервис, и полсотни запросов подряд ему ни к чему. Считаем только
пустые поля — выверенные руками значения точнее наших.

Точка назначения — начало маршрута, если оно известно, иначе само место:
машину подают к тропе, а не на вершину.
"""

import argparse
import asyncio
import time

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.models import Place

TASHKENT = (41.3111, 69.2797)
OSRM = (
    "http://router.project-osrm.org/route/v1/driving/"
    "{lng1},{lat1};{lng2},{lat2}?overview=false"
)
PAUSE_SEC = 0.4


async def run(apply: bool) -> None:
    async with SessionLocal() as session:
        places = (
            (
                await session.execute(
                    select(Place)
                    .options(selectinload(Place.tracks))
                    .where(Place.drive_minutes.is_(None))
                    .order_by(Place.name)
                )
            )
            .unique()
            .scalars()
            .all()
        )
        print(f"Мест без времени в дороге: {len(places)}\n")

        done = failed = 0
        with httpx.Client(timeout=25) as client:
            for place in places:
                # К началу тропы, а не к цели: на вершину машину не подать
                start = next(
                    (
                        (t.start_lat, t.start_lng)
                        for t in place.tracks
                        if t.start_lat is not None and t.start_lng is not None
                    ),
                    (place.lat, place.lng),
                )
                url = OSRM.format(
                    lng1=TASHKENT[1], lat1=TASHKENT[0], lng2=start[1], lat2=start[0]
                )
                try:
                    routes = client.get(url).raise_for_status().json().get("routes") or []
                except Exception as exc:  # noqa: BLE001 — одно место не повод падать
                    print(f"  ! {place.name[:30]:30} {type(exc).__name__}")
                    failed += 1
                    continue
                if not routes:
                    print(f"  ! {place.name[:30]:30} маршрут не найден")
                    failed += 1
                    continue

                minutes = round(routes[0]["duration"] / 60)
                km = round(routes[0]["distance"] / 1000, 1)
                done += 1
                print(f"  {place.name[:30]:30} {minutes // 60}:{minutes % 60:02} · {km} км")
                if apply:
                    place.drive_minutes = minutes
                    place.drive_km = km
                time.sleep(PAUSE_SEC)

        if apply:
            await session.commit()
            print(f"\nГотово: посчитано {done}, не вышло {failed}.")
        else:
            print(f"\nПоказ без изменений: посчитается {done}, не вышло {failed}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    asyncio.run(run(parser.parse_args().apply))


if __name__ == "__main__":
    main()
