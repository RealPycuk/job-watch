"""Карьерные сайты IT-компаний. Только то, что сайты сами отдают браузеру без авторизации и что не закрыто robots.txt.

Каждой компании — parse_*() для разбора ответа (тестируется на tests/fixtures) и fetch_*() для запросов.
Сайты компаний меняются редко, поэтому опрашиваются реже агрегаторов (sources.*.every_minutes в config.toml).
"""
import json
import re
from datetime import datetime
import logging
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup

from .models import Vacancy
from .net import _clean, _get, ru_ssl

MAX_PAGES = 10
log = logging.getLogger("job-watch")


def _iso(s: str | None) -> float:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _min_years(text: str) -> int | None:
    """«От 1 года до 3 лет», «1-3 года», «Нет опыта» → минимальный опыт в годах."""
    t = (text or "").lower()
    if not t:
        return None
    if "нет опыта" in t or "без опыта" in t:
        return 0
    m = re.search(r"(\d+)", t)
    return int(m.group(1)) if m else None


# ───────────── Яндекс: yandex.ru/jobs/api/publications (поиск по тексту) ─────────────

def parse_yandex(data: dict) -> list[Vacancy]:
    out = []
    for p in data.get("results") or []:
        vac = p.get("vacancy") or {}
        modes = [m.get("slug") for m in vac.get("work_modes") or []]
        mode_names = ", ".join(m.get("name", "") for m in vac.get("work_modes") or [])
        service = (p.get("public_service") or {}).get("name") or ""
        out.append(Vacancy(
            source="yandex", ext_id=str(p.get("id")), title=p.get("title") or "",
            url=f"https://yandex.ru/jobs/vacancies/{p.get('publication_slug_url')}",
            text=f"{p.get('short_summary') or ''}\nФормат: {mode_names}",
            company="Яндекс" + (f" · {service}" if service and service != "Яндекс" else ""),
            city=", ".join(c.get("name", "") for c in vac.get("cities") or []),
            remote=True if "remote" in modes else None, published_at=_iso(p.get("modified")),
            tags=[s.get("name", "") for s in vac.get("skills") or [] if isinstance(s, dict)]))
    return out


async def fetch_yandex(session, src: dict) -> list[Vacancy]:
    found = {}
    for q in src.get("queries", ["devops"]):
        data = json.loads(await _get(session, f"https://yandex.ru/jobs/api/publications?text={quote(q)}&page_size=50"))
        for v in parse_yandex(data):
            found.setdefault(v.ext_id, v)
    return list(found.values())


# ───────────── VK: team.vk.company/career/api/v2/vacancies (поиска нет — забираем все, ≤50 за запрос) ─────────────

def parse_vk(data: dict) -> list[Vacancy]:
    out = []
    for p in data.get("results") or []:
        parts = [(p.get("prof_area") or {}).get("name"), (p.get("specialty") or {}).get("name"), p.get("work_format")]
        group = (p.get("group") or {}).get("name")
        out.append(Vacancy(
            source="vk", ext_id=str(p.get("id")), title=p.get("title") or "",
            url=f"https://team.vk.company/vacancy/{p.get('id')}/",
            text="\n".join(x for x in parts if x), company="VK" + (f" · {group}" if group else ""),
            city=(p.get("town") or {}).get("name") or "", remote=True if p.get("remote") else None,
            tags=[t.get("name", "") for t in p.get("tags") or []]))
    return out


async def fetch_vk(session, src: dict) -> list[Vacancy]:
    out = []
    for page in range(MAX_PAGES):
        data = json.loads(await _get(session, f"https://team.vk.company/career/api/v2/vacancies/?limit=50&offset={page * 50}"))
        out += parse_vk(data)
        if not data.get("next"):
            break
    return out


# ───────────── Сбер: IT-вакансии developers.sber.ru (данные в __NEXT_DATA__ страницы) ─────────────

GRAPHIC = {"hybrid": "гибрид", "office": "офис", "remote": "удалённо"}


