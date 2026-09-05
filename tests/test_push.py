"""Пуши по расписанию: регистрация токенов и планировщик на подменных отправителях."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Announcement, AnnouncementStatus, PushToken
from app.push import SendResult
from app.push.sender import run_once

TOKEN_A = "a" * 64
TOKEN_B = "b" * 64
TOKEN_C = "c" * 152  # FCM-токены длинные


async def _clear() -> None:
    async with SessionLocal() as session:
        for row in (await session.execute(select(PushToken))).scalars():
            await session.delete(row)
        for row in (await session.execute(select(Announcement))).scalars():
            await session.delete(row)
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

    async def ios(token, title, body, slug):
        calls.append((token, title))
        # Вторая установка снесла приложение — Apple отвечает 410
        if token == TOKEN_B:
            return SendResult(ok=False, invalid_token=True, error="apns 410 Unregistered")
        return SendResult(ok=True)

    async def android(token, title, body, slug):
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

    async def ios(token, title, body, slug):
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

    async def ios(token, title, body, slug):
        nonlocal attempts
        attempts += 1
        return SendResult(ok=False, fatal=True, error="apns 403 InvalidProviderToken")

    async def android(token, title, body, slug):
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
