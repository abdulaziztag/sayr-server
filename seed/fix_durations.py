"""Пересчитать время хода по трекам: `uv run python -m seed.fix_durations`.

Зачем. Поле `duration_hours` заполняли семь разных мест, и каждое клало
свой смысл. Больше половины каталога получило ВИЛКУ ВРЕМЕНИ С TABIATSARI —
чужое время на чужой маршрут — и легло рядом с нашей собственной длиной,
посчитанной по нашему же треку. В одной строке оказались время «туда
и обратно» и длина «в одну сторону».

Что видно на числах: Амир Темур обещал 2,5 часа на 29,5 км с набором
3458 м, Каракуш — 3,5 часа на 2269 м набора, а семь групп мест делили
один и тот же триплет цифр, потому что у tabiatsari один маршрут привязан
к нескольким нашим точкам.

Решение владельца продукта (24 августа 2026): время в карточке — это
ЧИСТЫЙ ХОД ТУДА И ОБРАТНО, без дороги из Ташкента. Дорога у нас и так
показана отдельной станцией «В дороге», и приложение умножает её на два
само (TripPlan.swift:62).

Как считаем. Берём главный трек места, меряем его длину и набор,
спрашиваем `gpx.records_full_trip`, покрывает ли запись весь выход,
и если нет — удваиваем длину. Дальше формула Нейсмита из
`seed/estimate_duration.py`, чтобы во всём проекте она была одна.

Набор при удвоении НЕ удваиваем: обратно идут вниз.

Что не трогаем — три списка ниже, каждый со своей причиной.
"""

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.config import GPX_DIR
from app.models import Place
from app.services import gpx

from .estimate_duration import estimate

#: Числа, которые вбил человек, а не скрипт (seed/data/places.json).
#: Формула Нейсмита их не переспорит: на Большом Чимгане она даёт 7 часов
#: против выставленных 12, и права здесь скорее не она — четыре километра
#: в час по осыпи на наборе в полторы тысячи метров не ходят.
BY_HAND = {
    "bolshoy-chimgan",
    "gulkam-tesniny",
    "nuratau-sentob",
    "urungach-lakes",
    "aktash-waterfalls",
    "maly-chimgan",
    "beldersay",
    "yangiabad-waterfalls",
    "sarmishsay-petroglyphs",
    "hazrat-daud-cave",
    "sangardak-waterfall",
}

#: Места, где трек и поля карточки описывают РАЗНЫЕ маршруты. Пока не решено,
#: какой из них главный, считать нечего: любое число будет спорить с соседним.
CONFLICTING_ROUTE = {
    # У места два трека: кольцо на 10 км к водопаду и маршрут на 16,6 км
    # с вершиной. В карточке лежат цифры второго, а показывается первый
    "paltau-waterfall",
    # Привязан трек Нурекаты — место в 1,3 км от линии
    "beldersay",
    # Привязана Кумгаза: 10,3 км вместо 2,6
    "hazrat-daud-cave",
}

#: Треки без высот: набор в файле нулевой, и расчёт даст заниженное время.
#: Лучше оставить как есть, чем обещать меньше настоящего.
NO_ELEVATION = {"achikul-lake", "kyzylgaza", "ettikiz-amat-gor"}

#: Дальше этого дневной выход не бывает: светового дня не хватит.
#: Такому месту нужен признак ночёвки, а не время, ломающее окно выезда.
MAX_DAY_HOURS = 14.0

#: Меньше этого не правим — полчаса разницы не стоят движения данных
MIN_DELTA_HOURS = 0.5


async def run(apply: bool, only: set[str]) -> None:
    async with SessionLocal() as session:
        places = (
            (
                await session.execute(
                    select(Place)
                    .where(Place.is_published.is_(True))
                    .options(selectinload(Place.tracks))
                    .order_by(Place.slug)
                )
            )
            .scalars()
            .all()
        )

        changed: list[tuple[Place, float, float, str]] = []
        overnight: list[tuple[Place, float]] = []
        held: list[tuple[str, str]] = []

        for place in places:
            if only and place.slug not in only:
                continue
            if place.slug in BY_HAND:
                held.append((place.slug, "число выставлено руками"))
                continue
            if place.slug in CONFLICTING_ROUTE:
                held.append((place.slug, "трек и карточка про разные маршруты"))
                continue
            if place.slug in NO_ELEVATION:
                held.append((place.slug, "в треке нет высот, расчёт занизит"))
                continue

            track = min(place.tracks, key=lambda t: t.sort_order, default=None)
            if track is None or not track.gpx_file:
                held.append((place.slug, "нет трека"))
                continue

            path = GPX_DIR / Path(str(track.gpx_file)).name
            if not path.exists():
                held.append((place.slug, f"файл не найден: {path.name}"))
                continue

            data = path.read_bytes()
            stats = gpx.track_stats(data)
            full = gpx.records_full_trip(data)
            km = stats.distance_km * (1 if full else 2)
            hours = estimate(km, stats.ascent_m)
            shape = "полный путь" if full else "в одну сторону ×2"

            was = place.duration_hours
            if was is not None and abs(hours - was) < MIN_DELTA_HOURS:
                continue
            if hours > MAX_DAY_HOURS:
                overnight.append((place, hours))
                continue
            changed.append((place, was, hours, shape))

        _report(changed, overnight, held)

        if apply:
            for place, _, hours, _ in changed:
                place.duration_hours = hours
            await session.commit()
            print(f"\nГотово: обновлено {len(changed)}.")
        else:
            print("\nЭто прогон вхолостую. Записать — с ключом --apply.")


def _report(changed, overnight, held) -> None:
    print(f"\n  ПРАВКИ ({len(changed)})\n")
    for place, was, hours, shape in sorted(changed, key=lambda r: r[2] - (r[1] or 0)):
        arrow = "↑" if was is None or hours > was else "↓"
        old = "пусто" if was is None else f"{was:g} ч"
        print(f"  {arrow} {place.slug:28} {old:>8} → {hours:g} ч   {shape}")

    if overnight:
        print(f"\n  ВЫШЕ ДНЕВНОГО ПОТОЛКА — не трогаю ({len(overnight)})")
        print("  Такому маршруту нужен признак ночёвки, иначе окно выезда")
        print("  уедет в минус. Решение за владельцем.\n")
        for place, hours in sorted(overnight, key=lambda r: -r[1]):
            mark = place.overnight.value if place.overnight else "БЕЗ ПРИЗНАКА"
            was = "пусто" if place.duration_hours is None else f"{place.duration_hours:g} ч"
            print(f"    {place.slug:28} {was:>8} → расчёт {hours:g} ч   {mark}")

    if held:
        print(f"\n  ПРОПУЩЕНО ({len(held)})")
        reasons: dict[str, list[str]] = {}
        for slug, why in held:
            reasons.setdefault(why, []).append(slug)
        for why, slugs in sorted(reasons.items(), key=lambda r: -len(r[1])):
            print(f"    {why} — {len(slugs)}: {', '.join(sorted(slugs)[:6])}"
                  + (" …" if len(slugs) > 6 else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="записать в базу")
    parser.add_argument("--places", nargs="*", default=[], help="только эти слаги")
    args = parser.parse_args()
    asyncio.run(run(args.apply, set(args.places)))


if __name__ == "__main__":
    main()
