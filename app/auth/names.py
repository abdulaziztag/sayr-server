"""Правило «только настоящее имя».

Многие пишут в Telegram вместо имени символы: «✨Ali✨», «ᏗᏝᎥ», эмодзи,
точки. Такое имя в анкету не переносим — поле остаётся пустым, человек
впишет имя сам. Та же проверка стоит на ручном вводе анкеты: иначе те же
символы вписали бы руками (спека
docs/superpowers/specs/2026-09-30-telegram-apple-login-design.md,
«Имя из Telegram: только настоящее»).

Правило:
1. NFKC — декоративные «шрифты» (𝓐𝓵𝓲, 𝐀𝐳𝐢𝐳) становятся обычными
   буквами, и такие имена не теряются.
2. Только буквы латиницы и кириллицы (с узбекскими o‘ g‘ — апостроф
   бывает и ‘ U+2018, и ʻ U+02BB), пробел, дефис и апостроф. Обратный
   апостроф ` и знак ´, которыми o‘ g‘ набирают на клавиатурах без ‘,
   заменяем на ‘ — так, как узбекские буквы показывают приложения.
3. Букв не меньше двух, длина до 30 символов.
"""

import re
import unicodedata

MAX_LENGTH = 30
MIN_LETTERS = 2

#: Апострофы: прямой, типографские ‘ ’ (так узбекские o‘ g‘ пишут
#: приложения) и ʻ ʼ (так их пишет узбекская орфография и база каталога)
APOSTROPHES = frozenset("'‘’ʻʼ")
#: Дефисы: NFKC неразрывный дефис превращает в U+2010, приводим к обычному
_HYPHENS = str.maketrans({"‐": "-", "‑": "-"})
#: «O`tkir», «G´ulnora» — так o‘ g‘ набирают без узбекской раскладки.
#: Меняем до NFKC: ´ он разбирает на пробел и надстрочный знак, а ｀
#: (широкий) сводит к `, который и попадёт сюда
_BACKTICKS = str.maketrans({"`": "‘", "´": "‘", "｀": "‘"})
_SPACES = re.compile(r"\s+")


def _letter(ch: str) -> bool:
    """Буква латиницы или кириллицы — по имени символа в Юникоде: у всех
    них оно начинается с LATIN или CYRILLIC. «Похожие» буквы других
    письменностей (черокийские ᏗᏝᎥ, греческие) сюда не проходят"""
    if not unicodedata.category(ch).startswith("L"):
        return False
    name = unicodedata.name(ch, "")
    return name.startswith("LATIN ") or name.startswith("CYRILLIC ")


def clean_name(raw: str | None) -> str | None:
    """Имя по правилу или None, если оно правилу не отвечает.

    Пустое на входе — пустая строка: «стереть имя» проверка не запрещает.
    Возвращает уже нормализованное: 𝓐𝓵𝓲 → Ali, лишние пробелы схлопнуты.
    """
    if raw is None:
        return None
    text = unicodedata.normalize("NFKC", raw.translate(_BACKTICKS)).translate(_HYPHENS)
    text = _SPACES.sub(" ", text).strip()
    if not text:
        return ""
    if len(text) > MAX_LENGTH:
        return None
    letters = 0
    for ch in text:
        if _letter(ch):
            letters += 1
        elif ch not in APOSTROPHES and ch not in " -":
            return None
    return text if letters >= MIN_LETTERS else None


def split_name(raw: str | None) -> tuple[str, str]:
    """Полное имя из Telegram → (имя, фамилия) для анкеты.

    Первое слово — имя, остальное — фамилия. Не прошло правило целиком —
    обе пустые: человек впишет сам. Каждая часть проверяется ещё раз
    отдельно: «A Karimov» проходит целиком, но одна буква — не имя
    """
    whole = clean_name(raw)
    if not whole:
        return "", ""
    first, _, last = whole.partition(" ")
    first = clean_name(first) or ""
    if not first:
        return "", ""
    return first, clean_name(last) or ""
