from datetime import datetime

from pydantic import BaseModel

from .models import Difficulty, Place, PlaceCategory, PlacePhoto


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
    best_seasons: list[str]
    kid_friendly: bool
    short_desc: str
    cover_url: str | None
    cover_thumb_url: str | None
    has_gpx: bool


class PlaceDetail(PlaceListItem):
    description_md: str
    how_to_get_md: str
    photos: list[PhotoOut]
    gpx_url: str | None
    gpx_credit: str | None


class WeatherDay(BaseModel):
    date: str
    t_min: float
    t_max: float
    precip_prob: int | None
    wind_max: float | None
    weathercode: int


class WeatherOut(BaseModel):
    updated_at: datetime
    days: list[WeatherDay]


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
        best_seasons=list(p.best_seasons or []),
        kid_friendly=p.kid_friendly,
        short_desc=p.short_desc,
        cover_url=cover.url if cover else None,
        cover_thumb_url=(cover.thumb_url or cover.url) if cover else None,
        has_gpx=p.gpx_file is not None,
    )


def place_list_item(p: Place) -> PlaceListItem:
    return PlaceListItem(**_base_fields(p))


def place_detail(p: Place) -> PlaceDetail:
    return PlaceDetail(
        **_base_fields(p),
        description_md=p.description_md,
        how_to_get_md=p.how_to_get_md,
        photos=[photo_out(ph) for ph in p.photos],
        gpx_url=p.gpx_url,
        gpx_credit=p.gpx_credit,
    )
