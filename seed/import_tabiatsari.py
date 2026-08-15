"""Импорт треков и фотографий из tabiatsari.uz в существующие места.

    uv run python -m seed.import_tabiatsari            # показать, что будет
    uv run python -m seed.import_tabiatsari --apply    # выполнить

Слияние по полям, а не перезапись: координаты и высоты источник знает лучше
(его точки — GPS людей на месте, наши — из Wikidata, расхождение медианой
296 м), а названия, тексты, сложность и сезон есть только у нас. Свои фото
и тексты скрипт не трогает никогда — только дополняет.

Что с чем сопоставлено — в data/tabiatsari_map.json. Файл заполняется
человеком: сопоставление по расстоянию ошибается (Урунгач цеплялся к
кишлаку-старту вместо озера), и решать должен глаз, а не порог.
"""

import argparse
import asyncio
import json
import re
from pathlib import Path

from sqlalchemy import select

try:
    from fastapi_storages import StorageFile
except ImportError:  # расположение менялось между версиями пакета
    from fastapi_storages.base import StorageFile

from app.config import GPX_DIR, PHOTOS_DIR
from app.db import SessionLocal
from app.models import Place, PlacePhoto, PlaceTrack, gpx_storage, photo_storage
from app.services.gpx import clean, track_stats
from app.services.images import make_thumbnail

from . import tabiatsari as ts

DATA_DIR = Path(__file__).resolve().parent / "data"
MAP_FILE = DATA_DIR / "tabiatsari_map.json"

CREDIT_SUFFIX = "tabiatsari.uz"


def _declared_elevation(name: str, fallback: int | None) -> int | None:
    """Высота из названия, а не из поля.

    У Манкента в имени 3018, в поле 2946; так же расходятся Коракуш,
    Бабайтаг, Деволи Сурх, Амир Темур. Поле, похоже, снимается с рельефа,
    а имя несёт заявленную высоту вершины — по полю Манкент выпал бы
    из трёхтысячников.
    """
    match = re.search(r"(\d{3,4})\s*m", name)
    return int(match.group(1)) if match else fallback


def _credit(track: dict) -> str:
    """Подпись автора. Автор трека и автор снимков — разные люди:
    у трека «Go on Foot», у фотографий того же места другой человек."""
    source = track.get("source") or {}
    name = (source.get("name") or "").strip()
    return f"{name} · {CREDIT_SUFFIX}" if name else CREDIT_SUFFIX


def _photo_credit(media: dict) -> str:
    source = media.get("source") or {}
    name = (source.get("name") or "").strip()
    return f"Фото: {name}, {CREDIT_SUFFIX}" if name else f"Фото: {CREDIT_SUFFIX}"


async def run(apply: bool) -> None:
    mapping = json.loads(MAP_FILE.read_text("utf-8"))["matches"]
    mapping.pop("_", None)
    points = {p["id"]: p for p in ts.points()}

    async with SessionLocal() as session:
        for slug, point_id in mapping.items():
            place = (
                await session.execute(select(Place).where(Place.slug == slug))
            ).scalar_one_or_none()
            if place is None:
                print(f"  ! {slug}: места нет в базе, пропущено")
                continue
            point = points.get(point_id)
            if point is None:
                print(f"  ! {slug}: точки {point_id} нет в выдаче источника")
                continue

            print(f"\n{place.name}  ←  {point['name']}")
            _plan_place(place, point, apply)
            await _plan_tracks(session, place, point_id, apply)
            await _plan_photos(session, place, point_id, apply)

        if apply:
            await session.commit()
            print("\nГотово.")
        else:
            print("\nЭто был показ без изменений. Выполнить: --apply")


def _plan_place(place: Place, point: dict, apply: bool) -> None:
    lat, lng = round(point["lat"], 6), round(point["lon"], 6)
    if (place.lat, place.lng) != (lat, lng):
        print(f"  координаты {place.lat},{place.lng} → {lat},{lng}")
        if apply:
            place.lat, place.lng = lat, lng

    elevation = _declared_elevation(point["name"], point.get("elevation"))
    # Только если у нас пусто: свою высоту не переписываем, она выверена
    if place.elevation_m is None and elevation:
        print(f"  высота — → {elevation} м")
        if apply:
            place.elevation_m = elevation


async def _plan_tracks(session, place: Place, point_id: str, apply: bool) -> None:
    existing = {
        Path(t.gpx_file.name).name: t
        for t in (
            await session.execute(select(PlaceTrack).where(PlaceTrack.place_id == place.id))
        ).scalars()
    }
    for order, track in enumerate(ts.tracks(point_id), start=len(existing)):
        url = track.get("gpxFileUrl")
        if not url:
            continue
        name = f"{place.slug}-ts-{url.rsplit('/', 1)[-1]}"
        if name in existing:
            print(f"  трек «{track['name'][:40]}» уже есть")
            continue

        data = clean(ts.fetch_file(url))
        stats = track_stats(data)
        print(
            f"  + трек «{track['name'][:40]}» {stats.distance_km} км, "
            f"+{stats.ascent_m} м, {len(data) // 1024} КБ, «{_credit(track)}»"
        )
        if not apply:
            continue
        (GPX_DIR / name).write_bytes(data)
        session.add(
            PlaceTrack(
                place_id=place.id,
                gpx_file=StorageFile(name=name, storage=gpx_storage),
                name=track["name"],
                gpx_credit=_credit(track),
                distance_km=stats.distance_km,
                ascent_m=stats.ascent_m,
                sort_order=order,
            )
        )


async def _plan_photos(session, place: Place, point_id: str, apply: bool) -> None:
    existing = {
        Path(p.file.name).name
        for p in (
            await session.execute(select(PlacePhoto).where(PlacePhoto.place_id == place.id))
        ).scalars()
    }
    medias = ts.point(point_id).get("medias") or []
    added = 0
    for order, media in enumerate(medias, start=len(existing)):
        # Берём large, а не оригинал: 1200 px при 300 КБ против оригиналов
        # местами в 8448 px и 41 МБ. По всем 111 снимкам это 38 МБ вместо
        # 1,4 ГБ — на машине, где живут ещё шестнадцать чужих сайтов,
        # разница решающая, а для карточки и полного экрана телефона
        # 1200 px хватает с запасом
        url = media.get("large") or media.get("originalUrl")
        if not url or media.get("status") != "COMPLETED":
            continue
        name = f"{place.slug}-ts-{url.rsplit('/', 1)[-1]}"
        if name in existing:
            continue
        added += 1
        if not apply:
            continue
        (PHOTOS_DIR / name).write_bytes(ts.fetch_file(url))
        make_thumbnail(name)
        session.add(
            PlacePhoto(
                place_id=place.id,
                file=StorageFile(name=name, storage=photo_storage),
                credit=_photo_credit(media),
                sort_order=order,
            )
        )
    if added:
        print(f"  + фотографий: {added} (свои остаются)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # Показ по умолчанию: скрипт правит боевой каталог, и случайный запуск
    # не должен менять ничего молча
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    asyncio.run(run(parser.parse_args().apply))


if __name__ == "__main__":
    main()
