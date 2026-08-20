"""Расчёт связей «рядом» по геометрии треков.

Место считается соседним, если трек проходит мимо него ближе порога.
Порог щедрый нарочно: координаты точек взяты из OSM и Wikidata и сами
по себе гуляют на сотню метров, а тропа к водопаду идёт по руслу и к самой
отметке не подходит вплотную. Промахнуться в эту сторону дёшево — человек
увидит на карточке лишнюю ссылку; промахнуться в другую значит не показать
ему, что за один выход он захватит два места.
"""

from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import GPX_DIR
from ..models import Place, PlaceNeighbor, PlaceTrack
from .gpx import bbox, distance_to_track_m, track_coords

# 500, а не 300: на трёхстах отсекались связи, которые человек видит своими
# глазами. Классический подъём на Большой Чимган от Аксая проходит в 462 метрах
# от Чёрного водопада — мимо него идут, но на карточке этого не было. Замер
# по всему каталогу: 300 м дают 41 пару мест, 500 — 46, и все пять новых
# настоящие (Пальтау с гротом Оби-Рахмат, Бабайтаг с Шахтёром, Пулатхан
# с тесниной Нурекаты). 600 добавляют ещё две — там уже начинается натяжка
RADIUS_M = 500

# Градус широты — 111 км; для долготы на 41° с. ш. градус короче, но здесь
# нужен грубый отсев кандидатов по прямоугольнику, и запас в свою пользу
# только на руку
_MARGIN_DEG = RADIUS_M / 111_000 * 2


async def rebuild_for_track(session: AsyncSession, track: PlaceTrack) -> int:
    """Пересчитать связи одного трека. Возвращает число найденных соседей.

    Старые связи этого трека удаляются целиком: после перезаливки файла
    маршрут может идти уже другой тропой, и прежние соседи не наследуются.
    """
    await session.execute(
        delete(PlaceNeighbor).where(PlaceNeighbor.track_id == track.id)
    )
    if not track.gpx_file:
        return 0

    # basename: хранилище может держать в колонке полный путь
    path = GPX_DIR / Path(str(track.gpx_file.name)).name
    try:
        coords = track_coords(path.read_bytes())
    except Exception:  # noqa: BLE001 — битый файл не повод ронять сохранение
        return 0
    if len(coords) < 2:
        return 0

    south, west, north, east = bbox(coords)
    candidates = (
        await session.execute(
            select(Place).where(
                Place.id != track.place_id,
                Place.lat.between(south - _MARGIN_DEG, north + _MARGIN_DEG),
                Place.lng.between(west - _MARGIN_DEG, east + _MARGIN_DEG),
            )
        )
    ).scalars().all()

    found = 0
    for other in candidates:
        gap = distance_to_track_m(coords, other.lat, other.lng)
        if gap > RADIUS_M:
            continue
        found += 1
        # Обе стороны: на карточке соседа связь так же осмысленна
        session.add(
            PlaceNeighbor(
                place_id=track.place_id,
                neighbor_id=other.id,
                track_id=track.id,
                distance_m=round(gap),
            )
        )
        session.add(
            PlaceNeighbor(
                place_id=other.id,
                neighbor_id=track.place_id,
                track_id=track.id,
                distance_m=round(gap),
            )
        )
    return found
