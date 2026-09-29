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
from app.db import SessionLocal, engine
from app.models import User, UserSession
from app.services.images import (AVATAR_SIDE, TooManyPixels, open_upload, shrink,
                                 store_upload)


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


def _webp_header(width: int, height: int) -> bytes:
    """WebP без пикселей: один заголовок VP8L, сорок байт на любой размер.

    За заголовком нули, раскрыть такой кадр нельзя, но размер Image.open
    из него читает — и отказ обязан прийти уже по нему.
    """
    bits = (width - 1) | (height - 1) << 14
    body = b"\x2f" + struct.pack("<I", bits) + b"\x00" * 15
    chunk = b"VP8L" + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


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

    with open_upload(buf.getvalue(), 100) as opened:
        assert opened.mode == "P"
        small = shrink(opened, 100)
    assert small.mode == "RGB" and small.size == (100, 100)
    # Полосы через пиксель при сглаживании сливаются в серое; ближайший
    # пиксель дал бы чистые чёрный и белый
    assert 64 < small.getpixel((50, 50))[0] < 192


def test_предел_проверяется_по_заголовку(monkeypatch):
    monkeypatch.setattr(settings, "image_max_pixels", 1000)
    with pytest.raises(TooManyPixels):
        open_upload(_png_header(100, 100), 512)
    with open_upload(_png_header(10, 10), 512) as im:
        assert im.size == (10, 10)


async def test_лёгкий_webp_под_пределом_отбивается(client, avatars):
    """36 мегапикселей — под пределом для RGB, но WebP раскрывается вчетверо
    дороже: такой кадр в полтора килобайта съедал шестьсот мегабайт."""
    token = await _login()
    resp = await _upload(client, token, _webp_header(6000, 6000), "photo.webp")
    assert resp.status_code == 422
    assert resp.json()["detail"] == "image_too_large"
    assert set(AVATARS_DIR.iterdir()) == avatars


def test_дорогим_на_пиксель_кадрам_достаётся_меньше_пикселей(monkeypatch):
    """Прозрачность раскрывается вдвое дороже RGB, WebP — вчетверо."""
    side = 60

    def encoded(mode: str, fmt: str) -> bytes:
        buf = io.BytesIO()
        Image.new(mode, (side, side)).save(buf, fmt)
        return buf.getvalue()

    rgb, rgba, webp = encoded("RGB", "PNG"), encoded("RGBA", "PNG"), encoded("RGB", "WEBP")

    monkeypatch.setattr(settings, "image_max_pixels", side * side * 2 - 1)
    open_upload(rgb, AVATAR_SIDE).close()
    with pytest.raises(TooManyPixels):
        open_upload(rgba, AVATAR_SIDE)

    monkeypatch.setattr(settings, "image_max_pixels", side * side * 2)
    open_upload(rgba, AVATAR_SIDE).close()
    with pytest.raises(TooManyPixels):
        open_upload(webp, AVATAR_SIDE)

    monkeypatch.setattr(settings, "image_max_pixels", side * side * 4)
    open_upload(webp, AVATAR_SIDE).close()


async def test_jpeg_меряется_таким_каким_распакуется(client, monkeypatch):
    """JPEG раскрывается сразу уменьшенным, и мерить его надо таким.

    Кадр 3000×3000 под фото анкеты распаковывается вдвое меньшим по
    стороне — 2,25 мегапикселя — и при пределе в три проходит, а PNG
    того же размера нет. Так проходит и снимок 50-мегапиксельной камеры
    телефона. CMYK уменьшается так же: раньше его переводили в RGB
    целиком, мимо draft, и платили за это полной копией кадра.
    """
    monkeypatch.setattr(settings, "image_max_pixels", 3_000_000)
    for mode in ("RGB", "CMYK"):
        buf = io.BytesIO()
        Image.new(mode, (3000, 3000)).save(buf, "JPEG")
        with open_upload(buf.getvalue(), AVATAR_SIDE) as im:
            assert (im.mode, im.size) == (mode, (1500, 1500))
            assert shrink(im, AVATAR_SIDE).size == (AVATAR_SIDE, AVATAR_SIDE)

    token = await _login()
    resp = await _upload(client, token, _image("JPEG", (3000, 3000)), "photo.jpg")
    assert resp.status_code == 200
    resp = await _upload(client, token, _image("PNG", (3000, 3000)), "photo.png")
    assert resp.status_code == 422


def test_предел_pillow_опущен_и_мимо_open_upload():
    """Снимки каталога из админки раскрываются без open_upload. Свой предел
    Pillow держит на 179 мегапикселях; опущенный до нашего, он отказывает
    уже выше восьмидесяти — не раскрыв ни байта."""
    with pytest.raises(Image.DecompressionBombError):
        store_upload(_png_header(9500, 9500), "test-bomb")


async def test_очередь_фото_не_держит_соединение_с_базой(client, monkeypatch):
    """Фото раскрываются по одному на воркер, и очереди можно ждать долго.
    Каждый, кто ждал её с соединением в руках, выбывал из пула в пятнадцать
    соединений, и лента с админкой отваливались по таймауту пула."""
    held = []
    store = me.store_avatar

    def spy(data, user_id):
        held.append(engine.pool.checkedout())
        return store(data, user_id)

    monkeypatch.setattr(me, "store_avatar", spy)
    token = await _login()
    resp = await _upload(client, token, _image("JPEG"), "photo.jpg")
    assert resp.status_code == 200
    assert held == [0], "соединение с базой занято, пока фото ждёт очереди"
