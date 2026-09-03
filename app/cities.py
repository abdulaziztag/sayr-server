"""Справочник городов выезда — статика, не таблица.

Это справочник, а не данные: нужен клиентам целиком (они выбирают город
локально и офлайн), меняется редко и руками владельца. Ташкентская область
взята городами целиком — ради её пригородов разница и затевалась: из Чирчика
до Чимгана 40 минут, из Ташкента полтора часа. У остальных областей хватает
центров: там пригород от центра по дороге не отличается.

Падежные формы («из Ташкента», «Toshkentdan») лежат здесь, а не собираются
правилами в коде: несклоняемые «из Навои» и «из Карши» правил не переживут.
Координаты — центры городов по OpenStreetMap.
"""

from dataclasses import dataclass

TASHKENT_AREA_RU = "Ташкентская область"
TASHKENT_AREA_UZ = "Toshkent viloyati"
OTHER_AREA_RU = "Другие области"
OTHER_AREA_UZ = "Boshqa viloyatlar"


@dataclass(frozen=True)
class City:
    code: str
    name_ru: str
    name_uz: str
    from_ru: str
    from_uz: str
    lat: float
    lng: float
    area_ru: str
    area_uz: str
    #: Радиус, внутри которого точка считается этим городом раньше «ближайшего»:
    #: пригороды ближе к районам Ташкента, чем его центр
    radius_km: float = 0.0


def _tashkent_area(code, ru, uz, from_ru, from_uz, lat, lng, radius_km=0.0) -> City:
    return City(code, ru, uz, from_ru, from_uz, lat, lng, TASHKENT_AREA_RU, TASHKENT_AREA_UZ, radius_km)


def _center(code, ru, uz, from_ru, from_uz, lat, lng) -> City:
    return City(code, ru, uz, from_ru, from_uz, lat, lng, OTHER_AREA_RU, OTHER_AREA_UZ)


CITIES: tuple[City, ...] = (
    # --- Ташкентская область: город и его спутники ---
    _tashkent_area("tashkent", "Ташкент", "Toshkent", "из Ташкента", "Toshkentdan", 41.3111, 69.2797, radius_km=14.0),
    _tashkent_area("chirchiq", "Чирчик", "Chirchiq", "из Чирчика", "Chirchiqdan", 41.4689, 69.5822),
    _tashkent_area("gazalkent", "Газалкент", "Gʻazalkent", "из Газалкента", "Gʻazalkentdan", 41.5581, 69.7708),
    _tashkent_area("kibray", "Кибрай", "Qibray", "из Кибрая", "Qibraydan", 41.3897, 69.4653),
    _tashkent_area("angren", "Ангрен", "Angren", "из Ангрена", "Angrendan", 41.0167, 70.1436),
    _tashkent_area("akhangaran", "Ахангаран", "Ohangaron", "из Ахангарана", "Ohangarondan", 40.9078, 69.6394),
    _tashkent_area("almalyk", "Алмалык", "Olmaliq", "из Алмалыка", "Olmaliqdan", 40.8444, 69.5983),
    _tashkent_area("bekabad", "Бекабад", "Bekobod", "из Бекабада", "Bekoboddan", 40.2206, 69.2697),
    _tashkent_area("yangiyul", "Янгиюль", "Yangiyoʻl", "из Янгиюля", "Yangiyoʻldan", 41.1119, 69.0472),
    _tashkent_area("nurafshon", "Нурафшон", "Nurafshon", "из Нурафшона", "Nurafshondan", 41.0431, 69.3583),
    _tashkent_area("parkent", "Паркент", "Parkent", "из Паркента", "Parkentdan", 41.2947, 69.6767),
    _tashkent_area("pskent", "Пскент", "Piskent", "из Пскента", "Piskentdan", 40.8967, 69.3494),
    _tashkent_area("chinaz", "Чиназ", "Chinoz", "из Чиназа", "Chinozdan", 40.9364, 68.7639),
    _tashkent_area("buka", "Бука", "Boʻka", "из Буки", "Boʻkadan", 40.8106, 69.1978),
    _tashkent_area("keles", "Келес", "Keles", "из Келеса", "Kelesdan", 41.4008, 69.2064),
    _tashkent_area("zangiata", "Зангиата", "Zangiota", "из Зангиаты", "Zangiotadan", 41.2058, 69.1461),
    # --- Центры остальных областей ---
    _center("nukus", "Нукус", "Nukus", "из Нукуса", "Nukusdan", 42.4531, 59.6103),
    _center("urgench", "Ургенч", "Urganch", "из Ургенча", "Urganchdan", 41.5500, 60.6333),
    _center("bukhara", "Бухара", "Buxoro", "из Бухары", "Buxorodan", 39.7747, 64.4286),
    _center("navoi", "Навои", "Navoiy", "из Навои", "Navoiydan", 40.0844, 65.3792),
    _center("samarkand", "Самарканд", "Samarqand", "из Самарканда", "Samarqanddan", 39.6542, 66.9597),
    _center("jizzakh", "Джизак", "Jizzax", "из Джизака", "Jizzaxdan", 40.1158, 67.8422),
    _center("gulistan", "Гулистан", "Guliston", "из Гулистана", "Gulistondan", 40.4897, 68.7842),
    _center("karshi", "Карши", "Qarshi", "из Карши", "Qarshidan", 38.8606, 65.7891),
    _center("termez", "Термез", "Termiz", "из Термеза", "Termizdan", 37.2242, 67.2783),
    _center("namangan", "Наманган", "Namangan", "из Намангана", "Namangandan", 40.9983, 71.6726),
    _center("andijan", "Андижан", "Andijon", "из Андижана", "Andijondan", 40.7821, 72.3442),
    _center("fergana", "Фергана", "Fargʻona", "из Ферганы", "Fargʻonadan", 40.3842, 71.7843),
)

TASHKENT = CITIES[0]
BY_CODE = {c.code: c for c in CITIES}
assert len(BY_CODE) == len(CITIES), "коды городов должны быть уникальны"
