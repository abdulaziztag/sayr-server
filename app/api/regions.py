from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Place, Region
from ..schemas import DEFAULT_LANG, Lang, RegionOut, pick

router = APIRouter(prefix="/api/v1", tags=["regions"])


@router.get("/regions", response_model=list[RegionOut])
async def list_regions(
    session: AsyncSession = Depends(get_session),
    lang: Lang = Query(DEFAULT_LANG, description="язык названий; без него — русский"),
):
    published_count = (
        select(func.count())
        .where(Place.region_id == Region.id, Place.is_published)
        .scalar_subquery()
    )
    # Порядок задан вручную полем sort_order, языком он не управляется
    stmt = select(Region, published_count).order_by(Region.sort_order, Region.name)
    rows = (await session.execute(stmt)).all()
    return [
        RegionOut(id=r.id, name=pick(r.name, r.name_uz, lang), places_count=cnt)
        for r, cnt in rows
    ]
