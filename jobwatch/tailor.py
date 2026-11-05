"""Резюме под конкретную вакансию (только для владельца): Claude, если задан ключ, иначе GigaChat; результат — .docx."""
import io
import re
import zipfile
from xml.sax.saxutils import escape

from . import letter

CLAUDE_MODEL = "claude-opus-5-5"
MAX_TOKENS = 3000           # страница резюме — ~1500 токенов; лимит писем (700) её обрезал

SYSTEM_PROMPT = """Ты карьерный консультант и переписываешь резюме кандидата под конкретную вакансию на русском языке.
Правила:
- бери факты только из исходного резюме: не придумывай места работы, должности, проекты, даты, цифры и ссылки;
- заголовок — должность из вакансии, если она совпадает с направлением кандидата;
- переставь и переформулируй: сначала то, что требует вакансия, её же терминами (если в резюме то же самое названо
  иначе); нерелевантное сократи или убери; объём — одна страница;
- требования вакансии, которых нет в резюме, можно добавить только в раздел навыков — без проектов и опыта под них;
- никогда не пиши «изучаю», «осваиваю», «базовые знания», «знаком», «начинающий» и подобное — навыки перечисляй
  уверенно; строку «Изучаю: …» из исходника не переноси, эти навыки — в общий список, если они нужны вакансии;
- не дописывай телефон, почту и другие контакты, которых нет в исходнике;
- инструменты в пунктах опыта не заменяй на инструменты из вакансии (например, GitHub Actions не превращай в
  GitLab CI): в опыте — только то, что было на самом деле;
- контакты, ссылки на репозитории и названия учебных заведений переноси дословно; проекты с репозиториями не теряй;
- не добавляй технологий и инструментов, которых нет ни в резюме, ни в вакансии;
- формат строго такой (это потом превращается в файл): первая строка «# Имя Фамилия», вторая — должность,
  третья — контакты; разделы — строкой «## Название»; пункты — строкой «- текст»; остальное — обычные абзацы;
  никакого другого markdown, таблиц и эмодзи;
- выдай только текст резюме, готовый к отправке работодателю: без вступления, без пояснений, без списка
  изменений и комментариев до или после."""

HEDGE_PAREN_RX = re.compile(r"\s*\((основы|базово|базовый уровень|начальный уровень|в процессе изучения|изучаю)\)", re.I)
HEDGE_LINE_RX = re.compile(r"^\s*(изучаю|осваиваю|в планах|планирую изучить)\s*:.*$", re.I | re.M)
# хвост с пояснениями, если модель всё же его дописала: «Изменения…», «Что изменено», «Примечание», «Комментарий»
TAIL_RX = re.compile(r"^[#=*\s]*(что )?(изменени[яй]|изменено|примечани[ея]|комментари[йи]|пояснени[ея])\b[^\n.]{0,60}$",
                     re.I | re.M)
# названия технологий латиницей и аббревиатуры кириллицей (ФКТИ, ЛЭТИ)
TERM_RX = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]{1,}|\b[А-ЯЁ]{2,}\b")
EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RX = re.compile(r"\+?\d[\d\s()X-]{6,}[\dX]|\bX{2,}\b")


def _terms(text: str) -> list[str]:
    text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))   # заголовки бывают капсом
    return TERM_RX.findall(EMAIL_RX.sub(" ", letter.URL_RX.sub(" ", text)))


def unknown_terms(text: str, sources: str) -> list[str]:
    """Технологии и аббревиатуры, которых нет ни в резюме, ни в вакансии, — признак выдумки."""
    known = {t.lower().strip(".-") for t in TERM_RX.findall(sources)}
    out = []
    for t in _terms(text):
        k = t.lower().strip(".-")
        if k and k not in known and not any(k in x for x in known) and t not in out:
            out.append(t)
    return out


def moved_from_vacancy(text: str, resume_text: str, vacancy: str) -> list[str]:
    """Технологии из вакансии, которые модель вписала в пункты опыта, хотя в исходном резюме их нет вообще."""
    known = {t.lower().strip(".-") for t in TERM_RX.findall(HEDGE_LINE_RX.sub("", resume_text))}  # «Изучаю: …» — не опыт
    wanted = {t.lower().strip(".-") for t in TERM_RX.findall(vacancy)}
    bullets = "\n".join(ln for ln in text.splitlines() if ln.lstrip().startswith(("-", "•")))
    out = []
    for t in _terms(bullets):
        k = t.lower().strip(".-")
        if k not in known and k in wanted and t not in out:
            out.append(t)
    return out


