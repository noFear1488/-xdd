import asyncio
import contextlib
import io
import json
import tempfile
import types
import unittest
import unittest.mock
from datetime import datetime
from pathlib import Path

from botbuilder.codegen import ConfigError, generate
from botbuilder.schema import has_errors, starter, validate

try:
    import aiogram  # noqa: F401
except ImportError:  # конструктор работает и без aiogram, но запуск/сервер — нет
    aiogram = None


def handler(trigger, match="", reply="ok", **kw):
    return {"trigger": trigger, "match": match, "reply": reply, **kw}


def config(*handlers, **kw):
    return {"name": "Тест", "parse_mode": "HTML", "handlers": list(handlers), **kw}


def errors(cfg):
    return [(i.handler, i.field) for i in validate(cfg) if i.level == "error"]


class ValidateTest(unittest.TestCase):
    def test_starter_is_valid(self):
        self.assertEqual(validate(starter()), [])

    def test_bad_and_duplicate_commands(self):
        cfg = config(handler("command", "Старт"), handler("command", "/help"), handler("command", "help"))
        self.assertEqual(errors(cfg), [(0, "match"), (2, "match")])

    def test_empty_reply_and_bad_buttons(self):
        cfg = config(handler("command", "start", reply="  ", buttons=[
            {"text": "", "kind": "callback", "value": "x" * 65},
            {"text": "Сайт", "kind": "url", "value": "example.com"},
        ]))
        self.assertEqual(errors(cfg), [(0, "reply"), (0, "buttons.0.text"), (0, "buttons.0.value"), (0, "buttons.1.value")])

    def test_dangling_button_is_only_a_warning(self):
        cfg = config(handler("command", "start", buttons=[{"text": "Меню", "kind": "callback", "value": "menu"}]))
        issues = validate(cfg)
        self.assertFalse(has_errors(issues))
        self.assertEqual([(i.level, i.field) for i in issues], [("warning", "buttons.0.value")])

    def test_unknown_ai_provider(self):
        self.assertEqual(errors(config(handler("fallback"), ai_provider="gigachat")), [(None, "ai_provider")])

    def test_ai_not_allowed_on_buttons(self):
        self.assertEqual(errors(config(handler("callback", "x", ai="..."))), [(0, "ai")])

    def test_second_fallback_is_an_error(self):
        cfg = config(handler("fallback"), handler("fallback"))
        self.assertEqual(errors(cfg), [(1, "trigger")])

    def test_generate_refuses_invalid_config(self):
        with self.assertRaises(ConfigError):
            generate(config(handler("command", "")))


class UsersTest(unittest.TestCase):
    def test_bad_username(self):
        self.assertEqual(errors(config(handler("fallback", users="@ok_name, @ab, @кириллица"))), [(0, "users"), (0, "users")])

    def test_personal_fallback_does_not_clash_with_general(self):
        self.assertEqual(errors(config(handler("fallback", users="@marta"), handler("fallback"))), [])


class CodegenTest(unittest.TestCase):
    def test_output_is_valid_python(self):
        compile(generate(starter()), "bot.py", "exec")

    def test_user_strings_cannot_inject_code(self):
        nasty = '\'\'\'"""\n\\x"); import os; os.system("boom") #'
        cfg = config(handler("callback", "a'b", reply=nasty, buttons=[{"text": nasty, "kind": "callback", "value": "a'b"}]),
                     name=nasty)
        code = generate(cfg)
        compile(code, "bot.py", "exec")
        self.assertNotIn("\nimport os;", code)

    def test_plain_text_mode(self):
        self.assertIn("parse_mode=None", generate(config(handler("fallback"), parse_mode="")))


if aiogram is not None:
    from aiogram.client.session.base import BaseSession
    from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetMe, GetUpdates, SendMessage
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    from botbuilder import runner
    from botbuilder.runner import BotRunner, load_module

    USER = User(id=1, is_bot=False, first_name="Петя")
    CHAT = Chat(id=1, type="private")

    class FakeSession(BaseSession):
        """Записывает запросы к Telegram вместо отправки."""

        def __init__(self):
            super().__init__()
            self.calls = []

        async def make_request(self, bot, method, timeout=None):
            self.calls.append(method)
            if isinstance(method, GetMe):
                return User(id=42, is_bot=True, first_name="Bot", username="test_bot")
            if isinstance(method, GetUpdates):
                await asyncio.sleep(0.05)
                return []
            return True

        async def stream_content(self, *args, **kwargs):
            yield b""

        async def close(self):
            pass


