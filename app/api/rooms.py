"""Комнаты попутчиков: открыть, найти, вступить по ссылке, попроситься,
разобрать заявки, пожаловаться и заблокировать.

Кто что видит (спека docs/superpowers/specs/2026-09-27-companions-design.md,
части 1 и 3):
- гость — только сколько компаний ищут попутчиков по дням;
- вошедший в открытом поиске — карточку организатора (имя, фото, пол,
  возраст), сколько человек, дорогу и заметку;
- участник комнаты — всех участников с никами в Telegram, ссылку «Позвать
  своих» и свою личную ссылку в группу;
- номер телефона — никто и никогда.

Требования и блокировки проверяются здесь, а не только в приложении:
в любую комнату — с именем и годом рождения, в открытую ещё и с 18 лет.
Фото не требуется (решение владельца 30.09): приложение только советует
его — с фото охотнее берут в компанию.
Всё за флагом `SAYR_ROOMS_OPEN`: выключен — ручки отвечают 404.
"""

import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..auth.tokens import current_user, optional_user
from ..config import settings
from ..db import get_session
from ..moderation import is_clean
from ..models import (
    Difficulty,
    Gender,
    Place,
    PushOutbox,
    Room,
    RoomMember,
    RoomReport,
    TgJob,
    TgMessage,
    User,
    UserBlock,
)
from ..push import outbox
from ..schemas import DEFAULT_LANG, Lang, pick
from ..tg import texts

router = APIRouter(prefix="/api/v1", tags=["rooms"])

TASHKENT = ZoneInfo("Asia/Tashkent")
#: Код комнаты и секрет ссылки — без похожих знаков: их переписывают руками
_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
#: Сколько дней вперёд показываем открытые комнаты места
AHEAD_DAYS = 30
#: Возраст по одному году рождения точно не узнать — спорный год решаем
#: в пользу отказа: 18+ значит год рождения не позже «текущий − 19»
ADULT_YEARS = 19
LIVE = ("requested", "joined")
Transport = Literal["own_car", "need_car", "self"]
Reason = Literal["abuse", "danger", "fake", "spam", "other"]
#: Почему человека убирают из группы: вышел сам, убрали (организатор,
#: блок, запрет попутчиков) или удалил аккаунт
KickReason = Literal["left", "removed", "deleted"]


def rooms_on() -> None:
    if not settings.rooms_open:
        raise HTTPException(status_code=404, detail="rooms_closed")


def today() -> date:
    return datetime.now(TASHKENT).date()


