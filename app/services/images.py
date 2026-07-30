"""Генерация превью и dev-заглушек для фото мест."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from ..config import PHOTOS_DIR, THUMBS_DIR

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
    "other": ("#22c55e", "#14532d"),
}


def make_thumbnail(photo_filename: str) -> Path:
    src = PHOTOS_DIR / photo_filename
    dst = THUMBS_DIR / f"{Path(photo_filename).stem}_thumb.jpg"
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        thumb = ImageOps.fit(im, THUMB_SIZE, Image.Resampling.LANCZOS)
        thumb.save(dst, "JPEG", quality=82)
    return dst


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

    im = im.filter(ImageFilter.GaussianBlur(0.5))
    draw = ImageDraw.Draw(im)
    try:
        font = ImageFont.load_default(size=72)
        small = ImageFont.load_default(size=36)
    except TypeError:  # у очень старых Pillow load_default без size
        font = small = ImageFont.load_default()
    draw.text((80, h - 260), title, font=font, fill="white")
    draw.text((80, h - 150), "Фото-заглушка — замените в админке", font=small, fill="#e2e8f0")

    dst = PHOTOS_DIR / filename
    im.save(dst, "JPEG", quality=85)
    make_thumbnail(filename)
    return dst