@unittest.skipIf(aiogram is None, "нужен aiogram")
class GeneratedBotTest(unittest.TestCase):
    """Гоняем сгенерированный код через настоящий Dispatcher aiogram."""

    def make(self, cfg):
        module = load_module(generate(cfg))
        bot = module["create_bot"]("42:TEST")
        bot.session = FakeSession()
        return bot, module["create_dispatcher"]()

    def send(self, cfg, *updates):
        bot, dp = self.make(cfg)

        async def run():
            for n, upd in enumerate(updates):
                await dp.feed_update(bot, Update(update_id=n, **upd))

        asyncio.run(run())
        return [c for c in bot.session.calls if not isinstance(c, GetMe)]

    @staticmethod
    def text(text):
        return {"message": Message(message_id=1, date=datetime.now(), chat=CHAT, from_user=USER, text=text)}

    @staticmethod
    def press(data):
        msg = Message(message_id=7, date=datetime.now(), chat=CHAT, text="меню")
        return {"callback_query": CallbackQuery(id="c1", from_user=USER, chat_instance="x", data=data, message=msg)}

    def test_start_sends_menu(self):
        [call] = self.send(starter(), self.text("/start"))
        self.assertIsInstance(call, SendMessage)
        self.assertIn("бот-визитка", call.text)
        buttons = [b for row in call.reply_markup.inline_keyboard for b in row]
        self.assertEqual([b.callback_data for b in buttons], ["services", "contacts"])
        self.assertEqual(len(call.reply_markup.inline_keyboard), 2)  # columns=1 → по кнопке в ряд

    def test_button_edits_message(self):
        answer, edit = self.send(starter(), self.press("services"))
        self.assertIsInstance(answer, AnswerCallbackQuery)
        self.assertIsInstance(edit, EditMessageText)
        self.assertEqual(edit.message_id, 7)
        self.assertEqual(edit.text, "Делаем сайты, ботов и рекламу.")

    def test_unknown_button_still_answered(self):
        [answer] = self.send(starter(), self.press("nope"))
        self.assertIsInstance(answer, AnswerCallbackQuery)

    def test_text_variants_ignore_case(self):
        calls = self.send(starter(), self.text("Здравствуйте"), self.text("что-то"))
        self.assertEqual([c.text for c in calls], [
            "И вам привет! Нажмите /start, чтобы открыть меню.",
            "Не понял 🤔 Нажмите /start.",
        ])

    def test_fallback_runs_last_even_if_listed_first(self):
        cfg = config(handler("fallback", reply="иное"), handler("command", "help", reply="помощь"))
        calls = self.send(cfg, self.text("/help"), self.text("/start"))
        self.assertEqual([c.text for c in calls], ["помощь", "иное"])

    def test_reply_text_is_sent_verbatim(self):
        nasty = 'a\'b"c\\n\n"""'
        [call] = self.send(config(handler("command", "start", reply=nasty)), self.text("/start"))
        self.assertEqual(call.text, nasty)

    @staticmethod
    def text_from(text, username):
        user = User(id=2, is_bot=False, first_name="Марта", username=username)
        return {"message": Message(message_id=1, date=datetime.now(), chat=CHAT, from_user=user, text=text)}

    def test_personal_reply_wins_over_everything(self):
        cfg = starter()
        cfg["handlers"].append(handler("fallback", users="@martasoulll", reply="лично"))
        calls = self.send(cfg, self.text_from("/start", "MartaSoulll"), self.text_from("/start", "other"), self.text("/start"))
        self.assertEqual(calls[0].text, "лично")
        self.assertIn("бот-визитка", calls[1].text)
        self.assertIn("бот-визитка", calls[2].text)  # без username — обычный ответ

    def ai_bot(self, replies):
        """Бот с ИИ-блоком; replies — что вернёт «Claude» на каждый вызов (Exception — сбой)."""
        cfg = config(handler("fallback", reply="запасной", ai="Ты — Мартин друг."))
        module = load_module(generate(cfg))
        seen = []

        class FakeAI:
            class beta:
                class messages:
                    @staticmethod
                    async def create(**kwargs):
                        seen.append(kwargs)
                        reply = replies.pop(0)
                        if isinstance(reply, Exception):
                            raise reply
                        stop, text = reply
                        block = types.SimpleNamespace(type="text", text=text)
                        return types.SimpleNamespace(stop_reason=stop, content=[block] if text else [])

        module["ai_client"] = FakeAI
        bot = module["create_bot"]("42:TEST")
        bot.session = FakeSession()
        return bot, module["create_dispatcher"](), seen

    def chat(self, bot, dp, *texts):
        async def run():
            for n, text in enumerate(texts):
                await dp.feed_update(bot, Update(update_id=n, **self.text(text)))

        asyncio.run(run())
        return [c for c in bot.session.calls if isinstance(c, SendMessage)]

    def test_ai_reply_keeps_history(self):
        bot, dp, seen = self.ai_bot([("end_turn", "Привет, <b>Марта</b>!"), ("end_turn", "Помню тебя.")])
        calls = self.chat(bot, dp, "привет", "помнишь меня?")
        self.assertEqual([c.text for c in calls], ["Привет, <b>Марта</b>!", "Помню тебя."])
        self.assertIsNone(calls[0].parse_mode)  # «<b>» от модели не должен ронять отправку
        self.assertEqual(seen[0]["system"], "Ты — Мартин друг.")
        self.assertEqual(seen[0]["model"], "claude-opus-5-5")
        self.assertEqual([m["role"] for m in seen[1]["messages"]], ["user", "assistant", "user"])

    def test_ai_failure_and_refusal_fall_back(self):
        bot, dp, seen = self.ai_bot([TypeError("нет ключа"), ("refusal", ""), ("end_turn", "ок")])
        with self.assertLogs(level="ERROR"):
            calls = self.chat(bot, dp, "раз", "два", "три")
        self.assertEqual([c.text for c in calls], ["запасной", "запасной", "ок"])
        # неудачные реплики не копятся в истории — третий запрос видит только себя
        self.assertEqual([m["content"] for m in seen[2]["messages"]], ["три"])

    def test_openai_provider(self):
        cfg = config(handler("fallback", reply="запасной", ai="Ты — друг."), ai_provider="openai", ai_model="gpt-test")
        module = load_module(generate(cfg))
        seen = []

        def answer(text, finish="stop"):
            msg = types.SimpleNamespace(content=text, refusal=None)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason=finish)])

        replies = [answer("Привет!"), answer("", "content_filter")]

        class FakeOpenAI:
            class chat:
                class completions:
                    @staticmethod
                    async def create(**kwargs):
                        seen.append(kwargs)
                        return replies.pop(0)

        module["ai_client"] = FakeOpenAI
        bot = module["create_bot"]("42:TEST")
        bot.session = FakeSession()
        calls = self.chat(bot, module["create_dispatcher"](), "хай", "ещё")
        self.assertEqual([c.text for c in calls], ["Привет!", "запасной"])
        self.assertEqual(seen[0]["model"], "gpt-test")
        self.assertEqual(seen[1]["messages"][0], {"role": "system", "content": "Ты — друг."})
        self.assertEqual([m["role"] for m in seen[1]["messages"]], ["system", "user", "assistant", "user"])

    def test_ai_without_key_still_starts_and_falls_back(self):
        cfg = config(handler("fallback", reply="запасной", ai="..."), ai_provider="openai")
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            module = load_module(generate(cfg))  # импорт не должен падать без ключа
            bot = module["create_bot"]("42:TEST")
            bot.session = FakeSession()
            with self.assertLogs(level="ERROR"):
                calls = self.chat(bot, module["create_dispatcher"](), "хай")
        self.assertEqual([c.text for c in calls], ["запасной"])

    def test_url_button(self):
        cfg = config(handler("command", "start", buttons=[{"text": "Сайт", "kind": "url", "value": "https://example.com"}]))
        [call] = self.send(cfg, self.text("/start"))
        self.assertEqual(call.reply_markup.inline_keyboard[0][0].url, "https://example.com")


