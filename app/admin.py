"""Админка для кураторского наполнения каталога: /admin."""

import logging
import re
import secrets
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from sqladmin import Admin, BaseView, ModelView, expose
from sqladmin.authentication import AuthenticationBackend
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, RedirectResponse,
                                 Response)
from markupsafe import Markup
from wtforms import SelectField, SelectMultipleField
from wtforms.validators import NumberRange
from wtforms.validators import Optional as Blank

try:  # расположение менялось между версиями пакета
    from fastapi_storages import StorageFile
except ImportError:  # pragma: no cover
    from fastapi_storages.base import StorageFile

from . import stats
from .config import GPX_DIR, REPORTS_DIR, SERVER_DIR, settings
from .db import SessionLocal, engine
from .models import (
    Announcement,
    AnnouncementStatus,
    AppUpdate,
    Place,
    PlacePhoto,
    PlaceReport,
    PlaceReportFile,
    PlaceTrack,
    PushToken,
    Region,
    ReportStatus,
    Season,
    TesterSignup,
    photo_storage,
)
from .reports import STATUS_RU, telegram_url, topic_names
from .seasons import LIMITS
from .services import attachments
from .services.gpx import recorded_from_target, reverse_track, track_stats
from .services.images import make_thumbnail, retire_photo, store_upload
from .services.nearby import rebuild_for_track


log = logging.getLogger("sayr.admin")

#: Куда смонтирована админка. sqladmin по умолчанию берёт /admin, и своего
#: base_url мы не задаём (см. mount_admin) — но адрес нужен в разметке,
#: которую собираем руками, а до объекта Admin оттуда не дотянуться
_ADMIN_BASE = "/admin"


# Подписи двуязычных полей. Без них sqladmin выводит имя колонки, и
# «Name Uz» под «Name» читается как опечатка, а не как пара языков.
# Одна карта работает и в списке, и в форме — sqladmin передаёт
# column_labels в scaffold_form
_PLACE_LABELS = {
    Place.name: "Название · RU",
    Place.name_uz: "Название · UZ",
    Place.short_desc: "Короткое описание · RU",
    Place.short_desc_uz: "Короткое описание · UZ",
    Place.description_md: "Описание · RU",
    Place.description_md_uz: "Описание · UZ",
    Place.how_to_get_md: "Как добраться · RU",
    Place.how_to_get_md_uz: "Как добраться · UZ",
    # Пара про многодневку. Без подписей sqladmin показал бы имена
    # колонок, а «Trip Days» рядом с «Overnight» ни о чём не говорит:
    # заполняются они только вместе, и это должно быть видно
    Place.overnight: "Ночёвка",
    Place.trip_days: "Дней на выход",
    # Приходят из игры на сайте через проверку, но правятся и здесь
    Place.winter_load: "Тропёжка зимой",
    Place.danger: "Опасность",
    Place.limits: "Ограничения",
}

# Что считаем переведённым. Пустая строка — это «не переведено»
# наравне с NULL: так же считает и фолбэк в schemas.pick
_TRANSLATED_FIELDS = [
    ("имя", "name_uz"),
    ("кратко", "short_desc_uz"),
    ("текст", "description_md_uz"),
    ("путь", "how_to_get_md_uz"),
]


def _translation_progress(model, attribute, request=None) -> str:
    """Готовность перевода одной строкой: что уже есть, чего нет.

    Показывается в списке мест вместо голого name_uz. Пустой русский
    оригинал в знаменатель не идёт: «как добраться» заполнено не везде,
    и требовать перевод отсутствующего текста незачем — такое поле
    помечено как неприменимое, а не как долг.
    """
    marks = []
    for label, field in _TRANSLATED_FIELDS:
        source = getattr(model, field.removesuffix("_uz"), "") or ""
        if not source:
            continue
        marks.append(f"{label} {'✓' if (getattr(model, field) or '') else '—'}")
    return " · ".join(marks) if marks else "—"


