"""Ссылки, которые открывают приложение: приглашение в комнату
https://sayr.info/r/{invite}, заявка в открытую комнату
https://sayr.info/j/{code} и файлы «универсальных» ссылок.

С установленным приложением ссылку перехватывает система — iOS по
`apple-app-site-association`, Android по `assetlinks.json` — и открывает
комнату в приложении. Без приложения открывается эта страница: куда
и когда зовут, кто зовёт, и кнопки магазинов.
"""

from html import escape

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import get_session
from ..models import Place, Room, RoomMember
from ..push.outbox import day_text
from ..schemas import DEFAULT_LANG, Lang, pick
from ..typography import uz_display
from .app_links import smart_banner, store_buttons
from .rooms import _end, _organizer, _people, askable_room

router = APIRouter(tags=["links"])


@router.get("/.well-known/apple-app-site-association", include_in_schema=False)
async def apple_app_site_association() -> JSONResponse:
    """Какие адреса sayr.info открывает приложение на iPhone"""
    app_ids = [a.strip() for a in settings.ios_app_ids.split(",") if a.strip()]
    return JSONResponse(
        {
            "applinks": {
                "details": [
                    {
                        "appIDs": app_ids,
                        # /j/ — заявка в открытую комнату: приложение
                        # показывает комнату с «Попроситься»
                        "components": [{"/": "/p/*"}, {"/": "/r/*"}, {"/": "/j/*"}],
                    }
                ]
            }
        }
    )


@router.get("/.well-known/assetlinks.json", include_in_schema=False)
async def assetlinks() -> JSONResponse:
    """Каким подписям Android доверять адреса sayr.info. Без отпечатков —
    пустой список: ссылки тогда открываются через выбор «в приложении»"""
    prints = [p.strip().upper() for p in settings.android_cert_sha256.split(",") if p.strip()]
    if not prints:
        return JSONResponse([])
    return JSONResponse(
        [
            {
                "relation": ["delegate_permission/common.handle_all_urls"],
                "target": {
                    "namespace": "android_app",
                    "package_name": settings.android_package,
                    "sha256_cert_fingerprints": prints,
                },
            }
        ]
    )


# Одна вёрстка у приглашения своих и у заявки в открытую комнату: обе —
# «куда и когда» с кнопкой в приложение, различается только то, кто что
# видит. Стиль вставляется через format, поэтому скобки здесь одинарные
_STYLE = """<style>
  body { margin: 0; font-family: -apple-system, system-ui, sans-serif;
         background: #F3EEE3; color: #161A17; }
  .wrap { max-width: 480px; margin: 0 auto; padding: 24px 20px 40px; }
  img.cover { width: 100%; border-radius: 24px; aspect-ratio: 4/3; object-fit: cover; }
  h1 { font-size: 26px; margin: 18px 0 6px; line-height: 1.25; }
  .meta { color: #8A8272; font-size: 13px; text-transform: uppercase;
          letter-spacing: 0.06em; margin-bottom: 14px; }
  p { line-height: 1.55; color: #57524A; }
  p.people { color: #161A17; font-weight: 600; margin-bottom: 0; }
  a.btn { display: block; text-align: center; padding: 15px; border-radius: 999px;
          text-decoration: none; font-weight: 600; margin-top: 12px; }
  a.open { background: #2F5D3F; color: #FBF8F1; margin-top: 22px; }
  a.store { background: #FBF8F1; color: #161A17; border: 1.5px solid #DCD4C4; }
  .hint { text-align: center; color: #8A8272; font-size: 12px; margin-top: 10px; }
</style>"""

_PAGE = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Sayr</title>
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
{og_image}
{banner}
{style}
</head>
<body>
<div class="wrap">
  {cover}
  <h1>{title}</h1>
  <div class="meta">{meta}</div>
  <p>{desc}</p>
  <a class="btn open" href="sayr://invite/{invite}">{open_label}</a>
  <div class="hint">{hint}</div>
  {stores}
