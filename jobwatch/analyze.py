"""Разбор вакансии: роль, уровень, опыт, город и формат работы, зарплата, совпадение навыков."""
import re

from .config import Config
from .models import Vacancy, Verdict
from .profile import Matcher

REMOTE_RX = re.compile(r"удал[её]нн\w*|удал[её]нк\w*|дистанционн\w*|\bremote\b|из любой точки|work from home|"
                       r"можно удал|full[- ]?remote|\bwfh\b", re.I)
NO_REMOTE_RX = re.compile(r"(без|не|нет)\s+(возможност\w+\s+)?(удал|дистанц|remote)|"
                          r"удал[её]нк\w*\s+не\s+(рассматр|предусм|возможн)|только\s+(в\s+)?офис|"
                          r"only\s+(in\s+)?office|no\s+remote|not\s+remote", re.I)
HYBRID_RX = re.compile(r"гибрид\w*|hybrid", re.I)
OFFICE_RX = re.compile(r"\bофис\w*|on-?site|in office", re.I)
# города, по которым понятно, что вакансия не в домашнем городе
OTHER_CITY_RX = re.compile(r"москв\w*|\bмск\b|ташкент|минск|алматы|астан\w*|бишкек|баку|ереван|тбилиси|новосибирск\w*|"
                           r"екатеринбург\w*|казан[ьи]|нижн\w+ новгород|самар\w*|краснодар\w*|ростов\w*|перм[ьи]|воронеж\w*|"
                           r"\bуф[аеы]\b|челябинск\w*|тюмен\w*|томск\w*|иннополис|белград\w*|кипр|лимассол\w*|дуба[йи]|"
                           r"варшав\w*|берлин\w*|\bmoscow\b|tashkent|minsk|almaty|belgrade|cyprus|limassol|dubai", re.I)
JUNIOR_RX = re.compile(r"junior|джун\w*|младш\w*|стаж[её]р\w*|стажировк\w*|\bintern\w*|начинающ\w*|без опыта|\bentry\b|trainee", re.I)
MIDDLE_RX = re.compile(r"middle|мидл\w*", re.I)
LINUX_RX = re.compile(r"\blinux\b|ubuntu|debian|centos|rhel|astra", re.I)
EXP_RX = re.compile(
    r"опыт\w*[^.\n;]{0,50}?(?:от|не менее|более|минимум|min\.?)\s*(\d{1,2})(?:-?х|-?ти)?\s*(?:\+\s*)?(?:год|лет)"
    r"|(\d{1,2})\s*\+?\s*(?:год\w*|лет)\s+(?:опыта|коммерческого опыта)"
    r"|(\d{1,2})\+?\s*years?\s+(?:of\s+)?(?:commercial\s+|hands-on\s+)?experience"
    r"|experience[^.\n]{0,20}?(\d{1,2})\+?\s*years?", re.I)
SALARY_RX = re.compile(
    r"(?:(?:от|до|from|up to)\s*)?\d{2,3}(?:[\s .,]?\d{3})*(?:\s*[-–—]\s*\d{2,3}(?:[\s .,]?\d{3})*)?"
    r"\s*(?:₽|руб\w*|rub\b|usd\b|\$|€|eur\b|k\b|к\b|тыс\.?)", re.I)


def experience_years(v: Vacancy) -> int | None:
    if v.experience_years is not None:
        return v.experience_years
    m = EXP_RX.search(f"{v.title}\n{v.text}")
    if not m:
        return None
    return int(next(g for g in m.groups() if g))


def salary(v: Vacancy) -> str:
    if v.salary:
        return v.salary
    m = SALARY_RX.search(f"{v.title}\n{v.text}")
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else ""


def skills(cfg: Config, mine: list[str], text: str) -> tuple[list[str], list[str]]:
    found = {name for name, rx in cfg.skills_known.items() if rx.search(text)}
    matched = [s for s in mine if s in found]
    missing = [s for s in cfg.skills_known if s in found and s not in mine]
    return matched, missing


