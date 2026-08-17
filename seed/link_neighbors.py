"""Пересчитать связи «рядом» по всем трекам каталога.

    uv run python -m seed.link_neighbors            # показать, что найдётся
    uv run python -m seed.link_neighbors --apply    # записать

При загрузке трека через админку связи пересчитываются сами. Скрипт нужен
для разового прогона по накопленному — после массового импорта, — и после
правки координат места: они меняют геометрию, но ни один трек при этом
не сохраняется, и сама собой связь не обновится.

Черновики участвуют наравне с опубликованными: место публикуют позже,
и связи должны быть готовы к этому моменту. Прятать неопубликованных
соседей — забота API.
"""

import argparse
import asyncio

from sqlalchemy import delete, select
from sqlalchemy.orm import joinedload

from app.db import SessionLocal
from app.models import PlaceNeighbor, PlaceTrack
from app.services.nearby import RADIUS_M, rebuild_for_track


async def run(apply: bool) -> None:
    async with SessionLocal() as session:
        tracks = (
            await session.execute(
                select(PlaceTrack)
                .options(joinedload(PlaceTrack.place))
                .order_by(PlaceTrack.id)
            )
        ).unique().scalars().all()

        # Полный пересчёт: у трека, чей файл больше не читается, старые связи
        # иначе остались бы навсегда — rebuild_for_track про него не узнает
        await session.execute(delete(PlaceNeighbor))

        pairs = 0
        for track in tracks:
            found = await rebuild_for_track(session, track)
            if not found:
                continue
            pairs += found
            print(f"  {track.place.name[:26]:26} «{track.name[:34]}» → соседей: {found}")

        if apply:
            await session.commit()
            print(f"\nГотово: {pairs} связей по {len(tracks)} трекам, порог {RADIUS_M} м.")
        else:
            await session.rollback()
            print(
                f"\nПоказ без изменений: нашлось бы {pairs} связей "
                f"по {len(tracks)} трекам. Выполнить: --apply"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    asyncio.run(run(parser.parse_args().apply))


if __name__ == "__main__":
    main()
