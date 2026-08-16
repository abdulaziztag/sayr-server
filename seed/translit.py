"""Узбекская латиница → кириллица для названий из tabiatsari.uz.

Каталог русскоязычный, а источник отдаёт «Bigiztosh va Shoxqo'rg'on
cho'qqilari». В выборе маршрута такое имя стоит рядом с нашими «По новой
грунтовке» и «Классикой от Аксая», и латиница там смотрится чужеродно.

Это транслитерация, а не перевод: устоявшиеся русские имена мест
(Boboytog' — «Бабайтаг», а не «Бобойтог») она не восстановит. Для названий
маршрутов этого достаточно, а имена мест мы пишем руками.
"""

import re

# Апостроф в узбекской латинице пишут по-разному: прямой, типографский,
# модификатор. Приводим к одному, иначе o' и oʻ разойдутся по правилам
_APOSTROPHES = "'`’‘ʻʼ"

# Сначала длинные сочетания: иначе sh распадётся на s+h, а o' — на o+'
_PAIRS = [
    ("o'", "у"),  # Toshkent → Тошкент, qo'rg'on → кургон
    ("g'", "г"),
    ("sh", "ш"),
    ("ch", "ч"),
    ("yo", "ё"),
    ("yu", "ю"),
    ("ya", "я"),
    ("ye", "е"),
    ("ts", "ц"),
]

_LETTERS = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г",
    "h": "х", "i": "и", "j": "ж", "k": "к", "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у",
    "v": "в", "w": "в", "x": "х", "y": "й", "z": "з",
}


def to_cyrillic(text: str) -> str:
    """«Katta Chimyon» → «Катта Чимён». Цифры, скобки и знаки не трогаем."""
    # Разбираем именно буквенные куски, а не слова целиком: иначе
    # «Obi-Rahmat» и «(Nefrit)» теряют заглавную — регистр определялся бы
    # по дефису и скобке
    return re.sub(rf"[A-Za-z{_APOSTROPHES}]+", lambda m: _word(m.group()), text)


def _word(word: str) -> str:
    upper = word[:1].isupper()
    src = word.lower()
    for ch in _APOSTROPHES:
        src = src.replace(ch, "'")

    result = []
    i = 0
    while i < len(src):
        pair = src[i : i + 2]
        replacement = next((v for k, v in _PAIRS if k == pair), None)
        if replacement is not None:
            result.append(replacement)
            i += 2
            continue
        # Одинокий апостроф — твёрдый знак узбекского письма, в русской
        # записи его просто не пишут
        if src[i] == "'":
            i += 1
            continue
        result.append(_LETTERS.get(src[i], src[i]))
        i += 1

    word_out = "".join(result)
    return word_out[:1].upper() + word_out[1:] if upper else word_out
