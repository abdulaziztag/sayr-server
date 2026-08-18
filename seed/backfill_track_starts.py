"""Проставить точку старта существующим трекам.

    uv run python -m seed.backfill_track_starts            # показать
    uv run python -m seed.backfill_track_starts --apply    # записать

У новых треков точка старта считается при сохранении — в админке и в сиде.
Скрипт нужен для тех, что легли в базу раньше, и после массового импорта:
часть треков заводилась не через track_stats, а по данным источника.

Старт — первая точка записи, то есть место, откуда пошли пешком. Именно
её человек вбивает в автонавигатор: координаты самого места это цель,
вершина или водопад, и машину туда не подадут.
"""

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.config import GPX_DIR
from app.db import SessionLocal
from app.models import PlaceTrack
from app.services.gpx import track_stats


async def run(apply: bool) -> None:
    async with SessionLocal() as session:
        tracks = (
            await session.execute(
                select(PlaceTrack).options(joinedload(PlaceTrack.place)).order_by(PlaceTrack.id)
            )
        ).unique().scalars().all()

        filled = missed = 0
        for track in tracks:
            if not track.gpx_file:
                continue
            path = GPX_DIR / Path(str(track.gpx_file.name)).name
            try:
                stats = track_stats(path.read_bytes())
            except Exception as exc:  # noqa: BLE001 — один битый файл не повод падать
                missed += 1
                print(f"  ! {track.name[:34]}: {type(exc).__name__}")
                continue
            if stats.start_lat is None:
                missed += 1
                print(f"  ! {track.name[:34]}: в файле нет точек")
                continue

            filled += 1
            print(
                f"  {track.place.name[:22]:22} «{track.name[:30]:30}» "
                f"→ {stats.start_lat}, {stats.start_lng}"
            )
            track.start_lat = stats.start_lat
            track.start_lng = stats.start_lng

        if apply:
            await session.commit()
            print(f"\nГотово: проставлено {filled}, без старта {missed}.")
        else:
            await session.rollback()
            print(
                f"\nПоказ без изменений: проставилось бы {filled}, "
                f"без старта {missed}. Выполнить: --apply"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    asyncio.run(run(parser.parse_args().apply))


if __name__ == "__main__":
    main()
