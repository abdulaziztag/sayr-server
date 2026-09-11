"""Проверка ответов игры: одно место — одно решение.

Ответы игроков к местам сами не применяются. Здесь их смотрит человек:
все дуги по месту наложены на один круг, предложенная — жирной, и её
можно подкрутить прежде, чем нажать «Одобрить». Сто мест — сто решений
вместо тысячи, и ни одна цифра в каталоге не появляется без просмотра.

Страница снаружи админки и с собственным паролем: проверять сезоны
не значит иметь право править каталог, и однажды это захочется отдать
кому-то из клуба.
"""

import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import get_session
from ..models import Place, SeasonVote
from ..seasons import ENOUGH_VOTES, seasons_of, valid
from .seasons_page import render_login, render_review

router = APIRouter(tags=["seasons"])

COOKIE = "sayr_review"
#: Сутки. Проверка — занятие на вечер, и держать вход открытым дольше
#: незачем: пароль вводится один раз за заход
MAX_AGE = 24 * 3600

_signer = URLSafeTimedSerializer(settings.secret_key, salt="seasons-review")


def _password() -> str:
    """Свой пароль, если задан; иначе админский.

    Так страница работает сразу после выката, а когда проверку захочется
    отдать кому-то из клуба — заводится `SAYR_REVIEW_PASSWORD`, и вместе
    с ним не отдаётся вся админка.
    """
    return settings.review_password or settings.admin_password


def _allowed(request: Request) -> bool:
    token = request.cookies.get(COOKIE, "")
    if not token:
        return False
    try:
        _signer.loads(token, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return True


def _guard(request: Request) -> HTMLResponse | None:
    return None if _allowed(request) else HTMLResponse(render_login())


async def _queue(session: AsyncSession) -> list[Place]:
    """Места без сезона, у которых есть хоть один ответ с месяцами.

    Порядок — по убыванию числа ответов: набравшие порог идут первыми,
    у них и уверенности больше.
    """
    counted = (
        select(SeasonVote.place_id, func.count().label("n"))
        .where(SeasonVote.from_month.is_not(None))
        .group_by(SeasonVote.place_id)
        .subquery()
    )
    stmt = (
        select(Place)
        .join(counted, counted.c.place_id == Place.id)
        .where(Place.season_from.is_(None))
        .options(selectinload(Place.region), selectinload(Place.photos))
        .order_by(counted.c.n.desc(), Place.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _votes(
    session: AsyncSession, place_ids: list[int]
) -> dict[int, list[tuple[int, int]]]:
    """Ответы с месяцами для всех мест списка — одним запросом, а не сотней."""
    if not place_ids:
        return {}
    stmt = (
        select(SeasonVote.place_id, SeasonVote.from_month, SeasonVote.to_month)
        .where(SeasonVote.place_id.in_(place_ids), SeasonVote.from_month.is_not(None))
        .order_by(SeasonVote.id)
    )
    votes: dict[int, list[tuple[int, int]]] = {pid: [] for pid in place_ids}
    for place_id, start, end in (await session.execute(stmt)).all():
        votes[place_id].append((start, end))
    return votes


@router.get("/seasons/review", response_class=HTMLResponse)
async def review(request: Request, session: AsyncSession = Depends(get_session)):
    """Весь список сразу: проверка — это разбор подряд, а не игра по одной."""
    if (closed := _guard(request)) is not None:
        return closed
    queue = await _queue(session)
    votes = await _votes(session, [place.id for place in queue])
    return HTMLResponse(
        render_review([(place, votes[place.id]) for place in queue], ENOUGH_VOTES)
    )


def _done(request: Request):
    """Скрипту — короткое «да», без него — обратно в список."""
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": True})
    return RedirectResponse("/seasons/review", status_code=303)


@router.post("/seasons/review/login")
async def review_login(request: Request, password: str = Form("")):
    # Сравниваем БАЙТЫ: compare_digest на строках с кириллицей бросает
    # TypeError, и человек с русским паролем получал бы 500 вместо «не подошёл»
    if not secrets.compare_digest(password.encode(), _password().encode()):
        return HTMLResponse(render_login(failed=True), status_code=401)
    response = RedirectResponse("/seasons/review", status_code=303)
    response.set_cookie(
        COOKIE,
        _signer.dumps("ok"),
        max_age=MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.admin_cookie_secure,
    )
    return response


@router.post("/seasons/review/approve")
async def review_approve(
    request: Request,
    slug: str = Form(""),
    from_month: int | None = Form(None),
    to_month: int | None = Form(None),
    session: AsyncSession = Depends(get_session),
):
    """Одобрение: дуга становится сезоном места, и оно уходит из игры."""
    if (closed := _guard(request)) is not None:
        return closed
    if not (valid(from_month) and valid(to_month)):
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"ok": False, "error": "Нужны оба месяца"}, status_code=422)
        return RedirectResponse("/seasons/review", status_code=303)
    place = (
        await session.execute(select(Place).where(Place.slug == slug.strip()))
    ).scalar_one_or_none()
    if place is not None:
        place.season_from = from_month
        place.season_to = to_month
        # Четыре сезона считаются из дуги: держать их отдельным решением
        # значило бы задавать второй вопрос там, где задан один
        place.best_seasons = seasons_of(from_month, to_month)
        await session.commit()
    return _done(request)


@router.post("/seasons/review/clear")
async def review_clear(
    request: Request,
    slug: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """Ответы стёрты — место возвращается в игру, и возвращается первым."""
    if (closed := _guard(request)) is not None:
        return closed
    place = (
        await session.execute(select(Place).where(Place.slug == slug.strip()))
    ).scalar_one_or_none()
    if place is not None:
        await session.execute(
            delete(SeasonVote).where(SeasonVote.place_id == place.id)
        )
        await session.commit()
    return _done(request)
