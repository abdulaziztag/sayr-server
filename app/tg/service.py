"""Что делает Sayr Admin: задания из `tg_jobs` и события его групп.

Всё здесь знает Telegram только через `TgApi` (api.py), поэтому
проверяется на подменном клиенте — tests/test_tg_service.py.

Задания ставит API (app/api/rooms.py): второй человек в комнате —
`create_group`, вошедший в готовую группу — `invite_link`, ушедший —
`kick`, отмена похода — `post`, архив — `leave`. Служба ставит себе сама:
`promote`, когда в группу вошёл организатор, и `forget` по его /leave.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import PHOTOS_DIR, settings
from ..models import Place, Room, RoomMember, TgJob, TgMessage, TgStatus
from ..push import outbox
from . import texts
from .api import Chat, FloodWait, Gone, Restricted, TgApi

log = logging.getLogger("sayr.tg")

#: После стольких провалов подряд задание бросаем — ошибка в админке
MAX_ATTEMPTS = 5
#: Ссылка в группу живёт до конца похода и ещё столько: в группе договариваются
#: и после, а в архив комната уходит через те же две недели
LINK_TAIL = timedelta(days=14)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _chat(room: Room) -> Chat:
    return room.tg_chat_id, room.tg_access_hash or 0


async def _room(session: AsyncSession, room_id: int | None) -> Room | None:
    if room_id is None:
        return None
    return (
        await session.execute(
            select(Room)
            .where(Room.id == room_id)
            .options(
                selectinload(Room.place).selectinload(Place.photos),
                selectinload(Room.members).selectinload(RoomMember.user),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def room_by_chat(session: AsyncSession, chat_id: int) -> Room | None:
    room = (
        await session.execute(select(Room.id).where(Room.tg_chat_id == chat_id))
    ).scalar_one_or_none()
    return await _room(session, room)


def _push_ready(session: AsyncSession, room: Room, member: RoomMember) -> None:
    outbox.enqueue(
        session,
        member.user_id,
        "group_ready",
        {
            "place": room.place.name,
            "place_uz": room.place.name_uz or "",
            "day": room.day.isoformat(),
        },
        room.code,
    )


async def _issue_link(api: TgApi, session: AsyncSession, room: Room, member: RoomMember) -> None:
    end = room.day + timedelta(days=max(1, room.days) - 1)
    expire = datetime.combine(end + LINK_TAIL, datetime.min.time(), tzinfo=timezone.utc)
    member.tg_link = await api.export_link(_chat(room), f"m{member.id}", expire)
    member.tg_link_used = False
    _push_ready(session, room, member)


# MARK: - Задания


async def _create_group(api: TgApi, session: AsyncSession, job: TgJob, room: Room) -> None:
    if room.status != "active" or room.tg_state not in ("pending", "none"):
        return
    # Не больше N групп в час: аккаунт, создающий группы пачками, выглядит
    # как спамер. Лишнее ждёт следующего часа, попытка не тратится
    recent = (
        await session.execute(
            select(func.count())
            .select_from(TgJob)
            .where(
                TgJob.kind == "create_group",
                TgJob.status == "done",
                TgJob.done_at > _now() - timedelta(hours=1),
            )
        )
    ).scalar_one()
    if recent >= settings.tg_groups_per_hour:
        raise FloodWait(600)

    place = room.place
    if room.tg_chat_id is None:
        room.tg_chat_id, room.tg_access_hash = await api.create_group(
            texts.title(place.name, room.day), texts.about(place.name, room.day, place.slug)
        )
        # Сразу в базу: упади следующий шаг — повтор не заведёт вторую группу
        await session.commit()
    chat = _chat(room)
    if place.photos:
        path = PHOTOS_DIR / place.photos[0].file.name.split("/")[-1]
        if path.exists():
            try:
                await api.set_photo(chat, path)
            except (Gone, Restricted):
                raise
            except Exception:  # noqa: BLE001 — без фото группа всё равно нужна
                log.warning("фото группы не встало", exc_info=True)
    await api.show_history(chat)
    first = await api.send(chat, texts.first_message(place.name, place.name_uz or "", room.day, place.slug))
    await api.pin(chat, first)
    room.tg_state = "ready"
    for member in room.members:
        if member.status == "joined" and not member.tg_link:
            await _issue_link(api, session, room, member)


async def _invite_link(api: TgApi, session: AsyncSession, job: TgJob, room: Room) -> None:
    member = next((m for m in room.members if m.id == job.member_id), None)
    if member is None or member.status != "joined" or room.tg_state != "ready":
        return
    await _issue_link(api, session, room, member)


async def _promote(api: TgApi, session: AsyncSession, job: TgJob, room: Room) -> None:
    member = next((m for m in room.members if m.id == job.member_id), None)
    if member is None or not member.tg_user_id or room.tg_state != "ready":
        return
    await api.promote(_chat(room), member.tg_user_id, member.tg_user_hash or 0)


async def _kick(api: TgApi, session: AsyncSession, job: TgJob, room: Room) -> None:
    user_id = job.payload.get("tg_user_id")
    if not user_id or room.tg_state != "ready":
        return
    try:
        await api.kick(_chat(room), int(user_id), int(job.payload.get("tg_user_hash") or 0))
    except Gone:
        pass  # уже вышел сам


async def _post(api: TgApi, session: AsyncSession, job: TgJob, room: Room) -> None:
    text = texts.TEXTS.get(job.payload.get("text", ""))
    if text and room.tg_state == "ready":
        await api.send(_chat(room), text)


async def _leave(api: TgApi, session: AsyncSession, job: TgJob, room: Room) -> None:
    if room.tg_state != "ready":
        return
    text = texts.TEXTS.get(job.payload.get("text", ""))
    try:
        if text:
            await api.send(_chat(room), text)
        await api.leave(_chat(room))
    except Gone:
        pass  # нас уже выгнали — считаем, что вышли
    room.tg_state = "left"
    room.tg_left_at = _now()


async def _forget(api: TgApi, session: AsyncSession, job: TgJob, room: Room) -> None:
    await session.execute(delete(TgMessage).where(TgMessage.room_id == room.id))


HANDLERS = {
    "create_group": _create_group,
    "invite_link": _invite_link,
    "promote": _promote,
    "kick": _kick,
    "post": _post,
    "leave": _leave,
    "forget": _forget,
}


async def run_job(api: TgApi, session: AsyncSession, job: TgJob) -> None:
    """Одно задание: сделать, отложить или бросить. Коммитит само."""
    room = await _room(session, job.room_id)
    try:
        if room is not None:
            await HANDLERS[job.kind](api, session, job, room)
    except FloodWait as e:
        # Пауза — не провал: попытки не тратятся
        job.status = "pending"
        job.run_after = _now() + timedelta(seconds=e.seconds + 1)
        job.last_error = str(e)
        await session.commit()
        return
    except Restricted as e:
        job.status = "failed"
        job.done_at = _now()
        job.last_error = str(e)
        if room is not None and job.kind == "create_group":
            room.tg_state = "failed"
        await heartbeat(session, "restricted", str(e))
        await session.commit()
        return
    except Gone as e:
        job.status = "done"
        job.done_at = _now()
        job.last_error = str(e)
        if room is not None and room.tg_state == "ready":
            room.tg_state = "left"
            room.tg_left_at = _now()
        await session.commit()
        return
    except Exception as e:  # noqa: BLE001 — одно задание не должно ронять службу
        log.warning("задание %s #%s не вышло", job.kind, job.id, exc_info=True)
        # Откат, а не запись поверх: обработчик мог успеть сохранить часть
        # (создание группы коммитит сразу), остальное — нет
        job_id = job.id
        await session.rollback()
        job = await session.get(TgJob, job_id, populate_existing=True)
        job.attempts += 1
        job.last_error = f"{type(e).__name__}: {e}"[:2000]
        if job.attempts >= MAX_ATTEMPTS:
            job.status, job.done_at = "failed", _now()
            if job.kind == "create_group":
                room = await session.get(Room, job.room_id)
                if room is not None and room.tg_chat_id is None:
                    room.tg_state = "failed"
        else:
            job.status = "pending"
            job.run_after = _now() + timedelta(minutes=2**job.attempts)
        await session.commit()
        return
    job.status = "done"
    job.done_at = _now()
    job.last_error = None
    await session.commit()


async def run_due(api: TgApi, session: AsyncSession, limit: int = 20) -> int:
    """Всё созревшее — по порядку. Возвращает, сколько обработано."""
    jobs = list(
        (
            await session.execute(
                select(TgJob)
                .where(TgJob.status == "pending", TgJob.run_after <= _now())
                .order_by(TgJob.id)
                .limit(limit)
            )
        ).scalars()
    )
    for job in jobs:
        await run_job(api, session, job)
    return len(jobs)


# MARK: - События групп


async def _mapped(session: AsyncSession, room: Room, member: RoomMember, user_id: int, user_hash: int) -> None:
    member.tg_user_id = user_id
    member.tg_user_hash = user_hash
    member.tg_link_used = True
    if member.role == "organizer":
        session.add(TgJob(kind="promote", room_id=room.id, member_id=member.id, payload={}))


async def on_join(
    api: TgApi,
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    user_hash: int,
    link: str | None,
) -> None:
    """Кто-то вошёл. Ссылка известна — связываем сразу; нет — сверяем
    неиспользованные ссылки комнаты через getChatInviteImporters"""
    room = await room_by_chat(session, chat_id)
    if room is None:
        return
    member = next((m for m in room.members if link and m.tg_link == link), None)
    if member is not None:
        await _mapped(session, room, member, user_id, user_hash)
        await session.commit()
        return
    await reconcile_room(api, session, room)


async def reconcile_room(api: TgApi, session: AsyncSession, room: Room) -> None:
    for member in room.members:
        if not member.tg_link or member.tg_link_used or member.status != "joined":
            continue
        try:
            importers = await api.link_importers(_chat(room), member.tg_link)
        except (Gone, FloodWait):
            return
        if importers:
            user_id, user_hash = importers[0]
            await _mapped(session, room, member, user_id, user_hash)
    await session.commit()


async def reconcile_all(api: TgApi, session: AsyncSession) -> None:
    """Раз в несколько минут: вдруг событие входа не дошло"""
    ids = (
        await session.execute(
            select(Room.id)
            .join(RoomMember, RoomMember.room_id == Room.id)
            .where(
                Room.tg_state == "ready",
                RoomMember.tg_link.is_not(None),
                RoomMember.tg_link_used.is_(False),
            )
            .distinct()
            .limit(50)
        )
    ).scalars()
    for room_id in list(ids):
        room = await _room(session, room_id)
        if room is not None:
            await reconcile_room(api, session, room)


async def on_message(
    session: AsyncSession,
    chat_id: int,
    message_id: int,
    user_id: int | None,
    text: str,
    has_media: bool,
    sent_at: datetime,
) -> None:
    room = await room_by_chat(session, chat_id)
    if room is None or room.tg_state != "ready":
        return
    member = next((m for m in room.members if user_id and m.tg_user_id == user_id), None)
    if text.strip().lower().split("@")[0] == "/leave":
        if member is not None and member.role == "organizer":
            session.add(TgJob(kind="forget", room_id=room.id, payload={}))
            session.add(TgJob(kind="leave", room_id=room.id, payload={"text": "bye"}))
            await session.commit()
        return
    exists = (
        await session.execute(
            select(TgMessage.id).where(
                TgMessage.room_id == room.id, TgMessage.tg_message_id == message_id
            )
        )
    ).scalar_one_or_none()
    if exists:
        return
    session.add(
        TgMessage(
            room_id=room.id,
            tg_message_id=message_id,
            tg_user_id=user_id,
            user_id=member.user_id if member else None,
            sent_at=sent_at,
            text=text or "",
            has_media=has_media,
        )
    )
    await session.commit()


async def on_edit(
    session: AsyncSession, chat_id: int, message_id: int, text: str, edited_at: datetime
) -> None:
    room = await room_by_chat(session, chat_id)
    if room is None:
        return
    row = (
        await session.execute(
            select(TgMessage).where(
                TgMessage.room_id == room.id, TgMessage.tg_message_id == message_id
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        row.text = text or ""
        row.edited_at = edited_at
        await session.commit()


async def on_delete(session: AsyncSession, chat_id: int, message_ids: list[int]) -> None:
    room = await room_by_chat(session, chat_id)
    if room is None or not message_ids:
        return
    await session.execute(
        delete(TgMessage).where(
            TgMessage.room_id == room.id, TgMessage.tg_message_id.in_(message_ids)
        )
    )
    await session.commit()


# MARK: - Состояние


async def heartbeat(session: AsyncSession, state: str, note: str | None = None) -> None:
    """Отметка «жив» для админки. Коммитит вызывающий, если это не
    отдельный тик: run_job зовёт её внутри своей транзакции"""
    groups = (
        await session.execute(
            select(func.count()).select_from(Room).where(Room.tg_state == "ready")
        )
    ).scalar_one()
    row = await session.get(TgStatus, 1)
    if row is None:
        row = TgStatus(id=1, state=state, last_seen_at=_now())
        session.add(row)
    # «Ограничен» не перетирается обычным «ок»: снимать тревогу — решение
    # человека в админке, а не следующего тика
    if not (row.state == "restricted" and state == "ok"):
        row.state = state
        row.note = note
    row.last_seen_at = _now()
    row.groups = groups
