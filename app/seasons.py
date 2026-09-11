"""Круг года: месяцы, дуги и подсчёт согласия.

Сезон места — это дуга по кругу из двенадцати месяцев: «с мая по октябрь».
Круг замкнут, поэтому дуга 11 → 2 — это ноябрь, декабрь, январь, февраль,
а не «пусто»; на этом ломается всякая наивная проверка `from <= to`,
и потому вся работа с дугой живёт в одном месте.

Модуль намеренно не знает ни про базу, ни про HTTP — как `app/reports.py`
для формы обращений. Справочник и математика здесь, ручки зовут их.
"""

#: Сколько ответов достаточно, чтобы место ушло из игры и встало
#: в очередь на проверку. Три, а не пять: сто сорок семь мест при пороге
#: пять требуют семисот тридцати пяти ответов, при трёх — четырёхсот сорока
#: одного. Считаются только ответы с месяцами: три «не знаю» — это
#: не знание о месте, и выводить его из игры они не должны
ENOUGH_VOTES = 3

#: Сколько карточек уезжает игроку одной пачкой. Между карточками не должно
#: быть ожидания сети, иначе игра перестаёт быть игрой
DECK_SIZE = 10

#: С чего стоит круг, когда карточка только появилась: апрель–август.
#: Решение владельца (11 сентября 2026): пустой круг заставлял человека
#: сначала понять, как вообще начать, а готовую дугу достаточно подвинуть.
#: Цена известна — «Готово» без единого касания даст ровно эту дугу, —
#: и проверяющий видит её в разбросе ответов
DEFAULT_ARC = (4, 8)

#: Короткие имена месяцев, в родительном нет нужды: в центре круга
#: стоит «АПР — АВГ», и полные слова туда не помещаются
FULL_RU_UP = ("ЯНВАРЬ", "ФЕВРАЛЬ", "МАРТ", "АПРЕЛЬ", "МАЙ", "ИЮНЬ",
              "ИЮЛЬ", "АВГУСТ", "СЕНТЯБРЬ", "ОКТЯБРЬ", "НОЯБРЬ", "ДЕКАБРЬ")
FULL_UZ_UP = ("YANVAR", "FEVRAL", "MART", "APREL", "MAY", "IYUN",
              "IYUL", "AVGUST", "SENTABR", "OKTABR", "NOYABR", "DEKABR")


def months_word(count: int, lang: str = "ru") -> str:
    """«1 месяц», «3 месяца», «5 месяцев»; по-узбекски — «5 oy»."""
    if lang == "uz":
        return f"{count} oy"
    tail = count % 100
    if tail % 10 == 1 and tail != 11:
        word = "месяц"
    elif 2 <= tail % 10 <= 4 and not 12 <= tail <= 14:
        word = "месяца"
    else:
        word = "месяцев"
    return f"{count} {word}"


def centre_lines(start: int, end: int, lang: str = "ru") -> tuple[str, str]:
    """Что стоит в центре круга: диапазон крупно, длина — мелко под ним.

    Круглый год — отдельными словами: «ЯНВ — ДЕК» при дуге с мая по апрель
    читался бы как ошибка.
    """
    span = (end - start) % 12 + 1
    short = SHORT_UZ if lang == "uz" else SHORT_RU
    full = FULL_UZ_UP if lang == "uz" else FULL_RU_UP
    if span == 12:
        return ("YIL BOʻYI" if lang == "uz" else "ВЕСЬ ГОД"), ""
    if start == end:
        return full[start - 1], months_word(1, lang)
    return f"{short[start - 1]} — {short[end - 1]}", months_word(span, lang)

#: Месяцы для подписей на круге — коротко и капсом
SHORT_RU = ("ЯНВ", "ФЕВ", "МАР", "АПР", "МАЙ", "ИЮН",
            "ИЮЛ", "АВГ", "СЕН", "ОКТ", "НОЯ", "ДЕК")
SHORT_UZ = ("YAN", "FEV", "MAR", "APR", "MAY", "IYN",
            "IYL", "AVG", "SEN", "OKT", "NOY", "DEK")

#: Родительный падеж: «с мая по октябрь». Узбекские имена — те же, что
#: в календаре приложений (android/res/values-uz/strings_detail.xml)
FROM_RU = ("января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря")
TO_RU = ("январь", "февраль", "март", "апрель", "май", "июнь",
         "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")
MONTHS_UZ = ("yanvar", "fevral", "mart", "aprel", "may", "iyun",
             "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr")

#: Какому сезону принадлежит месяц. Порядок кодов — как в `models.Season`
_SEASON_OF = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}
_SEASON_ORDER = ("spring", "summer", "autumn", "winter")


