"""Подготовка обращений в Telegram: персонализация, ссылки, учёт отправленного.

Здесь нет автоматической отправки — и это осознанное решение. Массовая рассылка
незапрошенных сообщений с пользовательского аккаунта нарушает правила Telegram и
приводит к ограничению аккаунта по жалобам получателей. Модуль готовит текст и
ссылку на диалог, а отправляет человек, порциями. Побочный эффект такого подхода
приятный: персональное сообщение отвечается заметно чаще шаблонного.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .models import Business

# Пороговые значения темпа. Это не техническое ограничение, а то, при каком
# темпе живой человек не выглядит рассыльщиком.
BATCH_SIZE = 15
DAILY_ADVICE = "10–15 сообщений в день, с паузами в несколько минут"

DEFAULT_TEMPLATE = """Здравствуйте! Нашёл {name} на карте — и не нашёл у вас сайта.

Делаю сайты для таких компаний, как ваша: {benefit}. Неделя работы, показываю \
результат до оплаты.

Если интересно — пришлю пару примеров. Если нет — просто напишите «не надо», \
больше писать не буду."""

# Что конкретно даёт сайт в этой нише: общая фраза работает хуже конкретной.
BENEFITS: dict[str, str] = {
    "стоматология": "онлайн-запись, цены на услуги и отзывы пациентов",
    "клиника": "онлайн-запись, расписание врачей и цены",
    "медцентр": "онлайн-запись, расписание врачей и цены",
    "ветклиника": "запись на приём, цены и контакты дежурного врача",
    "салон красоты": "портфолио работ, прайс и запись онлайн",
    "парикмахерская": "портфолио работ, прайс и запись онлайн",
    "маникюр": "портфолио работ, прайс и запись онлайн",
    "косметика": "каталог с ценами и заказ без звонка",
    "массаж": "описание услуг, цены и запись",
    "тату-салон": "портфолио мастеров и запись",
    "автосервис": "прайс на работы, запись и отзывы",
    "автосалон": "каталог автомобилей с ценами и заявка на подбор",
    "автозапчасти": "каталог с наличием и ценами",
    "автомойка": "услуги, цены и запись на удобное время",
    "шиномонтаж": "цены, адреса и запись без очереди",
    "кафе": "меню с ценами, фото зала и бронь столика",
    "ресторан": "меню, фото и бронь столика",
    "пекарня": "витрина продукции и заказ на праздник",
    "кондитерская": "витрина тортов и приём заказов",
    "фастфуд": "меню и заказ навынос",
    "цветы": "каталог букетов с ценами и доставка",
    "одежда": "каталог с ценами и размерами",
    "мебель": "каталог, расчёт стоимости и заявка на замер",
    "окна": "калькулятор стоимости и заявка на замер",
    "фитнес": "расписание занятий, абонементы и пробное занятие",
    "спортклуб": "расписание, цены на абонементы и запись",
    "танцы": "расписание, цены и запись на пробное занятие",
    "юрист": "перечень услуг, цены и заявка на консультацию",
    "бухгалтер": "тарифы на обслуживание и заявка на консультацию",
    "недвижимость": "каталог объектов и заявка на подбор",
    "зоомагазин": "каталог с ценами и наличием",
    "аптека": "наличие препаратов и бронь",
    "гостиница": "фото номеров, цены и бронирование",
    "ателье": "перечень услуг, цены и запись на примерку",
    "детские товары": "каталог с ценами и наличием",
}

DEFAULT_BENEFIT = "описание услуг, цены и заявки прямо со страницы"

# Как назвать организацию в первой фразе, чтобы это звучало по-человечески.
_ARTICLE = {
    "стоматология": "вашу стоматологию",
    "ветклиника": "вашу ветклинику",
    "клиника": "вашу клинику",
    "медцентр": "ваш медцентр",
    "ветклиника": "вашу ветклинику",
    "салон красоты": "ваш салон",
    "парикмахерская": "вашу парикмахерскую",
    "автосервис": "ваш автосервис",
    "автосалон": "ваш автосалон",
    "кафе": "ваше кафе",
    "ресторан": "ваш ресторан",
    "пекарня": "вашу пекарню",
    "магазин": "ваш магазин",
    "фитнес": "ваш клуб",
}


class PhoneError(ValueError):
    """Номер не пригоден для Telegram."""


def normalize_phone(raw: str) -> str:
    """Приводит российский номер к виду 7XXXXXXXXXX."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11:
        raise PhoneError(f"непонятный номер: {raw!r}")
    return digits


