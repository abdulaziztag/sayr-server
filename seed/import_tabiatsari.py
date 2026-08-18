"""Импорт треков и фотографий из tabiatsari.uz в существующие места.

    uv run python -m seed.import_tabiatsari            # показать, что будет
    uv run python -m seed.import_tabiatsari --apply    # выполнить

Слияние по полям, а не перезапись: координаты и высоты источник знает лучше
(его точки — GPS людей на месте, наши — из Wikidata, расхождение медианой
296 м), а названия, тексты, сложность и сезон есть только у нас. Свои фото
и тексты скрипт не трогает никогда — только дополняет.

Что с чем сопоставлено — в data/tabiatsari_map.json. Файл заполняется
человеком: сопоставление по расстоянию ошибается (Урунгач цеплялся к
кишлаку-старту вместо озера), и решать должен глаз, а не порог.
"""

import argparse
import asyncio
import json
import re
from pathlib import Path

from sqlalchemy import select

try:
    from fastapi_storages import StorageFile
except ImportError:  # расположение менялось между версиями пакета
    from fastapi_storages.base import StorageFile

from app.config import GPX_DIR, PHOTOS_DIR
from app.db import SessionLocal
from app.models import Place, PlacePhoto, PlaceTrack, gpx_storage, photo_storage
from app.services.gpx import clean, outbound_only, track_stats
from app.services.images import make_thumbnail

from . import tabiatsari as ts
from .translit import to_cyrillic

DATA_DIR = Path(__file__).resolve().parent / "data"
MAP_FILE = DATA_DIR / "tabiatsari_map.json"

# Подписываем автором, без адреса источника: у снимков и треков есть
# конкретные люди, и в подписи должны стоять они. Автора нет — подписи нет:
# приписывать работу площадке, на которой она просто лежала, неверно

# Больше десятка снимков одного водопада никто не листает, а стопка полароидов
# на деталке и не рассчитана на такое: у Бадака их 34, у Чимгана 22
MAX_PHOTOS = 10

# Многодневные маршруты каталог не описывает: место — это выход на день,
# и нить «выехать — дойти — вернуться засветло» на 40 км не считается.
# Самый длинный из оставленных — Пулатхан, 22 км
MAX_TRACK_KM = 30.0

# Дольше этого — уже не выход на день: формула окна выезда
# (закат − дорога×2 − ход×1,5 − запас) на таком числе уходит в минус
MAX_DAY_HOURS = 14.0


def _declared_elevation(name: str, fallback: int | None) -> int | None:
    """Высота из названия, а не из поля.

    У Манкента в имени 3018, в поле 2946; так же расходятся Коракуш,
    Бабайтаг, Деволи Сурх, Амир Темур. Поле, похоже, снимается с рельефа,
    а имя несёт заявленную высоту вершины — по полю Манкент выпал бы
    из трёхтысячников.
    """
    match = re.search(r"(\d{3,4})\s*m", name)
    return int(match.group(1)) if match else fallback


def _credit(track: dict) -> str:
    """Подпись автора. Автор трека и автор снимков — разные люди:
    у трека «Go on Foot», у фотографий того же места другой человек."""
    source = track.get("source") or {}
    name = (source.get("name") or "").strip()
    # Источник иногда числит автором сам себя: подпись «Tabiat Sari» — это
    # не человек, а площадка, и в графе автора ей делать нечего
    return "" if name.casefold().replace(" ", "") == "tabiatsari" else name


def _photo_credit(media: dict) -> str:
    source = media.get("source") or {}
    name = (source.get("name") or "").strip()
    return f"Фото: {name}" if name else ""


