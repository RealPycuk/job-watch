import unittest
from pathlib import Path

from jobwatch import analyze as A
from jobwatch import config
from jobwatch.models import Vacancy
from jobwatch.profile import Profile, compile_profile

CFG = config.load(Path(__file__).resolve().parents[1] / "config.toml")
OWNER = compile_profile(Profile.from_config(CFG))


def vac(title="DevOps Engineer", text="", **kw):
    return Vacancy(source=kw.pop("source", "habr"), ext_id="1", title=title, url="https://x", text=text, **kw)


class LocationRule(unittest.TestCase):
    """СПб — любой формат; другой город — только удалёнка."""

    def ok(self, v):
        return A.check(CFG, OWNER, v)

    def test_spb_any_format(self):
        for text in ("Офис в Санкт-Петербурге", "Гибрид, СПб", "Питер, можно удалённо", "Saint Petersburg, on-site"):
            d = self.ok(vac(text=text))
            self.assertTrue(d.ok, text)
            self.assertTrue(d.location.startswith("СПб"), d.location)

    def test_spb_from_city_field(self):
        self.assertTrue(self.ok(vac(city="Санкт-Петербург", text="Только офис")).ok)

    def test_other_city_needs_remote(self):
        self.assertFalse(self.ok(vac(city="Москва", text="Офис, 5/2")).ok)
        self.assertFalse(self.ok(vac(text="Москва, гибрид 3/2")).ok)
        self.assertFalse(self.ok(vac(text="Офис (Ташкент, Узбекистан)")).ok)
        d = self.ok(vac(city="Москва", text="Можно удалённо"))
        self.assertTrue(d.ok)
        self.assertEqual(d.location, "удалённо (Москва)")

    def test_remote_flag_from_source(self):
        self.assertTrue(self.ok(vac(city="Новосибирск", remote=True)).ok)

    def test_negated_remote_is_not_remote(self):
        self.assertFalse(self.ok(vac(text="Москва. Удалёнка не рассматривается")).ok)
        self.assertFalse(self.ok(vac(text="Екатеринбург, без удалённой работы")).ok)

    def test_unknown_location_is_notified(self):
        d = self.ok(vac(text="Ищем инженера в команду платформы"))
        self.assertTrue(d.ok)
        self.assertEqual(d.location, "формат не указан")


class RoleLevelExperience(unittest.TestCase):
    def test_roles(self):
        for t in ("Junior DevOps", "SRE-инженер", "Инженер эксплуатации", "Platform Engineer", "Linux-администратор"):
            self.assertTrue(A.check(CFG, OWNER, vac(title=t, text="удалённо")).ok, t)
        for t in ("Разработчик .Net", "Менеджер по продажам", "Администратор баз данных"):
            self.assertEqual(A.check(CFG, OWNER, vac(title=t, text="удалённо, devops-культура")).reason, "роль", t)

    def test_sysadmin_needs_linux(self):
        self.assertTrue(A.check(CFG, OWNER, vac(title="Системный администратор", text="Linux, удалённо")).ok)
        self.assertEqual(A.check(CFG, OWNER, vac(title="Системный администратор", text="Windows, 1С, удалённо")).reason, "роль")

    def test_role_in_telegram_body(self):
        v = vac(title="Публикатор: hr", text="Публикатор: hr\nИщем DevOps-инженера, удалённо", source="telegram:x")
        self.assertTrue(A.check(CFG, OWNER, v).ok)

    def test_level_exclusions(self):
        for t in ("Senior DevOps", "Lead SRE", "Ведущий DevOps-инженер", "Head of Infrastructure", "DevOps Architect",
                  "Сеньор DevOps", "Синьор SRE", "DevOps Team & Community Leader"):
            self.assertEqual(A.check(CFG, OWNER, vac(title=t, text="удалённо")).reason, "уровень", t)

    def test_level_tag(self):
        self.assertEqual(A.check(CFG, OWNER, vac(title="Junior DevOps", text="удалённо")).level, "junior")
        self.assertEqual(A.check(CFG, OWNER, vac(title="Стажёр DevOps", text="удалённо")).level, "junior")
        self.assertEqual(A.check(CFG, OWNER, vac(title="Middle DevOps", text="удалённо")).level, "middle")

    def test_experience(self):
        cases = {"Опыт работы от 3 лет": 3, "опыт от 3-х лет": 3, "Опыт: от 1 года": 1, "5+ years of experience": 5,
                 "не менее 2 лет коммерческого опыта": 2, "2 года опыта с Kubernetes": 2, "без требований": None}
        for text, years in cases.items():
            self.assertEqual(A.experience_years(vac(text=text)), years, text)
        self.assertEqual(A.check(CFG, OWNER, vac(text="удалённо. Опыт от 3 лет")).reason, "опыт")
        self.assertTrue(A.check(CFG, OWNER, vac(text="удалённо. Опыт от 1 года")).ok)
        self.assertEqual(A.check(CFG, OWNER, vac(text="удалённо", experience_years=4)).reason, "опыт")

    def test_resume_posts_are_skipped(self):
        self.assertEqual(A.check(CFG, OWNER, vac(text="#резюме #devops ищу удалённую работу")).reason, "не вакансия")


