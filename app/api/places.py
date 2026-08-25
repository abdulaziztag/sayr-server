from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from ..db import get_session
from ..models import Difficulty, Place, PlaceCategory, PlaceNeighbor, Season
from ..schemas import (
    DEFAULT_LANG,
    Lang,
    NearbyOut,
    PlaceDetail,
    PlaceListItem,
    WeatherOut,
    nearby_out,
    place_detail,
    place_list_item,
)
from ..services import weather
from ..services.gpx import haversine_m

router = APIRouter(prefix="/api/v1", tags=["places"])

# Сколько соседей отдаём. В плотном кусте — Чимган, Бостанлык — длинный трек
# цепляет несколько точек сразу, и у популярного места список уходит за десяток.
# Клиенты рисуют его целиком и сразу, каждая строка тянет свою миниатюру;
# да и человеку внизу карточки нужен короткий список «что захвачу заодно»,
# а не второй каталог
NEARBY_LIMIT = 6


def _distance_km_expr(lat: float, lng: float):
    """Хаверсин на встроенных функциях Postgres — расширения не нужны.

    На каталоге в сотни мест точность и скорость эквивалентны PostGIS."""
    return 6371 * func.acos(
        func.least(
            1.0,
            func.cos(func.radians(lat)) * func.cos(func.radians(Place.lat))
            * func.cos(func.radians(Place.lng) - func.radians(lng))
            + func.sin(func.radians(lat)) * func.sin(func.radians(Place.lat)),
        )
    )


@router.get("/places", response_model=list[PlaceListItem])
async def list_places(
    session: AsyncSession = Depends(get_session),
    category: list[PlaceCategory] | None = Query(None),
    difficulty: list[Difficulty] | None = Query(None),
    region_id: int | None = None,
    season: Season | None = None,
    kid_friendly: bool | None = None,
    q: str | None = Query(None, max_length=100),
    near: str | None = Query(None, description="lat,lng — сортирует по удалённости"),
    radius_km: float = Query(150, gt=0, le=1000),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    lang: Lang = Query(DEFAULT_LANG, description="язык текстов; без него — русский"),
):
    stmt = (
        select(Place)
        .options(
            selectinload(Place.photos),
            selectinload(Place.tracks),
            joinedload(Place.region),
        )
        .where(Place.is_published)
        .limit(limit)
        .offset(offset)
    )
    if category:
        stmt = stmt.where(Place.category.in_(category))
    if difficulty:
        stmt = stmt.where(Place.difficulty.in_(difficulty))
    if region_id is not None:
        stmt = stmt.where(Place.region_id == region_id)
    if season is not None:
        stmt = stmt.where(Place.best_seasons.any(season.value))
    if kid_friendly is not None:
        stmt = stmt.where(Place.kid_friendly == kid_friendly)
    if q:
        pattern = f"%{q.strip()}%"
        # Ищем по обоим языкам независимо от lang: человек с узбекским
        # интерфейсом помнит место по русскому названию не реже, чем наоборот
        stmt = stmt.where(
            or_(
                Place.name.ilike(pattern),
                Place.name_uz.ilike(pattern),
                Place.short_desc.ilike(pattern),
                Place.short_desc_uz.ilike(pattern),
            )
        )

    if near:
        try:
            lat_s, lng_s = near.split(",")
            lat, lng = float(lat_s), float(lng_s)
        except ValueError:
            raise HTTPException(422, "near должен быть в формате 'lat,lng'")
        distance_km = _distance_km_expr(lat, lng)
        stmt = stmt.where(distance_km <= radius_km).order_by(distance_km)
    else:
        # Всегда по русскому имени, даже при lang=uz. База создана
        # с COLLATE C — сравнение побайтовое, и при неполном переводе
        # список развалился бы на два блока: сначала латиница, потом
        # кириллица. Порядок внутри каталога человек не запоминает,
        # а разрыв заметил бы сразу
        stmt = stmt.order_by(Place.name)

    places = (await session.execute(stmt)).unique().scalars().all()
    return [place_list_item(p, lang) for p in places]


async def _get_place_or_404(slug: str, session: AsyncSession) -> Place:
    stmt = (
        select(Place)
        .options(
            selectinload(Place.photos),
            selectinload(Place.tracks),
            joinedload(Place.region),
        )
        .where(Place.slug == slug, Place.is_published)
    )
    place = (await session.execute(stmt)).unique().scalar_one_or_none()
    if place is None:
        raise HTTPException(404, "Место не найдено")
    return place


async def _nearby(
    place: Place, session: AsyncSession, lang: Lang = DEFAULT_LANG
) -> list[NearbyOut]:
    """Соседи по треку, ближние первыми.

    Одну пару могут связывать несколько треков — берём по месту одну строку
    и кратчайший подход. Расстояние показываем между самими местами, а не
    подход трека: человек спрашивает «далеко ли отсюда до грота», и ответ
    про геометрию линии ему ничего не скажет.
    """
    stmt = (
        select(Place)
        .join(PlaceNeighbor, PlaceNeighbor.neighbor_id == Place.id)
        .options(selectinload(Place.photos))
        .where(PlaceNeighbor.place_id == place.id, Place.is_published)
        .group_by(Place.id)
    )
    others = (await session.execute(stmt)).unique().scalars().all()
    pairs = [
        (other, round(haversine_m(place.lat, place.lng, other.lat, other.lng)))
        for other in others
    ]
    pairs.sort(key=lambda pair: pair[1])
    return [nearby_out(other, gap, lang) for other, gap in pairs[:NEARBY_LIMIT]]


@router.get("/places/{slug}", response_model=PlaceDetail)
async def get_place(
    slug: str,
    session: AsyncSession = Depends(get_session),
    lang: Lang = Query(DEFAULT_LANG, description="язык текстов; без него — русский"),
):
    place = await _get_place_or_404(slug, session)
    return place_detail(place, await _nearby(place, session, lang), lang)


@router.get("/places/{slug}/weather", response_model=WeatherOut)
async def get_place_weather(slug: str, session: AsyncSession = Depends(get_session)):
    place = await _get_place_or_404(slug, session)
    try:
        return await weather.get_forecast(place.slug, place.lat, place.lng, place.elevation_m)
    except Exception:
        raise HTTPException(502, "Сервис погоды недоступен")
