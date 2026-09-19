import enum
from datetime import date, datetime
from datetime import time as datetime_time
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
    Time,
    UniqueConstraint,
    func,
    text,
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
    """Насколько дорого стоит ошибка, а не сколько уйдёт сил.

    Сил стоят часы и километры, они и так на карточке. Ступень
    определяется худшим участком маршрута, а не средним: десять часов
    по тропе — легко и долго, сорок минут по живой осыпи над обрывом —
    коротко и сложно.

    `extreme` наружу не выезжает: старые сборки падают на незнакомой
    строке и роняют разбор всего списка. Клиенту он приезжает как `hard`
    плюс отдельный флаг — см. schemas._base_fields
    """

    easy = "easy"
    medium = "medium"
    hard = "hard"
    extreme = "extreme"


class OvernightType(str, enum.Enum):
    """Способ ночёвки на многодневках — других вариантов в каталоге нет."""

    tent = "tent"
    yurt = "yurt"


class Season(str, enum.Enum):
    spring = "spring"
    summer = "summer"
    autumn = "autumn"
    winter = "winter"


class PlanStepKind(str, enum.Enum):
    """Вид станции в плане по дням — от него зависит карточка в нити.

    Семь видов, и все уже есть в расчётной нити однодневки: план не
    заводит нового языка, он только называет станции руками. Участки —
    `hike` и `road`, у них длительность; остальное — точки со временем.
    """

    depart = "depart"
    point = "point"
    hike = "hike"
    road = "road"
    summit = "summit"
    night = "night"
    home = "home"


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
    # Узбекское имя — nullable без UNIQUE. Пустая строка означала бы
    # «перевод есть и он пуст», а нам надо отличать это от «перевода нет»:
    # на различии стоит и фолбэк на русский, и подсчёт готовности в админке
    name_uz: Mapped[str | None] = mapped_column(String(120), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # Область региона: районы Ташкентской области (Чимган, Чарвак, Угам,
    # Пскем, Паркент, Ахангаран) в фильтре группируются под одной подписью,
    # целые области стоят сами по себе. Правило группировки — у клиента,
    # сервер лишь знает, кто чей. Льёт seed/apply_regions.py
    area: Mapped[str | None] = mapped_column(String(120), nullable=True)
    area_uz: Mapped[str | None] = mapped_column(String(120), nullable=True)

    places: Mapped[list["Place"]] = relationship(back_populates="region")

    def __str__(self) -> str:
        return self.name


class Place(Base):
    __tablename__ = "places"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    name_uz: Mapped[str | None] = mapped_column(String(200), nullable=True)
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
    # Сколько дней занимает выход. Заполняется только вместе с overnight
    # и уточняет его: два дня — «ночёвка», три и больше — «многодневка».
    # Пусто значит однодневный, а не ноль
    trip_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    best_seasons: Mapped[list[str]] = mapped_column(ARRAY(String(16)), default=list)
    # Тропёжка зимой, 1–10: сколько сил уходит на снег. Опасность, 1–10 —
    # отдельно от сложности: лёгкая тропа бывает камнеопасной. Ограничения —
    # коды из seasons.LIMITS: погранзона, закон, охота, временное закрытие.
    # Все три приходят из игры на сайте через одобрение проверяющим, как
    # и сезон. Пусто — «ещё не знаем», а не «нет»
    winter_load: Mapped[int | None] = mapped_column(Integer, nullable=True)
    danger: Mapped[int | None] = mapped_column(Integer, nullable=True)
    limits: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), default=list, server_default="{}"
    )
    kid_friendly: Mapped[bool] = mapped_column(Boolean, default=False)
    # Коллекции клуба «Проект 21»: cascade / horizon / mirage / underground.
    # Коды, не имена: названия коллекций — имена собственные и не переводятся,
    # клиент рисует их сам. Состав правится картой seed/data/collections.json
    collections: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), default=list, server_default="{}"
    )

    # Каждый перевод стоит сразу за своим оригиналом: порядок полей формы
    # в админке sqladmin берёт отсюда, и пара языков должна оказаться рядом,
    # а не двумя блоками «сначала всё по-русски, потом всё по-узбекски».
    #
    # Узбекские колонки nullable — см. комментарий у Region.name_uz. Русские
    # объявлены NOT NULL с default "", и там «пусто» и «не заполнено»
    # слиплись; повторять эту ошибку не будем
    short_desc: Mapped[str] = mapped_column(Text, default="")
    short_desc_uz: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_md: Mapped[str] = mapped_column(Text, default="")
    description_md_uz: Mapped[str | None] = mapped_column(Text, nullable=True)
    how_to_get_md: Mapped[str] = mapped_column(Text, default="")
    how_to_get_md_uz: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_published: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    region: Mapped[Region] = relationship(back_populates="places")
    # Дорога от каждого города выезда (app/cities.py); поля drive_minutes /
    # drive_km выше остаются строкой Ташкента для старых сборок
    drive_times: Mapped[list["PlaceDriveTime"]] = relationship(
        back_populates="place", cascade="all, delete-orphan"
    )
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
    # Планы по дням (спека 2026-09-13-multiday-plan-design.md). У места
    # без пометки ночёвки расчётная нить остаётся, планы — варианты к ней;
    # у места с ночёвкой план заменяет расчёт
    plans: Mapped[list["PlacePlan"]] = relationship(
        back_populates="place",
        cascade="all, delete-orphan",
        order_by="[PlacePlan.sort_order, PlacePlan.id]",
    )

    def __str__(self) -> str:
        return self.name


