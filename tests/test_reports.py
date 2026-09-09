"""Форма обратной связи по местам: /report.

Главное, что здесь проверяется, — заявка не теряется ни в одном
из способов её оставить: место выбрано из списка, вписано словами
или подставлено ссылкой со страницы места.
"""

import io
import os

from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.api import report
from app.config import GPX_DIR, REPORTS_DIR
from app.db import SessionLocal
from app.models import PlaceReport
from app.services import attachments

WATERFALL = "Тестовый водопад — Тестовый регион"


async def _rows() -> list[PlaceReport]:
    async with SessionLocal() as session:
        stmt = select(PlaceReport).options(
            selectinload(PlaceReport.place), selectinload(PlaceReport.files)
        )
        return list((await session.execute(stmt)).scalars().all())


async def _cleanup() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(PlaceReport))
        await session.commit()
    # Счётчик частоты живёт в памяти процесса и переживает тест
    report._RECENT.clear()


async def test_report_lands_in_the_base(client):
    try:
        resp = await client.post(
            "/report",
            data={
                "place": WATERFALL,
                "topics": ["duration", "track"],
                "comment": "  Шли шесть часов вместо четырёх  ",
                "contact": "https://t.me/hiker",
                "lang": "ru",
                "website": "",
            },
        )
        assert resp.status_code == 200
        rows = await _rows()
        assert len(rows) == 1
        row = rows[0]
        assert row.place.slug == "test-waterfall"
        assert row.place_note is None, "узнанное место словами не дублируем"
        assert row.topics == ["duration", "track"]
        assert row.comment == "Шли шесть часов вместо четырёх"
        assert row.contact == "@hiker", "ссылка t.me не свелась к нику"
        assert row.status == "new"
    finally:
        await _cleanup()


async def test_place_typed_by_hand_is_kept(client):
    """Места нет в каталоге — заявка всё равно доходит."""
    try:
        resp = await client.post(
            "/report",
            data={
                "place": "Ущелье Сурхат",
                "topics": ["missing"],
                "comment": "",
                "contact": "",
                "lang": "ru",
                "website": "",
            },
        )
        assert resp.status_code == 200
        row = (await _rows())[0]
        assert row.place_id is None
        assert row.place_note == "Ущелье Сурхат"
        assert row.contact is None
    finally:
        await _cleanup()


async def test_slug_from_the_place_page_resolves(client):
    """Со страницы места приходят с ?place=slug — он и подставляется в поле."""
    try:
        await client.post(
            "/report",
            data={"place": "test-peak", "comment": "тропа заросла", "lang": "ru",
                  "website": ""},
        )
        row = (await _rows())[0]
        assert row.place.slug == "test-peak"
        assert row.place_note is None
    finally:
        await _cleanup()


async def test_empty_report_is_rejected(client):
    resp = await client.post(
        "/report", data={"place": WATERFALL, "comment": "  ", "lang": "ru",
                         "website": ""}
    )
    assert resp.status_code == 422
    assert await _rows() == []


async def test_unknown_topics_are_dropped(client):
    try:
        await client.post(
            "/report",
            data={"place": WATERFALL, "topics": ["duration", "выдумка"],
                  "comment": "", "lang": "ru", "website": ""},
        )
        assert (await _rows())[0].topics == ["duration"]
    finally:
        await _cleanup()


async def test_honeypot_swallows_bots_quietly(client):
    try:
        resp = await client.post(
            "/report",
            data={"place": WATERFALL, "comment": "buy cheap", "lang": "ru",
                  "website": "http://spam"},
        )
        assert resp.status_code == 200, "бот не должен узнать, что его раскусили"
        assert await _rows() == []
    finally:
        await _cleanup()


async def test_answer_matches_the_caller(client):
    """Скрипту — JSON, обычной отправке формы — человеческая страница."""
    try:
        as_json = await client.post(
            "/report",
            data={"place": WATERFALL, "comment": "раз", "lang": "ru", "website": ""},
            headers={"Accept": "application/json"},
        )
        assert as_json.json() == {"ok": True}

        as_form = await client.post(
            "/report",
            data={"place": WATERFALL, "comment": "икки", "lang": "uz", "website": ""},
        )
        assert '<html lang="uz">' in as_form.text
        assert "rahmat" in as_form.text
        assert (await _rows())[0].lang in ("ru", "uz")
    finally:
        await _cleanup()


