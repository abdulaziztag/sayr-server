"""Профиль, фото, удаление аккаунта, синхронизация своего и планы по человеку.

Вход здесь не проверяется (это test_auth.py) — нужен только токен, поэтому
человек и сессия заводятся напрямую в базе.
"""

import io
from datetime import date, datetime, timedelta, timezone

import pytest
from PIL import Image
from sqlalchemy import delete, select

from app.auth.tokens import new_token
from app.config import AVATARS_DIR
from app.db import SessionLocal
from app.models import (
    Place,
    TripIntent,
    User,
    UserFavorite,
    UserSession,
    UserSetting,
    UserTripDay,
)

NOW = datetime.now(timezone.utc)


async def _login(phone: str = "+998901234567") -> tuple[str, int]:
    """Человек с готовым токеном: вход уже проверен в другом файле."""
    token, digest = new_token()
    async with SessionLocal() as session:
        user = User(phone=phone)
        session.add(user)
        await session.flush()
        session.add(UserSession(user_id=user.id, token_hash=digest, device_id="device-0001"))
        await session.commit()
        return token, user.id


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Device-Id": "device-0001"}


@pytest.fixture(autouse=True)
async def clean():
    async with SessionLocal() as session:
        await session.execute(delete(TripIntent))
        await session.execute(delete(UserSession))
        await session.execute(delete(UserFavorite))
        await session.execute(delete(UserTripDay))
        await session.execute(delete(UserSetting))
        await session.execute(delete(User))
        await session.commit()


def _jpeg(side: int = 900) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (side, side), (47, 93, 63)).save(buf, "JPEG")
    return buf.getvalue()


# --- Профиль --------------------------------------------------------------


async def test_анкета_пустая_и_правится_по_одному_полю(client):
    token, _ = await _login()

    me = await client.get("/api/v1/me", headers=_auth(token))
    assert me.status_code == 200
    assert me.json()["first_name"] == ""
    assert me.json()["profile_filled"] is False

    resp = await client.patch(
        "/api/v1/me", json={"first_name": "Абдулазиз"}, headers=_auth(token)
    )
    assert resp.status_code == 200
    assert resp.json()["first_name"] == "Абдулазиз"
    assert resp.json()["profile_filled"] is True

    # Второе поле не стирает первое
    resp = await client.patch("/api/v1/me", json={"gender": "male"}, headers=_auth(token))
    assert resp.json()["first_name"] == "Абдулазиз"
    assert resp.json()["gender"] == "male"


async def test_ник_телеграма_чистится_и_проверяется(client):
    token, _ = await _login()
    ok = await client.patch(
        "/api/v1/me", json={"telegram_username": " @manopov "}, headers=_auth(token)
    )
    assert ok.json()["telegram_username"] == "manopov"

    bad = await client.patch(
        "/api/v1/me", json={"telegram_username": "не ник"}, headers=_auth(token)
    )
    assert bad.status_code == 422


