"""Админка для кураторского наполнения каталога: /admin."""

import secrets
from pathlib import Path

from fastapi import FastAPI
from sqladmin import Admin, BaseView, ModelView, expose
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from starlette.responses import HTMLResponse
from wtforms import SelectMultipleField

from . import stats
from .config import GPX_DIR, settings
from .db import SessionLocal, engine
from .models import Place, PlacePhoto, PlaceTrack, Region, Season
from .services.gpx import track_stats
from .services.images import make_thumbnail
from .services.nearby import rebuild_for_track


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
    column_list = [Region.id, Region.name, Region.sort_order]
    form_columns = [Region.name, Region.sort_order]


class PlaceAdmin(ModelView, model=Place):
    name = "Место"
    name_plural = "Места"
    column_list = [Place.id, Place.name, Place.category, Place.difficulty, Place.is_published]
    column_searchable_list = [Place.name, Place.slug]
    column_default_sort = ("name", False)
    form_excluded_columns = [Place.photos, Place.created_at, Place.updated_at]
    form_overrides = {"best_seasons": SelectMultipleField}
    form_args = {
        "best_seasons": {
            "choices": [(s.value, s.value) for s in Season],
            "coerce": str,
        }
    }


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
        PlaceTrack.distance_km,
        PlaceTrack.ascent_m,
        PlaceTrack.sort_order,
    ]
    # Статистика считается из файла, руками её не вводят
    form_excluded_columns = [PlaceTrack.distance_km, PlaceTrack.ascent_m]

    async def after_model_change(self, data, model: PlaceTrack, is_created: bool, request) -> None:
        # Длина и набор — из загруженного файла. Имя файла берём ИЗ БАЗЫ,
        # а не из model: sqladmin передаёт сюда объект формы, где gpx_file —
        # это UploadFile без .name, и обращение к нему молча падало в except,
        # оставляя у трека «0 км · +0 м». Плюс при коллизии имён хранилище
        # дописывает _1, и правду знает только колонка.
        # Битый GPX не должен ронять сохранение: запись уже в базе, 500 после
        # коммита читается как «ничего не сохранилось»
        from sqlalchemy import select, update
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            stored = (
                await session.execute(
                    select(PlaceTrack.gpx_file).where(PlaceTrack.id == model.id)
                )
            ).scalar_one_or_none()
            if not stored:
                return
            try:
                stats = track_stats((GPX_DIR / Path(str(stored)).name).read_bytes())
            except Exception:
                return
            await session.execute(
                update(PlaceTrack)
                .where(PlaceTrack.id == model.id)
                .values(distance_km=stats.distance_km, ascent_m=stats.ascent_m)
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

    def top(rows: list[dict]) -> str:
        return _table(
            ["Место", "Открытий", "Устройств", "«Пойду»", "Загрузок GPX"],
            [
                [r["name"], r["opens"], r["devices"], r["votes"], r["downloads"]]
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
