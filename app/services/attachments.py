"""Файлы, приложенные к заявке с формы /report.

Словами человек описывает несошедшийся поворот минуту и всё равно
неточно; снимком — за один тап. Поэтому к форме приложены файлы: кадр
развилки, скриншот часов с настоящим временем в пути, трек GPX, который
он записал сам. Всё это владелец смотрит в админке, когда садится править
карточку, — и по этим же файлам решает, верить заявке или нет.

Что здесь есть и чего намеренно нет:

* Тип узнаём по первым байтам, а не по расширению и не по заголовку
  Content-Type. Оба присылает клиент, и оба он же придумывает.
* Байты сохраняем как есть. Пережимать — терять: скриншот после второго
  JPEG становится нечитаемым ровно в том месте, ради которого его и
  прислали. Съёмочные метаданные (EXIF, а в нём координаты) тоже
  оставляем: заявка часто именно про координаты, и стереть их значило бы
  выбросить ответ вместе с вопросом. Наружу файл не отдаётся, так что
  дальше владельца эти координаты не уходят.
* Имя — от содержимого. Один и тот же кадр, присланный дважды, лежит
  на диске один раз.
* Превью — только для картинок и только чтобы список в админке
  открывался на телефоне: четыре восьмимегабайтных кадра в одной
  странице владелец ждать не должен.
"""

import hashlib
import os
from pathlib import Path

from ..config import REPORTS_DIR
from .images import NotAnImage, TooManyPixels, open_upload, shrink

#: Сколько файлов принимаем к одной заявке и по сколько байт каждый.
#:
#: Общий предел держим ниже, чем `client_max_body_size 20m` у nginx
#: (server/deploy/nginx-sayr.conf): всё, что толще, обрывается ещё до
#: приложения, и человек видит не наши слова, а страницу ошибки nginx
MAX_FILES = 4
MAX_BYTES = 8 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024

THUMB_SIDE = 720

#: Что принимаем. Расширение и mime — наши, не присланные; проверка
#: смотрит на сами байты.
#:
#: Списка «всё, кроме опасного» здесь нет намеренно: он всегда неполный.
#: HTML и SVG не приняты как раз поэтому — открытые в браузере владельца,
#: они выполняют чужой скрипт на нашем домене.
JPEG = ("jpg", "image/jpeg")
PNG = ("png", "image/png")
WEBP = ("webp", "image/webp")
HEIC = ("heic", "image/heic")
PDF = ("pdf", "application/pdf")
GPX = ("gpx", "application/gpx+xml")

#: Марки контейнера HEIF, которыми подписывает снимки айфон
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis"}

IMAGE_TYPES = {JPEG[1], PNG[1], WEBP[1], HEIC[1]}

#: Какие из них мы раскрываем сами, чтобы сделать превью, — и под каким
#: именем формата их открывать. Ровно тем, что показали первые байты:
#: файл с подписью PNG Pillow не станет разбирать как что-то ещё
_DECODED = {JPEG[1]: "JPEG", PNG[1]: "PNG", WEBP[1]: "WEBP"}


