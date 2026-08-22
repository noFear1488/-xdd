"""Бесплатный источник данных: OpenStreetMap через Overpass API.

Поиск по организациям Яндекс Карт платный. OSM бесплатен, не требует ключа
и разрешает коммерческое использование при указании источника (лицензия ODbL).
Плата за это — полнота: телефон в OSM заполнен у меньшинства объектов, а
отсутствие тега website означает лишь, что сайт не внесён в карту, а не что
его нет у бизнеса. Поэтому здесь отдельно помечаются сетевые точки
(brand:wikidata) — у сети сайт почти наверняка есть.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .geo import BBox
from .models import Business

# Зеркала Overpass. Порядок — по надёжности из наблюдений; при отказе одного
# клиент переходит к следующему.
MIRRORS: tuple[str, ...] = (
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

ATTRIBUTION = "© Участники OpenStreetMap (ODbL)"

# Ключи тегов, по которым отбираются организации.
FEATURE_KEYS: tuple[str, ...] = (
    "shop",
    "amenity",
    "office",
    "craft",
    "healthcare",
    "leisure",
    "tourism",
)

SITE_TAGS: tuple[str, ...] = (
    "website",
    "contact:website",
    "url",
    "contact:url",
    "website:menu",
)

PHONE_TAGS: tuple[str, ...] = (
    "phone",
    "contact:phone",
    "contact:mobile",
    "mobile",
    "phone:mobile",
)

SOCIAL_TAGS: dict[str, str] = {
    "contact:vk": "https://vk.com/",
    "contact:telegram": "https://t.me/",
    "contact:instagram": "https://instagram.com/",
    "contact:facebook": "https://facebook.com/",
    "contact:whatsapp": "https://wa.me/",
    "contact:youtube": "https://youtube.com/",
    "contact:ok": "https://ok.ru/",
}

CHAIN_TAGS: tuple[str, ...] = ("brand:wikidata", "operator:wikidata")

# Рубрики, которые не имеет смысла обзванивать: инфраструктура, а не бизнес.
SKIP_VALUES: frozenset[str] = frozenset(
    {
        "bench", "waste_basket", "recycling", "bicycle_parking", "parking",
        "parking_space", "shelter", "drinking_water", "fountain", "toilets",
        "bus_station", "taxi", "car_sharing", "charging_station", "atm",
        "post_box", "telephone", "clock", "hunting_stand", "grave_yard",
        "place_of_worship", "school", "kindergarten", "college", "university",
        "hospital", "police", "fire_station", "townhall", "courthouse",
        "prison", "public_building", "community_centre", "library", "vacant",
        # Пункты выдачи заказов (Wildberries, Ozon и т.п.) — точки сетей,
        # а не самостоятельные бизнесы.
        "outpost", "parcel_locker", "post_depot", "money_transfer",
        "payment_terminal", "vending_machine", "bureau_de_change",
    }
)

# Человекочитаемые рубрики: в выгрузке «hairdresser» бесполезен менеджеру.
CATEGORY_TITLES: dict[str, str] = {
    "hairdresser": "парикмахерская",
    "beauty": "салон красоты",
    "cosmetics": "косметика",
    "massage": "массаж",
    "tattoo": "тату-салон",
    "nails": "маникюр",
    "dentist": "стоматология",
    "doctors": "медцентр",
    "clinic": "клиника",
    "pharmacy": "аптека",
    "veterinary": "ветклиника",
    "optician": "оптика",
    "cafe": "кафе",
    "restaurant": "ресторан",
    "bar": "бар",
    "pub": "паб",
    "fast_food": "фастфуд",
    "bakery": "пекарня",
    "confectionery": "кондитерская",
    "butcher": "мясная лавка",
    "greengrocer": "овощи и фрукты",
    "convenience": "продуктовый",
    "supermarket": "супермаркет",
    "alcohol": "алкомаркет",
    "car_repair": "автосервис",
    "car_parts": "автозапчасти",
    "car": "автосалон",
    "car_wash": "автомойка",
    "tyres": "шиномонтаж",
    "motorcycle": "мототехника",
    "driving_school": "автошкола",
    "fuel": "азс",
    "clothes": "одежда",
    "shoes": "обувь",
    "jewelry": "ювелирный",
    "bag": "сумки",
    "watches": "часы",
    "florist": "цветы",
    "gift": "подарки",
    "toys": "детские товары",
    "baby_goods": "детские товары",
    "furniture": "мебель",
    "kitchen": "кухни",
    "doityourself": "стройматериалы",
    "hardware": "хозтовары",
    "paint": "краски",
    "electronics": "электроника",
    "mobile_phone": "салон связи",
    "computer": "компьютеры",
    "sewing": "ткани и шитьё",
    "fabric": "ткани",
    "tailor": "ателье",
    "shoe_repair": "ремонт обуви",
    "laundry": "прачечная",
    "dry_cleaning": "химчистка",
    "copyshop": "полиграфия",
    "photo": "фотоуслуги",
    "travel_agency": "турагентство",
    "estate_agent": "недвижимость",
    "insurance": "страхование",
    "lawyer": "юрист",
    "accountant": "бухгалтер",
    "notary": "нотариус",
    "advertising_agency": "реклама",
    "employment_agency": "кадровое агентство",
    "company": "компания",
    "fitness_centre": "фитнес",
    "sports_centre": "спортклуб",
    "dance": "танцы",
    "pet": "зоомагазин",
    "pet_grooming": "груминг",
    "hotel": "гостиница",
    "guest_house": "гостевой дом",
    "hostel": "хостел",
    "bicycle": "велосипеды",
    "sports": "спорттовары",
    "books": "книги",
    "stationery": "канцтовары",
    "variety_store": "товары для дома",
    "e-cigarette": "вейпшоп",
    "fishing": "рыболовный",
    "garden_centre": "садовый центр",
    "funeral_directors": "ритуальные услуги",
    "locksmith": "изготовление ключей",
    "electrician": "электрик",
    "plumber": "сантехник",
    "carpenter": "столярная мастерская",
    "painter": "малярные работы",
    "window_construction": "окна",
    "shoemaker": "сапожник",
}


class OverpassError(RuntimeError):
    """Overpass не ответил или вернул ошибку."""


@dataclass
class OverpassStats:
    requests: int = 0
    elements: int = 0
    retries: int = 0
    mirror_failures: list[str] = field(default_factory=list)
    failed_groups: list[str] = field(default_factory=list)
    mirror_used: str = ""


class OverpassClient:
    """Клиент Overpass с перебором зеркал и вежливыми паузами."""

    def __init__(
        self,
        mirrors: Iterable[str] = MIRRORS,
        *,
        timeout: float = 60.0,
        query_timeout: int = 90,
        pause: float = 2.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        opener: Callable[[str, float], bytes] | None = None,
    ) -> None:
        self.mirrors = list(mirrors)
        self.timeout = timeout
        self.query_timeout = query_timeout
        self.pause = pause
        self.max_retries = max_retries
        self._sleep = sleep
        self._open = opener or _http_get
        self.stats = OverpassStats()
        # Зеркало, отказавшее один раз, обычно недоступно и дальше — не тратим
        # на него таймаут в каждом последующем запросе.
        self._dead: set[str] = set()

    # Коды, которыми Overpass просит подождать: это перегрузка, а не поломка.
    BUSY_CODES: frozenset[int] = frozenset({429, 502, 503, 504})

    def query(self, ql: str) -> list[dict[str, Any]]:
        """Выполняет запрос Overpass QL, возвращает список элементов."""
        errors: list[str] = []
        for mirror in self.mirrors:
            if _host(mirror) in self._dead:
                continue
            url = f"{mirror}?data={urllib.parse.quote(ql)}"
            payload = None
            delay = self.pause or 1.0
            for attempt in range(self.max_retries + 1):
                try:
                    raw = self._open(url, self.timeout)
                    payload = json.loads(raw.decode("utf-8"))
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code in self.BUSY_CODES and attempt < self.max_retries:
                        # Зеркало перегружено: ждём и пробуем снова — оно ещё
                        # понадобится для следующих областей.
                        self.stats.retries += 1
                        self._sleep(delay)
                        delay *= 2
                        continue
                    errors.append(f"{_host(mirror)}: HTTP {exc.code}")
                    self.stats.mirror_failures.append(_host(mirror))
                    break
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    errors.append(f"{_host(mirror)}: {exc}")
                    self.stats.mirror_failures.append(_host(mirror))
                    # Сетевой отказ — зеркало недоступно, больше не пробуем.
                    self._dead.add(_host(mirror))
                    break

            if payload is None:
                continue

            base = (payload.get("osm3s") or {}).get("timestamp_osm_base", "")
            if not _looks_like_timestamp(base):
                # Некоторые зеркала отвечают 200 с пустой базой — это не данные.
                errors.append(f"{_host(mirror)}: зеркало отдаёт пустую базу")
                self.stats.mirror_failures.append(_host(mirror))
                self._dead.add(_host(mirror))
                continue

            self.stats.requests += 1
            self.stats.mirror_used = _host(mirror)
            elements = payload.get("elements") or []
            self.stats.elements += len(elements)
            return elements

        if not errors:
            errors.append("все зеркала помечены недоступными")
        raise OverpassError(
            "ни одно зеркало Overpass не ответило: " + "; ".join(errors[:4])
        )

    def fetch_area(self, area: BBox, keys: Iterable[str] = FEATURE_KEYS) -> list[dict]:
        """Забирает организации области, по одному запросу на группу тегов."""
        seen: set[tuple[str, int]] = set()
        collected: list[dict] = []
        for key in keys:
            ql = (
                f"[out:json][timeout:{self.query_timeout}];"
                f'nwr["name"]["{key}"]({_bbox(area)});out center;'
            )
            try:
                elements = self.query(ql)
            except OverpassError as exc:
                # Одна группа может не даться из-за лимитов зеркала — не повод
                # терять остальные, но и молчать об этом нельзя.
                self.stats.failed_groups.append(f"{key}: {exc}")
                continue
            for element in elements:
                ident = (element.get("type", ""), int(element.get("id", 0)))
                if ident in seen:
                    continue
                seen.add(ident)
                collected.append(element)
            if self.pause:
                self._sleep(self.pause)
        return collected


def _http_get(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "yandex-nosite/0.1 (OSM lead finder)"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read()


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc


def _bbox(area: BBox) -> str:
    """Overpass принимает bbox как south,west,north,east."""
    return (
        f"{area.lat_min:.6f},{area.lon_min:.6f},{area.lat_max:.6f},{area.lon_max:.6f}"
    )


def _looks_like_timestamp(value: str) -> bool:
    return len(value) >= 10 and value[:4].isdigit() and value[4:5] == "-"


# --- преобразование в Business ----------------------------------------------


def category_of(tags: dict[str, str]) -> tuple[str, str]:
    """Возвращает (техническое значение, русское название рубрики)."""
    for key in FEATURE_KEYS:
        value = tags.get(key)
        if value and value != "yes":
            return value, CATEGORY_TITLES.get(value, value.replace("_", " "))
    return "", ""


def is_chain(tags: dict[str, str]) -> bool:
    """Сетевая точка: у сети сайт почти наверняка есть, просто не внесён в OSM."""
    return any(key in tags for key in CHAIN_TAGS)


def to_business(element: dict[str, Any], query: str = "osm") -> Business | None:
    """Преобразует элемент Overpass в Business."""
    tags = element.get("tags") or {}
    name = (tags.get("name") or "").strip()
    if not name:
        return None

    value, title = category_of(tags)
    if not value or value in SKIP_VALUES:
        return None

    center = element.get("center") or {}
    lat = element.get("lat", center.get("lat"))
    lon = element.get("lon", center.get("lon"))

    phones: list[str] = []
    for key in PHONE_TAGS:
        raw = tags.get(key)
        if not raw:
            continue
        for part in raw.replace(",", ";").split(";"):
            phone = part.strip()
            if phone and phone not in phones:
                phones.append(phone)

    url = ""
    for key in SITE_TAGS:
        if tags.get(key):
            url = tags[key].strip()
            break

    links: list[str] = []
    for key, prefix in SOCIAL_TAGS.items():
        raw = (tags.get(key) or "").strip()
        if not raw:
            continue
        links.append(raw if raw.startswith("http") else prefix + raw.lstrip("@/"))

    address = " ".join(
        part
        for part in (
            tags.get("addr:city", ""),
            tags.get("addr:street", ""),
            tags.get("addr:housenumber", ""),
        )
        if part
    ).strip()

    business = Business(
        company_id=f"osm:{element.get('type','')}/{element.get('id','')}",
        name=name,
        address=address,
        lon=float(lon) if lon is not None else None,
        lat=float(lat) if lat is not None else None,
        url=url,
        links=links,
        phones=phones,
        categories=[title] if title else [],
        hours=(tags.get("opening_hours") or "").strip(),
        query=query,
    )
    if is_chain(tags):
        # Помечаем прямо в рубрике: пользователь увидит это в выгрузке.
        business.categories.append("сетевая точка")
    return business


def osm_url(business: Business) -> str:
    ident = business.company_id.replace("osm:", "")
    return f"https://www.openstreetmap.org/{ident}" if ident else ""
