"""Sayr Admin на подменном клиенте: создание группы, личные ссылки, вход
по ссылке, админ-организатор, удаление из группы и заметка о нём, заметка
о заявке, переписка, /leave, паузы Telegram и ограничение аккаунта; гашение
ссылок ушедших, застрявшие без ссылки люди, недособранные группы
и недовыполненный /leave.

Настоящий Telegram здесь не нужен: логика знает его только через TgApi.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select, update

from app.api import rooms as rooms_api
from app.config import settings
from app.db import SessionLocal, engine
from app.models import (
    Gender,
    Place,
    PushOutbox,
    Room,
    RoomMember,
    TgJob,
    TgMessage,
    TgStatus,
    User,
    UserBlock,
    UserSession,
)
from app.tg import service, texts
from app.tg.api import FloodWait, Gone, MessageGone, NotParticipant, Restricted, TelethonApi

NOW = datetime.now(timezone.utc)
DAY = rooms_api.today() + timedelta(days=5)


class FakeApi:
    """Telegram понарошку: помнит, что с ним делали"""

    def __init__(self):
        self.calls: list[tuple] = []
        self.importers: dict[str, list[tuple[int, int]]] = {}
        self.fail: Exception | None = None
        self.links = 0
        #: Номер следующего сообщения: первое в группе — 42
        self.message_id = 42

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
        self.message_id += 1
        return self.message_id - 1

    async def edit(self, chat, message_id, text):
        await self._step("edit", chat, message_id, text)

    async def delete(self, chat, message_id):
        await self._step("delete", chat, message_id)

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
        rooms_api.kick(session, room, member, "left" if status == "left" else "removed")
        await session.commit()


async def job_of(kind: str) -> TgJob:
    async with SessionLocal() as session:
        return (await session.execute(select(TgJob).where(TgJob.kind == kind))).scalar_one()


async def all_jobs() -> list[TgJob]:
    async with SessionLocal() as session:
        return list((await session.execute(select(TgJob))).scalars())


def said(api: FakeApi) -> list[str]:
    """Что Sayr Admin написал в группу"""
    return [c[2] for c in api.calls if c[0] == "send"]


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
    # Без имени: удаление уносит всё о человеке, и заметки о нём не будет
    assert kicks and kicks[0].payload == {
        "tg_user_id": 5002, "tg_user_hash": 6002, "tg_link": member.tg_link, "reason": "deleted",
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
    # Выгнан на деле — и группе заметка, хотя службе он был не известен
    assert said(api)[-1] == "Человек1 больше не в походе.\n\nЧеловек1 endi sayohatda emas."


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
    payload = (await job_of("kick")).payload
    assert payload["tg_user_id"] == 5002
    # Задание поставила служба, а не API — причина и имя для заметки и в нём
    assert (payload["reason"], payload["name"]) == ("removed", "Человек1")
    await run(api)
    assert ("kick", (777, 999), 5002, 6002) in api.calls
    assert said(api)[-1].startswith("Человек1 больше не в походе.")


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


# --- Вышел из комнаты — заметка в группе ------------------------------------


async def in_group(**user) -> tuple[int, list[int], FakeApi]:
    """Готовая группа, и второй участник вошёл в неё по своей ссылке. Что
    служба делала до этого, забыто"""
    room_id, members, api = await ready_room()
    member = await get(RoomMember, members[1])
    async with SessionLocal() as session:
        person = await session.get(User, member.user_id)
        for k, v in user.items():
            setattr(person, k, v)
        await service.on_join(api, session, 777, 5002, 6002, member.tg_link)
    api.calls.clear()
    return room_id, members, api


@pytest.mark.parametrize(
    "status, user, note",
    [
        (
            "left",
            dict(first_name="Азиз", gender=Gender.male),
            "Азиз вышел из комнаты в Sayr.\n\nАзиз Sayr ilovasidagi xonadan chiqdi.",
        ),
        (
            "left",
            dict(first_name="Мадина", gender=Gender.female),
            "Мадина вышла из комнаты в Sayr.\n\nМадина Sayr ilovasidagi xonadan chiqdi.",
        ),
        (
            "removed",
            dict(first_name="Мадина", gender=Gender.female),
            "Мадина больше не в походе.\n\nМадина endi sayohatda emas.",
        ),
        (
            "left",
            dict(first_name=" "),
            "Пользователь Sayr вышел из комнаты в Sayr.\n\n"
            "Sayr foydalanuvchisi Sayr ilovasidagi xonadan chiqdi.",
        ),
    ],
    ids=["вышел", "вышла", "убрали", "без-имени"],
)
async def test_выгнанному_заметка_в_группу(status, user, note):
    """Имя — как в комнате, и только оно: ни номера, ни ника Telegram.
    Убрал ли организатор, блок или запрет — группе одно «больше не в походе»"""
    room_id, members, api = await in_group(telegram_username="madina_tg", **user)
    await drop(members[1], status)
    await run(api)
    assert ("kick", (777, 999), 5002, 6002) in api.calls
    assert said(api) == [note]


async def test_вышедшему_из_группы_самому_заметки_нет():
    """Из группы он ушёл раньше, чем из комнаты: выгонять некого, и вопроса
    «куда он делся» у группы нет"""
    room_id, members, api = await in_group()

    async def absent(chat, user_id, user_hash):
        await api._step("kick", chat, user_id, user_hash)
        raise NotParticipant("UserNotParticipantError")

    api.kick = absent
    await drop(members[1], "left")
    await run(api)
    assert (await job_of("kick")).status == "done"
    assert ("kick", (777, 999), 5002, 6002) in api.calls
    assert said(api) == []


async def test_не_входившему_в_группу_заметки_нет():
    """Ссылку он так и не открыл: гасим её, а в группе о нём не пишем"""
    room_id, members, api = await ready_room()
    api.calls.clear()
    await drop(members[1], "left")
    await run(api)
    assert [c[0] for c in api.calls] == ["revoke_link", "link_importers"]


async def test_заметка_после_паузы_пишется_хотя_выгонять_уже_некого():
    room_id, members, api = await in_group()
    await drop(members[1], "left")
    send = api.send

    async def flood(chat, text):
        raise FloodWait(30)

    async def absent(chat, user_id, user_hash):
        raise NotParticipant("UserNotParticipantError")

    # Выгнали, а на заметке — пауза
    api.send = flood
    await run(api)
    assert (await job_of("kick")).status == "pending"
    # Повтор: в группе его уже нет, а сказать группе всё ещё нужно — один раз
    api.send, api.kick = send, absent
    await rerun(api)
    await rerun(api)
    assert (await job_of("kick")).status == "done"
    assert said(api) == [
        "Человек1 вышел из комнаты в Sayr.\n\nЧеловек1 Sayr ilovasidagi xonadan chiqdi."
    ]


async def test_пока_sayr_admin_уходит_заметок_нет():
    """Организатор сказал /leave: выгнать ещё выгоняем, а писать — нет"""
    room_id, members, api = await in_group()
    async with SessionLocal() as session:
        (await session.get(Room, room_id)).tg_state = "leaving"
        await session.commit()
    await drop(members[1], "left")
    await run(api)
    assert ("kick", (777, 999), 5002, 6002) in api.calls
    assert said(api) == []


async def test_об_удалившем_аккаунт_заметки_нет():
    room_id, members, api = await in_group()
    async with SessionLocal() as session:
        member = await session.get(RoomMember, members[1])
        user = await session.get(User, member.user_id)
        await rooms_api.on_account_deleted(session, user)
        await session.delete(user)
        await session.commit()
    await run(api)
    assert ("kick", (777, 999), 5002, 6002) in api.calls
    assert said(api) == []


async def test_вышел_и_удалил_аккаунт_раньше_службы_заметки_нет():
    """Вышел из комнаты, и тут же удалил аккаунт — задание на выход ещё
    ждало службу: выгнать выгоняем, а о нём ни слова, и имени его в задании
    больше нет"""
    room_id, members, api = await in_group(first_name="Азиз")
    await drop(members[1], "left")
    async with SessionLocal() as session:
        member = await session.get(RoomMember, members[1])
        user = await session.get(User, member.user_id)
        await rooms_api.on_account_deleted(session, user)
        await session.delete(user)
        await session.commit()
    kick = await job_of("kick")
    assert "name" not in kick.payload and "gender" not in kick.payload
    await run(api)
    assert ("kick", (777, 999), 5002, 6002) in api.calls
    assert said(api) == []


async def test_выгнать_можно_только_того_кто_в_группе():
    """Бан вышедшего Telegram принимает молча — служба сначала спрашивает,
    в группе ли он: заметка только о выгнанном на деле"""
    from telethon.tl import types
    from telethon.tl.functions.channels import GetParticipantRequest

    class Client:
        def __init__(self, participant):
            self.participant = participant
            self.requests: list[str] = []

        async def __call__(self, request):
            self.requests.append(type(request).__name__)
            if isinstance(request, GetParticipantRequest):
                return types.channels.ChannelParticipant(
                    participant=self.participant, chats=[], users=[]
                )

    def banned(left: bool):
        return types.ChannelParticipantBanned(
            peer=types.PeerUser(3), kicked_by=1, date=None,
            banned_rights=types.ChatBannedRights(until_date=None, send_messages=True), left=left,
        )

    for participant in (types.ChannelParticipantLeft(peer=types.PeerUser(3)), banned(True)):
        client = Client(participant)
        with pytest.raises(NotParticipant):
            await TelethonApi(client).kick((1, 2), 3, 4)
        assert client.requests == ["GetParticipantRequest"]
    # В группе — и обычный участник, и ограниченный в правах
    for participant in (types.ChannelParticipant(user_id=3, date=None), banned(False)):
        client = Client(participant)
        await TelethonApi(client).kick((1, 2), 3, 4)
        assert client.requests == [
            "GetParticipantRequest", "EditBannedRequest", "EditBannedRequest",
        ]


# --- Заявка в комнату — заметка в группе ------------------------------------

_phones = iter(range(7000, 8000))


async def ask(room_id: int, **user) -> int:
    """Кто-то попросился в комнату — так, как это делает API"""
    async with SessionLocal() as session:
        fields = {"first_name": "Мадина", "telegram_username": "madina_tg"} | user
        person = User(phone=f"+99890000{next(_phones)}", **fields)
        session.add(person)
        await session.flush()
        member = RoomMember(
            room_id=room_id, user_id=person.id, role="member", status="requested",
            source="request",
        )
        session.add(member)
        await session.flush()
        rooms_api.request_note(session, await session.get(Room, room_id), member)
        await session.commit()
        return member.id


async def decide(member_id: int, status: str) -> None:
    """С заявкой что-то сделали — так, как это делает API"""
    async with SessionLocal() as session:
        member = await session.get(RoomMember, member_id)
        member.status = status
        rooms_api.request_note(session, await session.get(Room, member.room_id), member)
        await session.commit()


async def noted() -> tuple[int, int, int, FakeApi]:
    """Готовая группа, и в ней уже висит заметка о заявке: (комната, заявка,
    номер заметки, Telegram понарошку с чистой памятью)"""
    room_id, _, api = await ready_room()
    member_id = await ask(room_id)
    await run(api)
    note = (await get(RoomMember, member_id)).tg_request_message_id
    assert note is not None
    api.calls.clear()
    return room_id, member_id, note, api


async def note_jobs() -> list[TgJob]:
    async with SessionLocal() as session:
        q = select(TgJob).where(TgJob.kind == "request_note").order_by(TgJob.id)
        return list((await session.execute(q)).scalars())


@pytest.mark.parametrize(
    "first_name, ru, uz",
    [
        ("Мадина", "Мадина", "Мадина"),
        # Узбекские буквы — узкими знаками, как во всём, что идёт на экран
        ("Gʻayrat", "G‘ayrat", "G‘ayrat"),
        # Имя стёрли, пока заявка ждала службу
        ("", "Пользователь Sayr", "Sayr foydalanuvchisi"),
    ],
    ids=["имя", "узбекское", "без-имени"],
)
async def test_заявка_объявляется_в_группе(first_name, ru, uz):
    """Только имя, как в комнате, и ссылка на комнату: ни номера, ни ника"""
    room_id, _, api = await ready_room()
    api.calls.clear()
    member_id = await ask(room_id, first_name=first_name)
    await run(api)
    room = await get(Room, room_id)
    assert said(api) == [
        f"{ru} просится в поход. Решает организатор — в Sayr.\n\n"
        f"{uz} sayohatga qo‘shilmoqchi. Tashkilotchi Sayr ilovasida hal qiladi.\n\n"
        f"{settings.public_url}/j/{room.code}"
    ]
    # Номер — у заявки: по нему заметку поправят или уберут
    posted = api.message_id - 1
    assert (await get(RoomMember, member_id)).tg_request_message_id == posted
    assert (await job_of("request_note")).payload == {"posted": posted}


@pytest.mark.parametrize("state", ["pending", "leaving", "left"])
async def test_заметки_нет_если_sayr_admin_не_в_группе(state):
    """Задание поставили, пока группа была готова, а к его часу Sayr Admin
    ушёл или группу собирают заново: писать некуда"""
    room_id, _, api = await ready_room()
    await ask(room_id)
    async with SessionLocal() as session:
        (await session.get(Room, room_id)).tg_state = state
        await session.commit()
    api.calls.clear()
    await run(api)
    assert api.calls == []
    assert (await job_of("request_note")).status == "done"


async def test_не_больше_заметок_о_заявках_в_час(monkeypatch):
    monkeypatch.setattr(settings, "tg_request_notes_per_hour", 2)
    room_id, _, api = await ready_room()
    api.calls.clear()
    asked = [await ask(room_id, first_name=f"Гость{i}") for i in range(3)]
    await run(api)
    # Третья — сверх предела: организатору только пуш, как без группы
    assert len(said(api)) == 2
    assert (await get(RoomMember, asked[2])).tg_request_message_id is None
    # Пропущенная предел не продлевает: через час он снова открыт
    async with SessionLocal() as session:
        await session.execute(update(TgJob).values(done_at=NOW - timedelta(minutes=61)))
        await session.commit()
    await ask(room_id, first_name="Гость3")
    await run(api)
    assert len(said(api)) == 3


@pytest.mark.parametrize("asker_blocks", [True, False], ids=["она-его", "он-её"])
async def test_заявку_того_с_кем_у_вступившего_блок_не_объявляют(asker_blocks):
    """Блок с вступившим заявку не останавливает, а в приложении они друг
    друга не видят: и в группе её имени ему не покажем — организатору пуш"""
    room_id, members, api = await ready_room()
    api.calls.clear()
    asker = await ask(room_id)
    async with SessionLocal() as session:
        her = (await session.get(RoomMember, asker)).user_id
        him = (await session.get(RoomMember, members[1])).user_id
        pair = (her, him) if asker_blocks else (him, her)
        session.add(UserBlock(blocker_id=pair[0], blocked_id=pair[1]))
        await session.commit()
    await run(api)
    assert api.calls == []
    assert (await get(RoomMember, asker)).tg_request_message_id is None
    assert (await job_of("request_note")).status == "done"


async def test_блок_после_заметки_убирает_её():
    """Заметка уже висит, а тут блок с вступившим или вступил тот, с кем
    блок: API ставит задание, служба заметку молча удаляет, заявка — как была"""
    room_id, member_id, note, api = await noted()
    async with SessionLocal() as session:
        room = await service._room(session, room_id)
        asker = next(m for m in room.members if m.id == member_id)
        timur = next(m for m in room.members if m.status == "joined" and m.role == "member")
        session.add(UserBlock(blocker_id=timur.user_id, blocked_id=asker.user_id))
        rooms_api.request_note(session, room, asker)
        await session.commit()
    await run(api)
    assert api.calls == [("delete", (777, 999), note)]
    member = await get(RoomMember, member_id)
    assert (member.status, member.tg_request_message_id) == ("requested", None)


async def test_одобренная_заявка_правит_заметку():
    room_id, member_id, note, api = await noted()
    await decide(member_id, "joined")
    await run(api)
    assert api.calls == [
        ("edit", (777, 999), note, "Мадина теперь в походе.\n\nМадина endi sayohatda.")
    ]
    # С заметкой разобрались: номер ушёл из заявки в задание
    assert (await get(RoomMember, member_id)).tg_request_message_id is None
    assert (await note_jobs())[-1].payload == {"note": note}


@pytest.mark.parametrize("status", ["declined", "left", "removed"])
async def test_неодобренная_заявка_убирает_заметку_молча(status):
    """Отказ, отзыв, «убрать» — заметку просто удаляем: о том, что человека
    не взяли, группе знать незачем"""
    room_id, member_id, note, api = await noted()
    await decide(member_id, status)
    await run(api)
    assert api.calls == [("delete", (777, 999), note)]


async def test_отмена_похода_убирает_заметки_о_заявках():
    room_id, member_id, note, api = await noted()
    async with SessionLocal() as session:
        rooms_api.cancel(session, await service._room(session, room_id))
        await session.commit()
    await run(api)
    assert ("delete", (777, 999), note) in api.calls
    assert said(api) == [texts.TEXTS["cancelled"]]


@pytest.mark.parametrize(
    "status", [None, "declined", "left", "joined"], ids=["ждёт", "отказ", "отозвал", "одобрили"]
)
async def test_удалившего_аккаунт_заметку_убирают(status):
    """Что бы с заявкой ни было: задание на отказ, отзыв или одобрение могло
    ещё не дойти до службы, а строка заявки уйдёт с аккаунтом — номер
    заметки тогда только в задании, поставленном при удалении"""
    room_id, member_id, note, api = await noted()
    if status:
        await decide(member_id, status)
    async with SessionLocal() as session:
        user = await session.get(User, (await session.get(RoomMember, member_id)).user_id)
        await rooms_api.on_account_deleted(session, user)
        await session.delete(user)
        await session.commit()
    await run(api)
    assert api.calls == [("delete", (777, 999), note)]


async def test_разобранная_до_заметки_заявка_в_группу_не_попадает():
    """Одобрили раньше, чем служба дошла до заметки (пауза Telegram): писать
    «просится» уже незачем, править нечего"""
    room_id, _, api = await ready_room()
    member_id = await ask(room_id)
    await decide(member_id, "joined")
    api.calls.clear()
    await run(api)
    assert api.calls == []
    assert [j.status for j in await note_jobs()] == ["done", "done"]


async def test_отозвал_и_снова_попросился_заметка_одна():
    """Отозвал и попросился снова раньше, чем служба дошла до заметки:
    прежняя заметка снова про живую заявку — не удаляем и второй не пишем"""
    room_id, member_id, note, api = await noted()
    await decide(member_id, "left")
    await decide(member_id, "requested")
    await run(api)
    assert api.calls == []
    assert (await get(RoomMember, member_id)).tg_request_message_id == note


async def test_заметку_не_тронуть_и_задание_не_повторяется(caplog):
    """Заметку удалил организатор или у Sayr Admin отобрали права — повтор
    ничего не изменит: задание выполнено, в журнале — запись"""
    room_id, member_id, note, api = await noted()

    async def forbidden(chat, message_id):
        raise MessageGone("MessageDeleteForbiddenError")

    api.delete = forbidden
    await decide(member_id, "declined")
    with caplog.at_level(logging.INFO, logger="sayr.tg"):
        await run(api)
    last = (await note_jobs())[-1]
    assert (last.status, last.attempts) == ("done", 0)
    assert "не тронуть" in caplog.text
    assert (await get(Room, room_id)).tg_state == "ready"


async def test_sayr_admin_без_группы_заметку_бросает():
    """Sayr Admin выгнали из группы: заметку не тронуть, а группа для службы —
    «вышел», как от любого задания"""
    room_id, member_id, note, api = await noted()

    async def gone(chat, message_id):
        raise Gone("ChannelPrivateError")

    api.delete = gone
    await decide(member_id, "declined")
    await run(api)
    assert (await note_jobs())[-1].status == "done"
    assert (await get(Room, room_id)).tg_state == "left"


async def test_прошедшая_заявка_убирает_заметку():
    room_id, member_id, note, api = await noted()
    async with SessionLocal() as session:
        await rooms_api.housekeeping(session, DAY + timedelta(days=1))
        await session.commit()
    await run(api)
    assert api.calls == [("delete", (777, 999), note)]
    # Заявка как была — одобрить её уже нельзя, а заметки больше нет
    member = await get(RoomMember, member_id)
    assert (member.status, member.tg_request_message_id) == ("requested", None)


async def test_сбой_удаления_повторяется_но_не_вечно():
    room_id, member_id, note, api = await noted()
    async with SessionLocal() as session:
        await rooms_api.housekeeping(session, DAY + timedelta(days=1))
        await session.commit()
    api.fail = ConnectionError("обрыв")
    for _ in range(service.MAX_ATTEMPTS):
        await rerun(api)
    # Каждый повтор — та же заметка, номер её — в задании
    assert api.calls == [("delete", (777, 999), note)] * service.MAX_ATTEMPTS
    assert (await note_jobs())[-1].status == "failed"
    # Сдавшееся задание уборка заново не ставит: заметки у заявки уже нет
    async with SessionLocal() as session:
        await rooms_api.housekeeping(session, DAY + timedelta(days=2))
        await session.commit()
    assert len(await note_jobs()) == 2


async def test_миграция_0034_откатывается_и_накатывается():
    """Номер заметки — колонкой миграции 0034: откат её убирает, накат
    возвращает, а голова у миграций одна"""
    import importlib.util
    from pathlib import Path

    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect

    versions = Path(__file__).parent.parent / "alembic"
    config = Config()
    config.set_main_option("script_location", str(versions))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert script.get_revision("0034").down_revision == "0033"

    spec = importlib.util.spec_from_file_location(
        "migration_0034", versions / "versions" / "0034_tg_request_notes.py"
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def columns(conn) -> set[str]:
        return {c["name"] for c in inspect(conn).get_columns("room_members")}

    def roundtrip(conn) -> None:
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
            assert "tg_request_message_id" not in columns(conn)
            migration.upgrade()
            assert "tg_request_message_id" in columns(conn)

    # Одной транзакцией: схема тестовой базы остаётся как была при любом исходе
    async with engine.begin() as conn:
        await conn.run_sync(roundtrip)


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


async def test_своё_сообщение_не_тронуть_не_то_же_что_пропавшая_группа():
    """Заметку удалил организатор или у Sayr Admin отобрали права — это про
    сообщение, а не про группу. Уже поправленное — не ошибка вовсе"""
    from telethon import errors

    class Client:
        def __init__(self, error):
            self.error = error

        async def __call__(self, request):
            raise self.error(request=request)

    await TelethonApi(Client(errors.MessageNotModifiedError)).edit((1, 2), 3, "текст")
    for error in (
        errors.MessageIdInvalidError,
        errors.MessageEditTimeExpiredError,
        errors.ChatAdminRequiredError,
    ):
        with pytest.raises(MessageGone):
            await TelethonApi(Client(error)).edit((1, 2), 3, "текст")
    for error in (errors.MessageDeleteForbiddenError, errors.ChatAdminRequiredError):
        with pytest.raises(MessageGone):
            await TelethonApi(Client(error)).delete((1, 2), 3)
    for error in (errors.ChannelPrivateError, errors.ChannelInvalidError):
        with pytest.raises(Gone):
            await TelethonApi(Client(error)).delete((1, 2), 3)


async def test_сообщения_уходят_без_разметки():
    """Имя «[Жми](ссылка)» в заметке не становится ссылкой от Sayr Admin"""

    class Client:
        async def send_message(self, peer, text, **kw):
            self.kw = kw
            return type("Message", (), {"id": 7})()

    client = Client()
    assert await TelethonApi(client).send((1, 2), "[Жми](https://example.com)") == 7
    assert client.kw["parse_mode"] is None


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
