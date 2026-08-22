"""Командный интерфейс: сканирование, планирование бюджета, экспорт, статистика."""

from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path

from . import __version__
from .api import (
    MAX_RESULTS_PER_REQUEST,
    AuthError,
    RequestBudget,
    SearchClient,
    YandexApiError,
)
from .export import write
from .geo import REGION_TITLES, REGIONS, BBox, resolve_region
from .models import Business
from .pipeline import ScanConfig, ScanResult, scan
from .presets import PRESET_TITLES, PRESETS, resolve_queries
from .scoring import apply_score
from .sitecheck import (
    LEAD_STATUSES,
    STATUS_TITLES,
    STRICT_LEAD_STATUSES,
    classify,
)
from .storage import Storage

ENV_KEY = "YANDEX_MAPS_API_KEY"
FREE_DAILY_LIMIT = 500


class CliError(Exception):
    """Ошибка в аргументах или окружении: печатается пользователю без трассировки."""


# --- вспомогательное ---------------------------------------------------------


def load_dotenv(path: str | Path = ".env") -> None:
    """Читает простые KEY=VALUE из .env, не перетирая уже заданные переменные."""
    file = Path(path)
    if not file.is_file():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def resolve_area(args: argparse.Namespace) -> tuple[BBox, str]:
    """Определяет область поиска и её человекочитаемое имя."""
    if args.bbox:
        return BBox.parse(args.bbox), args.region or "bbox"
    if args.center:
        raw = args.center.replace(";", ",").split(",")
        if len(raw) != 2:
            raise CliError("ошибка: --center задаётся как 'долгота,широта'")
        lon, lat = float(raw[0]), float(raw[1])
        return BBox.from_center(lon, lat, args.radius), args.region or "center"
    if args.region:
        try:
            return resolve_region(args.region), args.region.lower()
        except KeyError:
            raise CliError(
                f"ошибка: регион '{args.region}' неизвестен. "
                "Список: yandex-nosite regions, либо задайте --bbox / --center."
            )
    raise CliError("ошибка: укажите область поиска: --region, --bbox или --center")


def resolve_statuses(args: argparse.Namespace) -> tuple[str, ...]:
    if args.statuses:
        wanted = tuple(s.strip() for s in args.statuses.split(",") if s.strip())
        unknown = [s for s in wanted if s not in STATUS_TITLES]
        if unknown:
            raise CliError(
                f"ошибка: неизвестные статусы {', '.join(unknown)}. "
                f"Доступны: {', '.join(STATUS_TITLES)}"
            )
        return wanted
    return STRICT_LEAD_STATUSES if args.strict else LEAD_STATUSES


