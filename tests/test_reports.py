"""Форма обратной связи по местам: /report.

Главное, что здесь проверяется, — заявка не теряется ни в одном
из способов её оставить: место выбрано из списка, вписано словами
или подставлено ссылкой со страницы места.
"""

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.api import report
from app.db import SessionLocal
from app.models import PlaceReport

WATERFALL = "Тестовый водопад — Тестовый регион"


async def _rows() -> list[PlaceReport]:
    async with SessionLocal() as session:
        stmt = select(PlaceReport).options(selectinload(PlaceReport.place))
        return list((await session.execute(stmt)).scalars().all())


async def _cleanup() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(PlaceReport))
        await session.commit()
    # Счётчик частоты живёт в памяти процесса и переживает тест
    report._RECENT.clear()


async def test_report_lands_in_the_base(client):
    try:
        resp = await client.post(
            "/report",
            data={
                "place": WATERFALL,
                "topics": ["duration", "track"],
                "comment": "  Шли шесть часов вместо четырёх  ",
                "contact": "https://t.me/hiker",
                "lang": "ru",
                "website": "",
            },
        )
        assert resp.status_code == 200
        rows = await _rows()
        assert len(rows) == 1
        row = rows[0]
        assert row.place.slug == "test-waterfall"
        assert row.place_note is None, "узнанное место словами не дублируем"
        assert row.topics == ["duration", "track"]
        assert row.comment == "Шли шесть часов вместо четырёх"
        assert row.contact == "@hiker", "ссылка t.me не свелась к нику"
        assert row.status == "new"
    finally:
        await _cleanup()


async def test_place_typed_by_hand_is_kept(client):
    """Места нет в каталоге — заявка всё равно доходит."""
    try:
        resp = await client.post(
            "/report",
            data={
                "place": "Ущелье Сурхат",
                "topics": ["missing"],
                "comment": "",
                "contact": "",
                "lang": "ru",
                "website": "",
            },
        )
        assert resp.status_code == 200
        row = (await _rows())[0]
        assert row.place_id is None
        assert row.place_note == "Ущелье Сурхат"
        assert row.contact is None
    finally:
        await _cleanup()


async def test_slug_from_the_place_page_resolves(client):
    """Со страницы места приходят с ?place=slug — он и подставляется в поле."""
    try:
        await client.post(
            "/report",
            data={"place": "test-peak", "comment": "тропа заросла", "lang": "ru",
                  "website": ""},
        )
        row = (await _rows())[0]
        assert row.place.slug == "test-peak"
        assert row.place_note is None
    finally:
        await _cleanup()


async def test_empty_report_is_rejected(client):
    resp = await client.post(
        "/report", data={"place": WATERFALL, "comment": "  ", "lang": "ru",
                         "website": ""}
    )
    assert resp.status_code == 422
    assert await _rows() == []


async def test_unknown_topics_are_dropped(client):
    try:
        await client.post(
            "/report",
            data={"place": WATERFALL, "topics": ["duration", "выдумка"],
                  "comment": "", "lang": "ru", "website": ""},
        )
        assert (await _rows())[0].topics == ["duration"]
    finally:
        await _cleanup()


async def test_honeypot_swallows_bots_quietly(client):
    try:
        resp = await client.post(
            "/report",
            data={"place": WATERFALL, "comment": "buy cheap", "lang": "ru",
                  "website": "http://spam"},
        )
        assert resp.status_code == 200, "бот не должен узнать, что его раскусили"
        assert await _rows() == []
    finally:
        await _cleanup()


async def test_answer_matches_the_caller(client):
    """Скрипту — JSON, обычной отправке формы — человеческая страница."""
    try:
        as_json = await client.post(
            "/report",
            data={"place": WATERFALL, "comment": "раз", "lang": "ru", "website": ""},
            headers={"Accept": "application/json"},
        )
        assert as_json.json() == {"ok": True}

        as_form = await client.post(
            "/report",
            data={"place": WATERFALL, "comment": "икки", "lang": "uz", "website": ""},
        )
        assert '<html lang="uz">' in as_form.text
        assert "rahmat" in as_form.text
        assert (await _rows())[0].lang in ("ru", "uz")
    finally:
        await _cleanup()


async def test_form_prefills_place_from_the_link(client):
    ru = await client.get("/report", params={"place": "test-lake"})
    assert 'value="Тестовое озеро — Тестовый регион"' in ru.text
    assert 'name="topics" value="duration"' in ru.text

    uz = await client.get("/uz/report", params={"place": "test-lake"})
    assert 'value="Test koʻli — Test viloyati"' in uz.text
    assert "Boshqa" in uz.text, "темы не перевелись"


async def test_place_page_invites_to_the_form(client):
    page = await client.get("/p/test-waterfall")
    assert "/report?place=test-waterfall" in page.text

    uz = await client.get("/p/test-waterfall", params={"lang": "uz"})
    assert "/uz/report?place=test-waterfall" in uz.text