async def test_form_prefills_place_from_the_link(client):
    ru = await client.get("/report", params={"place": "test-lake"})
    assert 'value="Тестовое озеро — Тестовый регион"' in ru.text
    assert 'name="topics" value="duration"' in ru.text

    uz = await client.get("/uz/report", params={"place": "test-lake"})
    assert 'value="Test koʻli — Test viloyati"' in uz.text
    assert "Boshqa" in uz.text, "темы не перевелись"


async def test_place_page_invites_to_the_form(client):
    page = await client.get("/p/test-waterfall")
    assert "/report?place=test-waterfall" in page.text

    uz = await client.get("/p/test-waterfall", params={"lang": "uz"})
    assert "/uz/report?place=test-waterfall" in uz.text


# --- Приложенные файлы ---------------------------------------------------


def _png(side: int = 24) -> bytes:
    """Настоящий PNG: проверка смотрит на байты, а не на имя файла."""
    buf = io.BytesIO()
    Image.new("RGB", (side, side), (47, 93, 63)).save(buf, "PNG")
    return buf.getvalue()


def _noise_png(side: int = 32) -> bytes:
    """PNG, который не сжимается: одноцветный весит сотню байт, а тесту
    на предел веса нужны предсказуемые килобайты."""
    buf = io.BytesIO()
    Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(buf, "PNG")
    return buf.getvalue()


GPX_BYTES = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<gpx version="1.1" creator="test"><trk><trkseg>'
    b'<trkpt lat="41.5" lon="70.0"/></trkseg></trk></gpx>'
)


async def _files_on_disk() -> list[str]:
    return sorted(p.name for p in REPORTS_DIR.iterdir() if p.is_file())


async def _cleanup_files() -> None:
    for path in REPORTS_DIR.iterdir():
        if path.is_file():
            path.unlink()


async def test_attachment_lands_next_to_the_report(client):
    """Кадр развилки доезжает до заявки и ложится на диск с превью."""
    try:
        resp = await client.post(
            "/report",
            data={"place": WATERFALL, "comment": "тропа уходит правее",
                  "lang": "ru", "website": ""},
            files=[
                ("files", ("развилка.png", _png(), "image/png")),
                ("files", ("трек.gpx", GPX_BYTES, "application/octet-stream")),
            ],
        )
        assert resp.status_code == 200
        row = (await _rows())[0]
        assert len(row.files) == 2

        shot, track = row.files
        assert shot.original_name == "развилка.png"
        assert shot.content_type == "image/png", "тип берём из байтов"
        assert shot.size == len(_png())
        # Тип трека клиент назвал octet-stream — нам важно, что внутри
        assert track.content_type == "application/gpx+xml"

        on_disk = await _files_on_disk()
        assert shot.name in on_disk and track.name in on_disk
        assert attachments.thumb_name(shot.name) in on_disk, "превью не сделалось"
        assert attachments.thumb_name(track.name) not in on_disk, "превью у GPX лишнее"
    finally:
        await _cleanup()
        await _cleanup_files()


async def test_one_photo_is_a_whole_report(client):
    """Снимок без единого слова — тоже заявка: он объясняет больше абзаца."""
    try:
        resp = await client.post(
            "/report",
            data={"place": WATERFALL, "comment": "", "lang": "ru", "website": ""},
            files=[("files", ("вид.png", _png(), "image/png"))],
        )
        assert resp.status_code == 200
        assert len((await _rows())[0].files) == 1
    finally:
        await _cleanup()
        await _cleanup_files()


