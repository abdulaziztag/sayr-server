"""Пуши по расписанию: регистрация токенов и планировщик на подменных отправителях."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.auth.tokens import new_token
from app.db import SessionLocal
from app.models import Announcement, AnnouncementStatus, PushToken, User, UserSession
from app.push import SendResult, outbox
from app.push.sender import run_once

TOKEN_A = "a" * 64
TOKEN_B = "b" * 64
TOKEN_C = "c" * 152  # FCM-токены длинные
PHONES = "+998970"


async def _clear() -> None:
    async with SessionLocal() as session:
        for row in (await session.execute(select(PushToken))).scalars():
            await session.delete(row)
        for row in (await session.execute(select(Announcement))).scalars():
            await session.delete(row)
        # Люди из тестов привязки — со своей приставкой номера; сессии
        # и очередь пушей уходят за ними каскадом
        await session.execute(delete(User).where(User.phone.like(f"{PHONES}%")))
        await session.commit()


async def _token(token: str) -> PushToken | None:
    async with SessionLocal() as session:
        return await session.get(PushToken, token)


@pytest.fixture(autouse=True)
async def clean():
    await _clear()
    yield
    await _clear()


async def test_register_then_update_and_revive(client):
    resp = await client.post(
        "/api/v1/push/devices",
        json={"token": TOKEN_A, "platform": "ios", "lang": "ru", "city": "chirchik", "app_version": "1.2.3"},
        headers={"X-Device-Id": "dev-1"},
    )
    assert resp.status_code == 204
    row = await _token(TOKEN_A)
    assert row.platform == "ios" and row.city == "chirchik" and row.device == "dev-1"

    # Погасили — а установка снова прислала тот же токен: он живой
    async with SessionLocal() as session:
        row = await session.get(PushToken, TOKEN_A)
        row.disabled_at = datetime.now().astimezone()
        row.disabled_reason = "test"
        await session.commit()
    resp = await client.post(
        "/api/v1/push/devices", json={"token": TOKEN_A, "platform": "ios", "lang": "uz"}
    )
    assert resp.status_code == 204
    row = await _token(TOKEN_A)
    assert row.lang == "uz" and row.disabled_at is None and row.disabled_reason is None


async def test_garbage_is_rejected(client):
    resp = await client.post("/api/v1/push/devices", json={"token": "short", "platform": "ios"})
    assert resp.status_code == 422
    resp = await client.post("/api/v1/push/devices", json={"token": TOKEN_A, "platform": "windows"})
    assert resp.status_code == 422


async def test_forget(client):
    await client.post("/api/v1/push/devices", json={"token": TOKEN_A, "platform": "android"})
    resp = await client.delete(f"/api/v1/push/devices/{TOKEN_A}")
    assert resp.status_code == 204
    assert await _token(TOKEN_A) is None


async def _seed(now: datetime) -> None:
    async with SessionLocal() as session:
        session.add_all([
            PushToken(token=TOKEN_A, platform="ios", lang="ru"),
            PushToken(token=TOKEN_B, platform="ios", lang="uz"),
            PushToken(token=TOKEN_C, platform="android", lang="ru"),
            PushToken(token="d" * 64, platform="ios", lang="ru", disabled_at=now.astimezone()),
            Announcement(title="Созрело", body="пора", send_at=now - timedelta(minutes=1)),
            Announcement(title="Рано", body="ещё нет", send_at=now + timedelta(hours=1)),
        ])
        await session.commit()


async def test_sends_only_ripe_and_disables_dead_tokens():
    now = datetime(2026, 9, 5, 12, 0)
    await _seed(now)
    calls: list[tuple[str, str]] = []

    async def ios(token, title, body, slug, announcement_id):
        calls.append((token, title))
        # Номер рассылки едет всегда — по нему приложение сообщает открытие
        assert isinstance(announcement_id, int) and announcement_id > 0
        # Вторая установка снесла приложение — Apple отвечает 410
        if token == TOKEN_B:
            return SendResult(ok=False, invalid_token=True, error="apns 410 Unregistered")
        return SendResult(ok=True)

    async def android(token, title, body, slug, announcement_id):
        calls.append((token, title))
        return SendResult(ok=True)

    async with SessionLocal() as session:
        done = await run_once(session, {"ios": ios, "android": android}, now=now)

    assert [a.title for a in done] == ["Созрело"]
    # Погашенный токен не трогали, остальным ушло
    assert sorted(t for t, _ in calls) == sorted([TOKEN_A, TOKEN_B, TOKEN_C])
    ripe = done[0]
    assert ripe.status == AnnouncementStatus.sent.value
    assert (ripe.sent_count, ripe.failed_count) == (2, 1)
    assert "apns 410 Unregistered" in ripe.last_error
    dead = await _token(TOKEN_B)
    assert dead.disabled_at is not None and dead.disabled_reason.startswith("apns 410")

    async with SessionLocal() as session:
        early = await session.scalar(select(Announcement).where(Announcement.title == "Рано"))
        assert early.status == AnnouncementStatus.scheduled.value
        # Второй тик ничего не повторяет: статус уже не scheduled
        assert await run_once(session, {"ios": ios, "android": android}, now=now) == []


async def test_platform_without_keys_is_reported_not_fatal():
    now = datetime(2026, 9, 5, 12, 0)
    await _seed(now)

    async def ios(token, title, body, slug, announcement_id):
        return SendResult(ok=True)

    async with SessionLocal() as session:
        done = await run_once(session, {"ios": ios}, now=now)
    ripe = done[0]
    assert ripe.status == AnnouncementStatus.sent.value
    assert (ripe.sent_count, ripe.failed_count) == (2, 1)
    assert "android: не настроено" in ripe.last_error


async def test_broken_keys_stop_the_platform_after_first_answer():
    now = datetime(2026, 9, 5, 12, 0)
    await _seed(now)
    attempts = 0

    async def ios(token, title, body, slug, announcement_id):
        nonlocal attempts
        attempts += 1
        return SendResult(ok=False, fatal=True, error="apns 403 InvalidProviderToken")

    async def android(token, title, body, slug, announcement_id):
        return SendResult(ok=True)

    async with SessionLocal() as session:
        done = await run_once(session, {"ios": ios, "android": android}, now=now)
    ripe = done[0]
    # Android дошёл, iOS упал целиком, но не всеми токенами по очереди
    assert ripe.status == AnnouncementStatus.sent.value
    assert (ripe.sent_count, ripe.failed_count) == (1, 2)
    assert attempts <= 2
    assert "ios:" in ripe.last_error or "InvalidProviderToken" in ripe.last_error


async def test_nobody_to_send_is_not_a_failure():
    now = datetime(2026, 9, 5, 12, 0)
    async with SessionLocal() as session:
        session.add(Announcement(title="В пустоту", body="…", send_at=now))
        await session.commit()
        done = await run_once(session, {}, now=now)
    assert done[0].status == AnnouncementStatus.sent.value
    assert done[0].sent_count == 0 and done[0].failed_count == 0


async def test_prune_forgets_tokens_disabled_a_month_ago():
    now = datetime.now().astimezone()
    async with SessionLocal() as session:
        session.add_all(
            [
                PushToken(
                    token=TOKEN_A, platform="ios",
                    disabled_at=now - timedelta(days=31), disabled_reason="apns 410 Unregistered",
                ),
                PushToken(
                    token=TOKEN_B, platform="ios",
                    disabled_at=now - timedelta(days=29), disabled_reason="apns 410 Unregistered",
                ),
                PushToken(token=TOKEN_C, platform="android"),
            ]
        )
        await session.commit()

    async with SessionLocal() as session:
        assert await run_once(session, {}) == []

    # Месяц прошёл — стёрт (/privacy это обещает); свежепогашенный и живой на месте
    assert await _token(TOKEN_A) is None
    assert (await _token(TOKEN_B)).disabled_at is not None
    assert (await _token(TOKEN_C)).disabled_at is None


# --- Чьё устройство: личные пуши только под входом ---------------------------
#
# Личные пуши о комнатах идут на токены устройств, где человек вошёл.
# Номер устройства выбирает клиент, и раньше любой, кто знал чужой номер,
# регистрировал под ним свой токен и получал чужие пуши.

_phones = iter(range(1000000, 9999999))


async def _person(device: str) -> tuple[dict, int]:
    """Вошедший человек: (заголовки, id). Сам вход проверяется в test_auth.py"""
    token, digest = new_token()
    async with SessionLocal() as session:
        user = User(phone=f"{PHONES}{next(_phones)}")
        session.add(user)
        await session.flush()
        session.add(UserSession(user_id=user.id, token_hash=digest, device_id=device))
        await session.commit()
        return {"Authorization": f"Bearer {token}", "X-Device-Id": device}, user.id


async def _register(client, token: str, headers: dict):
    resp = await client.post(
        "/api/v1/push/devices", json={"token": token, "platform": "ios"}, headers=headers
    )
    assert resp.status_code == 204


async def _deliver(user_id: int) -> list[str]:
    """Поставить человеку личный пуш и разослать: на какие токены он ушёл."""
    calls: list[str] = []

    async def ios(token, title, body, slug, announcement_id, extra=None, channel=None):
        calls.append(token)
        return SendResult(ok=True)

    async with SessionLocal() as session:
        outbox.enqueue(session, user_id, "room_approved", {"place": "Пик", "day": "2026-10-01"}, None)
        await session.commit()
        await outbox.send_outbox(session, {"ios": ios})
    return calls


async def test_вошедший_привязывает_токен_к_устройству_своей_сессии(client):
    headers, user_id = await _person("phone-a-0001")
    await _register(client, TOKEN_A, {**headers, "X-Device-Id": "phone-b-0002"})
    assert (await _token(TOKEN_A)).device == "phone-a-0001"
    assert await _deliver(user_id) == [TOKEN_A]


async def test_гость_с_чужим_номером_устройства_не_получает_личных_пушей(client):
    _, victim = await _person("phone-a-0001")
    await _register(client, TOKEN_B, {"X-Device-Id": "phone-a-0001"})
    row = await _token(TOKEN_B)
    # Токен живой — общие рассылки ему идут, устройства у него нет
    assert row.device is None and row.disabled_at is None
    assert await _deliver(victim) == []


async def test_токен_присланный_под_номером_до_входа_не_ждёт_хозяина(client):
    # Номер ещё ничей — гость спокойно привязывает к нему свой токен
    await _register(client, TOKEN_B, {"X-Device-Id": "phone-a-0001"})
    assert (await _token(TOKEN_B)).device == "phone-a-0001"
    headers, victim = await _person("phone-a-0001")
    assert await _deliver(victim) == []

    # Своё приложение после входа присылает токен заново — уже под сессией
    await _register(client, TOKEN_A, headers)
    assert await _deliver(victim) == [TOKEN_A]


async def test_второй_аккаунт_с_тем_же_номером_устройства_не_перехватывает_пуши(client):
    victim_headers, victim = await _person("phone-a-0001")
    await _register(client, TOKEN_A, victim_headers)
    thief_headers, thief = await _person("phone-a-0001")
    await _register(client, TOKEN_B, thief_headers)
    # Устройство держит последний вход: вошедший следом получает на свой
    # токен только свои пуши. Хозяину это глушит личные пуши — цена
    # правила, описанная в api/push.py, — но чужому они не уходят
    assert await _deliver(thief) == [TOKEN_B]
    assert await _deliver(victim) == []

    # Хозяин присылает свой токен заново — устройство уже не его
    await _register(client, TOKEN_A, victim_headers)
    assert (await _token(TOKEN_A)).device is None
    assert await _deliver(victim) == []
    assert await _deliver(thief) == [TOKEN_B]


async def test_выход_без_сети_не_оставляет_телефон_прежнему_аккаунту(client):
    """Выход без сети гасит токен только в телефоне, а сессия на сервере
    живёт. Раньше она держала устройство вечно: вошедший на этом телефоне
    следом регистрировал токен без устройства и личных пушей не получал."""
    first_headers, first = await _person("phone-a-0001")
    await _register(client, TOKEN_A, first_headers)
    assert await _deliver(first) == [TOKEN_A]

    # Выход до сервера не дошёл, на том же телефоне входит другой человек.
    # Токен телефона ещё не прислан заново, но прежнему он уже не служит
    second_headers, second = await _person("phone-a-0001")
    assert await _deliver(first) == []

    await _register(client, TOKEN_A, second_headers)
    assert (await _token(TOKEN_A)).device == "phone-a-0001"
    assert await _deliver(second) == [TOKEN_A]
    assert await _deliver(first) == []

    # Второй вышел как следует — телефон не возвращается к первому
    resp = await client.post("/api/v1/auth/logout", headers=second_headers)
    assert resp.status_code == 204
    assert await _deliver(second) == []
    assert await _deliver(first) == []


async def test_nul_в_токене_это_422(client):
    resp = await client.post(
        "/api/v1/push/devices", json={"token": "a" * 20 + "\x00", "platform": "ios"}
    )
    assert resp.status_code == 422
    resp = await client.delete("/api/v1/push/devices/" + "a" * 20 + "%00")
    assert resp.status_code == 422
