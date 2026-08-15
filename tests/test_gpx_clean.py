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
