from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_ as sa_and
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.tokens import optional_user
from ..db import get_session
from ..models import Place, PlacePaceStats, TripIntent, User
from ..schemas import NO_NUL, SLUG
# «Сегодня» — по Ташкенту, как у комнат. У службы нет TZ, и date.today()
# шёл по часам сервера: с полуночи до пяти утра по Ташкенту вчерашний
# день ещё принимался как сегодняшний, а окно счётчиков съезжало на сутки
from .rooms import today as tashkent_today

router = APIRouter(prefix="/api/v1", tags=["intents"])

# 62, не 60: календарь клиента показывает два месяца целиком, а 31+31
# дней в 60 не помещаются — последние даты оставались без счётчиков
DEFAULT_DAYS = 62


class DayCount(BaseModel):
    date: date
    count: int
    #: Голосовало ли это устройство
    mine: bool = False


class IntentsOut(BaseModel):
    days: list[DayCount]


class IntentIn(BaseModel):
    date: date
    device_id: str = Field(min_length=8, max_length=64, pattern=NO_NUL)


#: Насколько прошедший выход разошёлся с расчётным временем
Pace = Literal["faster", "expected", "slower"]


class PaceIn(BaseModel):
    date: date
    device_id: str = Field(min_length=8, max_length=64, pattern=NO_NUL)
    #: Состоялся ли выход. False — человек не пошёл, темпа тогда нет
    went: bool
    pace: Pace | None = None


def _mine(user: User | None, device_id: str | None):
    """Чьи это отметки.

    У вошедшего — его собственные, с любого телефона. У гостя — отметки
    его устройства, но только те, что ещё никому не достались: после входа
    они принадлежат человеку, и вторым голосом с того же телефона быть
    не должны.
    """
    if user is not None:
        return TripIntent.user_id == user.id
    return sa_and(TripIntent.device_id == device_id, TripIntent.user_id.is_(None))


async def _place_id(slug: str, session: AsyncSession) -> int:
    if not SLUG.match(slug):
        raise HTTPException(404, "Место не найдено")
    stmt = select(Place.id).where(Place.slug == slug, Place.is_published)
    place_id = (await session.execute(stmt)).scalar_one_or_none()
    if place_id is None:
        raise HTTPException(404, "Место не найдено")
    return place_id