def build_queries(args: argparse.Namespace) -> list[str]:
    queries = list(args.query or [])
    if args.queries_file:
        file = Path(args.queries_file)
        if not file.is_file():
            raise CliError(f"ошибка: файл с запросами не найден: {file}")
        queries.extend(
            line.strip()
            for line in file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    preset = args.preset
    if not preset and not queries:
        preset = "quick"
    try:
        result = resolve_queries(preset, queries)
    except KeyError as exc:
        raise CliError(
            f"ошибка: пресет '{exc.args[0]}' неизвестен. "
            f"Доступны: {', '.join(PRESETS)}"
        )
    if not result:
        raise CliError("ошибка: не задано ни одного поискового запроса")
    return result


def resolve_db(args: argparse.Namespace) -> str:
    """Демо-прогон не должен смешивать синтетику с реальной базой."""
    if args.db:
        return args.db
    return "demo-leads.db" if getattr(args, "demo", False) else "leads.db"


def make_client(args: argparse.Namespace, storage: Storage | None) -> SearchClient:
    budget = RequestBudget(limit=args.max_requests)
    if getattr(args, "demo", False):
        from .demo import DemoTransport

        return SearchClient(
            "demo-key",
            timeout=args.timeout,
            min_interval=0.0,
            transport=DemoTransport(),
            cache=None,
            budget=budget,
        )
    api_key = args.api_key or os.environ.get(ENV_KEY, "")
    try:
        return SearchClient(
            api_key,
            timeout=args.timeout,
            min_interval=args.min_interval,
            cache=None if args.no_cache else storage,
            budget=budget,
        )
    except AuthError as exc:
        raise CliError(f"ошибка: {exc}")


def make_progress(args: argparse.Namespace):
    if args.quiet:
        return None

    state = {"tiles": 0}

    def progress(event: dict) -> None:
        kind = event.get("type")
        if kind == "query_start":
            print(f"\n▶ рубрика: {event['query']}", file=sys.stderr)
        elif kind in ("tile", "page"):
            state["tiles"] += 1
            mark = "кэш" if event.get("cached") else "API"
            if kind == "tile":
                print(
                    f"  [{state['tiles']:>4}] {mark} найдено={event['found']:<5}"
                    f" получено={event['returned']:<3} новых лидов={event['new_leads']:<3}"
                    f" всего={event['leads_total']}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"  [{state['tiles']:>4}] {mark} стр.+{event['skip']:<4}"
                    f" получено={event['returned']:<3} новых лидов={event['new_leads']:<3}"
                    f" всего={event['leads_total']}",
                    file=sys.stderr,
                )
        elif kind == "verify_start":
            print(f"\n⏳ проверяю доступность {event['count']} сайтов…", file=sys.stderr)
        elif kind == "verify_done":
            print(
                f"   проверено {event['checked']}, не открывается {event['dead']}",
                file=sys.stderr,
            )
        elif kind == "stopped":
            print(f"\n⚠ остановка: {event['reason']}", file=sys.stderr)

    return progress


# --- команды -----------------------------------------------------------------


def cmd_scan(args: argparse.Namespace) -> int:
    area, region_name = resolve_area(args)
    queries = build_queries(args)
    statuses = resolve_statuses(args)

    db_path = resolve_db(args)
    storage = Storage(db_path, cache_ttl_days=args.cache_ttl)
    client = make_client(args, storage)

    width_km, height_km = area.km_size()
    if not args.quiet:
        title = REGION_TITLES.get(region_name, region_name)
        print(
            f"Область: {title} ({width_km:.1f}×{height_km:.1f} км), "
            f"рубрик: {len(queries)}, статусы лидов: "
            f"{', '.join(STATUS_TITLES.get(s, s) for s in statuses)}",
            file=sys.stderr,
        )
        if args.demo:
            print("Режим: ДЕМО — данные синтетические, к API обращений нет", file=sys.stderr)
        else:
            used_today = storage.requests_today()
            print(
                f"Запросов к API сегодня: {used_today} из {FREE_DAILY_LIMIT} "
                f"(бесплатный лимит), бюджет прогона: {args.max_requests}",
                file=sys.stderr,
            )

    config = ScanConfig(
        queries=queries,
        area=area,
        region=region_name,
        lead_statuses=statuses,
        max_depth=args.max_depth,
        min_tile_km=args.min_tile_km,
        page_size=min(args.page_size, MAX_RESULTS_PER_REQUEST),
        deep_paging=not args.no_deep_paging,
        verify_sites=args.verify_sites,
        include_closed=args.include_closed,
        min_score=args.min_score,
        limit=args.limit,
    )

    def record() -> None:
        if not args.demo:
            storage.record_requests(client.stats["requests"])

    try:
        result = scan(client, config, make_progress(args))
    except YandexApiError as exc:
        record()
        storage.close()
        print(f"ошибка API: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        record()
        storage.close()
        print("\nпрервано пользователем", file=sys.stderr)
        return 130

    record()
    new_count, updated = storage.save_many(result.leads, region_name)

    written = 0
    if args.out and result.leads:
        written = write(
            result.leads,
            args.out,
            args.format,
            compact=args.compact,
            sheet_title="ДЕМО - данные не настоящие" if args.demo else "Лиды",
        )

    print_summary(result, new_count, updated, args.out if written else None)
    storage.close()
    return 0


def print_summary(
    result: ScanResult, new_count: int, updated: int, out: str | None
) -> None:
    print("\n" + "─" * 62)
    print(f"Просмотрено организаций : {result.seen_total}")
    print(f"Из них с сайтом         : {result.with_site}")
    if result.skipped_closed:
        print(f"Пропущено закрытых      : {result.skipped_closed}")
    print(
        f"Найдено лидов           : {len(result.leads)} "
        f"({result.lead_share:.0f}% от просмотренных)"
    )
    print(f"  новых в базе          : {new_count}, обновлено: {updated}")
    print(f"Запросов к API          : {result.requests} (из кэша: {result.cache_hits})")
    print(f"Обработано тайлов       : {result.tiles_visited}")
    if result.stopped_early:
        print(f"⚠ Прогон не завершён    : {result.stop_reason}")
    if out:
        print(f"Файл                    : {out}")

    if result.leads:
        print("\nТоп лидов:")
        for business in result.leads[:10]:
            status = STATUS_TITLES.get(business.site_status, business.site_status)
            phone = business.phone or "телефон не указан"
            print(
                f"  {business.lead_score:>3}  {business.name[:38]:<38} "
                f"{phone:<20} {status}"
            )
    print("─" * 62)


def cmd_plan(args: argparse.Namespace) -> int:
    """Оценивает объём работы: по одному запросу на рубрику, без полного обхода."""
    area, region_name = resolve_area(args)
    queries = build_queries(args)
    storage = Storage(resolve_db(args), cache_ttl_days=args.cache_ttl)
    client = make_client(args, storage)

    print(f"Область: {region_name}, рубрик: {len(queries)}\n")
    print(f"{'рубрика':<34}{'найдено':>9}{'≈ запросов':>12}")
    print("─" * 55)

    total_found = 0
    total_requests = 0
    for query in queries:
        try:
            response = client.search(query, area, results=1)
        except YandexApiError as exc:
            print(f"{query:<34}{'ошибка':>9}  {exc}")
            continue
        estimate = estimate_requests(response.found, args.page_size, args.max_depth)
        total_found += response.found
        total_requests += estimate
        print(f"{query[:33]:<34}{response.found:>9}{estimate:>12}")

    if not args.demo:
        storage.record_requests(client.stats["requests"])
    print("─" * 55)
    print(f"{'ИТОГО':<34}{total_found:>9}{total_requests:>12}")
    print(
        f"\nНа разведку потрачено запросов: {client.stats['requests']}. "
        f"Полное сканирование обойдётся примерно в {total_requests} запросов "
        f"(бесплатный лимит — {FREE_DAILY_LIMIT} в сутки)."
    )
    if total_requests > FREE_DAILY_LIMIT:
        days = math.ceil(total_requests / FREE_DAILY_LIMIT)
        print(
            f"Это больше суточной квоты: разбейте работу примерно на {days} дня(ей) "
            f"или сузьте область (--radius / --bbox), либо возьмите меньше рубрик."
        )
    storage.close()
    return 0


def estimate_requests(found: int, page_size: int, max_depth: int) -> int:
    """Грубая оценка числа запросов на одну рубрику при адаптивном дроблении."""
    page_size = max(1, min(page_size, MAX_RESULTS_PER_REQUEST))
    if found <= page_size:
        return 1
    # На каждом уровне дробления область делится на 4; спускаемся, пока в тайле
    # помещается не больше page_size объектов или пока не упрёмся в max_depth.
    depth = min(max_depth, math.ceil(math.log(found / page_size, 4)))
    tiles = 4**depth
    # Плюс добор страниц в тайлах, которые всё ещё переполнены.
    leftovers = max(0, math.ceil(found / max(1, tiles)) - page_size) // page_size
    return int(tiles * (1 + leftovers) + (4**depth - 1) // 3)


def cmd_regions(args: argparse.Namespace) -> int:
    print(f"{'ключ':<20}{'город':<22}{'размер, км':>14}")
    print("─" * 56)
    for slug, area in sorted(REGIONS.items()):
        width, height = area.km_size()
        print(
            f"{slug:<20}{REGION_TITLES.get(slug, ''):<22}"
            f"{f'{width:.0f}×{height:.0f}':>14}"
        )
    print(
        "\nЛюбую другую область задавайте через --bbox 'lon1,lat1~lon2,lat2' "
        "или --center 'lon,lat' --radius KM"
    )
    return 0


def cmd_presets(args: argparse.Namespace) -> int:
    for name, queries in PRESETS.items():
        print(f"\n{name} — {PRESET_TITLES.get(name, '')} ({len(queries)} рубрик)")
        print("  " + ", ".join(queries[:12]) + (" …" if len(queries) > 12 else ""))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    storage = Storage(args.db)
    statuses = resolve_statuses(args)
    leads = list(
        storage.iter_businesses(
            statuses=statuses,
            region=args.region,
            min_score=args.min_score,
            limit=args.limit,
        )
    )
    if not leads:
        print("в базе нет записей под заданные условия", file=sys.stderr)
        storage.close()
        return 1
    count = write(leads, args.out, args.format, compact=args.compact)
    print(f"выгружено {count} организаций в {args.out}")
    storage.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    storage = Storage(args.db)
    total = storage.total()
    print(f"База: {args.db}")
    print(f"Всего организаций: {total}")
    if total:
        print("\nПо статусу сайта:")
        for status, count in sorted(
            storage.counts_by_status(args.region).items(), key=lambda kv: -kv[1]
        ):
            share = count / total * 100
            print(
                f"  {STATUS_TITLES.get(status, status):<24}{count:>7}  ({share:.0f}%)"
            )
    history = storage.usage_history()
    if history:
        print("\nРасход запросов к API:")
        for day, count in history:
            flag = "  ⚠ близко к лимиту" if count >= FREE_DAILY_LIMIT * 0.9 else ""
            print(f"  {day}  {count:>4} / {FREE_DAILY_LIMIT}{flag}")
    storage.close()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Быстрая проверка классификатора на произвольном адресе."""
    for url in args.url:
        status, domain = classify(url, [])
        business = Business(company_id="test", name="test", url=url, phones=["+7"])
        business.site_status = status
        apply_score(business)
        print(
            f"{url:<45}{STATUS_TITLES.get(status, status):<24}"
            f"{domain:<28}оценка={business.lead_score}"
        )
    return 0


# --- разбор аргументов -------------------------------------------------------


def add_area_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("область поиска")
    group.add_argument("--region", help="ключ города из справочника (см. команду regions)")
    group.add_argument("--bbox", help="прямоугольник 'lon1,lat1~lon2,lat2'")
    group.add_argument("--center", help="центр области 'долгота,широта'")
    group.add_argument(
        "--radius", type=float, default=3.0, help="радиус в км для --center (по умолчанию 3)"
    )


def add_query_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("что искать")
    group.add_argument(
        "--preset", help=f"набор рубрик: {', '.join(PRESETS)} (по умолчанию quick)"
    )
    group.add_argument(
        "--query", action="append", help="произвольный поисковый запрос (можно повторять)"
    )
    group.add_argument("--queries-file", help="файл со списком запросов, по одному в строке")


def add_common_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("общие параметры")
    group.add_argument("--db", help="файл базы (по умолчанию leads.db)")
    group.add_argument("--api-key", help=f"ключ API (иначе берётся из {ENV_KEY})")
    group.add_argument(
        "--demo",
        action="store_true",
        help="прогон на синтетических данных без ключа и без обращения к API",
    )
    group.add_argument(
        "--max-requests",
        type=int,
        default=FREE_DAILY_LIMIT,
        help="потолок запросов к API за прогон (по умолчанию 500)",
    )
    group.add_argument("--timeout", type=float, default=15.0, help="таймаут запроса, сек")
    group.add_argument(
        "--min-interval",
        type=float,
        default=0.35,
        help="минимальная пауза между запросами, сек",
    )
    group.add_argument("--no-cache", action="store_true", help="не использовать кэш ответов")
    group.add_argument(
        "--cache-ttl", type=int, default=14, help="срок жизни кэша в днях (0 — вечно)"
    )
    group.add_argument("--page-size", type=int, default=MAX_RESULTS_PER_REQUEST)
    group.add_argument("--max-depth", type=int, default=4, help="глубина дробления сетки")


def add_filter_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("фильтр лидов")
    group.add_argument(
        "--strict",
        action="store_true",
        help="только организации совсем без ссылок (по умолчанию считаются также "
        "соцсети, агрегаторы и конструкторы)",
    )
    group.add_argument(
        "--statuses",
        help=f"явный список статусов через запятую: {', '.join(STATUS_TITLES)}",
    )
    group.add_argument("--min-score", type=int, default=0, help="минимальная оценка лида")
    group.add_argument("--limit", type=int, help="ограничение на число организаций")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yandex-nosite",
        description="Поиск бизнесов без сайта по данным Яндекс Карт "
        "(официальный Search API).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  yandex-nosite plan --region kazan --preset beauty\n"
            "  yandex-nosite scan --region kazan --preset beauty --out leads.xlsx\n"
            "  yandex-nosite scan --center '49.12,55.79' --radius 2 --query 'автосервис'\n"
            "  yandex-nosite export --min-score 70 --out hot.csv\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"yandex-nosite {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="просканировать область и собрать лидов")
    add_area_args(scan_parser)
    add_query_args(scan_parser)
    add_filter_args(scan_parser)
    add_common_args(scan_parser)
    scan_parser.add_argument("--out", help="файл выгрузки (.csv, .xlsx или .json)")
    scan_parser.add_argument("--format", choices=["csv", "xlsx", "json"])
    scan_parser.add_argument(
        "--compact",
        action="store_true",
        help="короткий набор колонок — для просмотра с телефона",
    )
    scan_parser.add_argument(
        "--verify-sites",
        action="store_true",
        help="проверять, открывается ли указанный сайт (мёртвый сайт — тоже лид)",
    )
    scan_parser.add_argument("--include-closed", action="store_true")
    scan_parser.add_argument("--no-deep-paging", action="store_true")
    scan_parser.add_argument("--min-tile-km", type=float, default=0.5)
    scan_parser.add_argument("--quiet", action="store_true")
    scan_parser.set_defaults(func=cmd_scan)

    plan_parser = subparsers.add_parser(
        "plan", help="оценить, сколько запросов и лидов даст сканирование"
    )
    add_area_args(plan_parser)
    add_query_args(plan_parser)
    add_common_args(plan_parser)
    plan_parser.set_defaults(func=cmd_plan)

    regions_parser = subparsers.add_parser("regions", help="список известных городов")
    regions_parser.set_defaults(func=cmd_regions)

    presets_parser = subparsers.add_parser("presets", help="список наборов рубрик")
    presets_parser.set_defaults(func=cmd_presets)

    export_parser = subparsers.add_parser("export", help="выгрузить лидов из базы")
    export_parser.add_argument("--out", required=True, help="файл выгрузки")
    export_parser.add_argument("--format", choices=["csv", "xlsx", "json"])
    export_parser.add_argument(
        "--compact",
        action="store_true",
        help="короткий набор колонок — для просмотра с телефона",
    )
    export_parser.add_argument("--region", help="фильтр по региону")
    export_parser.add_argument("--db", default="leads.db")
    add_filter_args(export_parser)
    export_parser.set_defaults(func=cmd_export)

    stats_parser = subparsers.add_parser("stats", help="статистика по базе и расходу квоты")
    stats_parser.add_argument("--db", default="leads.db")
    stats_parser.add_argument("--region")
    stats_parser.set_defaults(func=cmd_stats)

    check_parser = subparsers.add_parser(
        "check", help="проверить, как классифицируется конкретный адрес сайта"
    )
    check_parser.add_argument("url", nargs="+")
    check_parser.set_defaults(func=cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:
        # Вывод оборвал получатель (`| head`) — это не ошибка работы.
        try:
            sys.stdout.close()
        except BrokenPipeError:
            pass
        return 0
    except CliError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise CliError(main())
