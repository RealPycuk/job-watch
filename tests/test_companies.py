"""Карьерные сайты компаний (сохранённые ответы в tests/fixtures) и извлечение контактов HR."""
import asyncio
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from jobwatch import analyze as A
from jobwatch import companies as C
from jobwatch import config, sources
from jobwatch.models import Vacancy
from jobwatch.profile import Profile, compile_profile

F = Path(__file__).resolve().parent / "fixtures"
CFG = config.load(Path(__file__).resolve().parents[1] / "config.toml")
OWNER = compile_profile(Profile.from_config(CFG))


def load(name):
    p = F / name
    return json.loads(p.read_text(encoding="utf-8")) if name.endswith(".json") else p.read_text(encoding="utf-8")


class Companies(unittest.TestCase):
    def check_common(self, vs, source, company, url_prefix):
        self.assertTrue(vs, source)
        self.assertEqual(len({v.key for v in vs}), len(vs), "ключи уникальны")
        for v in vs:
            self.assertEqual(v.source, source)
            self.assertTrue(v.title)
            self.assertTrue(v.url.startswith(url_prefix), v.url)
            self.assertTrue(v.company.startswith(company), v.company)

    def test_yandex(self):
        vs = C.parse_yandex(load("yandex.json"))
        self.check_common(vs, "yandex", "Яндекс", "https://yandex.ru/jobs/vacancies/")
        self.assertTrue(all(v.city for v in vs))
        self.assertTrue(all("Формат:" in v.text for v in vs))
        self.assertTrue(all(v.published_at > 0 for v in vs))

    def test_vk(self):
        data = load("vk.json")
        vs = C.parse_vk(data)
        self.assertEqual(len(vs), len(data["results"]))
        self.check_common(vs, "vk", "VK", "https://team.vk.company/vacancy/")
        self.assertTrue(any(v.city for v in vs))

    def test_sber(self):
        vs = C.parse_sber(load("sber.html"))
        self.check_common(vs, "sber", "Сбер", "https://developers.sber.ru/kak-v-sbere/vacancies/")
        self.assertGreater(len(vs), 20)
        self.assertTrue(any(v.experience_years is not None for v in vs))
        self.assertTrue(all("Формат:" in v.text for v in vs))
        self.assertEqual(C.parse_sber("<html>нет данных</html>"), [])

    def test_mts(self):
        vs = C.parse_mts(load("mts.json"))
        self.check_common(vs, "mts", "МТС", "https://job.mts.ru/vacancies/")
        self.assertTrue(all("devops" in v.title.lower() for v in vs))      # фильтр по названию отработал на стороне API
        self.assertTrue(all(v.city for v in vs))

    def test_kaspersky_and_avito_html(self):
        k = C.parse_kaspersky(load("kaspersky.html"))
        self.check_common(k, "kaspersky", "Лаборатория Касперского", "https://careers.kaspersky.ru/vacancy/")
        self.assertGreaterEqual(len(k), 10)
        a = C.parse_avito(load("avito.html"))
        self.check_common(a, "avito", "Авито", "https://career.avito.com/vacancies/")
        self.assertTrue(any("DevOps" in v.title for v in a))

    def test_selectel(self):
        vs = C.parse_selectel(load("selectel.html"))
        self.check_common(vs, "selectel", "Selectel", "https://selectel.ru/careers/all/vacancy/")
        self.assertEqual(len(vs), 24)
        self.assertTrue(all(v.city and v.city != v.title for v in vs))
        self.assertTrue(any(v.city == "Санкт-Петербург" for v in vs))

    def test_x5(self):
        vs = C.parse_x5(load("x5.html"))
        self.check_common(vs, "x5", "X5 Tech", "https://x5.tech/vacancy/")
        self.assertGreaterEqual(len(vs), 15)
        self.assertTrue(any(re.search(r"Офис|Гибрид|Удал", v.text) for v in vs))

    def test_company_feeds_in_aggregators(self):
        """Хабр по company_ids и «Работа России» по ОГРН — для компаний, чьи сайты закрыты для серверов."""
        calls = []

        async def fake_get(session, url):
            calls.append(url)
            return "{}" if "trudvsem" in url else "<rss></rss>"

        async def main():
            with mock.patch.object(sources, "_get", fake_get):
                await sources.fetch_habr(None, {"queries": ["devops"], "company_ids": [42]})
                await sources.fetch_trudvsem(None, {"queries": [], "companies": ["1027700067328"]})
        asyncio.run(main())
        self.assertIn("https://career.habr.com/vacancies/rss?company_ids%5B%5D=42&type=all", calls)
        self.assertIn("https://opendata.trudvsem.ru/api/v1/vacancies/company/1027700067328?limit=100", calls)
        for name in ("company_ids", "companies"):
            self.assertTrue(CFG.raw["sources"]["habr" if name == "company_ids" else "trudvsem"][name])

    def test_generic_site(self):
        vs = C.parse_site(load("site_cloudru.html"), company="Cloud.ru", base_url="https://cloud.ru/career/vacancies",
                          link=r"^https://cloud\.ru/career/vacancies/\d+$")
        self.check_common(vs, "site:cloud-ru", "Cloud.ru", "https://cloud.ru/career/vacancies/")
        self.assertGreaterEqual(len(vs), 5)
        self.assertTrue(any(v.experience_years is not None for v in vs))   # «Более 6 лет», «От 3 до 6 лет» из карточки
        self.assertTrue(all(sources.needs_enrich(v) for v in vs))

    def test_generic_site_picks_richest_link(self):
        page = ('<a href="/job/1"><img></a><a href="/job/1">Подробнее</a>'
                '<a href="/job/1"><span>DevOps-инженер</span><span>Удалённо</span></a><a href="/about">О нас</a>')
        vs = C.parse_site(page, company="Acme", base_url="https://acme.ru/jobs", link=r"/job/\d+$")
        self.assertEqual([(v.title, v.text, v.url) for v in vs], [("DevOps-инженер", "DevOps-инженер · Удалённо", "https://acme.ru/job/1")])

    def test_sites_in_config_are_valid(self):
        for site in CFG.raw["sources"]["sites"]["list"]:
            self.assertTrue(site["company"] and site["url"].startswith("https://"))
            re.compile(site["link"])

    def test_jobposting_location(self):
        page = ('<script type="application/ld+json">{"@type": "JobPosting", "description": "<p>Текст</p>", "jobLocation": '
                '{"@type": "Place", "address": {"addressLocality": "Москва, удалённая работа"}}}</script>')
        self.assertEqual(sources.jobposting_location(page), ("Москва", True))
        self.assertEqual(sources.jobposting_location("<html></html>"), ("", None))

    def test_min_years(self):
        self.assertEqual(C._min_years("От 1 года до 3 лет"), 1)
        self.assertEqual(C._min_years("1-3 года"), 1)
        self.assertEqual(C._min_years("Нет опыта"), 0)
        self.assertIsNone(C._min_years(""))

    def test_registered_with_titles(self):
        for name in C.SOURCES:
            self.assertIn(name, sources.SOURCES)
            self.assertIn(name, sources.TITLES)
            self.assertIn(name, CFG.raw["sources"])

    def test_owner_filter_on_company_vacancies(self):
        """Senior/lead и не-DevOps отсекаются и на сайтах компаний."""
        all_v = (C.parse_yandex(load("yandex.json")) + C.parse_vk(load("vk.json")) + C.parse_sber(load("sber.html"))
                 + C.parse_mts(load("mts.json")) + C.parse_kaspersky(load("kaspersky.html")) + C.parse_avito(load("avito.html")))
        ok = [v for v in all_v if A.check(CFG, OWNER, v).ok]
        for v in ok:
            self.assertNotRegex(v.title.lower(), r"senior|ведущ|руковод|тимлид|lead")


