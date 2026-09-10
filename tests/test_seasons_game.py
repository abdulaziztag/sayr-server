"""Игра «когда сюда идти»: колода, ответы и проверка.

Главное, что проверяется, — правила выбывания из колоды. Ошибка здесь
не видна на глаз: игра продолжает показывать карточки, просто не те,
и хвост каталога остаётся без ответов.
"""

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update

from app.api import seasons
from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import Place, SeasonVote


async def _clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(SeasonVote))
        await session.execute(
            update(Place).values(season_from=None, season_to=None, best_seasons=[])
        )
        await session.commit()
    # Счётчик частоты живёт в памяти процесса и переживает тест
    seasons._RECENT.clear()


async def _vote(client, slug: str, voter: str, months: tuple[int, int] | None):
    data = {"slug": slug, "lang": "ru"}
    if months is None:
        data["skip"] = "1"
    else:
        data["from_month"], data["to_month"] = months
    return await client.post(
        "/seasons/vote",
        data=data,
        cookies={seasons.COOKIE: voter},
        headers={"Accept": "application/json"},
    )


async def _deck(client, voter: str) -> dict:
    resp = await client.get("/seasons/deck", cookies={seasons.COOKIE: voter})
    return resp.json()


@pytest.fixture
async def review_client():
    """Клиент с открытой сессией проверяющего.

    Адрес https: cookie помечена Secure, и по http клиент её не сохранит.
    Пароль отдельный не задан, значит пускает админский
    """
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://test") as client:
            entered = await client.post(
                "/seasons/review/login", data={"password": settings.admin_password}
            )
            assert entered.status_code in (200, 303), entered.text[:200]
            yield client


async def test_страница_открывается_и_ставит_номер_устройства(client):
    try:
        resp = await client.get("/seasons")
        assert resp.status_code == 200
        assert seasons.COOKIE in resp.cookies, "без номера колоду не собрать"
        assert "Когда сюда идти" in resp.text

        uz = await client.get("/uz/seasons")
        assert "Bu yerga qachon" in uz.text
    finally:
        await _clean()


async def test_ответ_ложится_и_перезаписывается(client):
    try:
        assert (await _vote(client, "test-waterfall", "one", (5, 10))).status_code == 200
        async with SessionLocal() as session:
            rows = list((await session.execute(select(SeasonVote))).scalars().all())
        assert len(rows) == 1 and (rows[0].from_month, rows[0].to_month) == (5, 10)

        # Человек передумал: голосов по-прежнему один, но другой
        await _vote(client, "test-waterfall", "one", (6, 9))
        async with SessionLocal() as session:
            rows = list((await session.execute(select(SeasonVote))).scalars().all())
        assert len(rows) == 1, "второй ответ обязан заменить первый, а не лечь рядом"
        assert (rows[0].from_month, rows[0].to_month) == (6, 9)
    finally:
        await _clean()


async def test_не_знаю_не_считается_но_убирает_место(client):
    try:
        await _vote(client, "test-peak", "one", None)
        mine = await _deck(client, "one")
        assert all(p["slug"] != "test-peak" for p in mine["places"]), (
            "пропущенное не должно возвращаться тому же устройству"
        )
        # А другому — должно: он-то ещё не отвечал
        other = await _deck(client, "two")
        assert any(p["slug"] == "test-peak" for p in other["places"])
    finally:
        await _clean()


async def test_набравшее_порог_уходит_из_колоды(client):
    try:
        for voter in ("a", "b", "c"):
            await _vote(client, "test-lake", voter, (5, 9))
        fresh = await _deck(client, "newbie")
        assert all(p["slug"] != "test-lake" for p in fresh["places"]), (
            "три ответа — достаточно, дальше место ждёт проверки"
        )
    finally:
        await _clean()


async def test_три_не_знаю_место_из_колоды_не_выводят(client):
    try:
        for voter in ("a", "b", "c"):
            await _vote(client, "test-lake", voter, None)
        fresh = await _deck(client, "newbie")
        assert any(p["slug"] == "test-lake" for p in fresh["places"]), (
            "о месте так ничего и не узнали — оно обязано остаться в игре"
        )
    finally:
        await _clean()


async def test_первым_идёт_то_о_чём_знаем_меньше(client):
    try:
        # У водопада два ответа, у остальных ни одного
        for voter in ("a", "b"):
            await _vote(client, "test-waterfall", voter, (5, 9))
        deck = await _deck(client, "newbie")
        assert deck["places"], "колода не должна быть пустой"
        assert deck["places"][-1]["slug"] == "test-waterfall", (
            "место с ответами обязано уйти в хвост"
        )
    finally:
        await _clean()


async def test_одобренное_место_не_показывается_и_ответов_не_принимает(client):
    try:
        async with SessionLocal() as session:
            await session.execute(
                update(Place)
                .where(Place.slug == "test-peak")
                .values(season_from=6, season_to=9)
            )
            await session.commit()
        deck = await _deck(client, "newbie")
        assert all(p["slug"] != "test-peak" for p in deck["places"])

        late = await _vote(client, "test-peak", "newbie", (5, 10))
        assert late.status_code == 409, "ответ опоздал — сезон уже стоит"
    finally:
        await _clean()


async def test_счётчик_считает_оставшееся_этому_устройству(client):
    try:
        before = (await _deck(client, "counter"))["left"]
        await _vote(client, "test-waterfall", "counter", (5, 9))
        after = (await _deck(client, "counter"))["left"]
        assert after == before - 1
    finally:
        await _clean()


async def test_проверка_закрыта_паролем(client):
    resp = await client.get("/seasons/review")
    assert "Пароль" in resp.text, "без cookie — форма, а не карточки"
    assert "Одобрить" not in resp.text

    wrong = await client.post("/seasons/review/login", data={"password": "мимо"})
    assert wrong.status_code == 401


async def test_одобрение_пишет_месяцы_и_сезоны(client, review_client):
    try:
        for voter in ("a", "b", "c"):
            await _vote(client, "test-lake", voter, (11, 2))
        page = await review_client.get("/seasons/review")
        assert "Тестовое озеро" in page.text
        assert "с ноября по февраль" in page.text

        done = await review_client.post(
            "/seasons/review/approve",
            data={"slug": "test-lake", "from_month": 11, "to_month": 2},
        )
        assert done.status_code in (200, 303)

        async with SessionLocal() as session:
            place = (
                await session.execute(select(Place).where(Place.slug == "test-lake"))
            ).scalar_one()
            assert (place.season_from, place.season_to) == (11, 2)
            assert place.best_seasons == ["autumn", "winter"], "сезоны считаются из дуги"

        empty = await review_client.get("/seasons/review")
        assert "Пока нечего проверять" in empty.text
    finally:
        await _clean()


async def test_очистка_возвращает_место_в_игру(client, review_client):
    try:
        for voter in ("a", "b", "c"):
            await _vote(client, "test-lake", voter, (5, 9))
        assert all(
            p["slug"] != "test-lake" for p in (await _deck(client, "newbie"))["places"]
        )

        await review_client.post("/seasons/review/clear", data={"slug": "test-lake"})
        back = await _deck(client, "newbie")
        # Не «первым»: у всех снова ноль ответов, а ничьи разбиваются
        # случайно. Обещать порядок здесь нельзя — только возвращение
        assert any(p["slug"] == "test-lake" for p in back["places"]), (
            "ответы стёрты — место обязано вернуться в игру"
        )
        async with SessionLocal() as session:
            left = list((await session.execute(select(SeasonVote))).scalars().all())
        assert left == []
    finally:
        await _clean()
