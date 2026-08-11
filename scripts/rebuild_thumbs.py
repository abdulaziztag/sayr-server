"""Пересобирает все миниатюры: `uv run python -m scripts.rebuild_thumbs`.

Нужен один раз после смены правила нарезки. Старые миниатюры обрезались
до ровных 640×400 (ImageOps.fit) и теряли форму оригинала; новые
вписываются в габарит с сохранением соотношения сторон.

Идемпотентен: просто перезаписывает файлы в media/thumbs.
"""

from PIL import Image

from app.config import PHOTOS_DIR, THUMBS_DIR
from app.services.images import make_thumbnail

SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def main() -> None:
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    photos = sorted(p for p in PHOTOS_DIR.iterdir() if p.suffix.lower() in SUFFIXES)
    print(f"фотографий: {len(photos)}")

    changed = failed = 0
    for photo in photos:
        try:
            with Image.open(photo) as im:
                before = im.size
            dst = make_thumbnail(photo.name)
            with Image.open(dst) as th:
                after = th.size
            same = abs(before[0] / before[1] - after[0] / after[1]) < 0.01
            print(f"  {photo.name:34} {before[0]}x{before[1]} → {after[0]}x{after[1]}"
                  f"{'' if same else '  ← соотношение НЕ совпало'}")
            changed += 1
        except Exception as exc:  # битый файл не должен ронять весь прогон
            failed += 1
            print(f"  {photo.name:34} ОШИБКА: {exc}")

    print(f"\nпересобрано: {changed}, с ошибкой: {failed}")


if __name__ == "__main__":
    main()
