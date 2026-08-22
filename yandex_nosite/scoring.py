"""Оценка лида: насколько вероятно, что бизнесу можно продать сайт."""

from __future__ import annotations

from .models import Business
from .sitecheck import (
    AGGREGATOR_ONLY,
    BUILDER,
    NO_SITE,
    OWN_SITE,
    SOCIAL_ONLY,
)

# Веса подобраны так, чтобы «идеальный» лид набирал ~90, а не упирался в потолок:
# иначе верх списка превращается в сплошные сотни и его нельзя ранжировать.
BASE_SCORE: dict[str, int] = {
    NO_SITE: 45,
    SOCIAL_ONLY: 36,
    AGGREGATOR_ONLY: 32,
    BUILDER: 24,
    OWN_SITE: 0,
}

BASE_REASON: dict[str, str] = {
    NO_SITE: "сайта нет вообще",
    SOCIAL_ONLY: "вместо сайта соцсеть",
    AGGREGATOR_ONLY: "вместо сайта карточка на агрегаторе",
    BUILDER: "сайт на бесплатном конструкторе",
    OWN_SITE: "есть собственный сайт",
}

# Ниши, где сайт продаётся легче: высокий средний чек и зависимость от заявок.
HOT_CATEGORY_KEYWORDS: tuple[str, ...] = (
    "стоматолог",
    "клиник",
    "медицин",
    "юрист",
    "адвокат",
    "нотариус",
    "ремонт квартир",
    "строительн",
    "натяжные потолки",
    "окна",
    "мебель на заказ",
    "автосервис",
    "автосалон",
    "шиномонтаж",
    "салон красоты",
    "барбершоп",
    "косметолог",
    "фитнес",
    "стома",
    "недвижим",
    "бухгалтер",
    "логистик",
    "грузопере",
    "натяжн",
    "образован",
    "языков",
    "детск",
    "ветеринар",
    "туристическ",
)


def score(business: Business, *, site_alive: str | None = None) -> tuple[int, list[str]]:
    """Возвращает оценку 0..100 и список причин — почему лид интересен.

    site_alive — необязательный результат проверки доступности сайта
    ('alive' / 'dead'); мёртвый сайт поднимает приоритет.
    """
    status = business.site_status or NO_SITE
    value = BASE_SCORE.get(status, 0)
    reasons: list[str] = [BASE_REASON.get(status, status)]

    if site_alive == "dead" and status != NO_SITE:
        value += 18
        reasons.append("указанный сайт не открывается")

    if business.phones:
        value += 15
        reasons.append("есть телефон для связи")
    else:
        value -= 10
        reasons.append("телефон не указан — сложно дозвониться")

    reviews = business.reviews or 0
    if reviews >= 20:
        value += 12
        reasons.append(f"живой бизнес: {reviews} отзывов")
    elif reviews >= 5:
        value += 7
        reasons.append(f"есть отзывы ({reviews})")

    if business.rating is not None and business.rating >= 4.0 and reviews >= 5:
        value += 5
        reasons.append(f"хороший рейтинг {business.rating}")

    if business.hours:
        value += 4
        reasons.append("указан режим работы")

    if len(business.categories) >= 2:
        value += 3
        reasons.append("несколько рубрик — развитая деятельность")

    category_text = " ".join(business.categories).lower()
    if any(keyword in category_text for keyword in HOT_CATEGORY_KEYWORDS):
        value += 8
        reasons.append("ниша с высоким спросом на сайт")

    if "сетевая точка" in business.categories:
        # Сеть: сайт почти наверняка есть, просто не указан в источнике.
        value -= 25
        reasons.append("сетевая точка — сайт, скорее всего, есть у сети")

    if business.closed:
        value -= 40
        reasons.append("организация закрыта")

    if not business.address:
        value -= 5
        reasons.append("нет адреса")

    return max(0, min(100, value)), reasons


def apply_score(business: Business, *, site_alive: str | None = None) -> Business:
    business.lead_score, business.score_reasons = score(business, site_alive=site_alive)
    return business