async def run(apply: bool) -> None:
    config = json.loads(MAP_FILE.read_text("utf-8"))
    mapping = config["matches"]
    mapping.pop("_", None)
    names = config.get("track_names", {})
    points = {p["id"]: p for p in ts.points()}

    async with SessionLocal() as session:
        for slug, point_id in mapping.items():
            place = (
                await session.execute(select(Place).where(Place.slug == slug))
            ).scalar_one_or_none()
            if place is None:
                print(f"  ! {slug}: места нет в базе, пропущено")
                continue
            point = points.get(point_id)
            if point is None:
                print(f"  ! {slug}: точки {point_id} нет в выдаче источника")
                continue

            print(f"\n{place.name}  ←  {point['name']}")
            _plan_place(place, point, apply)
            walked = await _plan_tracks(session, place, point_id, names, apply)
            _plan_effort(place, walked, apply)
            await _plan_photos(session, place, point_id, apply)

        if apply:
            await session.commit()
            print("\nГотово.")
        else:
            print("\nЭто был показ без изменений. Выполнить: --apply")


def _plan_place(place: Place, point: dict, apply: bool) -> None:
    lat, lng = round(point["lat"], 6), round(point["lon"], 6)
    if (place.lat, place.lng) != (lat, lng):
        print(f"  координаты {place.lat},{place.lng} → {lat},{lng}")
        if apply:
            place.lat, place.lng = lat, lng

    elevation = _declared_elevation(point["name"], point.get("elevation"))
    # Только если у нас пусто: свою высоту не переписываем, она выверена
    if place.elevation_m is None and elevation:
        print(f"  высота — → {elevation} м")
        if apply:
            place.elevation_m = elevation


async def _plan_tracks(
    session, place: Place, point_id: str, names: dict, apply: bool
) -> list:
    existing = {
        Path(t.gpx_file.name).name: t
        for t in (
            await session.execute(select(PlaceTrack).where(PlaceTrack.place_id == place.id))
        ).scalars()
    }
    walked: list = []
    for order, track in enumerate(ts.tracks(point_id), start=len(existing)):
        url = track.get("gpxFileUrl")
        if not url:
            continue
        name = f"{place.slug}-ts-{url.rsplit('/', 1)[-1]}"
        data = clean(ts.fetch_file(url))
        # Запись «туда-обратно» режем до пути в одну сторону: на карте она
        # рисовалась двойной линией поверх себя. Возврат другой дорогой
        # не трогаем — там своя половина маршрута
        cut = outbound_only(data)
        round_trip = track_stats(data).distance_km
        if cut is not None:
            data = cut
        stats = track_stats(data)
        # Имя из выверенного списка, транслитерация — запасной путь:
        # механическая замена букв даёт «Бобойтог» вместо «Бабайтаг»
        title = names.get(track["id"]) or to_cyrillic(track["name"])
        if stats.distance_km > MAX_TRACK_KM:
            print(f"  ~ пропущен многодневный «{title[:40]}» — {stats.distance_km} км")
            continue
        mark = " (обрезан до пути туда)" if cut is not None else ""
        verb = "обновлён" if name in existing else "+ трек"
        print(
            f"  {verb} «{title[:40]}» {stats.distance_km} км, "
            f"+{stats.ascent_m} м, {len(data) // 1024} КБ{mark}"
        )
        walked.append((round_trip, track))
        if not apply:
            continue
        (GPX_DIR / name).write_bytes(data)
        row = existing.get(name)
        if row is None:
            row = PlaceTrack(
                place_id=place.id,
                gpx_file=StorageFile(name=name, storage=gpx_storage),
                sort_order=order,
            )
            session.add(row)
        row.name = title
        row.gpx_credit = _credit(track)
        row.distance_km = stats.distance_km
        row.ascent_m = stats.ascent_m
    return walked


