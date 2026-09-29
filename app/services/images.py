"""Генерация превью и dev-заглушек для фото мест."""

import hashlib
import io
from collections.abc import Callable
from pathlib import Path

from anyio import CapacityLimiter, to_thread
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from ..config import AVATARS_DIR, DELETED_PHOTOS_DIR, PHOTOS_DIR, THUMBS_DIR, settings

# Свой предел Pillow держит на 89 мегапикселях: выше предупреждает, выше
# вдвое — отказывает. Опускаем его до нашего, чтобы и то, что открывается
# мимо open_upload, не раскрывалось больше чем вдвое против него. Снимки
# каталога из админки это тоже касается: кадр больше восьмидесяти
# мегапикселей там пропустится, как любой, что не открылся, — остальные
# и так ужимаются до 2560 px
Image.MAX_IMAGE_PIXELS = settings.image_max_pixels

THUMB_SIZE = (640, 400)

# Больше этого в каталоге не нужно: снимки смотрят на телефоне,
# а восьмимегапиксельный кадр с зеркалки — это мегабайты трафика в горах
MAX_SIDE = 2560
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Пары цветов градиента по категории — чтобы заглушки различались на глаз
CATEGORY_COLORS: dict[str, tuple[str, str]] = {
    "waterfall": ("#0ea5e9", "#164e63"),
    "peak": ("#64748b", "#1e293b"),
    "gorge": ("#b45309", "#431407"),
    "cave": ("#6d28d9", "#1e1b4b"),
    "lake": ("#06b6d4", "#0c4a6e"),
    "canyon": ("#ea580c", "#7c2d12"),
    "spring": ("#10b981", "#064e3b"),
    "plateau": ("#84cc16", "#365314"),
    "petroglyphs": ("#a16207", "#422006"),
    "reserve": ("#166534", "#052e16"),
    "desert": ("#d97706", "#78350f"),
    "other": ("#6b7280", "#1f2937"),
}


def make_thumbnail(photo_filename: str) -> Path:
    src = PHOTOS_DIR / photo_filename
    dst = THUMBS_DIR / f"{Path(photo_filename).stem}_thumb.jpg"
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        # Вписываем в габарит, СОХРАНЯЯ соотношение сторон. Раньше был
        # ImageOps.fit — он обрезал кадр до ровных 640×400, и миниатюра
        # получалась другой формы, чем оригинал (1,60 против 1,78 у 16:9).
        # В полноэкранном просмотре она подставляется на время загрузки,
        # и снимок на глазах менял пропорции. Кадрируют пусть клиенты —
        # там, где это нужно по макету (карточки каталога, полароиды).
        thumb = im.copy()
        thumb.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
        thumb.save(dst, "JPEG", quality=82)
    return dst


