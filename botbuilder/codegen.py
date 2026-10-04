"""Сборка самостоятельного ``bot.py`` на aiogram 3 из конфига.

Все пользовательские строки попадают в код только через ``repr``, поэтому
конфиг не может внедрить в него произвольный Python.
"""

from __future__ import annotations

from .schema import command_name, has_errors, normalize, text_variants, validate


class ConfigError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__("; ".join(i.message for i in issues if i.level == "error"))


def _keyboard(h: dict) -> str:
    if not h["buttons"]:
        return ""
    rows = ", ".join(repr((b["text"], b["kind"], b["value"])) for b in h["buttons"])
    return f", reply_markup=keyboard([{rows}], {h['columns']})"


def _handler(index: int, h: dict) -> list[str]:
    reply = repr(h["reply"])
    markup = _keyboard(h)
    name = f"handler_{index + 1}"

    if h["trigger"] == "callback":
        send = "callback.message.edit_text" if h["edit"] else "callback.message.answer"
        return [
            f"@router.callback_query(F.data == {h['match']!r})",
            f"async def {name}(callback: CallbackQuery) -> None:",
            "    await callback.answer()",
            f"    await {send}({reply}{markup})",
        ]

    if h["trigger"] == "command":
        decorator = f"@router.message(Command({command_name(h['match'])!r}))"
    elif h["trigger"] == "text":
        decorator = f"@router.message(F.text.lower().in_({tuple(text_variants(h['match']))!r}))"
    else:
        decorator = "@router.message()"
    return [
        decorator,
        f"async def {name}(message: Message) -> None:",
        f"    await message.answer({reply}{markup})",
    ]


def generate(config: dict) -> str:
    issues = validate(config)
    if has_errors(issues):
        raise ConfigError(issues)
    cfg = normalize(config)

    handlers = list(enumerate(cfg["handlers"]))
    # aiogram проверяет обработчики по порядку регистрации: «всё остальное» — в конец,
    # и только первый из них (остальные всё равно не сработали бы).
    regular = [(i, h) for i, h in handlers if h["trigger"] != "fallback"]
    fallback = [(i, h) for i, h in handlers if h["trigger"] == "fallback"][:1]

    has_buttons = any(h["buttons"] for h in cfg["handlers"])
    has_callbacks = any(h["trigger"] == "callback" for h in cfg["handlers"]) or any(
        b["kind"] == "callback" for h in cfg["handlers"] for b in h["buttons"]
    )
    parse_mode = repr(cfg["parse_mode"]) if cfg["parse_mode"] else "None"

    types = ["CallbackQuery", "Message"] if has_callbacks else ["Message"]
    out = [
        f"# Telegram-бот {cfg['name']!r}, собран в botbuilder.",
        "# Запуск:  BOT_TOKEN=123:abc python bot.py",
        "",
        "import asyncio",
        "import logging",
        "import os",
        "",
        "from aiogram import Bot, Dispatcher, F, Router",
        "from aiogram.client.default import DefaultBotProperties",
        "from aiogram.filters import Command",
        f"from aiogram.types import {', '.join(types)}",
    ]
    if has_buttons:
        out.append("from aiogram.utils.keyboard import InlineKeyboardBuilder")
    out += ["", "router = Router()", ""]

    if has_buttons:
        out += [
            "",
            "def keyboard(buttons, columns):",
            "    builder = InlineKeyboardBuilder()",
            "    for text, kind, value in buttons:",
            '        if kind == "url":',
            "            builder.button(text=text, url=value)",
            "        else:",
            "            builder.button(text=text, callback_data=value)",
            "    builder.adjust(columns)",
            "    return builder.as_markup()",
            "",
        ]

    for i, h in regular + fallback:
        out += [""] + _handler(i, h) + [""]

    if has_callbacks:
        out += [
            "",
            "@router.callback_query()",
            "async def unknown_button(callback: CallbackQuery) -> None:",
            "    # Без ответа у кнопки бесконечно крутятся часики.",
            "    await callback.answer()",
            "",
        ]

    out += [
        "",
        "def create_bot(token: str) -> Bot:",
        f"    return Bot(token, default=DefaultBotProperties(parse_mode={parse_mode}))",
        "",
        "",
        "def create_dispatcher() -> Dispatcher:",
        "    dp = Dispatcher()",
        "    dp.include_router(router)",
        "    return dp",
        "",
        "",
        "async def main() -> None:",
        "    logging.basicConfig(level=logging.INFO)",
        '    await create_dispatcher().start_polling(create_bot(os.environ["BOT_TOKEN"]))',
        "",
        "",
        'if __name__ == "__main__":',
        "    asyncio.run(main())",
        "",
    ]
    return "\n".join(out)
