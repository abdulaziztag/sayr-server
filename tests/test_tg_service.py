"""Sayr Admin на подменном клиенте: создание группы, личные ссылки, вход
по ссылке, админ-организатор, удаление из группы, переписка, /leave,
паузы Telegram и ограничение аккаунта; гашение ссылок ушедших, застрявшие
без ссылки люди, недособранные группы и недовыполненный /leave.

Настоящий Telegram здесь не нужен: логика знает его только через TgApi.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.api import rooms as rooms_api
from app.config import settings
from app.db import SessionLocal
from app.models import (
    Place,
    PushOutbox,
    Room,
    RoomMember,
    TgJob,
    TgMessage,
    TgStatus,
    User,
    UserSession,
)
from app.tg import service
from app.tg.api import FloodWait, Gone, NotParticipant, Restricted, TelethonApi

NOW = datetime.now(timezone.utc)
DAY = rooms_api.today() + timedelta(days=5)


class FakeApi:
    """Telegram понарошку: помнит, что с ним делали"""

    def __init__(self):
        self.calls: list[tuple] = []
        self.importers: dict[str, list[tuple[int, int]]] = {}
        self.fail: Exception | None = None
        self.links = 0

    async def _step(self, *call):
        self.calls.append(call)
        if self.fail:
            raise self.fail

    async def create_group(self, title, about):
        await self._step("create_group", title)
        return 777, 999

    async def set_photo(self, chat, path):
        await self._step("set_photo", chat)

    async def show_history(self, chat):
        await self._step("show_history", chat)

    async def send(self, chat, text):
        await self._step("send", chat, text)
        return 42

    async def pin(self, chat, message_id):
        await self._step("pin", chat, message_id)

    async def export_link(self, chat, title, expire):
        await self._step("export_link", chat, title)
        self.links += 1
        return f"https://t.me/+link{self.links}"

    async def revoke_link(self, chat, link):
        await self._step("revoke_link", chat, link)

    async def link_importers(self, chat, link):
        await self._step("link_importers", link)
        return self.importers.get(link, [])

    async def promote(self, chat, user_id, user_hash):
        await self._step("promote", chat, user_id, user_hash)

    async def kick(self, chat, user_id, user_hash):
        await self._step("kick", chat, user_id, user_hash)

    async def leave(self, chat):
        await self._step("leave", chat)


@pytest.fixture(autouse=True)
async def clean():
    yield
    async with SessionLocal() as session:
        for model in (TgMessage, TgJob, PushOutbox, RoomMember, Room, TgStatus, UserSession, User):
            await session.execute(delete(model))
        await session.commit()


async def room_with(people: int = 2, state: str = "pending") -> tuple[int, list[int]]:
    """Комната на тестовом пике: организатор и ещё people-1 участник"""
    async with SessionLocal() as session:
        place = (await session.execute(select(Place).where(Place.slug == "test-peak"))).scalar_one()
        users = [User(phone=f"+99890000{i:04d}", first_name=f"Человек{i}") for i in range(people)]
        session.add_all(users)
        await session.flush()
        room = Room(
            code=f"c{users[0].id}", invite=f"i{users[0].id}", place_id=place.id,
            organizer_id=users[0].id, day=DAY, tg_state=state,
        )
        session.add(room)
        await session.flush()
        members = []
        for i, u in enumerate(users):
            m = RoomMember(
                room_id=room.id, user_id=u.id, role="organizer" if i == 0 else "member",
                status="joined", source="organizer" if i == 0 else "link",
            )
            session.add(m)
            members.append(m)
        await session.flush()
        if state == "pending":
            session.add(TgJob(kind="create_group", room_id=room.id, payload={}))
        await session.commit()
        return room.id, [m.id for m in members]


async def get(model, id_):
    async with SessionLocal() as session:
        return await session.get(model, id_)


async def run(api) -> int:
    async with SessionLocal() as session:
        return await service.run_due(api, session)


async def rerun(api) -> int:
    """Отложенное — созревшим, и ещё проход"""
    async with SessionLocal() as session:
        waiting = await session.execute(select(TgJob).where(TgJob.status == "pending"))
        for job in waiting.scalars():
            job.run_after = NOW - timedelta(seconds=1)
        await session.commit()
    return await run(api)


async def drop(member_id: int, status: str = "removed") -> None:
    """Человек ушёл из комнаты так, как это делает API: статус и задание"""
    async with SessionLocal() as session:
        member = await session.get(RoomMember, member_id)
        room = await service._room(session, member.room_id)
        member = next(m for m in room.members if m.id == member_id)
        member.status = status
        rooms_api.kick(session, room, member)
        await session.commit()


async def job_of(kind: str) -> TgJob:
    async with SessionLocal() as session:
        return (await session.execute(select(TgJob).where(TgJob.kind == kind))).scalar_one()


async def all_jobs() -> list[TgJob]:
    async with SessionLocal() as session:
        return list((await session.execute(select(TgJob))).scalars())


async def test_группа_заводится_и_всем_приходят_ссылки():
    room_id, members = await room_with(3)
    api = FakeApi()
    assert await run(api) == 1

    room = await get(Room, room_id)
    assert room.tg_state == "ready" and (room.tg_chat_id, room.tg_access_hash) == (777, 999)
    kinds = [c[0] for c in api.calls]
    assert kinds[:4] == ["create_group", "show_history", "send", "pin"]
    assert api.calls[0][1] == f"Sayr · Тестовый пик · {DAY:%d.%m}"
    first = next(c for c in api.calls if c[0] == "send")[2]
    assert "/leave" in first and "Sayr Admin" in first
    # Каждому участнику — своя одноразовая ссылка с его номером в названии
    titles = sorted(c[2] for c in api.calls if c[0] == "export_link")
    assert titles == sorted(f"m{m}" for m in members)
    async with SessionLocal() as session:
        pushed = (await session.execute(select(PushOutbox.kind))).scalars().all()
    assert pushed == ["group_ready"] * 3


async def test_повтор_не_заводит_вторую_группу():
    room_id, _ = await room_with()
    api = FakeApi()
    # Группа создалась, а на отправке первого сообщения связь оборвалась
    original = api.send

    async def broken(chat, text):
        raise ConnectionError("обрыв")

    api.send = broken
    await run(api)
    async with SessionLocal() as session:
        job = (await session.execute(select(TgJob))).scalar_one()
        assert job.status == "pending" and job.attempts == 1
        job.run_after = NOW - timedelta(seconds=1)
        await session.commit()
    api.send = original
    await run(api)
    assert [c[0] for c in api.calls].count("create_group") == 1
    assert (await get(Room, room_id)).tg_state == "ready"


async def test_не_больше_групп_в_час(monkeypatch):
    monkeypatch.setattr(settings, "tg_groups_per_hour", 0)
    room_id, _ = await room_with()
    await run(FakeApi())
    async with SessionLocal() as session:
        job = (await session.execute(select(TgJob))).scalar_one()
    assert job.status == "pending" and job.run_after > NOW + timedelta(minutes=5)
    assert job.attempts == 0
    assert (await get(Room, room_id)).tg_state == "pending"


async def test_пауза_телеграма_откладывает():
    await room_with()
    api = FakeApi()
    api.fail = FloodWait(120)
    await run(api)
    async with SessionLocal() as session:
        job = (await session.execute(select(TgJob))).scalar_one()
    assert job.status == "pending" and job.attempts == 0
    assert job.run_after > NOW + timedelta(seconds=100)


async def test_пауза_одного_задания_не_держит_остальные():
    """Откат после паузы сбрасывает всё, что держала сессия: задания, загруженные
    заранее, служба прочесть уже не могла, и остаток пачки ждал следующего тика"""
    room_id, members, api = await ready_room()
    async with SessionLocal() as session:
        (await session.get(RoomMember, members[1])).tg_link = None
        session.add(TgJob(kind="post", room_id=room_id, payload={"text": "cancelled"}))
        session.add(TgJob(kind="invite_link", room_id=room_id, member_id=members[1], payload={}))
        await session.commit()

    async def flood(chat, text):
        raise FloodWait(30)

    api.send = flood
    assert await run(api) == 2
    # Сообщение ждёт паузы, а ссылка ушла в том же проходе
    assert (await job_of("post")).status == "pending"
    assert (await job_of("invite_link")).status == "done"
    assert (await get(RoomMember, members[1])).tg_link


async def test_ограниченный_аккаунт_поднимает_тревогу():
    room_id, _ = await room_with()
    api = FakeApi()
    api.fail = Restricted("UserRestrictedError")
    await run(api)
    assert (await get(Room, room_id)).tg_state == "failed"
    status = await get(TgStatus, 1)
    assert status.state == "restricted"
    async with SessionLocal() as session:
        await service.heartbeat(session, "ok")
        await session.commit()
    # Обычный тик тревогу не снимает — это решение человека в админке
    assert (await get(TgStatus, 1)).state == "restricted"


async def ready_room(people: int = 2) -> tuple[int, list[int], FakeApi]:
    room_id, members = await room_with(people)
    api = FakeApi()
    await run(api)
    return room_id, members, api


async def test_вход_по_ссылке_связывает_и_организатор_становится_админом():
    room_id, members, api = await ready_room()
    organizer = await get(RoomMember, members[0])
    async with SessionLocal() as session:
        await service.on_join(api, session, 777, 5001, 6001, organizer.tg_link)
    organizer = await get(RoomMember, members[0])
    assert (organizer.tg_user_id, organizer.tg_user_hash, organizer.tg_link_used) == (5001, 6001, True)
    await run(api)
    assert ("promote", (777, 999), 5001, 6001) in api.calls


async def test_вышедший_из_группы_не_закрывает_её_службе():
    """Организатор вошёл и сразу вышел, а назначить его админом Telegram не
    даёт: это про человека, не про группу. Раньше комната становилась «Sayr
    вышел», и ссылки удалённых после этого больше не гасились"""
    room_id, members, api = await ready_room(3)
    organizer = await get(RoomMember, members[0])
    async with SessionLocal() as session:
        await service.on_join(api, session, 777, 5001, 6001, organizer.tg_link)

    async def absent(chat, user_id, user_hash):
        raise NotParticipant("UserNotParticipantError")

    api.promote = absent
    await run(api)
    assert (await job_of("promote")).status == "done"
    assert (await get(Room, room_id)).tg_state == "ready"
    link = (await get(RoomMember, members[1])).tg_link
    await drop(members[1])
    await run(api)
    assert ("revoke_link", (777, 999), link) in api.calls


async def test_вход_без_ссылки_сверяется_по_списку_вошедших():
    room_id, members, api = await ready_room()
    member = await get(RoomMember, members[1])
    api.importers[member.tg_link] = [(5002, 6002)]
    async with SessionLocal() as session:
        await service.on_join(api, session, 777, 5002, 0, None)
    member = await get(RoomMember, members[1])
    assert member.tg_user_id == 5002 and member.tg_link_used
    # Не организатор — админом не делаем
    await run(api)
    assert not any(c[0] == "promote" for c in api.calls)


async def test_ушедшего_убирают_из_группы():
    room_id, members, api = await ready_room()
    async with SessionLocal() as session:
        session.add(
            TgJob(kind="kick", room_id=room_id, payload={"tg_user_id": 5002, "tg_user_hash": 6002})
        )
        await session.commit()
    await run(api)
    assert ("kick", (777, 999), 5002, 6002) in api.calls


async def test_переписка_сохраняется_правится_и_удаляется():
    room_id, members, api = await ready_room()
    member = await get(RoomMember, members[1])
    async with SessionLocal() as session:
        await service.on_join(api, session, 777, 5002, 6002, member.tg_link)
        await service.on_message(session, 777, 10, 5002, "Тропа размыта после моста", False, NOW)
        await service.on_message(session, 777, 11, 9999, "", True, NOW)
        # Повтор того же сообщения не задваивает
        await service.on_message(session, 777, 10, 5002, "Тропа размыта после моста", False, NOW)
    async with SessionLocal() as session:
        rows = (await session.execute(select(TgMessage).order_by(TgMessage.tg_message_id))).scalars().all()
    assert [(r.tg_message_id, r.user_id, r.has_media) for r in rows] == [
        (10, member.user_id, False),
        (11, None, True),
    ]
    async with SessionLocal() as session:
        await service.on_edit(session, 777, 10, "Тропа размыта, обход слева", NOW)
        await service.on_delete(session, 777, [11])
    async with SessionLocal() as session:
        rows = (await session.execute(select(TgMessage))).scalars().all()
    assert [r.text for r in rows] == ["Тропа размыта, обход слева"]


async def test_leave_работает_только_от_организатора_и_стирает_переписку():
    room_id, members, api = await ready_room()
    organizer = await get(RoomMember, members[0])
    member = await get(RoomMember, members[1])
    async with SessionLocal() as session:
        await service.on_join(api, session, 777, 5001, 6001, organizer.tg_link)
        await service.on_join(api, session, 777, 5002, 6002, member.tg_link)
        await service.on_message(session, 777, 10, 5002, "Встречаемся в 6:00", False, NOW)
        # Участник не может выгнать Sayr Admin
        await service.on_message(session, 777, 11, 5002, "/leave", False, NOW)
    await run(api)
    assert (await get(Room, room_id)).tg_state == "ready"

    async with SessionLocal() as session:
        await service.on_message(session, 777, 12, 5001, "/leave@SayrAdmin", False, NOW)
    await run(api)
    assert (await get(Room, room_id)).tg_state == "left"
    assert ("leave", (777, 999)) in api.calls
    bye = [c[2] for c in api.calls if c[0] == "send"][-1]
    assert "удалено" in bye
    async with SessionLocal() as session:
        assert (await session.execute(select(TgMessage))).scalars().all() == []


async def test_удаление_аккаунта_стирает_его_сообщения():
    room_id, members, api = await ready_room()
    member = await get(RoomMember, members[1])
    async with SessionLocal() as session:
        await service.on_join(api, session, 777, 5002, 6002, member.tg_link)
        await service.on_message(session, 777, 10, 5002, "моё сообщение", False, NOW)
        user = await session.get(User, member.user_id)
        await rooms_api.on_account_deleted(session, user)
        await session.commit()
    async with SessionLocal() as session:
        assert (await session.execute(select(TgMessage))).scalars().all() == []
        kicks = (await session.execute(select(TgJob).where(TgJob.kind == "kick"))).scalars().all()
    assert kicks and kicks[0].payload == {
        "tg_user_id": 5002, "tg_user_hash": 6002, "tg_link": member.tg_link,
    }


async def test_старая_переписка_уходит_через_полгода():
    room_id, members, api = await ready_room()
    async with SessionLocal() as session:
        await service.on_message(session, 777, 10, 1, "давно", False, NOW - timedelta(days=181))
        await service.on_message(session, 777, 11, 1, "недавно", False, NOW - timedelta(days=10))
        await rooms_api.housekeeping(session, rooms_api.today())
        await session.commit()
        rows = (await session.execute(select(TgMessage.text))).scalars().all()
    assert rows == ["недавно"]


async def test_разделы_sayr_admin_открываются_в_админке(admin_client):
    room_id, members, api = await ready_room()
    async with SessionLocal() as session:
        await service.on_message(session, 777, 10, 1, "Тропа сухая", False, NOW)
        await service.heartbeat(session, "ok")
        await session.commit()
    for path in ("/admin/tg-status/list", "/admin/tg-job/list", "/admin/tg-message/list"):
        resp = await admin_client.get(path)
        assert resp.status_code == 200, path
    assert "Тропа сухая" in (await admin_client.get("/admin/tg-message/list")).text


# --- Ушедшие не возвращаются по старой ссылке -------------------------------


async def test_удалённый_до_входа_теряет_ссылку():
    room_id, members, api = await ready_room()
    link = (await get(RoomMember, members[1])).tg_link
    # Убрали раньше, чем он открыл свою ссылку: аккаунт службе ещё не известен
    await drop(members[1])
    await run(api)
    assert ("revoke_link", (777, 999), link) in api.calls
    assert not any(c[0] == "kick" for c in api.calls)
    assert (await get(RoomMember, members[1])).tg_link is None


async def test_вошедший_по_ссылке_после_ухода_вылетает():
    room_id, members, api = await ready_room()
    link = (await get(RoomMember, members[1])).tg_link
    await drop(members[1], "left")
    # Ссылку он открыл раньше, чем служба её погасила
    async with SessionLocal() as session:
        await service.on_join(api, session, 777, 5002, 6002, link)
    # Выгонит то же задание, что поставило API: аккаунт теперь известен
    assert [j.kind for j in await all_jobs()].count("kick") == 1
    await run(api)
    assert api.calls.count(("kick", (777, 999), 5002, 6002)) == 1
    assert not any(c[0] == "promote" for c in api.calls)


async def test_вход_удалённого_без_события_находит_гашение_ссылки():
    room_id, members, api = await ready_room()
    link = (await get(RoomMember, members[1])).tg_link
    await drop(members[1])
    # Событие входа не дошло: кто успел войти, видно по списку вошедших
    api.importers[link] = [(5002, 6002)]
    await run(api)
    kinds = [c[0] for c in api.calls]
    assert kinds.index("revoke_link") < kinds.index("kick")
    assert ("kick", (777, 999), 5002, 6002) in api.calls


async def test_известного_выгоняют_раньше_гашения_и_дважды_не_гасят():
    room_id, members, api = await ready_room()
    member = await get(RoomMember, members[1])
    async with SessionLocal() as session:
        await service.on_join(api, session, 777, 5002, 6002, member.tg_link)
    await drop(members[1])
    revoke, importers = api.revoke_link, api.link_importers

    async def broken(chat, link):
        raise ConnectionError("обрыв")

    # Погасить ссылку не выходит — а уже вошедший всё равно вылетает
    api.revoke_link = broken
    await run(api)
    assert ("kick", (777, 999), 5002, 6002) in api.calls

    async def flood(chat, link):
        raise FloodWait(30)

    # Погасили, а на списке вошедших — пауза: повтор второй раз не гасит
    api.revoke_link, api.link_importers = revoke, flood
    await rerun(api)
    api.link_importers = importers
    await rerun(api)
    assert (await job_of("kick")).status == "done"
    assert [c[0] for c in api.calls].count("revoke_link") == 1


async def test_сверка_выгоняет_вошедшего_после_удаления():
    room_id, members, api = await ready_room()
    link = (await get(RoomMember, members[1])).tg_link
    async with SessionLocal() as session:
        (await session.get(RoomMember, members[1])).status = "removed"
        await session.commit()
    api.importers[link] = [(5002, 6002)]
    async with SessionLocal() as session:
        await service.reconcile_all(api, session)
    assert (await job_of("kick")).payload["tg_user_id"] == 5002
    await run(api)
    assert ("kick", (777, 999), 5002, 6002) in api.calls


async def test_удаление_аккаунта_гасит_ссылку_и_выгоняет():
    room_id, members, api = await ready_room(3)
    joined = await get(RoomMember, members[1])
    fresh = await get(RoomMember, members[2])
    async with SessionLocal() as session:
        await service.on_join(api, session, 777, 5002, 6002, joined.tg_link)
        for m in (joined, fresh):
            user = await session.get(User, m.user_id)
            await rooms_api.on_account_deleted(session, user)
            await session.delete(user)
        await session.commit()
    await run(api)
    assert ("revoke_link", (777, 999), fresh.tg_link) in api.calls
    assert ("kick", (777, 999), 5002, 6002) in api.calls


# --- Никто не остаётся без ссылки -------------------------------------------


async def test_пауза_посреди_выдачи_ссылок_не_оставляет_без_ссылки():
    room_id, members = await room_with(3)
    api = FakeApi()
    original = api.export_link
    calls = 0

    async def flaky(chat, title, expire):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise FloodWait(30)
        return await original(chat, title, expire)

    api.export_link = flaky
    await run(api)
    job = await job_of("create_group")
    assert job.status == "pending" and job.attempts == 0
    await rerun(api)
    assert (await job_of("create_group")).status == "done"
    for member_id in members:
        assert (await get(RoomMember, member_id)).tg_link
    assert [c[0] for c in api.calls].count("create_group") == 1


async def test_вступивший_пока_группа_собирается_получает_ссылку():
    room_id, members = await room_with(2)
    api = FakeApi()
    original = api.pin
    late: list[int] = []

    async def pin_while_joining(chat, message_id):
        await original(chat, message_id)
        # API в ту же минуту: видит «создаётся» и задания на ссылку не ставит
        async with SessionLocal() as other:
            user = User(phone="+998900009999", first_name="Опоздавший")
            other.add(user)
            await other.flush()
            member = RoomMember(
                room_id=room_id, user_id=user.id, role="member", status="joined", source="link"
            )
            other.add(member)
            await other.commit()
            late.append(member.id)

    api.pin = pin_while_joining
    await run(api)
    assert (await get(RoomMember, late[0])).tg_link


async def test_сверка_выдаёт_ссылку_застрявшим():
    room_id, members, api = await ready_room()
    async with SessionLocal() as session:
        (await session.get(RoomMember, members[1])).tg_link = None
        await session.commit()
    async with SessionLocal() as session:
        await service.reconcile_all(api, session)
        # Второй проход задание не задваивает
        await service.reconcile_all(api, session)
    assert [j.kind for j in await all_jobs()].count("invite_link") == 1
    await run(api)
    assert (await get(RoomMember, members[1])).tg_link


async def test_новая_ссылка_гасит_прежнюю():
    room_id, members, api = await ready_room()
    async with SessionLocal() as session:
        member = await session.get(RoomMember, members[1])
        old, member.tg_link = member.tg_link, None
        session.add(
            TgJob(
                kind="invite_link", room_id=room_id, member_id=members[1], payload={"revoke": old}
            )
        )
        await session.commit()
    api.calls.clear()
    await run(api)
    assert [c[0] for c in api.calls] == ["revoke_link", "export_link"]
    assert api.calls[0] == ("revoke_link", (777, 999), old)
    assert (await get(RoomMember, members[1])).tg_link not in (None, old)


# --- Застрявшие группы ------------------------------------------------------


async def test_сборка_после_сбоя_не_повторяет_сделанного():
    room_id, _ = await room_with()
    api = FakeApi()
    original = api.pin

    async def broken(chat, message_id):
        raise ConnectionError("обрыв")

    api.pin = broken
    await run(api)
    api.pin = original
    await rerun(api)
    kinds = [c[0] for c in api.calls]
    # Первое сообщение одно: повтор начал с закрепления
    assert kinds.count("send") == 1 and kinds.count("show_history") == 1
    assert ("pin", (777, 999), 42) in api.calls
    assert (await get(Room, room_id)).tg_state == "ready"


async def test_несобранная_группа_не_висит_вечно():
    room_id, _ = await room_with()
    api = FakeApi()

    async def broken(chat, message_id):
        raise ConnectionError("обрыв")

    api.pin = broken
    await run(api)
    for _ in range(service.MAX_ATTEMPTS - 1):
        await rerun(api)
    assert (await job_of("create_group")).status == "failed"
    room = await get(Room, room_id)
    # Группу завели, собрать не вышло: «не вышло», а не вечное «создаётся»
    assert room.tg_state == "failed" and room.tg_chat_id == 777
    async with SessionLocal() as session:
        await rooms_api.housekeeping(session, DAY + timedelta(days=15))
        await session.commit()
    assert (await job_of("leave")).payload == {}
    await run(api)
    assert ("leave", (777, 999)) in api.calls
    room = await get(Room, room_id)
    assert room.tg_state == "failed" and room.tg_left_at is not None


async def test_отменённый_до_сборки_поход_бросает_группу():
    room_id, _ = await room_with()
    api = FakeApi()
    original = api.send

    async def broken(chat, text):
        raise ConnectionError("обрыв")

    api.send = broken
    await run(api)
    async with SessionLocal() as session:
        (await session.get(Room, room_id)).status = "cancelled"
        await session.commit()
    api.send = original
    await rerun(api)
    assert (await get(Room, room_id)).tg_state == "failed"
    await run(api)
    assert ("leave", (777, 999)) in api.calls
    assert not any(c[0] == "send" for c in api.calls)


async def test_повтор_шага_телеграм_не_считает_ошибкой():
    from telethon import errors

    class Client:
        def __init__(self, error):
            self.error = error

        async def __call__(self, request):
            raise self.error(request=request)

    # История уже видна, сообщение уже закреплено, ссылка уже погашена
    await TelethonApi(Client(errors.ChatNotModifiedError)).show_history((1, 2))
    await TelethonApi(Client(errors.ChatNotModifiedError)).pin((1, 2), 3)
    for error in (
        errors.InviteRevokedMissingError,
        errors.InviteHashExpiredError,
        errors.ChatNotModifiedError,
    ):
        await TelethonApi(Client(error)).revoke_link((1, 2), "https://t.me/+x")


async def test_вышедший_человек_не_то_же_что_пропавшая_группа():
    from telethon import errors

    class Client:
        def __init__(self, error):
            self.error = error

        async def __call__(self, request):
            raise self.error(request=request)

    for error in (errors.UserNotParticipantError, errors.ParticipantIdInvalidError):
        with pytest.raises(NotParticipant):
            await TelethonApi(Client(error)).promote((1, 2), 3, 4)
    for error in (errors.ChannelPrivateError, errors.ChannelInvalidError):
        with pytest.raises(Gone):
            await TelethonApi(Client(error)).promote((1, 2), 3, 4)


# --- /leave -----------------------------------------------------------------


async def test_после_leave_переписка_не_копится_даже_в_паузу():
    room_id, members, api = await ready_room()
    organizer = await get(RoomMember, members[0])
    member = await get(RoomMember, members[1])
    async with SessionLocal() as session:
        await service.on_join(api, session, 777, 5001, 6001, organizer.tg_link)
        await service.on_join(api, session, 777, 5002, 6002, member.tg_link)
    await run(api)
    async with SessionLocal() as session:
        await service.on_message(session, 777, 10, 5002, "до", False, NOW)
        await service.on_message(session, 777, 11, 5001, "/leave", False, NOW)
    assert (await get(Room, room_id)).tg_state == "leaving"
    api.fail = FloodWait(60)
    await run(api)
    # Выйти мешает пауза, а сказанное после /leave уже не сохраняется
    async with SessionLocal() as session:
        await service.on_message(session, 777, 12, 5002, "после", False, NOW)
        stored = (await session.execute(select(TgMessage.text))).scalars().all()
    assert stored == ["до"]
    api.fail = None
    await rerun(api)
    room = await get(Room, room_id)
    assert room.tg_state == "left" and ("leave", (777, 999)) in api.calls
    async with SessionLocal() as session:
        assert (await session.execute(select(TgMessage))).scalars().all() == []


async def test_leave_стирает_переписку_и_когда_прощание_опередило():
    room_id, members, api = await ready_room()
    organizer = await get(RoomMember, members[0])
    async with SessionLocal() as session:
        await service.on_join(api, session, 777, 5001, 6001, organizer.tg_link)
    await run(api)
    # Уборка поставила прощание по сроку, а организатор успел сказать /leave
    async with SessionLocal() as session:
        await rooms_api.housekeeping(session, DAY + timedelta(days=15))
        await session.commit()
    async with SessionLocal() as session:
        await service.on_message(session, 777, 10, 5001, "секрет", False, NOW)
        await service.on_message(session, 777, 11, 5001, "/leave", False, NOW)
    await run(api)
    assert (await get(Room, room_id)).tg_state == "left"
    assert api.calls.count(("leave", (777, 999))) == 1
    async with SessionLocal() as session:
        assert (await session.execute(select(TgMessage))).scalars().all() == []


@pytest.mark.parametrize(
    "error",
    [ConnectionError("обрыв"), Restricted("UserRestrictedError")],
    ids=["сбои", "ограничен"],
)
async def test_невышедший_по_leave_выходит_при_архиве(error):
    """Выйти по /leave так и не дали — переписку всё равно стираем, как
    обещали, а выходит Sayr Admin с уходом комнаты в архив, а не сидит в
    группе вечно в «уходит»"""
    room_id, members, api = await ready_room()
    organizer = await get(RoomMember, members[0])
    async with SessionLocal() as session:
        await service.on_join(api, session, 777, 5001, 6001, organizer.tg_link)
    await run(api)
    async with SessionLocal() as session:
        await service.on_message(session, 777, 10, 5001, "до", False, NOW)
        await service.on_message(session, 777, 11, 5001, "/leave", False, NOW)
    api.fail = error
    for _ in range(service.MAX_ATTEMPTS):
        await rerun(api)
    assert [j.status for j in await all_jobs() if j.kind == "leave"] == ["failed"]
    assert (await get(Room, room_id)).tg_state == "leaving"
    async with SessionLocal() as session:
        assert (await session.execute(select(TgMessage))).scalars().all() == []

    api.fail = None
    async with SessionLocal() as session:
        await rooms_api.housekeeping(session, DAY + timedelta(days=15))
        await session.commit()
    [again] = [j for j in await all_jobs() if j.kind == "leave" and j.status == "pending"]
    assert again.payload == {"forget": True}
    await run(api)
    room = await get(Room, room_id)
    assert room.tg_state == "left" and ("leave", (777, 999)) in api.calls


async def test_выходя_служба_гасит_ссылки_ушедших():
    """После выхода служба ссылку уже не погасит, а задание на выход для
    удалённого встало в очередь позже /leave — гасим, пока ещё в группе"""
    room_id, members, api = await ready_room(3)
    organizer = await get(RoomMember, members[0])
    async with SessionLocal() as session:
        await service.on_join(api, session, 777, 5001, 6001, organizer.tg_link)
    await run(api)
    async with SessionLocal() as session:
        await service.on_message(session, 777, 10, 5001, "/leave", False, NOW)
    link = (await get(RoomMember, members[1])).tg_link
    await drop(members[1])
    api.calls.clear()
    await run(api)
    kinds = [c[0] for c in api.calls]
    assert ("revoke_link", (777, 999), link) in api.calls
    assert kinds.index("revoke_link") < kinds.index("leave")
    # Ссылку того, кто в комнате, не трогаем — по ней войдёт свой
    assert [c[2] for c in api.calls if c[0] == "revoke_link"] == [link]
    assert (await get(RoomMember, members[2])).tg_link
