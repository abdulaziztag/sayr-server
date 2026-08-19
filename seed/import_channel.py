"""Импорт GPX-треков из телеграм-канала владельца в существующие места.

    uv run python -m seed.import_channel                       # показать, что будет
    uv run python -m seed.import_channel --apply               # выполнить
    uv run python -m seed.import_channel --credit "Имя"        # подпись автора

Источник — выгрузка канала из Telegram Desktop: 178 файлов GPX, KML и KMZ.
Разбор выгрузки сделан заранее и лежит в data/channel_parsed.json (что за
файл, длина, ближайшее место), тексты постов — в data/channel_posts.json.
Сами файлы в репозиторий не тащим: путь к выгрузке передаётся ключом
--files, а на сервер импорт не переносится, пока файлы не выверены.

Правила отбора — те же, что у «Горца» и tabiatsari:

- берём только пешие треки, начинающиеся ближе километра к месту: маршрут
  принадлежит месту, только если стартует или кончается рядом с ним;
- точки, автомобильные записи и архивы пропускаем с печатью причины;
- запись «туда-обратно» режем до пути в одну сторону, потом чистим от
  времени и прореживаем — публикуется очищенный файл, не оригинал;
- длиннее 30 км после обрезки — многодневка, в каталог дневных выходов
  не идёт;
- дубли по геометрии схлопываем: та же тропа уже лежит у места — новый
  файл не нужен;
- не больше трёх треков на место ИТОГО, короткие вперёд — короткий
  вариант ценнее для дневного выхода.

Новые места скрипт НЕ заводит: появление места в каталоге — отдельное
решение человека, а не побочный эффект импорта.
"""

import argparse
import asyncio
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

try:
    from fastapi_storages import StorageFile
except ImportError:  # расположение менялось между версиями пакета
    from fastapi_storages.base import StorageFile

from app.config import GPX_DIR
from app.db import SessionLocal
from app.models import Place, PlaceTrack, gpx_storage
from app.services.gpx import (
    TrackStats,
    clean,
    haversine_m,
    outbound_only,
    track_coords,
    track_stats,
)

from .estimate_duration import estimate

DATA_DIR = Path(__file__).resolve().parent / "data"
PARSED_FILE = DATA_DIR / "channel_parsed.json"
POSTS_FILE = DATA_DIR / "channel_posts.json"

# Выгрузка Telegram Desktop живёт вне репозитория: гигабайты сырых записей
# на сервер не тащат, а импорт запускается с машины, где выгрузка есть
DEFAULT_FILES_DIR = Path.home() / "Downloads/Telegram Desktop/ChatExport_2026-08-19/files"

# Дальше километра — трек не «этого места»: сопоставление по расстоянию
# уже ошибалось (Урунгач цеплялся к кишлаку-старту), и километр — порог,
# за которым решать должен человек, а не скрипт
MAX_DIST_M = 1000

# Многодневные маршруты каталог не описывает: место — это выход на день,
# и нить «выехать — дойти — вернуться засветло» на 40 км не считается
MAX_TRACK_KM = 30.0

# Дольше этого — уже не выход на день: формула окна выезда
# (закат − дорога×2 − ход×1,5 − запас) на таком числе уходит в минус
MAX_DAY_HOURS = 14.0

# Не больше трёх на место, вместе с уже лежащими: человек выбирает маршрут
# по имени и длине, а стопка из пяти вариантов — это уже не выбор, а разбор
MAX_TRACKS_PER_PLACE = 3

# Дубль по геометрии: доля точек нового трека ближе 60 м к уже лежащему.
# Порог тот же, что при отборе «Горца»: больше 0,7 — та же тропа
DUP_RADIUS_M = 60.0
DUP_SHARE = 0.7


@dataclass
class Candidate:
    """Один прошедший отбор файл: очищенный трек и его числа."""

    src: str  # имя файла в выгрузке
    gpx_name: str  # имя в GPX_DIR
    title: str
    by_place: bool  # имя дано по месту, а не из файла
    data: bytes  # очищенное содержимое — публикуется оно, не оригинал
    stats: TrackStats  # после обрезки и чистки
    round_trip_km: float  # длина ДО обрезки: столько человек пройдёт ногами
    cut: bool  # запись «туда-обратно» обрезана до пути туда


