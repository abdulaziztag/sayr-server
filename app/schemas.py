from datetime import datetime

from pydantic import BaseModel

from .models import Difficulty, OvernightType, Place, PlaceCategory, PlacePhoto, PlaceTrack


class RegionOut(BaseModel):
    id: int
    name: str
    places_count: int = 0


class PhotoOut(BaseModel):
    url: str
    thumb_url: str
    credit: str = ""


class PlaceListItem(BaseModel):
    id: int
    slug: str
    name: str
    category: PlaceCategory
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


def _base_fields(p: Place) -> dict:
    cover = p.photos[0] if p.photos else None
    return dict(
        id=p.id,
        slug=p.slug,
        name=p.name,
        category=p.category,
        difficulty=p.difficulty,
        region_id=p.region_id,
        region_name=p.region.name,
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
        best_seasons=list(p.best_seasons or []),
        kid_friendly=p.kid_friendly,
        short_desc=p.short_desc,
        cover_url=cover.url if cover else None,
        cover_thumb_url=(cover.thumb_url or cover.url) if cover else None,
        has_gpx=bool(p.tracks),
    )


def place_list_item(p: Place) -> PlaceListItem:
    return PlaceListItem(**_base_fields(p))


def track_out(t: PlaceTrack) -> TrackOut:
    return TrackOut(
        id=t.id,
        name=t.name,
        gpx_url=t.gpx_url or "",
        credit=t.gpx_credit,
        distance_km=t.distance_km,
        ascent_m=t.ascent_m,
        start_lat=t.start_lat,
        start_lng=t.start_lng,
    )


def nearby_out(p: Place, distance_m: int) -> NearbyOut:
    cover = p.photos[0] if p.photos else None
    return NearbyOut(
        slug=p.slug,
        name=p.name,
        category=p.category,
        cover_thumb_url=(cover.thumb_url or cover.url) if cover else None,
        distance_m=distance_m,
    )


def place_detail(p: Place, nearby: list[NearbyOut] | None = None) -> PlaceDetail:
    primary = p.tracks[0] if p.tracks else None
    return PlaceDetail(
        **_base_fields(p),
        description_md=p.description_md,
        how_to_get_md=p.how_to_get_md,
        photos=[photo_out(ph) for ph in p.photos],
        tracks=[track_out(t) for t in p.tracks],
        gpx_url=primary.gpx_url if primary else None,
        gpx_credit=primary.gpx_credit if primary else None,
        nearby=nearby or [],
    )
