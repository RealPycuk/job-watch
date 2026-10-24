"""Многопользовательский цикл (фейковые источники и бот), миграция БД, приглашения, шифрование, резюме, GigaChat."""
import asyncio
import io
import json
import sqlite3
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import aiohttp
from aiohttp import web
from cryptography.fernet import Fernet

from jobwatch import app as A
from jobwatch import config, letter, resume, sources
from jobwatch.models import Vacancy
from jobwatch.profile import Profile
from jobwatch.secret import Vault
from jobwatch.store import Store

ROOT = Path(__file__).resolve().parents[1]
OWNER, FRIEND = 1, 2


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def v(n, title="Junior DevOps", text="удалённо", company=None, age_h=1, source="habr"):
    return Vacancy(source=source, ext_id=str(n), title=title, url=f"https://x/{n}", text=text,
                   company=company if company is not None else f"C{n}", published_at=time.time() - age_h * 3600)


def run(coro):
    return asyncio.run(coro)


class Harness:
    """App с временной БД, владельцем из config.toml и фейковым ботом."""

    def __init__(self, d: str, **cfg_over):
        self.env = config.Env(bot_token="x", owner_id=OWNER, gigachat_key="", gigachat_model="GigaChat-2",
                              gigachat_scope="GIGACHAT_API_PERS", gigachat_api_url="", data_dir=Path(d),
                              config_path=ROOT / "config.toml", profile_path=Path(d) / "profile.md",
                              secret=Fernet.generate_key().decode())
        Path(d, "profile.md").write_text("Кратко: Делаю инфраструктуру.\nПодпись: Руслан", encoding="utf-8")
        self.cfg = config.load(self.env.config_path)
        for k, val in cfg_over.items():
            setattr(self.cfg, k, val)
        self.cfg.raw["sources"] = {"habr": {"enabled": True}}
        self.bot = FakeBot()

    async def __aenter__(self):
        self.store = await Store.open(self.env.data_dir / "db.sqlite", owner_id=OWNER)
        self.http = aiohttp.ClientSession()
        self.app = A.App(self.env, self.cfg, self.store, self.http, self.bot, Vault(self.env.secret))
        await self.app.seed_owner()
        return self

    async def __aexit__(self, *exc):
        await self.http.close()
        await self.store.close()

    async def add_friend(self, **profile):
        await self.store.add_user(FRIEND, step="active", invited_by=OWNER)
        await self.store.update_user(FRIEND, profile=Profile(**profile).to_dict())

    async def cycle(self, batch):
        with mock.patch.dict(sources.SOURCES, {"habr": mock.AsyncMock(return_value=batch)}), \
                mock.patch("asyncio.sleep", mock.AsyncMock()):
            return await self.app.cycle()

    def to(self, uid):
        return [t for c, t in self.bot.sent if c == uid]


