from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Place, TripIntent

router = APIRouter(prefix="/api/v1", tags=["intents"])


class DayCount(BaseModel):
    date: date
    count: int
    #: Голосовало ли это устройство
    mine: bool = False


class IntentsOut(BaseModel):
    days: list[DayCount]


class IntentIn(BaseModel):
    date: date
    device_id: str = Field(min_length=8, max_length=64)


async def _place_id(slug: str, session: AsyncSession) -> int:
    stmt = select(Place.id).where(Place.slug == slug, Place.is_published)
    place_id = (await session.execute(stmt)).scalar_one_or_none()
    if place_id is None:
        raise HTTPException(404, "Место не найдено")
    return place_id


@router.get("/places/{slug}/intents", response_model=IntentsOut)
async def list_intents(
    slug: str,
    device_id: str | None = None,
    days: int = Query(60, ge=1, le=180),
    session: AsyncSession = Depends(get_session),
):
    """Сколько человек собирается в место по дням — числа под датами календаря."""
    place_id = await _place_id(slug, session)
    today = date.today()
    horizon = today + timedelta(days=days)

    counts = (
        await session.execute(
            select(TripIntent.day, func.count())
            .where(
                TripIntent.place_id == place_id,
                TripIntent.day >= today,
                TripIntent.day <= horizon,
            )
            .group_by(TripIntent.day)
            .order_by(TripIntent.day)
        )
    ).all()

    mine: set[date] = set()
    if device_id:
        mine = {
            row[0]
            for row in (
                await session.execute(
                    select(TripIntent.day).where(
                        TripIntent.place_id == place_id,
                        TripIntent.device_id == device_id,
                        TripIntent.day >= today,
                    )
                )
            ).all()
        }

    return IntentsOut(
        days=[DayCount(date=day, count=count, mine=day in mine) for day, count in counts]
    )


@router.post("/places/{slug}/intents", response_model=IntentsOut)
async def add_intent(
    slug: str, body: IntentIn, session: AsyncSession = Depends(get_session)
):
    """Отметиться на дату. Одно устройство — один голос на день (повтор не удваивает)."""
    place_id = await _place_id(slug, session)
    if body.date < date.today():
        raise HTTPException(422, "Дата в прошлом")

    # Устройство идёт куда-то одно в день: старая отметка на эту же дату снимается
    await session.execute(
        delete(TripIntent).where(
            TripIntent.device_id == body.device_id, TripIntent.day == body.date
        )
    )
    await session.execute(
        insert(TripIntent)
        .values(place_id=place_id, day=body.date, device_id=body.device_id)
        .on_conflict_do_nothing(constraint="uq_intent_place_day_device")
    )
    await session.commit()
    return await list_intents(slug, body.device_id, 60, session)


@router.delete("/places/{slug}/intents", response_model=IntentsOut)
async def remove_intent(
    slug: str,
    date_: date = Query(alias="date"),
    device_id: str = Query(min_length=8, max_length=64),
    session: AsyncSession = Depends(get_session),
):
    place_id = await _place_id(slug, session)
    await session.execute(
        delete(TripIntent).where(
            TripIntent.place_id == place_id,
            TripIntent.day == date_,
            TripIntent.device_id == device_id,
        )
    )
    await session.commit()
    return await list_intents(slug, device_id, 60, session)
