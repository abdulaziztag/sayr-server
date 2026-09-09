"""Корзина удалённых снимков.

Главное здесь — не механика переноса, а место корзины на диске. Боевой юнит
идёт с ProtectSystem=strict и ReadWritePaths=<...>/media, systemd выполняет
это bind-монтированием, и rename() из media/photos наружу упирается в EXDEV.
С 24 по 26 августа так молча потерялись 55 снимков. Отсюда два теста-сторожа:
корзина обязана лежать внутри media_dir — и обязана не раздаваться наружу.
"""

from pathlib import Path

import pytest

from app.config import DELETED_PHOTOS_DIR, GPX_DIR, PHOTOS_DIR, THUMBS_DIR, settings
from app.services.images import retire_photo


@pytest.fixture
def photo_on_disk():
    """Кладёт снимок с миниатюрой и убирает за собой всё, что осталось."""
    written: list[Path] = []

    def make(name: str) -> str:
        stem = Path(name).stem
        for path in (PHOTOS_DIR / name, THUMBS_DIR / f"{stem}_thumb.jpg"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"jpeg-" + name.encode())
            written.append(path)
        return name

    yield make

    for path in written:
        path.unlink(missing_ok=True)
    if DELETED_PHOTOS_DIR.exists():
        for path in DELETED_PHOTOS_DIR.iterdir():
            path.unlink()


def test_trash_lives_inside_media_dir():
    """Иначе rename() уедет через границу монтирования и упадёт с EXDEV."""
    assert DELETED_PHOTOS_DIR.is_relative_to(settings.media_dir)


def test_trash_is_not_under_a_served_directory():
    """Внутри media_dir — но не там, откуда StaticFiles отдаёт файлы."""
    for served in (PHOTOS_DIR, THUMBS_DIR, GPX_DIR):
        assert not DELETED_PHOTOS_DIR.is_relative_to(served)


def test_photo_and_thumb_move_to_trash(photo_on_disk):
    name = photo_on_disk("test-retire.jpg")

    moved = retire_photo(name)

    assert {p.name for p in moved} == {"test-retire.jpg", "test-retire_thumb.jpg"}
    assert all(p.parent == DELETED_PHOTOS_DIR and p.exists() for p in moved)
    # Из выдачи пропали: по прямой ссылке уже не откроются
    assert not (PHOTOS_DIR / name).exists()
    assert not (THUMBS_DIR / "test-retire_thumb.jpg").exists()


def test_moved_file_keeps_its_content(photo_on_disk):
    name = photo_on_disk("test-content.jpg")

    moved = retire_photo(name)

    photo = next(p for p in moved if p.name == name)
    assert photo.read_bytes() == b"jpeg-test-content.jpg"


def test_namesake_does_not_overwrite_predecessor(photo_on_disk):
    """Два места легко держат снимки с одинаковым именем."""
    name = "test-twin.jpg"
    photo_on_disk(name)
    first = retire_photo(name)
    photo_on_disk(name)

    second = retire_photo(name)

    assert {p.name for p in first} & {p.name for p in second} == set()
    assert all(p.exists() for p in first + second)
    assert (DELETED_PHOTOS_DIR / "test-twin-1.jpg").exists()


def test_missing_file_does_not_break_deletion():
    """Строку из базы уже удалили — падать на её файле поздно и незачем."""
    assert retire_photo("test-nothing-here.jpg") == []


def test_trash_is_created_on_demand(photo_on_disk):
    name = photo_on_disk("test-mkdir.jpg")
    if DELETED_PHOTOS_DIR.exists():
        for path in DELETED_PHOTOS_DIR.iterdir():
            path.unlink()
        DELETED_PHOTOS_DIR.rmdir()

    retire_photo(name)

    assert DELETED_PHOTOS_DIR.is_dir()


async def test_trash_is_not_served_over_http(client, photo_on_disk):
    """Ради этого корзину и держали вне media_dir — проверяем, что переезд
    внутрь ничего не открыл наружу."""
    name = photo_on_disk("test-http.jpg")
    retire_photo(name)

    assert (DELETED_PHOTOS_DIR / name).exists()
    response = await client.get(f"/media/deleted-photos/{name}")
    assert response.status_code == 404