class Rejected(Exception):
    """Файл не приняли. `reason` — код, по нему страница берёт слова.

    Кодом, а не готовой фразой: форма двуязычная, и русский текст,
    собранный здесь, пришлось бы переводить обратно на странице.
    """

    def __init__(self, reason: str, filename: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.filename = filename


def sniff(data: bytes) -> tuple[str, str] | None:
    """Расширение и mime по первым байтам. `None` — не наш тип."""
    if data[:3] == b"\xff\xd8\xff":
        return JPEG
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return PNG
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return WEBP
    if data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return HEIC
    if data[:5] == b"%PDF-":
        return PDF
    # GPX — это XML, и заголовка у него нет. Ищем корневой тег в начале
    # файла: перед ним стоит объявление XML и иногда комментарий, но
    # не килобайт текста
    head = data[:2048].lstrip()
    if head[:5] == b"<?xml" or head[:4] == b"<gpx":
        if b"<gpx" in data[:4096].lower():
            return GPX
    return None


def thumb_name(name: str) -> str:
    return f"{Path(name).stem}-thumb.jpg"


def save(data: bytes, original_name: str) -> tuple[str, str, int, bool]:
    """Кладёт файл на диск. Отдаёт (имя, mime, размер, новый ли он).

    Последнее — про то, лежал ли такой файл здесь до нас. Имя собрано из
    содержимого, поэтому один и тот же кадр в двух заявках — один файл,
    и уборка за неудавшейся отправкой не должна его унести.

    Поднимает `Rejected`, если файл пуст, толще предела, не того типа или
    картинка в нём больше settings.image_max_pixels. Всё это проверяется
    до записи: отказ после неё оставлял бы на диске файл, о котором
    не знает ни одна строка в базе, — и лежал бы он там вечно.

    Картинку раскрывает, чтобы сделать превью, — звать через off_loop.
    """
    if not data:
        raise Rejected("empty", original_name)
    if len(data) > MAX_BYTES:
        raise Rejected("too_big", original_name)
    kind = sniff(data)
    if kind is None:
        raise Rejected("bad_type", original_name)

    ext, mime = kind
    decoded = _DECODED.get(mime)
    if decoded:
        _check_image(data, decoded, original_name)
    name = f"{hashlib.sha1(data).hexdigest()[:16]}.{ext}"
    path = REPORTS_DIR / name
    fresh = not path.exists()
    path.write_bytes(data)
    if decoded:
        _make_thumb(data, name, decoded)
    return name, mime, len(data), fresh


def _check_image(data: bytes, decoded: str, original_name: str) -> None:
    """Картинка открывается и не больше предела по пикселям.

    Читается один заголовок, сами пиксели не трогаем. PNG в несколько
    сотен килобайт с размером 13000×13000 до этой проверки разворачивался
    на превью в полтора гигабайта памяти — с формы, открытой всем без входа.
    """
    try:
        with open_upload(data, THUMB_SIDE, (decoded,)):
            pass
    except TooManyPixels:
        raise Rejected("too_large", original_name) from None
    except NotAnImage:
        raise Rejected("bad_type", original_name) from None


def _make_thumb(data: bytes, name: str, decoded: str) -> None:
    """Превью 720 px рядом с оригиналом. Не вышло — и ладно.

    HEIC сюда не попадает вовсе: Pillow без отдельного плагина его
    не открывает, а тащить плагин в зависимости ради превью незачем.
    Сам файл сохранён и открывается, в списке вместо картинки будет ссылка.

    Ловим любое исключение, а не только «не картинка». Заголовок уже
    проверен, но пиксели за ним бывают битыми, и Pillow отвечает на это
    кто чем — OSError, SyntaxError, struct.error. Файл в этот момент уже
    на диске, и пятисотая вместо «спасибо» оставила бы его сиротой.
    """
    try:
        with open_upload(data, THUMB_SIDE, (decoded,)) as im:
            shrink(im, THUMB_SIDE).save(REPORTS_DIR / thumb_name(name), "JPEG", quality=80)
    except Exception:  # noqa: BLE001 — превью необязательно, заявка важнее
        return


def stored_bytes() -> int:
    """Сколько места уже занимают файлы заявок вместе с превью."""
    total = 0
    with os.scandir(REPORTS_DIR) as entries:
        for entry in entries:
            if entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
    return total


def drop(name: str) -> None:
    """Убирает файл и его превью с диска.

    Насовсем, без корзины: на /privacy обещано, что разобранная заявка
    удаляется вместе с присланным, и тихая копия в соседней папке делала
    бы это обещание неправдой. Снимкам каталога корзина полагается —
    те мы искали руками и второй раз можем не найти; этот файл прислал
    человек, у которого он и остался.
    """
    for path in (REPORTS_DIR / name, REPORTS_DIR / thumb_name(name)):
        path.unlink(missing_ok=True)
