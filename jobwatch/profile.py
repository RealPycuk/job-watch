"""Профиль соискателя: какие вакансии ему подходят. Хранится в БД как JSON, для фильтра компилируется в Matcher."""
import re
from dataclasses import dataclass, field

from .config import Config

LEVELS = {"intern": "стажёр", "junior": "junior", "middle": "middle", "senior": "senior"}
FORMATS = {"office": "офис", "hybrid": "гибрид", "remote": "удалённо"}

# что отсекать в заголовке в зависимости от уровня кандидата
TITLE_EXCLUDE = {
    "intern": ['senior', r'\blead\b', 'team ?lead', 'тимлид', r'\bhead\b', 'principal', r'\bstaff\b', 'architect',
               'архитектор', r'ведущ\w*', r'старш\w*', r'руководител\w*', 'директор', r'\bcto\b', r'сень?[оё]р\w*',
               r'синь?[оё]р\w*', r'\bleader\b'],
    "middle": [r'\blead\b', 'team ?lead', 'тимлид', r'\bhead\b', 'principal', r'\bstaff\b', 'architect', 'архитектор',
               r'руководител\w*', 'директор', r'\bcto\b', r'\bleader\b'],
    "senior": [r'\bhead\b', 'директор', r'\bcto\b'],
}
TITLE_EXCLUDE["junior"] = TITLE_EXCLUDE["intern"]

CITY_ALIASES = {
    "санкт-петербург": ['санкт[- ]?петербург', r'\bспб\b', r'\bпитер\w*', 'saint[- ]petersburg', r'st\.? ?petersburg',
                        r'ленинградск\w+ обл'],
    "москва": [r'москв\w*', r'\bмск\b', r'\bmoscow\b', r'московск\w+ обл'],
    "екатеринбург": [r'екатеринбург\w*', r'\bекб\b', 'yekaterinburg'],
    "нижний новгород": [r'нижн\w+ новгород\w*', r'\bнн\b'],
}
CITY_SHORT = {"санкт-петербург": "СПб", "москва": "Москва"}


def _rx(patterns: list[str]) -> re.Pattern | None:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I) if patterns else None


def keyword_rx(word: str) -> str:
    """«DevOps-инженер» → устойчивая к дефисам и окончаниям регулярка."""
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", word.strip().lower()) if p]
    return r"[\s\-]?".join(parts) + r"\w*" if parts else ""


def city_patterns(city: str) -> list[str]:
    key = city.strip().lower().replace("ё", "е")
    if key in CITY_ALIASES:
        return CITY_ALIASES[key]
    stem = key[:max(4, len(key) - 2)]
    return [re.escape(stem) + r"\w*"] if stem else []


@dataclass
class Profile:
    title: str = ""                                     # желаемая должность
    roles: list[str] = field(default_factory=list)      # ключевые слова ролей
    level: str = "junior"
    experience_years: int = 0
    skills: list[str] = field(default_factory=list)     # названия из skills.known
    city: str = ""
    home_formats: list[str] = field(default_factory=lambda: list(FORMATS))
    remote_other_cities: bool = True
    salary_min: int | None = None
    summary: str = ""                                    # 1–2 предложения о себе для письма-шаблона
    signature: str = ""
    # «тонкая» настройка регулярками (у владельца — из config.toml); у остальных строится из полей выше
    roles_rx: list[str] = field(default_factory=list)
    roles_need_linux_rx: list[str] = field(default_factory=list)
    title_exclude_rx: list[str] = field(default_factory=list)
    max_experience_years: int | None = None
    unknown_location: str = "notify"

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        known = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @classmethod
    def from_config(cls, cfg: Config, *, summary: str = "", signature: str = "") -> "Profile":
        """Профиль владельца — ровно те правила, что в config.toml."""
        f = cfg.raw["filter"]
        return cls(title="Junior DevOps-инженер", roles=["devops", "sre", "linux"], level="junior",
                   experience_years=1, skills=list(cfg.skills_mine), city=cfg.home_city,
                   roles_rx=list(f["roles"]), roles_need_linux_rx=list(f.get("roles_need_linux", [])),
                   title_exclude_rx=list(f.get("title_exclude", [])), max_experience_years=cfg.max_experience_years,
                   unknown_location=cfg.unknown_location, summary=summary, signature=signature)

    def search_queries(self, limit: int = 3) -> list[str]:
        return [r for r in self.roles if r.strip()][:limit]

    def describe(self) -> str:
        fmts = ", ".join(FORMATS[f] for f in self.home_formats if f in FORMATS) or "—"
        other = "только удалённо" if self.remote_other_cities else "не рассматриваю"
        lines = [f"🎯 <b>{self.title or 'должность не указана'}</b>",
                 f"🧭 Роли: {', '.join(self.roles) or '—'}",
                 f"📈 Уровень: {LEVELS.get(self.level, self.level)}, опыт {self.experience_years} г.",
                 f"🏙 {self.city or 'город не указан'}: {fmts}; другие города: {other}",
                 f"💰 Мин. зарплата: {f'{self.salary_min:,} ₽'.replace(',', ' ') if self.salary_min else 'не важно'}",
                 f"🧩 Навыки: {', '.join(self.skills) or '—'}"]
        return "\n".join(lines)


@dataclass
class Matcher:
    """Скомпилированный профиль для analyze.check()."""
    roles_rx: re.Pattern | None
    roles_need_linux_rx: re.Pattern | None
    title_exclude_rx: re.Pattern | None
    max_experience_years: int
    home_rx: re.Pattern | None
    home_label: str
    home_formats: set[str]
    remote_other_cities: bool
    unknown_location: str
    skills_mine: list[str]
    salary_min: int | None


def compile_profile(p: Profile) -> Matcher:
    roles = p.roles_rx or [keyword_rx(r) for r in p.roles if keyword_rx(r)]
    title_ex = p.title_exclude_rx or TITLE_EXCLUDE.get(p.level, TITLE_EXCLUDE["junior"])
    max_exp = p.max_experience_years if p.max_experience_years is not None else (
        p.experience_years + 1 if p.level in ("intern", "junior") else p.experience_years + 3)
    key = p.city.strip().lower().replace("ё", "е")
    return Matcher(roles_rx=_rx(roles), roles_need_linux_rx=_rx(p.roles_need_linux_rx), title_exclude_rx=_rx(title_ex),
                   max_experience_years=max(max_exp, 1), home_rx=_rx(city_patterns(p.city)) if p.city else None,
                   home_label=CITY_SHORT.get(key, p.city.strip()), home_formats=set(p.home_formats),
                   remote_other_cities=p.remote_other_cities, unknown_location=p.unknown_location,
                   skills_mine=list(p.skills), salary_min=p.salary_min)