async def test_снятые_поля_анкеты_стираются(client):
    """Анкета приходит целиком: снятый год, пол и ник должны исчезнуть,
    а не остаться на сервере и вернуться при следующем открытии"""
    token, _ = await _login()
    await client.patch(
        "/api/v1/me",
        json={"gender": "female", "birth_year": 1995, "telegram_username": "manopov"},
        headers=_auth(token),
    )
    resp = await client.patch(
        "/api/v1/me",
        json={"gender": None, "birth_year": None, "telegram_username": ""},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["gender"] is None
    assert body["birth_year"] is None
    assert body["telegram_username"] is None


async def test_год_рождения_с_опечаткой_не_принимается(client):
    token, _ = await _login()
    assert (
        await client.patch("/api/v1/me", json={"birth_year": 1080}, headers=_auth(token))
    ).status_code == 422
    assert (
        await client.patch("/api/v1/me", json={"birth_year": 3000}, headers=_auth(token))
    ).status_code == 422
    good = await client.patch(
        "/api/v1/me", json={"birth_year": 1995}, headers=_auth(token)
    )
    assert good.json()["birth_year"] == 1995


async def test_без_токена_профиль_не_отдаётся(client):
    assert (await client.get("/api/v1/me")).status_code == 401
    assert (
        await client.get("/api/v1/me", headers={"Authorization": "Bearer nope"})
    ).status_code == 401


async def test_фото_ужимается_и_меняется(client):
    token, user_id = await _login()
    resp = await client.post(
        "/api/v1/me/avatar",
        files={"file": ("photo.jpg", _jpeg(), "image/jpeg")},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    url = resp.json()["avatar_url"]
    assert url and url.startswith("/media/avatars/")

    saved = AVATARS_DIR / url.rsplit("/", 1)[-1]
    assert saved.exists()
    with Image.open(saved) as im:
        assert max(im.size) == 512

    # Новое фото приходит на смену старому, и старый файл уходит с диска
    second = await client.post(
        "/api/v1/me/avatar",
        files={"file": ("other.jpg", _jpeg(700), "image/jpeg")},
        headers=_auth(token),
    )
    assert second.json()["avatar_url"] != url
    assert not saved.exists()


async def test_не_картинка_отбивается(client):
    token, _ = await _login()
    resp = await client.post(
        "/api/v1/me/avatar",
        files={"file": ("track.gpx", b"<gpx></gpx>", "application/gpx+xml")},
        headers=_auth(token),
    )
    assert resp.status_code == 415


async def test_удаление_аккаунта_уносит_всё_своё(client):
    token, user_id = await _login()
    await client.post(
        "/api/v1/me/avatar",
        files={"file": ("photo.jpg", _jpeg(), "image/jpeg")},
        headers=_auth(token),
    )
    async with SessionLocal() as session:
        place_id = (await session.execute(select(Place.id).limit(1))).scalar_one()
        session.add(UserFavorite(user_id=user_id, place_id=place_id, updated_at=NOW))
        session.add(
            UserTripDay(
                user_id=user_id,
                place_id=place_id,
                day=date.today(),
                outcome="went",
                updated_at=NOW,
            )
        )
        session.add(
            TripIntent(
                place_id=place_id, day=date.today(), device_id="device-0001", user_id=user_id
            )
        )
        await session.commit()
        avatar = (await session.get(User, user_id)).avatar.name

    resp = await client.delete("/api/v1/me", headers=_auth(token))
    assert resp.status_code == 204

    async with SessionLocal() as session:
        assert await session.get(User, user_id) is None
        for model in (UserSession, UserFavorite, UserTripDay, TripIntent):
            assert (await session.execute(select(model))).first() is None
    assert not (AVATARS_DIR / avatar).exists()
    # Токен после удаления не работает
    assert (await client.get("/api/v1/me", headers=_auth(token))).status_code == 401


# --- Синхронизация --------------------------------------------------------


async def _slugs() -> list[str]:
    async with SessionLocal() as session:
        return list(
            (await session.execute(select(Place.slug).order_by(Place.id).limit(2)))
            .scalars()
            .all()
        )


async def test_первый_вход_забирает_всё_локальное(client):
    token, _ = await _login()
    one, two = await _slugs()
    body = {
        "since": None,
        "favorites": [{"slug": one, "updated_at": NOW.isoformat()}],
        "trip_days": [
            {
                "slug": two,
                "day": "2026-09-12",
                "outcome": "went",
                "pace": "slower",
                "distance_km": 7.2,
                "elevation_gain_m": 1744,
                "updated_at": NOW.isoformat(),
            }
        ],
        "settings": {"departure_city": "tashkent", "updated_at": NOW.isoformat()},
    }
    resp = await client.post("/api/v1/sync", json=body, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert {f["slug"] for f in out["favorites"]} == {one}
    assert out["trip_days"][0]["distance_km"] == 7.2
    assert out["settings"]["departure_city"] == "tashkent"
    assert out["now"]


async def test_второй_телефон_получает_то_же_самое(client):
    token, user_id = await _login()
    one, _ = await _slugs()
    await client.post(
        "/api/v1/sync",
        json={"favorites": [{"slug": one, "updated_at": NOW.isoformat()}]},
        headers=_auth(token),
    )

    # Второй телефон того же человека: свой токен, пустой первый запрос
    second, digest = new_token()
    async with SessionLocal() as session:
        session.add(UserSession(user_id=user_id, token_hash=digest, device_id="device-0002"))
        await session.commit()

    resp = await client.post(
        "/api/v1/sync", json={}, headers={"Authorization": f"Bearer {second}"}
    )
    assert [f["slug"] for f in resp.json()["favorites"]] == [one]


async def test_свежая_правка_побеждает_старую(client):
    token, _ = await _login()
    one, _ = await _slugs()
    old = NOW - timedelta(hours=2)

    await client.post(
        "/api/v1/sync",
        json={"favorites": [{"slug": one, "updated_at": NOW.isoformat()}]},
        headers=_auth(token),
    )
    # Старое снятие сердечка не отменяет свежую постановку
    stale = await client.post(
        "/api/v1/sync",
        json={
            "favorites": [{"slug": one, "updated_at": old.isoformat(), "deleted": True}]
        },
        headers=_auth(token),
    )
    assert stale.json()["favorites"][0]["deleted"] is False

    # А свежее снятие — отменяет, и уезжает отметкой, а не пропажей строки
    fresh = await client.post(
        "/api/v1/sync",
        json={
            "favorites": [
                {
                    "slug": one,
                    "updated_at": (NOW + timedelta(minutes=1)).isoformat(),
                    "deleted": True,
                }
            ]
        },
        headers=_auth(token),
    )
    assert fresh.json()["favorites"][0]["deleted"] is True


async def test_since_отдаёт_только_новое(client):
    token, user_id = await _login()
    one, two = await _slugs()
    first = await client.post(
        "/api/v1/sync",
        json={"favorites": [{"slug": one, "updated_at": NOW.isoformat()}]},
        headers=_auth(token),
    )
    mark = first.json()["now"]

    later = NOW + timedelta(minutes=5)
    resp = await client.post(
        "/api/v1/sync",
        json={
            "since": mark,
            "favorites": [{"slug": two, "updated_at": later.isoformat()}],
        },
        headers=_auth(token),
    )
    assert [f["slug"] for f in resp.json()["favorites"]] == [two]


async def test_место_снятое_с_публикации_пропускается(client):
    token, _ = await _login()
    resp = await client.post(
        "/api/v1/sync",
        json={"favorites": [{"slug": "нет-такого-места", "updated_at": NOW.isoformat()}]},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["favorites"] == []


async def test_синхронизация_требует_входа(client):
    assert (await client.post("/api/v1/sync", json={})).status_code == 401


# --- Планы по человеку ----------------------------------------------------


async def test_план_вошедшего_виден_со_второго_телефона(client):
    token, user_id = await _login()
    one, _ = await _slugs()
    day = (date.today() + timedelta(days=3)).isoformat()

    added = await client.post(
        f"/api/v1/places/{one}/intents",
        json={"date": day, "device_id": "device-0001"},
        headers=_auth(token),
    )
    assert added.status_code == 200
    assert any(d["date"] == day and d["mine"] for d in added.json()["days"])

    # Второе устройство того же человека видит свой план как свой
    second, digest = new_token()
    async with SessionLocal() as session:
        session.add(UserSession(user_id=user_id, token_hash=digest, device_id="device-0002"))
        await session.commit()
    seen = await client.get(
        f"/api/v1/places/{one}/intents?device_id=device-0002",
        headers={"Authorization": f"Bearer {second}"},
    )
    assert any(d["date"] == day and d["mine"] for d in seen.json()["days"])

    # А гость с того же телефона своим его не считает
    guest = await client.get(f"/api/v1/places/{one}/intents?device_id=device-0001")
    assert all(not d["mine"] for d in guest.json()["days"])


async def test_гость_продолжает_работать_без_токена(client):
    one, _ = await _slugs()
    day = (date.today() + timedelta(days=4)).isoformat()
    added = await client.post(
        f"/api/v1/places/{one}/intents", json={"date": day, "device_id": "guest-device"}
    )
    assert added.status_code == 200
    assert any(d["date"] == day and d["mine"] for d in added.json()["days"])

    async with SessionLocal() as session:
        row = (await session.execute(select(TripIntent))).scalar_one()
    assert row.user_id is None
