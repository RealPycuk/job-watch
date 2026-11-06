"""Разбор вакансии и подготовка к собеседованию — для самого кандидата, поэтому честно, без «я всё знаю»."""
import datetime
import re

MAX_TOKENS = 1800

EVAL_PROMPT = """Ты опытный IT-рекрутер и тимлид. Оцени, стоит ли кандидату откликаться на вакансию.
Пиши для самого кандидата, по-русски, честно и конкретно, без воды. Опирайся только на резюме и текст вакансии,
ничего не придумывай. Формат строго такой, без markdown (без звёздочек и решёток):
Оценка: N/5 — <одно из: «откликаться», «можно попробовать», «не тратить время»>
Почему: <1–2 предложения>
Сильные стороны:
- <требование вакансии, которое закрывает опыт из резюме — с конкретикой из резюме>
Чего не хватает:
- <требование, которого нет в резюме, и насколько оно критично для этой вакансии>
Что подчеркнуть в отклике:
- <2–3 пункта>
Риски и вопросы к вакансии:
- <подозрительное: размытые обязанности, завышенные требования для junior, нет зарплаты и т. п.; если нет — «нет»>
Шкала: 5 — закрывает почти всё ключевое; 4 — ключевое закрыто, мелкие пробелы; 3 — половина ключевого;
2 — ключевые требования не закрыты; 1 — другая роль или уровень."""

INTERVIEW_PROMPT = """Ты тимлид, который проводит техническое собеседование на эту вакансию. Подготовь кандидата.
По-русски, конкретно, без markdown (без звёздочек и решёток). Опирайся на текст вакансии и резюме,
не придумывай кандидату опыт: если по теме опыта в резюме нет, так и скажи и предложи, как честно ответить.
Формат:
Вопросы, которые скорее всего зададут:
1. <вопрос по требованию вакансии>
Как ответить: <2–3 предложения: что сказать и какой пример из резюме привести>
(8–10 вопросов: сначала по ключевым технологиям вакансии, затем 1–2 про инциденты/отладку и 1 поведенческий)
Что повторить перед собеседованием:
- <3–5 тем, которые есть в вакансии и слабо покрыты резюме>
Что спросить у работодателя:
- <3 вопроса, которые покажут интерес и помогут понять команду>"""

SCORE_RX = re.compile(r"Оценка:\s*([1-5])\s*/\s*5")


def build_prompt(resume_text: str, vacancy: str) -> str:
    today = datetime.date.today().strftime("%d.%m.%Y")         # иначе модель неверно считает стаж «с 10.2025»
    return f"СЕГОДНЯ: {today}\n\nРЕЗЮМЕ КАНДИДАТА:\n{resume_text.strip()}\n\nВАКАНСИЯ:\n{vacancy.strip()[:8000]}"


def clean(text: str) -> str:
    text = re.sub(r"\*\*|__|^#+\s*", "", text.strip(), flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def evaluate(llm, resume_text: str, vacancy: str) -> str:
    out = clean(await llm.chat(EVAL_PROMPT, build_prompt(resume_text, vacancy), max_tokens=MAX_TOKENS))
    if m := SCORE_RX.search(out):                     # цветная метка к оценке — видно с первого взгляда
        n = int(m.group(1))
        out = out.replace(m.group(0), f"{'🟢' if n >= 4 else '🟡' if n == 3 else '🔴'} {m.group(0)}", 1)
    return out


async def interview(llm, resume_text: str, vacancy: str) -> str:
    return clean(await llm.chat(INTERVIEW_PROMPT, build_prompt(resume_text, vacancy), max_tokens=MAX_TOKENS))


def chunks(text: str, limit: int = 3900) -> list[str]:
    """Длинный ответ — на несколько сообщений, по границам абзацев."""
    out, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:
            out.append(para[:limit])
            para = para[limit:]
        if cur and len(cur) + len(para) + 2 > limit:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


def quick_fit(matched: list[str], missing: list[str]) -> str:
    """Быстрая оценка без нейросети — по совпавшим и недостающим навыкам из текста вакансии."""
    total = len(matched) + len(missing)
    if total < 2:
        return ""
    n = 1 + round(4 * len(matched) / total)
    label = "стоит откликнуться" if n >= 4 else "можно попробовать" if n == 3 else "скорее не твоё"
    return f"{'🟢' if n >= 4 else '🟡' if n == 3 else '🔴'} Совпадение по навыкам: {n}/5 — {label}"
