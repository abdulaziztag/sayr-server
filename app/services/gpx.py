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
    # Первая точка записи — это место, откуда пошли пешком. Именно её человек
    # вбивает в автонавигатор: координаты самого места — это вершина или
    # водопад, и машину туда не подадут
    start_lat: float | None = None
    start_lng: float | None = None


def _points_with_ele(data: bytes) -> list[tuple[float, float, float | None]]:
    """Точки всех треков файла вместе с высотой, где она есть."""
    root = ET.fromstring(data)
    ns = _ns(root)
    out = []
    for trkpt in root.iter(f"{ns}trkpt"):
        ele = trkpt.find(f"{ns}ele")
        out.append((
            float(trkpt.get("lat")),
            float(trkpt.get("lon")),
            float(ele.text) if ele is not None and ele.text else None,
        ))
    return out


def _ascent(points: list[tuple[float, float, float | None]]) -> float:
    """Набор высоты в порядке точек.

    Копим от «якоря»: подъём засчитывается, только когда высота ушла от него
    вверх дальше порога, — так дрожание записи не суммируется. Якорь берём
    с ПЕРВОЙ точки, а не со второй: иначе подъём на стартовом отрезке
    пропадал. На сырых записях с шагом в метр это терялось в шуме, а после
    прореживания отрезки длинные, и потеря становится заметной.

    Вынесено отдельно, чтобы прогонять и по перевёрнутому списку: набор
    несимметричен, и разница двух прогонов — это перепад между концами.
    """
    ascent = 0.0
    anchor: float | None = next((p[2] for p in points if p[2] is not None), None)
    for cur in points[1:]:
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
    return ascent


def track_stats(data: bytes) -> TrackStats:
    """Длина, набор и точка старта по точкам всех треков файла."""
    points = _points_with_ele(data)
    distance = sum(
        haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:])
    )
    ascent = _ascent(points)
    start = points[0] if points else None
    return TrackStats(
        distance_km=round(distance / 1000, 1),
        ascent_m=round(ascent),
        start_lat=round(start[0], 6) if start else None,
        start_lng=round(start[1], 6) if start else None,
    )


def closest_approach_pos(coords: list[tuple[float, float]], lat: float, lng: float) -> float:
    """Где по длине трека он ближе всего подходит к точке: доля от 0 до 1.

    Мерка направления, устойчивая к вранью координат. Сравнивать расстояния
    от концов до цели нельзя: координаты мест взяты из OSM и промахиваются
    на сотни метров поперёк тропы, и тогда «ближний конец» определяется
    ошибкой, а не маршрутом. Доля же от поперечного сноса не двигается вовсе.

    Заодно это единственная мерка, которая видит случай «цель в середине
    маршрута» — там оба конца далеко, и сравнение концов молчит.
    """
    if len(coords) < 2:
        return 0.0
    legs = [haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(coords, coords[1:])]
    total = sum(legs)
    if total <= 0:
        return 0.0
    best_i = min(
        range(len(legs)),
        key=lambda i: _point_to_line_m((lat, lng), coords[i], coords[i + 1]),
    )
    return sum(legs[:best_i]) / total


def recorded_from_target(data: bytes, lat: float, lng: float) -> bool:
    """Записан ли трек ОТ цели вниз, а не к ней.

    Такие записи ломают сразу две вещи: точкой старта становится сама цель
    (и человек вбивает в автонавигатор вершину вместо парковки), а набор
    считается в направлении спуска — у Большого Чимгана выходило 22 метра
    вместо полутора тысяч.

    Три условия разом, и каждое закрывает свой промах:

    - ближайший подход к цели лежит в первых 15 % длины — то есть запись
      начинается у цели, а не приходит к ней;
    - концы разнесены: у кольца оба конца у дороги, переворачивать нечего;
    - идти к цели — значит набирать хотя бы сотню метров.

    Последнее считаем разницей двух прогонов накопителя, а НЕ высотами
    концов. У первой точки трека Большого Чимгана нет тега ele вовсе —
    единственная такая точка на весь каталог, — и проверка по высотам концов
    пропустила бы ровно тот случай, ради которого всё затевалось.
    """
    coords = track_coords(data)
    if len(coords) < 2:
        return False
    legs = [haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(coords, coords[1:])]
    length = sum(legs)
    if length < 200:
        return False
    if haversine_m(*coords[0], *coords[-1]) < max(200.0, 0.05 * length):
        return False
    if closest_approach_pos(coords, lat, lng) > 0.15:
        return False

    points = _points_with_ele(data)
    return _ascent(points[::-1]) - _ascent(points) >= 100


#: Насколько концы записи могут разойтись, чтобы её всё ещё считать
#: полным выходом. Пятнадцать процентов длины, а не пять как у проверки
#: направления: люди дописывают трек не там, где припарковались, и
#: полвонакская тройка водопадов — кольцо, у которого концы разошлись
#: на 1,4 км при длине 12,5. По пятипроцентному порогу оно ушло бы
#: в «в одну сторону», и время удвоилось бы вместо честных пяти с половиной
_CLOSED_ENDS_SHARE = 0.15


def records_full_trip(data: bytes) -> bool:
    """Покрывает ли запись весь выход целиком, а не только путь до цели.

    Вопрос не праздный: время в карточке — это ход ТУДА И ОБРАТНО, а из
    174 треков каталога 76 обрываются на цели. Считать по ним время как
    есть значило бы обещать половину; проверка отделяет одно от другого.

    Полным выход считаем в двух случаях. Либо концы записи сошлись —
    кольцо или доведённое до машины «туда-обратно». Либо вторая половина
    идёт по первой: человек вернулся той же тропой и дописал файл, просто
    остановил запись, не дойдя до парковки.

    Обратное — запись, у которой концы врозь И возврата по своим следам
    нет: она кончается на вершине или у водопада. Для неё время удваивают.
    """
    coords = track_coords(data)
    if len(coords) < 20:
        return True  # мерить нечего — не выдумываем удвоение

    legs = [haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(coords, coords[1:])]
    length = sum(legs)
    if length < 500:
        return True

    if haversine_m(*coords[0], *coords[-1]) < max(200.0, _CLOSED_ENDS_SHARE * length):
        return True
    # Половина, а не 0,65 как у обрезки: там порог осторожный, потому что
    # ошибка режет чужой кусок маршрута. Здесь ошибка всего лишь оставляет
    # время неудвоенным, и планка ниже
    return _retraced_share(coords) > 0.5


def reverse_track(data: bytes) -> bytes:
    """Развернуть запись: точки в каждом сегменте и порядок сегментов.

    Имя файла при этом не меняется намеренно. Клиенты считают набор сами
    по скачанному файлу и ставят флаг «Старт» на его первую точку, поэтому
    чинить надо сам файл; а переименование увело бы в никуда ссылки
    в уже скачанных офлайн-комплектах.
    """
    root = ET.fromstring(data)
    ns = _ns(root)
    for seg in root.iter(f"{ns}trkseg"):
        points = list(seg.findall(f"{ns}trkpt"))
        for point in points:
            seg.remove(point)
        for point in reversed(points):
            seg.append(point)
    for trk in root.iter(f"{ns}trk"):
        segs = list(trk.findall(f"{ns}trkseg"))
        if len(segs) > 1:
            for seg in segs:
                trk.remove(seg)
            for seg in reversed(segs):
                trk.append(seg)
    if ns:
        ET.register_namespace("", ns[1:-1])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


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
