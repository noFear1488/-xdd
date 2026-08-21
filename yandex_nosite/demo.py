"""Демо-транспорт: эмулирует ответы Yandex Search API без ключа и сети.

Нужен для двух вещей: посмотреть весь конвейер и формат выгрузки до получения
ключа, и прогонять тесты, не завися от внешнего сервиса. Данные синтетические,
но структура ответа повторяет настоящую.
"""

from __future__ import annotations

import json
import random
import urllib.parse
from typing import Any

from .api import MAX_RESULTS_PER_REQUEST
from .geo import BBox

_NAME_PARTS_A = [
    "Уют", "Мастер", "Сфера", "Гармония", "Эталон", "Профи", "Лидер", "Формула",
    "Ритм", "Стиль", "Атмосфера", "Вершина", "Импульс", "Компас", "Оазис",
]
_NAME_PARTS_B = [
    "плюс", "сервис", "групп", "центр", "студия", "мастерская", "дом", "клуб",
]

_SITE_POOL = [
    "",  # сайта нет
    "",
    "",
    "https://vk.com/club{n}",
    "https://t.me/company{n}",
    "https://zoon.ru/msk/place_{n}/",
    "https://company{n}.tilda.ws",
    "https://company{n}.ru",
    "https://www.company{n}.ru/",
    "https://xn--80abc{n}.рф",
]

_HOURS = ["ежедневно, 9:00–21:00", "пн-пт 10:00–19:00", "круглосуточно", ""]


class DemoTransport:
    """Отдаёт стабильный набор организаций, разложенный по области поиска."""

    def __init__(self, per_query: int = 240, seed: int = 20260821) -> None:
        self.per_query = per_query
        self.seed = seed
        self.calls = 0
        self._cache: dict[str, list[dict[str, Any]]] = {}

    def get(self, url: str, timeout: float) -> tuple[int, bytes]:
        self.calls += 1
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        text = params.get("text", "")
        area = BBox.parse(params.get("bbox", "37.3,55.5~37.9,55.9"))
        results = int(params.get("results", MAX_RESULTS_PER_REQUEST))
        skip = int(params.get("skip", 0))

        pool = self._pool(text)
        inside = [
            feature
            for feature in pool
            if area.contains(*feature["geometry"]["coordinates"])
        ]
        window = inside[skip : skip + results]
        payload = {
            "type": "FeatureCollection",
            "properties": {
                "ResponseMetaData": {
                    "SearchResponse": {
                        "found": len(inside),
                        "display": "multiple",
                        "request": text,
                    }
                }
            },
            "features": window,
        }
        return 200, json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _pool(self, text: str) -> list[dict[str, Any]]:
        """Организации для рубрики — одни и те же при каждом запросе."""
        if text in self._cache:
            return self._cache[text]
        rng = random.Random(f"{self.seed}:{text}")
        # Организации размещаются по всей Москве; область поиска вырезает нужное.
        world = BBox(37.32, 55.55, 37.90, 55.92)
        features = []
        for index in range(self.per_query):
            lon = rng.uniform(world.lon_min, world.lon_max)
            lat = rng.uniform(world.lat_min, world.lat_max)
            site_template = rng.choice(_SITE_POOL)
            site = site_template.format(n=1000 + index) if site_template else ""
            has_phone = rng.random() > 0.12
            reviews = rng.choice([0, 0, 3, 7, 12, 25, 48, 140])
            name = (
                f"{rng.choice(_NAME_PARTS_A)}-{rng.choice(_NAME_PARTS_B)} "
                f"№{index + 1}"
            )
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {
                        "name": name,
                        "description": "Москва, демо-данные",
                        "CompanyMetaData": {
                            "id": f"demo-{abs(hash((text, index))) % 10**10}",
                            "name": name,
                            "address": f"Москва, ул. Примерная, д. {index % 90 + 1}",
                            "url": site,
                            "Phones": (
                                [
                                    {
                                        "type": "phone",
                                        "formatted": f"+7 (495) {rng.randint(100, 999)}-"
                                        f"{rng.randint(10, 99)}-{rng.randint(10, 99)}",
                                    }
                                ]
                                if has_phone
                                else []
                            ),
                            "Categories": [{"class": "biz", "name": text}],
                            "Hours": {"text": rng.choice(_HOURS)},
                            "Rating": {
                                "score": round(rng.uniform(3.4, 5.0), 1),
                                "reviews": reviews,
                            },
                        },
                    },
                }
            )
        self._cache[text] = features
        return features