def store_upload(data: bytes, slug: str) -> str:
    """Кладёт присланный кадр в каталог и делает превью. Отдаёт имя файла.

    Пересобираем в JPEG, а не сохраняем как есть, по трём причинам сразу:
    битый файл под видом картинки не доедет до каталога, а упадёт здесь;
    имя получается из содержимого, поэтому повторная заливка того же кадра
    не плодит копий; и главное — не переносится EXIF, а в нём у снимка
    с телефона лежат координаты съёмки и серийный номер камеры. Это данные
    автора, а не места, и в открытый доступ им не надо.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("файл больше допустимого")
    name = f"{slug}-{hashlib.sha1(data).hexdigest()[:8]}.jpg"
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
        im.save(PHOTOS_DIR / name, "JPEG", quality=88)
    make_thumbnail(name)
    return name


#: Что соглашаемся открывать из присланного чужими людьми. Сам Pillow
#: пробует подряд все свои форматы, от TIFF до файлов древних сканеров,
#: и каждый — лишний разборщик на пути чужих байтов; нам нужны три
UPLOAD_FORMATS = ("JPEG", "PNG", "WEBP")


class NotAnImage(Exception):
    """Присланное не открылось как картинка разрешённого формата."""


class TooManyPixels(Exception):
    """В кадре больше settings.image_max_pixels — раскрывать его не стали."""


def open_upload(
    data: bytes, side: int, formats: tuple[str, ...] = UPLOAD_FORMATS
) -> Image.Image:
    """Открывает присланную картинку, ещё не распаковывая её.

    Image.open читает один заголовок: размер уже известен, а пиксели
    по-прежнему сжаты. Отказ по размеру даём здесь, до распаковки, —
    после неё гигабайт памяти уже занят. Отказ самого Pillow
    (DecompressionBombError) — тот же отказ: он наследует голый Exception
    и мимо узкого except долетал бы до человека пятисотой.

    `side` — до скольких пикселей кадр потом ужмёт shrink. JPEG умеет
    распаковываться сразу уменьшенным — вдвое, вчетверо или в восемь раз
    (draft), и просим его об этом до проверки: размер после draft и есть
    то, что ляжет в память. Снимок 50-мегапиксельной камеры телефона
    раскроется в три мегапикселя и пройдёт, а не отобьётся за пятьдесят.
    Запас вдвое против `side` — тот же, что просит сам thumbnail:
    уменьшению нужно из чего сглаживать. Другим форматам draft ничего
    не делает.
    """
    try:
        im = Image.open(io.BytesIO(data), formats=formats)
        im.draft(None, (side * 2, side * 2))
    except Image.DecompressionBombError as no:
        raise TooManyPixels from no
    except Exception as no:
        raise NotAnImage from no
    width, height = im.size
    if width * height * _weight(im) > settings.image_max_pixels:
        im.close()
        raise TooManyPixels
    return im


def _weight(im: Image.Image) -> int:
    """Во сколько раз этот кадр дороже раскрыть, чем RGB того же размера.

    Предел в настройках посчитан на RGB: раскрыть и ужать его стоит около
    пяти байт на пиксель (Pillow 12, по пиковой памяти процесса). Палитра,
    серый и CMYK — примерно столько же. Прозрачность — вдвое: уменьшает
    её Pillow через полную копию с домноженной альфой, а 16-битный серый
    переводим в RGB до уменьшения (его Pillow не уменьшает вовсе).
    WebP — вчетверо: Pillow раскрывает его через WebPAnimDecoder, у которого
    два полных холста RGBA, и дважды копирует результат — выходит до
    двадцати байт на пиксель. Лёгкий WebP под предел по одним пикселям
    раскрывался в восемьсот мегабайт.
    """
    if im.format == "WEBP":
        return 4
    return 1 if im.mode in ("1", "L", "P", "RGB", "CMYK") else 2


def shrink(im: Image.Image, side: int) -> Image.Image:
    """Открытая картинка — в RGB не больше `side` по длинной стороне.

    Сначала уменьшаем, потом поворачиваем по EXIF и переводим в RGB:
    наоборот поворот и перевод шли бы по полному кадру, по копии на шаг.
    Рамка квадратная, поэтому поворот после уменьшения даёт тот же
    размер, что и до.

    Заранее в RGB переводим только то, что thumbnail сам не уменьшит
    по-человечески: палитру и однобитные он берёт по ближайшему пикселю,
    без сглаживания, и скриншот рассыпается, а 16-битный серый не берёт
    вовсе. Прозрачность и CMYK уменьшаются как есть: перевод до
    уменьшения — лишняя полная копия, а JPEG в CMYK ещё и распаковывался
    бы целиком, мимо draft.
    """
    if im.mode not in ("RGB", "L", "RGBA", "LA", "CMYK"):
        im = im.convert("RGB")
    im.thumbnail((side, side), Image.Resampling.LANCZOS)
    return ImageOps.exif_transpose(im).convert("RGB")


#: Сколько картинок процесс раскрывает разом. Остальные ждут очереди:
#: кадр у предела занимает при раскрытии около двухсот мегабайт
#: (_weight), и десять одновременных отправок не должны раскрыть десять
#: кадров сразу на сервере, который мы делим с чужими сайтами. По одному
#: на воркер — живым отправкам хватает: фото анкеты оба приложения
#: присылают уже ужатым квадратом (squareJPEG / squareJpeg). Соединение
#: с базой на время очереди отпускают сами ручки: пятнадцать отправок,
#: ждущих здесь, иначе держали бы весь пул воркера
_DECODING = CapacityLimiter(1)


async def off_loop[T](func: Callable[..., T], *args) -> T:
    """Работа с присланной картинкой — в отдельном потоке.

    Распаковка кадра на десятки мегапикселей — это секунды процессора.
    В самой корутине они останавливали бы весь воркер: ни ленте, ни админке,
    ни соседней форме он в это время не отвечает.
    """
    return await to_thread.run_sync(func, *args, limiter=_DECODING)


#: Фото из анкеты. 512 пикселей хватает и кружку в профиле, и карточке
#: попутчика: крупнее оно нигде не показывается, а место и трафик экономит
AVATAR_SIDE = 512
MAX_AVATAR_BYTES = 8 * 1024 * 1024


def store_avatar(data: bytes, user_id: int) -> str:
    """Кладёт фото анкеты и отдаёт имя файла.

    Пересобираем в JPEG по той же причине, что и снимки мест: EXIF
    с координатами съёмки и серийным номером камеры остаётся за бортом.
    HEIC сюда не доезжает — Pillow его не открывает без отдельной
    библиотеки, поэтому в JPEG кадр перекодирует само приложение
    (обе платформы умеют это одной строкой).

    Поднимает ValueError (файл тяжелее предела), NotAnImage и TooManyPixels.
    Раскрывает кадр целиком — звать через off_loop.
    """
    if len(data) > MAX_AVATAR_BYTES:
        raise ValueError("файл больше допустимого")
    name = f"u{user_id}-{hashlib.sha1(data).hexdigest()[:8]}.jpg"
    with open_upload(data, AVATAR_SIDE) as im:
        try:
            small = shrink(im, AVATAR_SIDE)
        except Exception as no:
            # Заголовок прочитался, а пиксели нет: обрезанный файл, битый
            # поток сжатия. Pillow отвечает на это кто чем — OSError,
            # SyntaxError, struct.error, — а для человека это одно: не картинка
            raise NotAnImage from no
    small.save(AVATARS_DIR / name, "JPEG", quality=85)
    return name


def drop_avatar(name: str) -> None:
    """Убирает фото анкеты совсем, без корзины.

    У снимков мест корзина есть: их отбирает владелец и вправе промахнуться.
    Здесь наоборот — человек попросил удалить своё лицо, и держать его
    копию «на всякий случай» нельзя.
    """
    (AVATARS_DIR / Path(name).name).unlink(missing_ok=True)


def retire_photo(photo_filename: str) -> list[Path]:
    """Убирает снимок и его миниатюру из выдачи, не стирая их.

    Файлы уезжают в DELETED_PHOTOS_DIR — внутри media_dir, но не под одним
    из трёх смонтированных подкаталогов, поэтому по прямой ссылке они сразу
    перестают открываться. Совсем не удаляем: промах по кнопке в админке
    стоил бы кадра, который искали руками по выгрузке форума, а лишний файл
    на диске не стоит ничего.

    Перенос делается через rename(), а он не пересекает границу
    монтирования. Пока корзина лежала вне media_dir, на бою это давало
    EXDEV на каждом удалении (см. комментарий у DELETED_PHOTOS_DIR):
    каталог обязан оставаться на том же томе, что и photos.

    Имена в корзине не перетираем: два места легко держат снимки
    с одинаковым именем, и второй бы затёр первый.
    """
    DELETED_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(photo_filename).stem
    moved = []
    for src in (PHOTOS_DIR / photo_filename, THUMBS_DIR / f"{stem}_thumb.jpg"):
        if not src.exists():
            continue
        dst = DELETED_PHOTOS_DIR / src.name
        n = 1
        while dst.exists():
            dst = DELETED_PHOTOS_DIR / f"{src.stem}-{n}{src.suffix}"
            n += 1
        src.rename(dst)
        moved.append(dst)
    return moved


def _fonts():
    """Шрифт с кириллицей: у дефолтного bitmap-шрифта Pillow её нет."""
    for path in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, 72), ImageFont.truetype(path, 36)
        except OSError:
            continue
    f = ImageFont.load_default()
    return f, f


def _hex(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def generate_placeholder(filename: str, title: str, category: str) -> Path:
    """Вертикальный градиент + название места; используется, пока нет реальных фото."""
    w, h = 1600, 1000
    top, bottom = (_hex(c) for c in CATEGORY_COLORS.get(category, CATEGORY_COLORS["other"]))
    im = Image.new("RGB", (w, h))
    for y in range(h):
        k = y / (h - 1)
        row = tuple(round(top[i] + (bottom[i] - top[i]) * k) for i in range(3))
        im.paste(Image.new("RGB", (w, 1), row), (0, y))

    # Чистый градиент без текста: подписи на кадрах запрещены дизайн-системой
    im = im.filter(ImageFilter.GaussianBlur(0.5))

    dst = PHOTOS_DIR / filename
    im.save(dst, "JPEG", quality=85)
    make_thumbnail(filename)
    return dst