class PlaceDriveTime(Base):
    """Минуты и километры дороги от города выезда до места.

    Строка есть только у пар, которые роутер смог построить; отсутствие
    строки — сигнал клиенту взять запасной вариант из полей места.
    """

    __tablename__ = "place_drive_times"

    place_id: Mapped[int] = mapped_column(
        ForeignKey("places.id", ondelete="CASCADE"), primary_key=True
    )
    city: Mapped[str] = mapped_column(String(32), primary_key=True)
    minutes: Mapped[int] = mapped_column(Integer)
    km: Mapped[float] = mapped_column(Float)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    place: Mapped[Place] = relationship(back_populates="drive_times")


class CityDriveTime(Base):
    """Минуты и километры дороги между городом выезда и областным хабом.

    Нужны одной строке нити — «накануне доехать до Ташкента · 4:30».
    Пар совпадения (город сам себе хаб) в таблице нет: ехать некуда.
    """

    __tablename__ = "city_drive_times"

    origin: Mapped[str] = mapped_column(String(32), primary_key=True)
    hub: Mapped[str] = mapped_column(String(32), primary_key=True)
    minutes: Mapped[int] = mapped_column(Integer)
    km: Mapped[float] = mapped_column(Float)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PlaceTrack(Base):
    """Маршрут к месту. Их может быть несколько — разные тропы к одной цели,
    и человек выбирает между ними по имени, длине и набору."""

    __tablename__ = "place_tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[int] = mapped_column(
        ForeignKey("places.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    name_uz: Mapped[str | None] = mapped_column(String(200), nullable=True)
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


class PlacePlan(Base):
    """План выхода по дням, написанный человеком.

    Расчётная нить считает световой день и про погранпост, лагерь
    и подъём в полчетвёртого не знает. План — это часы клуба: «2:00 выезд,
    5:30 пост, 14:00 лагерь». У места их может быть несколько (Коксу:
    одним днём и с ночёвкой), в карточке между ними переключатель.
    """

    __tablename__ = "place_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[int] = mapped_column(
        ForeignKey("places.id", ondelete="CASCADE"), index=True
    )
    #: «С ночёвкой» — видно только в переключателе вариантов
    title: Mapped[str] = mapped_column(String(120))
    title_uz: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Метка «черновик»: цифры ещё не подтверждены
    is_draft: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    # joined: подписи в выпадающих списках админки зовут __str__, а ленивая
    # подгрузка из async-сессии там падает на greenlet
    place: Mapped[Place] = relationship(back_populates="plans", lazy="joined")
    days: Mapped[list["PlanDay"]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="PlanDay.n",
    )

    def __str__(self) -> str:
        # Так подписан выпадающий список дней в админке
        return f"{self.place.name} · {self.title}" if self.place else self.title


