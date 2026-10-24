"""Резюме → текст → профиль (через GigaChat, с проверкой каждого поля)."""
import io
import json
import re
import zipfile

from .profile import FORMATS, LEVELS, Profile

MAX_FILE = 5 * 1024 * 1024
MAX_TEXT = 12000


class ResumeError(Exception):
    pass


def extract_text(filename: str, data: bytes) -> str:
    if len(data) > MAX_FILE:
        raise ResumeError("Файл больше 5 МБ")
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:10])
        except Exception as e:
            raise ResumeError(f"Не удалось прочитать PDF: {e}") from e
    elif name.endswith(".docx"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "replace")
        except (zipfile.BadZipFile, KeyError) as e:
            raise ResumeError("Не похоже на файл Word (.docx)") from e
        xml = re.sub(r"</w:p>|<w:br/>|<w:tab/>", "\n", xml)
        text = re.sub(r"<[^>]+>", "", xml)
    elif name.endswith((".txt", ".md")):
        text = data.decode("utf-8", "replace")
    else:
        raise ResumeError("Нужен PDF, DOCX или TXT")
    text = re.sub(r"[ \t ]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()
    if len(text) < 200:
        raise ResumeError("В файле почти нет текста — если это скан, пришли текстом или в DOCX")
    return text[:MAX_TEXT]


def profile_prompt(skills_known: list[str]) -> str:
    return f"""Ты разбираешь резюме соискателя в IT и возвращаешь ТОЛЬКО JSON без пояснений и без markdown.
Поля:
- "title": желаемая должность (строка);
- "roles": 2–6 коротких ключевых слов для поиска вакансий по названию (например "devops", "sre", "python developer",
  "qa engineer", "системный администратор") — на языке, на котором пишут названия таких вакансий;
- "level": одно из {list(LEVELS)};
- "experience_years": коммерческий опыт в годах, целое число (учёба и пет-проекты считай как 0–1);
- "skills": навыки кандидата, выбранные ТОЛЬКО из списка: {", ".join(skills_known)};
- "city": город проживания или пустая строка;
- "home_formats": какие форматы подходят в своём городе, подмножество {list(FORMATS)} (если не указано — все три);
- "remote_other_cities": true, если готов работать удалённо на компании из других городов (если не указано — true);
- "salary_min": минимальная зарплата в рублях в месяц или null;
- "summary": 1–2 предложения о кандидате от первого лица, только факты из резюме;
- "signature": имя и фамилия, плюс Telegram или e-mail, если они есть в резюме.
Не придумывай того, чего нет в резюме."""


def parse_profile(raw: str, skills_known: list[str]) -> Profile:
    """Достаёт JSON из ответа модели и приводит каждое поле к допустимым значениям."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ResumeError("Модель не вернула профиль")
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ResumeError("Модель вернула испорченный JSON") from e

    def s(key, limit=200):
        v = d.get(key)
        return str(v).strip()[:limit] if isinstance(v, (str, int, float)) else ""

    roles = [str(r).strip().lower()[:40] for r in d.get("roles") or [] if isinstance(r, str) and r.strip()][:6]
    level = d.get("level") if d.get("level") in LEVELS else "junior"
    try:
        exp = max(0, min(int(d.get("experience_years") or 0), 40))
    except (TypeError, ValueError):
        exp = 0
    known = {k.lower(): k for k in skills_known}
    skills = []
    for x in d.get("skills") or []:
        k = known.get(str(x).strip().lower())
        if k and k not in skills:
            skills.append(k)
    formats = [f for f in d.get("home_formats") or [] if f in FORMATS] or list(FORMATS)
    try:
        salary = int(d["salary_min"]) if d.get("salary_min") not in (None, "", 0) else None
        salary = salary if salary and 10000 <= salary <= 10_000_000 else None
    except (TypeError, ValueError):
        salary = None
    if not roles:
        title = s("title", 60).lower()
        roles = [title] if title else []
    if not roles:
        raise ResumeError("Не удалось понять, какую работу ты ищешь — напиши роли вручную")
    return Profile(title=s("title", 80), roles=roles, level=level, experience_years=exp, skills=skills,
                   city=s("city", 60), home_formats=formats, remote_other_cities=d.get("remote_other_cities") is not False,
                   salary_min=salary, summary=s("summary", 400), signature=s("signature", 150))
