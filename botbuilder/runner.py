"""Запуск сгенерированного бота прямо из интерфейса.

Исполняется ровно тот код, что отдаётся на «Скачать bot.py», — без отдельной
реализации, поэтому поведение в интерфейсе и в продакшене совпадает.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import suppress

from .codegen import generate


class EventLog(logging.Handler):
    """Последние строки логов aiogram — для панели «Журнал»."""

    def __init__(self, size: int = 100):
        super().__init__(logging.INFO)
        self.lines: deque[dict] = deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append({
            "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname.lower(),
            "text": record.getMessage(),
        })


def load_module(code: str) -> dict:
    namespace: dict = {"__name__": "botbuilder_generated"}
    exec(compile(code, "bot.py", "exec"), namespace)
    return namespace


class BotRunner:
    def __init__(self) -> None:
        self.log = EventLog()
        logger = logging.getLogger("aiogram")
        logger.setLevel(logging.INFO)
        logger.addHandler(self.log)
        self._task: asyncio.Task | None = None
        self._dispatcher = None
        self._bot = None
        self.username: str | None = None
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict:
        return {
            "running": self.running,
            "username": self.username,
            "error": self.error,
            "log": list(self.log.lines),
        }

    async def start(self, config: dict, token: str) -> None:
        """Перезапускает бота с новым конфигом. Ошибки токена и конфига летят наружу."""
        await self.stop()
        self.error = None
        module = load_module(generate(config))
        bot = module["create_bot"](token)
        try:
            me = await bot.get_me()  # сразу проверяем токен, а не молча падаем в фоне
        except BaseException:
            await bot.session.close()
            raise
        self.username = me.username
        self._bot = bot
        self._dispatcher = module["create_dispatcher"]()
        self._task = asyncio.create_task(self._dispatcher.start_polling(bot, handle_signals=False))
        self._task.add_done_callback(self._on_done)
        self.log.emit(logging.makeLogRecord({"levelname": "INFO", "msg": f"Бот @{me.username} запущен"}))

    def _on_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            self.error = str(task.exception())

    async def stop(self) -> None:
        if not self.running:
            return
        try:
            await self._dispatcher.stop_polling()
        except RuntimeError:
            self._task.cancel()  # polling ещё не успел стартовать — останавливать нечего
        # Ошибка polling уже сохранена в _on_done; wait_for сам отменит зависшую задачу.
        with suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
            await asyncio.wait_for(self._task, timeout=15)
        await self._bot.session.close()  # если polling не стартовал, сессию никто не закрыл
        self.log.emit(logging.makeLogRecord({"levelname": "INFO", "msg": "Бот остановлен"}))
