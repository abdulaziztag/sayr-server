"""Круг года: дуги, согласие и сезоны из дуги.

Проверяется главным образом замкнутость круга: почти каждая ошибка здесь —
это забытый переход через декабрь.
"""

import pytest

from app.seasons import arc_months, arc_text, seasons_of, suggest


def test_дуга_считается_по_кругу():
    assert arc_months(5, 10) == [5, 6, 7, 8, 9, 10]
    assert arc_months(11, 2) == [11, 12, 1, 2], "зима не должна разрываться"
    assert arc_months(7, 7) == [7], "один месяц — это тоже дуга"
    assert arc_months(1, 12) == list(range(1, 13))
    assert arc_months(12, 11) == [12] + list(range(1, 12)), "весь год с декабря"


def test_единственный_ответ_и_есть_предложение():
    assert suggest([(5, 10)]) == (5, 10)


def test_единодушие():
    assert suggest([(6, 9), (6, 9), (6, 9)]) == (6, 9)


def test_большинство_а_не_объединение():
    # Двое из трёх за июль–август, третий тянет края: края не проходят
    assert suggest([(7, 8), (7, 8), (4, 11)]) == (7, 8)


def test_при_двух_ответах_нужны_оба():
    assert suggest([(6, 8), (7, 9)]) == (7, 8), "остаётся пересечение"
    assert suggest([(1, 2), (7, 8)]) is None, "общего месяца нет — молчим"


def test_разнобой_не_даёт_предложения():
    assert suggest([(1, 1), (5, 5), (9, 9)]) is None


def test_согласие_через_декабрь():
    assert suggest([(11, 2), (11, 2), (12, 1)]) == (11, 2)


def test_круглый_год():
    assert suggest([(1, 12), (1, 12)]) == (1, 12)


def test_пустые_ответы():
    assert suggest([]) is None


def test_из_двух_кусков_берём_длинный():
    # Двое за лето, двое за зиму, пятый — за круглый год. Большинство
    # набрали ДВА куска сразу: июнь–сентябрь и декабрь–февраль
    votes = [(6, 9), (6, 9), (12, 2), (12, 2), (1, 12)]
    assert suggest(votes) == (6, 9), "четыре месяца длиннее трёх"


@pytest.mark.parametrize(
    "start,end,expected",
    [
        (7, 7, ["summer"]),
        (5, 10, ["spring", "summer", "autumn"]),
        (11, 2, ["autumn", "winter"]),
        (1, 12, ["spring", "summer", "autumn", "winter"]),
        (3, 4, ["spring"]),
        (12, 12, ["winter"]),
    ],
)
def test_сезоны_из_дуги(start, end, expected):
    assert seasons_of(start, end) == expected


def test_дуга_словами():
    assert arc_text(5, 10) == "с мая по октябрь"
    assert arc_text(7, 7) == "июль"
    assert arc_text(11, 2) == "с ноября по февраль"
    assert arc_text(5, 10, "uz") == "may — oktabr"
