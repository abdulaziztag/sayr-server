"""Синхронизация своего: избранное, история выходов, город выезда.

Одна ручка в обе стороны. Клиент присылает то, что изменилось у него
после прошлой сверки, и получает то, что изменилось на сервере. При
расхождении побеждает запись со свежим временем правки — этого достаточно:
человек правит своё с одного телефона за раз, а не соревнуется сам с собой.

Часов двое. Время правки (`updated_at`) — по часам телефона: оно решает
только, чья правка свежее. Что отдать после `since`, решает время записи
на сервере (`server_updated_at`): правку, сделанную без связи или на
телефоне с отстающими часами, по времени телефона второй телефон считал
бы старой и не получал никогда.

Первый вход шлёт всё локальное без `since` — так гостевое избранное
и выходы попадают в аккаунт, ничего не спрашивая у человека.

Записанных треков здесь нет: они остаются на телефоне, это обещано
в политике.
"""

from datetime import date, datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import AfterValidator, BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.tokens import current_user
from ..db import get_session
from ..models import Place, User, UserFavorite, UserSetting, UserTripDay

router = APIRouter(prefix="/api/v1", tags=["sync"])

Outcome = Literal["planned", "went", "skipped", "unmarked"]
Pace = Literal["faster", "expected", "slower"]

#: Ключ замка pg_advisory_xact_lock для сверок одного человека. Число любое,
#: лишь бы не совпало с другими замками
_SYNC_LOCK = 0x53594E43  # «SYNC»


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


#: Время без зоны считаем UTC: сравнение с временем из базы иначе
#: падало на наивном и осведомлённом времени и роняло сверку в 500
Stamp = Annotated[datetime, AfterValidator(_utc)]


class FavoriteIn(BaseModel):
    slug: str
    updated_at: Stamp
    deleted: bool = False


class TripDayIn(BaseModel):
    slug: str
    day: date
    outcome: Outcome = "planned"
    pace: Pace | None = None
    distance_km: float | None = None
    elevation_gain_m: int | None = None
    answered_at: Stamp | None = None
    updated_at: Stamp
    deleted: bool = False


class SettingsIn(BaseModel):
    departure_city: str | None = None
    updated_at: Stamp


class SyncIn(BaseModel):
    #: Пусто — отдать всё: первый вход и переустановка
    since: Stamp | None = None
    favorites: list[FavoriteIn] = []
    trip_days: list[TripDayIn] = []
    settings: SettingsIn | None = None


class SyncOut(BaseModel):
    #: Время сервера: клиент запомнит его и пришлёт следующим `since`.
    #: Своё время телефона для этого не годится — часы у всех разные
    now: datetime
    favorites: list[FavoriteIn] = []
    trip_days: list[TripDayIn] = []
    settings: SettingsIn | None = None


async def _slug_to_id(session: AsyncSession, slugs: set[str]) -> dict[str, int]:
    if not slugs:
        return {}
    rows = (
        await session.execute(select(Place.slug, Place.id).where(Place.slug.in_(slugs)))
    ).all()
    return {slug: place_id for slug, place_id in rows}


@router.post("/sync", response_model=SyncOut)
async def sync(
    body: SyncIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SyncOut:
    ids = await _slug_to_id(
        session,
        {f.slug for f in body.favorites} | {t.slug for t in body.trip_days},
    )

    # Сверки одного человека идут по очереди, замок держится до коммита.
    # Две первые сверки с двух телефонов не заводят одно избранное дважды
    # (уникальность роняла вторую в 500), а метка `now` честная: всё, что
    # записано с меткой не позже неё, к этому моменту закоммичено и попадёт
    # в ответ, а следующая запись получит метку позже и уедет следующей сверкой
    await session.execute(select(func.pg_advisory_xact_lock(_SYNC_LOCK, user.id)))
    now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
    now = now.astimezone(timezone.utc)

    # --- то, что пришло с телефона -----------------------------------
    favorites = {
        row.place_id: row
        for row in (
            await session.execute(
                select(UserFavorite).where(UserFavorite.user_id == user.id)
            )
        ).scalars()
    }
    for item in body.favorites:
        place_id = ids.get(item.slug)
        # Место сняли с публикации, пока оно лежало в избранном на телефоне:
        # хранить ссылку в никуда незачем
        if place_id is None:
            continue
        # Часы телефона, убежавшие вперёд, выигрывали бы у всех правок
        # до той даты: время правки из будущего срезаем до времени сервера
        at = min(item.updated_at, now)
        row = favorites.get(place_id)
        if row is None:
            row = UserFavorite(user_id=user.id, place_id=place_id)
            session.add(row)
            favorites[place_id] = row
        elif row.updated_at >= at:
            continue
        row.updated_at = at
        row.server_updated_at = now
        row.deleted_at = at if item.deleted else None

    days = {
        (row.place_id, row.day): row
        for row in (
            await session.execute(
                select(UserTripDay).where(UserTripDay.user_id == user.id)
            )
        ).scalars()
    }
    for item in body.trip_days:
        place_id = ids.get(item.slug)
        if place_id is None:
            continue
        at = min(item.updated_at, now)
        row = days.get((place_id, item.day))
        if row is None:
            row = UserTripDay(user_id=user.id, place_id=place_id, day=item.day)
            session.add(row)
            days[(place_id, item.day)] = row
        elif row.updated_at >= at:
            continue
        row.outcome = item.outcome
        row.pace = item.pace
        row.distance_km = item.distance_km
        row.elevation_gain_m = item.elevation_gain_m
        row.answered_at = item.answered_at
        row.updated_at = at
        row.server_updated_at = now
        row.deleted_at = at if item.deleted else None

    settings_row = await session.get(UserSetting, user.id)
    if body.settings is not None:
        at = min(body.settings.updated_at, now)
        if settings_row is None:
            settings_row = UserSetting(user_id=user.id)
            session.add(settings_row)
        if settings_row.updated_at is None or settings_row.updated_at < at:
            settings_row.departure_city = body.settings.departure_city
            settings_row.updated_at = at
            settings_row.server_updated_at = now

    await session.commit()

    # --- то, что уезжает на телефон ----------------------------------
    since = body.since
    back_favorites = [
        FavoriteIn(
            slug=slug,
            updated_at=row.updated_at,
            deleted=row.deleted_at is not None,
        )
        for row, slug in (
            await session.execute(
                select(UserFavorite, Place.slug)
                .join(Place, Place.id == UserFavorite.place_id)
                .where(
                    UserFavorite.user_id == user.id,
                    *( [UserFavorite.server_updated_at > since] if since else [] ),
                )
            )
        ).all()
    ]
    back_days = [
        TripDayIn(
            slug=slug,
            day=row.day,
            outcome=row.outcome,
            pace=row.pace,
            distance_km=row.distance_km,
            elevation_gain_m=row.elevation_gain_m,
            answered_at=row.answered_at,
            updated_at=row.updated_at,
            deleted=row.deleted_at is not None,
        )
        for row, slug in (
            await session.execute(
                select(UserTripDay, Place.slug)
                .join(Place, Place.id == UserTripDay.place_id)
                .where(
                    UserTripDay.user_id == user.id,
                    *( [UserTripDay.server_updated_at > since] if since else [] ),
                )
            )
        ).all()
    ]
    back_settings = None
    if settings_row is not None and (
        since is None or settings_row.server_updated_at > since
    ):
        back_settings = SettingsIn(
            departure_city=settings_row.departure_city,
            updated_at=settings_row.updated_at,
        )

    return SyncOut(
        now=now, favorites=back_favorites, trip_days=back_days, settings=back_settings
    )
