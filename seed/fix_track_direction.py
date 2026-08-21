"""Разворачивает треки, записанные ОТ цели вниз, а не к ней.

    uv run python -m seed.fix_track_direction              # показать, что будет
    uv run python -m seed.fix_track_direction --apply      # развернуть файлы и треки
    uv run python -m seed.fix_track_direction --apply --places   # плюс числа мест

Зачем. Часть записей сделана на спуске: файл начинается у самой цели
и уходит вниз к дороге. Из-за этого ломается сразу двое:

- точкой старта становится сама цель, и человек вбивает в автонавигатор
  вершину вместо парковки («начало маршрута» на карточке места);
- набор высоты считается в направлении спуска — у Большого Чимгана
  выходило 22 метра вместо полутора тысяч.

Чиним сам файл, а не только числа в базе: клиенты считают набор
самостоятельно по скачанному GPX и ставят флаг «Старт» на его первую
точку (ios GPX.swift, android Gpx.kt). Правка одних лишь колонок
до просмотрщика трека не доехала бы.

Имя файла не меняется намеренно — иначе ссылки в уже скачанных
офлайн-комплектах уйдут в никуда. Оригиналы складываем рядом,
в gpx-before-normalize: перезапись чужой записи должна быть обратимой.

Числа МЕСТА (elevation_gain_m, duration_hours) трогаем только с --places
и только там, где они в точности совпадают со старым значением первого
трека, — это доказывает, что их скопировал скрипт, а не выставил человек.
"""

import argparse
import asyncio
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import GPX_DIR, SERVER_DIR
from app.db import SessionLocal
from app.models import Place, PlaceTrack
from app.services.gpx import recorded_from_target, reverse_track, track_stats

BACKUP_DIR = SERVER_DIR / "gpx-before-normalize"

# Формула Найсмита, та же, что в seed/estimate_duration.py: километры делить
# на четыре плюс набор делить на шестьсот, округление до получаса
def naismith_hours(distance_km: float, ascent_m: int) -> float:
    return round((distance_km / 4 + ascent_m / 600) * 2) / 2


async def run(apply: bool, touch_places: bool) -> None:
    async with SessionLocal() as session:
        places = (
            (await session.execute(select(Place).options(selectinload(Place.tracks))))
            .unique()
            .scalars()
            .all()
        )

        flipped: list[tuple[Place, PlaceTrack, dict]] = []
        for place in places:
            for track in place.tracks:
                if not track.gpx_file:
                    continue
                path = GPX_DIR / Path(str(track.gpx_file)).name
                if not path.exists():
                    print(f"  ! нет файла: {path.name}")
                    continue
                data = path.read_bytes()
                if not recorded_from_target(data, place.lat, place.lng):
                    continue
                new_data = reverse_track(data)
                stats = track_stats(new_data)
                flipped.append((place, track, {"data": new_data, "stats": stats, "path": path}))

        if not flipped:
            print("  Развёрнутых треков не нашлось.")
            return

        print(f"Треков записано от цели вниз: {len(flipped)}\n")
        print(f"  {'место':26} {'трек':26} {'набор':>14}  {'старт':>10}")
        for place, track, info in flipped:
            stats = info["stats"]
            moved = "переедет" if track.start_lat != stats.start_lat else "тот же"
            print(
                f"  {place.name[:26]:26} {track.name[:26]:26} "
                f"{track.ascent_m:5} → {stats.ascent_m:5} м  {moved:>10}"
            )

        # Числа места: правим только те, что в точности равны старому треку
        place_updates = []
        for place, track, info in flipped:
            first = sorted(place.tracks, key=lambda t: (t.sort_order, t.id))[0]
            if first.id != track.id:
                continue  # карточка места берёт первый трек, этот не он
            if place.elevation_gain_m != track.ascent_m:
                continue  # значение выставлено руками — не наше дело
            stats = info["stats"]
            was_hours = place.duration_hours
            fits = was_hours == naismith_hours(place.distance_km or 0, track.ascent_m)
            new_hours = naismith_hours(place.distance_km or 0, stats.ascent_m) if fits else was_hours
            place_updates.append((place, stats.ascent_m, new_hours, fits))

        if place_updates:
            print(f"\nМест, чьи числа считаны из этих треков: {len(place_updates)}\n")
            print(f"  {'место':26} {'набор':>14}  {'ход':>14}")
            for place, gain, hours, fits in place_updates:
                mark = "" if fits else "  (время выставлено руками, не трогаем)"
                print(
                    f"  {place.name[:26]:26} {place.elevation_gain_m:5} → {gain:5} м  "
                    f"{str(place.duration_hours):>5} → {str(hours):>5} ч{mark}"
                )

        if not apply:
            print("\nПоказ без изменений. Записать — ключ --apply.")
            return

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        for place, track, info in flipped:
            path: Path = info["path"]
            shutil.copyfile(path, BACKUP_DIR / path.name)
            path.write_bytes(info["data"])
            stats = info["stats"]
            track.distance_km = stats.distance_km
            track.ascent_m = stats.ascent_m
            track.start_lat = stats.start_lat
            track.start_lng = stats.start_lng

        if touch_places:
            for place, gain, hours, _fits in place_updates:
                place.elevation_gain_m = gain
                place.duration_hours = hours

        await session.commit()
        print(f"\nГотово: развёрнуто {len(flipped)} треков, оригиналы в {BACKUP_DIR}.")
        if touch_places:
            print(f"Числа обновлены у {len(place_updates)} мест.")
        else:
            print("Числа мест не тронуты — для них ключ --places.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    parser.add_argument("--places", action="store_true", help="обновить и числа мест")
    args = parser.parse_args()
    asyncio.run(run(args.apply, args.places))


if __name__ == "__main__":
    main()