def salary_rub_max(text: str) -> int | None:
    """Верхняя граница зарплаты в рублях из строки вида «от 120 000 до 150 000 ₽» / «150к»; None — не рубли или не понять."""
    if not text or re.search(r"\$|€|usd|eur", text, re.I):
        return None
    nums = []
    for m in re.finditer(r"(\d{2,3}(?:[\s\u00a0.,]?\d{3})*)\s*(k\b|к\b|тыс)?", text, re.I):
        n = int(re.sub(r"\D", "", m.group(1)))
        nums.append(n * 1000 if m.group(2) and n < 10000 else n)
    nums = [n for n in nums if n >= 10000]
    return max(nums) if nums else None


def location(m: Matcher, v: Vacancy) -> tuple[str, str]:
    """Возвращает (решение, подпись): решение — ok / reject / unknown."""
    text = f"{v.title}\n{v.city}\n{v.text}"
    remote = v.remote if v.remote is not None else bool(REMOTE_RX.search(text))
    if NO_REMOTE_RX.search(text):
        remote = False
    hybrid = bool(HYBRID_RX.search(text))
    office = bool(OFFICE_RX.search(text))
    home = bool(m.home_rx and (m.home_rx.search(v.city) if v.city else m.home_rx.search(text)))

    if home:
        detected = {f for f, on in (("office", office), ("hybrid", hybrid), ("remote", remote)) if on}
        if detected and not detected & m.home_formats:
            return "reject", ""
        names = {"office": "офис", "hybrid": "гибрид", "remote": "удалённо"}
        return "ok", " · ".join([m.home_label] + [names[f] for f in ("office", "hybrid", "remote") if f in detected])
    if remote and m.remote_other_cities:
        return "ok", "удалённо" + (f" ({v.city})" if v.city else "")
    if v.city or OTHER_CITY_RX.search(text) or office or hybrid or remote:
        return "reject", ""
    return "unknown", "формат не указан"


def check(cfg: Config, m: Matcher, v: Vacancy) -> Verdict:
    text = f"{v.title}\n{v.text}"
    if cfg.text_exclude_rx and cfg.text_exclude_rx.search(text):
        return Verdict(False, "не вакансия")
    if m.title_exclude_rx and m.title_exclude_rx.search(v.title):
        return Verdict(False, "уровень")

    # у RSS и API заголовок надёжный — роль ищем только в нём; в Telegram заголовка нет, берём начало поста
    role_text = f"{v.title}\n{v.text[:600]}" if v.source.startswith("telegram:") else v.title
    role_ok = bool(m.roles_rx and m.roles_rx.search(role_text))
    if not role_ok and m.roles_need_linux_rx and m.roles_need_linux_rx.search(role_text):
        role_ok = bool(LINUX_RX.search(text))
    if not role_ok:
        return Verdict(False, "роль")

    exp = experience_years(v)
    if exp is not None and exp > m.max_experience_years:
        return Verdict(False, "опыт")

    decision, loc = location(m, v)
    if decision == "reject" or (decision == "unknown" and m.unknown_location == "skip"):
        return Verdict(False, "город/формат")

    if m.salary_min:
        top = salary_rub_max(salary(v))
        if top is not None and top < m.salary_min:
            return Verdict(False, "зарплата")

    level = "junior" if JUNIOR_RX.search(text) else "middle" if MIDDLE_RX.search(text) else ""
    matched, missing = skills(cfg, m.skills_mine, f"{text}\n{' '.join(v.tags)}")
    return Verdict(True, location=loc, level=level, matched=matched, missing=missing)


def fingerprint(v: Vacancy) -> str:
    """Для склейки одной вакансии из разных источников."""
    norm = lambda s: re.sub(r"[^a-zа-яё0-9]+", "", s.lower())  # noqa: E731
    return norm(v.title)[:60] + "|" + norm(v.company)[:40]
