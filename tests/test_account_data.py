"""Профиль, фото, удаление аккаунта, синхронизация своего и планы по человеку.

Вход здесь не проверяется (это test_auth.py) — нужен только токен, поэтому
человек и сессия заводятся напрямую в базе.
"""

import asyncio
import io
from datetime import date, datetime, timedelta, timezone

import pytest
from PIL import Image
from sqlalchemy import delete, func, select

from app.api import sync as sync_api
from app.auth.tokens import new_token
from app.config import AVATARS_DIR
from app.db import SessionLocal, engine
from app.models import (
    LoginRequest,
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
        await session.execute(delete(LoginRequest))
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


async def test_стёртое_имя_становится_пустым_а_не_роняет_ручку(client):
    token, _ = await _login()
    await client.patch(
        "/api/v1/me", json={"first_name": "Азиз", "last_name": "Каримов"}, headers=_auth(token)
    )
    resp = await client.patch("/api/v1/me", json={"last_name": None}, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["last_name"] == ""
    assert resp.json()["first_name"] == "Азиз"


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


async def test_удаление_аккаунта_стирает_номер_из_заявок_на_вход(client):
    """Заявки на код с человеком внешним ключом не связаны: номер из них
    удаление стирает явно. Сами заявки остаются — по ним считаются лимиты
    адреса и устройства, и «вошёл — удалился» их не обнуляет"""
    token, _ = await _login()
    expires = NOW + timedelta(minutes=5)
    rows = (
        ("+998901234567", "verified"),
        ("+998901234567", "sent"),
        ("+998907654321", "sent"),
    )
    async with SessionLocal() as session:
        for n, (phone, status) in enumerate(rows):
            session.add(
                LoginRequest(
                    id=f"00000000-0000-0000-0000-00000000000{n}",
                    phone=phone,
                    channel="telegram",
                    gateway_request_id=f"gw-{n}",
                    status=status,
                    expires_at=expires,
                    device_id="device-0001",
                    ip="203.0.113.7",
                )
            )
        await session.commit()

    assert (await client.delete("/api/v1/me", headers=_auth(token))).status_code == 204

    async with SessionLocal() as session:
        left = (
            await session.execute(select(LoginRequest).order_by(LoginRequest.id))
        ).scalars().all()
    assert [(r.phone, r.gateway_request_id, r.status) for r in left] == [
        ("", None, "verified"),
        # Живая заявка без номера никого не впустит
        ("", None, "expired"),
        # Чужой номер не задет
        ("+998907654321", "gw-2", "sent"),
    ]
    assert {r.ip for r in left} == {"203.0.113.7"}


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


async def _second_phone(user_id: int) -> dict:
    """Второй телефон того же человека: свой токен"""
    token, digest = new_token()
    async with SessionLocal() as session:
        session.add(UserSession(user_id=user_id, token_hash=digest, device_id="device-0002"))
        await session.commit()
    return {"Authorization": f"Bearer {token}", "X-Device-Id": "device-0002"}


async def test_правка_без_связи_доезжает_до_второго_телефона(client):
    """В горах правят без связи, а уезжает правка позже. По часам телефона
    она старше метки второго телефона — новое отбирается по времени записи
    на сервере, иначе такая правка до второго телефона не доезжала никогда"""
    token, user_id = await _login()
    _, two = await _slugs()
    mark = (await client.post("/api/v1/sync", json={}, headers=_auth(token))).json()["now"]

    offline = datetime.fromisoformat(mark) - timedelta(hours=3)
    other = await _second_phone(user_id)
    sent = await client.post(
        "/api/v1/sync",
        json={"favorites": [{"slug": two, "updated_at": offline.isoformat()}]},
        headers=other,
    )
    assert sent.status_code == 200, sent.text

    resp = await client.post("/api/v1/sync", json={"since": mark}, headers=_auth(token))
    assert [f["slug"] for f in resp.json()["favorites"]] == [two]


async def test_часы_из_будущего_не_выигрывают_навсегда(client):
    """Телефон с часами на год вперёд выигрывал бы у всех правок этого года:
    время из будущего срезается до времени сервера"""
    token, user_id = await _login()
    one, _ = await _slugs()
    future = datetime.now(timezone.utc) + timedelta(days=365)
    first = await client.post(
        "/api/v1/sync",
        json={"favorites": [{"slug": one, "updated_at": future.isoformat()}]},
        headers=_auth(token),
    )
    stored = datetime.fromisoformat(first.json()["favorites"][0]["updated_at"])
    assert stored <= datetime.fromisoformat(first.json()["now"])

    other = await _second_phone(user_id)
    resp = await client.post(
        "/api/v1/sync",
        json={
            "favorites": [
                {
                    "slug": one,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "deleted": True,
                }
            ]
        },
        headers=other,
    )
    assert resp.json()["favorites"][0]["deleted"] is True


async def test_время_без_зоны_не_роняет_сверку(client):
    """Строка времени без зоны считается UTC: сравнение с временем из базы
    иначе падало на наивном и осведомлённом времени"""
    token, _ = await _login()
    one, _ = await _slugs()
    await client.post(
        "/api/v1/sync",
        json={"favorites": [{"slug": one, "updated_at": NOW.isoformat()}]},
        headers=_auth(token),
    )
    naive = (NOW + timedelta(minutes=1)).replace(tzinfo=None).isoformat()
    since = (NOW - timedelta(minutes=1)).replace(tzinfo=None).isoformat()
    resp = await client.post(
        "/api/v1/sync",
        json={"since": since, "favorites": [{"slug": one, "updated_at": naive, "deleted": True}]},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["favorites"][0]["deleted"] is True


async def test_первая_сверка_с_двух_телефонов_разом(client, monkeypatch):
    """Оба телефона отдают одно и то же гостевое избранное одновременно:
    второй не должен падать на уникальности избранного и настроек"""
    token, user_id = await _login()
    one, two = await _slugs()
    other = await _second_phone(user_id)

    # Оба запроса доходят до записи вместе, а не по очереди
    barrier = asyncio.Barrier(2)
    slug_to_id = sync_api._slug_to_id

    async def together(session, slugs):
        ids = await slug_to_id(session, slugs)
        await barrier.wait()
        return ids

    monkeypatch.setattr(sync_api, "_slug_to_id", together)
    body = {
        "favorites": [{"slug": one, "updated_at": NOW.isoformat()}],
        "trip_days": [{"slug": two, "day": "2026-09-12", "updated_at": NOW.isoformat()}],
        "settings": {"departure_city": "tashkent", "updated_at": NOW.isoformat()},
    }
    a, b = await asyncio.gather(
        client.post("/api/v1/sync", json=body, headers=_auth(token)),
        client.post("/api/v1/sync", json=body, headers=other),
    )
    assert a.status_code == 200, a.text
    assert b.status_code == 200, b.text
    async with SessionLocal() as session:
        for model in (UserFavorite, UserTripDay, UserSetting):
            count = (await session.execute(select(func.count()).select_from(model))).scalar_one()
            assert count == 1, model.__name__


# --- План в записи дня: длина похода и час выезда ---------------------------


async def test_миграция_0035_откатывается_и_накатывается():
    """Длина похода и час выезда — колонками миграции 0035: откат убирает
    их, не трогая самих записей дня, накат возвращает пустыми, голова одна"""
    import importlib.util
    from pathlib import Path

    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory
    from sqlalchemy import Integer, inspect, text

    versions = Path(__file__).parent.parent / "alembic"
    config = Config()
    config.set_main_option("script_location", str(versions))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert script.get_revision("0035").down_revision == "0034"

    spec = importlib.util.spec_from_file_location(
        "migration_0035", versions / "versions" / "0035_trip_day_plan.py"
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def plan_columns(conn) -> dict:
        return {
            c["name"]: c
            for c in inspect(conn).get_columns("user_trip_days")
            if c["name"] in ("days", "depart_minutes")
        }

    def roundtrip(conn) -> None:
        user_id = conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id")).scalar_one()
        conn.execute(
            text(
                "INSERT INTO user_trip_days"
                " (user_id, place_id, day, outcome, days, depart_minutes)"
                " SELECT :user, id, '2026-10-10', 'planned', 3, 120 FROM places LIMIT 1"
            ),
            {"user": user_id},
        )
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
            assert plan_columns(conn) == {}
            migration.upgrade()
        columns = plan_columns(conn)
        assert set(columns) == {"days", "depart_minutes"}
        assert all(isinstance(c["type"], Integer) and c["nullable"] for c in columns.values())
        # Запись дня пережила откат, а длина и час после наката пустые —
        # так выглядят и строки, записанные до миграции
        row = conn.execute(
            text("SELECT outcome, days, depart_minutes FROM user_trip_days WHERE user_id = :user"),
            {"user": user_id},
        ).one()
        assert tuple(row) == ("planned", None, None)

    # Одной транзакцией с откатом: ни схема, ни строки тестовой базы
    # не меняются при любом исходе
    async with engine.connect() as conn:
        await conn.run_sync(roundtrip)
        await conn.rollback()


def _trip_day(slug: str, at: datetime, day: str = "2026-10-10", **fields) -> dict:
    """Запись дня с телефона. Без days и depart_minutes — как от сборки,
    которая этих полей не знает"""
    return {"slug": slug, "day": day, "updated_at": at.isoformat(), **fields}


def _plan(out: dict) -> tuple:
    """Длина и час единственной записи дня в ответе сверки"""
    [day] = out["trip_days"]
    return day["days"], day["depart_minutes"]


async def test_длина_похода_и_час_выезда_доезжают_до_второго_телефона(client):
    """По ним вход восстанавливает план: многодневка возвращается
    многодневкой, с часом выезда из плана"""
    token, user_id = await _login()
    _, two = await _slugs()
    sent = await client.post(
        "/api/v1/sync",
        json={"trip_days": [_trip_day(two, NOW, days=3, depart_minutes=120)]},
        headers=_auth(token),
    )
    assert sent.status_code == 200, sent.text
    assert _plan(sent.json()) == (3, 120)

    resp = await client.post("/api/v1/sync", json={}, headers=await _second_phone(user_id))
    assert _plan(resp.json()) == (3, 120)


async def test_сборка_без_плана_его_не_стирает(client):
    """Старые сборки шлют запись дня без длины и часа. Их правка свежее —
    исход меняется, а план, записанный новым телефоном, остаётся: иначе
    отметка «Были» со старого телефона превращала бы многодневку в однодневку"""
    token, user_id = await _login()
    _, two = await _slugs()
    old = await _second_phone(user_id)
    base = NOW - timedelta(hours=1)

    # Запись, заведённая старой сборкой: длины и часа нет, ключи в ответе пустые
    created = await client.post(
        "/api/v1/sync", json={"trip_days": [_trip_day(two, base)]}, headers=old
    )
    assert _plan(created.json()) == (None, None)

    await client.post(
        "/api/v1/sync",
        json={
            "trip_days": [
                _trip_day(two, base + timedelta(minutes=10), days=3, depart_minutes=120)
            ]
        },
        headers=_auth(token),
    )
    resp = await client.post(
        "/api/v1/sync",
        json={
            "trip_days": [
                _trip_day(two, base + timedelta(minutes=20), outcome="went", pace="slower")
            ]
        },
        headers=old,
    )
    assert resp.status_code == 200, resp.text
    [day] = resp.json()["trip_days"]
    assert (day["outcome"], day["pace"]) == ("went", "slower")
    assert (day["days"], day["depart_minutes"]) == (3, 120)
    async with SessionLocal() as session:
        row = (await session.execute(select(UserTripDay))).scalar_one()
    assert (row.outcome, row.days, row.depart_minutes) == ("went", 3, 120)


async def test_null_стирает_план_а_старая_правка_его_не_трогает(client):
    """Длина и час правятся, как вся запись: побеждает свежая правка. Ключ
    с null стирает значение — так план меняют на расчётный выход, —
    а отсутствующий ключ оставляет лежащее, у каждого поля отдельно"""
    token, _ = await _login()
    _, two = await _slugs()
    base = NOW - timedelta(hours=1)

    async def send(minutes: int, **fields) -> tuple:
        resp = await client.post(
            "/api/v1/sync",
            json={"trip_days": [_trip_day(two, base + timedelta(minutes=minutes), **fields)]},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        return _plan(resp.json())

    assert await send(10, days=3, depart_minutes=120) == (3, 120)
    # Правка старше записанной проигрывает целиком
    assert await send(0, days=2, depart_minutes=60) == (3, 120)
    # Пришла только длина — час остаётся
    assert await send(20, days=2) == (2, 120)
    assert await send(30, days=None, depart_minutes=None) == (None, None)


@pytest.mark.parametrize(
    "field, value",
    [("days", 0), ("days", 31), ("depart_minutes", -1), ("depart_minutes", 24 * 60)],
)
async def test_длина_и_час_вне_пределов_не_принимаются(client, field, value):
    """Длина — от 1 до 30 дней, час — минуты от полуночи, 0..1439. Остальное —
    ошибка клиента: сверка отвечает 422 и не записывает из запроса ничего"""
    token, _ = await _login()
    one, two = await _slugs()
    resp = await client.post(
        "/api/v1/sync",
        json={
            "favorites": [{"slug": one, "updated_at": NOW.isoformat()}],
            "trip_days": [_trip_day(two, NOW, **{field: value})],
        },
        headers=_auth(token),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "trip_days", 0, field]
    async with SessionLocal() as session:
        for model in (UserFavorite, UserTripDay):
            assert (await session.execute(select(model))).first() is None


async def test_крайние_длина_и_час_принимаются(client):
    token, _ = await _login()
    _, two = await _slugs()
    resp = await client.post(
        "/api/v1/sync",
        json={
            "trip_days": [
                _trip_day(two, NOW, days=1, depart_minutes=0),
                _trip_day(two, NOW, day="2026-10-11", days=30, depart_minutes=24 * 60 - 1),
            ]
        },
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    got = sorted((d["day"], d["days"], d["depart_minutes"]) for d in resp.json()["trip_days"])
    assert got == [("2026-10-10", 1, 0), ("2026-10-11", 30, 1439)]


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
