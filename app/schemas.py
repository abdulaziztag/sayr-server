from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from .models import Difficulty, OvernightType, Place, PlaceCategory, PlacePhoto, PlaceTrack

# Язык ответа. Список закрытый и совпадает с тем, что зашито в клиентах
# (AppLanguage на обеих платформах); «как в системе» там нет, и здесь тоже
Lang = Literal["ru", "uz"]
DEFAULT_LANG: Lang = "ru"


def pick(ru: str, uz: str | None, lang: Lang) -> str:
    """Перевод, если он есть; иначе оригинал.

    Пустая строка в `uz` считается отсутствием перевода наравне с NULL:
    сохранить в админке пустое поле — обычный способ сказать «не переведено»,
    и заставлять человека помнить разницу между NULL и '' незачем.

    Фолбэк молчаливый: перевод каталога наливается порциями, и место
    с готовым названием, но ещё не переведённым описанием, должно
    показываться целиком, а не наполовину.
    """
    if lang == "uz" and uz:
        return uz
    return ru


class RegionOut(BaseModel):
    id: int
    name: str
    places_count: int = 0
    # Область — для группировки регионов в фильтре; None, пока не проставлена
    area: str | None = None


class PhotoOut(BaseModel):
    url: str
    thumb_url: str
    credit: str = ""


class PlaceListItem(BaseModel):
    id: int
    slug: str
    name: str
    category: PlaceCategory
    # Коды коллекций «Проекта 21»; пусто у большинства мест
    collections: list[str] = []
    difficulty: Difficulty
    region_id: int
    region_name: str
    lat: float
    lng: float
    elevation_m: int | None
    distance_km: float | None
    duration_hours: float | None
    elevation_gain_m: int | None
    drive_minutes: int | None
    drive_km: float | None
    season_from: int | None
    season_to: int | None
    overnight: OvernightType | None
    #: Четвёртая ступень отдельным флагом, а не значением difficulty:
    #: незнакомая строка уронила бы разбор каталога у старых сборок
    alpine: bool
    trip_days: int | None
    best_seasons: list[str]
    kid_friendly: bool
    short_desc: str
    cover_url: str | None
    cover_thumb_url: str | None
    has_gpx: bool


class TrackOut(BaseModel):
    id: int
    name: str
    gpx_url: str
    credit: str | None
    distance_km: float
    ascent_m: int
    # Точка старта маршрута, а не самого места: её человек вбивает в навигатор
    start_lat: float | None = None
    start_lng: float | None = None


class NearbyOut(BaseModel):
    """Соседнее место: то, мимо чего проходит трек одного из двух."""

    slug: str
    name: str
    category: PlaceCategory
    cover_thumb_url: str | None
    distance_m: int


class PlaceDetail(PlaceListItem):
    description_md: str
    how_to_get_md: str
    photos: list[PhotoOut]
    tracks: list[TrackOut]
    # Старые клиенты живут на этих полях: заполняются из основного
    # (первого по порядку) трека
    gpx_url: str | None
    gpx_credit: str | None
    nearby: list[NearbyOut] = []


class WeatherDay(BaseModel):
    date: str
    t_min: float
    t_max: float
    precip_prob: int | None
    wind_max: float | None
    weathercode: int


class WeatherHour(BaseModel):
    time: str
    t: float
    precip_prob: int | None
    wind: float | None
    weathercode: int


class WeatherOut(BaseModel):
    updated_at: datetime
    days: list[WeatherDay]
    hours: list[WeatherHour] = []


def photo_out(p: PlacePhoto) -> PhotoOut:
    return PhotoOut(url=p.url or "", thumb_url=p.thumb_url or p.url or "", credit=p.credit)


def _base_fields(p: Place, lang: Lang = DEFAULT_LANG) -> dict:
    cover = p.photos[0] if p.photos else None
    return dict(
        id=p.id,
        slug=p.slug,
        name=pick(p.name, p.name_uz, lang),
        category=p.category,
        collections=list(p.collections or []),
        # Наружу едут только три исходные ступени. И Swift, и kotlinx
        # падают на значении, которого нет в их enum, — падает при этом
        # разбор всего списка, а не одного места, и человек со старой
        # сборкой остаётся с пустым каталогом. Четвёртая ступень
        # приезжает как «сложно» и повторяется во флаге ниже
        difficulty=Difficulty.hard if p.difficulty is Difficulty.extreme else p.difficulty,
        region_id=p.region_id,
        region_name=pick(p.region.name, p.region.name_uz, lang),
        lat=p.lat,
        lng=p.lng,
        elevation_m=p.elevation_m,
        distance_km=p.distance_km,
        duration_hours=p.duration_hours,
        elevation_gain_m=p.elevation_gain_m,
        drive_minutes=p.drive_minutes,
        drive_km=p.drive_km,
        season_from=p.season_from,
        season_to=p.season_to,
        overnight=p.overnight,
        alpine=p.difficulty is Difficulty.extreme,
        trip_days=p.trip_days,
        best_seasons=list(p.best_seasons or []),
        kid_friendly=p.kid_friendly,
        short_desc=pick(p.short_desc, p.short_desc_uz, lang),
        cover_url=cover.url if cover else None,
        cover_thumb_url=(cover.thumb_url or cover.url) if cover else None,
        has_gpx=bool(p.tracks),
    )


def place_list_item(p: Place, lang: Lang = DEFAULT_LANG) -> PlaceListItem:
    return PlaceListItem(**_base_fields(p, lang))


def track_out(t: PlaceTrack, lang: Lang = DEFAULT_LANG) -> TrackOut:
    return TrackOut(
        id=t.id,
        name=pick(t.name, t.name_uz, lang),
        gpx_url=t.gpx_url or "",
        credit=t.gpx_credit,
        distance_km=t.distance_km,
        ascent_m=t.ascent_m,
        start_lat=t.start_lat,
        start_lng=t.start_lng,
    )


def nearby_out(p: Place, distance_m: int, lang: Lang = DEFAULT_LANG) -> NearbyOut:
    cover = p.photos[0] if p.photos else None
    return NearbyOut(
        slug=p.slug,
        name=pick(p.name, p.name_uz, lang),
        category=p.category,
        cover_thumb_url=(cover.thumb_url or cover.url) if cover else None,
        distance_m=distance_m,
    )


def place_detail(
    p: Place, nearby: list[NearbyOut] | None = None, lang: Lang = DEFAULT_LANG
) -> PlaceDetail:
    primary = p.tracks[0] if p.tracks else None
    return PlaceDetail(
        **_base_fields(p, lang),
        description_md=pick(p.description_md, p.description_md_uz, lang),
        how_to_get_md=pick(p.how_to_get_md, p.how_to_get_md_uz, lang),
        photos=[photo_out(ph) for ph in p.photos],
        tracks=[track_out(t, lang) for t in p.tracks],
        gpx_url=primary.gpx_url if primary else None,
        gpx_credit=primary.gpx_credit if primary else None,
        nearby=nearby or [],
    )
