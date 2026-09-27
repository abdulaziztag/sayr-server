"""Фильтр мата для того, что видят чужие: заметки комнат, имя и фамилия.

Магазины требуют фильтр с того момента, как пользовательский текст видят
посторонние (App Review 1.2, политика Play о контенте пользователей).
Цель — отсечь очевидное, а не модерировать по-взрослому: остальное ловят
жалобы. Поэтому корни подобраны так, чтобы не задевать обычные слова:
«себе», «требуется», «хуже», «сикл», «Самарканд» проходят.

Слова сверяются целиком по корню в начале слова (с частыми приставками),
кроме «пизд», который ругательство в любом месте. Перед сверкой: нижний
регистр, ё → е, похожие латинские буквы → кириллица для русских корней,
повторы букв схлопываются («хууууй»).
"""

import re

# Латинские двойники кириллических букв: «xуй», «cука» набирают и так
_LOOKALIKE = str.maketrans("aeopcxykmtbh", "аеорсхукмтвн")

_PREFIX = r"(?:за|вы|от|на|по|у|раз|рас|до|пере|недо|при|подъ|въ|съ|объ|отъ)?"

_RUSSIAN = [
    re.compile(_PREFIX + r"ху[йеяи]"),
    re.compile(r".*пизд"),
    # «еб» только с приставкой или в начале и только перед этими буквами:
    # «себе», «требую», «хлебу» так не ловятся
    re.compile(_PREFIX + r"[е]б[аулнит]"),
    re.compile(r"бля"),
    re.compile(r"сук[аи]$|сучк|суча"),
    re.compile(r"муд[ао]к|мудил|мудозвон"),
    re.compile(r"пид[оа]р|пидр"),
    re.compile(r"залуп"),
    re.compile(r"г[ао]ндон"),
    re.compile(r"шлюх"),
    re.compile(r"дроч"),
    re.compile(r"манд[ауоы]$|мандав"),
    re.compile(r"д[ао]лб[ао][её]б"),
    # узбекская кириллица
    re.compile(r"жалаб"),
    re.compile(r"қанжиқ|канжик"),
    # формы глагола, а не корень: «сикл», «сикамор» проходят
    re.compile(r"сик(аман|ай|дим|иб|иш|тир)"),
]

_LATIN = [
    # узбекская латиница
    re.compile(r"jalab"),
    re.compile(r"qanjiq"),
    re.compile(r"sik(aman|ay|dim|ib|ish|tir)"),
    re.compile(r"q[oö]'?toq|kotok"),
    re.compile(r"d[ao]lb[ao]y[oe]b"),
    # английский
    re.compile(r"(mother)?fuck"),
    # не корень целиком: «shiitake» после схлопывания повторов — «shitake»
    re.compile(r"shit(s|ty|head)?$"),
    re.compile(r"bitch"),
    re.compile(r"cunt"),
    re.compile(r"asshole"),
    re.compile(r"whore"),
    re.compile(r"slut"),
]

_WORD = re.compile(r"[^\W\d_]+(?:['ʻʼ`][^\W\d_]+)?")


def _squeeze(word: str) -> str:
    """«хууууй» → «хуй»: повтор букв — обычный способ обойти фильтр"""
    return re.sub(r"(.)\1+", r"\1", word)


def is_clean(text: str | None) -> bool:
    """True — в тексте нет ругательств из списка."""
    if not text:
        return True
    for raw in _WORD.findall(text.lower().replace("ё", "е")):
        word = _squeeze(raw)
        cyrillic = _squeeze(raw.translate(_LOOKALIKE))
        if any(p.match(cyrillic) or p.match(word) for p in _RUSSIAN):
            return False
        if any(p.match(word) for p in _LATIN):
            return False
    return True
