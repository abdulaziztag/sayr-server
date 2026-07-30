import enum
from datetime import datetime
from pathlib import Path

from fastapi_storages import FileSystemStorage
from fastapi_storages.integrations.sqlalchemy import FileType
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import GPX_DIR, PHOTOS_DIR


class Base(DeclarativeBase):
    pass


class PlaceCategory(str, enum.Enum):
    waterfall = "waterfall"
    peak = "peak"
    gorge = "gorge"
    cave = "cave"
    lake = "lake"
    canyon = "canyon"
    spring = "spring"
    plateau = "plateau"
    petroglyphs = "petroglyphs"
    other = "other"


class Difficulty(str, enum.Enum):
    easy = "easy"
    medium = "medium"
    hard = "hard"


class Season(str, enum.Enum):
    spring = "spring"
    summer = "summer"
    autumn = "autumn"
    winter = "winter"


photo_storage = FileSystemStorage(path=str(PHOTOS_DIR))
gpx_storage = FileSystemStorage(path=str(GPX_DIR))


class Region(Base):
    __tablename__ = "regions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    places: Mapped[list["Place"]] = relationship(back_populates="region")

    def __str__(self) -> str:
        return self.name


class Place(Base):
    __tablename__ = "places"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[PlaceCategory] = mapped_column(
        Enum(PlaceCategory, name="place_category"), index=True
    )
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id"), index=True)

    # Точки храним простыми float: на сотнях мест geometry-колонка с индексом не нужна,
    # PostGIS-функции для near-фильтра строят geography прямо в запросе.
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    elevation_m: Mapped[int | None] = mapped_column(Integer, nullable=True)

    difficulty: Mapped[Difficulty] = mapped_column(
        Enum(Difficulty, name="difficulty"), index=True
    )
    distance_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    elevation_gain_m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    best_seasons: Mapped[list[str]] = mapped_column(ARRAY(String(16)), default=list)
    kid_friendly: Mapped[bool] = mapped_column(Boolean, default=False)

    short_desc: Mapped[str] = mapped_column(Text, default="")
    description_md: Mapped[str] = mapped_column(Text, default="")
    how_to_get_md: Mapped[str] = mapped_column(Text, default="")

    gpx_file = mapped_column(FileType(storage=gpx_storage), nullable=True)
    gpx_credit: Mapped[str | None] = mapped_column(String(300), nullable=True)

    is_published: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    region: Mapped[Region] = relationship(back_populates="places")
    photos: Mapped[list["PlacePhoto"]] = relationship(
        back_populates="place",
        cascade="all, delete-orphan",
        order_by="PlacePhoto.sort_order",
    )

    @property
    def gpx_url(self) -> str | None:
        # basename: fastapi-storages может сохранить в колонку полный путь
        return f"/media/gpx/{Path(self.gpx_file.name).name}" if self.gpx_file else None

    def __str__(self) -> str:
        return self.name


class PlacePhoto(Base):
    __tablename__ = "place_photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[int] = mapped_column(
        ForeignKey("places.id", ondelete="CASCADE"), index=True
    )
    file = mapped_column(FileType(storage=photo_storage), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    credit: Mapped[str] = mapped_column(String(300), default="")

    place: Mapped[Place] = relationship(back_populates="photos")

    @property
    def url(self) -> str | None:
        return f"/media/photos/{Path(self.file.name).name}" if self.file else None

    @property
    def thumb_url(self) -> str | None:
        if not self.file:
            return None
        return f"/media/thumbs/{Path(self.file.name).stem}_thumb.jpg"

    def __str__(self) -> str:
        return self.file.name if self.file else f"photo #{self.id}"
