"""Идемпотентный сид каталога: `uv run python -m seed.seed`.

Данные — seed/data/places.json. Фото: если в seed/data/photos/<slug>/ лежат файлы —
берём их, иначе генерируем заглушки. Треки: список tracks у места
в places.json, файлы — в seed/data/gpx/.
Повторный запуск обновляет поля мест и не дублирует фото.
"""

import asyncio
import json
import shutil
from pathlib import Path

from sqlalchemy import select

try:
    from fastapi_storages import StorageFile
except ImportError:  # расположение менялось между версиями пакета
    from fastapi_storages.base import StorageFile

from app.config import GPX_DIR, PHOTOS_DIR
from app.db import SessionLocal
from app.models import Place, PlacePhoto, PlaceTrack, Region, gpx_storage, photo_storage
from app.services.gpx import track_stats
from app.services.images import generate_placeholder, make_thumbnail

DATA_DIR = Path(__file__).resolve().parent / "data"
PLACE_FIELDS = (
    "name",
    "category",
    "lat",
    "lng",
    "elevation_m",
    "difficulty",
    "distance_km",
    "duration_hours",
    "elevation_gain_m",
    "drive_minutes",
    "drive_km",
    "season_from",
    "season_to",
    "overnight",
    "best_seasons",
    "kid_friendly",
    "short_desc",
    "description_md",
    "how_to_get_md",
    "is_published",
)


async def seed() -> None:
    payload = json.loads((DATA_DIR / "places.json").read_text())

    async with SessionLocal() as session:
        regions: dict[str, Region] = {
            r.name: r for r in (await session.execute(select(Region))).scalars()
        }
        for i, name in enumerate(payload["regions"]):
            if name not in regions:
                regions[name] = Region(name=name, sort_order=i)
                session.add(regions[name])
            else:
                regions[name].sort_order = i
        await session.flush()

        created = updated = 0
        for item in payload["places"]:
            slug = item["slug"]
            stmt = select(Place).where(Place.slug == slug)
            place = (await session.execute(stmt)).scalar_one_or_none()
            if place is None:
                place = Place(slug=slug, region=regions[item["region"]])
                session.add(place)
                created += 1
            else:
                place.region = regions[item["region"]]
                updated += 1

            for field in PLACE_FIELDS:
                if field not in item:
                    continue
                # Пустое значение из файла НЕ затирает заполненное в базе:
                # длину, время и набор проставляют импорты из tabiatsari
                # и «Горца», а в places.json у большинства мест там null.
                # Один прогон сида ради правки соседнего поля сносил бы
                # всё импортированное разом — так уже случилось с Пулатханом
                if item[field] is None and getattr(place, field, None) is not None:
                    continue
                setattr(place, field, item[field])

            await session.flush()
            await _ensure_tracks(session, place, item)
            await _ensure_photos(session, place, item)

        await session.commit()
    print(f"Seed готов: {created} мест создано, {updated} обновлено.")


async def _ensure_tracks(session, place: Place, item: dict) -> None:
    """Треки места из places.json: tracks: [{file, name, credit}].

    Идемпотентно по имени файла: повторный сид обновляет имя, подпись,
    порядок и статистику, а не плодит дубликаты.
    """
    existing = {
        Path(t.gpx_file.name).name: t
        for t in (
            await session.execute(
                select(PlaceTrack).where(PlaceTrack.place_id == place.id)
            )
        ).scalars()
    }
    for order, spec in enumerate(item.get("tracks", [])):
        src = DATA_DIR / "gpx" / spec["file"]
        if not src.exists():
            print(f"  ! {place.slug}: нет файла {spec['file']}, трек пропущен")
            continue
        shutil.copy(src, GPX_DIR / src.name)
        stats = track_stats(src.read_bytes())
        track = existing.get(src.name)
        if track is None:
            track = PlaceTrack(
                place_id=place.id,
                gpx_file=StorageFile(name=src.name, storage=gpx_storage),
            )
            session.add(track)
        track.name = spec["name"]
        track.gpx_credit = spec.get("credit")
        track.distance_km = stats.distance_km
        track.ascent_m = stats.ascent_m
        track.start_lat = stats.start_lat
        track.start_lng = stats.start_lng
        track.sort_order = order


async def _ensure_photos(session, place: Place, item: dict) -> None:
    existing = (
        (await session.execute(select(PlacePhoto).where(PlacePhoto.place_id == place.id)))
        .scalars()
        .all()
    )
    if existing:
        return

    local_dir = DATA_DIR / "photos" / place.slug
    # Явный список расширений: маска "*.[jJpP]*" ловила и credits.json,
    # и он уезжал в галерею как «фотография»
    local_files = (
        sorted(
            f for f in local_dir.iterdir()
            if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        )
        if local_dir.exists()
        else []
    )

    # У каждого снимка свой автор: фото с Викисклада лежат под CC BY-SA,
    # где автор указывается поимённо. Общая подпись на всё место называла бы
    # чужим именем чужую работу, поэтому подписи хранятся в credits.json
    # рядом с файлами, а photo_credit из JSON остаётся запасным вариантом.
    credits_file = local_dir / "credits.json" if local_dir.exists() else None
    per_file = (
        json.loads(credits_file.read_text())
        if credits_file and credits_file.exists()
        else {}
    )

    if local_files:
        for i, src in enumerate(local_files):
            fname = f"{place.slug}-{i + 1}{src.suffix.lower()}"
            shutil.copy(src, PHOTOS_DIR / fname)
            make_thumbnail(fname)
            session.add(
                PlacePhoto(
                    place_id=place.id,
                    file=StorageFile(name=fname, storage=photo_storage),
                    sort_order=i,
                    credit=per_file.get(src.name, item.get("photo_credit", "")),
                )
            )
    else:
        for i in range(2):
            fname = f"{place.slug}-{i + 1}.jpg"
            generate_placeholder(fname, place.name, item["category"])
            session.add(
                PlacePhoto(
                    place_id=place.id,
                    file=StorageFile(name=fname, storage=photo_storage),
                    sort_order=i,
                    credit="Заглушка — заменить реальным фото",
                )
            )


if __name__ == "__main__":
    asyncio.run(seed())
