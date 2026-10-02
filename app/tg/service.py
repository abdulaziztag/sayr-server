"""Что делает Sayr Admin: задания из `tg_jobs` и события его групп.

Всё здесь знает Telegram только через `TgApi` (api.py), поэтому
проверяется на подменном клиенте — tests/test_tg_service.py.

Задания ставит API (app/api/rooms.py): второй человек в комнате —
`create_group`, вошедший в готовую группу — `invite_link`, ушедший —
`kick`, отмена похода — `post`, архив — `leave`, любая перемена с заявкой
в комнату — `request_note`. Служба ставит себе сама:
`promote`, когда в группу вошёл организатор; `kick`, когда по личной ссылке
вошёл тот, кому в группе уже не место; `leave` по /leave организатора;
`invite_link` тем, кто при сверке остался без ссылки.

Статистика — событие `tg_group`: ready, когда группа собрана, joined,
когда человек на деле вошёл в неё. Пишется после коммита своей сессией
(stats.record_event), без номера устройства: у службы его нет.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import PHOTOS_DIR, settings
from ..models import Place, Room, RoomMember, TgJob, TgMessage, TgStatus, User, UserBlock
from ..push import outbox
from ..stats import record_event
from . import texts
from .api import Chat, FloodWait, Gone, MessageGone, NotParticipant, Restricted, TgApi

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


def _welcome(room: Room, member: RoomMember) -> bool:
    """Кому место в группе: вступившему, пока комната не в архиве и человеку
    не запретили попутчиков. Удалённый, вышедший, заблокированный — по своей
    старой ссылке уже не входят. Отменённая комната группу оставляет тем, кто
    идёт всё равно (texts.TEXTS["cancelled"]), — её вступившие входят"""
    return (
        member.status == "joined"
        and room.status != "archived"
        and member.user.companions_banned_at is None
    )


async def _step(session: AsyncSession, job: TgJob, **done) -> None:
    """Шаг сделан — отметка в самом задании и сразу в базу: упади следующий,
    повтор начнёт с несделанного, а не пришлёт второе первое сообщение"""
    job.payload = {**job.payload, **done}
    await session.commit()


async def _forget(session: AsyncSession, room_id: int) -> None:
    await session.execute(delete(TgMessage).where(TgMessage.room_id == room_id))


async def _issue_link(api: TgApi, session: AsyncSession, room: Room, member: RoomMember) -> None:
    end = room.day + timedelta(days=max(1, room.days) - 1)
    expire = datetime.combine(end + LINK_TAIL, datetime.min.time(), tzinfo=timezone.utc)
    member.tg_link = await api.export_link(_chat(room), f"m{member.id}", expire)
    member.tg_link_used = False
    _push_ready(session, room, member)


async def _links_for_all(api: TgApi, session: AsyncSession, room: Room) -> None:
    """Ссылку — каждому вступившему, у кого её нет. Участники — свежие из
    базы, а не из комнаты, загруженной в начале задания: пока служба заводила
    группу, в комнату могли вступить, а API, видя «создаётся», задания на
    ссылку не ставил. Каждую — сразу в базу: пауза Telegram посреди списка
    не должна стоить уже выданных, а повтор довыдаст остальным"""
    members = (
        await session.execute(
            select(RoomMember)
            .where(
                RoomMember.room_id == room.id,
                RoomMember.status == "joined",
                RoomMember.tg_link.is_(None),
            )
            .options(selectinload(RoomMember.user))
            .order_by(RoomMember.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    for member in members:
        if _welcome(room, member):
            await _issue_link(api, session, room, member)
            await session.commit()


# MARK: - Задания


async def _create_group(api: TgApi, session: AsyncSession, job: TgJob, room: Room) -> None:
    if room.tg_state == "ready":
        # Повтор после паузы посреди выдачи ссылок: группа собрана,
        # осталось довыдать тем, кому не успели
        await _links_for_all(api, session, room)
        return
    # «Не вышло» — тоже сюда: так «Повторить» в админке доводит сборку
    if room.tg_state not in ("none", "pending", "failed"):
        return
    if room.status != "active":
        # Поход отменили раньше, чем группа собралась: ссылок никто не
        # получал, людей в ней нет. Заведённую бросаем, а не сидим в ней вечно
        room.tg_state = "failed"
        if room.tg_chat_id is not None and room.tg_left_at is None:
            session.add(TgJob(kind="leave", room_id=room.id, payload={}))
        return

    place = room.place
    if room.tg_chat_id is None:
        # Не больше N групп в час: аккаунт, создающий группы пачками, выглядит
        # как спамер. Лишнее ждёт следующего часа, попытка не тратится.
        # Недособранную группу предел не держит — она уже заведена
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
        room.tg_chat_id, room.tg_access_hash = await api.create_group(
            texts.title(place.name, room.day), texts.about(place.name, room.day, place.slug)
        )
        # Сразу в базу: упади следующий шаг — повтор не заведёт вторую группу
        await session.commit()
    chat = _chat(room)
    # Остальные шаги — по одному, с отметкой в задании: повтор после сбоя
    # или паузы начинает с несделанного
    if not job.payload.get("photo"):
        if place.photos:
            path = PHOTOS_DIR / place.photos[0].file.name.split("/")[-1]
            if path.exists():
                try:
                    await api.set_photo(chat, path)
                except (Gone, Restricted, FloodWait):
                    raise
                except Exception:  # noqa: BLE001 — без фото группа всё равно нужна
                    log.warning("фото группы не встало", exc_info=True)
        await _step(session, job, photo=True)
    if not job.payload.get("history"):
        await api.show_history(chat)
        await _step(session, job, history=True)
    first = job.payload.get("first")
    if first is None:
        first = await api.send(
            chat, texts.first_message(place.name, place.name_uz or "", room.day, place.slug)
        )
        await _step(session, job, first=first)
    if not job.payload.get("pinned"):
        await api.pin(chat, first)
        await _step(session, job, pinned=True)
    # «Готова» — только собранная целиком и уже в базе, отдельно от ссылок
    room.tg_state = "ready"
    await session.commit()
    # Один раз на группу: повтор после паузы посреди ссылок идёт веткой
    # «уже готова» в начале и сюда не доходит
    await record_event("tg_group", "ready", None)
    await _links_for_all(api, session, room)


async def _invite_link(api: TgApi, session: AsyncSession, job: TgJob, room: Room) -> None:
    member = next((m for m in room.members if m.id == job.member_id), None)
    if member is None or not _welcome(room, member) or room.tg_state != "ready":
        return
    old = job.payload.get("revoke")
    if old:
        # Новая ссылка — только взамен старой, и старая гаснет
        await api.revoke_link(_chat(room), old)
    if member.tg_link and not member.tg_link_used:
        return  # живая ссылка уже есть — вторая ни к чему
    await _issue_link(api, session, room, member)


async def _promote(api: TgApi, session: AsyncSession, job: TgJob, room: Room) -> None:
    member = next((m for m in room.members if m.id == job.member_id), None)
    if member is None or not member.tg_user_id or room.tg_state != "ready":
        return
    if not _welcome(room, member):
        return  # пока ждали, его убрали — админом не делаем
    try:
        await api.promote(_chat(room), member.tg_user_id, member.tg_user_hash or 0)
    except NotParticipant:
        pass  # вошёл и сразу вышел — админом делать некого, а группа цела


async def _kick(api: TgApi, session: AsyncSession, job: TgJob, room: Room) -> None:
    """Ушёл из комнаты — уходит и из группы. Его личная ссылка гаснет, а кто
    успел по ней войти — вылетает: такого служба могла ещё не связать с
    человеком (событие входа опаздывает, сверка — раз в пять минут).
    Выгнали на деле — группе заметка, куда он делся (texts.left_note)"""
    if room.tg_chat_id is None or room.tg_state not in ("ready", "leaving"):
        return
    member = next((m for m in room.members if m.id == job.member_id), None)
    if member is not None and _welcome(room, member):
        return  # вернулся в комнату, пока задание ждало, — выгонять некого
    chat = _chat(room)
    # Что знало API, ставя задание, и что служба узнала с тех пор. Строки
    # участия может уже не быть — аккаунт удалён, тогда всё в задании
    known = [(job.payload.get("tg_user_id"), job.payload.get("tg_user_hash"))]
    links = {job.payload.get("tg_link")}
    if member is not None:
        known.append((member.tg_user_id, member.tg_user_hash))
        links.add(member.tg_link)
    kicked: set[int] = set()

    async def out(accounts: list[tuple]) -> None:
        best: dict[int, int] = {}
        for user_id, user_hash in accounts:
            if user_id and int(user_id) not in kicked and not best.get(int(user_id)):
                best[int(user_id)] = int(user_hash or 0)
        for user_id, user_hash in best.items():
            try:
                await api.kick(chat, user_id, user_hash)
            except NotParticipant:
                pass  # уже вышел сам
            else:
                if not job.payload.get("removed"):
                    # Отметка сразу: повтор после сбоя выгонять уже некого
                    # (NotParticipant), а заметку он должен написать
                    await _step(session, job, removed=True)
            kicked.add(user_id)

    # Сначала — уже известных: выгнать вошедшего важнее, чем погасить
    # ссылку, и сбой гашения не должен оставить его в группе
    await out(known)
    revoked = list(job.payload.get("revoked", []))
    for link in sorted(links - {None}):
        if link not in revoked:
            await api.revoke_link(chat, link)
            # Отметка в задании: пауза на списке вошедших не погасит второй раз
            revoked.append(link)
            await _step(session, job, revoked=revoked)
        await out(await api.link_importers(chat, link))
    if member is not None:
        member.tg_link = None  # погашена
    # Заметка — только выгнанному на деле: вышедший из группы сам или так и
    # не вошедший в неё вопроса «куда он делся» не вызывает. Заданиям без
    # причины её нет, удалившему аккаунт — тоже, даже если из комнаты он
    # вышел раньше: строка участия ушла вместе с аккаунтом. Иначе её не
    # станет лишь через полгода (rooms.FORGET_AFTER), а столько задание не ждёт
    reason = job.payload.get("reason")
    if (
        member is not None
        and job.payload.get("removed")
        and not job.payload.get("said")
        and reason in ("left", "removed")
        and room.tg_state == "ready"
    ):
        name, gender = job.payload.get("name"), job.payload.get("gender")
        await api.send(chat, texts.left_note(reason, name, gender))
        await _step(session, job, said=True)


async def _notes_left(session: AsyncSession, room: Room) -> bool:
    """Предел заметок о заявках на группу за час ещё не выбран. Считаются
    только написанные: пропущенная сверх предела его не продлевает"""
    recent = (
        await session.execute(
            select(func.count())
            .select_from(TgJob)
            .where(
                TgJob.kind == "request_note",
                TgJob.room_id == room.id,
                TgJob.payload["posted"].as_integer().is_not(None),
                TgJob.done_at > _now() - timedelta(hours=1),
            )
        )
    ).scalar_one()
    return recent < settings.tg_request_notes_per_hour


async def _hidden(session: AsyncSession, room: Room, member: RoomMember) -> bool:
    """У просящегося блокировка с кем-то из вступивших — в любую сторону.
    Заявку такой блок не останавливает (rooms.room_request), а в приложении
    заблокированные друг друга не видят: заметка показала бы его имя тому,
    от кого оно скрыто. Организатору — только пуш, как сверх предела"""
    joined = [m.user_id for m in room.members if m.status == "joined"]
    found = await session.execute(
        select(UserBlock.blocker_id)
        .where(
            or_(
                and_(UserBlock.blocker_id == member.user_id, UserBlock.blocked_id.in_(joined)),
                and_(UserBlock.blocked_id == member.user_id, UserBlock.blocker_id.in_(joined)),
            )
        )
        .limit(1)
    )
    return found.first() is not None


async def _request_note(api: TgApi, session: AsyncSession, job: TgJob, room: Room) -> None:
    """Заметка о заявке — по тому, что с заявкой сейчас, а не когда ставили
    задание: живая — объявить, одобренная — поправить на «теперь в походе»,
    остальные — удалить. Так порядок заданий и паузы Telegram не важны:
    одобрили раньше, чем служба написала, — она и не напишет"""
    if room.tg_state != "ready":
        return  # группа ещё собирается или Sayr Admin в ней уже нет
    member = next((m for m in room.members if m.id == job.member_id), None)
    chat = _chat(room)
    # Объявлять — только заявку, которую ещё можно одобрить: поход прошёл —
    # уже нет (rooms.housekeeping). И только ту, чьё имя никому из вступивших
    # не скрыто блоком: висящую такую заметку служба удаляет — блок или
    # вступление того, с кем он, ставят задание (rooms.block, rooms._hide_notes)
    live = (
        member is not None
        and member.status == "requested"
        and not job.payload.get("expired")
        and not await _hidden(session, room, member)
    )
    note = job.payload.get("note")
    if note is None:
        if member is None:
            return
        if member.tg_request_message_id is None:
            if live and await _notes_left(session, room):
                posted = await api.send(
                    chat, texts.request_note(member.user.first_name, room.code)
                )
                member.tg_request_message_id = posted
                await _step(session, job, posted=posted)
            return
        if live:
            return  # ждёт решения — заметка висит
        # Номер — из строки в задание, одной записью: повтор после сбоя
        # найдёт его здесь, а уборка не возьмётся за ту же заметку снова
        note, member.tg_request_message_id = member.tg_request_message_id, None
        await _step(session, job, note=note)
    await _settle(api, room, member, note)


async def _settle(api: TgApi, room: Room, member: RoomMember | None, note: int) -> None:
    """Заметку о заявке, решения по которой группе больше не ждать:
    одобренному — поправить на «теперь в походе», остальным — удалить"""
    try:
        if member is not None and member.status == "joined":
            await api.edit(_chat(room), note, texts.request_approved(member.user.first_name))
        else:
            # Отказ, отзыв, отмена, прошедший поход, блок, уход Sayr Admin —
            # молча: о том, что человека не взяли, группе знать незачем
            await api.delete(_chat(room), note)
    except MessageGone as e:
        # Заметку удалил организатор или у Sayr Admin отобрали права —
        # повтор ничего не изменит
        log.info("заметку %s о заявке в комнате %s не тронуть: %s", note, room.code, e)


async def _post(api: TgApi, session: AsyncSession, job: TgJob, room: Room) -> None:
    text = texts.TEXTS.get(job.payload.get("text", ""))
    if text and room.tg_state == "ready":
        await api.send(_chat(room), text)


async def _quit(api: TgApi, session: AsyncSession, job: TgJob, room: Room) -> None:
    if room.tg_chat_id is None or room.tg_left_at is not None:
        return
    if room.tg_state not in ("ready", "leaving", "failed"):
        return
    chat = _chat(room)
    text = texts.TEXTS.get(job.payload.get("text", ""))
    try:
        if room.status != "archived":
            # Ссылки тех, кому в группе уже не место, гасим, пока мы ещё в ней:
            # после выхода служба их не погасит, а задание на выход для
            # удалённого могло встать в очередь позже нашего. В архиве все
            # ссылки и так истекли (LINK_TAIL)
            for member in room.members:
                if member.tg_link and not member.tg_link_used and not _welcome(room, member):
                    await api.revoke_link(chat, member.tg_link)
                    member.tg_link = None
                    await session.commit()
        # Заметки о заявках — тоже, пока мы в группе: после выхода их уже не
        # поправить, и «просится» со ссылкой висела бы и после решения. Задания
        # на них служба после /leave не выполняет — Sayr Admin уже «уходит»
        for member in room.members:
            if member.tg_request_message_id is not None:
                await _settle(api, room, member, member.tg_request_message_id)
                member.tg_request_message_id = None
                await session.commit()
        if text and not job.payload.get("said"):
            await api.send(chat, text)
            await _step(session, job, said=True)
        await api.leave(chat)
    except (Gone, NotParticipant):
        pass  # нас уже выгнали — считаем, что вышли
    # Недособранная так и остаётся «не вышло»: людей в ней не было, и «Sayr
    # вышел — ссылку даст организатор» было бы неправдой
    if room.tg_state != "failed":
        room.tg_state = "left"
    room.tg_left_at = _now()


async def _leave(api: TgApi, session: AsyncSession, job: TgJob, room: Room) -> None:
    """Выйти: по сроку, по /leave организатора или из группы, которую так
    и не собрали"""
    await _quit(api, session, job, room)
    if job.payload.get("forget"):
        # По /leave: стираем, уже выйдя, — со всем, что успело прийти до выхода.
        # И когда вышли раньше, по сроку (прощание встало в очередь до /leave):
        # стереть обещано в группе
        await _forget(session, room.id)


HANDLERS = {
    "create_group": _create_group,
    "invite_link": _invite_link,
    "promote": _promote,
    "kick": _kick,
    "post": _post,
    "leave": _leave,
    "request_note": _request_note,
}


async def _rolled_back(session: AsyncSession, job: TgJob) -> tuple[TgJob, Room | None]:
    """Откат, а не запись поверх: обработчик мог успеть сохранить часть
    (создание группы коммитит по шагам), а недоделанное — нет. Задание
    и комнату перечитываем уже из базы"""
    job_id, room_id = job.id, job.room_id
    await session.rollback()
    job = await session.get(TgJob, job_id, populate_existing=True)
    room = await session.get(Room, room_id, populate_existing=True) if room_id else None
    return job, room


async def run_job(api: TgApi, session: AsyncSession, job: TgJob) -> None:
    """Одно задание: сделать, отложить или бросить. Коммитит само."""
    room = await _room(session, job.room_id)
    try:
        if room is not None:
            await HANDLERS[job.kind](api, session, job, room)
    except FloodWait as e:
        # Пауза — не провал: попытки не тратятся. Недоделанное откатываем, как
        # и при ошибке: иначе повтор увидел бы, например, «готова» без ссылок
        job, room = await _rolled_back(session, job)
        job.status = "pending"
        job.run_after = _now() + timedelta(seconds=e.seconds + 1)
        job.last_error = str(e)
        await session.commit()
        return
    except Restricted as e:
        job, room = await _rolled_back(session, job)
        job.status = "failed"
        job.done_at = _now()
        job.last_error = str(e)
        # Собранная группа с людьми остаётся: не создаются только новые
        if room is not None and job.kind == "create_group" and room.tg_state != "ready":
            room.tg_state = "failed"
        if job.kind == "leave" and job.payload.get("forget"):
            # Выйти не дают, а стереть переписку обещали — стираем. Выйдет
            # уборка, когда комната уйдёт в архив
            await _forget(session, job.room_id)
        await heartbeat(session, "restricted", str(e))
        await session.commit()
        return
    except Gone as e:
        job, room = await _rolled_back(session, job)
        job.status = "done"
        job.done_at = _now()
        job.last_error = str(e)
        if room is not None and room.tg_state == "ready":
            room.tg_state = "left"
            room.tg_left_at = _now()
        elif room is not None and job.kind == "create_group":
            # Группа пропала, не собравшись: «не вышло», а не вечное «создаётся»
            room.tg_state = "failed"
            room.tg_left_at = room.tg_left_at or _now()
        await session.commit()
        return
    except Exception as e:  # noqa: BLE001 — одно задание не должно ронять службу
        log.warning("задание %s #%s не вышло", job.kind, job.id, exc_info=True)
        job, room = await _rolled_back(session, job)
        job.attempts += 1
        job.last_error = f"{type(e).__name__}: {e}"[:2000]
        if job.attempts >= MAX_ATTEMPTS:
            job.status, job.done_at = "failed", _now()
            unbuilt = room is not None and room.tg_state in ("none", "pending")
            if job.kind == "create_group" and unbuilt:
                # Сборка так и не удалась — «не вышло», даже если группу успели
                # завести: иначе комната вечно «создаётся», а уборка из такой
                # группы не выходит. Теперь выйдет — при уходе комнаты в архив
                room.tg_state = "failed"
            if job.kind == "leave" and job.payload.get("forget"):
                # Выйти не удалось, а стереть переписку обещали — стираем
                await _forget(session, job.room_id)
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
    ids = list(
        (
            await session.execute(
                select(TgJob.id)
                .where(TgJob.status == "pending", TgJob.run_after <= _now())
                .order_by(TgJob.id)
                .limit(limit)
            )
        ).scalars()
    )
    for job_id in ids:
        # Каждое — свежим из базы: откат после паузы Telegram в прошлом
        # задании сбрасывает всё, что держала сессия, и заранее загруженные
        # задания было бы уже не прочесть
        job = await session.get(TgJob, job_id, populate_existing=True)
        if job is not None and job.status == "pending":
            await run_job(api, session, job)
    return len(ids)


# MARK: - События групп


async def _admit(
    session: AsyncSession, room: Room, member: RoomMember, user_id: int, user_hash: int
) -> bool:
    """Вошёл по личной ссылке — аккаунт его. Место в группе есть — только
    связываем; нет (удалили или вышел, пока ссылка лежала неоткрытой) —
    выгоняем, а не пускаем ко всем никам.

    Да — человек вошёл в группу на деле, для статистики: место есть,
    а ссылка до того не была использована. Повтор того же события
    о входе второго входа не делает"""
    entered = not member.tg_link_used
    member.tg_user_id = user_id
    member.tg_user_hash = user_hash
    member.tg_link_used = True
    if not _welcome(room, member):
        # Задание на выход, поставленное API, ещё ждёт — аккаунт оно возьмёт
        # из строки участия, второе не нужно
        waiting = (
            await session.execute(
                select(TgJob.id).where(
                    TgJob.kind == "kick", TgJob.member_id == member.id, TgJob.status == "pending"
                )
            )
        ).first()
        if waiting is None:
            session.add(
                TgJob(
                    kind="kick",
                    room_id=room.id,
                    member_id=member.id,
                    payload={
                        "tg_user_id": user_id,
                        "tg_user_hash": user_hash,
                        "tg_link": member.tg_link,
                        # Заметка — как при уходе: группа видела, что он вошёл
                        "reason": "left" if member.status == "left" else "removed",
                        **texts.person(member.user),
                    },
                )
            )
        return False
    if member.role == "organizer":
        session.add(TgJob(kind="promote", room_id=room.id, member_id=member.id, payload={}))
    return entered


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
        entered = await _admit(session, room, member, user_id, user_hash)
        await session.commit()
        await _count_entered(1 if entered else 0)
        return
    await reconcile_room(api, session, room)


async def _count_entered(people: int) -> None:
    """Статистика: сколько человек вошли в группу на деле. После коммита —
    своей сессией (stats._record): сбой записи службу не держит. Номера
    устройства у службы нет — событие без него"""
    for _ in range(people):
        await record_event("tg_group", "joined", None)


async def reconcile_room(api: TgApi, session: AsyncSession, room: Room) -> None:
    """Неиспользованные ссылки — у всех, а не только у вступивших: по ссылке
    удалённого, если её ещё не погасили, тоже могли войти"""
    entered = 0
    for member in room.members:
        if not member.tg_link or member.tg_link_used:
            continue
        try:
            importers = await api.link_importers(_chat(room), member.tg_link)
        except (Gone, FloodWait):
            break  # остальных — в следующий раз; уже сверенных не теряем
        if importers:
            user_id, user_hash = importers[0]
            if await _admit(session, room, member, user_id, user_hash):
                entered += 1
    await session.commit()
    await _count_entered(entered)


async def _stranded(session: AsyncSession) -> None:
    """Вступившие в готовую группу без ссылки и без задания на неё — так и
    видели бы «Группа создаётся…». Случается, если вступили в ту самую минуту,
    когда группа собиралась, или задание на ссылку бросили после сбоев. Раз
    в час, не чаще: иначе сломанная выдача ставила бы задания без конца"""
    busy = (
        select(TgJob.id)
        .where(
            TgJob.kind == "invite_link",
            TgJob.member_id == RoomMember.id,
            (TgJob.status == "pending") | (TgJob.created_at > _now() - timedelta(hours=1)),
        )
        .exists()
    )
    rows = (
        await session.execute(
            select(RoomMember.id, RoomMember.room_id)
            .join(Room, Room.id == RoomMember.room_id)
            .join(User, User.id == RoomMember.user_id)
            .where(
                Room.tg_state == "ready",
                Room.status != "archived",
                RoomMember.status == "joined",
                RoomMember.tg_link.is_(None),
                User.companions_banned_at.is_(None),
                ~busy,
            )
            .limit(50)
        )
    ).all()
    for member_id, room_id in rows:
        session.add(TgJob(kind="invite_link", room_id=room_id, member_id=member_id, payload={}))
    await session.commit()


async def reconcile_all(api: TgApi, session: AsyncSession) -> None:
    """Раз в несколько минут: вдруг событие входа не дошло или кто-то остался
    без ссылки"""
    await _stranded(session)
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
            # «Уходит» сразу: с этой минуты переписку не храним, даже если
            # выйти мешает пауза Telegram, — в группе уже обещано, что всё
            # удалено. Стирает задание, когда вышли
            room.tg_state = "leaving"
            session.add(
                TgJob(kind="leave", room_id=room.id, payload={"text": "bye", "forget": True})
            )
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
