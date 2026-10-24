"""Сквозной сценарий через настоящий диспетчер aiogram: приглашение → согласие → ключ → резюме → профиль → вакансии.

Telegram подменён фейковой сессией (запросы бота записываются), GigaChat — заглушкой.
"""
import asyncio
import datetime
import io
import json
import tempfile
import time
import unittest
from unittest import mock

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, GetMe, SendMessage
from aiogram.types import CallbackQuery, Chat, Document, Message, Update, User

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
    prompts = []

    async def token(self):
        return "tok"

    async def chat(self, system, user):
        FakeGiga.prompts.append(user)
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

                    async def msg(text, settle=True):
                        m = Message(message_id=next(n), date=datetime.datetime.now(), chat=chat, from_user=user, text=text)
                        await dp.feed_update(bot, Update(update_id=next(n), message=m))
                        if settle:                                   # дождаться склейки частей длинного текста
                            while h.app.pending:
                                await asyncio.gather(*(b["task"] for b in list(h.app.pending.values())), return_exceptions=True)

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
                            mock.patch("asyncio.sleep", mock.AsyncMock()), mock.patch.object(A, "PASTE_GAP", 0.05):
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

                        # письмо по вставленному тексту вакансии
                        vacancy = ("Junior QA Engineer\nООО Ромашка ищет тестировщика. Требования: Python, pytest, SQL, "
                                   "понимание клиент-серверной архитектуры. Обязанности: ручное и автоматизированное тестирование.")
                        await msg("/letter")
                        await msg("короткий текст")
                        too_short = texts()[-1]
                        await msg(vacancy)
                        letter_msg = texts()[-1]
                        letter_prompt = FakeGiga.prompts[-1]
                        await click("lt:again")
                        again = texts()[-1]
                        await msg("просто фраза")
                        hint = texts()[-1]

                        # вакансия двумя сообщениями (Telegram режет длинный текст) — одно письмо по обеим частям
                        letters_before = len([t for t in texts() if "Письмо." in t])
                        await msg("/letter")
                        await msg("DevOps Engineer\nТребования: Docker, Kubernetes, " + "опыт эксплуатации Linux. " * 10, settle=False)
                        await msg("Продолжение: Ansible, Terraform, мониторинг Prometheus.")
                        two_parts = FakeGiga.prompts[-1]
                        letters_two = len([t for t in texts() if "Письмо." in t]) - letters_before

                        # старый режим «жду резюме» истёк — длинный текст идёт в письмо, а не в профиль
                        profile_before = (await h.store.user(FRIEND))["profile"]
                        await h.store.kv_set(f"await_cv:{FRIEND}", str(time.time() - 3600))
                        await msg("Системный администратор\n" + "Требования: Linux, сети, Nginx. " * 15)
                        profile_kept = (await h.store.user(FRIEND))["profile"] == profile_before

                        # резюме по ошибке — вернуть прежний профиль
                        await msg("/cv")
                        await msg("Совсем другое резюме. " * 30)
                        replaced = (await h.store.user(FRIEND))["profile"] != profile_before
                        await click("p:undo")
                        restored = (await h.store.user(FRIEND))["profile"] == profile_before

                        # вакансия файлом: письмо, профиль не меняется
                        async def send_file(name, data):
                            doc = Document(file_id="f", file_unique_id="u", file_name=name, file_size=len(data))
                            m = Message(message_id=next(n), date=datetime.datetime.now(), chat=chat, from_user=user, document=doc)
                            with mock.patch.object(bot, "download", mock.AsyncMock(return_value=io.BytesIO(data))):
                                await dp.feed_update(bot, Update(update_id=next(n), message=m))

                        vac_file = "SRE-инженер\nТребования: Grafana, Loki, " + "дежурства и разбор инцидентов. " * 10
                        await send_file("vacancy.txt", vac_file.encode())
                        file_prompt = FakeGiga.prompts[-1]
                        file_letter = texts()[-1]
                        file_profile_kept = (await h.store.user(FRIEND))["profile"] == profile_before
                        await send_file("photo.jpg", b"x" * 300)
                        bad_file = texts()[-1]

                    others = [c.text for c in session.calls if isinstance(c, SendMessage) and c.chat_id == OWNER]
                    return dict(denied=denied, after_invite=after_invite, after_consent=after_consent, key_deleted=key_deleted,
                                stored_key=stored_key, after_key=after_key, after_resume=after_resume, city=city,
                                before_done=before_done, after_done=after_done, cards=cards, owner_msgs=others,
                                too_short=too_short, letter_msg=letter_msg, letter_prompt=letter_prompt, again=again, hint=hint,
                                vac_text=await h.store.kv_get(f"vac_text:{FRIEND}"),
                                two_parts=two_parts, letters_two=letters_two, profile_kept=profile_kept,
                                replaced=replaced, restored=restored, file_prompt=file_prompt, file_letter=file_letter,
                                file_profile_kept=file_profile_kept, bad_file=bad_file)

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
        self.assertIn("слишком коротко", r["too_short"])
        self.assertIn("Здравствуйте! Письмо.", r["letter_msg"])
        self.assertIn("ВАКАНСИЯ: Junior QA Engineer", r["letter_prompt"])
        self.assertIn("pytest", r["letter_prompt"])
        self.assertIn("Тестирую.", r["letter_prompt"])                    # факты из резюме пользователя
        self.assertIn("Совпадающие навыки: Python", r["letter_prompt"])
        self.assertIn("Здравствуйте! Письмо.", r["again"])
        self.assertIn("/letter", r["hint"])
        self.assertIn("SRE-инженер", r["vac_text"])                      # последняя присланная вакансия (файлом)
        self.assertEqual(r["letters_two"], 1)
        self.assertIn("Kubernetes", r["two_parts"])
        self.assertIn("Terraform", r["two_parts"])
        self.assertTrue(r["profile_kept"])
        self.assertTrue(r["replaced"])
        self.assertTrue(r["restored"])
        self.assertIn("ВАКАНСИЯ: SRE-инженер", r["file_prompt"])
        self.assertIn("Loki", r["file_prompt"])
        self.assertIn("Здравствуйте! Письмо.", r["file_letter"])
        self.assertTrue(r["file_profile_kept"])
        self.assertIn("Нужен PDF, DOCX или TXT", r["bad_file"])


if __name__ == "__main__":
    unittest.main()
