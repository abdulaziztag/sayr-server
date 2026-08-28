"""Форма закрытого теста Android на лендинге.

Главное свойство эндпоинта — спокойный одинаковый ответ: и на новый адрес,
и на повторный, и на пойманного приманкой бота. Разный ответ выдал бы
наружу, какие адреса лежат в базе, а боту — что его раскусили.
"""

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import TesterSignup


async def _emails() -> list[str]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(TesterSignup.email))).scalars().all()
    return sorted(rows)


async def _cleanup() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(TesterSignup))
        await session.commit()


async def test_signup_lands_in_the_base(client):
    try:
        resp = await client.post(
            "/android-testers",
            data={"email": "  Hiker@Gmail.com ", "lang": "ru", "website": ""},
        )
        assert resp.status_code == 200
        assert await _emails() == ["hiker@gmail.com"], "адрес не нормализован"
    finally:
        await _cleanup()


async def test_double_submit_stays_one_row(client):
    try:
        for _ in range(2):
            resp = await client.post(
                "/android-testers",
                data={"email": "hiker@gmail.com", "lang": "ru", "website": ""},
            )
            assert resp.status_code == 200, "повтор обязан выглядеть как успех"
        assert await _emails() == ["hiker@gmail.com"]
    finally:
        await _cleanup()


async def test_honeypot_swallows_bots_quietly(client):
    try:
        resp = await client.post(
            "/android-testers",
            data={"email": "bot@spam.com", "lang": "ru", "website": "http://spam"},
        )
        assert resp.status_code == 200, "бот не должен узнать, что его раскусили"
        assert await _emails() == []
    finally:
        await _cleanup()


async def test_garbage_is_rejected(client):
    resp = await client.post(
        "/android-testers", data={"email": "не почта", "lang": "ru", "website": ""}
    )
    assert resp.status_code == 422


async def test_answer_matches_the_caller(client):
    """Скрипту — JSON, обычной отправке формы — человеческая страница."""
    try:
        as_json = await client.post(
            "/android-testers",
            data={"email": "a@b.cd", "lang": "uz", "website": ""},
            headers={"Accept": "application/json"},
        )
        assert as_json.json() == {"ok": True}

        as_form = await client.post(
            "/android-testers", data={"email": "c@d.ef", "lang": "uz", "website": ""}
        )
        assert "Tayyor" in as_form.text
        assert '<html lang="uz">' in as_form.text
    finally:
        await _cleanup()


async def test_form_lives_only_until_play_release(client, monkeypatch):
    """Появится ссылка на Google Play — форма исчезнет сама."""
    from app.config import settings

    page = (await client.get("/")).text
    assert "android-testers" in page

    monkeypatch.setattr(settings, "play_store_url", "https://play.google.com/x")
    with_store = (await client.get("/")).text
    assert "android-testers" not in with_store
