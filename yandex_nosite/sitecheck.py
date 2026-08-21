"""Классификация «есть ли у бизнеса настоящий сайт».

Пустое поле url в карточке — не единственный признак. Для продажи услуг одинаково
интересны организации, у которых вместо сайта указана страница ВКонтакте, карточка
на агрегаторе или бесплатный конструктор, а также те, чей сайт уже не открывается.
"""

from __future__ import annotations

import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

# Статусы сайта, от самого «горячего» лида к самому холодному.
NO_SITE = "none"
SOCIAL_ONLY = "social"
AGGREGATOR_ONLY = "aggregator"
BUILDER = "builder"
OWN_SITE = "own"

STATUS_TITLES: dict[str, str] = {
    NO_SITE: "сайта нет",
    SOCIAL_ONLY: "только соцсети",
    AGGREGATOR_ONLY: "только агрегатор",
    BUILDER: "конструктор/поддомен",
    OWN_SITE: "свой сайт",
}

# Наборы лидов по умолчанию: что считать «бизнесом без сайта».
LEAD_STATUSES: tuple[str, ...] = (NO_SITE, SOCIAL_ONLY, AGGREGATOR_ONLY, BUILDER)
STRICT_LEAD_STATUSES: tuple[str, ...] = (NO_SITE,)

SOCIAL_DOMAINS: frozenset[str] = frozenset(
    {
        "vk.com",
        "vk.ru",
        "m.vk.com",
        "t.me",
        "telegram.me",
        "telegram.org",
        "ok.ru",
        "odnoklassniki.ru",
        "instagram.com",
        "facebook.com",
        "fb.com",
        "wa.me",
        "api.whatsapp.com",
        "whatsapp.com",
        "youtube.com",
        "youtu.be",
        "rutube.ru",
        "tiktok.com",
        "dzen.ru",
        "zen.yandex.ru",
        "pinterest.com",
        "linkedin.com",
        "taplink.cc",
        "taplink.ru",
        "linktr.ee",
        "lnk.bio",
        "vk.link",
    }
)

AGGREGATOR_DOMAINS: frozenset[str] = frozenset(
    {
        "yandex.ru",
        "yandex.com",
        "maps.yandex.ru",
        "market.yandex.ru",
        "eda.yandex.ru",
        "2gis.ru",
        "2gis.com",
        "zoon.ru",
        "avito.ru",
        "youla.ru",
        "flamp.ru",
        "prodoctorov.ru",
        "docdoc.ru",
        "sberhealth.ru",
        "napopravku.ru",
        "restoclub.ru",
        "restoran.ru",
        "afisha.ru",
        "tripadvisor.ru",
        "tripadvisor.com",
        "booking.com",
        "ostrovok.ru",
        "sutochno.ru",
        "hh.ru",
        "rabota.ru",
        "superjob.ru",
        "wildberries.ru",
        "ozon.ru",
        "dikidi.net",
        "dikidi.ru",
        "yclients.com",
        "n1.ru",
        "cian.ru",
        "domclick.ru",
        "otzovik.com",
        "irecommend.ru",
        "spr.ru",
        "orgpage.ru",
        "rusprofile.ru",
        "list-org.com",
        "zachestnyibiznes.ru",
    }
)

# Хостинги, на которых сайт живёт поддоменом — фактически визитка, а не сайт.
BUILDER_SUFFIXES: tuple[str, ...] = (
    ".tilda.ws",
    ".tilda.cc",
    ".wixsite.com",
    ".business.site",
    ".ucoz.ru",
    ".ucoz.net",
    ".narod.ru",
    ".nethouse.ru",
    ".a5.ru",
    ".megagroup.ru",
    ".umi.ru",
    ".s-hosting.ru",
    ".bitrix24.site",
    ".bitrix24.shop",
    ".shopify.com",
    ".blogspot.com",
    ".wordpress.com",
    ".livejournal.com",
    ".jimdosite.com",
    ".weebly.com",
    ".webnode.ru",
    ".readymag.com",
    ".craftum.com",
    ".flexbe.ru",
    ".platformalp.ru",
    ".tb.ru",
)

# Двухуровневые публичные суффиксы, чтобы не резать домен слишком коротко.
_MULTIPART_SUFFIXES: tuple[str, ...] = (
    ".com.ru",
    ".org.ru",
    ".net.ru",
    ".co.uk",
    ".com.ua",
    ".co.il",
    ".com.tr",
    ".com.kz",
)


