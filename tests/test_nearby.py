import pytest
from sqlalchemy import delete, select

from app.services.gpx import bbox, distance_to_track_m, track_coords
from app.services.nearby import RADIUS_M, rebuild_for_track

# Прямой отрезок с юга на север длиной около 1,1 км: двух точек достаточно,
# ровно так выглядит прореженный участок тропы без поворотов
LINE = b"""<?xml version="1.0" encoding="utf-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
 <trk><trkseg>
  <trkpt lat="41.1000" lon="70.1000"><ele>1000.0</ele></trkpt>
  <trkpt lat="41.1100" lon="70.1000"><ele>1100.0</ele></trkpt>
 </trkseg></trk>
</gpx>"""


def test_point_on_the_line_is_zero():
    coords = track_coords(LINE)
    assert distance_to_track_m(coords, 41.1050, 70.1000) < 1


def test_midpoint_measured_to_line_not_to_nearest_point():
    """Ради этого расстояние и меряется до линии.

    Точка стоит посреди отрезка в 40 метрах сбоку. До ближайшей записанной
    точки от неё больше полукилометра, и мерка по точкам объявила бы место
    далёким, хотя тропа проходит вплотную.
    """
    coords = track_coords(LINE)
    assert distance_to_track_m(coords, 41.1050, 70.10048) < RADIUS_M


def test_far_point_is_out():
    coords = track_coords(LINE)
    # Полтора километра на восток — за порогом
    assert distance_to_track_m(coords, 41.1050, 70.1180) > RADIUS_M


def test_empty_track_is_infinitely_far():
    assert distance_to_track_m([], 41.10, 70.10) == float("inf")


def test_bbox_covers_all_points():
    south, west, north, east = bbox(track_coords(LINE))
    assert (south, north) == (41.1000, 41.1100)
    assert west == east == 70.1000


def _gpx_through(*points: tuple[float, float]) -> bytes:
    body = "".join(
        f'<trkpt lat="{lat}" lon="{lng}"><ele>1500.0</ele></trkpt>' for lat, lng in points
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
        f"<trk><trkseg>{body}</trkseg></trk></gpx>"
    ).encode()


@pytest.fixture
async def track_from_peak_to_lake():
    """Трек пика, который по дороге заходит на озеро.

    Ровно случай Пальтау: один выход, два разных места каталога.
    """
    try:
        from fastapi_storages import StorageFile
    except ImportError:
        from fastapi_storages.base import StorageFile

    from app.config import GPX_DIR
    from app.db import SessionLocal
    from app.models import Place, PlaceNeighbor, PlaceTrack, gpx_storage

    name = "test-neighbors.gpx"
    GPX_DIR.mkdir(parents=True, exist_ok=True)
    async with SessionLocal() as session:
        peak = (
            await session.execute(select(Place).where(Place.slug == "test-peak"))
        ).scalar_one()
        lake = (
            await session.execute(select(Place).where(Place.slug == "test-lake"))
        ).scalar_one()
        (GPX_DIR / name).write_bytes(
            _gpx_through((peak.lat, peak.lng), (lake.lat, lake.lng))
        )
        track = PlaceTrack(
            place_id=peak.id,
            name="Через озеро",
            gpx_file=StorageFile(name=name, storage=gpx_storage),
        )
        session.add(track)
        await session.flush()
        await rebuild_for_track(session, track)
        await session.commit()
        track_id = track.id

    yield

    async with SessionLocal() as session:
        await session.execute(delete(PlaceNeighbor).where(PlaceNeighbor.track_id == track_id))
        await session.execute(delete(PlaceTrack).where(PlaceTrack.id == track_id))
        await session.commit()
    (GPX_DIR / name).unlink(missing_ok=True)


async def test_detail_lists_the_neighbor(client, track_from_peak_to_lake):
    data = (await client.get("/api/v1/places/test-peak")).json()
    assert [n["slug"] for n in data["nearby"]] == ["test-lake"]


async def test_link_is_visible_from_the_other_side(client, track_from_peak_to_lake):
    """Трек принадлежит пику, но на карточке озера связь так же осмысленна."""
    data = (await client.get("/api/v1/places/test-lake")).json()
    assert [n["slug"] for n in data["nearby"]] == ["test-peak"]


async def test_distance_is_between_places(client, track_from_peak_to_lake):
    """Показываем расстояние между местами, а не подход трека к точке."""
    neighbor = (await client.get("/api/v1/places/test-peak")).json()["nearby"][0]
    assert 12_000 < neighbor["distance_m"] < 14_000


async def test_unrelated_place_has_no_neighbors(client, track_from_peak_to_lake):
    data = (await client.get("/api/v1/places/test-waterfall")).json()
    assert data["nearby"] == []
