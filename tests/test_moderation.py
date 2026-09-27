"""Фильтр мата: ловит очевидное и не задевает обычные слова."""

import pytest

from app.moderation import is_clean


@pytest.mark.parametrize(
    "text",
    [
        "иду не спеша, встречаемся в 6:00 у Буюк Ипак Йули",
        "беру с собой газ, себе и вам",
        "требуется машина, хуже не будет",
        "Самарканд, сикл, Сикамор",
        "Азиз",
        "qo'shiling, hammaga salom",
        "Jalolbek",
        "Shitake mushrooms",
        "",
        None,
    ],
)
def test_обычные_слова_проходят(text):
    assert is_clean(text), text


@pytest.mark.parametrize(
    "text",
    [
        "иди нахуй",
        "ХУУУЙ",
        "xyй",
        "распиздяй",
        "заебал",
        "бля, опять",
        "сука",
        "пидор",
        "dolboyob",
        "jalab",
        "fuck you",
        "motherfucker",
    ],
)
def test_ругательства_ловятся(text):
    assert not is_clean(text), text
