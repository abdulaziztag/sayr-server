from pathlib import Path

from app.services.gpx import clean, track_stats

GPX_DIR = Path(__file__).resolve().parent.parent / "seed" / "data" / "gpx"

# Кусок сырой записи: время, точность приёма и скорость у каждой точки —
# ровно то, что приходит из записывающих приложений
RAW = b"""<?xml version="1.0" encoding="utf-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
 <trk><name>t</name><trkseg>
  <trkpt lat="41.1000" lon="70.1000"><ele>1000.0</ele><time>2023-08-19T01:08:13Z</time><hdop>3.6</hdop></trkpt>
  <trkpt lat="41.1001" lon="70.1000"><ele>1004.0</ele><time>2023-08-19T01:08:14Z</time><hdop>3.6</hdop></trkpt>
  <trkpt lat="41.1002" lon="70.1000"><ele>1008.0</ele><time>2023-08-19T01:08:15Z</time><hdop>3.6</hdop></trkpt>
  <trkpt lat="41.1003" lon="70.1000"><ele>1012.0</ele><time>2023-08-19T01:08:16Z</time><hdop>3.6</hdop></trkpt>
 </trkseg></trk>
</gpx>"""


def test_time_is_stripped():
    """По чужому треку не должно быть видно, когда человек шёл."""
    out = clean(RAW)
    assert b"<time>" not in out
    assert b"2023-08-19" not in out


def test_recorder_noise_is_stripped():
    out = clean(RAW)
    assert b"hdop" not in out


def test_elevation_survives():
    """Высота нужна: по ней считается набор."""
    out = clean(RAW)
    assert b"<ele>" in out
    assert track_stats(out).ascent_m > 0


def test_straight_line_collapses_to_ends():
    """Точки на прямой ничего не добавляют форме — остаются только концы."""
    out = clean(RAW)
    assert out.count(b"<trkpt") == 2


def test_our_tracks_are_untouched():
    """Наши треки уже прорежены: чистка не должна менять их статистику."""
    for path in sorted(GPX_DIR.glob("*.gpx")):
        raw = path.read_bytes()
        before, after = track_stats(raw), track_stats(clean(raw))
        assert before == after, path.name


def test_shape_survives_simplification():
    """Прореживание меняет длину и набор в пределах допустимого.

    Считаем на настоящем треке каталога, а не на синтетике: важно, что
    порог подобран под реальную запись, а не под ровную линию.
    """
    raw = (GPX_DIR / "bolshoy-chimgan-aksay.gpx").read_bytes()
    before, after = track_stats(raw), track_stats(clean(raw))
    assert abs(after.distance_km - before.distance_km) <= before.distance_km * 0.02
    assert abs(after.ascent_m - before.ascent_m) <= before.ascent_m * 0.02


def test_empty_segment_survives():
    empty = RAW.replace(
        RAW[RAW.index(b"  <trkpt") : RAW.index(b" </trkseg>")], b""
    )
    assert clean(empty).count(b"<trkpt") == 0


def test_start_point_is_the_first_recorded_point():
    """Старт — оттуда, откуда пошли пешком, а не координаты самого места:
    в автонавигатор вбивают именно его."""
    stats = track_stats(RAW)
    assert (stats.start_lat, stats.start_lng) == (41.1000, 70.1000)


def test_start_survives_cleaning():
    """Прореживание не должно съедать первую точку."""
    assert track_stats(clean(RAW)).start_lat == 41.1000


def test_empty_track_has_no_start():
    empty = b'<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"/>'
    assert track_stats(empty).start_lat is None


def _long(n: int, wiggle: float = 0.000002) -> bytes:
    """Сырой трек из n точек шагом в метр с дрожанием меньше порога."""
    body = "".join(
        f'<trkpt lat="{41.1 + i * 0.00001:.6f}" lon="{70.1 + (wiggle if i % 2 else 0):.6f}">'
        f"<ele>1000.0</ele></trkpt>\n"
        for i in range(n)
    )
    return (
        b'<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
        b"<trk><name>t</name><trkseg>\n" + body.encode() + b"</trkseg></trk></gpx>"
    )