class Cycle(unittest.TestCase):
    def test_owner_gets_matching_once(self):
        async def main():
            with tempfile.TemporaryDirectory() as d:
                async with Harness(d) as h:
                    batch = [v(1), v(2, title="Senior DevOps"), v(3, text="Москва, офис")]
                    first, second = await h.cycle(batch), await h.cycle(batch)
                    return first, second, h.to(OWNER)
        first, second, sent = run(main())
        self.assertEqual(first, {"new": 3, "sent": 1})
        self.assertEqual(second, {"new": 0, "sent": 0})
        self.assertEqual(len(sent), 1)
        self.assertIn("Junior DevOps", sent[0])

    def test_each_user_gets_own_vacancies(self):
        async def main():
            with tempfile.TemporaryDirectory() as d:
                async with Harness(d) as h:
                    await h.add_friend(title="QA", roles=["qa engineer"], level="junior", city="Москва")
                    await h.cycle([v(1, title="Junior DevOps"), v(2, title="Junior QA Engineer", text="Москва, офис")])
                    return h.to(OWNER), h.to(FRIEND)
        owner, friend = run(main())
        self.assertEqual(len(owner), 1)
        self.assertIn("DevOps", owner[0])
        self.assertEqual(len(friend), 1)
        self.assertIn("QA", friend[0])
        self.assertIn("Москва · офис", friend[0])

    def test_paused_and_inactive_users_get_nothing(self):
        async def main():
            with tempfile.TemporaryDirectory() as d:
                async with Harness(d) as h:
                    await h.add_friend(title="DevOps", roles=["devops"], city="Москва")
                    await h.store.update_user(FRIEND, paused=1)
                    await h.store.add_user(3, step="resume")
                    await h.cycle([v(1)])
                    return h.to(FRIEND), h.to(3)
        self.assertEqual(run(main()), ([], []))

    def test_first_run_capped(self):
        async def main():
            with tempfile.TemporaryDirectory() as d:
                async with Harness(d, first_run_max_messages=3) as h:
                    return (await h.cycle([v(i) for i in range(10)] + [v(100, age_h=200)]))["sent"]
        self.assertEqual(run(main()), 3)

    def test_backfill_for_new_user(self):
        async def main():
            with tempfile.TemporaryDirectory() as d:
                async with Harness(d) as h:
                    await h.cycle([v(1, title="Junior QA Engineer"), v(2, title="Junior DevOps")])
                    await h.add_friend(title="QA", roles=["qa engineer"], city="Москва")
                    with mock.patch("asyncio.sleep", mock.AsyncMock()):
                        n = await h.app.backfill(FRIEND)
                        again = await h.app.backfill(FRIEND)
                    return n, again, h.to(FRIEND)
        n, again, sent = run(main())
        self.assertEqual((n, again), (1, 0))
        self.assertIn("QA", sent[0])

    def test_cross_source_duplicate_per_user(self):
        async def main():
            with tempfile.TemporaryDirectory() as d:
                async with Harness(d) as h:
                    await h.cycle([v(1, title="DevOps-инженер", company="Ромашка")])
                    await h.cycle([v(2, title="DevOps инженер", company="Ромашка", source="geekjob")])
                    return h.to(OWNER)
        self.assertEqual(len(run(main())), 1)

    def test_card_shows_hr_contacts(self):
        async def main():
            with tempfile.TemporaryDirectory() as d:
                async with Harness(d) as h:
                    await h.cycle([v(1, text="удалённо. Резюме на hr@acme.ru")])
                    return h.to(OWNER)
        self.assertIn("✉️ <code>hr@acme.ru</code>", run(main())[0])

    def test_card_escapes_html(self):
        async def main():
            with tempfile.TemporaryDirectory() as d:
                async with Harness(d) as h:
                    await h.cycle([v(1, title="DevOps <script>", company="A&B")])
                    return h.to(OWNER)
        sent = run(main())
        self.assertIn("DevOps &lt;script&gt;", sent[0])
        self.assertIn("A&amp;B", sent[0])


