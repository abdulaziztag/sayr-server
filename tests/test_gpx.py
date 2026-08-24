"""Направление и полнота записи: развороты и «сколько на самом деле идти»."""

import math

from app.services.gpx import (
    records_full_trip,
    recorded_from_target,
    reverse_track,
    track_stats,
)


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


# MARK: - Полнота записи
#
# Время в карточке — ход ТУДА И ОБРАТНО, а из 174 файлов каталога 132
# обрываются на цели. Пока это не различалось, каталог обещал половину:
# Амир Темур стоял с 2,5 часа на 29,5 км и наборе 3458 м.


def test_record_stopping_at_target_is_not_full_trip():
    """Запись, оборванная на вершине: концы врозь, возврата по своим следам нет."""
    pts = [
        (41.70 + 0.02 * (i / 40), 70.10 - 0.03 * (i / 40), 1000 + 600 * (i / 40))
        for i in range(41)
    ]
    assert records_full_trip(_gpx(pts)) is False


def test_loop_is_full_trip():
    """Кольцо: концы сошлись, удваивать нечего."""
    pts = []
    for i in range(61):
        a = 2 * math.pi * i / 60
        pts.append(
            (41.71 + 0.01 * math.sin(a), 70.08 + 0.01 * math.cos(a), 1000 + 300 * math.sin(a))
        )
    assert records_full_trip(_gpx(pts)) is True


def test_retraced_return_is_full_trip_even_with_open_ends():
    """Вернулись той же тропой, но выключили запись, не дойдя до машины.

    Концы разнесены, а путь пройден весь — удвоение обещало бы двойной день.
    """
    up = [
        (41.70 + 0.02 * (i / 40), 70.10 - 0.03 * (i / 40), 1000 + 600 * (i / 40))
        for i in range(41)
    ]
    # Возврат по своим следам, но останавливаемся на четверти пути от старта
    down = list(reversed(up))[: int(len(up) * 0.75)]
    assert records_full_trip(_gpx(up + down)) is True


def test_near_loop_with_parted_ends_is_full_trip():
    """Полвонакская тройка: кольцо, у которого концы разошлись на 11 % длины.

    По пятипроцентному порогу проверки направления это ушло бы в «одну
    сторону», и время удвоилось бы с пяти с половиной часов до восьми
    с половиной. Порог здесь свой, пятнадцать процентов, — ровно из-за
    таких записей.
    """
    pts = []
    for i in range(55):  # неполное кольцо: не доходим до старта
        a = 2 * math.pi * i / 60
        pts.append(
            (41.71 + 0.01 * math.sin(a), 70.08 + 0.01 * math.cos(a), 1000 + 300 * math.sin(a))
        )
    assert records_full_trip(_gpx(pts)) is True


def test_too_short_to_judge_is_left_alone():
    """На огрызке записи гадать не о чем — удвоение не выдумываем."""
    pts = [(41.70 + 0.0001 * i, 70.10, 1000 + i) for i in range(5)]
    assert records_full_trip(_gpx(pts)) is True