def resolve_export_path(files_dir: Path, name: str) -> Path | None:
    """Файл выгрузки с поправкой на юникод.

    macOS хранит имена разложенными (NFD), и «ё» с «й» там — буква плюс
    диакритика. На линуксовом сервере такой файл не находится по составному
    (NFC) имени из плана. Пробуем обе формы.
    """
    import unicodedata

    for form in ("NFC", "NFD"):
        path = files_dir / unicodedata.normalize(form, name)
        if path.exists():
            return path
    return None


def _kml_to_gpx(data: bytes) -> bytes | None:
    """KML → GPX: LineString → trkpt. Точечные Placemark не переносим —
    точки в каталог не идут, их отсеял разбор выгрузки."""
    root = ET.fromstring(data)
    # Пространство имён у KML гуляет (2.0, 2.2, gx) — ищем по локальному
    # имени тега, а не по жёстко зашитому namespace
    lines = [el for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "LineString"]
    gpx = ET.Element(
        "gpx",
        {"version": "1.1", "creator": "sayr", "xmlns": "http://www.topografix.com/GPX/1/1"},
    )
    trk = ET.SubElement(gpx, "trk")
    total = 0
    for line in lines:
        coords = next(
            (el for el in line.iter() if el.tag.rsplit("}", 1)[-1] == "coordinates"), None
        )
        if coords is None or not (coords.text or "").strip():
            continue
        seg = ET.SubElement(trk, "trkseg")
        for tuple_ in coords.text.split():
            parts = tuple_.split(",")
            if len(parts) < 2:
                continue
            lon, lat = float(parts[0]), float(parts[1])
            pt = ET.SubElement(seg, "trkpt", {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}"})
            # Высота в KML — третье число кортежа; без неё набор не посчитать,
            # но трек всё равно годен: длина и линия на карте остаются.
            # Ноль — не высота: нарисованные в Google Earth линии клеятся
            # к земле и несут 0 на каждой точке, а публиковать «0 м» в горах —
            # врать в файле
            try:
                ele = float(parts[2]) if len(parts) > 2 else 0.0
            except ValueError:
                ele = 0.0
            if ele:
                ET.SubElement(pt, "ele").text = parts[2]
            total += 1
    if total < 2:
        return None
    return ET.tostring(gpx, encoding="utf-8", xml_declaration=True)


def _load_gpx(path: Path) -> bytes | None:
    """Файл выгрузки → GPX-байты. KMZ — это zip с KML внутри."""
    data = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".kmz":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            kml_name = next((n for n in zf.namelist() if n.lower().endswith(".kml")), None)
            if kml_name is None:
                return None
            data = zf.read(kml_name)
        suffix = ".kml"
    if suffix == ".kml":
        return _kml_to_gpx(data)
    return data


# Телеграмные id и прочие безымянные файлы: цифры, подчёркивания, скобки
_DIGITS_ONLY = re.compile(r"[\d_\s().\-]+")
# Дата-префикс «16.05.2021_», «2025-01-19_», «2022.06.25_»
_DATE_PREFIX = re.compile(r"^\d{2,4}[.\-]\d{2}[.\-]\d{2,4}\s+")
# Хвост регистратора: дата, за ней бывают время и счётчик записи —
# «Азадбаш 08.12.2022», «Деволисурх общий 2022-09-26 10-26-32»,
# «Манкент 2022-09-10 09-49 002». Просто числа в конце не трогаем:
# они могут быть высотой вершины
_DATE_SUFFIX = re.compile(
    r"\s+\d{2,4}[.\-]\d{2}[.\-]\d{2,4}(\s+\d{2}[-:]\d{2}([-:]\d{2})?)?(\s+\d{1,4})?$"
)
# « (1)» — суффикс повторной загрузки из Telegram, к маршруту отношения не имеет
_COPY_SUFFIX = re.compile(r"\s*\(\d+\)$")


def _title(filename: str, place_name: str) -> tuple[str, bool]:
    """Имя трека из имени файла. Файлы из одних цифр (телеграмные id)
    называем по месту: цифры человеку в выборе маршрута не говорят ничего."""
    stem = Path(filename).stem
    if _DIGITS_ONLY.fullmatch(stem):
        return f"{place_name} (из канала)", True
    stem = _COPY_SUFFIX.sub("", stem)
    stem = re.sub(r"\s+", " ", stem.replace("_", " ")).strip()
    stem = _DATE_PREFIX.sub("", stem)
    stem = _DATE_SUFFIX.sub("", stem)
    return stem[:200], False


def _densify(coords: list, step_m: float = 25.0) -> list:
    """Дорисовать линию промежуточными точками с шагом не длиннее step_m.

    Лежащие в GPX_DIR треки прорежены, и на прямом участке соседние точки
    расходятся на сотни метров (см. distance_to_track_m в app/services/gpx.py).
    Сравнение «точка к точке» на 60 м сказало бы про такой участок «другая
    тропа» — и дубль той же тропы прошёл бы в каталог.
    """
    if len(coords) < 2:
        return list(coords)
    out: list = []
    for a, b in zip(coords, coords[1:]):
        out.append(a)
        gap = haversine_m(*a, *b)
        for k in range(1, int(gap // step_m) + 1):
            t = k * step_m / gap
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    out.append(coords[-1])
    return out


def _covered_share(new: list, base: list) -> float:
    """Какая доля точек нового трека проходит по уже лежащему.

    Сетка вместо перебора всех пар — как в _retraced_share из
    app/services/gpx.py: на десятках тысяч точек квадратичное сравнение
    считалось бы минутами. Лежащий трек перед этим дорисовываем (_densify):
    сетке нужна его линия, а не редкие вершины после прореживания.
    """
    sample = new[:: max(1, len(new) // 2000)]
    if not sample or not base:
        return 0.0
    grid: dict[tuple[float, float], list] = {}
    for point in _densify(base):
        grid.setdefault((round(point[0], 3), round(point[1], 3)), []).append(point)
    hits = 0
    for point in sample:
        neighbours = [
            other
            for dx in (-0.001, 0, 0.001)
            for dy in (-0.001, 0, 0.001)
            for other in grid.get(
                (round(point[0] + dx, 3), round(point[1] + dy, 3)), []
            )
        ]
        if any(haversine_m(*point, *other) < DUP_RADIUS_M for other in neighbours):
            hits += 1
    return hits / len(sample)


def _post_caption(posts: dict, pid: str | None) -> str:
    text = (posts.get(pid or "", {}).get("text") or "").strip().splitlines()
    return text[0][:60] if text else ""


async def run(apply: bool, credit: str, files_dir: Path) -> None:
    parsed = json.loads(PARSED_FILE.read_text("utf-8"))
    posts = json.loads(POSTS_FILE.read_text("utf-8"))

    # Отсев до базы: что не пеший трек у места — не кандидат вовсе
    by_slug: dict[str, list[dict]] = {}
    print("Не берём из выгрузки:")
    for entry in parsed:
        kind = entry.get("kind", "?")
        if kind != "трек":
            reason = {
                "точка": "точка, не маршрут",
                "авто": f"автомобильная запись, {entry.get('km', '?')} км",
                "zip": "архив, не трек",
                "empty": "пустой файл",
            }.get(kind, kind)
            print(f"  ~ {entry['f']}: {reason}")
            continue
        dist = entry.get("dist_m")
        if dist is None or dist >= MAX_DIST_M:
            near = entry.get("near", "?")
            print(f"  ~ {entry['f']}: далеко от места — {dist} м до «{near}»")
            continue
        by_slug.setdefault(entry["near_slug"], []).append(entry)

    added = skipped = filled = 0

    async with SessionLocal() as session:
        for slug in sorted(by_slug):
            entries = by_slug[slug]
            place = (
                await session.execute(select(Place).where(Place.slug == slug))
            ).scalar_one_or_none()
            if place is None:
                print(f"\n! {slug}: места нет в базе — мимо {len(entries)} трек(а)")
                skipped += len(entries)
                continue

            existing = (
                (
                    await session.execute(
                        select(PlaceTrack).where(PlaceTrack.place_id == place.id)
                    )
                )
                .scalars()
                .all()
            )
            existing_names = {Path(t.gpx_file.name).name for t in existing}
            print(f"\n{place.name}")

            # Геометрия уже лежащих треков — для дедупа. Файла может не быть
            # на этой машине (база локальная, медиа на сервере) — тогда
            # сравнить не с чем, честно предупреждаем
            base_coords: list[list] = []
            for track in existing:
                path = GPX_DIR / Path(track.gpx_file.name).name
                if path.exists():
                    base_coords.append(track_coords(path.read_bytes()))
                else:
                    print(f"  ! нет файла существующего трека {path.name} — дедуп без него")

            candidates: list[Candidate] = []
            planned_names: set[str] = set()
            for entry in entries:
                src = resolve_export_path(files_dir, entry["f"])
                if src is None:
                    print(f"  ! нет файла {entry['f']}")
                    continue
                # Имя в GPX_DIR — из исходного файла, а не из порядка добавления:
                # повторный запуск должен узнавать уже загруженное, даже если
                # между запусками у места появились другие треки
                digest = hashlib.md5(entry["f"].encode()).hexdigest()[:6]
                gpx_name = f"{place.slug}-channel-{digest}.gpx"
                title, by_place = _title(entry["f"], place.name)
                if gpx_name in existing_names:
                    print(f"  «{title[:40]}» уже есть")
                    skipped += 1
                    continue
                # Короткий хеш двух разных файлов совпал — второй затёр бы
                # первый в GPX_DIR, а обе записи глядели бы в один файл
                if gpx_name in planned_names:
                    print(f"  ! совпал хеш имени у {entry['f']} — пропущен, добавить руками")
                    skipped += 1
                    continue
                planned_names.add(gpx_name)
                try:
                    data = _load_gpx(src)
                except (ET.ParseError, zipfile.BadZipFile, ValueError) as err:
                    print(f"  ! не разобрать {entry['f']}: {err}")
                    continue
                if data is None:
                    print(f"  ! в {entry['f']} нет линии маршрута")
                    continue
                round_trip_km = track_stats(data).distance_km
                cut = outbound_only(data)
                if cut is not None:
                    data = cut
                data = clean(data)
                stats = track_stats(data)
                if stats.distance_km > MAX_TRACK_KM:
                    print(
                        f"  ~ пропущен многодневный «{title[:40]}» — {stats.distance_km} км"
                    )
                    skipped += 1
                    continue
                candidates.append(
                    Candidate(
                        src=entry["f"],
                        gpx_name=gpx_name,
                        title=title,
                        by_place=by_place,
                        data=data,
                        stats=stats,
                        round_trip_km=round_trip_km,
                        cut=cut is not None,
                    )
                )

            # Короткие вперёд: короткий вариант ценнее для дневного выхода,
            # и при лимите в три места достаётся именно ему
            candidates.sort(key=lambda c: (c.stats.distance_km, c.src))
            room = MAX_TRACKS_PER_PLACE - len(existing)
            accepted: list[Candidate] = []
            used_titles = {t.name for t in existing}
            for cand in candidates:
                # Имя занято чужим файлом — затирать нельзя: в GPX_DIR лежат
                # и треки других импортов, и загруженное через админку.
                # Тот же байт в байт — наш же след прерванного --apply,
                # его переписать безопасно
                target = GPX_DIR / cand.gpx_name
                if target.exists() and target.read_bytes() != cand.data:
                    print(
                        f"  ! файл {cand.gpx_name} уже лежит в GPX_DIR с другим "
                        f"содержимым — «{cand.title[:40]}» пропущен"
                    )
                    skipped += 1
                    continue
                coords = track_coords(cand.data)
                share = max(
                    (_covered_share(coords, base) for base in base_coords),
                    default=0.0,
                )
                if share > DUP_SHARE:
                    print(
                        f"  ~ пропущен «{cand.title[:40]}» — та же тропа, "
                        f"что уже лежит ({share:.0%} точек рядом)"
                    )
                    skipped += 1
                    continue
                if len(accepted) >= room:
                    print(
                        f"  ~ пропущен «{cand.title[:40]}» {cand.stats.distance_km} км — "
                        f"у места уже {MAX_TRACKS_PER_PLACE} трека"
                    )
                    skipped += 1
                    continue
                # Два безымянных файла у одного места дали бы два одинаковых
                # «Имя (из канала)» — а выбирают маршрут по имени
                if cand.title in used_titles:
                    cand.title = f"{cand.title} {len(used_titles) + 1}"
                used_titles.add(cand.title)
                accepted.append(cand)
                # Принятый становится базой для следующих: два свежих файла
                # одной тропы — тоже дубль, даже если у места было пусто
                base_coords.append(coords)

                pid = next(e.get("pid") for e in entries if e["f"] == cand.src)
                caption = _post_caption(posts, pid) if cand.by_place else ""
                tail = " (обрезан до пути туда)" if cand.cut else ""
                tail += f" — пост: «{caption}»" if caption else ""
                print(
                    f"  + трек «{cand.title[:40]}» {cand.stats.distance_km} км, "
                    f"+{cand.stats.ascent_m} м, {len(cand.data) // 1024} КБ{tail}"
                )
                added += 1
                if not apply:
                    continue
                (GPX_DIR / cand.gpx_name).write_bytes(cand.data)
                session.add(
                    PlaceTrack(
                        place_id=place.id,
                        gpx_file=StorageFile(name=cand.gpx_name, storage=gpx_storage),
                        name=cand.title,
                        gpx_credit=credit,
                        distance_km=cand.stats.distance_km,
                        ascent_m=cand.stats.ascent_m,
                        start_lat=cand.stats.start_lat,
                        start_lng=cand.stats.start_lng,
                        sort_order=len(existing) + len(accepted) - 1,
                    )
                )

            filled += _fill_effort(place, accepted, apply)

        if apply:
            await session.commit()
            print(f"\nГотово: добавлено {added}, пропущено {skipped}, дозаполнено мест {filled}.")
        else:
            print(
                f"\nПоказ без изменений: добавится {added}, пропущено {skipped}, "
                f"дозаполнится мест {filled}. Выполнить: --apply"
            )


def _fill_effort(place: Place, accepted: list[Candidate], apply: bool) -> int:
    """Длина, время и набор для наклеек на главной — по самому короткому
    из добавленных дневных треков: наклейка зовёт на базовый выход, а не
    на самый амбициозный вариант.

    Заполняем только пустое: где числа выверены руками или пришли из
    tabiatsari, они точнее наших. Длина — ПОЛНАЯ, до обрезки «туда-обратно»:
    по ней считается окно выезда, и ходить человек будет в обе стороны.
    """
    if not accepted:
        return 0
    # Короткий — по пройденному ногами (туда-обратно), а не по длине файла:
    # обрезанная до пути туда восьмёрка километров — это 16 км ходьбы,
    # и рядом с необрезанным десятикилометровым кольцом она не короче
    shortest = min(accepted, key=lambda c: c.round_trip_km)
    touched = 0

    if place.distance_km is None:
        print(f"  длина — → {shortest.round_trip_km} км")
        touched = 1
        if apply:
            place.distance_km = shortest.round_trip_km

    if place.duration_hours is None:
        hours = estimate(shortest.round_trip_km, shortest.stats.ascent_m)
        if hours > MAX_DAY_HOURS:
            # За потолком дня формула окна выезда уходит в минус — число
            # не ставим, пусть человек решает сам
            print(f"  ! время не проставлено: по Нейсмиту {hours} ч, это не день")
        else:
            print(f"  время — → {hours} ч (Нейсмит)")
            touched = 1
            if apply:
                place.duration_hours = hours

    if place.elevation_gain_m is None and shortest.stats.ascent_m:
        print(f"  набор — → {shortest.stats.ascent_m} м")
        touched = 1
        if apply:
            place.elevation_gain_m = shortest.stats.ascent_m

    return touched


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # Показ по умолчанию: скрипт правит боевой каталог, и случайный запуск
    # не должен менять ничего молча
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    parser.add_argument(
        "--credit", default="", help="подпись автора треков (PlaceTrack.gpx_credit)"
    )
    parser.add_argument(
        "--files",
        type=Path,
        default=DEFAULT_FILES_DIR,
        help="каталог files из выгрузки Telegram Desktop",
    )
    args = parser.parse_args()
    asyncio.run(run(args.apply, args.credit, args.files))


if __name__ == "__main__":
    main()
