"""Источники вакансий. Только открытые форматы: RSS, открытое API, публичные веб-версии Telegram-каналов.

Каждый источник — асинхронная функция fetch(session, cfg) -> list[Vacancy]; разбор вынесен в parse_*()
и тестируется на сохранённых ответах (tests/fixtures).
"""
import asyncio
import json
import re
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from bs4 import BeautifulSoup

from .models import Vacancy
from .net import TIMEOUT, UA, _clean, _get  # noqa: F401  (UA и TIMEOUT — часть интерфейса модуля)

SPB_REGION = "7800000000000"


def _rss_items(xml: str) -> list[dict]:
    """Терпимый к ошибкам разбор RSS: ленты бывают невалидным XML (например, неэкранированный & в Geekjob)."""
    items = []
    for raw in re.findall(r"<item>(.*?)</item>", xml, re.S):
        item = {}
        for tag in ("title", "link", "description", "author", "pubDate", "guid"):
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", raw, re.S)
            item[tag] = _clean(m.group(1)) if m else ""
        items.append(item)
    return items


def _ts(rfc822: str) -> float:
    try:
        return parsedate_to_datetime(rfc822).timestamp()
    except (TypeError, ValueError):
        return 0.0


# ───────────── Хабр Карьера ─────────────

HABR_TITLE_RX = re.compile(r"«(.+)»\s*(?:\((.*)\))?\s*$")


def parse_habr(xml: str) -> list[Vacancy]:
    out = []
    for it in _rss_items(xml):
        title, extra = it["title"], ""
        m = HABR_TITLE_RX.search(it["title"])
        if m:
            title, extra = m.group(1), m.group(2) or ""
        parts = [p.strip() for p in extra.split(",")] if extra else []
        city = parts[0] if parts and not re.search(r"\d", parts[0]) else ""
        sal = ", ".join(p for p in parts if re.search(r"\d", p))
        desc = it["description"]
        tags = re.findall(r"#([\w+.#-]+)", desc)
        out.append(Vacancy(source="habr", ext_id=it["guid"] or it["link"], title=title, url=it["link"],
                           text=desc, company=it["author"], salary=sal, city=city,
                           remote=True if re.search(r"можно удал", desc, re.I) else None,
                           published_at=_ts(it["pubDate"]), tags=tags))
    return out


async def fetch_habr(session, src: dict) -> list[Vacancy]:
    found = {}
    urls = [f"https://career.habr.com/vacancies/rss?q={quote(q)}&type=all" for q in src.get("queries", ["devops"])]
    # все вакансии отдельных компаний на Хабре — для тех, чьи собственные сайты закрыты для серверов
    urls += [f"https://career.habr.com/vacancies/rss?company_ids%5B%5D={cid}&type=all" for cid in src.get("company_ids", [])]
    for url in urls:
        for v in parse_habr(await _get(session, url)):
            found.setdefault(v.ext_id, v)
    return list(found.values())


# ───────────── Geekjob ─────────────

def parse_geekjob(xml: str) -> list[Vacancy]:
    out = []
    for it in _rss_items(xml):
        desc = it["description"]
        m = re.search(r"на вакансию «(.+?)»,?\s*(.*?)(?:\.|$)", desc)
        sal = m.group(2).strip() if m and re.search(r"\d", m.group(2) or "") else ""
        url = it["link"].split("?")[0]
        out.append(Vacancy(source="geekjob", ext_id=it["guid"] or url, title=it["title"].strip(), url=url,
                           text=desc, company=it["author"], salary=sal,
                           remote=True if re.search(r"удал[её]нн", desc, re.I) else None,
                           published_at=_ts(it["pubDate"])))
    return out


async def fetch_geekjob(session, src: dict) -> list[Vacancy]:
    return parse_geekjob(await _get(session, "https://geekjob.ru/vacancies/rss"))


# ───────────── Работа России (trudvsem.ru, открытые данные) ─────────────