def lost_matches(text: str, resume_text: str, vacancy: str) -> list[str]:
    """Что есть и в резюме, и в вакансии, но пропало из переписанного резюме, — главное, что надо было оставить."""
    have = {t.lower().strip(".-") for t in TERM_RX.findall(text)}
    wanted = {t.lower().strip(".-") for t in TERM_RX.findall(vacancy)}
    out = []
    for t in TERM_RX.findall(resume_text):
        k = t.lower().strip(".-")
        if len(k) > 2 and k in wanted and k not in have and t not in out:
            out.append(t)
    return out


def restore_education(text: str, resume_text: str) -> str:
    """Раздел «Образование» модель искажает (факультет, сроки) — берём строку из исходника как есть."""
    m = re.search(r"^\s*(учёба|учеба|образование)\s*:\s*(.+)$", resume_text, re.I | re.M)
    if not m:
        return text
    if not re.search(r"(?m)^##\s*образование", text, re.I):
        return f"{text.rstrip()}\n\n## Образование\n{m.group(2).strip()}"
    return re.sub(r"(?m)(^##\s*образование[^\n]*\n)((?:(?!##)[^\n]*\n?)*)", lambda x: f"{x.group(1)}{m.group(2).strip()}\n\n",
                  text, count=1, flags=re.I).strip()


def restore_names(text: str, resume_text: str) -> str:
    """Строка с искажённой аббревиатурой (факультет, вуз) заменяется самой похожей строкой исходника."""
    known = set(TERM_RX.findall(resume_text))
    src = [re.sub(r"^[^:\n]{1,20}:\s*", "", ln).strip() for ln in resume_text.splitlines() if ln.strip()]
    out = []
    for ln in text.splitlines():
        bad = [t for t in re.findall(r"\b[А-ЯЁ]{2,}\b", ln) if t not in known]
        if bad and not ln.lstrip().startswith("#"):
            words = set(re.findall(r"\w{4,}", ln.lower()))
            best = max(src, key=lambda x: len(words & set(re.findall(r"\w{4,}", x.lower()))), default="")
            if best and len(words & set(re.findall(r"\w{4,}", best.lower()))) >= 2:
                ln = re.match(r"^\s*[-•]?\s*", ln).group(0) + best
        out.append(ln)
    return "\n".join(out)


