"""Четвёртая ступень и число дней на выход.

Главное здесь — совместимость. Клиенты декодируют сложность в свой
enum: iOS через `String, Codable`, Android через kotlinx. Обе стороны
падают на значении, которого не знают, и падает при этом разбор всего
списка, а не одного места — человек со старой сборкой остался бы
с пустым каталогом. Поэтому наружу едут только три исходные ступени,
а четвёртая повторяется отдельным флагом, который старые сборки просто
не заметят: лишние ключи игнорируют и Swift, и kotlinx.

`test-alpine-peak` в фикстурах — единственное место со ступенью
`extreme` и ночёвкой, на нём и проверяется.
"""

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Difficulty, Place


async def test_extreme_arrives_as_hard_with_flag(client):
    place = (await client.get("/api/v1/places/test-alpine-peak")).json()
    assert place["difficulty"] == "hard"
    assert place["alpine"] is True


async def test_flag_travels_in_the_list_too(client):
    by_slug = {p["slug"]: p for p in (await client.get("/api/v1/places")).json()}
    assert by_slug["test-alpine-peak"]["alpine"] is True
    assert by_slug["test-alpine-peak"]["difficulty"] == "hard"


async def test_other_levels_keep_their_own(client):
    """У остальных трёх флаг ложен, а ступень совпадает с базой."""
    async with SessionLocal() as session:
        stored = {
            p.slug: p.difficulty
            for p in (await session.execute(select(Place))).scalars().all()
        }
    for item in (await client.get("/api/v1/places")).json():
        if stored[item["slug"]] is Difficulty.extreme:
            continue
        assert item["alpine"] is False, item["slug"]
        assert item["difficulty"] == stored[item["slug"]].value, item["slug"]


@pytest.mark.parametrize("params", [{}, {"lang": "ru"}, {"lang": "uz"}])
async def test_extreme_never_leaks(client, params):
    """Ни на каком языке и ни в каком ответе строки extreme быть не должно."""
    listing = await client.get("/api/v1/places", params=params)
    assert "extreme" not in listing.text
    detail = await client.get("/api/v1/places/test-alpine-peak", params=params)
    assert "extreme" not in detail.text


async def test_filter_by_hard_finds_the_alpine_one(client):
    """Старый клиент фильтрует по «сложно» — и находит альпинистское место.

    Так и надо: для него оно и есть «сложно», другого слова у него нет.
    """
    hard = await client.get("/api/v1/places", params={"difficulty": "hard"})
    assert "test-alpine-peak" in {p["slug"] for p in hard.json()}

    # А в чужую ступень оно при этом не попадает
    easy = await client.get("/api/v1/places", params={"difficulty": "easy"})
    assert "test-alpine-peak" not in {p["slug"] for p in easy.json()}


async def test_trip_days_reach_the_client(client):
    by_slug = {p["slug"]: p for p in (await client.get("/api/v1/places")).json()}
    assert by_slug["test-alpine-peak"]["trip_days"] == 3
    assert by_slug["test-alpine-peak"]["overnight"] == "tent"


async def test_trip_days_empty_without_overnight(client):
    """Пусто значит однодневный выход, а не ноль дней."""
    for item in (await client.get("/api/v1/places")).json():
        if item["overnight"] is None:
            assert item["trip_days"] is None, item["slug"]