def is_mobile(raw: str) -> bool:
    """Telegram привязан к мобильному номеру: городской писать бессмысленно."""
    try:
        digits = normalize_phone(raw)
    except PhoneError:
        return False
    # Мобильные России — коды 9xx; всё остальное (495, 499, 800) — стационарные
    # или сервисные номера, Telegram к ним не привязывают.
    return digits.startswith("79")


def telegram_link(raw: str) -> str:
    """Ссылка, открывающая диалог с номером."""
    return f"https://t.me/+{normalize_phone(raw)}"


def telegram_uri(raw: str) -> str:
    """Схема для десктопного клиента."""
    return f"tg://resolve?phone={normalize_phone(raw)}"


def _match(category: str, table: dict[str, str]) -> str | None:
    """Ищет самое длинное совпадение: «ветклиника» не должна попасть в «клинику»."""
    for key in sorted(table, key=len, reverse=True):
        if key in category:
            return table[key]
    return None


def benefit_for(business: Business) -> str:
    return _match((business.category or "").lower(), BENEFITS) or DEFAULT_BENEFIT


def subject_for(business: Business) -> str:
    """Как назвать организацию: «вашу стоматологию „Новодент“»."""
    name = business.name.strip()
    phrase = _match((business.category or "").lower(), _ARTICLE)
    if phrase:
        return f"{phrase} «{name}»" if name else phrase
    return f"«{name}»" if name else "вашу компанию"


@dataclass
class Message:
    """Готовое обращение к одному контакту."""

    business: Business
    text: str
    link: str
    uri: str
    phone: str

    @property
    def name(self) -> str:
        return self.business.name

    @property
    def category(self) -> str:
        return self.business.category


def render(business: Business, template: str = DEFAULT_TEMPLATE) -> Message:
    """Подставляет данные организации в шаблон."""
    phone = normalize_phone(business.phone)
    text = template.format(
        name=subject_for(business),
        title=business.name,
        category=business.category or "компаний",
        benefit=benefit_for(business),
        address=business.address or "",
        city=(business.query or "").strip(),
    )
    return Message(
        business=business,
        text=text.strip(),
        link=telegram_link(phone),
        uri=telegram_uri(phone),
        phone=phone,
    )


def prepare(
    businesses: Iterable[Business],
    template: str = DEFAULT_TEMPLATE,
    *,
    limit: int = BATCH_SIZE,
    mobile_only: bool = True,
) -> tuple[list[Message], list[tuple[Business, str]]]:
    """Готовит порцию сообщений. Возвращает (сообщения, пропущенные с причиной)."""
    messages: list[Message] = []
    skipped: list[tuple[Business, str]] = []
    for business in businesses:
        if len(messages) >= limit:
            break
        if not business.phone:
            skipped.append((business, "нет телефона"))
            continue
        if mobile_only and not is_mobile(business.phone):
            skipped.append((business, "не мобильный номер — Telegram маловероятен"))
            continue
        try:
            messages.append(render(business, template))
        except PhoneError as exc:
            skipped.append((business, str(exc)))
    return messages, skipped


def to_markdown(messages: list[Message], title: str = "Порция обращений") -> str:
    """Чеклист для работы с телефона: текст, ссылка и место для отметки."""
    lines = [
        f"# {title}",
        "",
        f"Отправляйте вручную, {DAILY_ADVICE}. Каждое сообщение — отдельный диалог;",
        "если человек не отвечает, второе сообщение не пишем.",
        "",
    ]
    for index, message in enumerate(messages, start=1):
        lines += [
            f"## {index}. {message.name} — {message.category}",
            "",
            f"- Телефон: `+{message.phone}`",
            f"- Открыть диалог: {message.link}",
            f"- Адрес: {message.business.address or '—'}",
            "- [ ] отправлено  - [ ] ответили  - [ ] отказ",
            "",
            "```",
            message.text,
            "```",
            "",
        ]
    return "\n".join(lines)
