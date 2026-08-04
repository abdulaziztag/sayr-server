"""Идемпотентный сид каталога: `uv run python -m seed.seed`.

Данные — seed/data/places.json. Фото: если в seed/data/photos/<slug>/ лежат файлы —
берём их, иначе генерируем заглушки. GPX: seed/data/gpx/<slug>.gpx, если есть.
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
from app.models import Place, PlacePhoto, Region, gpx_storage, photo_storage
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
    "best_seasons",
    "kid_friendly",
    "short_desc",
    "description_md",
    "how_to_get_md",
    "gpx_credit",
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
                if field in item:
                    setattr(place, field, item[field])

            gpx_src = DATA_DIR / "gpx" / f"{slug}.gpx"
            if gpx_src.exists():
                shutil.copy(gpx_src, GPX_DIR / gpx_src.name)
                place.gpx_file = StorageFile(name=gpx_src.name, storage=gpx_storage)

            await session.flush()
            await _ensure_photos(session, place, item)

        await session.commit()
    print(f"Seed готов: {created} мест создано, {updated} обновлено.")


async def _ensure_photos(session, place: Place, item: dict) -> None:
    existing = (
        (await session.execute(select(PlacePhoto).where(PlacePhoto.place_id == place.id)))
        .scalars()
        .all()
    )
    if existing:
        return

    local_dir = DATA_DIR / "photos" / place.slug
    local_files = sorted(local_dir.glob("*.[jJpP]*")) if local_dir.exists() else []

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
                    credit=item.get("photo_credit", ""),
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
