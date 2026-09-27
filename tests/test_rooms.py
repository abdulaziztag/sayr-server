"""Комнаты попутчиков: требования, кто что видит, ссылка и заявки,
блокировки, пуши, задания Sayr Admin, уборка и универсальные ссылки.

Человек и сессия заводятся прямо в базе — вход проверяется в test_auth.py.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi_storages import StorageFile
from sqlalchemy import delete, select

from app.api import rooms as rooms_api
from app.auth.tokens import new_token
from app.config import settings
from app.db import SessionLocal
from app.models import (
    PushOutbox,
    PushToken,
    Room,
    RoomMember,
    RoomReport,
    TgJob,
    User,
    UserBlock,
    UserSession,
    avatar_storage,
)
from app.push import SendResult
from app.push.outbox import render, send_outbox

TODAY = rooms_api.today()
DAY = TODAY + timedelta(days=5)
ADULT = TODAY.year - 30


@pytest.fixture(autouse=True)
async def rooms_on(monkeypatch):
    monkeypatch.setattr(settings, "rooms_open", True)
    yield
    async with SessionLocal() as session:
        for model in (TgJob, PushOutbox, RoomReport, RoomMember, Room, UserBlock, PushToken):
            await session.execute(delete(model))
        await session.execute(delete(UserSession))
        await session.execute(delete(User))
        await session.commit()


_phones = iter(range(1000000, 9999999))


async def person(
    name: str = "Азиз",
    birth_year: int | None = ADULT,
    photo: bool = True,
    telegram: str | None = "aziz_tg",
    device: str | None = None,
) -> tuple[dict, int]:
    """Человек с анкетой и токеном: (заголовки, id)"""
    token, digest = new_token()
    device = device or f"dev-{next(_phones)}"
    async with SessionLocal() as session:
        user = User(
            phone=f"+99890{next(_phones)}",
            first_name=name,
            birth_year=birth_year,
            telegram_username=telegram,
        )
        if photo:
            user.avatar = StorageFile(name=f"{name}.jpg", storage=avatar_storage)
        session.add(user)
        await session.flush()
        session.add(UserSession(user_id=user.id, token_hash=digest, device_id=device))
        await session.commit()
        return {"Authorization": f"Bearer {token}", "X-Device-Id": device}, user.id


async def open_room(client, headers, **kw) -> dict:
    body = {"place": "test-peak", "day": DAY.isoformat(), "is_open": True} | kw
    resp = await client.post("/api/v1/rooms", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def jobs(kind: str | None = None) -> list[TgJob]:
    async with SessionLocal() as session:
        q = select(TgJob)
        if kind:
            q = q.where(TgJob.kind == kind)
        return list((await session.execute(q)).scalars())


async def pushes(user_id: int) -> list[str]:
    async with SessionLocal() as session:
        rows = await session.execute(select(PushOutbox.kind).where(PushOutbox.user_id == user_id))
        return list(rows.scalars())


async def set_room(code: str, **fields) -> None:
    async with SessionLocal() as session:
        room = (await session.execute(select(Room).where(Room.code == code))).scalar_one()
        for k, v in fields.items():
            setattr(room, k, v)
        await session.commit()


# --- Флаг и требования ------------------------------------------------------


async def test_без_флага_комнат_нет(client, monkeypatch):
    monkeypatch.setattr(settings, "rooms_open", False)
    h, _ = await person()
    assert (await client.get("/api/v1/places/test-peak/rooms")).status_code == 404
    resp = await client.post(
        "/api/v1/rooms", json={"place": "test-peak", "day": DAY.isoformat()}, headers=h
    )
    assert resp.status_code == 404
    upd = await client.get("/api/v1/app/update", params={"platform": "ios", "version": "1.9.0"})
    assert upd.json()["features"] == {"rooms": False}


async def test_флаг_виден_приложениям(client):
    upd = await client.get("/api/v1/app/update", params={"platform": "android", "version": "1.9.0"})
    assert upd.json()["features"] == {"rooms": True}


@pytest.mark.parametrize(
    "profile, detail",
    [
        (dict(name=""), "name_required"),
        (dict(photo=False), "photo_required"),
        (dict(birth_year=None), "birth_year_required"),
        # Спорный год — в пользу отказа: «текущий − 18» ещё нельзя
        (dict(birth_year=TODAY.year - 18), "adult_only"),
    ],
)
async def test_для_незнакомых_нужна_анкета_и_18(client, profile, detail):
    h, _ = await person(**profile)
    resp = await client.post(
        "/api/v1/rooms",
        json={"place": "test-peak", "day": DAY.isoformat(), "is_open": True},
        headers=h,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == detail


async def test_для_своих_хватает_имени(client):
    h, _ = await person(photo=False, birth_year=None)
    room = await open_room(client, h, is_open=False)
    assert room["my_role"] == "organizer"
    assert room["invite_url"].startswith("https://sayr.info/r/")


async def test_ровно_19_лет_по_году_пускают(client):
    h, _ = await person(birth_year=TODAY.year - 19)
    await open_room(client, h)


async def test_одна_комната_на_день(client):
    h, _ = await person()
    await open_room(client, h, days=3)
    resp = await client.post(
        "/api/v1/rooms",
        json={"place": "test-lake", "day": (DAY + timedelta(days=2)).isoformat()},
        headers=h,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "busy_day"


async def test_мат_в_заметке_не_проходит(client):
    h, _ = await person()
    resp = await client.post(
        "/api/v1/rooms",
        json={"place": "test-peak", "day": DAY.isoformat(), "note": "бля, опять пешком"},
        headers=h,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "bad_text"


async def test_мат_в_имени_анкеты_не_проходит(client):
    h, _ = await person()
    resp = await client.patch("/api/v1/me", json={"first_name": "Сука"}, headers=h)
    assert resp.status_code == 422


# --- Кто что видит ----------------------------------------------------------


async def test_гость_видит_только_число(client):
    h, _ = await person()
    await open_room(client, h)
    await open_room(client, (await person(name="Мадина"))[0], is_open=False)
    resp = await client.get("/api/v1/places/test-peak/rooms")
    assert resp.status_code == 200
    body = resp.json()
    # Закрытая комната для своих в поиск не попадает
    assert body["days"] == [{"day": DAY.isoformat(), "rooms": 1}]
    assert body["rooms"] == []


async def test_в_поиске_карточка_организатора_без_ника(client):
    org, _ = await person(telegram="secret_nick")
    await open_room(client, org, transport="own_car", seats=2, note="иду не спеша")
    viewer, _ = await person(name="Мадина")
    rooms = (await client.get("/api/v1/places/test-peak/rooms", headers=viewer)).json()["rooms"]
    assert len(rooms) == 1
    card = rooms[0]["organizer"]
    assert card["first_name"] == "Азиз"
    assert card["age"] == 30
    assert card["seats"] == 2 and card["note"] == "иду не спеша"
    assert card["telegram_username"] is None
    assert "phone" not in card


async def test_чужой_видит_организатора_но_не_участников(client):
    org, _ = await person()
    room = await open_room(client, org)
    friend, _ = await person(name="Друг", telegram="friend_tg")
    invite = room["invite_url"].rsplit("/", 1)[1]
    await client.post(f"/api/v1/invites/{invite}/join", headers=friend)

    stranger, _ = await person(name="Мадина")
    view = (await client.get(f"/api/v1/rooms/{room['code']}", headers=stranger)).json()
    assert view["my_role"] == "none"
    assert view["people"] == 2
    assert view["members"] == [] and view["invite_url"] is None and view["group"] is None
    assert view["organizer"]["telegram_username"] is None


async def test_закрытую_комнату_чужой_не_видит(client):
    org, _ = await person()
    room = await open_room(client, org, is_open=False)
    stranger, _ = await person(name="Мадина")
    resp = await client.get(f"/api/v1/rooms/{room['code']}", headers=stranger)
    assert resp.status_code == 404


# --- Ссылка и заявки --------------------------------------------------------


async def test_по_ссылке_вступают_сразу_и_заводится_группа(client):
    org, org_id = await person()
    room = await open_room(client, org, is_open=False)
    friend, _ = await person(name="Друг", telegram="friend_tg")
    invite = room["invite_url"].rsplit("/", 1)[1]

    preview = await client.get(f"/api/v1/invites/{invite}", headers=friend)
    assert preview.status_code == 200 and preview.json()["my_role"] == "none"

    joined = (await client.post(f"/api/v1/invites/{invite}/join", headers=friend)).json()
    assert joined["my_role"] == "joined"
    assert joined["people"] == 2
    # Внутри видны ники участников — и организатора
    assert joined["organizer"]["telegram_username"] == "aziz_tg"
    assert await pushes(org_id) == ["room_joined"]
    assert [j.kind for j in await jobs()] == ["create_group"]
    async with SessionLocal() as session:
        r = (await session.execute(select(Room).where(Room.code == room["code"]))).scalar_one()
        assert r.tg_state == "pending"


async def test_заявка_одобрение_и_ссылка_в_готовую_группу(client):
    org, org_id = await person()
    room = await open_room(client, org)
    await set_room(room["code"], tg_state="ready", tg_chat_id=-100123)

    madina, madina_id = await person(name="Мадина", telegram="madina_tg")
    req = await client.post(
        f"/api/v1/rooms/{room['code']}/requests",
        json={"transport": "need_car", "note": "впервые на Чимгане"},
        headers=madina,
    )
    assert req.status_code == 200 and req.json()["my_role"] == "requested"
    assert await pushes(org_id) == ["room_request"]

    mine = (await client.get(f"/api/v1/rooms/{room['code']}", headers=org)).json()
    assert len(mine["requests"]) == 1
    request = mine["requests"][0]
    assert request["transport"] == "need_car"
    # До одобрения ника не видно даже организатору
    assert request["telegram_username"] is None

    ok = await client.post(
        f"/api/v1/rooms/{room['code']}/members/{request['member_id']}/approve", headers=org
    )
    assert ok.status_code == 200
    assert ok.json()["people"] == 2
    assert await pushes(madina_id) == ["room_approved"]
    assert [j.kind for j in await jobs()] == ["invite_link"]


async def test_отказ_и_повторная_заявка(client):
    org, _ = await person()
    room = await open_room(client, org)
    madina, madina_id = await person(name="Мадина")
    await client.post(f"/api/v1/rooms/{room['code']}/requests", json={}, headers=madina)
    member_id = (await client.get(f"/api/v1/rooms/{room['code']}", headers=org)).json()[
        "requests"
    ][0]["member_id"]
    await client.post(f"/api/v1/rooms/{room['code']}/members/{member_id}/decline", headers=org)
    assert await pushes(madina_id) == ["room_declined"]
    again = await client.post(f"/api/v1/rooms/{room['code']}/requests", json={}, headers=madina)
    assert again.status_code == 409 and again.json()["detail"] == "declined"


async def test_попроситься_без_анкеты_нельзя(client):
    org, _ = await person()
    room = await open_room(client, org)
    kid, _ = await person(name="Школьник", birth_year=TODAY.year - 15)
    resp = await client.post(f"/api/v1/rooms/{room['code']}/requests", json={}, headers=kid)
    assert resp.status_code == 403 and resp.json()["detail"] == "adult_only"


async def test_удалённого_убирают_из_группы_и_по_ссылке_не_пускают(client):
    org, _ = await person()
    room = await open_room(client, org, is_open=False)
    await set_room(room["code"], tg_state="ready", tg_chat_id=-100123)
    friend, friend_id = await person(name="Друг")
    invite = room["invite_url"].rsplit("/", 1)[1]
    joined = (await client.post(f"/api/v1/invites/{invite}/join", headers=friend)).json()
    async with SessionLocal() as session:
        m = (
            await session.execute(select(RoomMember).where(RoomMember.user_id == friend_id))
        ).scalar_one()
        m.tg_user_id = 555
        await session.commit()
        member_id = m.id
    assert joined["my_role"] == "joined"

    out = await client.delete(f"/api/v1/rooms/{room['code']}/members/{member_id}", headers=org)
    assert out.status_code == 200
    kicks = await jobs("kick")
    assert len(kicks) == 1 and kicks[0].payload == {"tg_user_id": 555, "tg_user_hash": None}
    assert "room_removed" in await pushes(friend_id)
    back = await client.post(f"/api/v1/invites/{invite}/join", headers=friend)
    assert back.status_code == 403 and back.json()["detail"] == "removed"


async def test_организатор_не_уходит_а_отменяет(client):
    org, _ = await person()
    room = await open_room(client, org)
    resp = await client.post(f"/api/v1/rooms/{room['code']}/leave", headers=org)
    assert resp.status_code == 409


async def test_отмена_похода(client):
    org, _ = await person()
    room = await open_room(client, org)
    await set_room(room["code"], tg_state="ready", tg_chat_id=-100123)
    friend, friend_id = await person(name="Друг")
    madina, madina_id = await person(name="Мадина")
    invite = room["invite_url"].rsplit("/", 1)[1]
    await client.post(f"/api/v1/invites/{invite}/join", headers=friend)
    await client.post(f"/api/v1/rooms/{room['code']}/requests", json={}, headers=madina)

    assert (await client.delete(f"/api/v1/rooms/{room['code']}", headers=org)).status_code == 204
    assert "room_cancelled" in await pushes(friend_id)
    assert "room_cancelled" in await pushes(madina_id)
    posts = await jobs("post")
    assert len(posts) == 1 and posts[0].payload == {"text": "cancelled"}
    mine = (await client.get("/api/v1/me/rooms", headers=friend)).json()
    assert mine[0]["status"] == "cancelled"


async def test_новая_ссылка_гасит_старую(client):
    org, _ = await person()
    room = await open_room(client, org, is_open=False)
    old = room["invite_url"].rsplit("/", 1)[1]
    fresh = (await client.post(f"/api/v1/rooms/{room['code']}/invite/reset", headers=org)).json()
    assert fresh["invite_url"].rsplit("/", 1)[1] != old
    friend, _ = await person(name="Друг")
    assert (await client.get(f"/api/v1/invites/{old}", headers=friend)).status_code == 404


# --- Блокировки и жалобы -----------------------------------------------------


async def test_блокировка_скрывает_в_обе_стороны(client):
    org, org_id = await person()
    room = await open_room(client, org)
    madina, madina_id = await person(name="Мадина")
    await client.post("/api/v1/blocks", json={"user_id": org_id}, headers=madina)

    listing = (await client.get("/api/v1/places/test-peak/rooms", headers=madina)).json()
    assert listing["rooms"] == []
    assert (await client.get(f"/api/v1/rooms/{room['code']}", headers=madina)).status_code == 404
    req = await client.post(f"/api/v1/rooms/{room['code']}/requests", json={}, headers=madina)
    assert req.status_code == 404
    blocked = (await client.get("/api/v1/blocks", headers=madina)).json()
    assert [b["user_id"] for b in blocked] == [org_id]
    await client.delete(f"/api/v1/blocks/{org_id}", headers=madina)
    assert (await client.get(f"/api/v1/rooms/{room['code']}", headers=madina)).status_code == 200


async def test_блокировка_в_общей_комнате_убирает(client):
    org, _ = await person()
    room = await open_room(client, org, is_open=False)
    friend, friend_id = await person(name="Друг")
    invite = room["invite_url"].rsplit("/", 1)[1]
    await client.post(f"/api/v1/invites/{invite}/join", headers=friend)
    await client.post("/api/v1/blocks", json={"user_id": friend_id}, headers=org)
    view = (await client.get(f"/api/v1/rooms/{room['code']}", headers=org)).json()
    assert view["people"] == 1


async def test_жалоба_сохраняется(client):
    org, org_id = await person()
    room = await open_room(client, org)
    madina, _ = await person(name="Мадина")
    resp = await client.post(
        "/api/v1/reports",
        json={"user_id": org_id, "room": room["code"], "reason": "fake", "text": "чужие фото"},
        headers=madina,
    )
    assert resp.status_code == 204
    async with SessionLocal() as session:
        r = (await session.execute(select(RoomReport))).scalar_one()
        assert r.target_user_id == org_id and r.reason == "fake"


async def test_запрещённого_нет_в_поиске(client):
    org, org_id = await person()
    await open_room(client, org)
    async with SessionLocal() as session:
        (await session.get(User, org_id)).companions_banned_at = datetime.now(timezone.utc)
        await session.commit()
    viewer, _ = await person(name="Мадина")
    assert (await client.get("/api/v1/places/test-peak/rooms", headers=viewer)).json()["rooms"] == []


# --- Удаление аккаунта и уборка -----------------------------------------------


async def test_удаление_организатора_отменяет_поход(client):
    org, org_id = await person()
    room = await open_room(client, org)
    friend, friend_id = await person(name="Друг")
    invite = room["invite_url"].rsplit("/", 1)[1]
    await client.post(f"/api/v1/invites/{invite}/join", headers=friend)
    assert (await client.delete("/api/v1/me", headers=org)).status_code == 204
    assert "room_cancelled" in await pushes(friend_id)
    async with SessionLocal() as session:
        r = (await session.execute(select(Room).where(Room.code == room["code"]))).scalar_one()
        assert r.status == "cancelled" and r.organizer_id is None


async def test_уборка_архивирует_и_выводит_из_группы(client):
    org, _ = await person()
    room = await open_room(client, org)
    await set_room(room["code"], tg_state="ready", tg_chat_id=-100123)
    async with SessionLocal() as session:
        await rooms_api.housekeeping(session, DAY + timedelta(days=14))
        await session.commit()
    async with SessionLocal() as session:
        r = (await session.execute(select(Room).where(Room.code == room["code"]))).scalar_one()
        assert r.status == "active"
    async with SessionLocal() as session:
        await rooms_api.housekeeping(session, DAY + timedelta(days=15))
        await session.commit()
    async with SessionLocal() as session:
        r = (await session.execute(select(Room).where(Room.code == room["code"]))).scalar_one()
        assert r.status == "archived"
    leaves = await jobs("leave")
    assert len(leaves) == 1 and leaves[0].payload == {"text": "farewell"}


# --- Личные пуши ----------------------------------------------------------------


async def test_личный_пуш_уходит_на_устройства_человека(client):
    org, org_id = await person(device="org-phone")
    room = await open_room(client, org)
    async with SessionLocal() as session:
        session.add(PushToken(token="ios-token", platform="ios", device="org-phone", lang="uz"))
        session.add(PushToken(token="stranger", platform="ios", device="other-phone"))
        await session.commit()
    madina, _ = await person(name="Мадина")
    await client.post(f"/api/v1/rooms/{room['code']}/requests", json={}, headers=madina)

    calls = []

    async def ios(token, title, body, slug, announcement_id, extra=None, channel=None):
        calls.append((token, title, body, announcement_id, extra, channel))
        return SendResult(ok=True)

    async with SessionLocal() as session:
        assert await send_outbox(session, {"ios": ios}) == 1
    assert len(calls) == 1
    token, title, body, announcement_id, extra, channel = calls[0]
    assert token == "ios-token"
    assert title == "Мадина siz bilan bormoqchi"
    assert body.endswith(f"{DAY.day}-{['yanvar','fevral','mart','aprel','may','iyun','iyul','avgust','sentabr','oktabr','noyabr','dekabr'][DAY.month-1]}")
    assert announcement_id is None and extra == {"room": room["code"]}
    assert channel == "rooms"
    async with SessionLocal() as session:
        assert await send_outbox(session, {"ios": ios}) == 0


def test_тексты_пушей_на_русском():
    title, body = render(
        "room_approved", {"place": "Большой Чимган", "day": "2026-09-25"}, "ru"
    )
    assert title == "Вас взяли в компанию"
    assert body == "Большой Чимган, 25 сентября"


# --- Ссылки, открывающие приложение ---------------------------------------------


async def test_страница_приглашения(client):
    org, _ = await person()
    room = await open_room(client, org, is_open=False)
    invite = room["invite_url"].rsplit("/", 1)[1]
    page = await client.get(f"/r/{invite}")
    assert page.status_code == 200
    assert "Азиз зовёт на Тестовый пик" in page.text
    assert f"sayr://invite/{invite}" in page.text
    assert (await client.get("/r/nothing")).status_code == 404


async def test_файлы_универсальных_ссылок(client):
    aasa = (await client.get("/.well-known/apple-app-site-association")).json()
    detail = aasa["applinks"]["details"][0]
    assert detail["appIDs"] == ["Z39Z5TJZCG.uz.sayr.ios"]
    assert {"/": "/r/*"} in detail["components"] and {"/": "/p/*"} in detail["components"]
    links = (await client.get("/.well-known/assetlinks.json")).json()
    assert links[0]["target"]["package_name"] == "uz.sayr.android"
    prints = links[0]["target"]["sha256_cert_fingerprints"]
    # Ключ загрузки и оба ключа подписи Play — классический и постквантовый
    assert [p[:8] for p in prints] == ["8E:08:07", "09:8D:22", "E1:3A:97"]
    assert all(len(p.split(":")) == 32 for p in prints)


async def test_запрет_попутчиков_убирает_отовсюду(client):
    org, org_id = await person()
    own = await open_room(client, org)
    other_org, _ = await person(name="Мадина")
    other = await open_room(client, other_org, is_open=False)
    # Тот же человек — участник чужой комнаты на другой день
    await set_room(other["code"], day=DAY + timedelta(days=3))
    invite = other["invite_url"].rsplit("/", 1)[1]
    await client.post(f"/api/v1/invites/{invite}/join", headers=org)

    async with SessionLocal() as session:
        await rooms_api.ban_companions(session, await session.get(User, org_id))
        await session.commit()
    mine = (await client.get("/api/v1/me/rooms", headers=org)).json()
    assert [r["status"] for r in mine] == ["cancelled"]
    assert mine[0]["code"] == own["code"]
    resp = await client.post(
        "/api/v1/rooms",
        json={"place": "test-lake", "day": (DAY + timedelta(days=9)).isoformat()},
        headers=org,
    )
    assert resp.status_code == 403 and resp.json()["detail"] == "companions_banned"


async def test_комнаты_и_жалобы_открываются_в_админке(client, admin_client):
    org, org_id = await person()
    room = await open_room(client, org)
    madina, _ = await person(name="Мадина")
    await client.post(
        "/api/v1/reports",
        json={"user_id": org_id, "room": room["code"], "reason": "spam"},
        headers=madina,
    )
    assert (await admin_client.get("/admin/room/list")).status_code == 200
    reports = await admin_client.get("/admin/room-report/list")
    assert reports.status_code == 200 and "spam" in reports.text
