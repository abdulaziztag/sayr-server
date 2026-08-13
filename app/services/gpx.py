"""Статистика GPX-трека: длина и набор высоты.

Считается на сервере при сохранении — в сиде и в админке. Клиент ничего
не считает и качает только файл выбранного маршрута: файлы, загруженные
в админку сырыми из записывающих приложений, весят мегабайты, и качать
их все, чтобы показать «8,2 км · +1571 м» в выборе маршрута, нельзя.
"""

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass

_NS = "{http://www.topografix.com/GPX/1/1}"

# Подъёмы короче этого не считаем: сырые записи шумят по высоте,
# и без порога набор завышается на сотни метров
_ASCENT_THRESHOLD_M = 3.0


@dataclass(frozen=True)
class TrackStats:
    distance_km: float
    ascent_m: int


def track_stats(data: bytes) -> TrackStats:
    """Длина и набор по точкам всех треков файла. Пустой файл — нули."""
    root = ET.fromstring(data)
    points: list[tuple[float, float, float | None]] = []
    for trkpt in root.iter(f"{_NS}trkpt"):
        ele = trkpt.find(f"{_NS}ele")
        points.append((
            float(trkpt.get("lat")),
            float(trkpt.get("lon")),
            float(ele.text) if ele is not None and ele.text else None,
        ))

    distance = 0.0
    ascent = 0.0
    # Набор копим от «якоря»: подъём засчитывается, только когда высота
    # ушла от него вверх дальше порога, — так дрожание записи не суммируется
    anchor: float | None = None
    for prev, cur in zip(points, points[1:]):
        distance += _haversine_m(prev[0], prev[1], cur[0], cur[1])
        ele = cur[2]
        if ele is None:
            continue
        if anchor is None:
            anchor = ele
        elif ele > anchor + _ASCENT_THRESHOLD_M:
            ascent += ele - anchor
            anchor = ele
        elif ele < anchor:
            anchor = ele

    return TrackStats(distance_km=round(distance / 1000, 1), ascent_m=round(ascent))


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
