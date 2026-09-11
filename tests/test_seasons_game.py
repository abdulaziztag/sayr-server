"""Игра «когда сюда идти»: колода, ответы и проверка.

Главное, что проверяется, — правила выбывания из колоды. Ошибка здесь
не видна на глаз: игра продолжает показывать карточки, просто не те,
и хвост каталога остаётся без ответов.
"""

from html.parser import HTMLParser

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, inspect, select, update

from app.api import seasons
from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import Place, SeasonVote


async def _clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(SeasonVote))
        await session.execute(
            update(Place).values(season_from=None, season_to=None, best_seasons=[],
                                 winter_load=None, danger=None, limits=[])
        )
        await session.commit()
    # Счётчик частоты живёт в памяти процесса и переживает тест
    seasons._RECENT.clear()


async def _vote(client, slug: str, voter: str, months: tuple[int, int] | None,
                extra: dict | None = None):
    data = {"slug": slug, "lang": "ru"}
    if months is None:
        data["skip"] = "1"
    else:
        data["from_month"], data["to_month"] = months
    if extra:
        data.update(extra)
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


async def test_карточка_стартует_с_апреля_по_август(client):
    """Решение владельца: готовую дугу проще подвинуть, чем начать с нуля."""
    try:
        page = await client.get("/seasons")
        assert '<option value="4" selected>' in page.text, "без скрипта — апрель"
        assert '<option value="8" selected>' in page.text, "…по август"
        assert "АПР — АВГ" in page.text, "итог в середине круга"
        assert "5 месяцев" in page.text
    finally:
        await _clean()


def test_круглый_год_рисуется_по_кольцу():
    """Замкнутая дуга — две полуокружности через верх и низ.

    Прежняя версия вела вторую половину не по той окружности, и полоса
    вылезала за кольцо — на телефоне это выглядело как сломанный круг.
    """
    from app.api.seasons_page import band_path

    path = band_path(5, 4)
    # Дорожка радиусом 104 в круге 300 × 300: верх (150, 46), низ (150, 254)
    assert path.startswith("M150.0 46.0"), path
    assert "A104 104 0 0 0 150.0 254.0" in path, "первая половина кончается внизу"
    assert path.endswith("A104 104 0 0 0 150.0 46.0"), "и возвращается наверх"


def test_январь_наверху_весна_слева():
    """Год — линия слева направо, загнутая концами вверх: весна слева."""
    from app.api.seasons_page import _centre, _pt

    x, y = _pt(_centre(1))
    assert (round(x), round(y)) == (150, 46), "январь ровно наверху"
    assert _pt(_centre(4))[0] < 100, "апрель слева"
    assert _pt(_centre(10))[0] > 200, "октябрь справа"
    assert _pt(_centre(7))[1] > 200, "июль внизу"


async def test_проверка_показывает_все_места_списком(client, review_client):
    """Проверка — рабочий список: все ждущие места на одной странице."""
    try:
        for voter in ("a", "b", "c"):
            await _vote(client, "test-lake", voter, (5, 9))
        await _vote(client, "test-peak", "a", (6, 8))
        page = await review_client.get("/seasons/review")
        # Считаем разметку строк, а не «data-row»: эта подстрока есть ещё
        # и в скрипте страницы, который строки ищет
        assert page.text.count('class="row"') == 2, "оба места сразу, а не по одному"
        assert page.text.index("Тестовое озеро") < page.text.index("Тестовый пик"), (
            "сверху те, где ответов больше"
        )
        assert "порог набран" in page.text
    finally:
        await _clean()


async def test_одобрение_отвечает_скрипту_без_перезагрузки(client, review_client):
    try:
        await _vote(client, "test-lake", "a", (5, 9))
        done = await review_client.post(
            "/seasons/review/approve",
            data={"slug": "test-lake", "from_month": 5, "to_month": 9},
            headers={"Accept": "application/json"},
        )
        assert done.status_code == 200 and done.json() == {"ok": True}

        broken = await review_client.post(
            "/seasons/review/approve",
            data={"slug": "test-lake"},
            headers={"Accept": "application/json"},
        )
        assert broken.status_code == 422, "без месяцев одобрять нечего"
    finally:
        await _clean()