class PlanDay(Base):
    """День плана: название, станции и трек, по которому идут."""

    __tablename__ = "plan_days"
    __table_args__ = (UniqueConstraint("plan_id", "n", name="uq_plan_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("place_plans.id", ondelete="CASCADE"), index=True
    )
    #: Номер дня с единицы
    n: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(120))
    title_uz: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Трек дня — ради профиля и наклеек км и набора в карточке пешего
    #: участка. Пусто — карточка без профиля. Трек удалили — день остаётся
    track_id: Mapped[int | None] = mapped_column(
        ForeignKey("place_tracks.id", ondelete="SET NULL"), nullable=True
    )
    #: День идёт по треку обратно: набор показывается спуском, профиль зеркалится
    reversed: Mapped[bool] = mapped_column(Boolean, default=False)

    plan: Mapped[PlacePlan] = relationship(back_populates="days", lazy="joined")
    track: Mapped["PlaceTrack | None"] = relationship()
    steps: Mapped[list["PlanStep"]] = relationship(
        back_populates="day",
        cascade="all, delete-orphan",
        order_by="[PlanStep.sort_order, PlanStep.id]",
    )

    def __str__(self) -> str:
        return f"{self.plan} · день {self.n} · {self.title}" if self.plan else self.title


class PlanStep(Base):
    """Станция плана. `at` и `minutes` оба необязательны: у черновика
    станции без часов, и это законно — вид решает, что показывать."""

    __tablename__ = "plan_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    day_id: Mapped[int] = mapped_column(
        ForeignKey("plan_days.id", ondelete="CASCADE"), index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[PlanStepKind] = mapped_column(
        Enum(PlanStepKind, name="plan_step_kind"), default=PlanStepKind.point
    )
    #: Время суток точки; у участков пусто
    at: Mapped[datetime_time | None] = mapped_column(Time, nullable=True)
    #: Длительность участка в минутах; у точек пусто
    minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    title_uz: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Подпись капсом под названием: «проверка пропусков»
    sub: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sub_uz: Mapped[str | None] = mapped_column(String(200), nullable=True)

    day: Mapped[PlanDay] = relationship(back_populates="steps", lazy="joined")

    def __str__(self) -> str:
        return self.title


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

    # Как всё прошло. Заполняется вечером дня выхода, когда приложение
    # спрашивает «были?»; до ответа оба поля пусты — и «не ответил»
    # должно отличаться от «ответил», поэтому nullable без default
    went: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: faster | expected | slower — насколько разошлось с расчётным временем
    pace: Mapped[str | None] = mapped_column(String(8), nullable=True)


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
    __table_args__ = (
        Index("ix_api_events_kind_ts", "kind", "ts"),
        # Частичный: у событий, выведенных сервером из запросов, номера нет,
        # а NULL в уникальном индексе Postgres и так не сравнивает
        Index(
            "ux_api_events_client_id",
            "client_id",
            unique=True,
            postgresql_where=text("client_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    # Пусто у страницы шеринга (её открывает браузер) и у клиентов,
    # которые ещё не научились слать заголовок
    device: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 32, а не 16: sticker_layout и rec_autoresume в шестнадцать не влезают
    kind: Mapped[str] = mapped_column(String(32))
    # Слаг места; у события gpx — имя файла трека, у каталога пусто.
    # У событий с телефона — ключ из перечня (см. api/events.py)
    slug: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # Случайный номер события с телефона: пачка, ушедшая дважды из-за
    # потерянного подтверждения, гасится индексом, а не логикой
    client_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class DailyStat(Base):
    """Свёртка за сутки. Живёт вечно: сырьё стирается, история — нет."""

    __tablename__ = "daily_stats"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    active_devices: Mapped[int] = mapped_column(Integer, default=0)
    new_devices: Mapped[int] = mapped_column(Integer, default=0)
    place_opens: Mapped[int] = mapped_column(Integer, default=0)
    catalog_opens: Mapped[int] = mapped_column(Integer, default=0)
    gpx_downloads: Mapped[int] = mapped_column(Integer, default=0)


class PlacePaceStats(Base):
    """Сколько людей прошли место быстрее, дольше или как в расчёте.

    Отдельно от `trip_intents`, потому что та чистится через срок
    хранения: личная отметка живёт свои тридцать дней и уходит,
    а накопленная картина по местам нужна навсегда — ради неё всё
    и затевалось. То же разделение, что у статистики просмотров:
    сырьё стирается, итоги остаются.

    Обезличено по построению: ни устройств, ни дат, только числа.
    """

    __tablename__ = "place_pace_stats"

    place_id: Mapped[int] = mapped_column(
        ForeignKey("places.id", ondelete="CASCADE"), primary_key=True
    )
    faster: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    expected: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    slower: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class TesterSignup(Base):
    """Заявка на закрытый тест Android с лендинга.

    pytest видит приставку Test* и пытается собрать класс как тестовый —
    __test__ говорит ему пройти мимо.

    Google Play пускает в тест только адреса из списка в консоли, поэтому
    вместо ссылки «скачать» на лендинге форма: почта падает сюда, владелец
    добавляет её в список руками и присылает ссылку. `invited` — галочка
    «добавил и написал», ставится в админке.
    """

    __test__ = False
    __tablename__ = "tester_signups"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    #: На каком языке была страница — на нём и писать человеку
    lang: Mapped[str] = mapped_column(String(2), default="ru", server_default="ru")
    invited: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __str__(self) -> str:
        return self.email


class Device(Base):
    """Когда устройство увидели впервые.

    Отдельная таблица, а не вывод из событий: после ротации сырья
    «новизну» определить будет не из чего, а «всего за историю» —
    это просто число строк здесь. Строка на устройство, растёт медленно.
    """

    __tablename__ = "devices"

    device: Mapped[str] = mapped_column(String(64), primary_key=True)
    first_seen: Mapped[date] = mapped_column(Date, index=True)
    # Из заголовка X-Sayr-App: последние известные значения, обновляются
    # не чаще раза в сутки. Города здесь нет и не будет — это приблизительное
    # местоположение по определениям Google, а анкета говорит «не собираем»
    platform: Mapped[str | None] = mapped_column(String(8), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    lang: Mapped[str | None] = mapped_column(String(2), nullable=True)
    os_major: Mapped[str | None] = mapped_column(String(8), nullable=True)
    last_seen: Mapped[date | None] = mapped_column(Date, nullable=True)


class DailyCount(Base):
    """Свёртка сырья за сутки по виду и ключу. Живёт вечно.

    Универсальная: ротация группирует по `kind` и `key`, какие бы виды
    ни появились, — новый вид попадает сюда без миграций. `devices` —
    уникальные устройства за день; их нельзя досуммировать потом,
    поэтому считаются при закрытии дня.
    """

    __tablename__ = "daily_counts"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    # Пустая строка, а не NULL: NULL не бывает частью первичного ключа
    key: Mapped[str] = mapped_column(String(160), primary_key=True, default="")
    events: Mapped[int] = mapped_column(Integer, default=0)
    devices: Mapped[int] = mapped_column(Integer, default=0)


class DailyPlatform(Base):
    """Активные и новые устройства за сутки по платформе. Живёт вечно."""

    __tablename__ = "daily_platform"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    # 'ios', 'android' или 'unknown' — сборки без заголовка
    platform: Mapped[str] = mapped_column(String(8), primary_key=True)
    active: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)


class CohortRetention(Base):
    """Удержание по неделям: из пришедших в неделю N сколько активны в N+k.

    Считается при закрытии недели из сырья, которого на это хватает
    (30 дней больше семи). Обезличено по построению: только числа.
    """

    __tablename__ = "cohort_retention"

    cohort_week: Mapped[date] = mapped_column(Date, primary_key=True)
    week_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    devices: Mapped[int] = mapped_column(Integer, default=0)


class PushToken(Base):
    """Установка приложения, готовая принимать пуши.

    Строка на токен. Протухший токен гасится, а не удаляется: по `disabled_at`
    видно, сколько установок отвалилось и когда. Язык и город лежат с первого
    дня — фильтры «кому слать» обещаны позже, и миграции ради них не будет
    (спека 2026-09-05-push-announcements-design.md).
    """

    __tablename__ = "push_tokens"

    token: Mapped[str] = mapped_column(String(512), primary_key=True)
    # ios / android строкой, а не enum в базе: enum в Postgres дорого расширять
    platform: Mapped[str] = mapped_column(String(8), index=True)
    # X-Device-Id из статистики — чтобы связать установку с её событиями
    device: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lang: Mapped[str] = mapped_column(String(2), default="ru", server_default="ru")
    city: Mapped[str | None] = mapped_column(String(32), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    disabled_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AnnouncementStatus(str, enum.Enum):
    """Жизнь объявления: scheduled → sending → sent | failed; cancelled — руками."""

    scheduled = "scheduled"
    sending = "sending"
    sent = "sent"
    failed = "failed"
    cancelled = "cancelled"


class Announcement(Base):
    """Уведомление из админки: заголовок, текст и когда отправить.

    `send_at` — по Ташкенту и без зоны: владелец думает во времени Ташкента,
    и форма показывает ровно то, что записано. В UTC переводит планировщик
    (app/push/sender.py), и только он.
    """

    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text)
    # По тапу открывается место — тем же путём, что напоминания
    place_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    send_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), index=True)
    status: Mapped[str] = mapped_column(
        String(16), default=AnnouncementStatus.scheduled.value, index=True
    )
    # Аудитория. Пусто — всем; в форму пока не выведено
    audience_lang: Mapped[str | None] = mapped_column(String(2), nullable=True)
    audience_city: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __str__(self) -> str:
        return self.title


class AppUpdate(Base):
    """Порог версии по платформе: ниже него приложение показывает алерт
    «нужно обновить» и не пускает дальше, пока стоит флаг `force`.

    Две строки, по одной на платформу, заводит миграция; в админке они
    только правятся. Версии сравниваются по числам через точку, а не
    строками: «1.10.0» новее «1.9.0».
    """

    __tablename__ = "app_updates"

    platform: Mapped[str] = mapped_column(String(8), primary_key=True)
    min_version: Mapped[str] = mapped_column(String(16), default="0.0.0", server_default="0.0.0")
    force: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __str__(self) -> str:
        return self.platform


class ReportStatus(str, enum.Enum):
    """Что стало с заявкой: new → taken → fixed | rejected."""

    new = "new"
    taken = "taken"
    fixed = "fixed"
    rejected = "rejected"


class PlaceReport(Base):
    """Сообщение о неточности в карточке места — с формы /report.

    Каталог собран руками по отчётам и трекам, и часть цифр в нём заведомо
    приблизительная. Дешевле всего их правит тот, кто только что сходил:
    он и присылает сюда, что не сошлось.

    `place_id` может быть пуст, и это не поломка: человек мог не найти своё
    место в списке — тогда он вписывает его словами в `place_note`, и заявка
    всё равно доходит. По той же причине пуст бывает и контакт: неверная
    координата остаётся неверной, даже если ответить автору некуда.

    Имя места не дублируется в строку заявки: место живёт в каталоге,
    а не в архиве обращений, и переименование должно быть видно и здесь.
    Удаление места оставляет заявку сиротой (ON DELETE SET NULL) —
    из двух зол потерять привязку лучше, чем потерять текст.
    """

    __tablename__ = "place_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[int | None] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Что человек вписал руками, когда места в списке не нашлось
    place_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Коды тем из app/reports.py: что именно не сходится
    topics: Mapped[list[str]] = mapped_column(
        ARRAY(String(24)), default=list, server_default="{}"
    )
    comment: Mapped[str] = mapped_column(Text, default="", server_default="")
    #: Ник в телеграме (`@name`), почта или телефон — как оставили
    contact: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Язык страницы, с которой пришли: на нём и отвечать
    lang: Mapped[str] = mapped_column(String(2), default="ru", server_default="ru")
    #: Откуда заявка. Пока только web; ручка та же и для приложения
    source: Mapped[str] = mapped_column(String(16), default="web", server_default="web")
    status: Mapped[str] = mapped_column(
        String(16), default=ReportStatus.new.value, server_default="new", index=True
    )
    #: Владелец прочитал заявку и подтвердил: так и есть, чиню.
    #:
    #: Отдельно от статуса, потому что это разные вопросы. Статус говорит,
    #: где заявка в работе; флаг — верю ли я ей вообще. Пишет их один
    #: человек, но в разное время: сначала он просматривает очередь и
    #: отмечает то, что похоже на правду, и только потом садится править.
    #: Отмеченное и есть список работ — по нему заявки и выбираются
    #: (`/admin/place-report/list?verified=1`).
    #:
    #: Неотмеченная заявка — не «плохая», а всего лишь непросмотренная:
    #: разобранное и не подтвердившееся уходит в статус rejected
    verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    #: Пометки владельца: что проверил, что поправил
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    place: Mapped["Place | None"] = relationship(lazy="selectin")
    files: Mapped[list["PlaceReportFile"]] = relationship(
        back_populates="report",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="PlaceReportFile.id",
    )

    def __str__(self) -> str:
        where = self.place.name if self.place else (self.place_note or "без места")
        return f"{where} · {self.created_at:%d.%m.%Y}" if self.created_at else where


class SeasonVote(Base):
    """Ответ игрока в игре «когда сюда идти»: дуга по кругу года.

    Сезон места собирается толпой: на сайте человеку показывают карточку
    и круг из двенадцати месяцев, он ставит два маркера — и это строка
    здесь. К месту сезон применяется не отсюда, а после одобрения
    проверяющим (спека 2026-09-11-season-game-design.md).

    Месяцы пусты — это «не знаю». Такая строка не идёт ни в подсчёт, ни
    в порог: три «не знаю» — не знание о месте. Но она остаётся, и место
    этому устройству больше не выпадает — иначе колода ходила бы по кругу.

    `voter` — случайный номер из cookie, не человек: имени, почты и входа
    в игре нет вовсе. Пара «место + номер» уникальна, поэтому второй ответ
    с того же телефона перезаписывает первый, а не добавляется.
    """

    __tablename__ = "season_votes"
    __table_args__ = (UniqueConstraint("place_id", "voter", name="uq_season_vote"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[int] = mapped_column(
        ForeignKey("places.id", ondelete="CASCADE"), index=True
    )
    #: Концы дуги, 1–12; пусто — «не знаю»
    from_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    to_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Необязательное из той же карточки: снег (только если дуга задевает
    #: зиму), опасность, ограничения, комментарий. Хранится и при «не знаю»:
    #: человек может не помнить сезон, но точно знать, что там погранзона
    snow_load: Mapped[int | None] = mapped_column(Integer, nullable=True)
    danger: Mapped[int | None] = mapped_column(Integer, nullable=True)
    limits: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), default=list, server_default="{}"
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Номер устройства из cookie
    voter: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    place: Mapped["Place"] = relationship()

    def __str__(self) -> str:
        if self.from_month is None:
            return "не знаю"
        return f"{self.from_month}–{self.to_month}"


class PlaceReportFile(Base):
    """Файл, приложенный к заявке: снимок развилки, скриншот, трек.

    Отдельной таблицей, а не колонкой-массивом: к одной заявке прикладывают
    и три кадра подряд, и у каждого своё имя, свой размер и свой тип, а
    массив строк заставил бы хранить это склейкой и разбирать её обратно.

    На диске лежит то, что прислали, байт в байт. Пережимать нечего:
    приложенное — это доказательство, и скриншот, потерявший читаемость
    после второго JPEG, доказывать перестаёт. Имя файла — от содержимого,
    поэтому один и тот же кадр, присланный дважды, копий не плодит.

    Каталог (`config.REPORTS_DIR`) наружу не отдаётся. Удаление заявки
    уносит и строку (CASCADE), и файл с диска — так обещано на /privacy.
    """

    __tablename__ = "place_report_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("place_reports.id", ondelete="CASCADE"), index=True
    )
    #: Имя на диске: хеш содержимого плюс расширение по настоящему типу
    name: Mapped[str] = mapped_column(String(80))
    #: Как файл назывался у человека — чтобы отдать его обратно с тем же
    #: именем и чтобы в админке было видно «трек.gpx», а не хеш
    original_name: Mapped[str] = mapped_column(String(200), default="", server_default="")
    content_type: Mapped[str] = mapped_column(String(60), default="", server_default="")
    size: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    report: Mapped[PlaceReport] = relationship(back_populates="files")

    def __str__(self) -> str:
        return self.original_name or self.name