def strip_made_up_contacts(text: str, resume_text: str) -> str:
    """Почту, телефон и ссылки модель любит дописать сама — оставляем только те, что есть в исходном резюме."""
    lines = []
    for ln in text.splitlines():
        for url in re.findall(r"https?://\S+|(?:gitlab|github)\.com/\S+", ln):
            if url.rstrip(".,)") not in resume_text and not any(url.rstrip(".,)") in u for u in re.findall(r"\S+", resume_text)):
                ln = ln.replace(url, "")
                if re.fullmatch(r"\s*[-•]?\s*[\w /]{0,30}:?\s*", ln):          # от пункта осталась только подпись
                    ln = None
                    break
        if ln is None:
            continue
        if any(x not in resume_text for x in EMAIL_RX.findall(ln) + PHONE_RX.findall(ln)):
            ln = PHONE_RX.sub("", EMAIL_RX.sub("", ln))
            ln = re.sub(r"^\s*(телефон|тел\.|e-?mail|почта|email)\s*:?\s*$", "", ln, flags=re.I)
            ln = re.sub(r"(\s*[,;·|]\s*){2,}", ", ", ln).strip(" ,;·|")
        # «Тел.:  | Email:», «E-mail: не указан» — пустые подписи контактов
        ln = re.sub(r"(тел\.?|телефон|e-?mail|почта)\s*:?\s*(не указан\w*|нет|—|-)?\s*(?=[|,;·]|$)", "", ln, flags=re.I)
        if lines and re.fullmatch(r"[\s|,;·]*", ln):
            continue
        lines.append(ln)
    out = "\n".join(lines)
    out = re.sub(r"(?m)^\s*контакты\s*:?\s*$\n?(?=\s*(\n|##|$))", "", out, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


CONTACT_RX = re.compile(r"(?<![\w.])@\w{3,}|github\.com/[\w-]++(?!/)|[\w.+-]+@[\w-]+\.[\w.]+|\+7[\d\s()-]{9,}\d")


def restore_contacts(text: str, resume_text: str) -> str:
    """Контакты из исходника, которые модель потеряла, — дописываем в строку контактов под должностью."""
    have = set(CONTACT_RX.findall(text))
    lost = [c for c in dict.fromkeys(CONTACT_RX.findall(resume_text)) if c not in have]
    if not lost:
        return text
    lines = text.splitlines()
    at = next((i for i, ln in enumerate(lines[:6]) if CONTACT_RX.search(ln)), None)
    if at is None:
        at = next((i for i, ln in enumerate(lines) if ln.startswith("## ")), len(lines))
        lines.insert(min(at, 2), ", ".join(lost))
    else:
        lines[at] = f"{lines[at].rstrip(' ,')}, {', '.join(lost)}"
    return "\n".join(lines)


def strip_made_up_numbers(text: str, sources: str) -> str:
    """Скобки с цифрами, которых нет в исходнике («май 2023 – октябрь 2025»), убираются целиком."""
    known = set(re.findall(r"\d+", letter.URL_RX.sub(" ", sources)))

    def fix(m):
        return "" if any(n not in known for n in re.findall(r"\d+", m.group(0))) else m.group(0)
    return re.sub(r"\s*\([^()\n]*\d[^()\n]*\)", fix, text)


def build_prompt(resume_text: str, vacancy: str) -> str:
    return f"ИСХОДНОЕ РЕЗЮМЕ:\n{resume_text.strip()}\n\nВАКАНСИЯ:\n{vacancy.strip()[:8000]}"


def clean(out: str) -> str:
    """Ответ модели → только резюме: без вступления до «# Имя», пояснений после и разделителей."""
    out = re.sub(r"^```\w*\s*|```\s*$", "", out.strip()).replace("**", "")
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", lambda m: m.group(2), out)      # [ссылка](ссылка) → ссылка
    if (start := out.find("# ")) > 0 and not out.startswith("#"):
        out = out[start:]
    body = TAIL_RX.split(out, maxsplit=1)[0]
    body = HEDGE_PAREN_RX.sub("", HEDGE_LINE_RX.sub("", body))
    body = "\n".join(ln.rstrip() for ln in body.splitlines() if not re.fullmatch(r"\s*[-—_*=]{3,}\s*", ln))
    return re.sub(r"\n{3,}", "\n\n", body).strip()


class Claude:
    """Клиент Claude API. SDK импортируется только при первом вызове — бот работает в 256 МБ, а функция редкая."""

    def __init__(self, api_key: str, model: str = CLAUDE_MODEL):
        self.api_key, self.model, self._client = api_key, model or CLAUDE_MODEL, None

    async def chat(self, system: str, user: str, max_tokens: int = 16000) -> str:
        import anthropic
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key, timeout=300.0, max_retries=2)
        resp = await self._client.beta.messages.create(
            model=self.model, max_tokens=max(max_tokens, 16000), system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("Claude отказался отвечать")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise RuntimeError(f"Claude: пустой ответ ({resp.stop_reason})")
        return text


async def rewrite(llm, resume_text: str, vacancy: str) -> tuple[str, str]:
    """Переписать резюме → (текст, предупреждение владельцу — в файл не идёт).

    Если модель приписала цифры, технологии, оговорки «изучаю» или потеряла ссылки — одна попытка исправить."""
    prompt, sources = build_prompt(resume_text, vacancy), f"{resume_text}\n{vacancy}"

    def issues(body: str) -> list[str]:
        out = letter.problems(body, sources) + [f"«{t}» нет ни в резюме, ни в вакансии" for t in unknown_terms(body, sources)]
        out += [f"в опыт вписан «{t}», которого в исходном резюме нет (его можно только в навыки)"
                for t in moved_from_vacancy(body, resume_text, vacancy)]
        out += ["выдуманные контакты" for _ in [1] if strip_made_up_contacts(body, resume_text) != body]
        if lost := lost_matches(body, resume_text, vacancy):
            out.append(f"пропало то, что есть и в резюме, и в вакансии: {', '.join(lost)}")
        out += [f"потеряна ссылка {u}" for u in dict.fromkeys(re.findall(r"https?://\S+", resume_text)) if u.rstrip(".,)") not in body]
        if re.search(r"образование|учёба|учеба|университет", resume_text, re.I) and not re.search(r"образован", body, re.I):
            out.append("пропал раздел «Образование»")
        return out

    body = clean(await llm.chat(SYSTEM_PROMPT, prompt, max_tokens=MAX_TOKENS))
    if found := issues(body):
        retry = f"{prompt}\n\nПРЕДЫДУЩИЙ ВАРИАНТ НАРУШИЛ ПРАВИЛА: {'; '.join(found)}. Перепиши без этого."
        body = clean(await llm.chat(SYSTEM_PROMPT, retry, max_tokens=MAX_TOKENS))
    # последняя страховка: строки с оговорками удаляются, выдуманные контакты вычищаются, потерянные ссылки возвращаются
    body = "\n".join(ln for ln in body.splitlines() if not letter.HEDGE_RX.search(ln)).strip()
    body = strip_made_up_numbers(body, sources)
    body = restore_education(strip_made_up_contacts(body, resume_text), resume_text)
    body = restore_contacts(restore_names(body, resume_text), resume_text)
    lost = [u.rstrip(".,)") for u in dict.fromkeys(re.findall(r"https?://\S+", resume_text)) if u.rstrip(".,)") not in body]
    if lost:
        body += "\n\n## Репозитории\n" + "\n".join(f"- {u}" for u in lost)
    warn = [x for x in letter.problems(body, sources) if x.startswith("число")]
    if extra := unknown_terms(body, sources):
        warn.append(f"нет в исходном резюме: {', '.join(extra)}")
    if moved := moved_from_vacancy(body, resume_text, vacancy):
        warn.append(f"в опыте упомянуто то, чего в твоём опыте не было: {', '.join(moved)}")
    return body, ("⚠️ Проверь перед отправкой — " + "; ".join(warn)) if warn else ""


# ───────────── .docx без сторонних библиотек ─────────────

_CT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
       '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
       '<Default Extension="xml" ContentType="application/xml"/>'
       '<Override PartName="/word/document.xml" '
       'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
         'Target="word/document.xml"/></Relationships>')


