"""Слить две карточки одного и того же места в одну.

    uv run python -m seed.merge_duplicate --from obi-kashka --into okhotnichiy \\
        --take elevation_m,distance_km,duration_hours,elevation_gain_m
    ... --apply                                                # записать

Каталог рос из нескольких источников, и одно место иногда заходило
дважды под разными именами. Первый такой случай — «Оби Кашка»
и «Пик Охотничий»: форум объясняет прямо, что Аукашка — русифицированное
Обиқашқа, а точки в базе стоят в 670 метрах друг от друга.

Донор **не удаляется**, а снимается с публикации. Причина простая:
если окажется, что это всё-таки две разные вершины, вернуть запись
будет нечем — фотографии и треки к тому моменту уже переехали.
Скрытая карточка стоит места в базе, потерянная — работы заново.

Переносится всё, что нельзя восстановить: фотографии, треки, связи
с соседями. А вот поля карточки перезаписываются **только по списку
--take**: слепое «заполнить из донора» затёрло бы выверенное руками
описание тем, что просто оказалось длиннее.
"""

import argparse
import asyncio

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.models import Place, PlaceNeighbor, PlacePhoto, PlaceTrack


async def run(source: str, target: str, take: list[str], apply: bool) -> None:
    async with SessionLocal() as session:
        places = {
            p.slug: p
            for p in (
                await session.execute(
                    select(Place)
                    .where(Place.slug.in_([source, target]))
                    .options(selectinload(Place.photos), selectinload(Place.tracks))
                )
            )
            .scalars()
            .all()
        }
        donor, keeper = places.get(source), places.get(target)
        if donor is None or keeper is None:
            raise SystemExit(f"нет места: {source if donor is None else target}")

        for field in take:
            if not hasattr(donor, field):
                raise SystemExit(f"нет поля {field}")
            was, now = getattr(keeper, field), getattr(donor, field)
            if was == now:
                continue
            print(f"  {field:20} {was} → {now}")
            if apply:
                setattr(keeper, field, now)

        # Фотографии встают в хвост: у цели свой порядок, и первая её
        # фотография — обложка, менять её слиянием мы не вправе
        shift = max((p.sort_order for p in keeper.photos), default=-1) + 1
        photos = (
            await session.execute(
                select(PlacePhoto)
                .where(PlacePhoto.place_id == donor.id)
                .order_by(PlacePhoto.sort_order)
            )
        ).scalars().all()
        print(f"  фотографий переезжает: {len(photos)} (встанут с {shift}-й)")
        for index, photo in enumerate(photos):
            if apply:
                photo.place_id = keeper.id
                photo.sort_order = shift + index

        track_shift = max((t.sort_order for t in keeper.tracks), default=-1) + 1
        tracks = (
            await session.execute(
                select(PlaceTrack)
                .where(PlaceTrack.place_id == donor.id)
                .order_by(PlaceTrack.sort_order)
            )
        ).scalars().all()
        print(f"  треков переезжает: {len(tracks)}")
        for index, track in enumerate(tracks):
            print(f"    {track.name} — {track.distance_km:g} км, набор {track.ascent_m}")
            if apply:
                track.place_id = keeper.id
                track.sort_order = track_shift + index

        # Связи: и свои, и встречные. Соседом самому себе место быть
        # не может, дубли по (place_id, neighbor_id) тоже не нужны
        links = (
            await session.execute(
                select(PlaceNeighbor).where(
                    (PlaceNeighbor.place_id == donor.id)
                    | (PlaceNeighbor.neighbor_id == donor.id)
                )
            )
        ).scalars().all()
        existing = {
            (n.place_id, n.neighbor_id)
            for n in (
                await session.execute(
                    select(PlaceNeighbor).where(
                        (PlaceNeighbor.place_id == keeper.id)
                        | (PlaceNeighbor.neighbor_id == keeper.id)
                    )
                )
            )
            .scalars()
            .all()
        }
        moved = dropped = 0
        for link in links:
            pair = (
                keeper.id if link.place_id == donor.id else link.place_id,
                keeper.id if link.neighbor_id == donor.id else link.neighbor_id,
            )
            if pair[0] == pair[1] or pair in existing:
                dropped += 1
                if apply:
                    await session.delete(link)
                continue
            existing.add(pair)
            moved += 1
            if apply:
                link.place_id, link.neighbor_id = pair
        print(f"  связей переезжает: {moved}, отброшено как дубли: {dropped}")

        print(f"  {source}: снимаю с публикации (запись остаётся в базе)")
        if apply:
            donor.is_published = False
            await session.commit()
            print("\nЗаписано.")
        else:
            print("\nПоказ без изменений. Выполнить: --apply")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="source", required=True, help="slug дубля")
    parser.add_argument("--into", dest="target", required=True, help="slug того, кто остаётся")
    parser.add_argument(
        "--take",
        default="",
        help="поля карточки, которые взять у дубля, через запятую",
    )
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    args = parser.parse_args()
    take = [f.strip() for f in args.take.split(",") if f.strip()]
    asyncio.run(run(args.source, args.target, take, args.apply))


if __name__ == "__main__":
    main()
