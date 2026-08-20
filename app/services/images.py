"""Генерация превью и dev-заглушек для фото мест."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from ..config import DELETED_PHOTOS_DIR, PHOTOS_DIR, THUMBS_DIR

THUMB_SIZE = (640, 400)

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


def retire_photo(photo_filename: str) -> list[Path]:
    """Убирает снимок и его миниатюру из выдачи, не стирая их.

    Файлы уезжают в DELETED_PHOTOS_DIR — вне media_dir, поэтому по прямой
    ссылке они сразу перестают открываться. Совсем не удаляем: промах по
    кнопке в админке стоил бы кадра, который искали руками по выгрузке
    форума, а лишний файл на диске не стоит ничего.

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