def _para(text: str, *, size: int = 21, bold: bool = False, before: int = 0, after: int = 60,
          indent: int = 0, color: str = "") -> str:
    ppr = f'<w:pPr><w:spacing w:before="{before}" w:after="{after}"/>'
    if indent:
        ppr += f'<w:ind w:left="{indent}" w:hanging="220"/>'
    ppr += "</w:pPr>"
    color_xml = f'<w:color w:val="{color}"/>' if color else ""
    rpr = (f'<w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>{"<w:b/>" if bold else ""}'
           f'{color_xml}<w:sz w:val="{size}"/></w:rPr>')
    return f'<w:p>{ppr}<w:r>{rpr}<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def to_docx(text: str) -> bytes:
    """Размеченный текст (# имя, ## раздел, - пункт) → документ Word."""
    paras = []
    for i, raw in enumerate(ln.rstrip() for ln in text.splitlines()):
        ln = raw.strip()
        if not ln:
            continue
        if ln.startswith("# "):
            paras.append(_para(ln[2:], size=34, bold=True, after=40))
        elif ln.startswith("### "):
            paras.append(_para(ln[4:], bold=True, before=80, after=40))
        elif ln.startswith("## "):
            paras.append(_para(ln[3:].upper(), size=23, bold=True, before=200, after=80, color="1F4E79"))
        elif re.match(r"^[-•*]\s+", ln):
            paras.append(_para("• " + re.sub(r"^[-•*]\s+", "", ln), indent=360, after=40))
        elif i <= 2 and not paras[1:]:
            paras.append(_para(ln, size=24, bold=True, after=40))       # должность сразу под именем
        else:
            paras.append(_para(ln))
    body = "".join(paras)
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           f'{body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
           '<w:pgMar w:top="850" w:right="850" w:bottom="850" w:left="1134" w:header="0" w:footer="0" w:gutter="0"/>'
           '</w:sectPr></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def filename(vacancy: str) -> str:
    title = letter.title_guess(vacancy) or "вакансия"
    title = re.sub(r"[^\w\s.-]", "", title)[:40].strip() or "вакансия"
    return f"Резюме — {title}.docx"