def parse_sber(page: str) -> list[Vacancy]:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        return []
    blocks = json.loads(m.group(1))["props"]["pageProps"]["page"]["MainContent"]
    out = []
    for b in blocks:
        for p in b.get("vacancies") or []:
            graphic = str(p.get("Graphic") or "").lower()
            exp = "; ".join(e.get("Title", "") for e in p.get("vacancy_experiences") or [])
            salary = f"от {p['Salary']} ₽" if p.get("ShowSalary") and p.get("Salary") else ""
            out.append(Vacancy(
                source="sber", ext_id=p.get("slug") or "", title=p.get("Title") or "",
                url=f"https://developers.sber.ru/kak-v-sbere/vacancies/{p.get('slug')}",
                text=f"Формат: {GRAPHIC.get(graphic, graphic)}\nОпыт: {exp}", company="Сбер",
                city=p.get("City") or ", ".join(c.get("CityTitle", "") for c in p.get("vacancies_cities") or []),
                salary=salary, remote=True if graphic == "remote" else None,
                experience_years=_min_years(exp)))
    return [v for v in out if v.ext_id]


async def fetch_sber(session, src: dict) -> list[Vacancy]:
    return parse_sber(await _get(session, "https://developers.sber.ru/kak-v-sbere/vacancies"))


# ───────────── МТС: job.mts.ru/api/v2/vacancies (Strapi, поиск filters[title][$containsi]) ─────────────

def parse_mts(data: dict) -> list[Vacancy]:
    out = []
    for p in data.get("data") or []:
        formats = ", ".join(f.get("title", "") for f in p.get("workFormats") or [])
        lo, hi = p.get("salaryFrom"), p.get("salaryTo")
        salary = (f"{lo:,}–{hi:,} ₽" if lo and hi else f"от {lo:,} ₽" if lo else f"до {hi:,} ₽" if hi else "").replace(",", " ")
        text = "\n".join(_clean(str(x)) for x in (p.get("tasks"), p.get("requirements"), p.get("description")) if x)
        exp = (p.get("experience") or {}).get("title") or ""
        out.append(Vacancy(
            source="mts", ext_id=str(p.get("slug") or p.get("id")), title=p.get("title") or "",
            url=f"https://job.mts.ru/vacancies/{p.get('slug')}", text=f"{text}\nФормат: {formats}\nОпыт: {exp}".strip(),
            company="МТС", city=(p.get("region") or {}).get("title") or "", salary=salary,
            remote=True if re.search(r"удал", formats, re.I) else None, experience_years=_min_years(exp),
            published_at=_iso(p.get("publishedAt"))))
    return out


async def fetch_mts(session, src: dict) -> list[Vacancy]:
    found = {}
    for q in src.get("queries", ["devops"]):
        url = (f"https://job.mts.ru/api/v2/vacancies?filters%5Btitle%5D%5B%24containsi%5D={quote(q)}"
               f"&pagination%5BpageSize%5D=50")
        for v in parse_mts(json.loads(await _get(session, url))):
            found.setdefault(v.ext_id, v)
    return list(found.values())


# ───────────── HTML-списки: Лаборатория Касперского, Авито ─────────────