async def test_вразнобой_списки_пустые_и_обязательные(client, review_client):
    """Разошлись — чей-то ответ за итог не выдаём: месяцы ставит человек."""
    try:
        await _vote(client, "test-lake", "a", (1, 1))
        await _vote(client, "test-lake", "b", (7, 7))
        page = await review_client.get("/seasons/review")
        assert "вразнобой" in page.text
        assert '<option value="">—</option>' in page.text
        assert "required" in page.text
    finally:
        await _clean()



async def _one(slug: str, voter: str) -> SeasonVote:
    async with SessionLocal() as session:
        stmt = (
            select(SeasonVote)
            .join(Place, Place.id == SeasonVote.place_id)
            .where(Place.slug == slug, SeasonVote.voter == voter)
        )
        return (await session.execute(stmt)).scalar_one()


async def test_необязательное_ложится_вместе_с_ответом(client):
    try:
        await _vote(client, "test-lake", "a", (11, 2), {
            "snow_load": "4", "danger": "7", "limits": ["border", "выдумка"],
            "note": "  " + "x" * 400 + "  ",
        })
        vote = await _one("test-lake", "a")
        assert vote.snow_load == 4
        assert vote.danger == 7
        assert vote.limits == ["border"], "незнакомый код отбрасывается"
        assert len(vote.note) == 300, "комментарий обрезан до трёхсот знаков"
    finally:
        await _clean()


async def test_снег_без_зимы_не_хранится(client):
    try:
        await _vote(client, "test-lake", "a", (4, 8), {"snow_load": "6", "danger": "11"})
        vote = await _one("test-lake", "a")
        assert vote.snow_load is None, "у дуги без зимы балл за снег ничего не значит"
        assert vote.danger is None, "11 — не балл"
    finally:
        await _clean()


async def test_не_знаю_с_ограничением_сохраняется(client):
    """Сезон можно не помнить, а про погранзону знать точно."""
    try:
        await _vote(client, "test-peak", "a", None, {"limits": ["border"], "note": "пропуск"})
        vote = await _one("test-peak", "a")
        assert vote.from_month is None
        assert vote.limits == ["border"]
        assert vote.note == "пропуск"
    finally:
        await _clean()


async def test_игра_спрашивает_необязательное(client):
    page = (await client.get("/seasons")).text
    assert "Тропёжка зимой" in page
    assert 'name="snow_load"' in page and 'name="danger"' in page
    assert "Погранзона" in page and 'name="note"' in page
    assert 'class="winter off"' in page, "место под шкалу держится, пока она не видна"
    uz = (await client.get("/uz/seasons")).text
    assert "Chegara hududi" in uz


async def test_проверяющий_видит_и_применяет_факты(client, review_client):
    try:
        for voter, snow in (("a", 2), ("b", 3), ("c", 9)):
            extra = {"snow_load": str(snow), "danger": "5"}
            if voter != "c":
                extra["limits"] = ["border"]
            await _vote(client, "test-lake", voter, (11, 2), extra)
        await _vote(client, "test-lake", "d", None, {"note": "Кумбель закрыт"})

        page = (await review_client.get("/seasons/review")).text
        assert '<option value="3" selected>3</option>' in page, "середина 2, 3, 9 — это 3"
        assert "Погранзона · 2" in page
        assert "«Кумбель закрыт»" in page, "комментарий из «не знаю» тоже виден"

        await review_client.post("/seasons/review/approve", data={
            "slug": "test-lake", "from_month": 11, "to_month": 2,
            "winter_load": "3", "danger": "5", "limits": ["border"], "limits_shown": "1",
        })
        async with SessionLocal() as session:
            place = (
                await session.execute(select(Place).where(Place.slug == "test-lake"))
            ).scalar_one()
        assert (place.winter_load, place.danger, place.limits) == (3, 5, ["border"])
    finally:
        await _clean()