class BasicAuthBackend(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        ok_user = secrets.compare_digest(str(form.get("username", "")), settings.admin_username)
        ok_pass = secrets.compare_digest(str(form.get("password", "")), settings.admin_password)
        if ok_user and ok_pass:
            request.session.update({"admin": True})
            return True
        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        return bool(request.session.get("admin"))


class RegionAdmin(ModelView, model=Region):
    name = "Регион"
    name_plural = "Регионы"
    column_list = [Region.id, Region.name, Region.name_uz, Region.sort_order]
    form_columns = [Region.name, Region.name_uz, Region.sort_order]
    column_labels = {Region.name: "Название · RU", Region.name_uz: "Название · UZ"}


class PlaceAdmin(ModelView, model=Place):
    name = "Место"
    name_plural = "Места"
    column_list = [
        Place.id,
        Place.name,
        Place.name_uz,
        Place.category,
        Place.difficulty,
        Place.is_published,
    ]
    # Вместо голого name_uz в списке — готовность перевода целиком:
    # по одному месту в форме этого не видно, а наливается перевод
    # порциями, и надо понимать, что осталось
    column_formatters = {Place.name_uz: _translation_progress}
    column_searchable_list = [Place.name, Place.name_uz, Place.slug]
    column_labels = {**_PLACE_LABELS, Place.name_uz: "Перевод"}
    column_default_sort = ("name", False)
    form_excluded_columns = [Place.photos, Place.created_at, Place.updated_at]
    # Порядок полей формы sqladmin берёт из порядка колонок в модели,
    # а там каждое *_uz стоит сразу за своим оригиналом — пара языков
    # оказывается рядом сама, без form_columns со списком всех полей
    form_widget_args = {
        "short_desc": {"rows": 3},
        "short_desc_uz": {"rows": 3},
        "description_md": {"rows": 14},
        "description_md_uz": {"rows": 14},
        "how_to_get_md": {"rows": 8},
        "how_to_get_md_uz": {"rows": 8},
    }
    form_overrides = {"best_seasons": SelectMultipleField, "limits": SelectMultipleField}
    form_args = {
        "limits": {
            "choices": [(code, ru) for code, ru, _ in LIMITS],
            "description": "Что мешает попасть, кроме погоды",
        },
        # Optional первым: пустое поле — «ещё не знаем», и на нём цепочка
        # проверок останавливается, не доходя до «от 1 до 10»
        "winter_load": {
            "validators": [Blank(), NumberRange(min=1, max=10)],
            "description": "1 — натоптано, 10 — тропить по пояс",
        },
        "danger": {
            "validators": [Blank(), NumberRange(min=1, max=10)],
            "description": "Отдельно от сложности: 1 — спокойно, 10 — камнепады, лавины",
        },
        "best_seasons": {
            "choices": [(s.value, s.value) for s in Season],
            "coerce": str,
        },
        # В списке колонка name_uz показывает сводку по всем переводам,
        # и подпись там «Перевод». В форме это обычное поле — подпись
        # из form_args перекрывает column_labels (setdefault в конвертере)
        "name_uz": {"label": "Название · UZ"},
    }
    # Своя страница места: под таблицей полей — плитка снимков.
    # Без неё, чтобы увидеть фотографии места, надо уйти в «Фото мест»
    # и отфильтровать список по имени, а чтобы убрать неудачный кадр —
    # опознать его там по номеру строки
    details_template = "place_details.html"
    # В таблице полей photos рисовались как строка путей к файлам во всю
    # ширину экрана. Плитка ниже показывает то же самое и по-человечески
    column_details_exclude_list = [Place.photos]

    def details_query(self, request: Request) -> Select:
        # photos убраны из таблицы полей, а вместе с этим пропали
        # и из автоматической предзагрузки sqladmin — плитке они нужны,
        # иначе шаблон полезет за ними лениво и упадёт на greenlet
        return super().details_query(request).options(selectinload(Place.photos))

    @expose("/photo-add", methods=["POST"])
    async def add_photos(self, request: Request) -> Response:
        """Заливает снимки прямо со страницы места.

        Берём пачкой: дырки в каталоге закрывают не по одному кадру, а сразу
        подборкой из поездки. Подпись автора одна на пачку — снимки обычно
        из одного источника, а поправить отдельный можно в его карточке.

        Битый или слишком большой файл пропускаем молча и грузим остальные:
        уронить всю пачку из-за одного кадра — худшее, что тут можно сделать.
        """
        form = await request.form()
        try:
            place_id = int(str(form.get("place_id", "")))
        except ValueError:
            return RedirectResponse(
                request.url_for("admin:list", identity=self.identity), status_code=303
            )
        credit = str(form.get("credit", "")).strip()[:300]
        uploads = [f for f in form.getlist("photos") if getattr(f, "filename", "")]

        async with AsyncSession(engine) as session:
            place = await session.get(Place, place_id)
            if place is None:
                return RedirectResponse(
                    request.url_for("admin:list", identity=self.identity), status_code=303
                )
            existing = (
                (
                    await session.execute(
                        select(PlacePhoto).where(PlacePhoto.place_id == place_id)
                    )
                )
                .scalars()
                .all()
            )
            have = {Path(str(p.file)).name for p in existing if p.file}
            order = max((p.sort_order for p in existing), default=-1) + 1

            for upload in uploads:
                try:
                    name = store_upload(await upload.read(), place.slug)
                except Exception:  # noqa: BLE001 — не картинка или не влезла
                    continue
                # Имя считается из содержимого: тот же кадр второй раз
                # не заводит вторую строку, только перезаписывает файл собой
                if name in have:
                    continue
                have.add(name)
                session.add(
                    PlacePhoto(
                        place_id=place_id,
                        file=StorageFile(name=name, storage=photo_storage),
                        credit=credit,
                        sort_order=order,
                    )
                )
                order += 1
            await session.commit()

        return RedirectResponse(
            request.url_for("admin:details", identity=self.identity, pk=place_id),
            status_code=303,
        )

    @expose("/photo-cover", methods=["POST"])
    async def make_cover(self, request: Request) -> Response:
        """Двигает снимок на первое место.

        Обложка — это photos[0] по sort_order (schemas._base_fields), так что
        «сделать обложкой» и «поставить первым» — одно и то же действие.
        Заодно перенумеровываем остальные подряд: иначе после нескольких
        перестановок номера расползаются и одинаковый sort_order у двух
        снимков делает обложку делом случая.
        """
        photo_id, place_id = self._photo_form(await request.form())
        if photo_id is None:
            return RedirectResponse(
                request.url_for("admin:list", identity=self.identity), status_code=303
            )

        async with AsyncSession(engine) as session:
            photos = list(
                (
                    await session.execute(
                        select(PlacePhoto)
                        .where(PlacePhoto.place_id == place_id)
                        .order_by(PlacePhoto.sort_order, PlacePhoto.id)
                    )
                )
                .scalars()
                .all()
            )
            chosen = next((p for p in photos if p.id == photo_id), None)
            if chosen is not None:
                rest = [p for p in photos if p.id != photo_id]
                for i, photo in enumerate([chosen, *rest]):
                    photo.sort_order = i
                await session.commit()

        return RedirectResponse(
            request.url_for("admin:details", identity=self.identity, pk=place_id),
            status_code=303,
        )

    @staticmethod
    def _photo_form(form) -> tuple[int | None, int | None]:
        """id снимка и места из формы. Мусор — не повод отвечать пятисоткой."""
        try:
            return int(str(form.get("photo_id", ""))), int(str(form.get("place_id", "")))
        except ValueError:
            return None, None

    @expose("/photo-delete", methods=["POST"])
    async def delete_photo(self, request: Request) -> Response:
        """Убирает снимок со страницы места.

        Запись из базы уходит совсем, файлы — в корзину (см. retire_photo):
        отбор фотографий человек делает на глаз и вправе промахнуться.
        """
        photo_id, place_id = self._photo_form(await request.form())
        if photo_id is None:
            return RedirectResponse(
                request.url_for("admin:list", identity=self.identity), status_code=303
            )

        async with AsyncSession(engine) as session:
            photo = await session.get(PlacePhoto, photo_id)
            # Чужой place_id в форме не должен удалять снимок другого места
            if photo is not None and photo.place_id == place_id:
                # str(file), а НЕ file.name: свойство .name прогоняет имя через
                # санитайзер хранилища, который вырезает кириллицу — «фото.jpg»
                # превращалось в «.jpg», и с диска не убиралось ничего
                name = Path(str(photo.file)).name if photo.file else None
                await session.delete(photo)
                await session.commit()
                if name:
                    # Пишем в журнал, что именно уехало в корзину: 20 августа
                    # 167 снимков удалились, а корзина осталась пуста — молчащий
                    # except не дал понять, почему. Запись уже в любом случае
                    # удалена, файл подождёт уборки, но знать об этом надо
                    try:
                        moved = retire_photo(name)
                    except OSError as exc:
                        log.warning("снимок %s: не убрался файл %s (%s)", photo_id, name, exc)
                    else:
                        if moved:
                            log.info("снимок %s: в корзину %s", photo_id, [m.name for m in moved])
                        else:
                            log.warning("снимок %s: файла %s на диске нет", photo_id, name)

        return RedirectResponse(
            request.url_for("admin:details", identity=self.identity, pk=place_id),
            status_code=303,
        )


class PlacePhotoAdmin(ModelView, model=PlacePhoto):
    name = "Фото"
    name_plural = "Фото мест"
    column_list = [PlacePhoto.id, PlacePhoto.place, PlacePhoto.sort_order, PlacePhoto.credit]

    async def after_model_change(self, data, model: PlacePhoto, is_created: bool, request) -> None:
        # Битая картинка не должна ронять сохранение: запись уже в базе,
        # а 500 после успешного коммита читается как «ничего не сохранилось»
        if model.file:
            try:
                make_thumbnail(Path(model.file.name).name)
            except Exception:
                pass


class PlaceTrackAdmin(ModelView, model=PlaceTrack):
    name = "Трек"
    name_plural = "Треки мест"
    column_list = [
        PlaceTrack.id,
        PlaceTrack.place,
        PlaceTrack.name,
        PlaceTrack.name_uz,
        PlaceTrack.distance_km,
        PlaceTrack.ascent_m,
        PlaceTrack.sort_order,
    ]
    # Статистика считается из файла, руками её не вводят
    form_excluded_columns = [
        PlaceTrack.distance_km,
        PlaceTrack.ascent_m,
        PlaceTrack.start_lat,
        PlaceTrack.start_lng,
    ]

    async def after_model_change(self, data, model: PlaceTrack, is_created: bool, request) -> None:
        # Длина и набор — из загруженного файла. Имя файла берём ИЗ БАЗЫ,
        # а не из model: sqladmin передаёт сюда объект формы, где gpx_file —
        # это UploadFile без .name, и обращение к нему молча падало в except,
        # оставляя у трека «0 км · +0 м». Плюс при коллизии имён хранилище
        # дописывает _1, и правду знает только колонка.
        # Битый GPX не должен ронять сохранение: запись уже в базе, 500 после
        # коммита читается как «ничего не сохранилось»
        from sqlalchemy import update

        async with AsyncSession(engine) as session:
            row = (
                await session.execute(
                    select(PlaceTrack.gpx_file, Place.lat, Place.lng)
                    .join(Place, Place.id == PlaceTrack.place_id)
                    .where(PlaceTrack.id == model.id)
                )
            ).first()
            if row is None or not row[0]:
                return
            stored, place_lat, place_lng = row
            path = GPX_DIR / Path(str(stored)).name
            try:
                data = path.read_bytes()
                # Запись, сделанная НА СПУСКЕ, начинается у самой цели. Такую
                # разворачиваем сразу и переписываем файл: иначе в автонавигатор
                # уедет вершина вместо парковки, а набор посчитается в сторону
                # спуска — у Большого Чимгана так вышло 22 метра вместо 1566.
                # Правим файл, а не только колонки: клиенты считают набор сами
                # по скачанному GPX и ставят флаг «Старт» на его первую точку
                if recorded_from_target(data, place_lat, place_lng):
                    data = reverse_track(data)
                    path.write_bytes(data)
                stats = track_stats(data)
            except Exception:
                return
            await session.execute(
                update(PlaceTrack)
                .where(PlaceTrack.id == model.id)
                .values(
                    distance_km=stats.distance_km,
                    ascent_m=stats.ascent_m,
                    start_lat=stats.start_lat,
                    start_lng=stats.start_lng,
                )
            )
            await session.commit()

        # Связи «рядом» держатся на геометрии: заменили файл — прежние соседи
        # уже не про этот маршрут. Отдельной сессией, чтобы сбой пересчёта
        # не утянул за собой уже посчитанную статистику
        async with AsyncSession(engine) as session:
            track = (
                await session.execute(select(PlaceTrack).where(PlaceTrack.id == model.id))
            ).scalar_one_or_none()
            if track is None:
                return
            try:
                await rebuild_for_track(session, track)
                await session.commit()
            except Exception:
                await session.rollback()


class StatsView(BaseView):
    """Страница «Статистика»: кто пользуется, что смотрят, что делают.

    Собственный HTML, а не шаблон sqladmin: страница одна, таблиц пять,
    и заводить ради них каталог шаблонов с наследованием от темы —
    больше возни, чем пользы. Сессию берём сами: add_base_view,
    в отличие от модельных вьюх, session_maker внутрь не отдаёт.
    """

    name = "Статистика"
    icon = "fa-solid fa-chart-simple"

    @expose("/stats", methods=["GET"])
    async def page(self, request: Request) -> HTMLResponse:
        async with SessionLocal() as session:
            data = await stats.dashboard(session)
        return HTMLResponse(_render(data))


def _cells(values, tag: str = "td") -> str:
    return "".join(f"<{tag}>{v}</{tag}>" for v in values)


def _table(headers: list[str], rows: list[list], empty: str) -> str:
    if not rows:
        return f'<p class="text-muted">{empty}</p>'
    body = "".join(f"<tr>{_cells(row)}</tr>" for row in rows)
    return (
        '<div class="table-responsive"><table class="table table-sm">'
        f"<thead><tr>{_cells(headers, 'th')}</tr></thead><tbody>{body}</tbody>"
        "</table></div>"
    )


def _render(d: dict) -> str:
    numbers = [
        ("Сегодня", d["active_today"]),
        ("Вчера", d["active_yesterday"]),
        ("За 7 дней", d["wau"]),
        ("За 30 дней", d["mau"]),
        ("Новых за неделю", d["new_week"]),
        ("Всего устройств", d["total_devices"]),
    ]
    tiles = "".join(
        f'<div class="col"><div class="card"><div class="card-body">'
        f'<div class="h1 m-0">{value}</div>'
        f'<div class="text-muted">{label}</div></div></div></div>'
        for label, value in numbers
    )

    def pace_cell(counts: tuple[int, int, int] | None) -> str:
        """Как прошли: быстрее · так · дольше.

        Прочерк вместо трёх нулей — чтобы места, про которые ещё никто
        не ответил, не выглядели как места, где всё сошлось. Разница
        существенная: во втором случае формулу трогать не надо,
        в первом — про неё просто ничего не известно.
        """
        if not counts or not any(counts):
            return "—"
        faster, expected, slower = counts
        return f"{faster} · {expected} · {slower}"

    def top(rows: list[dict]) -> str:
        return _table(
            ["Место", "Открытий", "Устройств", "«Пойду»", "Как прошли", "Загрузок GPX"],
            [
                [
                    r["name"],
                    r["opens"],
                    r["devices"],
                    r["votes"],
                    pace_cell(r.get("pace")),
                    r["downloads"],
                ]
                for r in rows
            ],
            "Пока никто ничего не открывал.",
        )

    days = _table(
        ["День", "Активных", "Новых", "Мест", "Каталог", "Треков"],
        [
            [
                r["day"].strftime("%d.%m"),
                r["active_devices"],
                r["new_devices"],
                r["place_opens"],
                r["catalog_opens"],
                r["gpx_downloads"],
            ]
            for r in d["days"]
        ],
        "Событий ещё не было.",
    )
    upcoming = _table(
        ["День", "Место", "Человек"],
        [[r["day"].strftime("%d.%m"), r["name"], r["people"]] for r in d["upcoming"]],
        "На ближайшие дни никто не собрался.",
    )
    shares = _table(
        ["Место", "Открытий"],
        [[r["name"], r["opens"]] for r in d["shares"]],
        "Ссылками пока не делились.",
    )

    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Статистика — Sayr</title>
<link rel="stylesheet" href="/admin/statics/css/tabler.min.css">
</head><body class="antialiased">
<div class="page-wrapper"><div class="container-xl py-4">
  <div class="d-flex justify-content-between align-items-center mb-3">
    <h1 class="m-0">Статистика</h1>
    <a href="/admin" class="btn">В админку</a>
  </div>
  <p class="text-muted">Обезличенные счётчики. Сырьё живёт
     {settings.stats_retention_days} дней, дневные итоги — всегда.<br>
     «Загрузок GPX» — это обращения за файлом трека, а не осознанные
     скачивания: приложение подтягивает трек само при открытии места,
     так что число близко к открытиям мест, у которых трек есть.
     Повторные открытия отдаёт кэш и сюда не попадают.</p>

  <h2 class="h3 mt-4">Активные устройства</h2>
  <div class="row row-cards row-cols-2 row-cols-md-3 row-cols-xl-6 g-2">{tiles}</div>

  <h2 class="h3 mt-4">Две недели</h2>
  {days}

  <h2 class="h3 mt-4">Топ мест за 7 дней</h2>
  {top(d["top_week"])}

  <h2 class="h3 mt-4">Топ мест за 30 дней</h2>
  {top(d["top_month"])}

  <h2 class="h3 mt-4">Кто куда собирается</h2>
  {upcoming}

  <h2 class="h3 mt-4">Открытия ссылок «поделиться» за 30 дней</h2>
  {shares}
</div></div></body></html>"""


class TesterSignupAdmin(ModelView, model=TesterSignup):
    """Очередь заявок на закрытый тест Android с лендинга.

    Рабочий цикл: открыть, отсортировать по галочке, добавить непозванных
    в список тестировщиков Play Console, отправить ссылку письмом
    и поставить `invited`. Без галочки при десятке заявок уже не вспомнить,
    кому ссылка ушла.
    """

    name = "Заявка на тест"
    name_plural = "Тестировщики Android"
    icon = "fa-solid fa-envelope"
    column_list = [
        TesterSignup.email,
        TesterSignup.lang,
        TesterSignup.invited,
        TesterSignup.created_at,
    ]
    column_default_sort = ("created_at", True)
    column_sortable_list = [TesterSignup.invited, TesterSignup.created_at]
    column_searchable_list = [TesterSignup.email]
    column_labels = {
        TesterSignup.email: "Почта",
        TesterSignup.lang: "Язык",
        TesterSignup.invited: "Приглашён",
        TesterSignup.created_at: "Оставлена",
    }
    form_columns = [TesterSignup.invited]
    can_create = False
    # Удалять можно: спам-адреса чистятся отсюда же



def _report_where(model, attribute, request=None) -> str:
    """Место заявки: из каталога, вписанное руками или ничего."""
    if model.place:
        return model.place.name
    return model.place_note or "—"


def _report_excerpt(model, attribute, request=None):
    """Начало текста: в списке нужен повод открыть, а не весь рассказ.

    Скрепка с числом идёт впереди текста, а не отдельной колонкой: в узкой
    таблице лишний столбец отбирает ширину у самого текста, а знать про
    приложенный кадр надо ровно тогда, когда решаешь, открывать ли заявку.
    """
    text = (model.comment or "").replace("\n", " ").strip()
    cut = text if len(text) <= 140 else text[:139] + "…"
    if not model.files:
        return cut
    return Markup(
        f'<span class="text-muted me-2" title="Приложено файлов: {len(model.files)}">'
        f'<i class="fa-solid fa-paperclip"></i> {len(model.files)}</span>{escape(cut)}'
    )


def _report_verified(model, attribute, request=None):
    """Отметка «проверено» — кнопкой прямо в списке.

    Разбор очереди — это десятки заявок подряд, и открывать ради галочки
    форму правки каждой значило бы четыре перехода вместо одного нажатия.
    Кнопка шлёт POST: галочка меняет базу, а всё, что меняет базу по GET,
    рано или поздно щёлкает предзагрузчик браузера.
    """
    on = bool(model.verified)
    look = "btn-success" if on else "btn-outline-secondary"
    face = '<i class="fa-solid fa-check"></i> проверено' if on else "отметить"
    hint = "Снять отметку" if on else "Подтверждаю: так и есть, беру в работу"
    return Markup(
        f'<form method="post" action="{_ADMIN_BASE}/report-verify" class="d-inline">'
        f'<input type="hidden" name="id" value="{model.id}">'
        f'<button type="submit" class="btn btn-sm {look}" '
        f'title="{escape(hint, quote=True)}">{face}</button></form>'
    )


def _human_size(size: int) -> str:
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} КБ"
    return f"{size / 1024 / 1024:.1f} МБ".replace(".", ",")


def _report_files(model, attribute, request=None) -> list:
    """Приложенные файлы — карточками; список, а не строка, намеренно.

    Шаблон sqladmin проходит по связи поэлементно (`zip(value, formatted)`),
    поэтому форматтер обязан вернуть столько же кусков, сколько файлов.
    Ссылки на соседнюю вьюху не строим (`non_link_related_fields`):
    отдельной админки у файлов нет, открывается сам файл.
    """
    cards = []
    for row in model.files:
        href = f"{_ADMIN_BASE}/report-file/{row.id}"
        caption = escape(row.original_name or row.name)
        weight = _human_size(row.size)
        if row.content_type in attachments.IMAGE_TYPES:
            face = (
                f'<img src="{href}/thumb" alt="{caption}" loading="lazy" '
                f'style="max-width:220px;max-height:220px;border-radius:10px;'
                f'display:block;margin-bottom:.25rem">'
            )
        else:
            face = '<i class="fa-solid fa-file-arrow-down fa-2x d-block mb-1"></i>'
        cards.append(
            Markup(
                f'<a href="{href}" target="_blank" rel="noopener" '
                f'class="d-inline-block mb-2 text-decoration-none">{face}'
                f'<span class="small text-muted">{caption} · {weight}</span></a>'
            )
        )
    return cards


def _report_contact(model, attribute, request=None):
    """Телеграм — сразу ссылкой на переписку: разбор заявки кончается тем,
    что владелец пишет автору, и лишний copy-paste здесь ни к чему.

    Почта и телефон остаются текстом. HTML собираем руками, поэтому
    и ник, и адрес экранируем: контакт пришёл из формы, а не от нас.
    """
    contact = model.contact
    if not contact:
        return "—"
    url = telegram_url(contact)
    if not url:
        return contact
    return Markup(
        f'<a href="{escape(url, quote=True)}" target="_blank" '
        f'rel="noopener">{escape(contact)}</a>'
    )


class PlaceReportAdmin(ModelView, model=PlaceReport):
    """Очередь сообщений о неточностях с формы /report.

    Рабочий цикл: сверху новые, открыл, проверил по треку или отчёту,
    поправил карточку, поставил статус, написал автору — ссылка
    на переписку стоит прямо в списке.

    Отбор по статусу — адресом: `/admin/place-report/list?status=new`.
    Своих фильтров у sqladmin нет, а заводить ради одного целую вьюху
    незачем. Фильтр учтён и в счётчике страниц, иначе постраничная
    навигация обещала бы строки, которых на странице нет.

    Правится только то, что принадлежит владельцу, — статус и заметка.
    Сам текст заявки не наш: это чужие слова, и переписывать их нельзя,
    иначе через месяц не понять, что человек говорил на самом деле.
    """

    name = "Заявка"
    name_plural = "Заявки о неточностях"
    icon = "fa-solid fa-flag"
    # Отметка — второй колонкой, сразу за датой: в разбор садятся ради
    # неё, а длинный текст заявки уводил бы кнопку за правый край экрана
    column_list = [
        PlaceReport.created_at,
        PlaceReport.verified,
        PlaceReport.place,
        PlaceReport.topics,
        PlaceReport.comment,
        PlaceReport.contact,
        PlaceReport.status,
    ]
    column_details_list = [
        PlaceReport.id,
        PlaceReport.created_at,
        PlaceReport.place,
        PlaceReport.place_note,
        PlaceReport.topics,
        PlaceReport.comment,
        PlaceReport.files,
        PlaceReport.contact,
        PlaceReport.lang,
        PlaceReport.source,
        PlaceReport.verified,
        PlaceReport.status,
        PlaceReport.admin_note,
    ]
    # Файлы открываются сами, а не карточкой в админке: своей вьюхи у них нет
    non_link_related_fields = [PlaceReport.files]
    # Компактный список sqladmin берёт каждый элемент связи в скобки —
    # под снимком получалось «(развилка.png · 96 КБ)». Одна связь-список
    # у заявки и есть, файлы, так что настройка ничего больше не задевает
    show_compact_lists = False
    column_default_sort = ("created_at", True)
    column_sortable_list = [
        PlaceReport.created_at,
        PlaceReport.verified,
        PlaceReport.status,
    ]
    column_searchable_list = [
        PlaceReport.comment,
        PlaceReport.contact,
        PlaceReport.place_note,
    ]
    column_labels = {
        PlaceReport.created_at: "Пришла",
        PlaceReport.place: "Место",
        PlaceReport.place_note: "Вписано руками",
        PlaceReport.topics: "Что не так",
        PlaceReport.comment: "Текст",
        PlaceReport.contact: "Связь",
        PlaceReport.files: "Приложено",
        PlaceReport.verified: "Проверено",
        PlaceReport.lang: "Язык",
        PlaceReport.source: "Откуда",
        PlaceReport.status: "Статус",
        PlaceReport.admin_note: "Заметка",
    }
    column_formatters = {
        PlaceReport.created_at: lambda m, a: (
            m.created_at.strftime("%d.%m %H:%M") if m.created_at else ""
        ),
        PlaceReport.place: _report_where,
        PlaceReport.topics: lambda m, a: topic_names(m.topics),
        PlaceReport.comment: _report_excerpt,
        PlaceReport.contact: _report_contact,
        PlaceReport.verified: _report_verified,
        PlaceReport.status: lambda m, a: STATUS_RU.get(m.status, m.status),
    }
    column_formatters_detail = {
        PlaceReport.created_at: lambda m, a: (
            m.created_at.strftime("%d.%m.%Y %H:%M") if m.created_at else ""
        ),
        PlaceReport.place: _report_where,
        PlaceReport.topics: lambda m, a: topic_names(m.topics),
        PlaceReport.contact: _report_contact,
        PlaceReport.files: _report_files,
        PlaceReport.verified: lambda m, a: "да" if m.verified else "нет",
        PlaceReport.status: lambda m, a: STATUS_RU.get(m.status, m.status),
    }
    form_columns = [PlaceReport.verified, PlaceReport.status, PlaceReport.admin_note]
    form_overrides = {"status": SelectField}
    form_args = {
        "verified": {
            "label": "Проверено",
            "description": "Заявка подтвердилась и идёт в работу. "
                           "То же самое делает кнопка в списке",
        },
        "status": {
            "label": "Статус",
            "choices": [(s.value, STATUS_RU[s.value]) for s in ReportStatus],
            "coerce": str,
        },
        "admin_note": {
            "label": "Заметка",
            "description": "Что проверил и что поправил — для себя же через месяц",
        },
    }
    # Высота поля — в widget_args: form_args уходят прямо в конструктор
    # wtforms-поля, и «rows» там роняет форму
    form_widget_args = {"admin_note": {"rows": 4}}
    can_create = False
    # Удалять можно: спам чистится отсюда же

    def list_query(self, request: Request) -> Select:
        return self._picked(select(self.model), request)

    def count_query(self, request: Request) -> Select:
        return self._picked(super().count_query(request), request)

    @staticmethod
    def _picked(stmt: Select, request: Request) -> Select:
        """Отбор адресом: ?status=new и ?verified=1.

        `?verified=1` — и есть список работ: заявки, которые владелец
        прочитал и подтвердил. `?verified=0` — обратное, то, до чего руки
        ещё не дошли.
        """
        status = request.query_params.get("status", "")
        if status in STATUS_RU:
            stmt = stmt.where(PlaceReport.status == status)
        verified = request.query_params.get("verified", "")
        if verified in ("0", "1"):
            stmt = stmt.where(PlaceReport.verified.is_(verified == "1"))
        return stmt

    async def on_model_delete(self, model: PlaceReport, request: Request) -> None:
        """Заявку уносим вместе с приложенным.

        Строки уберёт CASCADE, а байты с диска — некому: на /privacy
        обещано, что разобранная заявка удаляется целиком, и файл,
        переживший её, это обещание нарушает.

        Имя файла — хеш содержимого, поэтому две заявки с одинаковым
        кадром делят один файл. Перед удалением проверяем, не остался ли
        он нужен второй, — иначе уборка за спамом уносила бы чужой снимок.
        """
        names = [f.name for f in model.files]
        if not names:
            return
        async with SessionLocal() as session:
            shared = set(
                (
                    await session.execute(
                        select(PlaceReportFile.name).where(
                            PlaceReportFile.name.in_(names),
                            PlaceReportFile.report_id != model.id,
                        )
                    )
                ).scalars()
            )
        for name in names:
            if name not in shared:
                attachments.drop(name)


class ReportFilesView(BaseView):
    """Служебные адреса разбора заявок: файлы и отметка «проверено».

    Не страница, а три ручки, поэтому из меню вьюха убрана. Живёт она
    именно здесь, внутри админки, ради одного: `@expose` заворачивает
    обработчик в проверку сессии. Файлы присылают незнакомые люди, и
    открываться они должны только владельцу — из /media каталог с ними
    поэтому и не отдаётся (см. main.py).
    """

    name = "Файлы заявок"

    def is_visible(self, request: Request) -> bool:
        return False

    @expose("/report-file/{file_id}", methods=["GET"])
    async def original(self, request: Request) -> Response:
        return await self._send(request, thumb=False)

    @expose("/report-file/{file_id}/thumb", methods=["GET"])
    async def preview(self, request: Request) -> Response:
        return await self._send(request, thumb=True)

    @staticmethod
    async def _send(request: Request, thumb: bool) -> Response:
        try:
            file_id = int(request.path_params.get("file_id", ""))
        except ValueError:
            return Response("Нет такого файла", status_code=404)
        async with SessionLocal() as session:
            row = await session.get(PlaceReportFile, file_id)
        if row is None:
            return Response("Нет такого файла", status_code=404)

        path = REPORTS_DIR / attachments.thumb_name(row.name) if thumb else None
        if path is None or not path.exists():
            path, thumb = REPORTS_DIR / row.name, False
        if not path.exists():
            # Строка есть, файла нет: том пересоздали или файл убрали
            # руками. 404 вместо ошибки: сама заявка должна открываться
            # и без снимка — текст в ней важнее
            log.warning("файл заявки пропал с диска: %s", row.name)
            return Response("Файл не найден на диске", status_code=404)

        media_type = "image/jpeg" if thumb else (row.content_type or "application/octet-stream")
        # Картинки показываем прямо в странице, остальное отдаём файлом:
        # PDF, открытый в браузере, выполняется на нашем домене, а прислал
        # его незнакомый человек. nosniff — чтобы тип не «уточнял» браузер
        return FileResponse(
            path,
            media_type=media_type,
            filename=None if thumb else (row.original_name or row.name),
            content_disposition_type=(
                "inline" if thumb or row.content_type in attachments.IMAGE_TYPES
                else "attachment"
            ),
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @expose("/report-verify", methods=["POST"])
    async def verify(self, request: Request) -> Response:
        """Переключает отметку и возвращает туда, откуда нажали.

        Возврат по Referer, а не по адресу списка: разбор идёт с фильтром
        и с третьей страницы, и прыжок в начало очереди после каждой
        галочки означал бы искать место заново.
        """
        form = await request.form()
        try:
            report_id = int(str(form.get("id", "")))
        except ValueError:
            return Response("Нет такой заявки", status_code=400)
        async with SessionLocal() as session:
            row = await session.get(PlaceReport, report_id)
            if row is None:
                return Response("Нет такой заявки", status_code=404)
            row.verified = not row.verified
            await session.commit()

        back = f"{_ADMIN_BASE}/place-report/list"
        referer = request.headers.get("referer", "")
        if referer:
            here = urlparse(referer)
            # Только свой адрес и только внутрь админки: Referer приходит
            # снаружи, и по нему можно увести куда угодно
            if here.netloc == request.url.netloc and here.path.startswith(
                f"{_ADMIN_BASE}/"
            ):
                back = here.path + (f"?{here.query}" if here.query else "")
        return RedirectResponse(back, status_code=303)


class AnnouncementAdmin(ModelView, model=Announcement):
    """Пуши по расписанию: заголовок, текст, время — и всем установкам.

    Время — по Ташкенту, как записано; планировщик (sayr-push.timer, раз
    в минуту) сам переведёт. Правится только запланированное: отправленное
    не трогаем, заводится новое. Счётчики и last_error показывают, дошло ли.
    """

    name = "Уведомление"
    name_plural = "Уведомления"
    icon = "fa-solid fa-bell"
    column_list = [
        Announcement.title,
        Announcement.send_at,
        Announcement.status,
        Announcement.sent_count,
        Announcement.failed_count,
        Announcement.place_slug,
    ]
    column_details_exclude_list = [Announcement.audience_lang, Announcement.audience_city]
    column_default_sort = ("send_at", True)
    column_sortable_list = [Announcement.send_at, Announcement.status]
    column_searchable_list = [Announcement.title]
    column_labels = {
        Announcement.title: "Заголовок",
        Announcement.body: "Текст",
        Announcement.send_at: "Когда (Ташкент)",
        Announcement.place_slug: "Место (slug)",
        Announcement.status: "Статус",
        Announcement.sent_count: "Дошло",
        Announcement.failed_count: "Не дошло",
        Announcement.last_error: "Ошибки",
        Announcement.sent_at: "Отправлено",
        Announcement.created_at: "Заведено",
    }
    column_formatters = {
        Announcement.send_at: lambda m, a: m.send_at.strftime("%d.%m.%Y %H:%M") if m.send_at else "",
        Announcement.status: lambda m, a: _STATUS_RU.get(m.status, m.status),
    }
    form_columns = [
        Announcement.title,
        Announcement.body,
        Announcement.send_at,
        Announcement.place_slug,
    ]
    form_args = {
        "send_at": {"description": "По Ташкенту. Планировщик проверяет раз в минуту"},
        "place_slug": {"description": "Необязательно: по тапу откроется это место"},
    }

    async def on_model_change(self, data: dict, model: Announcement, is_created: bool, request) -> None:
        if not is_created and model.status != AnnouncementStatus.scheduled.value:
            raise ValueError("Уже отправлено или отправляется — заведите новое уведомление")
        title = (data.get("title") or "").strip()
        body = (data.get("body") or "").strip()
        if not title or not body:
            raise ValueError("Нужны и заголовок, и текст")
        data["title"], data["body"] = title, body
        slug = (data.get("place_slug") or "").strip() or None
        data["place_slug"] = slug
        if slug:
            async with SessionLocal() as session:
                exists = await session.scalar(select(Place.id).where(Place.slug == slug))
            if exists is None:
                raise ValueError(f"Места «{slug}» нет в каталоге")

    async def on_model_delete(self, model: Announcement, request) -> None:
        if model.status == AnnouncementStatus.sending.value:
            raise ValueError("Сейчас отправляется — подождите минуту")


_STATUS_RU = {
    "scheduled": "запланировано",
    "sending": "отправляется",
    "sent": "отправлено",
    "failed": "не ушло",
    "cancelled": "отменено",
}


class AppUpdateAdmin(ModelView, model=AppUpdate):
    """Принудительное обновление: порог версии и флаг по платформам.

    Строки две, по одной на платформу, и они только правятся. Флаг без
    порога ничего не делает: сравнивается версия приложения с `min_version`,
    и алерт видят только те, кто ниже.
    """

    name = "Обновление приложения"
    name_plural = "Обновления приложений"
    icon = "fa-solid fa-arrow-up-from-bracket"
    column_list = [
        AppUpdate.platform,
        AppUpdate.min_version,
        AppUpdate.force,
        AppUpdate.note,
        AppUpdate.updated_at,
    ]
    column_labels = {
        AppUpdate.platform: "Платформа",
        AppUpdate.min_version: "Минимальная версия",
        AppUpdate.force: "Принудительно",
        AppUpdate.note: "Заметка",
        AppUpdate.updated_at: "Изменено",
    }
    form_columns = [AppUpdate.min_version, AppUpdate.force, AppUpdate.note]
    form_args = {
        "min_version": {"description": "Например 1.3.0 — версии ниже получат алерт, если стоит флаг"},
        "force": {"description": "Без флага порог не действует"},
    }
    can_create = False
    can_delete = False

    async def on_model_change(self, data: dict, model: AppUpdate, is_created: bool, request) -> None:
        version = (data.get("min_version") or "").strip()
        if not _VERSION.fullmatch(version):
            raise ValueError("Версия — числа через точку, например 1.3.0")
        data["min_version"] = version


_VERSION = re.compile(r"\d+(\.\d+){0,3}")


class PushTokenAdmin(ModelView, model=PushToken):
    """Установки, зарегистрировавшие пуш-токен. Только смотреть: сколько
    устройств на какой платформе и языке, и какие отвалились."""

    name = "Установка с пушами"
    name_plural = "Устройства с пушами"
    icon = "fa-solid fa-mobile-screen"
    column_list = [
        PushToken.platform,
        PushToken.lang,
        PushToken.city,
        PushToken.app_version,
        PushToken.last_seen,
        PushToken.disabled_at,
    ]
    column_default_sort = ("last_seen", True)
    column_sortable_list = [PushToken.platform, PushToken.last_seen, PushToken.disabled_at]
    column_searchable_list = [PushToken.device]
    column_labels = {
        PushToken.platform: "Платформа",
        PushToken.lang: "Язык",
        PushToken.city: "Город выезда",
        PushToken.app_version: "Версия",
        PushToken.last_seen: "Видели",
        PushToken.disabled_at: "Погашен",
        PushToken.disabled_reason: "Почему",
        PushToken.device: "Устройство",
        PushToken.created_at: "Впервые",
    }
    can_create = False
    can_edit = False


def mount_admin(app: FastAPI) -> Admin:
    admin = Admin(
        app,
        engine,
        title="Sayr Admin",
        # Абсолютный путь, а не "templates": по умолчанию sqladmin ищет папку
        # относительно рабочего каталога, и своя страница места находилась бы
        # только при запуске из server/
        templates_dir=str(SERVER_DIR / "templates"),
        # session_kwargs уходят в SessionMiddleware: по умолчанию он ставит
        # cookie без Secure и с same_site=lax — на публичном сервере это
        # сессия админа открытым текстом
        authentication_backend=BasicAuthBackend(
            secret_key=settings.secret_key,
            https_only=settings.admin_cookie_secure,
            same_site="strict",
        ),
    )
    admin.add_view(PlaceAdmin)
    admin.add_view(PlacePhotoAdmin)
    admin.add_view(PlaceTrackAdmin)
    admin.add_view(RegionAdmin)
    admin.add_view(PlaceReportAdmin)
    admin.add_base_view(ReportFilesView)
    admin.add_view(TesterSignupAdmin)
    admin.add_view(AnnouncementAdmin)
    admin.add_view(PushTokenAdmin)
    admin.add_view(AppUpdateAdmin)
    admin.add_view(StatsView)
    return admin