@router.get("/places/{slug}/intents", response_model=IntentsOut)
async def list_intents(
    slug: str,
    device_id: str | None = Query(None, pattern=NO_NUL),
    days: int = Query(DEFAULT_DAYS, ge=1, le=180),
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(optional_user),
):
    """Сколько человек собирается в место по дням — числа под датами календаря."""
    place_id = await _place_id(slug, session)
    today = tashkent_today()
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
    if user is not None or device_id:
        mine = {
            row[0]
            for row in (
                await session.execute(
                    select(TripIntent.day).where(
                        TripIntent.place_id == place_id,
                        _mine(user, device_id),
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
    slug: str,
    body: IntentIn,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(optional_user),
):
    """Отметиться на дату. Один голос на день (повтор не удваивает)."""
    place_id = await _place_id(slug, session)
    today = tashkent_today()
    if body.date < today:
        raise HTTPException(422, "Дата в прошлом")
    # Клиент даёт выбрать максимум два месяца вперёд; всё дальше — не человек
    if body.date > today + timedelta(days=180):
        raise HTTPException(422, "Дата слишком далеко")

    # В день идут куда-то одно: старая отметка на эту же дату снимается.
    # У вошедшего — на всех его телефонах сразу
    await _drop(session, _mine(user, body.device_id), TripIntent.day == body.date)
    await session.execute(
        insert(TripIntent)
        .values(
            place_id=place_id,
            day=body.date,
            device_id=body.device_id,
            user_id=user.id if user else None,
        )
        .on_conflict_do_nothing(constraint="uq_intent_place_day_device")
    )
    await session.commit()
    # Тот же горизонт, что у GET: с зашитыми здесь 60 ответ сразу после
    # отметки оказывался на два дня короче обычного — ровно та поломка,
    # ради которой дефолт и поднимали до 62
    return await list_intents(slug, body.device_id, DEFAULT_DAYS, session, user)


@router.delete("/places/{slug}/intents", response_model=IntentsOut)
async def remove_intent(
    slug: str,
    date_: date = Query(alias="date"),
    device_id: str = Query(min_length=8, max_length=64, pattern=NO_NUL),
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(optional_user),
):
    place_id = await _place_id(slug, session)
    await _drop(
        session,
        TripIntent.place_id == place_id,
        TripIntent.day == date_,
        _mine(user, device_id),
    )
    await session.commit()
    return await list_intents(slug, device_id, DEFAULT_DAYS, session, user)


@router.post("/places/{slug}/pace", response_model=IntentsOut)
async def set_pace(
    slug: str,
    body: PaceIn,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(optional_user),
):
    """Как прошёл выход: состоялся ли и разошёлся ли с расчётным временем.

    Приходит вечером дня выхода, когда приложение спрашивает «были?».
    Требует существующей отметки: поправку присылает тот, кто планировал,
    и это же не даёт накрутить счётчик с чистого листа.

    Повторный вызов **переносит** голос, а не добавляет второй: человек
    правит свой ответ тапом по записи в истории, и считаться дважды
    он не должен.

    День — сегодняшний или прошедший: «как сходили» про поход, который
    ещё впереди, — не ответ, а накрутка счётчика отметкой на любую дату.
    """
    place_id = await _place_id(slug, session)
    if body.date > tashkent_today():
        raise HTTPException(422, "День ещё не наступил")

    intent = (
        await session.execute(
            select(TripIntent).where(
                TripIntent.place_id == place_id,
                TripIntent.day == body.date,
                _mine(user, body.device_id),
            )
        )
    ).scalar_one_or_none()
    if intent is None:
        raise HTTPException(404, "Отметки на этот день нет")

    # Не пошёл — темпа быть не может, каким бы его ни прислали
    fresh = body.pace if body.went else None
    was = intent.pace

    intent.went = body.went
    intent.pace = fresh

    if was != fresh:
        await _move_vote(place_id, was, fresh, session)

    await session.commit()
    return await list_intents(slug, body.device_id, DEFAULT_DAYS, session, user)


async def _drop(session: AsyncSession, *where) -> None:
    """Снять отметки — и их голоса в счётчике темпа.

    Счётчик живёт отдельно от отметок и сам не узнает, что ответ исчез:
    отметка, снятая после вечернего вопроса, оставляла голос висеть,
    а «отметить → ответить → снять» по кругу накручивало его без единой
    отметки. Чистка по сроку (stats.purge) идёт мимо намеренно: там
    отметка уходит, а голос обязан остаться.
    """
    gone = await session.execute(
        delete(TripIntent).where(*where).returning(TripIntent.place_id, TripIntent.pace)
    )
    for place_id, pace in gone.all():
        if pace is not None:
            await _move_vote(place_id, pace, None, session)


async def _move_vote(
    place_id: int, was: str | None, now: str | None, session: AsyncSession
) -> None:
    """Снять голос со старого счётчика и добавить новому.

    Строка счётчика заводится при первом обращении: держать её пустой
    для каждого места незачем — мест сто двадцать шесть, а отвечают
    далеко не про все.
    """
    if now is not None:
        await session.execute(
            insert(PlacePaceStats).values(place_id=place_id).on_conflict_do_nothing()
        )

    for column, delta in ((was, -1), (now, 1)):
        if column is None:
            continue
        await session.execute(
            update(PlacePaceStats)
            .where(PlacePaceStats.place_id == place_id)
            .values({column: getattr(PlacePaceStats, column) + delta})
        )
