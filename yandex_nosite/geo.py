"""Работа с географическими областями: bbox, деление на тайлы, справочник городов."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator

EARTH_KM_PER_DEG_LAT = 111.32


@dataclass(frozen=True)
class BBox:
    """Прямоугольная область поиска в градусах WGS84."""

    lon_min: float
    lat_min: float
    lon_max: float
    lat_max: float

    def __post_init__(self) -> None:
        if self.lon_min >= self.lon_max or self.lat_min >= self.lat_max:
            raise ValueError(
                f"некорректный bbox: {self.lon_min},{self.lat_min}~{self.lon_max},{self.lat_max}"
            )

    @property
    def width(self) -> float:
        return self.lon_max - self.lon_min

    @property
    def height(self) -> float:
        return self.lat_max - self.lat_min

    @property
    def center(self) -> tuple[float, float]:
        return ((self.lon_min + self.lon_max) / 2, (self.lat_min + self.lat_max) / 2)

    def contains(self, lon: float, lat: float) -> bool:
        return self.lon_min <= lon <= self.lon_max and self.lat_min <= lat <= self.lat_max

    def to_api(self) -> str:
        """Формат параметра bbox для Yandex Search API: lon,lat~lon,lat."""
        return (
            f"{self.lon_min:.6f},{self.lat_min:.6f}~{self.lon_max:.6f},{self.lat_max:.6f}"
        )

    def to_ll_spn(self) -> tuple[str, str]:
        """Альтернативная адресация области: центр + протяжённость."""
        lon, lat = self.center
        return f"{lon:.6f},{lat:.6f}", f"{self.width:.6f},{self.height:.6f}"

    def km_size(self) -> tuple[float, float]:
        """Приблизительные размеры области в километрах (ширина, высота)."""
        _, lat = self.center
        km_per_deg_lon = EARTH_KM_PER_DEG_LAT * math.cos(math.radians(lat))
        return self.width * km_per_deg_lon, self.height * EARTH_KM_PER_DEG_LAT

    def split(self) -> list["BBox"]:
        """Делит область на 4 равных квадранта (квадродерево)."""
        lon_mid = (self.lon_min + self.lon_max) / 2
        lat_mid = (self.lat_min + self.lat_max) / 2
        return [
            BBox(self.lon_min, self.lat_min, lon_mid, lat_mid),
            BBox(lon_mid, self.lat_min, self.lon_max, lat_mid),
            BBox(self.lon_min, lat_mid, lon_mid, self.lat_max),
            BBox(lon_mid, lat_mid, self.lon_max, self.lat_max),
        ]

    @classmethod
    def parse(cls, raw: str) -> "BBox":
        """Разбирает строку вида '36.83,55.67~38.24,55.91' или '36.83,55.67,38.24,55.91'."""
        text = raw.strip().replace("~", ",")
        parts = [p for p in text.replace(";", ",").split(",") if p.strip()]
        if len(parts) != 4:
            raise ValueError(
                "bbox задаётся четырьмя числами: lon_min,lat_min~lon_max,lat_max"
            )
        lon_min, lat_min, lon_max, lat_max = (float(p) for p in parts)
        return cls(
            min(lon_min, lon_max),
            min(lat_min, lat_max),
            max(lon_min, lon_max),
            max(lat_min, lat_max),
        )

    @classmethod
    def from_center(cls, lon: float, lat: float, radius_km: float) -> "BBox":
        """Строит квадрат со стороной 2*radius_km вокруг точки."""
        if radius_km <= 0:
            raise ValueError("радиус должен быть положительным")
        d_lat = radius_km / EARTH_KM_PER_DEG_LAT
        cos_lat = max(math.cos(math.radians(lat)), 1e-6)
        d_lon = radius_km / (EARTH_KM_PER_DEG_LAT * cos_lat)
        return cls(lon - d_lon, lat - d_lat, lon + d_lon, lat + d_lat)


def iter_grid(area: BBox, rows: int, cols: int) -> Iterator[BBox]:
    """Равномерная сетка — используется, когда адаптивное деление не нужно."""
    if rows < 1 or cols < 1:
        raise ValueError("размер сетки должен быть не меньше 1x1")
    step_lon = area.width / cols
    step_lat = area.height / rows
    for row in range(rows):
        for col in range(cols):
            yield BBox(
                area.lon_min + col * step_lon,
                area.lat_min + row * step_lat,
                area.lon_min + (col + 1) * step_lon,
                area.lat_min + (row + 1) * step_lat,
            )


# Встроенный справочник городов: приблизительные границы застройки.
REGIONS: dict[str, BBox] = {
    "moscow": BBox(37.32, 55.55, 37.90, 55.92),
    "spb": BBox(30.10, 59.80, 30.55, 60.09),
    "novosibirsk": BBox(82.75, 54.90, 83.15, 55.15),
    "ekaterinburg": BBox(60.45, 56.72, 60.75, 56.93),
    "kazan": BBox(48.98, 55.72, 49.30, 55.88),
    "nizhny-novgorod": BBox(43.79, 56.20, 44.13, 56.38),
    "chelyabinsk": BBox(61.25, 55.05, 61.55, 55.25),
    "samara": BBox(50.05, 53.15, 50.35, 53.30),
    "omsk": BBox(73.20, 54.90, 73.50, 55.06),
    "rostov-on-don": BBox(39.55, 47.19, 39.85, 47.32),
    "ufa": BBox(55.90, 54.65, 56.15, 54.85),
    "krasnoyarsk": BBox(92.72, 55.97, 93.10, 56.11),
    "voronezh": BBox(39.09, 51.60, 39.35, 51.76),
    "perm": BBox(56.10, 57.95, 56.40, 58.08),
    "volgograd": BBox(44.35, 48.60, 44.62, 48.82),
    "krasnodar": BBox(38.90, 44.98, 39.15, 45.13),
    "saratov": BBox(45.90, 51.45, 46.10, 51.62),
    "tyumen": BBox(65.40, 57.09, 65.65, 57.20),
    "sochi": BBox(39.60, 43.53, 39.85, 43.70),
    "kaliningrad": BBox(20.35, 54.62, 20.63, 54.77),
    "vladivostok": BBox(131.83, 43.06, 132.02, 43.20),
    "minsk": BBox(27.40, 53.83, 27.72, 53.99),
    "almaty": BBox(76.82, 43.18, 77.05, 43.31),
    "astana": BBox(71.32, 51.06, 71.55, 51.20),
    "tashkent": BBox(69.15, 41.22, 69.40, 41.38),
    "yerevan": BBox(44.42, 40.12, 44.58, 40.24),
    "tbilisi": BBox(44.72, 41.66, 44.90, 41.80),
}

REGION_TITLES: dict[str, str] = {
    "moscow": "Москва",
    "spb": "Санкт-Петербург",
    "novosibirsk": "Новосибирск",
    "ekaterinburg": "Екатеринбург",
    "kazan": "Казань",
    "nizhny-novgorod": "Нижний Новгород",
    "chelyabinsk": "Челябинск",
    "samara": "Самара",
    "omsk": "Омск",
    "rostov-on-don": "Ростов-на-Дону",
    "ufa": "Уфа",
    "krasnoyarsk": "Красноярск",
    "voronezh": "Воронеж",
    "perm": "Пермь",
    "volgograd": "Волгоград",
    "krasnodar": "Краснодар",
    "saratov": "Саратов",
    "tyumen": "Тюмень",
    "sochi": "Сочи",
    "kaliningrad": "Калининград",
    "vladivostok": "Владивосток",
    "minsk": "Минск",
    "almaty": "Алматы",
    "astana": "Астана",
    "tashkent": "Ташкент",
    "yerevan": "Ереван",
    "tbilisi": "Тбилиси",
}


def resolve_region(name: str) -> BBox:
    """Ищет регион в справочнике по ключу или русскому названию."""
    key = name.strip().lower()
    if key in REGIONS:
        return REGIONS[key]
    for slug, title in REGION_TITLES.items():
        if title.lower() == key:
            return REGIONS[slug]
    raise KeyError(name)