def _links(page: str, pattern: str, base: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(page, "html.parser")
    seen, out = set(), []
    for a in soup.select("a[href]"):
        href, title = a["href"], a.get_text(" ", strip=True)
        if re.fullmatch(pattern, href) and title and href not in seen:
            seen.add(href)
            out.append((base + href, title))
    return out


def parse_kaspersky(page: str) -> list[Vacancy]:
    return [Vacancy(source="kaspersky", ext_id=url.rsplit("/", 1)[-1], title=title, url=url, company="Лаборатория Касперского")
            for url, title in _links(page, r"/vacancy/\d+", "https://careers.kaspersky.ru")]


async def fetch_kaspersky(session, src: dict) -> list[Vacancy]:
    out = []
    for page in range(1, int(src.get("pages", 3)) + 1):         # новые вакансии — на первых страницах
        found = parse_kaspersky(await _get(session, f"https://careers.kaspersky.ru/vacancy?page={page}"))
        if not found:
            break
        out += found
    return out


def parse_avito(page: str) -> list[Vacancy]:
    return [Vacancy(source="avito", ext_id=url.rstrip("/").rsplit("/", 1)[-1], title=title, url=url, company="Авито")
            for url, title in _links(page, r"/vacancies/[\w-]+/\d+/", "https://career.avito.com")]


async def fetch_avito(session, src: dict) -> list[Vacancy]:
    out = []
    for cat in src.get("categories", ["razrabotka"]):
        out += parse_avito(await _get(session, f"https://career.avito.com/vacancies/{cat}/"))
    return out


def parse_selectel(page: str) -> list[Vacancy]:
    """Карточка: направление | название | город | «Подробнее»."""
    soup = BeautifulSoup(page, "html.parser")
    out, seen = [], set()
    for card in soup.select("div.card"):
        a = next((x for x in card.select("a[href]") if re.search(r"/careers/all/vacancy/\d+/", x["href"])), None)
        if not a:
            continue
        vid = re.search(r"/vacancy/(\d+)/", a["href"]).group(1)
        parts = [t for t in card.get_text("|", strip=True).split("|") if t and t != "Подробнее"]
        if vid in seen or len(parts) < 2:
            continue
        seen.add(vid)
        out.append(Vacancy(source="selectel", ext_id=vid, title=parts[-2], url=f"https://selectel.ru/careers/all/vacancy/{vid}/",
                           text=" · ".join(parts), company="Selectel", city=parts[-1]))
    return out


async def fetch_selectel(session, src: dict) -> list[Vacancy]:
    return parse_selectel(await _get(session, "https://selectel.ru/careers/all/"))


def parse_x5(page: str) -> list[Vacancy]:
    """Карточка: название | формат (Офис / Гибрид / Удалённо…) | преимущества; города в списке нет — компания в Москве."""
    soup = BeautifulSoup(page, "html.parser")
    out, seen = [], set()
    for a in soup.select("a[href]"):
        m = re.fullmatch(r"/vacancy/([\w-]+)", a["href"])
        if not m or m.group(1) in seen or not a.get_text(strip=True):
            continue
        seen.add(m.group(1))
        card = a.find_parent("div") or a
        out.append(Vacancy(source="x5", ext_id=m.group(1), title=a.get_text(" ", strip=True), url=f"https://x5.tech{a['href']}",
                           text=card.get_text(" · ", strip=True), company="X5 Tech"))
    return out


async def fetch_x5(session, src: dict) -> list[Vacancy]:
    return parse_x5(await _get(session, "https://x5.tech/vacancy"))


# ───────────── универсальный источник: список вакансий на сайте компании, описанный в config.toml ─────────────

CARD_EXP_RX = re.compile(r"(?:более|от)\s+(\d+)\s*(?:до\s+\d+\s*)?(?:лет|года?)", re.I)


def parse_site(page: str, *, company: str, base_url: str, link: str) -> list[Vacancy]:
    """Ссылки, подходящие под регулярку link; название — первая строка текста ссылки, остальное — в описание.
    Если на одну вакансию ведёт несколько ссылок (картинка, «Подробнее»), берётся ссылка с самым длинным текстом."""
    soup = BeautifulSoup(page, "html.parser")
    rx = re.compile(link)
    best: dict[str, list[str]] = {}
    for a in soup.select("a[href]"):
        url = urljoin(base_url, a["href"]).split("#")[0].split("?")[0]
        strings = [s for s in a.stripped_strings if s not in ("Подробнее", "Откликнуться")]
        if rx.search(url) and strings and len(" ".join(strings)) > len(" ".join(best.get(url, []))):
            best[url] = strings
    slug = re.sub(r"\W+", "-", urlparse(base_url).netloc.removeprefix("www.")).strip("-")
    out = []
    for url, strings in best.items():
        card = " · ".join(strings)
        m = CARD_EXP_RX.search(card)
        out.append(Vacancy(source=f"site:{slug}", ext_id=url.rstrip("/").rsplit("/", 1)[-1] or url, title=strings[0][:150],
                           url=url, text=card, company=company, experience_years=int(m.group(1)) if m else None))
    return out


async def fetch_sites(session, src: dict) -> list[Vacancy]:
    out = []
    for site in src.get("list", []):
        try:
            page = await _get(session, site["url"], ru_ssl() if site.get("russian_ca") else None)
            out += parse_site(page, company=site["company"], base_url=site["url"], link=site["link"])
        except Exception as e:                    # один упавший сайт не должен ронять остальные
            log.warning("site %s failed: %s", site.get("company"), e)
    return out


SOURCES = {"yandex": fetch_yandex, "vk": fetch_vk, "sber": fetch_sber, "mts": fetch_mts,
           "kaspersky": fetch_kaspersky, "avito": fetch_avito, "selectel": fetch_selectel, "x5": fetch_x5,
           "sites": fetch_sites}
TITLES = {"yandex": "Яндекс", "vk": "VK", "sber": "Сбер", "mts": "МТС", "kaspersky": "Лаборатория Касперского",
          "avito": "Авито", "selectel": "Selectel", "x5": "X5 Tech", "sites": "Сайт компании", "site": "Сайт компании"}
