"""Разбор реальных ответов источников, сохранённых в tests/fixtures."""
import asyncio
import json
import unittest
from unittest import mock
from pathlib import Path

from jobwatch import sources as S

F = Path(__file__).resolve().parent / "fixtures"


class Habr(unittest.TestCase):
    def test_rss(self):
        vs = S.parse_habr((F / "habr.xml").read_text(encoding="utf-8"))
        self.assertEqual(len(vs), 50)
        v = vs[0]
        self.assertEqual(v.title, "DevOps Engineer")                 # из «Требуется «…» (город, зарплата)»
        self.assertEqual(v.company, "Hireway")
        self.assertEqual(v.city, "Москва")
        self.assertIn("280 000", v.salary)
        self.assertTrue(v.remote)
        self.assertTrue(v.url.startswith("https://career.habr.com/vacancies/"))
        self.assertIn("junior", v.tags)
        self.assertGreater(v.published_at, 0)
        self.assertEqual(len({x.key for x in vs}), 50)

    def test_vacancy_page_jobposting(self):
        text = S.parse_jobposting((F / "habr_vacancy.html").read_text(encoding="utf-8"))
        self.assertIn("Hireway", text)
        self.assertNotIn("<", text)


class Geekjob(unittest.TestCase):
    def test_rss_is_parsed_despite_invalid_xml(self):
        vs = S.parse_geekjob((F / "geekjob.xml").read_text(encoding="utf-8"))
        self.assertEqual(len(vs), 100)
        self.assertTrue(all(v.title and v.url.startswith("https://geekjob.ru/vacancy/") for v in vs))
        self.assertTrue(all("utm_" not in v.url for v in vs))
        self.assertTrue(any(v.remote for v in vs))


class Trudvsem(unittest.TestCase):
    def test_api(self):
        vs = S.parse_trudvsem(json.loads((F / "trudvsem.json").read_text(encoding="utf-8")))
        self.assertGreater(len(vs), 50)
        self.assertTrue(all(v.title and v.url.startswith("https://trudvsem.ru/") for v in vs))
        self.assertTrue(any(v.experience_years is not None for v in vs))
        self.assertTrue(any(v.city for v in vs))
        self.assertTrue(any("₽" in v.salary for v in vs))


class Telegram(unittest.TestCase):
    def test_channel_page(self):
        vs = S.parse_telegram((F / "telegram_devops_jobs_feed.html").read_text(encoding="utf-8"), "devops_jobs_feed")
        self.assertGreaterEqual(len(vs), 15)
        v = vs[1]
        self.assertTrue(v.url.startswith("https://t.me/devops_jobs_feed/"))
        self.assertEqual(v.source, "telegram:devops_jobs_feed")
        self.assertIn("\n", v.text)                                  # переносы строк сохранены
        self.assertGreater(v.published_at, 0)

    def test_title(self):
        self.assertEqual(S.telegram_title("Публикатор: hr\n#вакансия #devops\nВакансия: Junior DevOps;\nОпыт: 1 год"),
                         "Junior DevOps")
        self.assertEqual(S.telegram_title("Обсуждение: @x\n#job #remote\nФормат: удалённо\nDevOps-инженер в финтех"),
                         "DevOps-инженер в финтех")
        self.assertEqual(S.telegram_title("Ищем людей в команду\nподробности ниже"), "Ищем людей в команду")


class YoungJunior(unittest.TestCase):
    def test_listing(self):
        vs = S.parse_youngjunior((F / "youngjunior_devops.html").read_text(encoding="utf-8"))
        self.assertEqual(len(vs), 20)
        self.assertEqual(len({v.key for v in vs}), 20)
        self.assertEqual((vs[0].ext_id, vs[0].title, vs[0].url),
                         ("94652", "Системный инженер (SRE)", "https://youngjunior.ru/devops/jobs/94652"))
        self.assertTrue(all(S.needs_enrich(v) for v in vs))

    def test_enrich_from_jobposting(self):
        page = (F / "youngjunior_vacancy.html").read_text(encoding="utf-8")
        v = S.parse_youngjunior((F / "youngjunior_devops.html").read_text(encoding="utf-8"))[0]

        async def fake_get(session, url, ssl_ctx=None):
            return page
        with mock.patch.object(S, "_get", fake_get):
            v = asyncio.run(S.enrich(None, v))
        self.assertEqual(v.company, "Т-Банк")
        self.assertTrue(v.remote)
        self.assertIn("Docker", v.text)
        self.assertGreater(v.published_at, 0)

    def test_fetch_dedupes_categories(self):
        listing = (F / "youngjunior_devops.html").read_text(encoding="utf-8")
        calls = []

        async def fake_get(session, url, ssl_ctx=None):
            calls.append(url)
            if "/go/" in url:
                raise RuntimeError("HTTP 500")
            return listing
        with mock.patch.object(S, "_get", fake_get), mock.patch("asyncio.sleep", mock.AsyncMock()):
            vs = asyncio.run(S.fetch_youngjunior(None, {"categories": ["devops", "go", "data"], "kinds": ["jobs", "internships"]}))
        self.assertEqual(len(calls), 6)
        self.assertIn("https://youngjunior.ru/data/internships", calls)
        self.assertEqual(len(vs), 20)                       # одна и та же вакансия в разных категориях — один раз


if __name__ == "__main__":
    unittest.main()
