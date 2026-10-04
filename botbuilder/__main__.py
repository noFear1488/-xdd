"""python -m botbuilder [bot.json] — открыть конструктор; --export bot.py — только собрать код."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="botbuilder", description="Конструктор Telegram-ботов на aiogram 3")
    parser.add_argument("config", nargs="?", default="bot.json", help="файл конфига бота (по умолчанию bot.json)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    parser.add_argument("--export", metavar="FILE", help="записать bot.py и выйти, без интерфейса")
    args = parser.parse_args(argv)
    config_path = Path(args.config)

    if args.export:
        from .codegen import ConfigError, generate
        from .schema import load_config

        try:
            code = generate(load_config(config_path))
        except ConfigError as exc:
            for issue in exc.issues:
                if issue.level == "error":
                    where = f"обработчик {issue.handler + 1}: " if issue.handler is not None else ""
                    print(f"ошибка: {where}{issue.message}", file=sys.stderr)
            return 1
        Path(args.export).write_text(code, encoding="utf-8")
        print(f"Готово: {args.export}")
        return 0

    try:
        from aiohttp import web

        from .server import create_app
    except ImportError:
        print("Нужен aiogram: pip install aiogram", file=sys.stderr)
        return 1

    url = f"http://{args.host}:{args.port}/"
    print(f"Конструктор: {url}  (конфиг: {config_path.resolve()})")
    if not args.no_browser:
        webbrowser.open(url)
    web.run_app(create_app(config_path), host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
