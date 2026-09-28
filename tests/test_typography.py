"""Узбекские oʻ и gʻ уходят с сервера узкими кавычками, а база не меняется.

IBM Plex рисует знаки орфографии U+02BB «ʻ» и U+02BC «ʼ» шире буквы
и рвёт ими слово: «O ʻ zbekiston». Поэтому ни один из них не должен дойти
до экрана — ни через API, ни через страницы, ни через пуши и Telegram.
"""

from datetime import date

from sqlalchemy import select

from app.api import seasons
from app.config import settings
from app.db import SessionLocal
from app.models import Place
from app.schemas import pick
from app.tg import texts
from app.typography import fold_apostrophes, uz_display

MARKS = ("\u02bb", "\u02bc")


def _whole(text: str) -> bool:
    """Ни одного знака, который шрифт рисует отдельно от буквы."""
    return not any(mark in text for mark in MARKS)


def test_marks_become_narrow_quotes():
    assert uz_display("Oʻzbekiston") == "O\u2018zbekiston"
    assert uz_display("maʼlumot") == "ma\u2019lumot"
    # Повторный проход ничего не портит, чужой текст не трогается
    assert uz_display("O\u2018zbekiston") == "O\u2018zbekiston"
    assert uz_display("Большой Чимган, 3309 м") == "Большой Чимган, 3309 м"


def test_fold_finds_one_word_behind_every_apostrophe():
    spellings = ("Ko\u02bbl", "Ko\u2018l", "Ko\u2019l", "Ko\u02bcl", "Ko'l", "Ko`l", "Kol")
    assert {fold_apostrophes(word) for word in spellings} == {"Kol"}


def test_pick_converts_both_languages():
    """Русское описание тоже цитирует узбекские названия."""
    assert pick("Тестовое озеро", "Test koʻli", "uz") == "Test ko\u2018li"
    assert pick("узб. Beshtor choʻqqisi", None, "uz") == "узб. Beshtor cho\u2018qqisi"
    assert pick("узб. Beshtor choʻqqisi", "Beshtor", "ru") == "узб. Beshtor cho\u2018qqisi"


async def test_api_converts_on_the_way_out_and_base_keeps_spelling(client):
    detail = (await client.get("/api/v1/places/test-lake", params={"lang": "uz"})).json()
    assert detail["name"] == "Test ko\u2018li"
    assert detail["short_desc"] == "Shahardan uzoq bo\u2018lmagan ko\u2018l"
    listing = await client.get("/api/v1/places", params={"lang": "uz"})
    assert _whole(listing.text)
    async with SessionLocal() as session:
        stmt = select(Place.name_uz).where(Place.slug == "test-lake")
        assert (await session.execute(stmt)).scalar_one() == "Test koʻli"


async def test_pages_have_no_torn_letters(client, monkeypatch):
    monkeypatch.setattr(settings, "seasons_open", True)
    paths = ("/", "/uz", "/report", "/uz/report", "/seasons", "/uz/seasons")
    for path in paths:
        page = await client.get(path)
        assert page.status_code == 200, path
        assert _whole(page.text), path
    share = await client.get("/p/test-lake", params={"lang": "uz"})
    assert "Test ko\u2018li" in share.text and _whole(share.text)
    assert "O\u2018zbekiston" in (await client.get("/uz")).text
    # Ссылка на узбекскую версию стоит и на русской странице
    assert "O\u2018zbekcha" in (await client.get("/")).text


async def test_seasons_deck_json_is_converted_too(client, monkeypatch):
    """Колода догружается мимо страницы, и категория в ней — тоже текст."""
    monkeypatch.setattr(settings, "seasons_open", True)
    resp = await client.get(
        "/seasons/deck", params={"lang": "uz"}, cookies={seasons.COOKIE: "typography" * 3}
    )
    cards = {card["slug"]: card for card in resp.json()["places"]}
    assert cards["test-lake"]["name"] == "Test ko\u2018li"
    assert cards["test-lake"]["category"] == "ko\u2018l"
    assert _whole(resp.text)


def test_telegram_texts_are_converted():
    first = texts.first_message("Большой Чимган", "Katta Chimyon", date(2026, 9, 25), "chimgan")
    assert "so\u2018qmoq" in first and _whole(first)
    assert all(_whole(text) for text in texts.TEXTS.values())
