"""Админка для кураторского наполнения каталога: /admin."""

import secrets
from pathlib import Path

from fastapi import FastAPI
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from wtforms import SelectMultipleField

from .config import GPX_DIR, settings
from .db import engine
from .models import Place, PlacePhoto, PlaceTrack, Region, Season
from .services.gpx import track_stats
from .services.images import make_thumbnail


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
    return admin
