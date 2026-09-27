"""Sayr Admin на подменном клиенте: создание группы, личные ссылки, вход
по ссылке, админ-организатор, удаление из группы, переписка, /leave,
паузы Telegram и ограничение аккаунта.

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
from app.tg.api import FloodWait, Restricted

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
    assert kicks and kicks[0].payload == {"tg_user_id": 5002, "tg_user_hash": 6002}


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
