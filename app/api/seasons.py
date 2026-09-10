"""Игра «когда сюда идти»: сезоны местам собираем толпой.

Каталог собран руками, и сезон в нём — самое дорогое поле: чтобы его
проставить, надо там побывать. Поэтому вопрос задаётся тем, кто бывал,
и задаётся так, чтобы ответ стоил три секунды: карточка места, круг года,
два маркера. К месту сезон применяется не отсюда, а после одобрения
проверяющим (`seasons_review.py`).

Страниц две, как и у формы обращений: `/seasons` по-русски,
`/uz/seasons` по-узбекски.

Работает без JavaScript: обычный POST отвечает страницей со следующей
карточкой. Скрипт нужен, чтобы крутить круг пальцем и не ждать сети
между карточками.
"""

import secrets
import time

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import get_session
from ..models import Place, SeasonVote
from ..reports import CATEGORY
from ..schemas import Lang, pick
from ..seasons import DECK_SIZE, ENOUGH_VOTES, valid
from .seasons_page import render_page

router = APIRouter(tags=["seasons"])

#: Номер устройства. Не человек: ни имени, ни почты, ни входа в игре нет.
#: Нужен ровно для двух вещей — не показывать дважды одно место и не дать
#: одному телефону наотвечать за весь клуб
COOKIE = "sayr_voter"
COOKIE_AGE = 365 * 24 * 3600

#: Сколько ответов принимаем с одного адреса за час. Щедро: отвечать —
#: это и есть цель. Предел держит не человека, а скрипт с меняющимися
#: cookie; счётчик живёт в памяти процесса, как у формы обращений
_LIMIT, _WINDOW = 200, 3600
_RECENT: dict[str, list[float]] = {}


def _too_often(ip: str) -> bool:
    now = time.monotonic()
    for key in [k for k, v in _RECENT.items() if all(now - t > _WINDOW for t in v)]:
        del _RECENT[key]
    fresh = [t for t in _RECENT.get(ip, []) if now - t < _WINDOW]
    _RECENT[ip] = fresh
    if len(fresh) >= _LIMIT:
        return True
    fresh.append(now)
    return False


def _answered(voter: str):
    """Места, на которые это устройство уже отвечало — включая «не знаю»."""
    return select(SeasonVote.place_id).where(SeasonVote.voter == voter)


def _counted():
    """Сколько у места ответов С МЕСЯЦАМИ.

    «Не знаю» сюда не идёт: три таких ответа — не знание о месте,
    и выводить его из игры они не должны.
    """
    return (
        select(SeasonVote.place_id, func.count().label("n"))
        .where(SeasonVote.from_month.is_not(None))
        .group_by(SeasonVote.place_id)
        .subquery()
    )


def _open_places(voter: str):
    """Места, которым ещё нужен ответ этого устройства.

    Выбывают три категории: одобренные (у них проставлен `season_from`),
    те, на которые это устройство уже отвечало, и те, где ответов
    достаточно, — иначе все отвечали бы на первые двадцать мест,
    а хвост каталога остался бы пустым.
    """
    counted = _counted()
    votes = func.coalesce(counted.c.n, 0)
    return (
        select(Place, votes.label("votes"))
        .outerjoin(counted, counted.c.place_id == Place.id)
        .where(
            Place.is_published,
            Place.season_from.is_(None),
            votes < ENOUGH_VOTES,
            Place.id.not_in(_answered(voter)),
        )
    )


async def _deck(session: AsyncSession, voter: str, lang: Lang) -> list[dict]:
    """Пачка карточек: сначала те, о ком знаем меньше всего.

    Ничьи разбиваются случайно — иначе двое, открывшие игру рядом,
    получили бы одинаковую колоду в одинаковом порядке.
    """
    stmt = (
        _open_places(voter)
        .options(selectinload(Place.region), selectinload(Place.photos))
        .order_by("votes", func.random())
        .limit(DECK_SIZE)
    )
    rows = (await session.execute(stmt)).all()
    return [_card(place, lang) for place, _ in rows]


async def _left(session: AsyncSession, voter: str) -> int:
    """Сколько мест ждут ответа ИМЕННО ЭТОГО устройства.

    Не размер каталога: тот не двигался бы неделями, и счётчик врал бы
    про усилие человека.
    """
    stmt = select(func.count()).select_from(_open_places(voter).subquery())
    return int((await session.execute(stmt)).scalar_one())


async def _mine(session: AsyncSession, voter: str) -> int:
    """Сколько мест этот человек уже разметил — для экрана «спасибо»."""
    stmt = select(func.count()).where(
        SeasonVote.voter == voter, SeasonVote.from_month.is_not(None)
    )
    return int((await session.execute(stmt)).scalar_one())


