"""Справочник для формы обратной связи: темы, статусы, разбор контакта.

Отдельный модуль, потому что список тем нужен в трёх местах сразу —
странице `/report`, разборе ответа и админке. Держать его внутри
любого из них значит, что двум другим придётся тянуть чужой модуль
ради одной таблицы.
"""

import re

#: Что бывает не так с карточкой. Код, русская подпись, узбекская.
#:
#: Кодами, а не свободным текстом: по коду видно, куда смотреть, и заявки
#: на одну и ту же беду собираются в кучу. Порядок — от того, что чаще
#: врёт и дороже стоит человеку (время, дорога, трек), к мелочам.
TOPICS: tuple[tuple[str, str, str], ...] = (
    ("duration", "Ходовое время", "Yurish vaqti"),
    ("drive", "Дорога и время выезда", "Yoʻl va chiqish vaqti"),
    ("track", "Трек GPX", "GPX trek"),
    ("start", "Точка старта, координаты", "Start nuqtasi, koordinatalar"),
    ("distance", "Длина маршрута", "Marshrut uzunligi"),
    ("ascent", "Набор высоты", "Koʻtarilish"),
    ("difficulty", "Сложность", "Murakkablik"),
    ("season", "Сезон", "Mavsum"),
    ("access", "Доступ, пропуска", "Ruxsat, propusk"),
    ("photos", "Фотографии", "Suratlar"),
    ("text", "Название и описание", "Nomi va tavsifi"),
    ("missing", "Места нет в каталоге", "Katalogda joy yoʻq"),
    ("other", "Другое", "Boshqa"),
)

TOPIC_CODES = frozenset(code for code, _, _ in TOPICS)

_TOPIC_RU = {code: ru for code, ru, _ in TOPICS}


def topic_names(codes: list[str] | None) -> str:
    """Коды в человеческие слова — для админки и писем."""
    if not codes:
        return "—"
    return ", ".join(_TOPIC_RU.get(code, code) for code in codes)


#: Куда категория места попадает в подписи выпадающего списка. Слова те же,
#: что на странице места (app/api/share.py) и в приложениях
CATEGORY_RU = {
    "waterfall": "водопад", "peak": "пик", "gorge": "ущелье", "cave": "пещера",
    "lake": "озеро", "canyon": "каньон", "spring": "родник", "plateau": "плато",
    "petroglyphs": "петроглифы", "reserve": "нацпарк", "desert": "пустыня",
    "other": "место",
}

CATEGORY_UZ = {
    "waterfall": "sharshara", "peak": "choʻqqi", "gorge": "dara", "cave": "gʻor",
    "lake": "koʻl", "canyon": "kanyon", "spring": "buloq", "plateau": "plato",
    "petroglyphs": "petrogliflar", "reserve": "milliy bogʻ", "desert": "choʻl",
    "other": "joy",
}

CATEGORY = {"ru": CATEGORY_RU, "uz": CATEGORY_UZ}

STATUS_RU = {
    "new": "новая",
    "taken": "в работе",
    "fixed": "исправлено",
    "rejected": "не подтвердилось",
}

#: Ник в телеграме: буквы, цифры и подчёркивание, от пяти знаков.
#: Почту и телефон нарочно не трогаем — их пишут как хотят
_TG_NAME = re.compile(r"^@?([A-Za-z0-9_]{5,32})$")
_TG_LINK = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})/?$",
    re.IGNORECASE,
)


def normalize_contact(raw: str) -> str:
    """`@ник` из чего угодно телеграмного; остальное — как ввели.

    Люди пишут ник четырьмя способами: с собакой, без, ссылкой t.me
    и ссылкой с http. В админке это должно выглядеть одинаково, иначе
    один и тот же человек читается как четверо разных.
    """
    value = raw.strip()
    link = _TG_LINK.match(value)
    if link:
        return f"@{link.group(1)}"
    name = _TG_NAME.match(value)
    if name:
        return f"@{name.group(1)}"
    return value


def telegram_url(contact: str | None) -> str | None:
    """Ссылка на переписку — админке, чтобы писать в один клик."""
    if contact and contact.startswith("@"):
        return f"https://t.me/{contact[1:]}"
    return None