def parse_trudvsem(data: dict) -> list[Vacancy]:
    out = []
    for wrap in (data.get("results") or {}).get("vacancies") or []:
        v = wrap.get("vacancy") or {}
        lo, hi = v.get("salary_min") or 0, v.get("salary_max") or 0
        sal = (f"{lo:,}–{hi:,} ₽" if lo and hi and hi != lo else f"от {lo:,} ₽" if lo else f"до {hi:,} ₽" if hi else "")
        req = v.get("requirement") or {}
        fields = (v.get("duty"), v.get("requirements"), v.get("skills"), req.get("qualification"),
                  v.get("schedule"), v.get("employment"))
        text = "\n".join(_clean(str(x)) for x in fields if x)
        employment = str(v.get("employment") or "")
        created = v.get("creation-date") or ""
        try:
            ts = datetime.fromisoformat(created).replace(tzinfo=timezone(timedelta(hours=3))).timestamp()
        except ValueError:
            ts = 0.0
        emails = [c.get("contact_value", "") for c in v.get("contact_list") or []
                  if "почт" in str(c.get("contact_type", "")).lower() and "@" in str(c.get("contact_value", ""))]
        if not emails and "@" in str((v.get("company") or {}).get("email") or ""):
            emails = [v["company"]["email"]]
        person = str(v.get("contact_person") or "").strip()
        contacts = [f"{e} ({person.title()})" if person and i == 0 else e for i, e in enumerate(emails[:2])]
        out.append(Vacancy(source="trudvsem", ext_id=str(v.get("id") or v.get("vac_url")), title=v.get("job-name") or "",
                           url=v.get("vac_url") or "", text=text, company=(v.get("company") or {}).get("name") or "",
                           salary=sal.replace(",", " "), city=(v.get("region") or {}).get("name") or "",
                           remote=True if "дистанц" in employment.lower() else None,
                           experience_years=req.get("experience") if isinstance(req.get("experience"), int) else None,
                           published_at=ts, contacts=contacts))
    return out


async def fetch_trudvsem(session, src: dict) -> list[Vacancy]:
    found = {}
    since = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    urls = [f"https://opendata.trudvsem.ru/api/v1/vacancies?text={quote(q)}&limit=100&modifiedFrom={since}"
            for q in src.get("queries", ["devops"])]
    # все вакансии конкретных компаний по ОГРН — IT среди них отбирает фильтр
    urls += [f"https://opendata.trudvsem.ru/api/v1/vacancies/company/{ogrn}?limit=100" for ogrn in src.get("companies", [])]
    for url in urls:
        for v in parse_trudvsem(json.loads(await _get(session, url))):
            found.setdefault(v.ext_id, v)
    return list(found.values())


# ───────────── Telegram (веб-версия публичного канала t.me/s/<channel>) ─────────────

def parse_telegram(page: str, channel: str) -> list[Vacancy]:
    soup = BeautifulSoup(page, "html.parser")
    out = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        body = msg.select_one("div.tgme_widget_message_text")
        if not body:
            continue
        for br in body.find_all("br"):
            br.replace_with("\n")
        text = body.get_text().strip()
        if len(text) < 40:
            continue
        post = msg["data-post"]                          # channel/123
        when = msg.select_one("time[datetime]")
        try:
            ts = datetime.fromisoformat(when["datetime"]).timestamp() if when else 0.0
        except ValueError:
            ts = 0.0
        title = telegram_title(text)
        out.append(Vacancy(source=f"telegram:{channel}", ext_id=post, title=title, url=f"https://t.me/{post}",
                           text=text, published_at=ts))
    return out


TG_META_RX = re.compile(r"^(публикатор|обсуждение|автор|источник)\s*:", re.I)
TG_TITLE_RX = re.compile(r"^(?:вакансия|позиция|должность|position|role|vacancy)\s*[:—–-]\s*(.+)", re.I)
TG_ROLE_RX = re.compile(r"dev ?ops|\bsre\b|engineer|инженер|администратор|admin|developer|разработчик|специалист|стаж[её]р|intern", re.I)
TG_FIELD_RX = re.compile(r"^[^:]{2,25}:\s", re.I)          # «Формат работы: …», «Опыт: …»
TG_STRIP = " *_#📌🔥💼🚀⚡️✅👉🔹🔸▪️•—-:;"