</div>
</body>
</html>"""

# Заявка в открытую комнату. Ссылку выкладывают в большие чаты, где её
# откроет кто угодно и без входа, поэтому людей на странице нет вовсе —
# ни имени организатора, ни фото, ни пола: только место, дни и сколько
# человек. Карточки людей, как и в приложении, — вошедшим. Превью для
# Telegram — полным набором og- и twitter-тегов
_REQUEST_PAGE = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Sayr</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Sayr">
<meta property="og:url" content="{url}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:image" content="{image}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{desc}">
<meta name="twitter:image" content="{image}">
{banner}
{style}
</head>
<body>
<div class="wrap">
  {cover}
  <h1>{place}</h1>
  <div class="meta">{meta}</div>
  <p class="people">{people}</p>
  <p>{explain}</p>
  <a class="btn open" href="sayr://room/{code}">{open_label}</a>
  <div class="hint">{hint}</div>
  {stores}
</div>
</body>
</html>"""

_GONE = """<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sayr</title>
<style>body {{ margin: 0; font-family: -apple-system, system-ui, sans-serif;
background: #F3EEE3; color: #161A17; }} .wrap {{ max-width: 480px; margin: 0 auto;
padding: 48px 20px; }} p {{ color: #57524A; line-height: 1.55; }}</style></head>
<body><div class="wrap"><h1>{title}</h1><p>{text}</p></div></body></html>"""

_T = {
    "ru": {
        "calls": "{name} зовёт на {place}",
        "calls_anon": "Зовут на {place}",
        "desc": "Поход с компанией в Sayr — гиде по местам Узбекистана. "
        "Откройте приглашение в приложении, чтобы вступить.",
        "open": "Открыть в приложении",
        "hint": "Работает, если приложение Sayr установлено",
        "gone_title": "Ссылка больше не действует",
        "gone_text": "Поход отменён или прошёл, либо организатор сменил ссылку.",
        "days": "{n} дн.",
        "looking": "Ищут попутчиков",
        "explain": "Открытая комната: попроситься можно в приложении Sayr — "
        "организатор решает, кого взять.",
        "ask": "Попроситься в приложении",
        "gone_request": "Поход отменён или прошёл, либо организатор больше "
        "не ищет попутчиков.",
        # Одно, два, пять — как plurals в приложениях
        "people": ("человек", "человека", "человек"),
    },
    "uz": {
        "calls": "{name} sizni {place}ga taklif qilmoqda",
        "calls_anon": "{place}ga taklif",
        "desc": "Sayr — Oʻzbekiston joylari boʻyicha qoʻllanmada kompaniya bilan sayohat. "
        "Qoʻshilish uchun taklifni ilovada oching.",
        "open": "Ilovada ochish",
        "hint": "Sayr ilovasi oʻrnatilgan boʻlsa ishlaydi",
        "gone_title": "Havola endi ishlamaydi",
        "gone_text": "Sayohat bekor qilingan yoki oʻtib ketgan, yoki tashkilotchi havolani almashtirgan.",
        "days": "{n} kun",
        "looking": "Hamroh izlashmoqda",
        "explain": "Ochiq xona: Sayr ilovasida qoʻshilishni soʻrash mumkin — "
        "kimni olishni tashkilotchi hal qiladi.",
        "ask": "Ilovada qoʻshilishni soʻrash",
        "gone_request": "Sayohat bekor qilingan yoki oʻtib ketgan, yoki tashkilotchi "
        "endi hamroh izlamayapti.",
        # Son bilan ot birlikda turadi: «3 kishi»
        "people": ("kishi",) * 3,
    },
}


