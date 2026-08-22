"""Клиент Yandex Search API (поиск по организациям).

Документация: https://yandex.ru/maps-api/docs/search-api/

Используется только официальный API с ключом — никакого парсинга вёрстки карт,
который нарушает условия использования сервиса и ломается при любом релизе.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .geo import BBox

API_ENDPOINT = "https://search-maps.yandex.ru/v1/"
MAX_RESULTS_PER_REQUEST = 50
# Больше объектов на один поисковый запрос API не отдаёт, сколько бы ни было found.
MAX_RESULTS_PER_QUERY = 500


class YandexApiError(RuntimeError):
    """Ошибка обращения к API."""


class AuthError(YandexApiError):
    """Ключ не принят: неверный, не активирован или не тот тип ключа."""


class QuotaError(YandexApiError):
    """Исчерпан суточный лимит запросов ключа."""


class BudgetExhausted(RuntimeError):
    """Локальный лимит запросов, заданный пользователем, израсходован."""


class Transport(Protocol):
    """Минимальный HTTP-транспорт — подменяется в тестах."""

    def get(self, url: str, timeout: float) -> tuple[int, bytes]:  # pragma: no cover
        ...


class UrllibTransport:
    def get(self, url: str, timeout: float) -> tuple[int, bytes]:
        request = urllib.request.Request(
            url, headers={"User-Agent": "yandex-nosite/0.1", "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise YandexApiError(f"сетевая ошибка: {exc.reason}") from exc


@dataclass
class SearchResponse:
    """Разобранный ответ API."""

    found: int
    features: list[dict[str, Any]] = field(default_factory=list)
    from_cache: bool = False

    @classmethod
    def from_payload(cls, payload: dict[str, Any], from_cache: bool = False) -> "SearchResponse":
        meta = (
            (payload.get("properties") or {})
            .get("ResponseMetaData", {})
            .get("SearchResponse", {})
        )
        try:
            found = int(meta.get("found") or 0)
        except (TypeError, ValueError):
            found = 0
        features = payload.get("features") or []
        return cls(found=found, features=list(features), from_cache=from_cache)


@dataclass
class RequestBudget:
    """Ограничитель числа запросов: квота бесплатного ключа — 500 в сутки."""

    limit: int | None = None
    used: int = 0

    def check(self) -> None:
        if self.limit is not None and self.used >= self.limit:
            raise BudgetExhausted(
                f"израсходован лимит запросов к API ({self.limit}). "
                "Увеличьте --max-requests или сузьте область поиска."
            )

    def spend(self) -> None:
        self.used += 1

    @property
    def left(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.used)


class SearchClient:
    """Обёртка над одним методом API с ретраями, троттлингом и кэшем."""

    def __init__(
        self,
        api_key: str,
        *,
        lang: str = "ru_RU",
        timeout: float = 15.0,
        min_interval: float = 0.35,
        max_retries: int = 4,
        transport: Transport | None = None,
        cache: Any | None = None,
        budget: RequestBudget | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise AuthError(
                "не задан ключ API. Получите его в кабинете разработчика Яндекса "
                "(https://developer.tech.yandex.ru/, сервис «JavaScript API и HTTP Геокодер» "
                "→ «Поиск по организациям») и передайте через YANDEX_MAPS_API_KEY."
            )
        self.api_key = api_key
        self.lang = lang
        self.timeout = timeout
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.transport = transport or UrllibTransport()
        self.cache = cache
        self.budget = budget or RequestBudget()
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None
        self.stats = {"requests": 0, "cache_hits": 0, "retries": 0}

    # -- построение запроса ---------------------------------------------------

    def build_params(
        self, text: str, area: BBox, *, results: int, skip: int
    ) -> dict[str, str]:
        params = {
            "apikey": self.api_key,
            "text": text,
            "lang": self.lang,
            "type": "biz",
            "bbox": area.to_api(),
            "rspn": "1",  # не выходить за пределы области поиска
            "results": str(min(results, MAX_RESULTS_PER_REQUEST)),
        }
        if skip:
            params["skip"] = str(skip)
        return params

    def _cache_key(self, params: dict[str, str]) -> str:
        payload = {k: v for k, v in params.items() if k != "apikey"}
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)

    # -- выполнение -----------------------------------------------------------

    def search(
        self,
        text: str,
        area: BBox,
        *,
        results: int = MAX_RESULTS_PER_REQUEST,
        skip: int = 0,
    ) -> SearchResponse:
        params = self.build_params(text, area, results=results, skip=skip)
        key = self._cache_key(params)

        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                self.stats["cache_hits"] += 1
                return SearchResponse.from_payload(cached, from_cache=True)

        self.budget.check()
        payload = self._request(params)
        self.budget.spend()
        self.stats["requests"] += 1

        if self.cache is not None:
            self.cache.put(key, payload)
        return SearchResponse.from_payload(payload)

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        now = self._clock()
        if self._last_call is not None:
            wait = self.min_interval - (now - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def _request(self, params: dict[str, str]) -> dict[str, Any]:
        url = f"{API_ENDPOINT}?{urllib.parse.urlencode(params)}"
        delay = 1.0
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                status, body = self.transport.get(url, self.timeout)
            except YandexApiError as exc:
                last_error = exc
                status, body = 0, b""

            if status == 200:
                try:
                    return json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise YandexApiError(f"не удалось разобрать ответ API: {exc}") from exc

            if status in (401, 403):
                message = _error_message(body)
                if "limit" in message.lower() or "лимит" in message.lower():
                    raise QuotaError(f"исчерпан лимит запросов ключа: {message}")
                raise AuthError(
                    f"ключ API отклонён (HTTP {status}): {message}. "
                    + describe_key_problem(self.api_key)
                )
            if status == 400:
                raise YandexApiError(f"некорректный запрос: {_error_message(body)}")

            # 429 и 5xx — повторяем с экспоненциальной задержкой.
            if attempt < self.max_retries:
                self.stats["retries"] += 1
                self._sleep(delay)
                delay *= 2
                continue

            if last_error is not None:
                raise YandexApiError(str(last_error))
            raise YandexApiError(
                f"API вернул HTTP {status} после {self.max_retries} повторов: "
                f"{_error_message(body)}"
            )

        raise YandexApiError("не удалось выполнить запрос")  # pragma: no cover


def describe_key_problem(api_key: str) -> str:
    """Подсказка по отклонённому ключу.

    У Яндекса несколько разных «Search API», и ключи от них не взаимозаменяемы.
    Ключ Поиска по организациям — UUID из кабинета Яндекс Карт; ключ вида
    AQVN... принадлежит сервисному аккаунту Yandex Cloud и к картам не подходит.
    """
    key = (api_key or "").strip()
    if key.startswith("AQVN"):
        return (
            "Судя по формату (AQVN...), это API-ключ сервисного аккаунта Yandex Cloud "
            "— он подходит для Cloud Search API (поиск по интернету), но не для "
            "Поиска по организациям Яндекс Карт. Нужен ключ вида "
            "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx из кабинета разработчика Яндекс Карт: "
            "https://developer.tech.yandex.ru/services/ → «JavaScript API и HTTP Геокодер» "
            "→ ключ для Search API."
        )
    if key.startswith("t1.") or key.startswith("y0_"):
        return (
            "Похоже, это IAM- или OAuth-токен Яндекса, а не ключ Search API Карт. "
            "Нужен ключ вида xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx из кабинета "
            "разработчика Яндекс Карт: https://developer.tech.yandex.ru/services/"
        )
    if not _looks_like_uuid(key):
        return (
            "Ключ Поиска по организациям выглядит как UUID "
            "(xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx); присланный на него не похож. "
            "Проверьте, что ключ взят в кабинете разработчика Яндекс Карт "
            "(https://developer.tech.yandex.ru/services/) именно для Search API."
        )
    return (
        "Проверьте, что ключ выдан для «Поиска по организациям» и активирован — "
        "новый ключ начинает работать в течение примерно 15 минут после создания."
    )


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _looks_like_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value or ""))


_XML_MESSAGE_RE = re.compile(r"<message>(.*?)</message>", re.DOTALL)


def _error_message(body: bytes) -> str:
    """Достаёт человекочитаемую причину. Яндекс отвечает то JSON, то XML."""
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return "нет тела ответа"
    try:
        payload = json.loads(text)
    except ValueError:
        match = _XML_MESSAGE_RE.search(text)
        return (match.group(1).strip() if match else text)[:300]
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("error") or payload)[:300]
    return str(payload)[:300]
