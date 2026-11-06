import asyncio
import tempfile
import unittest

from jobwatch import coach
from test_app import OWNER, Harness
from test_tailor import FakeLLM, VACANCY


class Coach(unittest.TestCase):
    def test_quick_fit(self):
        self.assertEqual(coach.quick_fit(["Docker"], []), "")                      # мало данных — без оценки
        self.assertEqual(coach.quick_fit(["Docker", "Linux", "Ansible"], ["Terraform"]),
                         "🟢 Совпадение по навыкам: 4/5 — стоит откликнуться")
        self.assertTrue(coach.quick_fit(["Docker"], ["Go", "Java", "Kafka"]).startswith("🔴 Совпадение по навыкам: 2/5"))

    def test_chunks(self):
        parts = coach.chunks("а" * 3000 + "\n\n" + "б" * 3000 + "\n\nв")
        self.assertEqual([len(p) for p in parts], [3000, 3003])
        self.assertTrue(all(len(p) <= 3900 for p in coach.chunks("x" * 9000)))

    def test_evaluate_marks_score_and_strips_markdown(self):
        llm = FakeLLM("**Оценка: 2/5 — не тратить время**\n## Почему: другая роль")
        out = asyncio.run(coach.evaluate(llm, "резюме", VACANCY))
        self.assertEqual(out, "🔴 Оценка: 2/5 — не тратить время\nПочему: другая роль")
        self.assertEqual(llm.limits, [coach.MAX_TOKENS])

    def test_app_uses_users_gigachat_and_card_buttons(self):
        async def main():
            with tempfile.TemporaryDirectory() as d:
                async with Harness(d) as h:
                    llm = FakeLLM("1. Как устроен Docker?\nКак ответить: про проект.")
                    h.app.giga_for = lambda u: llm
                    out = await h.app.coach_text(await h.store.user(OWNER), "i", VACANCY)
                    self.assertIn("Как устроен Docker", out)
                    self.assertIn("ВАКАНСИЯ:\nDevOps-инженер", llm.prompts[0])
                    self.assertTrue(llm.prompts[0].startswith("СЕГОДНЯ: "))
                    data = [b.callback_data for row in h.app.vac_buttons(7).inline_keyboard for b in row]
                    self.assertIn("e:7", data)
                    self.assertIn("i:7", data)
        asyncio.run(main())
