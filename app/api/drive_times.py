"""Матрица «город выезда × место» целиком — клиент выбирает город сам.

Один JSON около 25 КБ: города справочника с падежными формами на языке
запроса и минуты с километрами по опубликованным местам. Целиком, а не по
одному городу, чтобы смена города работала офлайн и чтобы на сервер не
уходило даже название города — координата и выбор остаются на телефоне.
"""

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..cities import CITIES, HUBS
from ..db import get_session
from ..models import CityDriveTime, Place, PlaceDriveTime
from ..schemas import DEFAULT_LANG, Lang, pick

router = APIRouter(prefix="/api/v1", tags=["drive-times"])


@router.get("/drive-times")
async def drive_times(
    response: Response,
    session: AsyncSession = Depends(get_session),
    lang: Lang = Query(DEFAULT_LANG, description="язык названий; без него — русский"),
) -> dict:
    rows = (
        await session.execute(
            select(Place.slug, PlaceDriveTime.city, PlaceDriveTime.minutes, PlaceDriveTime.km)
            .join(PlaceDriveTime, PlaceDriveTime.place_id == Place.id)
            .where(Place.is_published)
        )
    ).all()
    matrix: dict[str, dict[str, list]] = {}
    for slug, city, minutes, km in rows:
        matrix.setdefault(slug, {})[city] = [minutes, round(km, 1)]
    # Дорога до областного хаба: ею нить показывает «накануне доехать до
    # Ташкента», когда день из своего города не сходится
    city_matrix: dict[str, dict[str, list]] = {}
    for row in (await session.execute(select(CityDriveTime))).scalars().all():
        city_matrix.setdefault(row.origin, {})[row.hub] = [row.minutes, round(row.km, 1)]
    # Сутки кэша: матрица пересчитывается раз в месяц
    response.headers["Cache-Control"] = "public, max-age=86400"
    return {
        "cities": [
            {
                "code": c.code,
                "name": pick(c.name_ru, c.name_uz, lang),
                "from": pick(c.from_ru, c.from_uz, lang),
                "lat": c.lat,
                "lng": c.lng,
                "area": pick(c.area_ru, c.area_uz, lang),
            }
            for c in CITIES
        ],
        "hubs": list(HUBS),
        "matrix": matrix,
        "city_matrix": city_matrix,
    }
