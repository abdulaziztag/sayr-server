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

# Прореживание: точку выбрасываем, если без неё линия трека сдвинется меньше
# чем на столько метров.
#
# Метр подобран по данным, а не на глаз. На тесте с треком Бабайтага
# (54 523 точки, 12,5 МБ) набор высоты при этом пороге даёт 2278 м против
# 2279 м, заявленных источником, — то есть профиль сохраняется целиком.
# Дальше порог начинает съедать рельеф: при 3 м набор уже 1998 м, при 5 м —
# 1925 м. Файл при этом худеет с 12,5 МБ до 172 КБ.
_SIMPLIFY_EPSILON_M = 1.0


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
    # Якорь берём с ПЕРВОЙ точки, а не со второй: иначе подъём на стартовом
    # отрезке пропадал. На сырых записях с шагом в метр это терялось в шуме,
    # а после прореживания отрезки длинные, и потеря становится заметной
    anchor: float | None = next((p[2] for p in points if p[2] is not None), None)
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


def clean(data: bytes, epsilon_m: float = _SIMPLIFY_EPSILON_M) -> bytes:
    """Подготовить чужой трек к публикации: снять лишнее и проредить.

    Файлы из записывающих приложений — это сырые логи: точка раз в секунду,
    время каждой, точность приёма, скорость. Публиковать их нельзя по двум
    причинам сразу.

    Время — это данные о человеке: по такому файлу видно, в какой день и час
    автор шёл. Свои треки мы от них чистим, чужие тем более, а файл у нас
    скачивается публично.

    Вес — это трафик там, где связи меньше всего: 54 тысячи точек на 18 км
    весят 12 МБ, и клиент качает файл выбранного маршрута целиком.
    """
    root = ET.fromstring(data)
    for seg in root.iter(f"{_NS}trkseg"):
        points = list(seg.findall(f"{_NS}trkpt"))
        keep = set(_significant(points, epsilon_m))
        for i, pt in enumerate(points):
            if i not in keep:
                seg.remove(pt)
                continue
            # Высоту сохраняем: по ней считается набор. Остальное — мусор
            # регистратора: время, точность приёма, скорость, крен
            for child in list(pt):
                if not child.tag.endswith("}ele"):
                    pt.remove(child)
    for node in root.iter():
        for child in list(node):
            if child.tag == f"{_NS}time":
                node.remove(child)
    ET.register_namespace("", _NS[1:-1])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _significant(points: list, epsilon_m: float) -> list[int]:
    """Дуглас–Пекер: индексы точек, без которых форма тропы поедет.

    Итеративно, а не рекурсией: на 54 тысячах точек рекурсия упирается
    в предел стека Python.
    """
    if len(points) < 3:
        return list(range(len(points)))

    coords = [(float(p.get("lat")), float(p.get("lon"))) for p in points]
    keep = [False] * len(coords)
    keep[0] = keep[-1] = True
    stack = [(0, len(coords) - 1)]
    while stack:
        start, end = stack.pop()
        if end - start < 2:
            continue
        worst, worst_at = 0.0, start
        for i in range(start + 1, end):
            gap = _point_to_line_m(coords[i], coords[start], coords[end])
            if gap > worst:
                worst, worst_at = gap, i
        if worst > epsilon_m:
            keep[worst_at] = True
            stack.append((start, worst_at))
            stack.append((worst_at, end))
    return [i for i, ok in enumerate(keep) if ok]


def _point_to_line_m(p, a, b) -> float:
    """Расстояние от точки до отрезка. Градусы переводим в метры заранее:
    на широте Узбекистана градус долготы почти вдвое короче градуса широты,
    и без поправки прореживание кромсало бы тропу вдоль востока-запада."""
    k = math.cos(math.radians(a[0]))
    px, py = (p[1] - a[1]) * k, p[0] - a[0]
    bx, by = (b[1] - a[1]) * k, b[0] - a[0]
    seg = bx * bx + by * by
    t = 0.0 if seg == 0 else max(0.0, min(1.0, (px * bx + py * by) / seg))
    dx, dy = px - bx * t, py - by * t
    return math.hypot(dx, dy) * 111_320.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
