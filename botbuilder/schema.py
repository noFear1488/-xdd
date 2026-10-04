"""Формат конфига бота и его проверка.

Конфиг — обычный JSON::

    {
      "name": "Мой бот",
      "parse_mode": "HTML",             # или "" — текст без разметки
      "ai_provider": "anthropic",       # кто пишет ИИ-ответы: anthropic (Claude) или openai
      "ai_model": "",                   # пусто — модель провайдера по умолчанию
      "handlers": [
        {
          "trigger": "command",         # command | text | callback | fallback
          "match": "start",             # команда, варианты текста через «|», callback_data
          "reply": "Привет!",
          "edit": false,                # для callback: менять сообщение, а не слать новое
          "users": "@ivan, @maria",     # необязательно: только для этих пользователей
          "ai": "Ты — вежливый ...",     # необязательно: отвечает Claude по этой инструкции,
                                        # а reply — запасной ответ, если ИИ недоступен
          "columns": 2,                 # кнопок в ряду
          "buttons": [
            {"text": "Меню", "kind": "callback", "value": "menu"},
            {"text": "Сайт", "kind": "url", "value": "https://example.com"}
          ]
        }
      ]
    }
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

TRIGGERS = ("command", "text", "callback", "fallback")
BUTTON_KINDS = ("callback", "url")
PARSE_MODES = ("HTML", "")
AI_PROVIDERS = {  # провайдер → (модель по умолчанию, переменная с ключом)
    "anthropic": ("claude-opus-5-5", "ANTHROPIC_API_KEY"),
    "openai": ("gpt-4o-mini", "OPENAI_API_KEY"),
}

COMMAND_RE = re.compile(r"^[a-z0-9_]{1,32}$")
USERNAME_RE = re.compile(r"^[a-z0-9_]{4,32}$")
URL_RE = re.compile(r"^(https?|tg)://\S+$")
MAX_CALLBACK_BYTES = 64  # лимит Telegram на callback_data
MAX_TEXT = 4096  # лимит Telegram на длину сообщения

STARTER = {
    "name": "Мой бот",
    "parse_mode": "HTML",
    "ai_provider": "anthropic",
    "ai_model": "",
    "handlers": [
        {
            "trigger": "command",
            "match": "start",
            "reply": "Привет! Я <b>бот-визитка</b>. Чем помочь?",
            "columns": 1,
            "buttons": [
                {"text": "📋 Услуги", "kind": "callback", "value": "services"},
                {"text": "📞 Контакты", "kind": "callback", "value": "contacts"},
            ],
        },
        {
            "trigger": "callback",
            "match": "services",
            "reply": "Делаем сайты, ботов и рекламу.",
            "edit": True,
            "columns": 1,
            "buttons": [{"text": "← Назад", "kind": "callback", "value": "home"}],
        },
        {
            "trigger": "callback",
            "match": "contacts",
            "reply": "Пишите: @username",
            "edit": True,
            "columns": 1,
            "buttons": [{"text": "← Назад", "kind": "callback", "value": "home"}],
        },
        {
            "trigger": "callback",
            "match": "home",
            "reply": "Привет! Я <b>бот-визитка</b>. Чем помочь?",
            "edit": True,
            "columns": 1,
            "buttons": [
                {"text": "📋 Услуги", "kind": "callback", "value": "services"},
                {"text": "📞 Контакты", "kind": "callback", "value": "contacts"},
            ],
        },
        {
            "trigger": "text",
            "match": "привет | здравствуйте",
            "reply": "И вам привет! Нажмите /start, чтобы открыть меню.",
            "columns": 1,
            "buttons": [],
        },
        {
            "trigger": "fallback",
            "match": "",
            "reply": "Не понял 🤔 Нажмите /start.",
            "columns": 1,
            "buttons": [],
        },
    ],
}


@dataclass
class Issue:
    message: str
    handler: int | None = None  # индекс обработчика, None — весь бот
    field: str = ""
    level: str = "error"  # error — код не собрать; warning — соберётся, но странно

    def to_dict(self) -> dict:
        return asdict(self)


def starter() -> dict:
    return copy.deepcopy(STARTER)


def load_config(path: Path) -> dict:
    """Конфиг из файла; если файла ещё нет — стартовый пример."""
    if path.exists():
        return normalize(json.loads(path.read_text(encoding="utf-8")))
    return starter()


def text_variants(match: str) -> list[str]:
    """Варианты текстового триггера: «привет | Здравствуйте» → ['привет', 'здравствуйте']."""
    seen: list[str] = []
    for part in str(match).split("|"):
        part = part.strip().lower()
        if part and part not in seen:
            seen.append(part)
    return seen


def usernames(users: str) -> list[str]:
    """«@Ivan, maria» → ['ivan', 'maria']: Telegram сравнивает username без учёта регистра."""
    seen: list[str] = []
    for part in re.split(r"[\s,;]+", str(users)):
        part = part.strip().lstrip("@").lower()
        if part and part not in seen:
            seen.append(part)
    return seen


def command_name(match: str) -> str:
    return str(match).strip().lstrip("/").lower()


def normalize(config: dict) -> dict:
    """Приводит конфиг к полному виду: недостающие поля получают значения по умолчанию."""
    handlers = []
    for raw in config.get("handlers") or []:
        if not isinstance(raw, dict):
            raw = {}
        buttons = []
        for b in raw.get("buttons") or []:
            if not isinstance(b, dict):
                b = {}
            buttons.append({
                "text": str(b.get("text", "")),
                "kind": str(b.get("kind", "callback")),
                "value": str(b.get("value", "")).strip(),
            })
        try:
            columns = int(raw.get("columns", 1))
        except (TypeError, ValueError):
            columns = 1
        handlers.append({
            "trigger": str(raw.get("trigger", "command")),
            "match": str(raw.get("match", "")).strip(),
            "reply": str(raw.get("reply", "")),
            "edit": bool(raw.get("edit", False)),
            "users": str(raw.get("users", "")).strip(),
            "ai": str(raw.get("ai", "")).strip(),
            "columns": columns,
            "buttons": buttons,
        })
    return {
        "name": str(config.get("name", "")).strip() or "Бот",
        "parse_mode": str(config.get("parse_mode", "HTML")),
        "ai_provider": str(config.get("ai_provider", "anthropic")),
        "ai_model": str(config.get("ai_model", "")).strip(),
        "handlers": handlers,
    }


def validate(config: dict) -> list[Issue]:
    cfg = normalize(config)
    issues: list[Issue] = []

    if cfg["parse_mode"] not in PARSE_MODES:
        issues.append(Issue(f"Неизвестный режим разметки: {cfg['parse_mode']!r}", field="parse_mode"))

    if cfg["ai_provider"] not in AI_PROVIDERS:
        issues.append(Issue(f"Неизвестный ИИ-провайдер: {cfg['ai_provider']!r}", field="ai_provider"))

    commands: dict[str, int] = {}
    callbacks: dict[str, int] = {}
    texts: dict[str, int] = {}
    fallbacks: list[int] = []

    for i, h in enumerate(cfg["handlers"]):
        trigger, match = h["trigger"], h["match"]
        if trigger not in TRIGGERS:
            issues.append(Issue(f"Неизвестный тип триггера: {trigger!r}", i, "trigger"))
            continue

        if trigger == "command":
            name = command_name(match)
            if not COMMAND_RE.match(name):
                issues.append(Issue("Команда: латиница в нижнем регистре, цифры и _, до 32 символов", i, "match"))
            elif name in commands:
                issues.append(Issue(f"Команда /{name} уже обрабатывается выше", i, "match"))
            else:
                commands[name] = i
        elif trigger == "text":
            variants = text_variants(match)
            if not variants:
                issues.append(Issue("Укажите текст, на который отвечать", i, "match"))
            for v in variants:
                if v in texts:
                    issues.append(Issue(f"Текст «{v}» уже обрабатывается выше", i, "match", "warning"))
                else:
                    texts[v] = i
        elif trigger == "callback":
            if not match:
                issues.append(Issue("Укажите идентификатор кнопки (callback_data)", i, "match"))
            elif len(match.encode()) > MAX_CALLBACK_BYTES:
                issues.append(Issue(f"Идентификатор длиннее {MAX_CALLBACK_BYTES} байт", i, "match"))
            elif match in callbacks:
                issues.append(Issue(f"Кнопка «{match}» уже обрабатывается выше", i, "match"))
            else:
                callbacks[match] = i
        elif not usernames(h["users"]):
            fallbacks.append(i)  # «остальное» для конкретных людей — не конкурент общему

        for name in usernames(h["users"]):
            if not USERNAME_RE.match(name):
                issues.append(Issue(f"@{name}: username — латиница, цифры и _, от 4 до 32 символов", i, "users"))

        if h["ai"] and trigger == "callback":
            issues.append(Issue("ИИ отвечает только на сообщения: у нажатия кнопки нет текста", i, "ai"))

        if not h["reply"].strip():
            issues.append(Issue("Пустой ответ: Telegram не отправит такое сообщение", i, "reply"))
        elif len(h["reply"]) > MAX_TEXT:
            issues.append(Issue(f"Ответ длиннее {MAX_TEXT} символов", i, "reply"))

        if not 1 <= h["columns"] <= 8:
            issues.append(Issue("Кнопок в ряду: от 1 до 8", i, "columns"))

        for j, b in enumerate(h["buttons"]):
            field = f"buttons.{j}.value"
            if not b["text"].strip():
                issues.append(Issue(f"Кнопка {j + 1}: пустая надпись", i, f"buttons.{j}.text"))
            if b["kind"] == "url":
                if not URL_RE.match(b["value"]):
                    issues.append(Issue(f"Кнопка {j + 1}: ссылка должна начинаться с https:// или tg://", i, field))
            elif b["kind"] == "callback":
                if not b["value"]:
                    issues.append(Issue(f"Кнопка {j + 1}: укажите, какой экран она открывает", i, field))
                elif len(b["value"].encode()) > MAX_CALLBACK_BYTES:
                    issues.append(Issue(f"Кнопка {j + 1}: идентификатор длиннее {MAX_CALLBACK_BYTES} байт", i, field))
            else:
                issues.append(Issue(f"Кнопка {j + 1}: неизвестный тип {b['kind']!r}", i, f"buttons.{j}.kind"))

    if len(fallbacks) > 1:
        for i in fallbacks[1:]:
            issues.append(Issue("«Всё остальное» уже есть выше — сработает только первый", i, "trigger"))

    # Кнопки, ведущие в никуда, — частая ошибка при сборке меню.
    for i, h in enumerate(cfg["handlers"]):
        for j, b in enumerate(h["buttons"]):
            if b["kind"] == "callback" and b["value"] and b["value"] not in callbacks:
                issues.append(Issue(
                    f"Кнопка {j + 1}: нет обработчика для «{b['value']}» — нажатие ничего не сделает",
                    i, f"buttons.{j}.value", "warning",
                ))

    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(issue.level == "error" for issue in issues)
