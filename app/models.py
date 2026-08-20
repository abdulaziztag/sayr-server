import enum
from datetime import date, datetime
from pathlib import Path

from fastapi_storages import FileSystemStorage
from fastapi_storages.integrations.sqlalchemy import FileType
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
    reserve = "reserve"
    desert = "desert"
    other = "other"


class Difficulty(str, enum.Enum):
    easy = "easy"
    medium = "medium"
    hard = "hard"


class OvernightType(str, enum.Enum):
    """Способ ночёвки на многодневках — других вариантов в каталоге нет."""

    tent = "tent"
    yurt = "yurt"


class Season(str, enum.Enum):
    spring = "spring"
    summer = "summer"
    autumn = "autumn"
    winter = "winter"


photo_storage = FileSystemStorage(path=str(PHOTOS_DIR))
gpx_storage = FileSystemStorage(path=str(GPX_DIR))
# Иначе photo.jpg, загруженный ко второму месту, молча перетирает файл первого:
# write() коллизий не проверяет, а с этим флагом StorageFile сам дописывает _1
photo_storage.OVERWRITE_EXISTING_FILES = False
gpx_storage.OVERWRITE_EXISTING_FILES = False


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
    # Дорога на машине от Ташкента (OSRM): для «ехать 1:50 · 63 км» и окна выезда
    drive_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    drive_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Сезон диапазоном месяцев («май — окт»); best_seasons остаются для фильтра
    season_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Многодневка: способ ночёвки (null = однодневный выход)
    overnight: Mapped[OvernightType | None] = mapped_column(
        Enum(OvernightType, name="overnight_type"), nullable=True
    )
    best_seasons: Mapped[list[str]] = mapped_column(ARRAY(String(16)), default=list)
    kid_friendly: Mapped[bool] = mapped_column(Boolean, default=False)

    short_desc: Mapped[str] = mapped_column(Text, default="")
    description_md: Mapped[str] = mapped_column(Text, default="")
    how_to_get_md: Mapped[str] = mapped_column(Text, default="")

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
        # id вторым ключом, как у треков: первый снимок — это обложка
        # (schemas._base_fields), и при одинаковом sort_order у двух кадров
        # обложка была бы делом случая и могла меняться между запросами
        order_by="[PlacePhoto.sort_order, PlacePhoto.id]",
    )
    # Первый по sort_order — основной: его рисует мини-карта, из него
    # заполняются старые поля gpx_url/gpx_credit в API
    tracks: Mapped[list["PlaceTrack"]] = relationship(
        back_populates="place",
        cascade="all, delete-orphan",
        order_by="[PlaceTrack.sort_order, PlaceTrack.id]",
    )

    def __str__(self) -> str:
        return self.name


class PlaceTrack(Base):
    """Маршрут к месту. Их может быть несколько — разные тропы к одной цели,
    и человек выбирает между ними по имени, длине и набору."""

    __tablename__ = "place_tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[int] = mapped_column(
        ForeignKey("places.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    gpx_file = mapped_column(FileType(storage=gpx_storage), nullable=False)
    gpx_credit: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Считаются на сервере при сохранении (сид, админка): клиент качает
    # только выбранный файл, а статистику видит до скачивания
    distance_km: Mapped[float] = mapped_column(Float, default=0)
    ascent_m: Mapped[int] = mapped_column(Integer, default=0)
    # Откуда идут пешком — первая точка записи. Координаты самого места
    # это цель: вершина или водопад, и в автонавигатор их вбивать бесполезно
    start_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    place: Mapped[Place] = relationship(back_populates="tracks")

    @property
    def gpx_url(self) -> str | None:
        # basename: fastapi-storages может сохранить в колонку полный путь
        return f"/media/gpx/{Path(self.gpx_file.name).name}" if self.gpx_file else None

    def __str__(self) -> str:
        return self.name


class PlaceNeighbor(Base):
    """Место, мимо которого проходит трек другого места.

    Связь по треку, а не по расстоянию: рядом на карте могут лежать точки
    совершенно разных выходов, и близость сама по себе ничего человеку
    не обещает. А вот «этот маршрут заходит и туда» — обещает, и проверяемо.
    Поводом стал трек «Водопад Пальтау и грот Оби-Рахмат»: два разных места
    в 1233 метрах, которые люди проходят за один выход.

    Строки симметричны — на карточке соседа связь видна тоже. Трек в ключе
    нужен для пересчёта: при перезаливке файла удаляются ровно его связи,
    остальные не трогаем.
    """

    __tablename__ = "place_neighbors"

    place_id: Mapped[int] = mapped_column(
        ForeignKey("places.id", ondelete="CASCADE"), primary_key=True
    )
    neighbor_id: Mapped[int] = mapped_column(
        ForeignKey("places.id", ondelete="CASCADE"), primary_key=True
    )
    track_id: Mapped[int] = mapped_column(
        ForeignKey("place_tracks.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    # Ближайший подход трека к точке соседа — для отладки и для порога
    distance_m: Mapped[int] = mapped_column(Integer, default=0)


class TripIntent(Base):
    """«Я пойду сюда в этот день». Аккаунтов нет — голос привязан к устройству."""

    __tablename__ = "trip_intents"
    __table_args__ = (
        UniqueConstraint("place_id", "day", "device_id", name="uq_intent_place_day_device"),
        Index("ix_trip_intents_place_day", "place_id", "day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[int] = mapped_column(ForeignKey("places.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date, index=True)
    device_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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


class ApiEvent(Base):
    """Обращение к смысловому маршруту: чем пользуются и насколько часто.

    Пишется сервером из запросов, которые к нему и так приходят, — клиенты
    никаких «событий» не отправляют. Сырьё живёт срок хранения из настроек,
    после чего от него остаются только дневные агрегаты.
    """

    __tablename__ = "api_events"
    __table_args__ = (Index("ix_api_events_kind_ts", "kind", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    # Пусто у страницы шеринга (её открывает браузер) и у клиентов,
    # которые ещё не научились слать заголовок
    device: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))
    # Слаг места; у события gpx — имя файла трека, у каталога пусто
    slug: Mapped[str | None] = mapped_column(String(160), nullable=True)


class DailyStat(Base):
    """Свёртка за сутки. Живёт вечно: сырьё стирается, история — нет."""

    __tablename__ = "daily_stats"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    active_devices: Mapped[int] = mapped_column(Integer, default=0)
    new_devices: Mapped[int] = mapped_column(Integer, default=0)
    place_opens: Mapped[int] = mapped_column(Integer, default=0)
    catalog_opens: Mapped[int] = mapped_column(Integer, default=0)
    gpx_downloads: Mapped[int] = mapped_column(Integer, default=0)


class Device(Base):
    """Когда устройство увидели впервые.

    Отдельная таблица, а не вывод из событий: после ротации сырья
    «новизну» определить будет не из чего, а «всего за историю» —
    это просто число строк здесь. Строка на устройство, растёт медленно.
    """

    __tablename__ = "devices"

    device: Mapped[str] = mapped_column(String(64), primary_key=True)
    first_seen: Mapped[date] = mapped_column(Date, index=True)
