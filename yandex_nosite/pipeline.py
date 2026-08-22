"""Сканирование области: адаптивная геосетка, дедупликация, фильтр и скоринг.

API отдаёт не больше 50 объектов на запрос, поэтому по большому городу «одним
запросом» собрать всё нельзя. Область делится на квадранты рекурсивно: тайл
дробится, только если в нём объектов больше, чем помещается в ответ. Так запросы
тратятся там, где плотность высокая, и не тратятся на пустые окраины.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from .api import (
    MAX_RESULTS_PER_QUERY,
    MAX_RESULTS_PER_REQUEST,
    BudgetExhausted,
    QuotaError,
    SearchClient,
)
from .geo import BBox
from .models import Business, parse_feature
from .scoring import apply_score
from .osm import OverpassClient, OverpassError, to_business
from .sitecheck import LEAD_STATUSES, classify, probe_many

ProgressFn = Callable[[dict], None]


@dataclass
class ScanConfig:
    """Параметры одного прогона сканирования."""

    queries: list[str]
    area: BBox
    region: str = ""
    lead_statuses: tuple[str, ...] = LEAD_STATUSES
    max_depth: int = 4
    min_tile_km: float = 0.5
    page_size: int = MAX_RESULTS_PER_REQUEST
    max_results_per_tile: int = MAX_RESULTS_PER_QUERY
    deep_paging: bool = True
    verify_sites: bool = False
    verify_workers: int = 8
    verify_timeout: float = 6.0
    include_closed: bool = False
    min_score: int = 0
    with_phone: bool = False
    no_chains: bool = False
    limit: int | None = None


@dataclass
class ScanResult:
    """Итог прогона."""

    leads: list[Business] = field(default_factory=list)
    seen_total: int = 0
    with_site: int = 0
    skipped_closed: int = 0
    duplicate_hits: int = 0
    tiles_visited: int = 0
    requests: int = 0
    cache_hits: int = 0
    stopped_early: bool = False
    stop_reason: str = ""
    per_query: dict[str, int] = field(default_factory=dict)

    @property
    def lead_share(self) -> float:
        return len(self.leads) / self.seen_total * 100 if self.seen_total else 0.0


def scan(
    client: SearchClient,
    config: ScanConfig,
    progress: ProgressFn | None = None,
) -> ScanResult:
    """Обходит область по всем запросам и возвращает отфильтрованных лидов."""
    result = ScanResult()
    collected: dict[str, Business] = {}
    # Тайлы соседствуют, а рубрики пересекаются, поэтому одна организация
    # приходит в ответах не раз; считаем её просмотренной только однажды.
    seen: set[str] = set()
    emit = progress or (lambda event: None)

    try:
        for query in config.queries:
            before = len(collected)
            _scan_query(client, config, query, collected, seen, result, emit)
            result.per_query[query] = len(collected) - before
            if config.limit and len(collected) >= config.limit:
                result.stopped_early = True
                result.stop_reason = f"достигнут лимит в {config.limit} организаций"
                break
    except (BudgetExhausted, QuotaError) as exc:
        result.stopped_early = True
        result.stop_reason = str(exc)
        emit({"type": "stopped", "reason": str(exc)})

    result.requests = client.stats["requests"]
    result.cache_hits = client.stats["cache_hits"]

    leads = list(collected.values())
    if config.verify_sites:
        _verify(leads, config, emit)
    else:
        for business in leads:
            apply_score(business)

    leads = [b for b in leads if b.lead_score >= config.min_score]
    if config.with_phone:
        leads = [b for b in leads if b.phones]
    if config.no_chains:
        leads = [b for b in leads if "сетевая точка" not in b.categories]
    leads = dedupe_similar(leads)
    leads.sort(key=lambda b: (-b.lead_score, -(b.reviews or 0), b.name))
    if config.limit:
        leads = leads[: config.limit]
    result.leads = leads
    return result


def scan_osm(
    client: OverpassClient,
    config: ScanConfig,
    progress: ProgressFn | None = None,
) -> ScanResult:
    """Собирает лидов из OpenStreetMap — бесплатно и без ключа.

    Overpass отдаёт всю область целиком, поэтому адаптивная сетка здесь не нужна:
    делить область приходится только если запрос не укладывается в таймаут.
    """
    result = ScanResult()
    collected: dict[str, Business] = {}
    seen: set[str] = set()
    emit = progress or (lambda event: None)

    emit({"type": "osm_start", "area": config.area.to_api()})
    try:
        elements = client.fetch_area(config.area)
    except OverpassError as exc:
        result.stopped_early = True
        result.stop_reason = str(exc)
        emit({"type": "stopped", "reason": str(exc)})
        return result

    result.tiles_visited = client.stats.requests
    result.requests = client.stats.requests
    emit(
        {
            "type": "osm_fetched",
            "elements": len(elements),
            "mirror": client.stats.mirror_used,
        }
    )

    for element in elements:
        business = to_business(element, query=config.region or "osm")
        if business is None:
            continue
        if business.company_id in seen:
            result.duplicate_hits += 1
            continue
        seen.add(business.company_id)
        result.seen_total += 1

        status, domain = classify(business.url, business.links)
        business.site_status = status
        business.site_domain = domain
        if status not in config.lead_statuses:
            result.with_site += 1
            continue
        collected[business.company_id] = business

    leads = list(collected.values())
    if config.verify_sites:
        _verify(leads, config, emit)
    else:
        for business in leads:
            apply_score(business)

    leads = [b for b in leads if b.lead_score >= config.min_score]
    if config.with_phone:
        leads = [b for b in leads if b.phones]
    if config.no_chains:
        leads = [b for b in leads if "сетевая точка" not in b.categories]
    leads = dedupe_similar(leads)
    leads.sort(key=lambda b: (-b.lead_score, b.name))
    if config.limit:
        leads = leads[: config.limit]
    result.leads = leads
    result.per_query[config.region or "osm"] = len(leads)
    return result


def dedupe_similar(businesses: list[Business]) -> list[Business]:
    """Убирает повторы одного бизнеса.

    В OSM одна организация нередко присутствует дважды: точкой и контуром
    здания, с разными id. Одинаковые название и телефон — это один бизнес;
    оставляем запись с большим объёмом данных.
    """
    best: dict[tuple[str, str], Business] = {}
    order: list[tuple[str, str]] = []
    for business in businesses:
        key = (
            " ".join(business.name.lower().split()),
            _digits(business.phone),
        )
        if not key[0]:
            continue
        current = best.get(key)
        if current is None:
            best[key] = business
            order.append(key)
        elif _richness(business) > _richness(current):
            best[key] = business
    return [best[key] for key in order]


def _digits(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit())


def _richness(business: Business) -> int:
    """Сколько полезных полей заполнено — по этому выбирается лучший дубль."""
    return sum(
        bool(x)
        for x in (
            business.address,
            business.hours,
            business.phones,
            business.url,
            business.links,
            business.categories,
        )
    )


def _scan_query(
    client: SearchClient,
    config: ScanConfig,
    query: str,
    collected: dict[str, Business],
    seen: set[str],
    result: ScanResult,
    emit: ProgressFn,
) -> None:
    stack: list[tuple[BBox, int]] = [(config.area, 0)]
    emit({"type": "query_start", "query": query})

    while stack:
        tile, depth = stack.pop()
        response = client.search(query, tile, results=config.page_size)
        result.tiles_visited += 1

        harvested = _harvest(response.features, query, config, collected, seen, result)
        emit(
            {
                "type": "tile",
                "query": query,
                "depth": depth,
                "found": response.found,
                "returned": len(response.features),
                "new_leads": harvested,
                "leads_total": len(collected),
                "tile": tile.to_api(),
                "cached": response.from_cache,
            }
        )

        if config.limit and len(collected) >= config.limit:
            return

        if _should_split(response.found, tile, depth, config):
            stack.extend((sub, depth + 1) for sub in tile.split())
            continue

        if config.deep_paging:
            _paginate(
                client, config, query, tile, response.found, collected, seen, result, emit
            )


def _should_split(found: int, tile: BBox, depth: int, config: ScanConfig) -> bool:
    """Дробить тайл имеет смысл, только если в него не помещаются все объекты."""
    if found <= config.page_size:
        return False
    if depth >= config.max_depth:
        return False
    width_km, height_km = tile.km_size()
    return min(width_km, height_km) > config.min_tile_km * 2


def _paginate(
    client: SearchClient,
    config: ScanConfig,
    query: str,
    tile: BBox,
    found: int,
    collected: dict[str, Business],
    seen: set[str],
    result: ScanResult,
    emit: ProgressFn,
) -> None:
    """Добирает оставшиеся страницы тайла, который решено не дробить."""
    target = min(found, config.max_results_per_tile)
    fetched = config.page_size
    while fetched < target:
        response = client.search(query, tile, results=config.page_size, skip=fetched)
        if not response.features:
            return
        harvested = _harvest(response.features, query, config, collected, seen, result)
        emit(
            {
                "type": "page",
                "query": query,
                "skip": fetched,
                "returned": len(response.features),
                "new_leads": harvested,
                "leads_total": len(collected),
            }
        )
        fetched += len(response.features)
        if config.limit and len(collected) >= config.limit:
            return


def _harvest(
    features: Iterable[dict],
    query: str,
    config: ScanConfig,
    collected: dict[str, Business],
    seen: set[str],
    result: ScanResult,
) -> int:
    """Разбирает объекты ответа, отбирает лидов и складывает их без дублей."""
    added = 0
    for feature in features:
        business = parse_feature(feature, query=query)
        if business is None:
            continue

        if business.company_id in seen:
            result.duplicate_hits += 1
            existing = collected.get(business.company_id)
            if existing is not None and query not in existing.query:
                # Одна организация может находиться по нескольким рубрикам.
                existing.query = f"{existing.query}; {query}".strip("; ")
            continue
        seen.add(business.company_id)
        result.seen_total += 1

        if business.closed and not config.include_closed:
            result.skipped_closed += 1
            continue

        status, domain = classify(business.url, business.links)
        business.site_status = status
        business.site_domain = domain

        if status not in config.lead_statuses:
            result.with_site += 1
            continue

        collected[business.company_id] = business
        added += 1
    return added


def _verify(leads: list[Business], config: ScanConfig, emit: ProgressFn) -> None:
    """Проверяет доступность указанных сайтов и пересчитывает оценку."""
    urls = [b.url for b in leads if b.url]
    emit({"type": "verify_start", "count": len(urls)})
    statuses = probe_many(urls, config.verify_workers, config.verify_timeout)
    dead = 0
    for business in leads:
        alive = statuses.get(business.url) if business.url else None
        if alive == "dead":
            dead += 1
        apply_score(business, site_alive=alive)
    emit({"type": "verify_done", "checked": len(urls), "dead": dead})