async def test_alien_file_is_refused_with_its_name(client):
    """Чужой тип не принимаем — и говорим, какой именно файл виноват."""
    resp = await client.post(
        "/report",
        data={"place": WATERFALL, "comment": "смотрите вложение", "lang": "ru",
              "website": ""},
        files=[("files", ("вирус.exe", b"MZ\x90\x00" + b"\x00" * 64,
                          "application/octet-stream"))],
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 422
    assert "вирус.exe" in resp.json()["detail"]
    assert await _rows() == [], "заявка не должна сохраниться наполовину"
    assert await _files_on_disk() == []


async def test_heavy_and_numerous_are_turned_away(client):
    many = [("files", (f"{n}.png", _png(), "image/png")) for n in range(5)]
    resp = await client.post(
        "/report",
        data={"place": WATERFALL, "comment": "пять кадров", "lang": "ru",
              "website": ""},
        files=many,
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == report.RU["too_many"]

    heavy = b"\xff\xd8\xff" + b"\x00" * attachments.MAX_BYTES
    resp = await client.post(
        "/report",
        data={"place": WATERFALL, "comment": "тяжёлый кадр", "lang": "uz",
              "website": ""},
        files=[("files", ("katta.jpg", heavy, "image/jpeg"))],
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 422
    assert "katta.jpg" in resp.json()["detail"], "узбекской странице — узбекский отказ"
    assert await _rows() == []
    assert await _files_on_disk() == []


async def test_all_together_too_heavy(client, monkeypatch):
    """Порознь файлы проходят, вместе — нет.

    Пределы подменяем, а не шлём настоящие шестнадцать мегабайт: проверяем
    здесь счёт по ходу чтения, а не способность httpx перекачать файл.
    """
    shots = [_noise_png(), _noise_png()]
    assert all(len(shot) < 4096 for shot in shots), "порознь должны проходить"
    monkeypatch.setattr(attachments, "MAX_BYTES", 4096)
    monkeypatch.setattr(attachments, "MAX_TOTAL", len(shots[0]) + 1)
    try:
        resp = await client.post(
            "/report",
            data={"place": WATERFALL, "comment": "два кадра", "lang": "ru",
                  "website": ""},
            files=[("files", (f"{n}.png", shot, "image/png"))
                   for n, shot in enumerate(shots)],
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == report.RU["too_heavy"]
        assert await _rows() == []
    finally:
        await _cleanup()
        await _cleanup_files()


async def test_refusal_takes_the_written_files_with_it(client):
    """Первый файл принят, второй нет — на диске не должно остаться ничего."""
    resp = await client.post(
        "/report",
        data={"place": WATERFALL, "comment": "кадр и мусор", "lang": "ru",
              "website": ""},
        files=[
            ("files", ("вид.png", _png(), "image/png")),
            ("files", ("заметки.txt", "просто текст".encode(), "text/plain")),
        ],
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 422
    assert await _rows() == []
    assert await _files_on_disk() == [], "принятый файл пережил отказ"


async def test_the_same_photo_in_two_reports_lies_once(client):
    """Имя файла — хеш содержимого, и повтор не плодит копий."""
    try:
        for text in ("первая", "вторая"):
            await client.post(
                "/report",
                data={"place": WATERFALL, "comment": text, "lang": "ru",
                      "website": ""},
                files=[("files", ("вид.png", _png(), "image/png"))],
            )
        names = {row.files[0].name for row in await _rows()}
        assert len(names) == 1
        assert len(await _files_on_disk()) == 2, "сам файл и его превью"
    finally:
        await _cleanup()
        await _cleanup_files()


async def test_bot_leaves_nothing_on_disk(client):
    """Пойманный приманкой получает то же «спасибо», но файл не сохраняется."""
    try:
        resp = await client.post(
            "/report",
            data={"place": WATERFALL, "comment": "buy cheap", "lang": "ru",
                  "website": "http://spam"},
            files=[("files", ("spam.png", _png(), "image/png"))],
        )
        assert resp.status_code == 200
        assert await _rows() == []
        assert await _files_on_disk() == []
    finally:
        await _cleanup()


async def test_form_offers_to_attach(client):
    ru = await client.get("/report")
    assert 'name="files"' in ru.text
    assert 'enctype="multipart/form-data"' in ru.text
    assert "Выбрать файлы" in ru.text

    uz = await client.get("/uz/report")
    assert "Fayl tanlash" in uz.text


async def test_attachments_are_not_public(client):
    """Каталог с чужими файлами не отдаётся по /media."""
    try:
        await client.post(
            "/report",
            data={"place": WATERFALL, "comment": "кадр", "lang": "ru", "website": ""},
            files=[("files", ("вид.png", _png(), "image/png"))],
        )
        name = (await _rows())[0].files[0].name
        assert (await client.get(f"/media/reports/{name}")).status_code == 404
        # А соседние каталоги по-прежнему отдаются: /media разобран
        # на три монтирования, и промахнуться легко в обе стороны
        (GPX_DIR / "check.gpx").write_bytes(GPX_BYTES)
        try:
            assert (await client.get("/media/gpx/check.gpx")).status_code == 200
        finally:
            (GPX_DIR / "check.gpx").unlink()
    finally:
        await _cleanup()
        await _cleanup_files()


# --- Отметка «проверено» -------------------------------------------------


async def test_report_arrives_unverified(client):
    try:
        await client.post(
            "/report",
            data={"place": WATERFALL, "comment": "раз", "lang": "ru", "website": ""},
        )
        assert (await _rows())[0].verified is False
    finally:
        await _cleanup()


async def test_owner_marks_and_unmarks_from_the_list(client, admin_client):
    """Кнопка в списке переключает флаг и возвращает туда, откуда нажали."""
    try:
        await client.post(
            "/report",
            data={"place": WATERFALL, "comment": "шли шесть часов", "lang": "ru",
                  "website": ""},
        )
        report_id = (await _rows())[0].id

        back = "/admin/place-report/list?status=new&page=2"
        resp = await admin_client.post(
            "/admin/report-verify",
            data={"id": str(report_id)},
            headers={"referer": f"https://test{back}"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == back, "вернулись не на ту страницу очереди"
        assert (await _rows())[0].verified is True

        await admin_client.post("/admin/report-verify", data={"id": str(report_id)},
                                follow_redirects=False)
        assert (await _rows())[0].verified is False, "второе нажатие снимает отметку"
    finally:
        await _cleanup()


async def test_verified_filter_is_the_work_list(client, admin_client):
    try:
        for text in ("правда", "выдумка"):
            await client.post(
                "/report",
                data={"place": WATERFALL, "comment": text, "lang": "ru",
                      "website": ""},
            )
        true_one = next(r for r in await _rows() if r.comment == "правда")
        await admin_client.post("/admin/report-verify", data={"id": str(true_one.id)},
                                follow_redirects=False)

        picked = await admin_client.get("/admin/place-report/list?verified=1")
        assert "правда" in picked.text
        assert "выдумка" not in picked.text

        rest = await admin_client.get("/admin/place-report/list?verified=0")
        assert "выдумка" in rest.text
        assert "правда" not in rest.text
    finally:
        await _cleanup()


async def test_files_open_only_to_the_owner(client, admin_client):
    try:
        await client.post(
            "/report",
            data={"place": WATERFALL, "comment": "кадр", "lang": "ru", "website": ""},
            files=[("files", ("вид.png", _png(), "image/png"))],
        )
        file_id = (await _rows())[0].files[0].id

        stranger = await client.get(f"/admin/report-file/{file_id}",
                                    follow_redirects=False)
        assert stranger.status_code in (302, 303), "чужому — на страницу входа"

        mine = await admin_client.get(f"/admin/report-file/{file_id}")
        assert mine.status_code == 200
        assert mine.headers["content-type"].startswith("image/png")
        assert mine.headers["x-content-type-options"] == "nosniff"
        assert mine.content == _png()

        thumb = await admin_client.get(f"/admin/report-file/{file_id}/thumb")
        assert thumb.status_code == 200
        assert thumb.headers["content-type"].startswith("image/jpeg")
    finally:
        await _cleanup()
        await _cleanup_files()


async def test_deleting_a_report_takes_its_files_along(client, admin_client):
    """Обещание с /privacy: разобрали — удалили, вместе с присланным."""
    try:
        await client.post(
            "/report",
            data={"place": WATERFALL, "comment": "кадр", "lang": "ru", "website": ""},
            files=[("files", ("вид.png", _png(), "image/png"))],
        )
        row = (await _rows())[0]
        assert await _files_on_disk(), "файл не лёг на диск"

        gone = await admin_client.request(
            "DELETE", f"/admin/place-report/delete?pks={row.id}"
        )
        assert gone.status_code in (200, 204, 302, 303)
        assert await _rows() == []
        assert await _files_on_disk() == [], "файл пережил заявку"
    finally:
        await _cleanup()
        await _cleanup_files()


async def test_shared_file_survives_a_neighbours_deletion(client, admin_client):
    """Один и тот же кадр в двух заявках лежит на диске один раз.

    Имя файла — хеш содержимого, поэтому уборка за первой заявкой не
    должна уносить снимок, на который смотрит вторая.
    """
    try:
        for text in ("первая", "вторая"):
            await client.post(
                "/report",
                data={"place": WATERFALL, "comment": text, "lang": "ru",
                      "website": ""},
                files=[("files", ("вид.png", _png(), "image/png"))],
            )
        rows = sorted(await _rows(), key=lambda r: r.id)
        assert rows[0].files[0].name == rows[1].files[0].name

        await admin_client.request(
            "DELETE", f"/admin/place-report/delete?pks={rows[0].id}"
        )
        assert rows[1].files[0].name in await _files_on_disk()

        await admin_client.request(
            "DELETE", f"/admin/place-report/delete?pks={rows[1].id}"
        )
        assert await _files_on_disk() == []
    finally:
        await _cleanup()
        await _cleanup_files()