@router.get("/r/{invite}", response_class=HTMLResponse)
async def invite_page(
    invite: str,
    lang: Lang = Query(DEFAULT_LANG),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    t = _T[lang]
    room = (
        await session.execute(
            select(Room)
            .where(Room.invite == invite)
            .options(
                selectinload(Room.place).selectinload(Place.photos),
                selectinload(Room.members).selectinload(RoomMember.user),
            )
        )
    ).scalar_one_or_none()
    if room is None or room.status != "active" or not settings.rooms_open:
        page = _GONE.format(lang=lang, title=t["gone_title"], text=t["gone_text"])
        return HTMLResponse(uz_display(page), status_code=404)

    place = pick(room.place.name, room.place.name_uz, lang)
    organizer = _organizer(room)
    name = organizer.user.first_name if organizer and organizer.user else ""
    title = (t["calls"] if name else t["calls_anon"]).format(name=name, place=place)
    meta = [day_text(room.day, lang)]
    if room.days > 1:
        meta.append(t["days"].format(n=room.days))
    # Превью в Telegram берёт картинку только по полному адресу
    photo = (
        f"{settings.public_url}{room.place.photos[0].url}" if room.place.photos else None
    )
    page = _PAGE.format(
        lang=lang,
        title=escape(title),
        desc=escape(t["desc"]),
        meta=escape(" · ".join(meta)),
        cover=f'<img class="cover" src="{photo}" alt="">' if photo else "",
        og_image=f'<meta property="og:image" content="{photo}">' if photo else "",
        invite=escape(room.invite),
        open_label=t["open"],
        hint=t["hint"],
        # Приглашение остаётся страницей и на телефоне: без приложения
        # человеку важно увидеть, кто и куда зовёт, а после установки —
        # открыть ту же ссылку снова
        stores=store_buttons(lang, "invite"),
        banner=smart_banner(f"{settings.public_url}/r/{room.invite}"),
        style=_STYLE,
    )
    return HTMLResponse(uz_display(page))


#: Превью, когда у места нет снимков, — картинка лендинга
_DEFAULT_IMAGE = "/static/img/shot-catalog.jpg"


def _count(n: int, forms: tuple[str, str, str]) -> str:
    """«1 человек», «3 человека», «5 человек»"""
    one, few, many = forms
    if n % 10 == 1 and n % 100 != 11:
        word = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


def _dates(room: Room, lang: Lang) -> str:
    """25 сентября; у многодневки — 25–27 сентября или 30 сентября – 2 октября"""
    end = _end(room)
    if end == room.day:
        return day_text(room.day, lang)
    if end.month == room.day.month:
        return f"{room.day.day}–{day_text(end, lang)}"
    return f"{day_text(room.day, lang)} – {day_text(end, lang)}"


@router.get("/j/{code}", response_class=HTMLResponse)
async def request_page(
    code: str,
    lang: Lang = Query(DEFAULT_LANG),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Заявка в открытую комнату: https://sayr.info/j/{code}.

    «Позвать своих» (/r/) пускает сразу, без одобрения и без 18+ — хватает
    имени и года рождения. Для друзей в самый раз, а выложенная в большую
    группу вроде «ГОРЦА» такая ссылка впустила бы в комнату кого угодно.
    Эта ведёт в ту же комнату к «Попроситься»: заявку разбирает
    организатор, анкету и 18+ проверяет сервер. Код комнаты не секрет — он
    и так виден в поиске."""
    t = _T[lang]
    room = await askable_room(session, code) if settings.rooms_open else None
    # Нет такой, только для своих, отменена или прошла — одна и та же
    # страница с тем же 404: по ответу не понять, стоит ли за кодом комната
    # только для своих (как ищем — `askable_room`)
    if room is None:
        page = _GONE.format(lang=lang, title=t["gone_title"], text=t["gone_request"])
        return HTMLResponse(uz_display(page), status_code=404)
    place = pick(room.place.name, room.place.name_uz, lang)
    dates = _dates(room, lang)
    title = f"{place} · {dates}"
    # Только общее число. Мужчин и женщин приложение показывает вошедшим,
    # а эту страницу и её превью видит кто угодно без входа: «1 человек ·
    # 1 женщина» с местом и датой в публичной группе — это объявление, что
    # девушка идёт туда одна
    people_line = _count(_people(room), t["people"])
    url = f"{settings.public_url}/j/{room.code}"
    # Превью в Telegram берёт картинку только по полному адресу
    photo = (
        f"{settings.public_url}{room.place.photos[0].url}" if room.place.photos else None
    )
    page = _REQUEST_PAGE.format(
        lang=lang,
        title=escape(title),
        place=escape(place),
        meta=escape(f"{t['looking']} · {dates}"),
        people=escape(people_line),
        explain=escape(t["explain"]),
        desc=escape(f"{people_line}. {t['explain']}"),
        url=escape(url),
        image=escape(photo or f"{settings.public_url}{_DEFAULT_IMAGE}"),
        cover=f'<img class="cover" src="{escape(photo)}" alt="">' if photo else "",
        # Кнопка и баннер Safari ведут на sayr://room/{код}, а не на https-адрес
        # этой же страницы: его понимают и уже вышедшие сборки — открывают
        # комнату с «Попроситься», — а /j/ приложения узнают только со
        # следующей версии. Да и ссылку на свой же домен Safari приложению
        # не отдаёт, открывает страницу снова
        code=escape(room.code),
        banner=smart_banner(f"sayr://room/{room.code}"),
        open_label=t["ask"],
        hint=t["hint"],
        stores=store_buttons(lang, "request"),
        style=_STYLE,
    )
    return HTMLResponse(uz_display(page))
