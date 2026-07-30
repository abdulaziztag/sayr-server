from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from ..db import get_session
from ..models import Difficulty, Place, PlaceCategory, Season
from ..schemas import PlaceDetail, PlaceListItem, WeatherOut, place_detail, place_list_item
from ..services import weather

router = APIRouter(prefix="/api/v1", tags=["places"])


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
    limit: int = Query(100, le=200),
    offset: int = Query(0, ge=0),
):
    stmt = (
        select(Place)
        .options(selectinload(Place.photos), joinedload(Place.region))
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
        stmt = stmt.where(or_(Place.name.ilike(pattern), Place.short_desc.ilike(pattern)))

    if near:
        try:
            lat_s, lng_s = near.split(",")
            lat, lng = float(lat_s), float(lng_s)
        except ValueError:
            raise HTTPException(422, "near должен быть в формате 'lat,lng'")
        distance_km = _distance_km_expr(lat, lng)
        stmt = stmt.where(distance_km <= radius_km).order_by(distance_km)
    else:
        stmt = stmt.order_by(Place.name)

    places = (await session.execute(stmt)).unique().scalars().all()
    return [place_list_item(p) for p in places]


async def _get_place_or_404(slug: str, session: AsyncSession) -> Place:
    stmt = (
        select(Place)
        .options(selectinload(Place.photos), joinedload(Place.region))
        .where(Place.slug == slug, Place.is_published)
    )
    place = (await session.execute(stmt)).unique().scalar_one_or_none()
    if place is None:
        raise HTTPException(404, "Место не найдено")
    return place


@router.get("/places/{slug}", response_model=PlaceDetail)
async def get_place(slug: str, session: AsyncSession = Depends(get_session)):
    return place_detail(await _get_place_or_404(slug, session))


@router.get("/places/{slug}/weather", response_model=WeatherOut)
async def get_place_weather(slug: str, session: AsyncSession = Depends(get_session)):
    place = await _get_place_or_404(slug, session)
    try:
        return await weather.get_forecast(place.slug, place.lat, place.lng, place.elevation_m)
    except Exception:
        raise HTTPException(502, "Сервис погоды недоступен")
