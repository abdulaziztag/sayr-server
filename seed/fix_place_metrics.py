"""Поправить цифры маршрута по карте slug → поля.

    uv run python -m seed.fix_place_metrics seed/data/place_metrics_fix.json
    uv run python -m seed.fix_place_metrics seed/data/place_metrics_fix.json --apply

Зачем отдельный скрипт: `apply_cards` пропускает опубликованные места, а
править цифры надо именно у них. Первая карта — Большой Чимган: в каталоге
стояло 15 км и 12 часов хода, тогда как самый длинный трек карточки
(«Классика от Аксая») — 13,8 км, и владелец подтвердил 10 часов.

Цифры маршрута кормят окно выезда, поэтому ошибка в них двигает время
выезда на часы: у Чимгана правка переносит выезд с 0:53 на 3:17.

Правятся только четыре поля: distance_km, duration_hours, elevation_m,
elevation_gain_m. Дорога (drive_minutes/drive_km) считается скриптом
enrich_drive_times и руками не трогается.

Файл — список объектов {"slug", ...поля}. Те же значения надо положить
в seed/data/places.json, иначе следующий сид вернёт старые.

Перед --apply на проде — бэкап данных:
    sudo -n -u postgres pg_dump sayr | gzip > /var/backups/sayr/pre-metrics.sql.gz
"""

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Place

#: Что разрешено править этим скриптом. Всё остальное в карте — опечатка,
#: и лучше упасть, чем молча записать поле, которого не ждали
FIELDS = ("distance_km", "duration_hours", "elevation_m", "elevation_gain_m")


async def run(path: Path, apply: bool) -> None:
    rows = json.loads(path.read_text("utf-8"))
    for row in rows:
        unknown = set(row) - {"slug", "note"} - set(FIELDS)
        if unknown:
            raise SystemExit(f"{row.get('slug')}: неизвестные поля {sorted(unknown)}")

    changed = missing = 0
    async with SessionLocal() as session:
        for row in rows:
            place = (
                await session.execute(select(Place).where(Place.slug == row["slug"]))
            ).scalar_one_or_none()
            if place is None:
                print(f"  ?  {row['slug']}: нет в базе")
                missing += 1
                continue
            marks = []
            for field in FIELDS:
                if field not in row:
                    continue
                old, new = getattr(place, field), row[field]
                if old == new:
                    continue
                marks.append(f"{field} {old} → {new}")
                if apply:
                    setattr(place, field, new)
            if not marks:
                continue
            changed += 1
            print(f"  *  {row['slug']:26} " + " ; ".join(marks))
        if apply:
            await session.commit()
    verb = "записано" if apply else "к записи"
    tail = "" if apply else "  (добавьте --apply)"
    print(f"\n{verb}: {changed} мест, не найдено: {missing}{tail}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("path", type=Path)
    parser.add_argument("--apply", action="store_true", help="записать в базу")
    args = parser.parse_args()
    asyncio.run(run(args.path, args.apply))


if __name__ == "__main__":
    main()
