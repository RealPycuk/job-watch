import asyncio
import io
import tempfile
import unittest
import zipfile

from jobwatch import app as A
from jobwatch import tailor
from test_app import FRIEND, OWNER, Harness

RESUME = "# Руслан Хуснутдинов\nJunior DevOps\n@rusyajez\n## Навыки\n- Docker, Ansible\nИзучаю: Terraform, Helm."
VACANCY = "DevOps-инженер\nТребования: Docker, Terraform, GitLab CI, опыт с Linux от 1 года."


class FakeLLM:
    def __init__(self, *answers):
        self.answers, self.prompts, self.limits = list(answers), [], []

    async def chat(self, system, user, max_tokens=700):
        self.prompts.append(user)
        self.limits.append(max_tokens)
        return self.answers.pop(0)


class Tailor(unittest.TestCase):
    def test_split_and_docx(self):
        body = tailor.clean("Вот резюме:\n```\n# Руслан\n**DevOps**\n- Docker & <Linux>\nИзучаю: Helm\n"
                            "=== ИЗМЕНЕНИЯ ===\n- поднял Docker\n```")
        self.assertEqual(body, "# Руслан\nDevOps\n- Docker & <Linux>")
        z = zipfile.ZipFile(io.BytesIO(tailor.to_docx(body)))
        self.assertEqual(sorted(z.namelist()), ["[Content_Types].xml", "_rels/.rels", "word/document.xml"])
        xml = z.read("word/document.xml").decode()
        self.assertIn("• Docker &amp; &lt;Linux&gt;", xml)
        self.assertIn("Руслан", xml)
        self.assertEqual(tailor.filename(VACANCY), "Резюме — DevOps-инженер.docx")
        out = tailor.clean("# Руслан  \n- Изменения конфигурации — через Ansible.\n---\n### Изменения, внесённые под вакансию:\n- x")
        self.assertEqual(out, "# Руслан\n- Изменения конфигурации — через Ansible.")
        self.assertEqual(tailor.clean("# Р\n## Навыки\n- Docker\n\nЧто изменено:\n- порядок"), "# Р\n## Навыки\n- Docker")
        self.assertEqual(tailor.unknown_terms("Linux (CentOS), GitLab CI, github.com/x/Foo", RESUME + VACANCY), ["CentOS"])

    def test_rewrite_retries_on_made_up_numbers_and_drops_hedges(self):
        llm = FakeLLM("# Руслан\n- 500 серверов", "# Руслан\n- Docker, Terraform\n- Сейчас изучаю Helm.\n=== ИЗМЕНЕНИЯ ===\n- навыки")
        body, warning = asyncio.run(tailor.rewrite(llm, RESUME, VACANCY))
        self.assertEqual(len(llm.prompts), 2)
        self.assertIn("ПРЕДЫДУЩИЙ ВАРИАНТ НАРУШИЛ ПРАВИЛА", llm.prompts[1])
        self.assertEqual(body, "# Руслан\n- Docker, Terraform\n@rusyajez")   # потерянный контакт вернулся
        self.assertIn("не было: Terraform", warning)       # Terraform в исходнике только в «Изучаю»
        llm = FakeLLM("# Руслан\n- CentOS", "# Руслан\n- CentOS, Docker")
        body, warning = asyncio.run(tailor.rewrite(llm, RESUME, VACANCY))
        self.assertIn("«CentOS» нет ни в резюме", llm.prompts[1])
        self.assertEqual(warning, "⚠️ Проверь перед отправкой — нет в исходном резюме: CentOS")

    def test_restore_contacts_and_links(self):
        src = "Подпись: Руслан Хуснутдинов, Telegram @rusyajez, github.com/RealPycuk\nРепозиторий: https://github.com/RealPycuk/x"
        self.assertEqual(tailor.restore_contacts("# Руслан\nDevOps\nTelegram @rusyajez\n## Опыт", src),
                         "# Руслан\nDevOps\nTelegram @rusyajez, github.com/RealPycuk\n## Опыт")
        self.assertEqual(tailor.restore_contacts("# Руслан\nDevOps\n## Опыт", src),
                         "# Руслан\nDevOps\n@rusyajez, github.com/RealPycuk\n## Опыт")
        self.assertEqual(tailor.clean("# Р\nКод: [https://github.com/a/b](https://github.com/a/b)"), "# Р\nКод: https://github.com/a/b")
        self.assertEqual(tailor.moved_from_vacancy("- CI на GitLab CI", "Изучаю: GitLab CI", "GitLab CI"), ["CI", "GitLab"])

    def test_made_up_dates_and_long_answers(self):
        self.assertEqual(tailor.strip_made_up_numbers("### Проект (май 2023 – 2025)\n- 2 реплики (с 2025)", "с 2025, 2 реплики"),
                         "### Проект\n- 2 реплики (с 2025)")
        llm = FakeLLM("# Р")
        asyncio.run(tailor.rewrite(llm, "", ""))
        self.assertEqual(llm.limits, [tailor.MAX_TOKENS])
        self.assertEqual(tailor.restore_contacts("# Р\nDevOps\n@rusyajez\n- https://github.com/RealPycuk/x",
                                                 "@rusyajez github.com/RealPycuk https://github.com/RealPycuk/x"),
                         "# Р\nDevOps\n@rusyajez, github.com/RealPycuk\n- https://github.com/RealPycuk/x")

    def test_education_urls_and_empty_contact_labels(self):
        src = "Учёба: СПбГЭТУ «ЛЭТИ», ФКТИ, выпуск 2027.\nРепо: https://github.com/RealPycuk/x"
        out = tailor.strip_made_up_contacts("# Р\nТел.:  | Email:\n- GitLab CI: https://gitlab.com/ru/p\n"
                                            "- Код: https://github.com/RealPycuk/x", src)
        self.assertEqual(out, "# Р\n- Код: https://github.com/RealPycuk/x")
        out = tailor.restore_education("# Р\n## Образование\nЛЭТИ, факультет КТиЗИ, окончание в июне 2027 г.\n\n## Навыки\n- Docker", src)
        self.assertEqual(out, "# Р\n## Образование\nСПбГЭТУ «ЛЭТИ», ФКТИ, выпуск 2027.\n\n## Навыки\n- Docker")
        self.assertEqual(tailor.clean("# Р\n- Ansible, Terraform (основы)."), "# Р\n- Ansible, Terraform.")
        self.assertEqual(tailor.strip_made_up_contacts("# Р\nТел.: не указан\nE-mail: не указан\nTelegram: @r", "@r"), "# Р\nTelegram: @r")
        self.assertEqual(tailor.restore_education("# Р\n## Навыки\n- Docker", src),
                         "# Р\n## Навыки\n- Docker\n\n## Образование\nСПбГЭТУ «ЛЭТИ», ФКТИ, выпуск 2027.")

    def test_lost_skills_and_restored_names(self):
        self.assertEqual(tailor.lost_matches("# Р\n- Ansible", RESUME, VACANCY), ["DevOps", "Docker", "Terraform"])
        src = "Учёба: СПбГЭТУ «ЛЭТИ», ФКТИ, «Информационные системы и технологии», выпуск 2027."
        out = tailor.restore_names("## Образование\nСПбГЭТУ «ЛЭТИ», факультет КТИТ, «Информационные системы и технологии»", src)
        self.assertEqual(out, "## Образование\nСПбГЭТУ «ЛЭТИ», ФКТИ, «Информационные системы и технологии», выпуск 2027.")

    def test_contacts_and_experience_tools_are_not_made_up(self):
        made_up = ("# Руслан\n@rusyajez\nКонтакты:\nТелефон: +7 XXXX XXX XX XX\nEmail: ru@yandex.ru\n"
                   "## Опыт\n- CI/CD на GitLab CI\n## Навыки\n- GitLab CI")
        self.assertEqual(tailor.strip_made_up_contacts(made_up, RESUME),
                         "# Руслан\n@rusyajez\n## Опыт\n- CI/CD на GitLab CI\n## Навыки\n- GitLab CI")
        self.assertEqual(tailor.moved_from_vacancy(made_up, RESUME, VACANCY), ["CI", "GitLab"])
        resume = RESUME + "\nРепозиторий: https://github.com/x/repo"
        llm = FakeLLM(made_up, made_up)
        body, warning = asyncio.run(tailor.rewrite(llm, resume, VACANCY))
        self.assertIn("в опыт вписан «GitLab»", llm.prompts[1])
        self.assertIn("выдуманные контакты", llm.prompts[1])
        self.assertNotIn("yandex", body)
        self.assertTrue(body.endswith("## Репозитории\n- https://github.com/x/repo"))
        self.assertIn("чего в твоём опыте не было: CI, GitLab", warning)

    def test_owner_only_and_fallback_to_gigachat(self):
        async def main():
            with tempfile.TemporaryDirectory() as d:
                async with Harness(d) as h:
                    await h.add_friend(roles=["qa"])
                    self.assertIs(h.app.menu_for(OWNER), A.MENU_OWNER)
                    self.assertIs(h.app.menu_for(FRIEND), A.MENU)
                    flat = lambda m: [b.callback_data for row in m.inline_keyboard for b in row]  # noqa: E731
                    self.assertIn("r:5", flat(h.app.vac_buttons(5, owner=True)))
                    self.assertNotIn("r:5", flat(h.app.vac_buttons(5)))
                    h.app.env.anthropic_key = "sk-test"
                    broken = FakeLLM()                          # Claude «упал» — дальше GigaChat
                    giga = FakeLLM(*["# Руслан\n- Docker\n=== ИЗМЕНЕНИЯ ===\n- порядок"] * 2)
                    tailor_claude = tailor.Claude
                    tailor.Claude = lambda *a: broken
                    h.app.giga_for = lambda u: giga
                    try:
                        doc, warning, how = await h.app.tailor_resume(await h.store.user(OWNER), VACANCY)
                    finally:
                        tailor.Claude = tailor_claude
                    self.assertEqual(how, "GigaChat")
                    self.assertIn("не было: Docker", warning)      # в резюме владельца из тестов Docker нет
                    xml = zipfile.ZipFile(io.BytesIO(doc)).read("word/document.xml").decode()
                    self.assertIn("Docker", xml)
                    self.assertNotIn("ИЗМЕНЕНИЯ", xml)
                    self.assertNotIn("порядок", xml)
        asyncio.run(main())
