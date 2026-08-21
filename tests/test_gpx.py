"""Направление записи: разворот треков, снятых на спуске."""

import math

from app.services.gpx import recorded_from_target, reverse_track, track_stats


def _gpx(points: list[tuple[float, float, float]]) -> bytes:
    """Собрать GPX из точек (широта, долгота, высота)."""
    body = "\n".join(
        f'  <trkpt lat="{lat:.6f}" lon="{lng:.6f}"><ele>{ele:.1f}</ele></trkpt>'
        for lat, lng, ele in points
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">\n'
        f"<trk><trkseg>\n{body}\n</trkseg></trk>\n</gpx>"
    ).encode()

def test_recorded_from_target_flips_descent_record():
    """Запись «от цели вниз» опознаётся и после разворота даёт набор к цели."""
    # Тропа от парковки на 1000 м к водопаду на 1600 м, записанная НА СПУСКЕ:
    # файл начинается у цели и уходит вниз
    target = (41.70, 70.10)
    pts = []
    for i in range(41):
        k = i / 40
        pts.append((41.70 + 0.02 * k, 70.10 - 0.03 * k, 1600 - 600 * k))
    gpx = _gpx(pts)

    assert recorded_from_target(gpx, *target) is True
    assert track_stats(gpx).ascent_m < 50
    flipped = reverse_track(gpx)
    assert track_stats(flipped).ascent_m > 500
    # Старт переехал с цели на дальний конец
    assert track_stats(flipped).start_lat != round(target[0], 6)
    # Повторный прогон ничего не меняет: правка идемпотентна
    assert recorded_from_target(flipped, *target) is False


def test_normal_record_is_not_flipped():
    """Обычная запись К ЦЕЛИ не трогается."""
    target = (41.72, 70.07)
    pts = []
    for i in range(41):
        k = i / 40
        pts.append((41.70 + 0.02 * k, 70.10 - 0.03 * k, 1000 + 600 * k))
    assert recorded_from_target(_gpx(pts), *target) is False


def test_closed_loop_is_not_flipped():
    """У кольца оба конца у дороги — переворачивать нечего."""
    target = (41.7150, 70.0850)
    pts = []
    for i in range(61):
        a = 2 * math.pi * i / 60
        pts.append((41.71 + 0.01 * math.sin(a), 70.08 + 0.01 * math.cos(a), 1000 + 300 * math.sin(a)))
    assert recorded_from_target(_gpx(pts), *target) is False