def _card(place: Place, lang: Lang) -> dict:
    """Карточка места: то же, что видно в каталоге приложения."""
    cover = place.photos[0] if place.photos else None
    return {
        "slug": place.slug,
        "name": pick(place.name, place.name_uz, lang),
        "region": pick(place.region.name, place.region.name_uz, lang)
        if place.region
        else "",
        "category": CATEGORY[lang].get(place.category.value, ""),
        "thumb": (cover.thumb_url or cover.url) if cover else None,
        "km": place.distance_km,
        "hours": place.duration_hours,
    }


def _voter_of(request: Request) -> str:
    return request.cookies.get(COOKIE, "")


def _fresh_voter() -> str:
    return secrets.token_hex(16)


def _with_cookie(response, voter: str):
    response.set_cookie(
        COOKIE,
        voter,
        max_age=COOKIE_AGE,
        httponly=True,
        samesite="lax",
        # Локально сервер работает по http, и cookie с Secure не встанет —
        # тот же приём, что у сессии админки
        secure=settings.admin_cookie_secure,
    )
    return response


async def _page(request: Request, lang: Lang, session: AsyncSession) -> HTMLResponse:
    voter = _voter_of(request) or _fresh_voter()
    html = render_page(
        lang=lang,
        deck=await _deck(session, voter, lang),
        left=await _left(session, voter),
        mine=await _mine(session, voter),
    )
    return _with_cookie(HTMLResponse(html), voter)


@router.get("/seasons", response_class=HTMLResponse)
async def seasons_page(request: Request, session: AsyncSession = Depends(get_session)):
    return await _page(request, "ru", session)


@router.get("/uz/seasons", response_class=HTMLResponse)
async def seasons_page_uz(request: Request, session: AsyncSession = Depends(get_session)):
    return await _page(request, "uz", session)


@router.get("/seasons/deck")
async def seasons_deck(
    request: Request,
    lang: str = "ru",
    session: AsyncSession = Depends(get_session),
):
    """Следующая пачка карточек — когда текущая подходит к концу."""
    voter = _voter_of(request)
    if not voter:
        # Без cookie колоду не собрать: непонятно, что этот человек уже видел
        return JSONResponse({"places": [], "left": 0})
    lang = lang if lang in ("ru", "uz") else "ru"
    return JSONResponse(
        {
            "places": await _deck(session, voter, lang),
            "left": await _left(session, voter),
        }
    )


@router.post("/seasons/vote")
async def seasons_vote(
    request: Request,
    slug: str = Form(""),
    from_month: int | None = Form(None),
    to_month: int | None = Form(None),
    skip: str = Form(""),
    lang: str = Form("ru"),
    accept: str = Header("", alias="Accept"),
    session: AsyncSession = Depends(get_session),
):
    """Один ответ: дуга по кругу или «не знаю».

    Второй ответ с того же устройства перезаписывает первый, а не ложится
    рядом: человек имеет право передумать, а два его голоса на одно место
    сделали бы «согласие большинства» ложью.
    """
    lang = lang if lang in ("ru", "uz") else "ru"
    voter = _voter_of(request) or _fresh_voter()
    if _too_often(request.client.host if request.client else "?"):
        raise HTTPException(429, "Слишком много ответов за час")

    place = (
        await session.execute(select(Place).where(Place.slug == slug.strip()))
    ).scalar_one_or_none()
    if place is None:
        raise HTTPException(404, "Нет такого места")
    # Место уже одобрено — ответ опоздал. Не ошибка человека, но и писать
    # его некуда: карточка ушла из игры, пока он думал
    if place.season_from is not None:
        raise HTTPException(409, "У этого места сезон уже проставлен")

    if skip:
        months: tuple[int | None, int | None] = (None, None)
    elif valid(from_month) and valid(to_month):
        months = (from_month, to_month)
    else:
        raise HTTPException(422, "Нужны оба месяца или «не знаю»")

    existing = (
        await session.execute(
            select(SeasonVote).where(
                SeasonVote.place_id == place.id, SeasonVote.voter == voter
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            SeasonVote(
                place_id=place.id,
                voter=voter,
                from_month=months[0],
                to_month=months[1],
            )
        )
    else:
        existing.from_month, existing.to_month = months
    await session.commit()

    if "application/json" in accept:
        return _with_cookie(JSONResponse({"ok": True}), voter)
    # Без скрипта возвращаемся на страницу: сервер соберёт её заново,
    # и наверху окажется следующая карточка
    back = "/uz/seasons" if lang == "uz" else "/seasons"
    return _with_cookie(RedirectResponse(back, status_code=303), voter)
