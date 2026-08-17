"""Статистика GPX-трека: длина и набор высоты.

Считается на сервере при сохранении — в сиде и в админке. Клиент ничего
не считает и качает только файл выбранного маршрута: файлы, загруженные
в админку сырыми из записывающих приложений, весят мегабайты, и качать
их все, чтобы показать «8,2 км · +1571 м» в выборе маршрута, нельзя.
"""

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass

# Пространство имён GPX 1.1. Но в переписке встречаются и файлы 1.0
# (их пишут «Советские военные карты»), и вовсе без пространства имён —
# у них другой тег, и жёстко зашитая версия молча давала нулевую
# статистику и нетронутый на 4,6 МБ файл
_NS = "{http://www.topografix.com/GPX/1/1}"


def _ns(root: ET.Element) -> str:
    """Пространство имён этого файла — по корневому тегу."""
    return root.tag[: root.tag.index("}") + 1] if "}" in root.tag else ""

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
    ns = _ns(root)
    points: list[tuple[float, float, float | None]] = []
    for trkpt in root.iter(f"{ns}trkpt"):
        ele = trkpt.find(f"{ns}ele")
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
        distance += haversine_m(prev[0], prev[1], cur[0], cur[1])
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
    ns = _ns(root)
    for seg in root.iter(f"{ns}trkseg"):
        points = list(seg.findall(f"{ns}trkpt"))
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
            if child.tag == f"{ns}time":
                node.remove(child)
    if ns:
        ET.register_namespace("", ns[1:-1])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def outbound_only(data: bytes) -> bytes | None:
    """Обрезать запись «туда-обратно» до пути в одну сторону.

    Люди записывают трек целиком, вместе с возвращением по той же тропе.
    На карте это двойная линия поверх себя, а в файле — лишняя половина.
    Возвращаем None, если возврат идёт другой дорогой: там вторая половина
    несёт свой маршрут, и резать её нельзя.

    Точка разворота — самая дальняя от старта: для выхода на вершину это
    и есть вершина.
    """
    root = ET.fromstring(data)
    ns = _ns(root)
    seg = root.find(f".//{ns}trkseg")
    if seg is None:
        return None
    points = list(seg.findall(f"{ns}trkpt"))
    if len(points) < 20:
        return None

    coords = [(float(p.get("lat")), float(p.get("lon"))) for p in points]
    # Две трети — порог осторожности, а не вкуса. На 60% оказался маршрут
    # через две вершины, который сам источник считает кольцом: обрезка по
    # дальней точке отбросила бы вторую вершину. Выше этой планки геометрия
    # совпадает с разметкой источника на всех треках, что есть
    if _retraced_share(coords) < 0.65:
        return None

    far = max(range(len(coords)), key=lambda i: haversine_m(*coords[0], *coords[i]))
    # Разворот у самого края записи — значит это не «туда-обратно»,
    # а петля, случайно замкнувшаяся рядом со стартом
    if not 0.2 < far / len(coords) < 0.8:
        return None

    for point in points[far + 1 :]:
        seg.remove(point)
    if ns:
        ET.register_namespace("", ns[1:-1])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def track_coords(data: bytes) -> list[tuple[float, float]]:
    """Точки всех треков файла — только широта и долгота."""
    root = ET.fromstring(data)
    ns = _ns(root)
    return [(float(p.get("lat")), float(p.get("lon"))) for p in root.iter(f"{ns}trkpt")]


def bbox(coords: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    """Охватывающий прямоугольник: юг, запад, север, восток."""
    lats = [c[0] for c in coords]
    lngs = [c[1] for c in coords]
    return min(lats), min(lngs), max(lats), max(lngs)


def distance_to_track_m(coords: list[tuple[float, float]], lat: float, lng: float) -> float:
    """Насколько близко трек подходит к точке.

    Именно до линии, а не до ближайшей записанной точки: после прореживания
    на прямом участке соседние точки расходятся на сотни метров, и мерка
    по точкам сказала бы «далеко» про место, мимо которого тропа проходит
    вплотную.
    """
    if not coords:
        return math.inf
    if len(coords) == 1:
        return haversine_m(*coords[0], lat, lng)
    return min(_point_to_line_m((lat, lng), a, b) for a, b in zip(coords, coords[1:]))


def _retraced_share(coords: list[tuple[float, float]]) -> float:
    """Какая доля второй половины пути проходит по первой.

    Сетка вместо перебора всех пар: на десятках тысяч точек квадратичное
    сравнение считалось бы минутами.
    """
    sample = coords[:: max(1, len(coords) // 2000)]
    half = len(sample) // 2
    if half < 5:
        return 0.0

    grid: dict[tuple[float, float], list] = {}
    for point in sample[:half]:
        grid.setdefault((round(point[0], 3), round(point[1], 3)), []).append(point)

    hits = 0
    for point in sample[half:]:
        key = (round(point[0], 3), round(point[1], 3))
        neighbours = [
            other
            for dx in (-0.001, 0, 0.001)
            for dy in (-0.001, 0, 0.001)
            for other in grid.get((round(key[0] + dx, 3), round(key[1] + dy, 3)), [])
        ]
        if any(haversine_m(*point, *other) < 40 for other in neighbours):
            hits += 1
    return hits / (len(sample) - half)


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


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
