"""Ссылки, которые открывают приложение: приглашение в комнату
https://sayr.info/r/{invite} и файлы «универсальных» ссылок.

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
from .rooms import _organizer

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
                        "components": [{"/": "/p/*"}, {"/": "/r/*"}],
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
<style>
  body {{ margin: 0; font-family: -apple-system, system-ui, sans-serif;
         background: #F3EEE3; color: #161A17; }}
  .wrap {{ max-width: 480px; margin: 0 auto; padding: 24px 20px 40px; }}
  img.cover {{ width: 100%; border-radius: 24px; aspect-ratio: 4/3; object-fit: cover; }}
  h1 {{ font-size: 26px; margin: 18px 0 6px; line-height: 1.25; }}
  .meta {{ color: #8A8272; font-size: 13px; text-transform: uppercase;
           letter-spacing: 0.06em; margin-bottom: 14px; }}
  p {{ line-height: 1.55; color: #57524A; }}
  a.btn {{ display: block; text-align: center; padding: 15px; border-radius: 999px;
           text-decoration: none; font-weight: 600; margin-top: 12px; }}
  a.open {{ background: #2F5D3F; color: #FBF8F1; margin-top: 22px; }}
  a.store {{ background: #FBF8F1; color: #161A17; border: 1.5px solid #DCD4C4; }}
  .hint {{ text-align: center; color: #8A8272; font-size: 12px; margin-top: 10px; }}
</style>
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
    )
    return HTMLResponse(uz_display(page))
