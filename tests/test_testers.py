"""Форма закрытого теста Android на лендинге.

Главное свойство эндпоинта — спокойный одинаковый ответ: и на новый адрес,
и на повторный, и на пойманного приманкой бота. Разный ответ выдал бы
наружу, какие адреса лежат в базе, а боту — что его раскусили.
"""

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.api import landing
from app.db import SessionLocal
from app.main import app
from app.models import TesterSignup


async def _emails() -> list[str]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(TesterSignup.email))).scalars().all()
    return sorted(rows)


async def _cleanup() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(TesterSignup))
        await session.commit()
    # Счётчик частоты живёт в памяти процесса и переживает тест
    landing._RECENT.clear()


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


async def test_частые_заявки_с_одного_адреса_отбиваются(client, monkeypatch):
    """Приманка ловит бота, что заполняет всё подряд, но не скрипт под эту
    форму. IPv6 считаем сетью /64: внутри неё адрес меняется как угодно."""
    monkeypatch.setattr(landing, "_LIMIT", 2)

    async def sign(host: str, email: str) -> int:
        transport = ASGITransport(app=app, client=(host, 40000))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/android-testers",
                data={"email": email, "lang": "ru", "website": ""},
                headers={"Accept": "application/json"},
            )
        return resp.status_code

    try:
        assert await sign("2001:db8:1:2::a", "a@b.cd") == 200
        assert await sign("2001:db8:1:2::b", "b@b.cd") == 200
        assert await sign("2001:db8:1:2:ffff::1", "c@b.cd") == 429
        assert await sign("2001:db8:1:3::1", "d@b.cd") == 200, "соседняя сеть ни при чём"
        assert await _emails() == ["a@b.cd", "b@b.cd", "d@b.cd"]
    finally:
        await _cleanup()


async def test_после_выхода_в_play_заявки_не_принимаются(client, monkeypatch):
    """Форма пропала со страницы — ручка за ней тоже закрыта.

    Скрипту — отказ, форме из старой вкладки — лендинг, где теперь стоит
    кнопка магазина.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "play_store_url", "https://play.google.com/x")
    try:
        as_json = await client.post(
            "/android-testers",
            data={"email": "late@b.cd", "lang": "ru", "website": ""},
            headers={"Accept": "application/json"},
        )
        assert as_json.status_code == 410

        as_form = await client.post(
            "/android-testers",
            data={"email": "late@b.cd", "lang": "uz", "website": ""},
            follow_redirects=False,
        )
        assert as_form.status_code == 303
        assert as_form.headers["location"] == "/uz"
        assert await _emails() == []
    finally:
        await _cleanup()
