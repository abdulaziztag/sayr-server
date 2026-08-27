"""Админка для кураторского наполнения каталога: /admin."""

import logging
import secrets
from pathlib import Path

from fastapi import FastAPI
from sqladmin import Admin, BaseView, ModelView, expose
from sqladmin.authentication import AuthenticationBackend
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from wtforms import SelectMultipleField

try:  # расположение менялось между версиями пакета
    from fastapi_storages import StorageFile
except ImportError:  # pragma: no cover
    from fastapi_storages.base import StorageFile

from . import stats
from .config import GPX_DIR, SERVER_DIR, settings
from .db import SessionLocal, engine
from .models import Place, PlacePhoto, PlaceTrack, Region, Season, photo_storage
from .services.gpx import recorded_from_target, reverse_track, track_stats
from .services.images import make_thumbnail, retire_photo, store_upload
from .services.nearby import rebuild_for_track


log = logging.getLogger("sayr.admin")


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
    form_overrides = {"best_seasons": SelectMultipleField}
    form_args = {
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
    admin.add_view(StatsView)
    return admin