_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")


def normalize_url(raw: str) -> str:
    """Приводит адрес к виду со схемой; пустая строка, если адрес бессмысленный."""
    url = (raw or "").strip().strip(",;")
    if not url:
        return ""
    if url.lower() in {"-", "нет", "none", "null", "n/a"}:
        return ""
    scheme_match = _SCHEME_RE.match(url)
    if scheme_match:
        if scheme_match.group(1).lower() not in {"http", "https"}:
            return ""  # mailto:, tel:, viber: и прочее сайтом не считаются
    else:
        url = "http://" + url.lstrip("/")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    if "." not in parsed.netloc:
        return ""
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
    )


def extract_domain(raw: str) -> str:
    """Возвращает хост без www."""
    url = normalize_url(raw)
    if not url:
        return ""
    host = urllib.parse.urlsplit(url).netloc.lower()
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def registrable_domain(host: str) -> str:
    """Грубое приведение хоста к регистрируемому домену (без поддоменов)."""
    if not host:
        return ""
    for suffix in _MULTIPART_SUFFIXES:
        if host.endswith(suffix):
            head = host[: -len(suffix)].split(".")[-1]
            return f"{head}{suffix}"
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def classify_url(raw: str) -> str:
    """Классифицирует одну ссылку."""
    host = extract_domain(raw)
    if not host:
        return NO_SITE
    root = registrable_domain(host)
    if host in SOCIAL_DOMAINS or root in SOCIAL_DOMAINS:
        return SOCIAL_ONLY
    if host in AGGREGATOR_DOMAINS or root in AGGREGATOR_DOMAINS:
        return AGGREGATOR_ONLY
    if any(host.endswith(suffix) for suffix in BUILDER_SUFFIXES):
        return BUILDER
    return OWN_SITE


# Чем «сильнее» статус, тем менее интересен лид.
_RANK: dict[str, int] = {
    NO_SITE: 0,
    SOCIAL_ONLY: 1,
    AGGREGATOR_ONLY: 2,
    BUILDER: 3,
    OWN_SITE: 4,
}


def classify(url: str, links: Iterable[str] = ()) -> tuple[str, str]:
    """Определяет статус сайта организации по основному url и дополнительным ссылкам.

    Возвращает (статус, домен). Статус — максимальный по «силе» среди всех ссылок:
    если есть хотя бы один собственный сайт, организация нам не интересна.
    """
    candidates = [url, *links]
    best_status = NO_SITE
    best_domain = ""
    for candidate in candidates:
        status = classify_url(candidate)
        if status == NO_SITE:
            continue
        if _RANK[status] >= _RANK[best_status]:
            best_status = status
            best_domain = extract_domain(candidate)
    return best_status, best_domain


def is_lead(status: str, allowed: Iterable[str] = LEAD_STATUSES) -> bool:
    return status in set(allowed)


# --- Необязательная проверка доступности сайта -------------------------------

SITE_ALIVE = "alive"
SITE_DEAD = "dead"
SITE_UNKNOWN = "unknown"

_USER_AGENT = "Mozilla/5.0 (compatible; yandex-nosite/0.1; +https://example.invalid)"


def probe_site(url: str, timeout: float = 6.0) -> str:
    """Проверяет, отвечает ли сайт. Мёртвый сайт — тоже повод для звонка."""
    normalized = normalize_url(url)
    if not normalized:
        return SITE_UNKNOWN
    request = urllib.request.Request(
        normalized, method="GET", headers={"User-Agent": _USER_AGENT}
    )
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as resp:
            return SITE_ALIVE if resp.status < 500 else SITE_DEAD
    except urllib.error.HTTPError as exc:
        # 4xx означает, что сервер жив; интересны только 5xx и 404 на корне.
        return SITE_DEAD if exc.code >= 500 or exc.code == 404 else SITE_ALIVE
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError, ValueError):
        return SITE_DEAD


def probe_many(urls: list[str], workers: int = 8, timeout: float = 6.0) -> dict[str, str]:
    """Параллельно проверяет доступность списка адресов."""
    unique = [u for u in dict.fromkeys(urls) if u]
    if not unique:
        return {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        statuses = list(pool.map(lambda u: probe_site(u, timeout), unique))
    return dict(zip(unique, statuses))