class Extraction(unittest.TestCase):
    def test_skills(self):
        matched, missing = A.skills(CFG, CFG.skills_mine, "Docker, k8s, Terraform, GitLab CI, Prometheus")
        self.assertEqual(matched, ["Docker", "Kubernetes", "Prometheus"])
        self.assertEqual(missing, ["GitLab CI", "Terraform"])

    def test_salary(self):
        self.assertEqual(A.salary(vac(text="ЗП: от 150 000 ₽ на руки")), "от 150 000 ₽")
        self.assertEqual(A.salary(vac(text="вилка 120 000 - 180 000 руб.")), "120 000 - 180 000 руб")
        self.assertEqual(A.salary(vac(text="Зарплата по договорённости")), "")
        self.assertEqual(A.salary(vac(salary="от 100 ₽")), "от 100 ₽")

    def test_fingerprint_ignores_case_and_punctuation(self):
        a = vac(title="DevOps-инженер", company="ООО «Ромашка»")
        b = vac(title="devops инженер", company="ООО Ромашка")
        self.assertEqual(A.fingerprint(a), A.fingerprint(b))


class OtherProfiles(unittest.TestCase):
    """Профили других пользователей: свой город, форматы, уровень, зарплата."""

    def m(self, **kw):
        base = dict(title="QA", roles=["qa engineer", "тестировщик"], level="junior", city="Москва")
        return compile_profile(Profile(**{**base, **kw}))

    def test_roles_from_keywords(self):
        m = self.m()
        self.assertTrue(A.check(CFG, m, vac(title="Junior QA Engineer", text="Москва, офис")).ok)
        self.assertTrue(A.check(CFG, m, vac(title="Тестировщик ПО", text="удалённо")).ok)
        self.assertEqual(A.check(CFG, m, vac(title="DevOps Engineer", text="удалённо")).reason, "роль")

    def test_home_city_and_formats(self):
        m = self.m(home_formats=["remote", "hybrid"])
        self.assertEqual(A.check(CFG, m, vac(title="QA Engineer", text="Москва, только офис")).reason, "город/формат")
        d = A.check(CFG, m, vac(title="QA Engineer", text="Москва, гибрид"))
        self.assertTrue(d.ok)
        self.assertEqual(d.location, "Москва · гибрид")
        self.assertFalse(A.check(CFG, m, vac(title="QA Engineer", text="Санкт-Петербург, офис")).ok)

    def test_no_remote_in_other_cities(self):
        m = self.m(remote_other_cities=False)
        self.assertEqual(A.check(CFG, m, vac(title="QA Engineer", text="Казань, удалённо")).reason, "город/формат")

    def test_level_rules(self):
        self.assertEqual(A.check(CFG, self.m(), vac(title="Senior QA Engineer", text="удалённо")).reason, "уровень")
        self.assertTrue(A.check(CFG, self.m(level="middle", experience_years=3),
                                vac(title="Senior QA Engineer", text="удалённо")).ok)
        self.assertEqual(A.check(CFG, self.m(level="middle"), vac(title="Lead QA", text="удалённо")).reason, "уровень")

    def test_salary_min(self):
        m = self.m(salary_min=150000)
        self.assertEqual(A.check(CFG, m, vac(title="QA Engineer", text="удалённо", salary="от 80 000 до 100 000 ₽")).reason,
                         "зарплата")
        self.assertTrue(A.check(CFG, m, vac(title="QA Engineer", text="удалённо", salary="до 200 000 ₽")).ok)
        self.assertTrue(A.check(CFG, m, vac(title="QA Engineer", text="удалённо")).ok)       # зарплата не указана
        self.assertTrue(A.check(CFG, m, vac(title="QA Engineer", text="удалённо", salary="2000 $")).ok)

    def test_salary_parse(self):
        self.assertEqual(A.salary_rub_max("от 120 000 до 150 000 ₽"), 150000)
        self.assertEqual(A.salary_rub_max("200к"), 200000)
        self.assertIsNone(A.salary_rub_max("1500 $"))
        self.assertIsNone(A.salary_rub_max(""))


if __name__ == "__main__":
    unittest.main()