def _token(length: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def _avatar_url(user: User) -> str | None:
    name = Path(user.avatar.name).name if user.avatar else None
    return f"/media/avatars/{name}" if name else None


def _age(user: User) -> int | None:
    return today().year - user.birth_year if user.birth_year else None


def _end(room: Room) -> date:
    return room.day + timedelta(days=max(1, room.days) - 1)


def askable(room: Room) -> bool:
    """К комнате можно попроситься: открыта, не отменена и поход не прошёл —
    то же, что проверяет заявка. Ссылка /j/ живёт ровно столько же: иначе
    её раздавали бы туда, где «Попроситься» уже ответит отказом"""
    return room.is_open and room.status == "active" and _end(room) >= today()


# MARK: - Ответы


class CardOut(BaseModel):
    """Человек глазами попутчиков. Номера телефона здесь нет и не будет."""

    user_id: int
    member_id: int | None = None
    first_name: str
    avatar_url: str | None = None
    gender: str | None = None
    age: int | None = None
    #: Только участникам комнаты: иначе любой из поиска писал бы в личку
    telegram_username: str | None = None
    transport: str | None = None
    seats: int | None = None
    note: str = ""


class GroupOut(BaseModel):
    #: none | pending | ready | left | failed
    state: str
    #: Моя личная одноразовая ссылка в группу, пока я по ней не вошёл
    link: str | None = None
    link_used: bool = False


class RoomOut(BaseModel):
    code: str
    place_slug: str
    place_name: str
    day: date
    days: int
    is_open: bool
    #: active | cancelled | archived
    status: str
    #: Сколько человек в комнате вместе с организатором
    people: int
    #: Сколько среди них мужчин и женщин — видно и тем, кто ещё не вступил:
    #: состав компании решает, проситься ли. Кто пол не указал, не попадает
    #: ни в одно число
    men: int = 0
    women: int = 0
    organizer: CardOut | None = None
    #: organizer | joined | requested | declined | removed | left | none
    my_role: str
    members: list[CardOut] = []
    requests: list[CardOut] = []
    invite_url: str | None = None
    #: Ссылка для незнакомых sayr.info/j/{код}: по ней не вступают, а просятся,
    #: и берёт организатор. «Позвать своих» пускает сразу и без 18+ — такую
    #: в большую группу не выложишь. Видна всем, кто видит комнату: код и так
    #: виден в поиске. Пусто, если к комнате попроситься нельзя
    request_url: str | None = None
    group: GroupOut | None = None


class RoomBrief(BaseModel):
    code: str
    place_slug: str
    place_name: str
    day: date
    days: int
    is_open: bool
    status: str
    people: int
    organizer: CardOut | None = None
    my_role: str


class DayCount(BaseModel):
    day: date
    rooms: int


class PlaceRoomsOut(BaseModel):
    days: list[DayCount]
    #: Пусто у гостя: карточки чужих людей — только вошедшим
    rooms: list[RoomBrief] = []


class FeedPlace(BaseModel):
    """Место в ленте «Походы»: нарисовать строку и отфильтровать без каталога —
    в нём может не оказаться только что открытого места. Категория, сложность
    и регион с областью — ради тех же фильтров, что на главной"""

    slug: str
    name: str
    category: str
    #: Три ступени, как в каталоге; четвёртая приходит флагом `alpine`
    difficulty: str
    alpine: bool = False
    region_id: int
    region_name: str
    #: Область региона — группировка в фильтре, как на главной
    region_area: str | None = None
    #: Порядок региона в фильтре — тот же, что у каталога
    region_order: int = 0
    cover_thumb_url: str | None = None


class FeedDay(BaseModel):
    """Сколько компаний ищут попутчиков в место на день"""

    place_slug: str
    day: date
    rooms: int


class RoomsFeedOut(BaseModel):
    """Лента вкладки «Походы»: открытые комнаты во все места. Платные туры
    придут своей ручкой и своим видом карточки рядом с этой лентой"""

    places: list[FeedPlace]
    #: Числа по месту и дню — всем; гость видит только их
    days: list[FeedDay]
    #: Пусто у гостя: карточки чужих людей — только вошедшим
    rooms: list[RoomBrief] = []


def _card(member: RoomMember, contact: bool) -> CardOut:
    user = member.user
    return CardOut(
        user_id=user.id,
        member_id=member.id,
        first_name=user.first_name,
        avatar_url=_avatar_url(user),
        gender=user.gender.value if user.gender else None,
        age=_age(user),
        telegram_username=(user.telegram_username or None) if contact else None,
        transport=member.transport,
        seats=member.seats,
        note=member.note,
    )


def _organizer(room: Room) -> RoomMember | None:
    return next((m for m in room.members if m.role == "organizer"), None)


def _mine(room: Room, user: User) -> RoomMember | None:
    return next((m for m in room.members if m.user_id == user.id), None)


def _known_account(member: RoomMember, user: User) -> None:
    """Аккаунт Telegram человека известен со входа — строке участия сразу:
    Sayr Admin уберёт его из группы и узнает его сообщения, не дожидаясь
    входа по личной ссылке. Уже связанный по ссылке не трогаем — вход
    по ссылке остаётся запасным путём для тех, кто вошёл по номеру"""
    if member.tg_user_id is None and user.telegram_id is not None:
        member.tg_user_id = user.telegram_id


async def remember_telegram(session: AsyncSession, user_id: int, telegram_id: int) -> None:
    """Telegram только что привязан к аккаунту — его строкам участия тоже.
    Уже связанные по личной ссылке не трогаем: им человек в группу и вошёл"""
    await session.execute(
        update(RoomMember)
        .where(RoomMember.user_id == user_id, RoomMember.tg_user_id.is_(None))
        .values(tg_user_id=telegram_id)
    )


def _role(member: RoomMember | None) -> str:
    if member is None:
        return "none"
    return "organizer" if member.role == "organizer" else member.status


def _people(room: Room) -> int:
    return sum(1 for m in room.members if m.status == "joined")


def _genders(room: Room) -> tuple[int, int]:
    """Мужчины и женщины среди вступивших, организатор тоже вступивший"""
    joined = [m.user.gender for m in room.members if m.status == "joined"]
    return joined.count(Gender.male), joined.count(Gender.female)


def _out(room: Room, viewer: User, lang: Lang, hidden: set[int]) -> RoomOut:
    """`hidden` — с кем у смотрящего блокировка: если оба уже были в комнате,
    когда один заблокировал другого, друг друга (и ники) они больше не видят"""
    mine = _mine(room, viewer)
    role = _role(mine)
    insider = role in ("organizer", "joined")
    organizer = _organizer(room)
    men, women = _genders(room)
    out = RoomOut(
        code=room.code,
        place_slug=room.place.slug,
        place_name=pick(room.place.name, room.place.name_uz, lang),
        day=room.day,
        days=room.days,
        is_open=room.is_open,
        status=room.status,
        people=_people(room),
        men=men,
        women=women,
        organizer=(
            _card(organizer, contact=insider and organizer.user_id not in hidden)
            if organizer
            else None
        ),
        my_role=role,
    )
    if askable(room):
        out.request_url = f"{settings.public_url}/j/{room.code}"
    if insider:
        out.members = [
            _card(m, contact=True)
            for m in room.members
            if m.status == "joined" and m.role != "organizer" and m.user_id not in hidden
        ]
        out.invite_url = f"{settings.public_url}/r/{room.invite}"
        out.group = GroupOut(
            # «Уходит» — внутреннее состояние службы: для людей Sayr Admin
            # уже вышел, приложения этого слова не знают
            state="left" if room.tg_state == "leaving" else room.tg_state,
            link=mine.tg_link if mine.tg_link and not mine.tg_link_used else None,
            link_used=mine.tg_link_used,
        )
    if role == "organizer":
        out.requests = [
            _card(m, contact=False)
            for m in room.members
            if m.status == "requested" and m.user_id not in hidden
        ]
    return out


async def _reply(session: AsyncSession, room: Room, viewer: User, lang: Lang) -> RoomOut:
    return _out(room, viewer, lang, await blocked_ids(session, viewer.id))


def _brief(room: Room, viewer: User | None, lang: Lang) -> RoomBrief:
    organizer = _organizer(room)
    return RoomBrief(
        code=room.code,
        place_slug=room.place.slug,
        place_name=pick(room.place.name, room.place.name_uz, lang),
        day=room.day,
        days=room.days,
        is_open=room.is_open,
        status=room.status,
        people=_people(room),
        organizer=_card(organizer, contact=False) if organizer else None,
        my_role=_role(_mine(room, viewer)) if viewer else "none",
    )


# MARK: - Проверки


def _profile_missing(user: User) -> str | None:
    """Чего не хватает анкете для любой комнаты — имени или года рождения.
    Год — и для своих: анкета после входа теперь спрашивает его у всех,
    а старым аккаунтам без года приложение предложит дописать только его.
    Фото больше не нужно нигде. Коды прежние: вышедшие сборки знают их
    и ведут в анкету, а `photo_required` просто перестаёт приходить"""
    if not user.first_name.strip():
        return "name_required"
    if not user.birth_year:
        return "birth_year_required"
    return None


def _require_profile(user: User) -> None:
    """Для своих: имя и год рождения."""
    if missing := _profile_missing(user):
        raise HTTPException(status_code=403, detail=missing)


def _adult(user: User) -> bool:
    return user.birth_year is not None and user.birth_year <= today().year - ADULT_YEARS


def _require_open_profile(user: User) -> None:
    """Для незнакомых: имя, год рождения и 18+."""
    _require_profile(user)
    if not _adult(user):
        raise HTTPException(status_code=403, detail="adult_only")


def _require_not_banned(user: User) -> None:
    if user.companions_banned_at is not None:
        raise HTTPException(status_code=403, detail="companions_banned")


def _require_clean(text: str | None) -> None:
    if not is_clean(text):
        raise HTTPException(status_code=422, detail="bad_text")


def _check_transport(transport: str | None, seats: int | None) -> int | None:
    """Места — только у своей машины; у остальных забываем, что прислали"""
    return seats if transport == "own_car" else None


async def blocked_ids(session: AsyncSession, user_id: int) -> set[int]:
    """Все, с кем у человека блокировка — в любую сторону"""
    rows = (
        await session.execute(
            select(UserBlock.blocker_id, UserBlock.blocked_id).where(
                or_(UserBlock.blocker_id == user_id, UserBlock.blocked_id == user_id)
            )
        )
    ).all()
    return {b if a == user_id else a for a, b in rows}


def _blocked(room: Room, hidden: set[int]) -> bool:
    """В комнате есть тот, с кем блокировка: организатор или любой вступивший.
    Не только организатор — иначе двое, заблокировавшие друг друга, сошлись бы
    в одной комнате и увидели ники друг друга. Проверяется там, где в комнату
    входят: по ссылке и при одобрении заявки. Лента, чужой просмотр и заявка
    смотрят только на организатора: кто вступил, со стороны не видно, и
    пропавшая комната выдала бы заблокированному, куда идёт тот, кто его
    заблокировал"""
    return any(m.user_id in hidden for m in room.members if m.status == "joined")


async def _lock_user(session: AsyncSession, user_id: int) -> None:
    """Держать строку человека до конца транзакции. Проверка «одна комната
    на день» и запись — два шага: без замка два параллельных запроса оба
    прошли бы проверку раньше, чем любой запишется. NO KEY UPDATE, а не
    UPDATE: не мешает внешним ключам — пушу этому человеку из чужого запроса"""
    await session.execute(
        select(User.id).where(User.id == user_id).with_for_update(key_share=True)
    )


async def _busy(
    session: AsyncSession, user_id: int, day: date, days: int, skip_room: int | None = None
) -> bool:
    """Уже идёт куда-то в эти дни: своя или чужая живая комната"""
    end = day + timedelta(days=max(1, days) - 1)
    rows = (
        await session.execute(
            select(Room)
            .join(RoomMember, RoomMember.room_id == Room.id)
            .where(
                RoomMember.user_id == user_id,
                RoomMember.status.in_(LIVE),
                Room.status == "active",
                Room.day <= end,
            )
        )
    ).scalars()
    return any(r.id != skip_room and _end(r) >= day for r in rows)


async def _load(session: AsyncSession, *, lock: bool = False, **where) -> Room | None:
    """`lock` — держать строку комнаты до конца транзакции: вступление по
    ссылке и одобрение идут в комнату по одному, и двое, заблокировавшие
    друг друга, не войдут в неё разом, разминувшись с проверкой. Замок
    человека (`_lock_user`) — раньше замка комнаты, везде в этом порядке"""
    column, value = next(iter(where.items()))
    query = (
        select(Room)
        .where(getattr(Room, column) == value)
        .options(
            selectinload(Room.place),
            selectinload(Room.members).selectinload(RoomMember.user),
        )
        # Перечитать и то, что уже лежит в сессии: после записи список
        # участников в памяти не знает о новой заявке
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update(key_share=True)
    return (await session.execute(query)).scalar_one_or_none()


async def _room_for(session: AsyncSession, code: str) -> Room:
    room = await _load(session, code=code)
    if room is None:
        raise HTTPException(status_code=404, detail="room_not_found")
    return room


def _require_organizer(room: Room, user: User) -> None:
    if _role(_mine(room, user)) != "organizer":
        raise HTTPException(status_code=403, detail="organizer_only")


def _require_active(room: Room) -> None:
    if room.status != "active" or _end(room) < today():
        raise HTTPException(status_code=410, detail="room_closed")


# MARK: - Побочные дела: пуши и задания службе


def notify(
    session: AsyncSession, room: Room, user_id: int | None, kind: str, name: str = ""
) -> None:
    if user_id is None:
        return
    outbox.enqueue(
        session,
        user_id,
        kind,
        {
            "place": room.place.name,
            "place_uz": room.place.name_uz or "",
            "day": room.day.isoformat(),
            "name": name,
        },
        room.code,
    )


def _after_join(session: AsyncSession, room: Room, member: RoomMember) -> None:
    """Второй человек в комнате — служба заводит группу; группа уже есть —
    выдать вошедшему личную ссылку в неё"""
    if room.tg_state == "none" and _people(room) >= 2:
        room.tg_state = "pending"
        session.add(TgJob(kind="create_group", room_id=room.id, payload={}))
    elif room.tg_state == "ready":
        session.add(TgJob(kind="invite_link", room_id=room.id, member_id=member.id, payload={}))


def kick(session: AsyncSession, room: Room, member: RoomMember, reason: KickReason) -> None:
    """Ушёл из комнаты — уходит и из группы. Задание и тогда, когда служба
    его аккаунта не знает: личная ссылка гаснет, иначе удалённый открыл бы её
    позже и оказался в группе со всеми никами. Пока группа создаётся, тоже:
    ссылку ему могли успеть выдать. Аккаунт и ссылку кладём в задание
    заранее — строка участия может уйти вместе с удалённым аккаунтом.

    Причина и имя — для заметки в группе, когда служба выгонит его на деле.
    Удалившему аккаунт заметки нет и имени в задании тоже: удаление уносит
    всё о человеке, а новая запись о нём в группе пережила бы и его, и
    Sayr Admin"""
    if room.tg_state in ("pending", "ready", "leaving"):
        payload = {
            "tg_user_id": member.tg_user_id,
            "tg_user_hash": member.tg_user_hash,
            "tg_link": member.tg_link,
            "reason": reason,
        }
        if reason != "deleted":
            payload |= texts.person(member.user)
        session.add(TgJob(kind="kick", room_id=room.id, member_id=member.id, payload=payload))


def request_note(
    session: AsyncSession, room: Room, member: RoomMember, note: int | None = None
) -> None:
    """С заявкой что-то случилось — служба сверяет с этим её заметку в группе
    (service._request_note): новую объявит, одобренную поправит, остальные
    удалит. Ставим на любую перемену, пока Sayr Admin в группе: заметка могла
    уже уйти, а могла ещё ждать очереди, — что делать, служба решает по тому,
    что с заявкой к часу задания. `note` — номер заметки, когда строки заявки
    к тому часу уже не будет"""
    if room.tg_state == "ready":
        payload = {"note": note} if note else {}
        session.add(
            TgJob(kind="request_note", room_id=room.id, member_id=member.id, payload=payload)
        )


def cancel(session: AsyncSession, room: Room) -> None:
    """Поход отменён: всем живым участникам пуш, в группу — сообщение"""
    now = datetime.now(timezone.utc)
    room.status = "cancelled"
    room.cancelled_at = now
    for m in room.members:
        if m.role == "organizer" or m.status not in LIVE:
            continue
        notify(session, room, m.user_id, "room_cancelled")
        if m.status == "requested":
            m.status = "declined"
            m.decided_at = now
            request_note(session, room, m)
    if room.tg_state == "ready":
        session.add(TgJob(kind="post", room_id=room.id, payload={"text": "cancelled"}))


# MARK: - Ручки


class RoomIn(BaseModel):
    place: str = Field(..., max_length=80)
    day: date
    days: int = Field(1, ge=1, le=14)
    is_open: bool = False
    transport: Transport | None = None
    seats: int | None = Field(None, ge=1, le=6)
    note: str = Field("", max_length=140)


class RoomPatch(BaseModel):
    is_open: bool | None = None
    transport: Transport | None = None
    seats: int | None = Field(None, ge=1, le=6)
    note: str | None = Field(None, max_length=140)


class JoinIn(BaseModel):
    """Своя дорога и заметка — у вступающего по ссылке необязательны"""

    transport: Transport | None = None
    seats: int | None = Field(None, ge=1, le=6)
    note: str = Field("", max_length=140)


@router.get("/places/{slug}/rooms", response_model=PlaceRoomsOut, dependencies=[Depends(rooms_on)])
async def place_rooms(
    slug: str,
    lang: Lang = Query(DEFAULT_LANG),
    ahead: int = Query(AHEAD_DAYS, ge=1, le=60),
    user: User | None = Depends(optional_user),
    session: AsyncSession = Depends(get_session),
) -> PlaceRoomsOut:
    shown = await _open_rooms(session, user, ahead, slug=slug)
    counts: dict[date, int] = {}
    for room in shown:
        counts[room.day] = counts.get(room.day, 0) + 1
    return PlaceRoomsOut(
        days=[DayCount(day=d, rooms=n) for d, n in sorted(counts.items())],
        rooms=[_brief(r, user, lang) for r in shown] if user else [],
    )


@router.get("/rooms", response_model=RoomsFeedOut, dependencies=[Depends(rooms_on)])
async def rooms_feed(
    lang: Lang = Query(DEFAULT_LANG),
    ahead: int = Query(AHEAD_DAYS, ge=1, le=60),
    user: User | None = Depends(optional_user),
    session: AsyncSession = Depends(get_session),
) -> RoomsFeedOut:
    """Вкладка «Походы»: открытые комнаты во все места на месяц вперёд.
    Правила те же, что у комнат места: гостю — числа, вошедшему — карточки"""
    shown = await _open_rooms(session, user, ahead)
    places: dict[str, FeedPlace] = {}
    counts: dict[tuple[date, str], int] = {}
    for room in shown:
        place = room.place
        if place.slug not in places:
            cover = place.photos[0] if place.photos else None
            region = place.region
            extreme = place.difficulty is Difficulty.extreme
            places[place.slug] = FeedPlace(
                slug=place.slug,
                name=pick(place.name, place.name_uz, lang),
                category=place.category.value,
                # Как в каталоге: наружу только три ступени, четвёртая — флагом
                difficulty=(Difficulty.hard if extreme else place.difficulty).value,
                alpine=extreme,
                region_id=place.region_id,
                region_name=pick(region.name, region.name_uz, lang),
                region_area=pick(region.area, region.area_uz, lang) if region.area else None,
                region_order=region.sort_order,
                cover_thumb_url=(cover.thumb_url or cover.url) if cover else None,
            )
        counts[(room.day, place.slug)] = counts.get((room.day, place.slug), 0) + 1
    return RoomsFeedOut(
        places=list(places.values()),
        days=[FeedDay(place_slug=s, day=d, rooms=n) for (d, s), n in sorted(counts.items())],
        rooms=[_brief(r, user, lang) for r in shown] if user else [],
    )


async def _open_rooms(
    session: AsyncSession, user: User | None, ahead: int, slug: str | None = None
) -> list[Room]:
    """Живые открытые комнаты на `ahead` дней вперёд — одного места или всех
    опубликованных. Без организаторов под запретом и без тех, с кем у
    смотрящего блокировка в любую сторону. Только организатор, а не любой
    вступивший (`_blocked`): иначе, сверив числа с гостевыми, заблокированный
    узнал бы, куда и когда идёт тот, кто его заблокировал"""
    start = today()
    query = (
        select(Room)
        .join(Place, Place.id == Room.place_id)
        .where(
            Room.status == "active",
            Room.is_open,
            Room.day >= start,
            Room.day <= start + timedelta(days=ahead),
        )
        .options(
            selectinload(Room.place).selectinload(Place.region),
            selectinload(Room.place).selectinload(Place.photos),
            selectinload(Room.members).selectinload(RoomMember.user),
        )
        .order_by(Room.day, Room.id)
    )
    query = query.where(Place.slug == slug) if slug is not None else query.where(Place.is_published)
    rooms = (await session.execute(query)).scalars()
    hidden = await blocked_ids(session, user.id) if user else set()
    shown = []
    for room in rooms:
        organizer = _organizer(room)
        if organizer is None or organizer.user.companions_banned_at is not None:
            continue
        if organizer.user_id in hidden:
            continue
        shown.append(room)
    return shown


@router.post("/rooms", response_model=RoomOut, status_code=201, dependencies=[Depends(rooms_on)])
async def open_room(
    body: RoomIn,
    lang: Lang = Query(DEFAULT_LANG),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RoomOut:
    _require_not_banned(user)
    (_require_open_profile if body.is_open else _require_profile)(user)
    _require_clean(body.note)
    if body.day < today():
        raise HTTPException(status_code=422, detail="day_past")
    place = (
        await session.execute(select(Place).where(Place.slug == body.place, Place.is_published))
    ).scalar_one_or_none()
    if place is None:
        raise HTTPException(status_code=404, detail="place_not_found")
    await _lock_user(session, user.id)
    if await _busy(session, user.id, body.day, body.days):
        raise HTTPException(status_code=409, detail="busy_day")

    room = Room(
        code=_token(8),
        invite=_token(12),
        place_id=place.id,
        organizer_id=user.id,
        day=body.day,
        days=body.days,
        is_open=body.is_open,
    )
    session.add(room)
    await session.flush()
    organizer = RoomMember(
        room_id=room.id,
        user_id=user.id,
        role="organizer",
        status="joined",
        source="organizer",
        transport=body.transport,
        seats=_check_transport(body.transport, body.seats),
        note=body.note.strip(),
    )
    _known_account(organizer, user)
    session.add(organizer)
    await session.commit()
    return await _reply(session, await _load(session, id=room.id), user, lang)


@router.get("/rooms/{code}", response_model=RoomOut, dependencies=[Depends(rooms_on)])
async def room_view(
    code: str,
    lang: Lang = Query(DEFAULT_LANG),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RoomOut:
    room = await _room_for(session, code)
    role = _role(_mine(room, user))
    hidden = await blocked_ids(session, user.id)
    if role in ("organizer", "joined"):
        return _out(room, user, lang, hidden)
    # Чужому — только открытую и живую комнату, и не того, с кем блокировка.
    # Блок с вступившим не прячет: пропавшая комната выдала бы, кто в ней
    organizer = _organizer(room)
    visible = (room.is_open and room.status == "active") or role != "none"
    if not visible or (organizer and organizer.user_id in hidden):
        raise HTTPException(status_code=404, detail="room_not_found")
    return _out(room, user, lang, hidden)


@router.patch("/rooms/{code}", response_model=RoomOut, dependencies=[Depends(rooms_on)])
async def room_edit(
    code: str,
    body: RoomPatch,
    lang: Lang = Query(DEFAULT_LANG),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RoomOut:
    room = await _room_for(session, code)
    _require_organizer(room, user)
    _require_active(room)
    mine = _mine(room, user)
    fields = body.model_dump(exclude_unset=True)
    if fields.get("is_open"):
        _require_not_banned(user)
        _require_open_profile(user)
    if "note" in fields:
        _require_clean(body.note)
        mine.note = (body.note or "").strip()
    if "is_open" in fields and body.is_open is not None:
        room.is_open = body.is_open
    if "transport" in fields:
        mine.transport = body.transport
    if "transport" in fields or "seats" in fields:
        mine.seats = _check_transport(mine.transport, body.seats if "seats" in fields else mine.seats)
    await session.commit()
    return await _reply(session, await _load(session, id=room.id), user, lang)


@router.delete("/rooms/{code}", status_code=204, dependencies=[Depends(rooms_on)])
async def room_cancel(
    code: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    room = await _room_for(session, code)
    _require_organizer(room, user)
    if room.status != "active":
        return
    cancel(session, room)
    await session.commit()


@router.post("/rooms/{code}/invite/reset", response_model=RoomOut, dependencies=[Depends(rooms_on)])
async def invite_reset(
    code: str,
    lang: Lang = Query(DEFAULT_LANG),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RoomOut:
    """Ссылка ушла не туда — старая перестаёт работать"""
    room = await _room_for(session, code)
    _require_organizer(room, user)
    room.invite = _token(12)
    await session.commit()
    return await _reply(session, room, user, lang)


async def _by_invite(session: AsyncSession, invite: str, user: User, lock: bool = False) -> Room:
    room = await _load(session, lock=lock, invite=invite)
    if room is None or room.status != "active":
        raise HTTPException(status_code=404, detail="invite_not_found")
    # Со стороны ссылка не ведёт туда, где есть тот, с кем блокировка. Своему
    # комната открывается как была: он в ней раньше, чем заблокировал
    inside = _role(_mine(room, user)) in ("organizer", "joined")
    if not inside and _blocked(room, await blocked_ids(session, user.id)):
        raise HTTPException(status_code=404, detail="invite_not_found")
    return room


@router.get("/invites/{invite}", response_model=RoomOut, dependencies=[Depends(rooms_on)])
async def invite_view(
    invite: str,
    lang: Lang = Query(DEFAULT_LANG),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RoomOut:
    """Комната по ссылке «Позвать своих» — до вступления, даже закрытая"""
    return await _reply(session, await _by_invite(session, invite, user), user, lang)


@router.post("/invites/{invite}/join", response_model=RoomOut, dependencies=[Depends(rooms_on)])
async def invite_join(
    invite: str,
    body: JoinIn | None = None,
    lang: Lang = Query(DEFAULT_LANG),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RoomOut:
    """Своих — без одобрения: ссылку им дал организатор или участник"""
    body = body or JoinIn()
    # Замки — до чтения комнаты, человек, потом комната: второй такой же
    # запрос увидит уже записанное, а одобрение в ту же комнату — вступившего
    await _lock_user(session, user.id)
    room = await _by_invite(session, invite, user, lock=True)
    _require_active(room)
    mine = _mine(room, user)
    role = _role(mine)
    if role in ("organizer", "joined"):
        return await _reply(session, room, user, lang)
    if role == "removed":
        raise HTTPException(status_code=403, detail="removed")
    now = datetime.now(timezone.utc)
    back_after = timedelta(seconds=settings.tg_link_renew_after_sec)
    if role == "left" and mine.decided_at and mine.decided_at > now - back_after:
        # Вышел и сразу обратно — не чаще раза в tg_link_renew_after_sec:
        # каждый круг — гашение старой ссылки и новая от единственного
        # аккаунта Sayr Admin, а организатору — пуш «вступил»
        raise HTTPException(status_code=429, detail="too_often")
    _require_not_banned(user)
    _require_profile(user)
    _require_clean(body.note)
    if await _busy(session, user.id, room.day, room.days, skip_room=room.id):
        raise HTTPException(status_code=409, detail="busy_day")

    if mine is None:
        mine = RoomMember(room_id=room.id, user_id=user.id, role="member", source="link")
        session.add(mine)
        room.members.append(mine)
    _known_account(mine, user)
    mine.status = "joined"
    mine.source = "link"
    mine.decided_at = now
    if body.transport:
        mine.transport = body.transport
        mine.seats = _check_transport(body.transport, body.seats)
    if body.note:
        mine.note = body.note.strip()
    await session.flush()
    notify(session, room, room.organizer_id, "room_joined", user.first_name)
    _after_join(session, room, mine)
    if role == "requested":
        # Просился, а вошёл по ссылке своих: заметка о заявке — «теперь в походе»
        request_note(session, room, mine)
    await session.commit()
    return await _reply(session, await _load(session, id=room.id), user, lang)


@router.post("/rooms/{code}/requests", response_model=RoomOut, dependencies=[Depends(rooms_on)])
async def room_request(
    code: str,
    body: JoinIn,
    lang: Lang = Query(DEFAULT_LANG),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RoomOut:
    # Замок — до чтения комнаты: второй такой же запрос увидит уже записанное
    await _lock_user(session, user.id)
    room = await _room_for(session, code)
    # Блок с вступившим заявку не останавливает — отказ выдал бы, кто в
    # комнате. Вместе их не сведёт одобрение (`member_blocked`)
    organizer = _organizer(room)
    if not room.is_open or (organizer and organizer.user_id in await blocked_ids(session, user.id)):
        raise HTTPException(status_code=404, detail="room_not_found")
    _require_active(room)
    mine = _mine(room, user)
    role = _role(mine)
    if role in ("organizer", "joined"):
        raise HTTPException(status_code=409, detail="already_member")
    if role == "requested":
        raise HTTPException(status_code=409, detail="already_requested")
    if role == "declined":
        raise HTTPException(status_code=409, detail="declined")
    if role == "removed":
        raise HTTPException(status_code=403, detail="removed")
    _require_not_banned(user)
    _require_open_profile(user)
    _require_clean(body.note)
    if await _busy(session, user.id, room.day, room.days, skip_room=room.id):
        raise HTTPException(status_code=409, detail="busy_day")

    if mine is None:
        mine = RoomMember(room_id=room.id, user_id=user.id, role="member")
        session.add(mine)
    _known_account(mine, user)
    mine.status = "requested"
    mine.source = "request"
    mine.transport = body.transport
    mine.seats = _check_transport(body.transport, body.seats)
    mine.note = body.note.strip()
    mine.decided_at = None
    notify(session, room, room.organizer_id, "room_request", user.first_name)
    await session.flush()  # новой строке — номер, заданию — на неё ссылку
    request_note(session, room, mine)
    await session.commit()
    return await _reply(session, await _load(session, id=room.id), user, lang)


def _member(room: Room, member_id: int) -> RoomMember:
    member = next((m for m in room.members if m.id == member_id), None)
    if member is None or member.role == "organizer":
        raise HTTPException(status_code=404, detail="member_not_found")
    return member


@router.post(
    "/rooms/{code}/members/{member_id}/approve",
    response_model=RoomOut,
    dependencies=[Depends(rooms_on)],
)
async def member_approve(
    code: str,
    member_id: int,
    lang: Lang = Query(DEFAULT_LANG),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RoomOut:
    room = await _room_for(session, code)
    _require_organizer(room, user)
    # Замки — человек, потом комната, как при вступлении по ссылке, — и всё
    # перечитать уже под ними: иначе двойное нажатие одобрило бы дважды, а
    # вступивший в ту же минуту разминулся бы с проверкой блокировок
    await _lock_user(session, _member(room, member_id).user_id)
    room = await _load(session, lock=True, id=room.id)
    _require_active(room)
    member = _member(room, member_id)
    if member.status != "requested":
        raise HTTPException(status_code=409, detail="not_requested")
    if member.user.companions_banned_at is not None:
        raise HTTPException(status_code=409, detail="member_banned")
    # Анкету просящегося проверила заявка, но пока та ждала, год могли стереть
    # или сменить на детский. Отказ — 409 про него, как member_banned: 403 с
    # его кодом приложение организатора приняло бы на свой счёт и повело бы
    # в анкету самого организатора
    if _profile_missing(member.user) or not _adult(member.user):
        raise HTTPException(status_code=409, detail="member_profile")
    # Пока заявка ждала, в комнату мог вступить тот, с кем у просящегося
    # блокировка: вместе их не сводим
    if _blocked(room, await blocked_ids(session, member.user_id)):
        raise HTTPException(status_code=409, detail="member_blocked")
    if await _busy(session, member.user_id, room.day, room.days, skip_room=room.id):
        raise HTTPException(status_code=409, detail="member_busy")
    member.status = "joined"
    member.decided_at = datetime.now(timezone.utc)
    notify(session, room, member.user_id, "room_approved")
    _after_join(session, room, member)
    request_note(session, room, member)
    await session.commit()
    return await _reply(session, await _load(session, id=room.id), user, lang)


@router.post(
    "/rooms/{code}/members/{member_id}/decline",
    response_model=RoomOut,
    dependencies=[Depends(rooms_on)],
)
async def member_decline(
    code: str,
    member_id: int,
    lang: Lang = Query(DEFAULT_LANG),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RoomOut:
    room = await _room_for(session, code)
    _require_organizer(room, user)
    member = _member(room, member_id)
    if member.status != "requested":
        raise HTTPException(status_code=409, detail="not_requested")
    member.status = "declined"
    member.decided_at = datetime.now(timezone.utc)
    notify(session, room, member.user_id, "room_declined")
    request_note(session, room, member)
    await session.commit()
    return await _reply(session, await _load(session, id=room.id), user, lang)


@router.delete(
    "/rooms/{code}/members/{member_id}", response_model=RoomOut, dependencies=[Depends(rooms_on)]
)
async def member_remove(
    code: str,
    member_id: int,
    lang: Lang = Query(DEFAULT_LANG),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RoomOut:
    room = await _room_for(session, code)
    _require_organizer(room, user)
    member = _member(room, member_id)
    if member.status not in LIVE:
        raise HTTPException(status_code=409, detail="not_member")
    was_joined = member.status == "joined"
    member.status = "removed"
    member.decided_at = datetime.now(timezone.utc)
    notify(session, room, member.user_id, "room_removed")
    if was_joined:
        kick(session, room, member, "removed")
    else:
        request_note(session, room, member)
    await session.commit()
    return await _reply(session, await _load(session, id=room.id), user, lang)


@router.post("/rooms/{code}/leave", status_code=204, dependencies=[Depends(rooms_on)])
async def room_leave(
    code: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    room = await _room_for(session, code)
    mine = _mine(room, user)
    role = _role(mine)
    if role == "organizer":
        # Организатор не уходит, а отменяет поход: иначе комната осталась бы
        # без того, кто одобряет заявки
        raise HTTPException(status_code=409, detail="organizer_cannot_leave")
    if role not in LIVE:
        return
    mine.status = "left"
    mine.decided_at = datetime.now(timezone.utc)
    if role == "joined":
        kick(session, room, mine, "left")
    else:
        request_note(session, room, mine)  # отозвал заявку
    await session.commit()


@router.post("/rooms/{code}/group-link", status_code=202, dependencies=[Depends(rooms_on)])
async def group_link(
    code: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Личная ссылка сгорела (её перехватили) — выдать новую. Только взамен
    использованной и не чаще раза в `tg_link_renew_after_sec`: каждая ссылка —
    вызов exportChatInvite от единственного аккаунта Sayr Admin, и частые
    вызовы Telegram наказывает паузой для всех групп разом"""
    room = await _room_for(session, code)
    mine = _mine(room, user)
    if _role(mine) not in ("organizer", "joined"):
        raise HTTPException(status_code=403, detail="members_only")
    if room.tg_state != "ready":
        raise HTTPException(status_code=409, detail="group_not_ready")
    if mine.tg_link and not mine.tg_link_used:
        raise HTTPException(status_code=409, detail="link_not_used")
    # Замок — чтобы двойное нажатие не поставило два задания разом
    await _lock_user(session, user.id)
    since = datetime.now(timezone.utc) - timedelta(seconds=settings.tg_link_renew_after_sec)
    jobs = (
        await session.execute(
            select(TgJob.status, TgJob.payload).where(
                TgJob.kind == "invite_link",
                TgJob.member_id == mine.id,
                (TgJob.status == "pending") | (TgJob.created_at > since),
            )
        )
    ).all()
    if any(status == "pending" for status, _ in jobs):
        raise HTTPException(status_code=409, detail="link_pending")
    # В предел идут только «выдать новую»: первая ссылка, выданная при
    # вступлении, не мешает заменить её, если её перехватили в ту же минуту
    if any(payload.get("renew") for _, payload in jobs):
        raise HTTPException(status_code=429, detail="too_often")
    # Старую служба погасит, прежде чем выдать новую
    payload = {"renew": True} | ({"revoke": mine.tg_link} if mine.tg_link else {})
    mine.tg_link = None
    mine.tg_link_used = False
    session.add(TgJob(kind="invite_link", room_id=room.id, member_id=mine.id, payload=payload))
    await session.commit()
    return {"state": "pending"}


@router.get("/me/rooms", response_model=list[RoomBrief], dependencies=[Depends(rooms_on)])
async def my_rooms(
    lang: Lang = Query(DEFAULT_LANG),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[RoomBrief]:
    """Мои живые комнаты и отменённые, чей день ещё не прошёл"""
    rooms = (
        await session.execute(
            select(Room)
            .join(RoomMember, RoomMember.room_id == Room.id)
            .where(
                RoomMember.user_id == user.id,
                RoomMember.status.in_(LIVE),
                or_(
                    Room.status == "active",
                    (Room.status == "cancelled") & (Room.day >= today()),
                ),
            )
            .options(
                selectinload(Room.place),
                selectinload(Room.members).selectinload(RoomMember.user),
            )
            .order_by(Room.day)
        )
    ).scalars()
    return [_brief(r, user, lang) for r in rooms]


class ReportIn(BaseModel):
    user_id: int | None = None
    room: str | None = Field(None, max_length=8)
    reason: Reason
    text: str = Field("", max_length=500)


@router.post("/reports", status_code=204, dependencies=[Depends(rooms_on)])
async def report(
    body: ReportIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Жалобы разбирает владелец руками, поэтому очередь бережём: одна
    неразобранная жалоба на ту же цель от того же человека и не больше
    `room_reports_per_day` в сутки — иначе любой аккаунт завалил бы её"""
    if body.user_id is None and body.room is None:
        raise HTTPException(status_code=422, detail="target_required")
    room_id = None
    if body.room:
        room = (await session.execute(select(Room).where(Room.code == body.room))).scalar_one_or_none()
        room_id = room.id if room else None
    target = await session.get(User, body.user_id) if body.user_id else None
    target_id = target.id if target else None
    # Замок — чтобы две одинаковые жалобы разом не прошли обе проверки
    await _lock_user(session, user.id)
    same = (
        await session.execute(
            select(RoomReport.id)
            .where(
                RoomReport.reporter_id == user.id,
                RoomReport.target_user_id.is_not_distinct_from(target_id),
                RoomReport.room_id.is_not_distinct_from(room_id),
                RoomReport.resolved_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if same is not None:
        # Та же жалоба уже ждёт разбора — вторая ничего не добавит. Человеку
        # отвечаем как обычно: жалоба у нас
        return
    recent = (
        await session.execute(
            select(func.count())
            .select_from(RoomReport)
            .where(
                RoomReport.reporter_id == user.id,
                RoomReport.created_at > datetime.now(timezone.utc) - timedelta(days=1),
            )
        )
    ).scalar_one()
    if recent >= settings.room_reports_per_day:
        raise HTTPException(status_code=429, detail="too_many_reports")
    session.add(
        RoomReport(
            reporter_id=user.id,
            target_user_id=target_id,
            room_id=room_id,
            reason=body.reason,
            text=body.text.strip(),
        )
    )
    await session.commit()


class BlockIn(BaseModel):
    user_id: int


class SharedRoom(BaseModel):
    code: str
    place_name: str
    day: date


class BlockOut(BaseModel):
    #: Где после блока оба остались простыми участниками: сам блок никого
    #: из них не выводит, и приложение сразу предлагает заблокировавшему выйти
    shared_rooms: list[SharedRoom] = []


@router.post("/blocks", response_model=BlockOut, dependencies=[Depends(rooms_on)])
async def block(
    body: BlockIn,
    lang: Lang = Query(DEFAULT_LANG),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> BlockOut:
    """Заблокировать. Если уже в одной комнате: организатор убирает его,
    а если организатор — он, сам человек из комнаты выходит. Двое простых
    участников остаются оба — иначе блоком можно было бы выставить из
    комнаты кого угодно, — но друг друга в комнате больше не видят (`_out`),
    а в чужие комнаты друг к другу не попадают (`_blocked`). Такие общие
    комнаты уходят в ответ: приложение сразу предлагает заблокировавшему
    выйти (решение владельца 29.09). Отменённые комнаты — тоже: их группа
    остаётся тем, кто идёт всё равно (service._welcome), и заблокированному
    в ней не место"""
    if body.user_id == user.id:
        raise HTTPException(status_code=422, detail="self_block")
    if await session.get(User, body.user_id) is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    if await session.get(UserBlock, (user.id, body.user_id)) is None:
        session.add(UserBlock(blocker_id=user.id, blocked_id=body.user_id))

    now = datetime.now(timezone.utc)
    rooms = (
        await session.execute(
            select(Room)
            .join(RoomMember, RoomMember.room_id == Room.id)
            .where(
                Room.status.in_(("active", "cancelled")),
                RoomMember.user_id.in_([user.id, body.user_id]),
            )
            .options(
                selectinload(Room.place),
                selectinload(Room.members).selectinload(RoomMember.user),
            )
        )
    ).scalars().unique()
    shared = []
    for room in rooms:
        me, them = _mine(room, user), next(
            (m for m in room.members if m.user_id == body.user_id), None
        )
        if me is None or them is None:
            continue
        if _role(me) == "organizer" and them.status in LIVE:
            joined = them.status == "joined"
            them.status, them.decided_at = "removed", now
            if joined:
                kick(session, room, them, "removed")
            else:
                request_note(session, room, them)
        elif _role(them) == "organizer" and me.status in LIVE:
            joined = me.status == "joined"
            me.status, me.decided_at = "left", now
            if joined:
                kick(session, room, me, "left")
            else:
                request_note(session, room, me)
        elif _role(me) == _role(them) == "joined":
            # Только оба вступивших: заявку к тому, с кем блокировка,
            # организатор уже не одобрит (`member_blocked`) — вместе их
            # не сведёт, и выходить незачем
            shared.append(room)
    out = BlockOut(
        shared_rooms=[
            SharedRoom(
                code=r.code, place_name=pick(r.place.name, r.place.name_uz, lang), day=r.day
            )
            for r in sorted(shared, key=lambda r: (r.day, r.id))
        ]
    )
    await session.commit()
    return out


@router.delete("/blocks/{user_id}", status_code=204, dependencies=[Depends(rooms_on)])
async def unblock(
    user_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await session.execute(
        delete(UserBlock).where(UserBlock.blocker_id == user.id, UserBlock.blocked_id == user_id)
    )
    await session.commit()


@router.get("/blocks", response_model=list[CardOut], dependencies=[Depends(rooms_on)])
async def blocks(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[CardOut]:
    """Кого я заблокировал — чтобы было где разблокировать"""
    rows = (
        await session.execute(
            select(User)
            .join(UserBlock, UserBlock.blocked_id == User.id)
            .where(UserBlock.blocker_id == user.id)
            .order_by(UserBlock.created_at.desc())
        )
    ).scalars()
    return [
        CardOut(user_id=u.id, first_name=u.first_name, avatar_url=_avatar_url(u)) for u in rows
    ]


# MARK: - Уборка

#: Через столько после похода комната уходит в архив, а Sayr Admin — из группы:
#: впечатления пишут как раз в первые дни после
ARCHIVE_AFTER = timedelta(days=14)
#: Отказы и выходы держим полгода — дальше они никому не нужны
FORGET_AFTER = timedelta(days=180)


async def housekeeping(session: AsyncSession, day: date) -> None:
    """Раз в час из `stats.purge`: заметки о прошедших заявках, архив, уход
    из групп, старые хвосты.

    Коммитит вызывающий."""
    now = datetime.now(timezone.utc)
    # Заявки, которых уже не одобрить: поход прошёл (_require_active). Их
    # заметки в группе служба удаляет — задание на заметку, и одно: номер
    # служба забирает из строки в задание, следующий проход его не увидит.
    # Раньше архива: из группы Sayr Admin выходит уже без них
    busy = (
        select(TgJob.id)
        .where(
            TgJob.kind == "request_note",
            TgJob.member_id == RoomMember.id,
            TgJob.status == "pending",
        )
        .exists()
    )
    expired = (
        await session.execute(
            select(RoomMember.id, RoomMember.room_id)
            .join(Room, Room.id == RoomMember.room_id)
            .where(
                RoomMember.status == "requested",
                RoomMember.tg_request_message_id.is_not(None),
                Room.tg_state == "ready",
                # _end(room) < day: последний день похода позади
                Room.day + func.greatest(Room.days, 1) <= day,
                ~busy,
            )
        )
    ).all()
    for member_id, room_id in expired:
        session.add(
            TgJob(
                kind="request_note",
                room_id=room_id,
                member_id=member_id,
                payload={"expired": True},
            )
        )
    rooms = (
        await session.execute(
            select(Room).where(
                Room.status.in_(("active", "cancelled")), Room.day < day - ARCHIVE_AFTER
            )
        )
    ).scalars()
    for room in rooms:
        if _end(room) + ARCHIVE_AFTER >= day:
            continue
        room.status = "archived"
        room.archived_at = now
        if room.tg_state == "ready":
            session.add(TgJob(kind="leave", room_id=room.id, payload={"text": "farewell"}))
        elif room.tg_state == "leaving" and room.tg_left_at is None:
            # /leave так и не вывел Sayr Admin (сбои, ограничение аккаунта):
            # выходим сейчас и стираем переписку, как обещали в группе
            session.add(TgJob(kind="leave", room_id=room.id, payload={"forget": True}))
        elif room.tg_state == "failed" and room.tg_chat_id and room.tg_left_at is None:
            # Группу завели, а собрать не вышло: людей в ней нет, прощаться
            # не с кем — но и сидеть в ней Sayr Admin вечно незачем
            session.add(TgJob(kind="leave", room_id=room.id, payload={}))
    await session.execute(
        delete(RoomMember).where(
            RoomMember.status.in_(("declined", "left", "removed")),
            RoomMember.decided_at < now - FORGET_AFTER,
        )
    )
    await session.execute(delete(PushOutbox).where(PushOutbox.created_at < now - timedelta(days=7)))
    # Переписку групп храним полгода — так обещано в политике
    await session.execute(delete(TgMessage).where(TgMessage.sent_at < now - FORGET_AFTER))
    await session.execute(
        delete(TgJob).where(
            TgJob.status.in_(("done", "failed")), TgJob.done_at < now - timedelta(days=30)
        )
    )


# MARK: - Модерация


async def ban_companions(session: AsyncSession, user: User) -> None:
    """«Запретить попутчиков» по жалобе: свои походы отменяются, из чужих
    комнат и групп человека убирают. Из групп своих отменённых — тоже:
    группа остаётся тем, кто идёт всё равно, а жалоба была как раз на него.
    Отменённые до запрета — так же: их группа жива.
    Коммитит вызывающий."""
    user.companions_banned_at = datetime.now(timezone.utc)
    now = user.companions_banned_at
    rooms = (
        await session.execute(
            select(Room)
            .join(RoomMember, RoomMember.room_id == Room.id)
            .where(RoomMember.user_id == user.id, Room.status.in_(("active", "cancelled")))
            .options(
                selectinload(Room.place),
                selectinload(Room.members).selectinload(RoomMember.user),
            )
        )
    ).scalars().unique()
    for room in rooms:
        mine = _mine(room, user)
        if _role(mine) == "organizer":
            if room.status == "active":
                cancel(session, room)
            kick(session, room, mine, "removed")
        elif mine.status in LIVE:
            joined = mine.status == "joined"
            mine.status, mine.decided_at = "removed", now
            if joined:
                kick(session, room, mine, "removed")
            else:
                request_note(session, room, mine)


# MARK: - Удаление аккаунта


async def on_account_deleted(session: AsyncSession, user: User) -> None:
    """Перед удалением человека: его походы отменяются (всем пуш, в группу
    сообщение), из групп — и чужих, и своих — служба его убирает, личные
    ссылки гасит. Отменённые комнаты — тоже: их группа жива. Строки участия
    уйдут каскадом вместе с человеком — поэтому аккаунт Telegram и ссылку
    кладём в задание заранее: после удаления ссылку не с кем было бы связать"""
    rows = (
        await session.execute(
            select(Room)
            .join(RoomMember, RoomMember.room_id == Room.id)
            .where(RoomMember.user_id == user.id, Room.status.in_(("active", "cancelled")))
            .options(
                selectinload(Room.place),
                selectinload(Room.members).selectinload(RoomMember.user),
            )
        )
    ).scalars().unique()
    for room in rows:
        mine = _mine(room, user)
        if _role(mine) == "organizer" and room.status == "active":
            cancel(session, room)
        if mine.status == "joined":
            kick(session, room, mine, "deleted")
        elif mine.status == "requested":
            # Заметку о заявке — удалить; номер её уйдёт со строкой заявки
            request_note(session, room, mine, mine.tg_request_message_id)
    # Его сообщения из групп — тоже: удаление аккаунта уносит всё его
    await session.execute(delete(TgMessage).where(TgMessage.user_id == user.id))
