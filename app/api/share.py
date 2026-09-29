"""Страница места для ссылок из «Поделиться»: https://sayr.info/p/{slug}.

С установленным приложением ссылку перехватывает система и открывает место
в приложении. Сюда доходят без приложения: телефон уходит дальше — в App
Store или через intent:// в приложение, а без него в Google Play
(app_links.py). Страницу видят компьютер и роботы превью: фото, описание,
кнопка «Открыть в приложении» через sayr:// и кнопки магазинов.
"""

from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import get_session
from ..models import Place
from ..schemas import DEFAULT_LANG, SLUG, Lang, pick
from ..typography import uz_display
from .app_links import phone_redirect, smart_banner, store_buttons

router = APIRouter(tags=["share"])

_PAGE = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} — Sayr</title>
<meta property="og:title" content="{name}">
<meta property="og:description" content="{desc}">
{og_image}
{banner}
<style>
  body {{ margin: 0; font-family: -apple-system, system-ui, sans-serif;
         background: #F3EEE3; color: #161A17; }}
  .wrap {{ max-width: 480px; margin: 0 auto; padding: 24px 20px 40px; }}
  img.cover {{ width: 100%; border-radius: 24px; aspect-ratio: 4/3; object-fit: cover; }}
  h1 {{ font-size: 28px; margin: 18px 0 6px; }}
  .meta {{ color: #8A8272; font-size: 13px; text-transform: uppercase;
           letter-spacing: 0.06em; margin-bottom: 14px; }}
  p {{ line-height: 1.55; color: #57524A; }}
  a.open {{ display: block; text-align: center; background: #2F5D3F; color: #FBF8F1;
            padding: 15px; border-radius: 16px; text-decoration: none;
            font-weight: 600; margin-top: 22px; }}
  .hint {{ text-align: center; color: #8A8272; font-size: 12px; margin-top: 10px; }}
  .stores {{ margin-top: 20px; }}
  a.store {{ display: block; text-align: center; background: #FBF8F1; color: #161A17;
             border: 1.5px solid #DCD4C4; padding: 14px; border-radius: 16px;
             text-decoration: none; font-weight: 600; margin-top: 10px; }}
  /* Ссылка на форму: сообщают о неточности с той же страницы,
     где её и заметили — место в форме подставится само */
  .report {{ text-align: center; margin-top: 26px; padding-top: 16px;
             border-top: 1px solid #DCD4C4; font-size: 13px; }}
  .report a {{ color: #57524A; }}
</style>
</head>
<body>
<div class="wrap">
  {cover}
  <h1>{name}</h1>
  <div class="meta">{meta}</div>
  <p>{desc}</p>
  <a class="open" href="sayr://place/{slug}">{open_label}</a>
  <div class="hint">{hint}</div>
  <div class="stores">{stores}</div>
  <div class="report"><a href="{report_href}">{report_label}</a></div>
</div>
</body>
</html>"""

_CATEGORY_RU = {
    "waterfall": "водопад", "peak": "пик", "gorge": "ущелье", "cave": "пещера",
    "lake": "озеро", "canyon": "каньон", "spring": "родник", "plateau": "плато",
    "petroglyphs": "петроглифы", "reserve": "нацпарк", "desert": "пустыня",
    "other": "место",
}

# Те же слова, что в приложениях (common_category_* в values-uz): страница
# и клиент — одна витрина, и называть категорию по-разному им незачем
_CATEGORY_UZ = {
    "waterfall": "sharshara", "peak": "choʻqqi", "gorge": "dara", "cave": "gʻor",
    "lake": "koʻl", "canyon": "kanyon", "spring": "buloq", "plateau": "plato",
    "petroglyphs": "petrogliflar", "reserve": "milliy bogʻ", "desert": "choʻl",
    "other": "joy",
}

_CATEGORY = {"ru": _CATEGORY_RU, "uz": _CATEGORY_UZ}

# Подписи самой страницы. Их две, отдельный файл переводов ради них
# заводить не за чем
_OPEN_LABEL = {"ru": "Открыть в приложении", "uz": "Ilovada ochish"}
_HINT = {
    "ru": "Работает, если приложение Sayr установлено",
    "uz": "Sayr ilovasi oʻrnatilgan boʻlsa ishlaydi",
}
_ELEVATION = {"ru": "м", "uz": "m"}
_REPORT = {
    "ru": "Нашли неточность? Напишите",
    "uz": "Xatolik topdingizmi? Yozing",
}


@router.get("/p/{slug}", response_class=HTMLResponse)
async def share_page(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    lang: Lang = Query(DEFAULT_LANG, description="язык страницы; без него — русский"),
) -> Response:
    if not SLUG.match(slug):
        raise HTTPException(404, "Место не найдено")
    stmt = (
        select(Place)
        .where(Place.slug == slug, Place.is_published)
        .options(selectinload(Place.photos), selectinload(Place.region))
    )
    place = (await session.execute(stmt)).scalar_one_or_none()
    if place is None:
        raise HTTPException(404, "Место не найдено")

    # Ответ зависит от устройства: кэш между нами и человеком не должен
    # отдать компьютеру редирект в магазин, а телефону — страницу
    vary = {"Vary": "User-Agent"}
    target = phone_redirect(request.headers.get("user-agent"), f"place/{place.slug}", "share")
    if target:
        return RedirectResponse(target, status_code=302, headers=vary)

    photo = place.photos[0] if place.photos else None
    photo_url = photo.url if photo else None
    cover = f'<img class="cover" src="{photo_url}" alt="">' if photo_url else ""
    # Превью ссылки берёт картинку только по полному адресу (протокол
    # Open Graph), а путь из базы — от корня сайта
    if photo_url and not photo_url.startswith("http"):
        photo_url = f"{settings.public_url}{photo_url}"
    og_image = f'<meta property="og:image" content="{photo_url}">' if photo_url else ""

    meta_parts = [_CATEGORY[lang].get(place.category.value, place.category.value)]
    if place.region:
        meta_parts.append(pick(place.region.name, place.region.name_uz, lang))
    if place.elevation_m:
        meta_parts.append(f"{place.elevation_m} {_ELEVATION[lang]}")

    page = _PAGE.format(
        lang=lang,
        name=escape(pick(place.name, place.name_uz, lang)),
        desc=escape(pick(place.short_desc or "", place.short_desc_uz, lang)),
        slug=escape(place.slug),
        meta=escape(" · ".join(meta_parts)),
        cover=cover,
        og_image=og_image,
        open_label=_OPEN_LABEL[lang],
        hint=_HINT[lang],
        report_href=("/report" if lang == "ru" else "/uz/report")
        + f"?place={place.slug}",
        report_label=_REPORT[lang],
        banner=smart_banner(f"{settings.public_url}/p/{place.slug}"),
        stores=store_buttons(lang, "share"),
    )
    return HTMLResponse(uz_display(page), headers=vary)
