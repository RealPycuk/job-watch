from dataclasses import dataclass, field


@dataclass
class Vacancy:
    source: str                 # habr / geekjob / trudvsem / telegram:<channel>
    ext_id: str                 # идентификатор внутри источника
    title: str
    url: str
    text: str = ""              # описание (в Telegram — весь пост)
    company: str = ""
    salary: str = ""
    city: str = ""              # если источник даёт город отдельно
    remote: bool | None = None  # если источник явно отмечает удалёнку
    experience_years: int | None = None
    published_at: float = 0.0   # unix time, 0 — неизвестно
    tags: list[str] = field(default_factory=list)
    contacts: list[str] = field(default_factory=list)   # почта HR и т.п., если источник даёт её отдельным полем

    @property
    def key(self) -> str:
        return f"{self.source}:{self.ext_id}"


@dataclass
class Verdict:
    ok: bool
    reason: str = ""            # почему отброшена (для статистики)
    location: str = ""          # «СПб · гибрид», «удалённо», «формат не указан»
    level: str = ""             # junior / middle / ""
    matched: list[str] = field(default_factory=list)   # мои навыки, которые есть в вакансии
    missing: list[str] = field(default_factory=list)   # навыки из вакансии, которых у меня нет
