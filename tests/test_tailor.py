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
        self.answers, self.prompts = list(answers), []

    async def chat(self, system, user):
        self.prompts.append(user)
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
        self.assertEqual(body, "# Руслан\n- Docker, Terraform")
        self.assertEqual(warning, "")
        llm = FakeLLM("# Руслан\n- CentOS", "# Руслан\n- CentOS, Docker")
        body, warning = asyncio.run(tailor.rewrite(llm, RESUME, VACANCY))
        self.assertIn("«CentOS» нет ни в резюме", llm.prompts[1])
        self.assertEqual(warning, "⚠️ Проверь, этого нет в исходном резюме: CentOS")

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
                    giga = FakeLLM("# Руслан\n- Docker\n=== ИЗМЕНЕНИЯ ===\n- порядок")
                    tailor_claude = tailor.Claude
                    tailor.Claude = lambda *a: broken
                    h.app.giga_for = lambda u: giga
                    try:
                        doc, warning, how = await h.app.tailor_resume(await h.store.user(OWNER), VACANCY)
                    finally:
                        tailor.Claude = tailor_claude
                    self.assertEqual((how, warning), ("GigaChat", ""))
                    xml = zipfile.ZipFile(io.BytesIO(doc)).read("word/document.xml").decode()
                    self.assertIn("Docker", xml)
                    self.assertNotIn("ИЗМЕНЕНИЯ", xml)
                    self.assertNotIn("порядок", xml)
        asyncio.run(main())
