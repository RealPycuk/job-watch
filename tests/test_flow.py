"""Сквозной сценарий через настоящий диспетчер aiogram: приглашение → согласие → ключ → резюме → профиль → вакансии.

Telegram подменён фейковой сессией (запросы бота записываются), GigaChat — заглушкой.
"""
import asyncio
import datetime
import json
import tempfile
import unittest
from unittest import mock

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, GetMe, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from jobwatch import app as A
from test_app import OWNER, Harness, v

FRIEND = 77
PROFILE_JSON = json.dumps({"title": "Junior QA", "roles": ["qa engineer", "тестировщик"], "level": "junior",
                           "experience_years": 0, "skills": ["Python"], "city": "Москва",
                           "home_formats": ["office", "hybrid", "remote"], "remote_other_cities": True,
                           "salary_min": None, "summary": "Тестирую.", "signature": "Анна"}, ensure_ascii=False)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        now = datetime.datetime.now()
        if isinstance(method, GetMe):
            return User(id=1000, is_bot=True, first_name="bot", username="test_bot")
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message(message_id=len(self.calls), date=now, chat=Chat(id=getattr(method, "chat_id", 0) or 0, type="private"),
                           text=method.text)
        if isinstance(method, (AnswerCallbackQuery, DeleteMessage)):
            return True
        return True


class FakeGiga:
    async def token(self):
        return "tok"

    async def chat(self, system, user):
        return PROFILE_JSON if "JSON" in system else "Здравствуйте! Письмо."


class Flow(unittest.TestCase):
    def test_invited_user_onboarding(self):
        async def main():
            with tempfile.TemporaryDirectory() as d:
                async with Harness(d) as h:
                    session = FakeSession()
                    bot = Bot("42:" + "x" * 35, session=session)
                    h.app.bot = bot
                    dp = Dispatcher()
                    dp.include_router(A.build_router(h.app))
                    user = User(id=FRIEND, is_bot=False, first_name="Анна", username="anna")
                    chat = Chat(id=FRIEND, type="private")
                    n = iter(range(1, 1000))

                    async def msg(text):
                        m = Message(message_id=next(n), date=datetime.datetime.now(), chat=chat, from_user=user, text=text)
                        await dp.feed_update(bot, Update(update_id=next(n), message=m))

                    async def click(data):
                        m = Message(message_id=next(n), date=datetime.datetime.now(), chat=chat, from_user=bot_user, text="x")
                        cq = CallbackQuery(id=str(next(n)), from_user=user, chat_instance="c", data=data, message=m)
                        await dp.feed_update(bot, Update(update_id=next(n), callback_query=cq))

                    bot_user = User(id=1000, is_bot=True, first_name="bot")

                    def texts():
                        return [c.text for c in session.calls if isinstance(c, SendMessage) and c.chat_id == FRIEND]

                    await msg("/start")                                      # без приглашения — отказ
                    denied = texts()[-1]

                    code = await h.store.create_invite(OWNER)
                    await msg(f"/start inv_{code}")
                    after_invite = (await h.store.user(FRIEND))["step"]
                    await click("c:ok")
                    after_consent = (await h.store.user(FRIEND))["step"]

                    with mock.patch.object(A.App, "make_giga", lambda self, key: FakeGiga()), \
                            mock.patch.object(A.App, "giga_for", lambda self, u: FakeGiga()), \
                            mock.patch("asyncio.sleep", mock.AsyncMock()):
                        await msg("super-secret-gigachat-key")
                        u = await h.store.user(FRIEND)
                        key_deleted = any(isinstance(c, DeleteMessage) for c in session.calls)
                        stored_key = h.app.vault.decrypt(u["gigachat_key"])
                        after_key = u["step"]

                        await msg("Анна, тестировщик. " + "Опыт ручного и автотестирования на Python. " * 10)
                        after_resume = (await h.store.user(FRIEND))["step"]

                        await click("p:city")
                        await msg("Казань")
                        city = h.app.profile_of(await h.store.user(FRIEND)).city

                        await h.cycle([v(1, title="Junior QA Engineer", text="Казань, офис"),
                                       v(2, title="Junior DevOps", text="удалённо")])
                        before_done = len([t for t in texts() if "QA Engineer" in t])
                        await click("p:done")
                        after_done = (await h.store.user(FRIEND))["step"]
                        cards = [t for t in texts() if "💼" in t]

                    others = [c.text for c in session.calls if isinstance(c, SendMessage) and c.chat_id == OWNER]
                    return dict(denied=denied, after_invite=after_invite, after_consent=after_consent, key_deleted=key_deleted,
                                stored_key=stored_key, after_key=after_key, after_resume=after_resume, city=city,
                                before_done=before_done, after_done=after_done, cards=cards, owner_msgs=others)

        r = asyncio.run(main())
        self.assertIn(f"Твой ID: <code>{FRIEND}</code>", r["denied"])
        self.assertEqual(r["after_invite"], "consent")
        self.assertEqual(r["after_consent"], "key")
        self.assertTrue(r["key_deleted"])                         # сообщение с ключом удалено из чата
        self.assertEqual(r["stored_key"], "super-secret-gigachat-key")
        self.assertEqual(r["after_key"], "resume")
        self.assertEqual(r["after_resume"], "review")
        self.assertEqual(r["city"], "Казань")
        self.assertEqual(r["before_done"], 0)                     # до подтверждения профиля вакансии не идут
        self.assertEqual(r["after_done"], "active")
        self.assertEqual(len(r["cards"]), 1)                      # после — backfill подходящей
        self.assertIn("Junior QA Engineer", r["cards"][0])
        self.assertTrue(any("По приглашению зашёл" in t for t in r["owner_msgs"]))


if __name__ == "__main__":
    unittest.main()
