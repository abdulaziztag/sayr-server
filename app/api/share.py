"""Страница места для ссылок из «Поделиться»: https://sayr.info/p/{slug}.

С установленным приложением ссылку перехватывает клиент (диплинк), без него
открывается эта страница: фото, описание и кнопка «Открыть в приложении»
через sayr:// — она сработает у тех, у кого приложение есть, а система
просто проигнорирует схему у остальных.
"""

from html import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..models import Place

router = APIRouter(tags=["share"])

_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} — Sayr</title>
<meta property="og:title" content="{name}">
<meta property="og:description" content="{desc}">
{og_image}
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
</style>
</head>
<body>
<div class="wrap">
  {cover}
  <h1>{name}</h1>
  <div class="meta">{meta}</div>
  <p>{desc}</p>
  <a class="open" href="sayr://place/{slug}">Открыть в приложении</a>
  <div class="hint">Работает, если приложение Sayr установлено</div>
</div>
</body>
</html>"""

_CATEGORY_RU = {
    "waterfall": "водопад", "peak": "пик", "gorge": "ущелье", "cave": "пещера",
    "lake": "озеро", "canyon": "каньон", "spring": "родник", "plateau": "плато",
    "petroglyphs": "петроглифы", "reserve": "нацпарк", "desert": "пустыня",
    "other": "место",
}


@router.get("/p/{slug}", response_class=HTMLResponse)
async def share_page(slug: str, session: AsyncSession = Depends(get_session)) -> str:
    stmt = (
        select(Place)
        .where(Place.slug == slug, Place.is_published)
        .options(selectinload(Place.photos), selectinload(Place.region))
    )
    place = (await session.execute(stmt)).scalar_one_or_none()
    if place is None:
        raise HTTPException(404, "Место не найдено")

    photo = place.photos[0] if place.photos else None
    photo_url = photo.url if photo else None
    cover = f'<img class="cover" src="{photo_url}" alt="">' if photo_url else ""
    og_image = f'<meta property="og:image" content="{photo_url}">' if photo_url else ""

    meta_parts = [_CATEGORY_RU.get(place.category.value, place.category.value)]
    if place.region:
        meta_parts.append(place.region.name)
    if place.elevation_m:
        meta_parts.append(f"{place.elevation_m} м")

    return _PAGE.format(
        name=escape(place.name),
        desc=escape(place.short_desc or ""),
        slug=escape(place.slug),
        meta=escape(" · ".join(meta_parts)),
        cover=cover,
        og_image=og_image,
    )
