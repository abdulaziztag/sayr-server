"""Картинки от чужих людей: фото анкеты.

Файл к заявке /report проверяется в test_reports.py, здесь — фото анкеты
и общие для них обоих open_upload и shrink. Главное, что сторожат эти
тесты: лёгкий файл с огромным кадром отбивается по заголовку, до
распаковки, и ни одна поломка внутри Pillow не превращается в пятисотую.
PNG в двадцать килобайт размером 13300×13300 раскрывался в гигабайт
с лишним, а выше 179 мегапикселей ронял ручку.
"""

import io
import struct
import threading
import zlib

import pytest
from PIL import Image
from sqlalchemy import delete

from app.api import me
from app.auth.tokens import new_token
from app.config import AVATARS_DIR, settings
from app.db import SessionLocal
from app.models import User, UserSession
from app.services.images import TooManyPixels, open_upload, shrink


PHONE = "+998907654321"


async def _login() -> str:
    """Человек с готовым токеном: вход проверяется в test_auth.py."""
    token, digest = new_token()
    async with SessionLocal() as session:
        user = User(phone=PHONE)
        session.add(user)
        await session.flush()
        session.add(UserSession(user_id=user.id, token_hash=digest, device_id="device-0002"))
        await session.commit()
    return token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Device-Id": "device-0002"}


@pytest.fixture(autouse=True)
async def avatars():
    """Что лежало в каталоге фото до теста. Убираем только своё."""
    before = set(AVATARS_DIR.iterdir())
    yield before
    async with SessionLocal() as session:
        await session.execute(delete(User).where(User.phone == PHONE))
        await session.commit()
    for path in set(AVATARS_DIR.iterdir()) - before:
        path.unlink()


def _png_header(width: int, height: int) -> bytes:
    """PNG, у которого есть только заголовок: размер любой, весит сотню байт.

    Настоящий кадр 20000×20000 в тесте не собрать — он и есть та бомба,
    от которой защищаемся. Пикселей за заголовком почти нет, и попытка
    их раскрыть кончается ошибкой «файл обрезан».
    """
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body)))

    head = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head)
            + chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + chunk(b"IEND", b""))


def _image(fmt: str, size: tuple[int, int] = (900, 600), **params) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (47, 93, 63)).save(buf, fmt, **params)
    return buf.getvalue()


async def _upload(client, token: str, data: bytes, name: str = "photo.png"):
    return await client.post(
        "/api/v1/me/avatar",
        files={"file": (name, data, "application/octet-stream")},
        headers=_auth(token),
    )


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
async def test_фото_с_огромным_кадром_отбивается_без_500(client, avatars):
    """400 мегапикселей — отказ самого Pillow, 49 — наш предел. Оба —
    внятный отказ, а не пятисотая и не гигабайт памяти."""
    token = await _login()
    for side in (20000, 7000):
        resp = await _upload(client, token, _png_header(side, side))
        assert resp.status_code == 422, side
        assert resp.json()["detail"] == "image_too_large"
    assert set(AVATARS_DIR.iterdir()) == avatars


async def test_предел_по_пикселям_берётся_из_настроек(client, monkeypatch):
    token = await _login()
    monkeypatch.setattr(settings, "image_max_pixels", 900 * 600 - 1)
    resp = await _upload(client, token, _image("JPEG"), "photo.jpg")
    assert resp.status_code == 422

    monkeypatch.setattr(settings, "image_max_pixels", 900 * 600)
    resp = await _upload(client, token, _image("JPEG"), "photo.jpg")
    assert resp.status_code == 200


async def test_фото_только_jpeg_png_webp(client):
    """Остальное Pillow тоже открыл бы, но каждый его разборщик — лишний
    путь для чужих байтов. Приложения и так шлют JPEG."""
    token = await _login()
    for fmt in ("GIF", "BMP", "TIFF"):
        resp = await _upload(client, token, _image(fmt), f"photo.{fmt.lower()}")
        assert resp.status_code == 415, fmt
        assert resp.json()["detail"] == "not_an_image"
    for fmt in ("JPEG", "PNG", "WEBP"):
        resp = await _upload(client, token, _image(fmt), f"photo.{fmt.lower()}")
        assert resp.status_code == 200, fmt


async def test_битое_фото_отбивается_а_не_роняет_ручку(client, avatars):
    """Заголовок цел, пиксели нет: для человека это одно — не картинка."""
    token = await _login()
    resp = await _upload(client, token, _png_header(100, 100))
    assert resp.status_code == 415
    assert set(AVATARS_DIR.iterdir()) == avatars


async def test_фото_раскрывается_не_в_цикле_событий(client, monkeypatch):
    threads = []
    store = me.store_avatar

    def spy(data, user_id):
        threads.append(threading.get_ident())
        return store(data, user_id)

    monkeypatch.setattr(me, "store_avatar", spy)
    token = await _login()
    resp = await _upload(client, token, _image("JPEG"), "photo.jpg")
    assert resp.status_code == 200
    assert threads and threading.get_ident() not in threads


async def test_фото_встаёт_по_exif_и_теряет_его(client):
    """Уменьшаем раньше, чем поворачиваем, — поворот от этого не теряется.

    Кадр 4000×3000 JPEG распаковывается сразу вполовину (draft), и EXIF
    с поворотом обязан пережить и это. В сохранённом файле EXIF нет:
    координаты съёмки и серийный номер камеры за борт.
    """
    exif = Image.Exif()
    exif[0x0112] = 6  # повернуть на 90° по часовой
    exif[0x0110] = "Test Camera"
    data = _image("JPEG", (4000, 3000), exif=exif.tobytes())

    token = await _login()
    resp = await _upload(client, token, data, "photo.jpg")
    assert resp.status_code == 200
    saved = AVATARS_DIR / resp.json()["avatar_url"].rsplit("/", 1)[-1]
    with Image.open(saved) as im:
        assert im.size == (384, 512), "портретный кадр лёг боком"
        assert not im.getexif(), "EXIF доехал до сохранённого фото"


def test_палитра_уменьшается_со_сглаживанием():
    """Палитровый скриншот уменьшается в RGB, а не по ближайшему пикселю."""
    im = Image.new("RGB", (400, 400), "white")
    for x in range(0, 400, 2):
        im.paste((0, 0, 0), (x, 0, x + 1, 400))
    buf = io.BytesIO()
    im.convert("P").save(buf, "PNG")

    with open_upload(buf.getvalue()) as opened:
        assert opened.mode == "P"
        small = shrink(opened, 100)
    assert small.mode == "RGB" and small.size == (100, 100)
    # Полосы через пиксель при сглаживании сливаются в серое; ближайший
    # пиксель дал бы чистые чёрный и белый
    assert 64 < small.getpixel((50, 50))[0] < 192


def test_предел_проверяется_по_заголовку(monkeypatch):
    monkeypatch.setattr(settings, "image_max_pixels", 1000)
    with pytest.raises(TooManyPixels):
        open_upload(_png_header(100, 100))
    with open_upload(_png_header(10, 10)) as im:
        assert im.size == (10, 10)