class Contacts(unittest.TestCase):
    def v(self, text, source="habr", contacts=None):
        return Vacancy(source=source, ext_id="1", title="DevOps", url="https://x", text=text, contacts=contacts or [])

    def test_emails_from_text(self):
        self.assertEqual(A.contacts(self.v("Резюме присылайте на hr@company.ru или Jobs@Company.RU.")),
                         ["hr@company.ru", "Jobs@Company.RU"])

    def test_skips_service_addresses(self):
        self.assertEqual(A.contacts(self.v("noreply@site.ru, logo@2x.png, user@example.com")), [])

    def test_field_contacts_first_and_dedup(self):
        v = self.v("пишите hr@company.ru", contacts=["hr@company.ru (Анна)"])
        self.assertEqual(A.contacts(v), ["hr@company.ru (Анна)"])

    def test_telegram_handles_only_from_contact_lines(self):
        text = ("Обсуждение: @devops_jobs\nКоманда @platform_team ищет инженера\n"
                "Контакты: @anna_hr, t.me/recruiter_bob\nОтклик через бота @jobs_apply_bot")
        got = A.contacts(self.v(text, source="telegram:devops_jobs_feed"))
        self.assertEqual(got, ["@anna_hr", "@recruiter_bob"])

    def test_no_handles_outside_telegram(self):
        self.assertEqual(A.contacts(self.v("Контакты: @anna_hr")), [])

    def test_trudvsem_contact_fields(self):
        vs = sources.parse_trudvsem(json.loads((F / "trudvsem.json").read_text(encoding="utf-8")))
        with_contacts = [v for v in vs if v.contacts]
        self.assertTrue(with_contacts)
        self.assertTrue(all("@" in v.contacts[0] for v in with_contacts))

    def test_jobposting_fallback_to_og_description(self):
        page = '<html><head><meta property="og:description" content="Ищем DevOps &amp; SRE"></head></html>'
        self.assertEqual(sources.parse_jobposting(page), "Ищем DevOps & SRE")


if __name__ == "__main__":
    unittest.main()