class StoreTest(unittest.TestCase):
    def test_migration_from_v1_keeps_owner_history(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "db.sqlite"
            db = sqlite3.connect(path)
            db.executescript("""
                create table vacancies(id integer primary key autoincrement, key text not null unique, source text not null,
                  title text not null, company text, url text not null, text text, salary text, city text, remote integer,
                  experience integer, published_at real, fingerprint text, status text not null, reason text, location text,
                  matched text, missing text, saved integer not null default 0, hidden integer not null default 0,
                  created_at real not null);
                create index idx_vac_fp on vacancies(fingerprint, created_at);
                insert into vacancies(key, source, title, url, status, matched, missing, saved, created_at)
                  values ('habr:1', 'habr', 'DevOps', 'https://x/1', 'sent', '[]', '[]', 1, 1.0),
                         ('habr:2', 'habr', 'QA', 'https://x/2', 'filtered', '[]', '[]', 0, 1.0);""")
            db.commit()
            db.close()

            async def main():
                s = await Store.open(path, owner_id=OWNER)
                vid = await s.vacancy_id("habr:1")
                rows = [tuple(r) for r in await (await s.db.execute(
                    "select user_id, vacancy_id, status, saved from deliveries order by vacancy_id")).fetchall()]
                await s.close()
                return vid, rows
            vid, rows = run(main())
        self.assertEqual(vid, 1)
        self.assertEqual(rows, [(OWNER, 1, "sent", 1), (OWNER, 2, "filtered", 0)])

    def test_invite_is_single_use(self):
        async def main():
            with tempfile.TemporaryDirectory() as d:
                s = await Store.open(Path(d) / "db.sqlite")
                code = await s.create_invite(OWNER)
                res = (await s.use_invite(code, 5), await s.use_invite(code, 6), await s.use_invite("nope", 7))
                await s.close()
                return res
        self.assertEqual(run(main()), (OWNER, None, None))

    def test_delete_user_removes_everything(self):
        async def main():
            with tempfile.TemporaryDirectory() as d:
                async with Harness(d) as h:
                    await h.add_friend(title="DevOps", roles=["devops"], city="Москва")
                    await h.cycle([v(1)])
                    await h.store.kv_set(f"vac_text:{FRIEND}", "вакансия")
                    await h.store.kv_set(f"vac_text:1{FRIEND}", "чужая")
                    await h.store.delete_user(FRIEND)
                    left = await (await h.store.db.execute("select count(*) from deliveries where user_id=?",
                                                           (FRIEND,))).fetchone()
                    return (await h.store.user(FRIEND), left[0], await h.store.kv_get(f"vac_text:{FRIEND}"),
                            await h.store.kv_get(f"vac_text:1{FRIEND}"))
        self.assertEqual(run(main()), (None, 0, "", "чужая"))


class VaultTest(unittest.TestCase):
    def test_roundtrip_and_wrong_key(self):
        a, b = Vault(Fernet.generate_key().decode()), Vault(Fernet.generate_key().decode())
        token = a.encrypt("secret-key")
        self.assertNotIn("secret-key", token)
        self.assertEqual(a.decrypt(token), "secret-key")
        self.assertEqual(b.decrypt(token), "")
        self.assertEqual(a.decrypt(None), "")
        with self.assertRaises(ValueError):
            Vault("")


class ResumeTest(unittest.TestCase):
    SKILLS = ["Linux", "Docker", "Python"]

    def test_parse_profile_validates_fields(self):
        raw = 'Вот профиль:\n```json\n' + json.dumps({
            "title": "Junior Python Developer", "roles": ["Python Developer", "backend"], "level": "guru",
            "experience_years": "2", "skills": ["python", "Docker", "Kubernetes"], "city": "Казань",
            "home_formats": ["remote", "space"], "remote_other_cities": False, "salary_min": "abc",
            "summary": "Пишу на Python.", "signature": "Анна, @anna"}, ensure_ascii=False) + '\n```'
        p = resume.parse_profile(raw, self.SKILLS)
        self.assertEqual(p.roles, ["python developer", "backend"])
        self.assertEqual(p.level, "junior")                 # неизвестный уровень → junior
        self.assertEqual(p.experience_years, 2)
        self.assertEqual(p.skills, ["Python", "Docker"])     # только из известного списка
        self.assertEqual(p.home_formats, ["remote"])
        self.assertFalse(p.remote_other_cities)
        self.assertIsNone(p.salary_min)

    def test_parse_profile_errors(self):
        with self.assertRaises(resume.ResumeError):
            resume.parse_profile("не знаю", self.SKILLS)
        with self.assertRaises(resume.ResumeError):
            resume.parse_profile('{"title": ""}', self.SKILLS)

    def test_extract_docx_and_limits(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            body = "".join(f"<w:p><w:r><w:t>Строка резюме номер {i} про Linux и Docker</w:t></w:r></w:p>" for i in range(10))
            z.writestr("word/document.xml", f"<w:document><w:body>{body}</w:body></w:document>")
        text = resume.extract_text("cv.docx", buf.getvalue())
        self.assertIn("Строка резюме номер 9", text)
        self.assertEqual(text.count("\n") >= 9, True)
        with self.assertRaises(resume.ResumeError):
            resume.extract_text("cv.docx", b"not a zip")
        with self.assertRaises(resume.ResumeError):
            resume.extract_text("cv.exe", b"x" * 500)
        with self.assertRaises(resume.ResumeError):
            resume.extract_text("cv.txt", "коротко".encode())


class GigaChatClient(unittest.TestCase):
    def test_token_cached_and_chat(self):
        calls = []

        async def oauth(req):
            calls.append(("oauth", req.headers.get("Authorization"), (await req.post()).get("scope"), req.headers.get("RqUID")))
            return web.json_response({"access_token": "tok", "expires_at": int((time.time() + 1800) * 1000)})

        async def chat(req):
            body = await req.json()
            calls.append(("chat", req.headers.get("Authorization"), body["model"]))
            return web.json_response({"choices": [{"message": {"content": "**Здравствуйте!** Письмо"}}]})

        async def main():
            appw = web.Application()
            appw.router.add_post("/oauth", oauth)
            appw.router.add_post("/v1/chat/completions", chat)
            runner = web.AppRunner(appw)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            try:
                async with aiohttp.ClientSession() as s:
                    g = letter.GigaChat(s, "KEY", api_url=f"http://127.0.0.1:{port}/v1", oauth_url=f"http://127.0.0.1:{port}/oauth")
                    return await g.chat("sys", "user"), await g.chat("sys", "user")
            finally:
                await runner.cleanup()

        out1, _ = run(main())
        self.assertEqual(letter.clean(out1), "Здравствуйте! Письмо")
        oauth_calls = [c for c in calls if c[0] == "oauth"]
        self.assertEqual(len(oauth_calls), 1)
        self.assertEqual(oauth_calls[0][1:3], ("Basic KEY", "GIGACHAT_API_PERS"))
        self.assertEqual([c[1:] for c in calls if c[0] == "chat"], [("Bearer tok", "GigaChat-2")] * 2)

    def test_ca_file_only_root(self):
        import ssl
        ctx = ssl.create_default_context(cafile=str(letter.CA_FILE))
        self.assertEqual(len(ctx.get_ca_certs()), 1)


class Template(unittest.TestCase):
    def test_title_guess_and_untitled_template(self):
        self.assertEqual(letter.title_guess("\n  DevOps-инженер (junior)\nТребования: Docker"), "DevOps-инженер (junior)")
        self.assertEqual(letter.title_guess("https://hh.ru/vacancy/1\nтекст"), "")
        self.assertEqual(letter.title_guess("Мы ищем " + "очень " * 30 + "хорошего инженера"), "")
        text = letter.template("", "", {"title": "", "company": ""}, ["Docker"], [], [])
        self.assertTrue(text.startswith("Здравствуйте! Откликаюсь на вашу вакансию."))
        self.assertIn("название и компанию определи", letter.build_prompt("факты", {"title": "", "company": ""}, "т", [], [], []))

    def test_template_and_repos(self):
        repos = letter.pick_repos({"CI/CD": "r1", "Docker": "r1", "Prometheus": "r2", "Ansible": "r3"},
                                  ["Docker", "CI/CD", "Prometheus", "Ansible"])
        self.assertEqual(repos, ["r1", "r2"])
        text = letter.template("Сопровождаю инфраструктуру.", "Иван, @ivan_dev", {"title": "DevOps", "company": "Acme"},
                               ["Docker"], ["Terraform"], repos)
        self.assertTrue(text.startswith("Здравствуйте! Откликаюсь на вакансию «DevOps» в Acme."))
        self.assertIn("Terraform", text)
        self.assertTrue(text.endswith("Иван, @ivan_dev"))


if __name__ == "__main__":
    unittest.main()
