"""Матрица дороги «город выезда × место» через OSRM.

    uv run python -m seed.enrich_drive_times                  # показать диф
    uv run python -m seed.enrich_drive_times --apply          # записать матрицу
    uv run python -m seed.enrich_drive_times --apply --tashkent   # + поля мест от Ташкента

Города — app/cities.py (28), места — все из базы, включая черновики.
Результат — таблица place_drive_times; строка есть только у пар, которые
роутер построил. С --tashkent поля places.drive_minutes/drive_km
переписываются из строки Ташкента: их читают старые сборки и запасной
вариант новых, расходиться с матрицей они не должны.

Источник времени спрятан в одной функции `matrix()`: она единственная знает
про OSRM. Заменить роутер — заменить её, база и клиенты не трогаются.
Считается свободная дорога: хайкеры выезжают на рассвете, пробок нет.

Публичный OSRM принимает ≤100 координат на запрос и просит ≤1 rps, поэтому
и города, и места режутся на пачки (`batches`). Раз в месяц по таймеру
(deploy/sayr-drive-times.timer): дороги в OSM правят, значения плывут.
"""

import argparse
import asyncio
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from app.cities import CITIES, TASHKENT
from app.db import SessionLocal
from app.models import Place, PlaceDriveTime

Coord = tuple[float, float]  # (lat, lng)
Cell = tuple[int, float] | None  # (минуты, км) или нет дороги
MatrixFn = Callable[[Sequence[Coord], Sequence[Coord]], list[list[Cell]]]

OSRM_TABLE = "http://router.project-osrm.org/table/v1/driving/{coords}"
LIMIT = 100  # координат на запрос у публичного сервера
SRC_CHUNK = 14  # городов в пачке: 28 городов → две пачки
#: Разница меньше этого не считается изменением в дифе: OSRM шумит на минуты
REPORT_DELTA_MIN = 5


def batches(n_src: int, n_dst: int, limit: int = LIMIT, src_chunk: int = SRC_CHUNK) -> list[tuple[range, range]]:
    """Пары (диапазон городов, диапазон мест), в каждой ≤ limit координат."""
    src_chunk = min(src_chunk, n_src) or 1
    dst_chunk = max(1, limit - src_chunk)
    out = []
    for s in range(0, n_src, src_chunk):
        for d in range(0, n_dst, dst_chunk):
            out.append((range(s, min(s + src_chunk, n_src)), range(d, min(d + dst_chunk, n_dst))))
    return out


def matrix(origins: Sequence[Coord], destinations: Sequence[Coord]) -> list[list[Cell]]:
    """Одна пачка: OSRM table, строки — города, столбцы — места."""
    coords = ";".join(f"{lng},{lat}" for lat, lng in [*origins, *destinations])
    n = len(origins)
    params = {
        "sources": ";".join(str(i) for i in range(n)),
        "destinations": ";".join(str(n + j) for j in range(len(destinations))),
        "annotations": "duration,distance",
    }
    with httpx.Client(timeout=60) as client:
        resp = client.get(OSRM_TABLE.format(coords=coords), params=params)
        resp.raise_for_status()
        data = resp.json()
    durations = data.get("durations") or []
    distances = data.get("distances") or []
    out: list[list[Cell]] = []
    for i in range(n):
        row: list[Cell] = []
        for j in range(len(destinations)):
            sec = durations[i][j] if i < len(durations) and j < len(durations[i]) else None
            met = distances[i][j] if i < len(distances) and j < len(distances[i]) else None
            row.append(None if sec is None or met is None else (round(sec / 60), round(met / 1000, 1)))
        out.append(row)
    return out


def compute(origins: Sequence[Coord], destinations: Sequence[Coord], fn: MatrixFn, pause: float = 1.1) -> dict[tuple[int, int], Cell]:
    """Вся матрица пачками; ключ — (индекс города, индекс места)."""
    table: dict[tuple[int, int], Cell] = {}
    parts = batches(len(origins), len(destinations))
    for k, (src, dst) in enumerate(parts):
        block = fn([origins[i] for i in src], [destinations[j] for j in dst])
        for bi, i in enumerate(src):
            for bj, j in enumerate(dst):
                table[(i, j)] = block[bi][bj]
        if k + 1 < len(parts) and pause:
            time.sleep(pause)
    return table


async def run(apply: bool, tashkent: bool = False, fn: MatrixFn = matrix, pause: float = 1.1) -> dict[str, int]:
    stats = {"added": 0, "changed": 0, "removed": 0, "missing": 0, "pairs": 0}
    async with SessionLocal() as session:
        places = (await session.execute(select(Place).order_by(Place.slug))).scalars().all()
        existing = {
            (r.place_id, r.city): r
            for r in (await session.execute(select(PlaceDriveTime))).scalars().all()
        }
        table = compute([(c.lat, c.lng) for c in CITIES], [(p.lat, p.lng) for p in places], fn, pause)
        now = datetime.now(UTC)
        for ci, city in enumerate(CITIES):
            for pi, place in enumerate(places):
                cell = table.get((ci, pi))
                old = existing.get((place.id, city.code))
                if cell is None:
                    stats["missing"] += 1
                    if old is not None:
                        stats["removed"] += 1
                        print(f"  −  {place.slug:28} {city.code:12} дороги больше нет")
                        if apply:
                            await session.delete(old)
                    continue
                stats["pairs"] += 1
                minutes, km = cell
                if old is None:
                    stats["added"] += 1
                    if apply:
                        session.add(PlaceDriveTime(place_id=place.id, city=city.code, minutes=minutes, km=km, computed_at=now))
                    continue
                if abs(old.minutes - minutes) > REPORT_DELTA_MIN:
                    stats["changed"] += 1
                    print(f"  *  {place.slug:28} {city.code:12} {old.minutes} → {minutes} мин")
                if apply and (old.minutes != minutes or old.km != km):
                    old.minutes, old.km, old.computed_at = minutes, km, now
        if tashkent:
            ti = CITIES.index(TASHKENT)
            for pi, place in enumerate(places):
                cell = table.get((ti, pi))
                if cell and apply:
                    place.drive_minutes, place.drive_km = cell[0], float(cell[1])
        if apply:
            await session.commit()
    verb = "записано" if apply else "к записи"
    print(
        f"\n{verb}: пар {stats['pairs']}, новых {stats['added']}, изменилось больше чем на "
        f"{REPORT_DELTA_MIN} мин {stats['changed']}, пропало {stats['removed']}, без дороги {stats['missing']}"
        + ("" if apply else "  (добавьте --apply)")
    )
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="записать в базу (без флага — только показать)")
    parser.add_argument("--tashkent", action="store_true", help="обновить и поля мест drive_minutes/drive_km из строки Ташкента")
    args = parser.parse_args()
    asyncio.run(run(args.apply, args.tashkent))


if __name__ == "__main__":
    main()
