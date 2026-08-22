"""Модель организации, полученной из Яндекс Карт."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Business:
    """Организация с карточки Яндекс Карт, приведённая к плоскому виду."""

    company_id: str
    name: str
    address: str = ""
    lon: float | None = None
    lat: float | None = None
    url: str = ""
    links: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    hours: str = ""
    rating: float | None = None
    reviews: int | None = None
    closed: bool = False
    query: str = ""
    site_status: str = ""
    site_domain: str = ""
    lead_score: int = 0
    score_reasons: list[str] = field(default_factory=list)

    @property
    def phone(self) -> str:
        return self.phones[0] if self.phones else ""

    @property
    def category(self) -> str:
        return self.categories[0] if self.categories else ""

    @property
    def maps_url(self) -> str:
        """Ссылка, по которой менеджер найдёт организацию на карте.

        У записей Яндекса есть свой id карточки. У записей OSM его нет, поэтому
        ссылка ведёт на поиск по названию и адресу — заодно видно, как
        организация выглядит в Яндексе и не появился ли у неё сайт.
        """
        if not self.company_id:
            return ""
        if self.company_id.startswith("osm:"):
            query = ", ".join(part for part in (self.name, self.address) if part)
            return "https://yandex.ru/maps/?text=" + urllib.parse.quote(query)
        return f"https://yandex.ru/maps/org/{self.company_id}/"

    @property
    def source_url(self) -> str:
        """Ссылка на исходный объект в источнике данных."""
        if self.company_id.startswith("osm:"):
            return "https://www.openstreetmap.org/" + self.company_id[4:]
        return self.maps_url

    def to_row(self) -> dict[str, Any]:
        """Плоская строка для CSV/XLSX — в порядке, удобном для обзвона."""
        return {
            "name": self.name,
            "category": self.category,
            "phone": self.phone,
            "all_phones": "; ".join(self.phones),
            "address": self.address,
            "site_status": self.site_status,
            "url": self.url,
            "site_domain": self.site_domain,
            "links": "; ".join(self.links),
            "lead_score": self.lead_score,
            "score_reasons": "; ".join(self.score_reasons),
            "rating": "" if self.rating is None else self.rating,
            "reviews": "" if self.reviews is None else self.reviews,
            "hours": self.hours,
            "closed": "да" if self.closed else "нет",
            "lon": "" if self.lon is None else self.lon,
            "lat": "" if self.lat is None else self.lat,
            "maps_url": self.maps_url,
            "source_url": self.source_url,
            "company_id": self.company_id,
            "query": self.query,
        }

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["maps_url"] = self.maps_url
        data["source_url"] = self.source_url
        return data


ROW_FIELDS: list[str] = [
    "name",
    "category",
    "phone",
    "all_phones",
    "address",
    "site_status",
    "url",
    "site_domain",
    "links",
    "lead_score",
    "score_reasons",
    "rating",
    "reviews",
    "hours",
    "closed",
    "lon",
    "lat",
    "maps_url",
    "source_url",
    "company_id",
    "query",
]


def parse_feature(feature: dict[str, Any], query: str = "") -> Business | None:
    """Преобразует GeoJSON-объект ответа Яндекса в Business.

    Возвращает None, если у объекта нет карточки организации (например, это топоним).
    """
    props = feature.get("properties") or {}
    meta = props.get("CompanyMetaData") or {}
    company_id = str(meta.get("id") or "").strip()
    name = (meta.get("name") or props.get("name") or "").strip()
    if not company_id and not name:
        return None

    coords = (feature.get("geometry") or {}).get("coordinates") or []
    lon = float(coords[0]) if len(coords) >= 2 else None
    lat = float(coords[1]) if len(coords) >= 2 else None

    phones: list[str] = []
    for phone in meta.get("Phones") or []:
        value = (phone.get("formatted") or phone.get("number") or "").strip()
        if value and value not in phones:
            phones.append(value)

    categories: list[str] = []
    for category in meta.get("Categories") or []:
        value = (category.get("name") or category.get("class") or "").strip()
        if value and value not in categories:
            categories.append(value)

    links: list[str] = []
    for link in meta.get("Links") or []:
        value = (link.get("href") or link.get("link") or "").strip()
        if value and value not in links:
            links.append(value)

    hours_block = meta.get("Hours") or {}
    hours = (hours_block.get("text") or "").strip()

    rating_block = meta.get("Rating") or {}
    rating = _as_float(rating_block.get("score") or rating_block.get("value"))
    reviews = _as_int(rating_block.get("reviews") or rating_block.get("ratings"))

    closed = bool(meta.get("Closed") or meta.get("closed"))

    return Business(
        company_id=company_id or f"noid:{name}:{lon}:{lat}",
        name=name,
        address=(meta.get("address") or props.get("description") or "").strip(),
        lon=lon,
        lat=lat,
        url=(meta.get("url") or "").strip(),
        links=links,
        phones=phones,
        categories=categories,
        hours=hours,
        rating=rating,
        reviews=reviews,
        closed=closed,
        query=query,
    )


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