def telegram_title(text: str) -> str:
    """Заголовок поста: строка «Вакансия: …», иначе первая содержательная строка (без служебных и хэштегов)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines:
        m = TG_TITLE_RX.match(ln.strip(TG_STRIP))
        if m:
            return m.group(1).strip(TG_STRIP)[:120]
    content = [ln for ln in lines if not TG_META_RX.match(ln)
               and sum(w.startswith("#") for w in ln.split()) < 0.6 * len(ln.split())]
    for ln in content:
        if TG_ROLE_RX.search(ln) and not TG_FIELD_RX.match(ln):
            return ln.strip(TG_STRIP)[:120]
    return content[0].strip(TG_STRIP)[:120] if content else ""


async def fetch_telegram(session, src: dict) -> list[Vacancy]:
    out = []
    for ch in src.get("channels", []):
        out += parse_telegram(await _get(session, f"https://t.me/s/{ch}"), ch)
    return out


# ───────────── Young&Junior: вакансии и стажировки для начинающих (youngjunior.ru) ─────────────
# Список — разметка schema.org ItemList на страницах категорий (новые сверху), подробности — JobPosting на странице
# вакансии; их бот дочитывает только для вакансий, прошедших фильтр по названию (ENRICH). robots.txt закрывает
# только /api/, /admin/ и /profile.

YJ_BASE = "https://youngjunior.ru"


def _ld_json(page: str) -> list[dict]:
    out = []
    for tag in BeautifulSoup(page, "html.parser").select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        out += [d for d in (data if isinstance(data, list) else [data]) if isinstance(d, dict)]
    return out


def parse_youngjunior(page: str) -> list[Vacancy]:
    out = []
    for d in _ld_json(page):
        if d.get("@type") != "ItemList":
            continue
        for item in d.get("itemListElement") or []:
            url, title = item.get("url") or "", (item.get("name") or "").strip()
            m = re.fullmatch(r"https://youngjunior\.ru/[\w-]+/(?:jobs|internships)/(\d+)", url)
            if m and title:
                out.append(Vacancy(source="youngjunior", ext_id=m.group(1), title=title, url=url))
    return out


async def fetch_youngjunior(session, src: dict) -> list[Vacancy]:
    found: dict[str, Vacancy] = {}
    for cat in src.get("categories", ["devops"]):
        for kind in src.get("kinds", ["jobs", "internships"]):
            try:
                vs = parse_youngjunior(await _get(session, f"{YJ_BASE}/{cat}/{kind}"))
            except Exception:
                continue                                   # одна категория не должна ронять весь источник
            for v in vs:                                    # одна вакансия бывает в нескольких категориях
                found.setdefault(v.ext_id, v)
            await asyncio.sleep(1)                          # вежливая пауза между страницами
    return list(found.values())


SOURCES = {"habr": fetch_habr, "geekjob": fetch_geekjob, "trudvsem": fetch_trudvsem, "telegram": fetch_telegram,
           "youngjunior": fetch_youngjunior}
TITLES = {"habr": "Хабр Карьера", "geekjob": "Geekjob", "trudvsem": "Работа России", "telegram": "Telegram",
          "youngjunior": "Young&Junior"}


# ───────────── полный текст вакансии для письма ─────────────

def parse_jobposting(page: str) -> str:
    """Описание из разметки schema.org JobPosting, иначе — из og:description (полный текст для письма)."""
    soup = BeautifulSoup(page, "html.parser")
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if isinstance(d, dict) and d.get("@type") == "JobPosting" and d.get("description"):
                return _clean(d["description"])
    og = soup.select_one('meta[property="og:description"]')
    return _clean(og.get("content", "")) if og else ""


def jobposting_location(page: str) -> tuple[str, bool | None]:
    """Город и признак удалёнки из JobPosting: addressLocality («Москва, удалённая работа») и jobLocationType."""
    soup = BeautifulSoup(page, "html.parser")
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if not (isinstance(d, dict) and d.get("@type") == "JobPosting"):
                continue
            locs = d.get("jobLocation") or []
            loc = " ".join(str(((x or {}).get("address") or {}).get("addressLocality") or "")
                           for x in (locs if isinstance(locs, list) else [locs]))
            remote = True if d.get("jobLocationType") == "TELECOMMUTE" or re.search(r"удал", loc, re.I) else None
            return loc.split(",")[0].strip(), remote
    return "", None


# источники, где город и формат есть только на странице вакансии — дополняются для вакансий, прошедших фильтр
ENRICH = {"avito", "youngjunior"}


def needs_enrich(v: Vacancy) -> bool:
    return v.source in ENRICH or v.source.startswith("site:")


def jobposting_meta(page: str) -> tuple[str, float]:
    """Компания (hiringOrganization) и дата публикации (datePosted) из JobPosting."""
    for d in _ld_json(page):
        if d.get("@type") == "JobPosting":
            org = d.get("hiringOrganization") or {}
            try:
                ts = datetime.fromisoformat(str(d.get("datePosted") or "").replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = 0.0
            return str(org.get("name") or "").strip() if isinstance(org, dict) else "", ts
    return "", 0.0


async def enrich(session, v: Vacancy) -> Vacancy:
    page = await _get(session, v.url)
    city, remote = jobposting_location(page)
    company, posted = jobposting_meta(page)
    v.city = v.city or city
    v.remote = v.remote if v.remote is not None else remote
    v.company = v.company or company
    v.published_at = v.published_at or posted
    v.text = (f"{v.text}\n{parse_jobposting(page)}").strip()
    return v


async def full_text(session, v: Vacancy) -> str:
    if v.source not in ("trudvsem",) and not v.source.startswith("telegram:"):
        try:
            return parse_jobposting(await _get(session, v.url)) or v.text
        except Exception:
            return v.text
    return v.text


# карьерные сайты компаний — в отдельном модуле, регистрируются здесь же
from . import companies  # noqa: E402

SOURCES.update(companies.SOURCES)
TITLES.update(companies.TITLES)
