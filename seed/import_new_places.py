"""Заводит новые места ЧЕРНОВИКАМИ: `uv run python -m seed.import_new_places`.

    uv run python -m seed.import_new_places            # показать, что будет
    uv run python -m seed.import_new_places --apply    # выполнить

Пятьдесят точек tabiatsari, которых нет в каталоге: вершины, водопады,
озёра, каньон и пещера. Всё, что можно вывести из данных, проставляется
само — координаты, высота, категория, регион, сложность, треки и цифры
маршрута. Всё, что вывести нельзя, остаётся человеку.

Места заводятся с is_published = False и в приложении не показываются.
Название и описание нужно написать руками через админку, там же и
публиковать. Транслитерация имени — заготовка, чтобы было от чего
оттолкнуться: «Qoraqush» станет «Коракуш», но привычное русское имя
она не восстановит.
"""

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

try:
    from fastapi_storages import StorageFile
except ImportError:  # расположение менялось между версиями пакета
    from fastapi_storages.base import StorageFile

from app.config import GPX_DIR, PHOTOS_DIR
from app.db import SessionLocal
from app.models import (
    Difficulty,
    Place,
    PlaceCategory,
    PlacePhoto,
    PlaceTrack,
    Region,
    gpx_storage,
    photo_storage,
)
from app.services.images import make_thumbnail

from . import tabiatsari as ts

DATA_DIR = Path(__file__).resolve().parent / "data"
PLAN_FILE = DATA_DIR / "new_places.json"
GPX_SRC = DATA_DIR / "newplaces"

MAX_PHOTOS = 10
DAY_KM = 30.0
# Дольше этого — не выход на день, и окно выезда на таком числе не считается
MAX_DAY_HOURS = 14.0


async def run(apply: bool) -> None:
    plan = json.loads(PLAN_FILE.read_text("utf-8"))
    created = existed = 0

    async with SessionLocal() as session:
        regions = {
            r.name: r for r in (await session.execute(select(Region))).scalars()
        }
        # Слаг уникален в схеме: два места с одинаковым именем уронили бы
        # весь прогон на вставке. Такое уже было — у источника две записи
        # одного водопада, отличающиеся регистром буквы
        seen: set[str] = set()
        for spec in plan:
            if spec["slug"] in seen:
                print(f"  ! {spec['name']}: слаг {spec['slug']} уже занят, пропущено")
                continue
            seen.add(spec["slug"])

            place = (
                await session.execute(select(Place).where(Place.slug == spec["slug"]))
            ).scalar_one_or_none()
            if place is not None:
                existed += 1
                continue

            created += 1
            day = [t for t in spec["tracks"] if t["day"]]
            longest = max(day, key=lambda t: t["km"]) if day else None
            hours = _hours(longest) if longest else None
            print(
                f"  + {spec['name'][:24]:24} {spec['category']:9} {spec['region'][:14]:14} "
                f"{spec['elevation_m'] or '—':>5} м  треков: {len(spec['tracks'])}"
                + (f"  {longest['km']} км" if longest else "  нет дневного")
            )
            if not apply:
                continue

            region = regions.get(spec["region"])
            place = Place(
                slug=spec["slug"],
                name=spec["name"],
                category=PlaceCategory(spec["category"]),
                difficulty=Difficulty(spec["difficulty"]),
                region=region,
                lat=spec["lat"],
                lng=spec["lng"],
                elevation_m=spec["elevation_m"],
                distance_km=(longest["km"] if longest else None),
                duration_hours=hours,
                elevation_gain_m=(longest["ascent"] if longest else None),
                # Черновик: в приложении не покажется, пока человек не напишет
                # название с описанием и не опубликует из админки
                is_published=False,
            )
            session.add(place)
            await session.flush()

            for order, track in enumerate(spec["tracks"]):
                src = GPX_SRC / track["file"]
                if not src.exists():
                    continue
                (GPX_DIR / track["file"]).write_bytes(src.read_bytes())
                session.add(
                    PlaceTrack(
                        place_id=place.id,
                        gpx_file=StorageFile(name=track["file"], storage=gpx_storage),
                        name=track["name"],
                        gpx_credit=track["credit"],
                        distance_km=track["km"],
                        ascent_m=track["ascent"],
                        sort_order=order,
                    )
                )
            await _photos(session, place, spec["point_id"])

        if apply:
            await session.commit()
            print(f"\nГотово: заведено {created}, уже было {existed}.")
        else:
            print(f"\nПоказ без изменений: заведётся {created}, уже есть {existed}.")


def _hours(track: dict) -> float | None:
    """Ходовое время из вилки источника серединой.

    Вилки местами дикие — 8–31 час, — и середина тогда обещает ночёвку.
    Такие берём по нижнему краю, а совсем неправдоподобные пропускаем:
    пусть человек проставит сам.
    """
    low, high = track.get("timeFrom"), track.get("timeTo")
    if not (low and high):
        return None
    hours = round((low + high) / 2 / 60, 1)
    if hours > MAX_DAY_HOURS:
        hours = round(low / 60, 1)
    return hours if hours <= MAX_DAY_HOURS else None


async def _photos(session, place: Place, point_id: str) -> None:
    medias = [
        m
        for m in (ts.point(point_id).get("medias") or [])
        if m.get("status") == "COMPLETED"
    ][:MAX_PHOTOS]
    for order, media in enumerate(medias):
        # large, а не оригинал: 1200 px при 300 КБ против оригиналов
        # местами в 8448 px и 41 МБ
        url = media.get("large") or media.get("originalUrl")
        if not url:
            continue
        name = f"{place.slug}-ts-{url.rsplit('/', 1)[-1]}"
        try:
            (PHOTOS_DIR / name).write_bytes(ts.fetch_file(url))
            make_thumbnail(name)
        except Exception:  # noqa: BLE001 — место важнее одного снимка
            continue
        source = (media.get("source") or {}).get("name", "").strip()
        session.add(
            PlacePhoto(
                place_id=place.id,
                file=StorageFile(name=name, storage=photo_storage),
                # Автором, без адреса источника: приписывать снимок площадке,
                # на которой он просто лежал, неверно. Автора нет — подписи нет
                credit=(f"Фото: {source}" if source else ""),
                sort_order=order,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    asyncio.run(run(parser.parse_args().apply))


if __name__ == "__main__":
    main()