def _plan_effort(place: Place, walked: list, apply: bool) -> None:
    """Длина, время и набор для наклеек на главной.

    Заполняем только пустое: у девяти мест эти числа выверены руками,
    у тридцати одного их нет вовсе, и карточка стоит без наклеек.

    Длина — ПОЛНАЯ, вместе с возвращением, даже если трек обрезан до пути
    туда: по ней считается окно выезда, и ходить человек будет в обе
    стороны. Время берём из вилки источника серединой — у него она своя
    на каждый маршрут, а наша формула сверху добавляет полуторный запас.
    """
    if not walked:
        return
    # Самый длинный из маршрутов места: наклейка обещает выход целиком,
    # а не самую короткую его версию
    km, track = max(walked, key=lambda pair: pair[0])

    if place.distance_km is None:
        print(f"  длина — → {km} км")
        if apply:
            place.distance_km = km

    if place.duration_hours is None:
        low, high = track.get("timeFrom"), track.get("timeTo")
        if low and high:
            hours = round((low + high) / 2 / 60, 1)
            # Вилки у источника местами дикие: у Бадака 8–31 час, и середина
            # даёт 19,7 — это ночёвка, а не выход на день, и окно выезда
            # на таком числе не считается вовсе. Берём нижний край как
            # реальное ходовое время; если и он за пределом — молчим,
            # пусть человек проставит руками
            if hours > MAX_DAY_HOURS:
                hours = round(low / 60, 1)
            if hours > MAX_DAY_HOURS:
                print(f"  ! время не проставлено: у них {low // 60}–{high // 60} ч, это не день")
                hours = None
            if hours is not None:
                print(f"  время — → {hours} ч (у них {low // 60}–{high // 60} ч)")
                if apply:
                    place.duration_hours = hours

    if place.elevation_gain_m is None and track.get("elevationGain"):
        gain = int(track["elevationGain"])
        print(f"  набор — → {gain} м")
        if apply:
            place.elevation_gain_m = gain


async def _plan_photos(session, place: Place, point_id: str, apply: bool) -> None:
    existing = {
        Path(p.file.name).name
        for p in (
            await session.execute(select(PlacePhoto).where(PlacePhoto.place_id == place.id))
        ).scalars()
    }
    medias = [m for m in (ts.point(point_id).get("medias") or []) if m.get("status") == "COMPLETED"]
    room = max(0, MAX_PHOTOS - len(existing))
    skipped = len(medias) - room
    added = 0
    for order, media in enumerate(medias[:room], start=len(existing)):
        # Берём large, а не оригинал: 1200 px при 300 КБ против оригиналов
        # местами в 8448 px и 41 МБ. По всем 111 снимкам это 38 МБ вместо
        # 1,4 ГБ — на машине, где живут ещё шестнадцать чужих сайтов,
        # разница решающая, а для карточки и полного экрана телефона
        # 1200 px хватает с запасом
        url = media.get("large") or media.get("originalUrl")
        if not url:
            continue
        name = f"{place.slug}-ts-{url.rsplit('/', 1)[-1]}"
        if name in existing:
            continue
        added += 1
        if not apply:
            continue
        (PHOTOS_DIR / name).write_bytes(ts.fetch_file(url))
        make_thumbnail(name)
        session.add(
            PlacePhoto(
                place_id=place.id,
                file=StorageFile(name=name, storage=photo_storage),
                credit=_photo_credit(media),
                sort_order=order,
            )
        )
    if added:
        tail = f", ещё {skipped} не берём" if skipped > 0 else ""
        print(f"  + фотографий: {added} (свои остаются{tail})")

    # Обложкой становится снимок хайкера: на Wikimedia Commons для этих мест
    # часто лежит общий вид издалека или вовсе соседняя долина, а у источника
    # кадр с самой тропы. Порядок задаём явно, а не сдвигом, иначе повторный
    # запуск каждый раз тасовал бы стопку заново
    if not apply:
        return
    rows = (
        (await session.execute(select(PlacePhoto).where(PlacePhoto.place_id == place.id)))
        .scalars()
        .all()
    )
    imported = [p for p in rows if "-ts-" in Path(p.file.name).name]
    own = [p for p in rows if "-ts-" not in Path(p.file.name).name]
    for order, row in enumerate(imported + own):
        row.sort_order = order


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # Показ по умолчанию: скрипт правит боевой каталог, и случайный запуск
    # не должен менять ничего молча
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    asyncio.run(run(parser.parse_args().apply))


if __name__ == "__main__":
    main()
