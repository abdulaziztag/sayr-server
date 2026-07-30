from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Place, Region
from ..schemas import RegionOut

router = APIRouter(prefix="/api/v1", tags=["regions"])


@router.get("/regions", response_model=list[RegionOut])
async def list_regions(session: AsyncSession = Depends(get_session)):
    published_count = (
        select(func.count())
        .where(Place.region_id == Region.id, Place.is_published)
        .scalar_subquery()
    )
    stmt = select(Region, published_count).order_by(Region.sort_order, Region.name)
    rows = (await session.execute(stmt)).all()
    return [RegionOut(id=r.id, name=r.name, places_count=cnt) for r, cnt in rows]
