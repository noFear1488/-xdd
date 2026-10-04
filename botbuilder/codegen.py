"""Сборка самостоятельного ``bot.py`` на aiogram 3 из конфига.

Все пользовательские строки попадают в код только через ``repr``, поэтому
конфиг не может внедрить в него произвольный Python.
"""

from __future__ import annotations

from .schema import AI_PROVIDERS, command_name, has_errors, normalize, text_variants, usernames, validate


# Общий код ИИ-ответов: история диалога в памяти, отказ и сбои API — запасной ответ.
# Разные провайдеры отличаются только вызовом модели (AI_CALL).
AI_HELPER = """
AI_MODEL = {model!r}
AI_HISTORY = 20  # сколько последних реплик помнить в каждом чате
ai_client = None  # создаётся при первом сообщении: без ключа бот всё равно запустится
ai_history = defaultdict(list)


async def ai_reply(message, system, fallback):
    global ai_client
    history = ai_history[message.chat.id]
    history.append({{"role": "user", "content": message.text or message.caption or "(сообщение без текста)"}})
    try:
{call}
    except Exception:  # сеть, лимиты, нет ключа — человеку всё равно надо ответить
        logging.exception("ИИ недоступен, отвечаю запасным текстом")
        history.pop()
        return fallback
    if refused or not text:
        history.pop()
        return fallback
    text = text[:4096]  # лимит Telegram на сообщение
    history.append({{"role": "assistant", "content": text}})
    del history[:-AI_HISTORY]  # длина чётная — история всегда начинается с реплики пользователя
    return text

"""

AI_IMPORTS = {
    "anthropic": "import anthropic",
    "openai": "from openai import AsyncOpenAI",
}

AI_CALL = {
    "anthropic": """\
        ai_client = ai_client or anthropic.AsyncAnthropic()  # ключ — из ANTHROPIC_API_KEY
        response = await ai_client.beta.messages.create(
            model=AI_MODEL,
            max_tokens=4000,
            system=system,
            messages=list(history),
            output_config={"effort": "low"},  # для переписки хватает, и отвечает быстрее
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # если модель откажется отвечать — повторить на рекомендованной
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        refused = response.stop_reason == "refusal"
""",
    "openai": """\
        ai_client = ai_client or AsyncOpenAI()  # ключ — из OPENAI_API_KEY
        response = await ai_client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "system", "content": system}, *history],
        )
        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        refused = choice.finish_reason == "content_filter" or bool(getattr(choice.message, "refusal", None))
""",
}


class ConfigError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__("; ".join(i.message for i in issues if i.level == "error"))


def _keyboard(h: dict) -> str:
    if not h["buttons"]:
        return ""
    rows = ", ".join(repr((b["text"], b["kind"], b["value"])) for b in h["buttons"])
    return f", reply_markup=keyboard([{rows}], {h['columns']})"


def _join(*filters: str) -> str:
    return ", ".join(f for f in filters if f)


def _handler(index: int, h: dict) -> list[str]:
    reply = repr(h["reply"])
    markup = _keyboard(h)
    name = f"handler_{index + 1}"

    users = usernames(h["users"])
    who = f"F.from_user.username.lower().in_({tuple(users)!r})" if users else ""

    if h["trigger"] == "callback":
        data = f"F.data == {h['match']!r}"
        send = "callback.message.edit_text" if h["edit"] else "callback.message.answer"
        return [
            f"@router.callback_query({_join(data, who)})",
            f"async def {name}(callback: CallbackQuery) -> None:",
            "    await callback.answer()",
            f"    await {send}({reply}{markup})",
        ]

    if h["ai"]:
        body = [
            "    await message.bot.send_chat_action(message.chat.id, \"typing\")",
            f"    text = await ai_reply(message, {h['ai']!r}, {reply})",
            # Ответ модели может содержать «<» или markdown — без разметки Telegram его не отвергнет.
            f"    await message.answer(text, parse_mode=None{markup})",
        ]
    else:
        body = [f"    await message.answer({reply}{markup})"]

    if h["trigger"] == "command":
        condition = f"Command({command_name(h['match'])!r})"
    elif h["trigger"] == "text":
        condition = f"F.text.lower().in_({tuple(text_variants(h['match']))!r})"
    else:
        condition = ""
    decorator = f"@router.message({_join(condition, who)})"
    return [
        decorator,
        f"async def {name}(message: Message) -> None:",
        *body,
    ]


def generate(config: dict) -> str:
    issues = validate(config)
    if has_errors(issues):
        raise ConfigError(issues)
    cfg = normalize(config)

    handlers = list(enumerate(cfg["handlers"]))
    # aiogram проверяет обработчики по порядку регистрации. Сначала — блоки для
    # конкретных людей (они уже всех остальных), в конце — общее «всё остальное»,
    # и только первое (остальные всё равно не сработали бы).
    personal = [(i, h) for i, h in handlers if usernames(h["users"])]
    regular = [(i, h) for i, h in handlers if not usernames(h["users"]) and h["trigger"] != "fallback"]
    fallback = [(i, h) for i, h in handlers if not usernames(h["users"]) and h["trigger"] == "fallback"][:1]

    has_buttons = any(h["buttons"] for h in cfg["handlers"])
    has_callbacks = any(h["trigger"] == "callback" for h in cfg["handlers"]) or any(
        b["kind"] == "callback" for h in cfg["handlers"] for b in h["buttons"]
    )
    parse_mode = repr(cfg["parse_mode"]) if cfg["parse_mode"] else "None"
    has_ai = any(h["ai"] for h in cfg["handlers"])
    provider = cfg["ai_provider"]

    types = ["CallbackQuery", "Message"] if has_callbacks else ["Message"]
    out = [
        f"# Telegram-бот {cfg['name']!r}, собран в botbuilder.",
        "# Запуск:  BOT_TOKEN=123:abc python bot.py",
        "",
        "import asyncio",
        "import logging",
        "import os",
        *(["from collections import defaultdict", "", AI_IMPORTS[provider]] if has_ai else []),
        "",
        "from aiogram import Bot, Dispatcher, F, Router",
        "from aiogram.client.default import DefaultBotProperties",
        "from aiogram.filters import Command",
        f"from aiogram.types import {', '.join(types)}",
    ]
    if has_buttons:
        out.append("from aiogram.utils.keyboard import InlineKeyboardBuilder")
    out += ["", "router = Router()", ""]

    if has_ai:
        model = cfg["ai_model"] or AI_PROVIDERS[provider][0]
        out += AI_HELPER.format(model=model, call=AI_CALL[provider].rstrip()).splitlines()

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

    for i, h in personal + regular + fallback:
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