#: Что мешает попасть в место, кроме погоды. Код, русская подпись, узбекская.
#:
#: Появилось после разговора с Данилой Алябьевым (11 сентября 2026): сезон
#: отвечает на «когда», а человеку нужно ещё «пустят ли». Погранзона на
#: Наували, заповедник на Бешторе, охота осенью, Кумбель, закрытый прямо
#: сейчас, — всё это разные причины, и смешивать их со сложностью нельзя.
#: Кодами, а не текстом: по коду в карточке можно показать нужную плашку.
#: Узбекские слова — по docs/uz-glossary.md
LIMITS: tuple[tuple[str, str, str], ...] = (
    ("border", "Погранзона", "Chegara hududi"),
    ("law", "Закрыто по закону", "Qonun bilan yopiq"),
    ("hunting", "Сезон охоты", "Ov mavsumi"),
    ("closed", "Сейчас закрыто", "Hozir yopiq"),
)
LIMIT_CODES = frozenset(code for code, _, _ in LIMITS)

#: Зимние месяцы: дуга, задевающая хоть один, открывает вопрос о тропёжке
WINTER = frozenset({12, 1, 2})


def touches_winter(start: int, end: int) -> bool:
    """Задевает ли дуга зиму — тогда имеет смысл спросить про снег."""
    return any(month in WINTER for month in arc_months(start, end))


def score(value: object) -> int | None:
    """Балл от 1 до 10 или ничего. Числа приходят из формы — доверять нельзя."""
    return value if isinstance(value, int) and 1 <= value <= 10 else None


def median(values: list[int]) -> int | None:
    """Середина ответов, половина — вверх: для «2 и 3» это 3.

    Середина, а не среднее: один «10» от перестраховщика не должен
    утащить за собой спокойное место.
    """
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid] + 1) // 2


def valid(month: object) -> bool:
    """Месяц ли это. Числа приходят из формы, доверять им нельзя."""
    return isinstance(month, int) and 1 <= month <= 12


def arc_months(start: int, end: int) -> list[int]:
    """Месяцы дуги, включая оба конца и переход через декабрь.

    `5 → 10` — с мая по октябрь. `11 → 2` — ноябрь, декабрь, январь,
    февраль. `7 → 7` — один июль. `1 → 12` — весь год.
    """
    months = [start]
    while months[-1] != end:
        months.append(months[-1] % 12 + 1)
    return months


def suggest(votes: list[tuple[int, int]]) -> tuple[int, int] | None:
    """Что предложить проверяющему по ответам игроков.

    Месяц входит в сезон, если за него больше половины ответивших.
    Из вошедших берётся самая длинная непрерывная дуга — круг замкнут,
    декабрь соседствует с январём.

    При трёх ответах «больше половины» — это два. При одном ответе
    предложением становится он сам. Ответили вразнобой и ни один месяц
    не набрал большинства — предложения нет, дугу поставит человек.

    Правило нарочно объяснимо одной фразой: итог всё равно смотрит
    проверяющий, и он должен понимать, откуда взялась жирная дуга.
    На вход идут только ответы с месяцами — «не знаю» сюда не доезжает.
    """
    if not votes:
        return None
    count = [0] * 13
    for start, end in votes:
        for month in arc_months(start, end):
            count[month] += 1
    need = len(votes) // 2 + 1
    keep = [month for month in range(1, 13) if count[month] >= need]
    if not keep:
        return None
    if len(keep) == 12:
        return 1, 12

    # Самый длинный забор идёт по кругу, поэтому список месяцев проходим
    # дважды: разрыв между декабрём и январём иначе разрезал бы зимнюю дугу
    best: list[int] = []
    run: list[int] = []
    kept = set(keep)
    for month in [m for m in range(1, 13)] * 2:
        if month in kept:
            run.append(month)
            if len(run) > len(best):
                best = run[:]
        else:
            run = []
        if len(best) == len(kept):
            break
    return best[0], best[-1]


def seasons_of(start: int, end: int) -> list[str]:
    """Коды сезонов, которых касается дуга.

    Сезон входит, если внутри дуги есть хоть один его месяц. Тонкости
    «открыто, но летом пекло» здесь нет намеренно: игра задаёт один
    вопрос, и `best_seasons` теперь значат «открыто», а не «лучше всего»
    (спека 2026-09-11-season-game-design.md).
    """
    touched = {_SEASON_OF[month] for month in arc_months(start, end)}
    return [season for season in _SEASON_ORDER if season in touched]


def arc_text(start: int, end: int, lang: str = "ru") -> str:
    """Дуга словами: «с мая по октябрь», «may — oktabr»."""
    if lang == "uz":
        if start == end:
            return MONTHS_UZ[start - 1]
        return f"{MONTHS_UZ[start - 1]} — {MONTHS_UZ[end - 1]}"
    if start == end:
        return TO_RU[start - 1].lower()
    return f"с {FROM_RU[start - 1]} по {TO_RU[end - 1].lower()}"