async def test_одобрение_без_фактов_их_не_стирает(client, review_client):
    """Поле, которого в строке не было, одобрение не трогает."""
    try:
        async with SessionLocal() as session:
            await session.execute(
                update(Place).where(Place.slug == "test-lake")
                .values(danger=8, limits=["law"])
            )
            await session.commit()
        await _vote(client, "test-lake", "a", (5, 9))
        await review_client.post("/seasons/review/approve", data={
            "slug": "test-lake", "from_month": 5, "to_month": 9,
        })
        async with SessionLocal() as session:
            place = (
                await session.execute(select(Place).where(Place.slug == "test-lake"))
            ).scalar_one()
        assert (place.danger, place.limits) == (8, ["law"]), "проставленное руками уцелело"
    finally:
        await _clean()


class _FormFields(HTMLParser):
    """Поля формы правки — ровно то, что отправил бы браузер, нажми владелец «Save»."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, list[str]] = {}
        self._in_form = False
        self._select: tuple[str, bool] | None = None
        self._options: list[tuple[str, bool]] = []
        self._textarea: str | None = None

    def _add(self, name: str, value: str) -> None:
        self.fields.setdefault(name, []).append(value)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and (a.get("method") or "").lower() == "post":
            self._in_form = True
        if not self._in_form:
            return
        name = a.get("name")
        if tag == "input" and name:
            kind = (a.get("type") or "text").lower()
            if kind in ("submit", "button", "file", "reset", "image"):
                return
            if kind in ("checkbox", "radio") and "checked" not in a:
                return
            self._add(name, a.get("value") or ("y" if kind == "checkbox" else ""))
        elif tag == "select" and name:
            self._select = (name, "multiple" in a)
            self._options = []
        elif tag == "option" and self._select:
            self._options.append((a.get("value") or "", "selected" in a))
        elif tag == "textarea" and name:
            self._textarea = name
            self._add(name, "")

    def handle_data(self, data):
        if self._textarea:
            self.fields[self._textarea][-1] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self._in_form = False
        elif tag == "textarea" and self._textarea:
            # Браузер съедает один перевод строки сразу за <textarea>
            value = self.fields[self._textarea][-1]
            self.fields[self._textarea][-1] = value.removeprefix("\r\n").removeprefix("\n")
            self._textarea = None
        elif tag == "select" and self._select:
            name, multiple = self._select
            chosen = [value for value, on in self._options if on]
            if not chosen and not multiple and self._options:
                chosen = [self._options[0][0]]
            for value in chosen:
                self._add(name, value)
            self._select = None


async def _place_row(slug: str) -> Place:
    async with SessionLocal() as session:
        return (await session.execute(select(Place).where(Place.slug == slug))).scalar_one()


async def test_факты_правятся_в_админке_и_форма_места_сохраняется(admin_client):
    """Владелец правит места в админке каждый день: новые поля не должны ей мешать."""
    place = await _place_row("test-lake")
    before = {attr.key: getattr(place, attr.key) for attr in inspect(Place).column_attrs}
    try:
        page = await admin_client.get(f"/admin/place/edit/{place.id}")
        assert page.status_code == 200
        assert "Тропёжка зимой" in page.text and "Погранзона" in page.text

        form = _FormFields()
        form.feed(page.text)
        fields = form.fields
        fields.update(limits=["border", "hunting"], danger=["7"], winter_load=[""],
                      save=["Save"])
        saved = await admin_client.post(f"/admin/place/edit/{place.id}", data=fields,
                                        follow_redirects=False)
        assert saved.status_code == 302, saved.text[:400]
        place = await _place_row("test-lake")
        assert (place.limits, place.danger, place.winter_load) == (["border", "hunting"], 7, None)

        fields.update(danger=["11"])
        refused = await admin_client.post(f"/admin/place/edit/{place.id}", data=fields,
                                          follow_redirects=False)
        assert refused.status_code == 400, "11 — не балл"
        assert (await _place_row("test-lake")).danger == 7
    finally:
        async with SessionLocal() as session:
            await session.execute(
                update(Place).where(Place.id == before["id"])
                .values(**{key: value for key, value in before.items() if key != "id"})
            )
            await session.commit()