def test_thin_if_heavy_leaves_short_files_alone():
    from app.services.gpx import thin_if_heavy

    data = _long(300)
    assert thin_if_heavy(data) is data


def test_thin_if_heavy_thins_long_files():
    from app.services.gpx import MAX_POINTS, thin_if_heavy, track_coords

    data = _long(MAX_POINTS + 500)
    out = thin_if_heavy(data)
    assert len(track_coords(out)) < 100, "дрожание в полметра прямой не делает"
    assert abs(track_stats(out).distance_km - track_stats(data).distance_km) <= 0.1


async def test_admin_upload_thins_heavy_track(client, admin_client):
    """Загрузили сырую запись в админку — в хранилище лёг лёгкий файл, цифры по нему."""
    from sqlalchemy import delete, select

    from app.config import GPX_DIR as STORE
    from app.db import SessionLocal
    from app.models import Place, PlaceTrack
    from app.services.gpx import MAX_POINTS, track_coords

    data = _long(MAX_POINTS + 800)
    try:
        async with SessionLocal() as session:
            place_id = (await session.execute(
                select(Place.id).where(Place.slug == "test-lake"))).scalar_one()
        resp = await admin_client.post(
            "/admin/place-track/create",
            data={"place": str(place_id), "name": "thin-test upload", "name_uz": "",
                  "gpx_credit": "", "sort_order": "0", "save": "Save"},
            files={"gpx_file": ("thin-test.gpx", data, "application/gpx+xml")},
            follow_redirects=False)
        assert resp.status_code == 302, resp.text[:600]
        async with SessionLocal() as session:
            track = (await session.execute(
                select(PlaceTrack).where(PlaceTrack.name == "thin-test upload"))).scalar_one()
            stored = (STORE / Path(str(track.gpx_file.name)).name).read_bytes()
            assert len(track_coords(stored)) < 100
            assert len(stored) < len(data) // 10
            assert track.distance_km == track_stats(stored).distance_km
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(PlaceTrack).where(PlaceTrack.name.like("thin-test%")))
            await session.commit()
        for f in STORE.glob("thin-test*"):
            f.unlink()


async def test_thin_tracks_rewrites_only_heavy(client):
    from fastapi_storages import StorageFile
    from sqlalchemy import delete, select

    from app.config import GPX_DIR as STORE
    from app.db import SessionLocal
    from app.models import Place, PlaceTrack, gpx_storage
    from app.services.gpx import MAX_POINTS
    from seed import thin_tracks

    heavy, light = _long(MAX_POINTS + 300), _long(200)
    (STORE / "thin-heavy.gpx").write_bytes(heavy)
    (STORE / "thin-light.gpx").write_bytes(light)
    try:
        async with SessionLocal() as session:
            place_id = (await session.execute(
                select(Place.id).where(Place.slug == "test-lake"))).scalar_one()
            for name in ("thin-heavy", "thin-light"):
                session.add(PlaceTrack(
                    place_id=place_id, name=name, distance_km=99.0, ascent_m=99,
                    gpx_file=StorageFile(name=f"{name}.gpx", storage=gpx_storage)))
            await session.commit()

        report = await thin_tracks.run(apply=False)
        assert [r[0] for r in report] == ["thin-heavy"]
        assert (STORE / "thin-heavy.gpx").read_bytes() == heavy, "без --apply файл цел"

        await thin_tracks.run(apply=True)
        assert len((STORE / "thin-heavy.gpx").read_bytes()) < len(heavy) // 10
        assert (STORE / "thin-light.gpx").read_bytes() == light
        async with SessionLocal() as session:
            rows = {t.name: t for t in (await session.execute(
                select(PlaceTrack).where(PlaceTrack.name.like("thin-%")))).scalars()}
        assert rows["thin-heavy"].distance_km != 99.0, "статистика пересчитана по новому файлу"
        assert rows["thin-light"].distance_km == 99.0, "лёгкий трек не тронут"
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(PlaceTrack).where(PlaceTrack.name.like("thin-%")))
            await session.commit()
        for f in STORE.glob("thin-*"):
            f.unlink()
