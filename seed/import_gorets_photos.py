"""Заливает в каталог фотографии из выгрузки форума «ГОРЕЦ».

    uv run python -m seed.import_gorets_photos --photos /путь/к/.gorets-photos
    ... --apply                                                   # записать

План — seed/data/gorets_photos.json: место → снимки с готовой подписью автора.
Собран поиском ПО ТЕКСТАМ сообщений (не по именам файлов): найдено упоминание
места — взяты фото этого сообщения и соседних от того же отправителя, потому
что телеграм режет альбомы на склеенные сообщения.

Отсев уже сделан при сборке плана: превью мельче 40 КБ и кадры, попавшие
двум местам сразу (в сообщении упомянуты оба — чей снимок на самом деле,
по файлу не понять). Разбирать кадры по содержимому никто не будет:
окончательный отбор и порядок — за человеком в админке.

Заливаем только местам БЕЗ фотографий: где снимки уже есть, они выверены,
и подмешивать к ним форумные незачем.
"""

import argparse
import asyncio
import json
import shutil
from pathlib import Path

from sqlalchemy import select

try:
    from fastapi_storages import StorageFile
except ImportError:  # расположение менялось между версиями пакета
    from fastapi_storages.base import StorageFile

from app.config import PHOTOS_DIR
from app.db import SessionLocal
from app.models import Place, PlacePhoto, photo_storage
from app.services.images import make_thumbnail

DATA_DIR = Path(__file__).resolve().parent / "data"
PLAN_FILE = DATA_DIR / "gorets_photos.json"


async def run(apply: bool, photos_dir: Path) -> None:
    plan = json.loads(PLAN_FILE.read_text("utf-8"))
    added = skipped = 0

    async with SessionLocal() as session:
        for slug, spec in sorted(plan.items()):
            place = (
                await session.execute(select(Place).where(Place.slug == slug))
            ).scalar_one_or_none()
            if place is None:
                print(f"  ! {slug}: места нет в базе")
                continue

            existing = (
                (
                    await session.execute(
                        select(PlacePhoto).where(PlacePhoto.place_id == place.id)
                    )
                )
                .scalars()
                .all()
            )
            have = {Path(p.file.name).name for p in existing if p.file}
            # У места уже есть выверенные снимки — форумные к ним не подмешиваем.
            # Но свои же, залитые прошлым прогоном, узнаём по имени и не двоим
            if existing and not (have & {p["name"] for p in spec["photos"]}):
                print(f"  ~ {spec['name']}: уже {len(existing)} фото, не трогаем")
                skipped += len(spec["photos"])
                continue

            fresh = [p for p in spec["photos"] if p["name"] not in have]
            if not fresh:
                print(f"  ~ {spec['name']}: все {len(spec['photos'])} уже залиты")
                skipped += len(spec["photos"])
                continue

            print(f"  + {spec['name'][:32]:32} {len(fresh)} фото — {fresh[0]['credit']}")
            for order, photo in enumerate(fresh, start=len(existing)):
                src = photos_dir / photo["path"]
                if not src.exists():
                    print(f"    ! нет файла: {photo['path']}")
                    skipped += 1
                    continue
                added += 1
                if not apply:
                    continue
                shutil.copyfile(src, PHOTOS_DIR / photo["name"])
                try:
                    make_thumbnail(photo["name"])
                except Exception:  # noqa: BLE001 — место важнее одной миниатюры
                    print(f"    ! миниатюра не сделалась: {photo['name']}")
                session.add(
                    PlacePhoto(
                        place_id=place.id,
                        file=StorageFile(name=photo["name"], storage=photo_storage),
                        credit=photo["credit"],
                        sort_order=order,
                    )
                )

        if apply:
            await session.commit()
            print(f"\nГотово: залито {added}, пропущено {skipped}.")
        else:
            print(f"\nПоказ без изменений: зальётся {added}, пропущено {skipped}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    parser.add_argument("--photos", type=Path, required=True, help="папка с фото")
    args = parser.parse_args()
    asyncio.run(run(args.apply, args.photos))


if __name__ == "__main__":
    main()
