"""Локальный веб-интерфейс конструктора (aiohttp, ставится вместе с aiogram)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from aiohttp import web

from .codegen import ConfigError, generate
from .runner import BotRunner
from .schema import has_errors, load_config, normalize, validate

STATIC = Path(__file__).parent / "static"

CONFIG_PATH = web.AppKey("config_path", Path)
RUNNER = web.AppKey("runner", BotRunner)


def save_config(path: Path, config: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def build(config: dict) -> dict:
    """Проверка + код одним ответом: интерфейс зовёт это на каждое изменение."""
    issues = validate(config)
    code = None if has_errors(issues) else generate(config)
    return {"issues": [i.to_dict() for i in issues], "code": code}


async def read_json(request: web.Request) -> dict:
    # Только application/json: такой запрос с чужого сайта требует CORS-preflight,
    # которого мы не разрешаем, — значит, посторонняя страница не запустит бота.
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="Нужен application/json")
    try:
        data = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Некорректный JSON")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="Ожидался JSON-объект")
    return data


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def get_config(request: web.Request) -> web.Response:
    config = load_config(request.app[CONFIG_PATH])
    return web.json_response({
        "config": config,
        "path": str(request.app[CONFIG_PATH]),
        "env_token": bool(os.environ.get("BOT_TOKEN")),
        **build(config),
    })


async def put_config(request: web.Request) -> web.Response:
    config = normalize(await read_json(request))
    save_config(request.app[CONFIG_PATH], config)  # черновик с ошибками тоже сохраняем
    return web.json_response(build(config))


async def run_bot(request: web.Request) -> web.Response:
    data = await read_json(request)
    token = str(data.get("token") or "").strip() or os.environ.get("BOT_TOKEN", "")
    if not token:
        return web.json_response({"error": "Нужен токен от @BotFather"}, status=400)
    config = load_config(request.app[CONFIG_PATH])
    runner = request.app[RUNNER]
    try:
        await runner.start(config, token)
    except ConfigError as exc:
        return web.json_response({"error": f"Исправьте ошибки: {exc}"}, status=400)
    except Exception as exc:  # неверный токен, нет сети и т.п.
        return web.json_response({"error": f"Не удалось запустить: {exc}"}, status=400)
    return web.json_response(runner.status())


async def stop_bot(request: web.Request) -> web.Response:
    await read_json(request)
    await request.app[RUNNER].stop()
    return web.json_response(request.app[RUNNER].status())


async def status(request: web.Request) -> web.Response:
    return web.json_response(request.app[RUNNER].status())


async def on_cleanup(app: web.Application) -> None:
    await app[RUNNER].stop()


def create_app(config_path: Path) -> web.Application:
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app[CONFIG_PATH] = config_path
    app[RUNNER] = BotRunner()
    app.router.add_get("/", index)
    app.router.add_get("/api/config", get_config)
    app.router.add_put("/api/config", put_config)
    app.router.add_post("/api/run", run_bot)
    app.router.add_post("/api/stop", stop_bot)
    app.router.add_get("/api/status", status)
    app.on_cleanup.append(on_cleanup)
    return app