@unittest.skipIf(aiogram is None, "нужен aiogram")
class RunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_restart_stop(self):
        def fake_load(code):
            module = load_module(code)
            real = module["create_bot"]

            def create_bot(token):
                bot = real(token)
                bot.session = FakeSession()
                return bot

            module["create_bot"] = create_bot
            return module

        with unittest.mock.patch.object(runner, "load_module", fake_load):
            bot = BotRunner()
            await bot.start(starter(), "42:TEST")
            self.assertEqual(bot.status()["username"], "test_bot")
            await asyncio.sleep(0.1)
            await bot.start(starter(), "42:TEST")  # «Применить правки» — перезапуск без ошибок
            self.assertTrue(bot.running)
            await bot.stop()
        self.assertFalse(bot.running)
        self.assertIsNone(bot.error)


@unittest.skipIf(aiogram is None, "нужен aiogram")
class ServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer

        from botbuilder.server import create_app

        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bot.json"
        self.client = TestClient(TestServer(create_app(self.path)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_index_and_starter_config(self):
        self.assertEqual((await self.client.get("/")).status, 200)
        data = await (await self.client.get("/api/config")).json()
        self.assertEqual(data["config"], starter())
        self.assertIn("create_dispatcher", data["code"])

    async def test_put_saves_even_with_errors(self):
        cfg = config(handler("command", "Плохая"))
        data = await (await self.client.put("/api/config", json=cfg)).json()
        self.assertIsNone(data["code"])
        self.assertEqual(data["issues"][0]["field"], "match")
        self.assertEqual(json.loads(self.path.read_text())["handlers"][0]["match"], "Плохая")

    async def test_rejects_non_json_writes(self):
        # Обычная форма с чужого сайта не должна менять конфиг или запускать бота.
        resp = await self.client.put("/api/config", data="{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(resp.status, 415)
        resp = await self.client.post("/api/run", data="token=1", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(resp.status, 415)
        self.assertFalse(self.path.exists())

    async def test_run_needs_token(self):
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            resp = await self.client.post("/api/run", json={"token": ""})
        self.assertEqual(resp.status, 400)
        self.assertIn("BotFather", (await resp.json())["error"])


class ExportCliTest(unittest.TestCase):
    def test_export(self):
        from botbuilder.__main__ import main

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bot.py"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([str(Path(tmp) / "missing.json"), "--export", str(out)]), 0)
            compile(out.read_text(), "bot.py", "exec")

            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps(config(handler("command", ""))))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(main([str(bad), "--export", str(out)]), 1)
            self.assertIn("обработчик 1", err.getvalue())


if __name__ == "__main__":
    unittest.main()
