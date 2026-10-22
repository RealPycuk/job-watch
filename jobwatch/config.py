import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


def _rx(patterns: list[str]) -> re.Pattern | None:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I) if patterns else None


@dataclass
class Config:
    raw: dict
    interval_minutes: int
    first_run_max_age_hours: int
    first_run_max_messages: int
    home_city: str
    home_rx: re.Pattern
    unknown_location: str
    roles_rx: re.Pattern
    roles_need_linux_rx: re.Pattern | None
    title_exclude_rx: re.Pattern | None
    text_exclude_rx: re.Pattern | None
    max_experience_years: int
    skills_known: dict[str, re.Pattern]
    skills_mine: list[str]
    repos: dict[str, str]

    def source(self, name: str) -> dict:
        return self.raw.get("sources", {}).get(name, {})


def load(path: str | Path) -> Config:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    sch, loc, flt, sk = raw["schedule"], raw["location"], raw["filter"], raw["skills"]
    known = {name: re.compile(rx, re.I) for name, rx in sk["known"].items()}
    unknown_mine = [s for s in sk["mine"] if s not in known]
    if unknown_mine:
        raise ValueError(f"skills.mine содержит навыки без регулярки в skills.known: {unknown_mine}")
    if loc.get("unknown", "notify") not in ("notify", "skip"):
        raise ValueError("location.unknown: ожидается notify или skip")
    return Config(
        raw=raw,
        interval_minutes=int(sch["interval_minutes"]),
        first_run_max_age_hours=int(sch["first_run_max_age_hours"]),
        first_run_max_messages=int(sch["first_run_max_messages"]),
        home_city=loc["home_city"],
        home_rx=_rx(loc["home_patterns"]),
        unknown_location=loc.get("unknown", "notify"),
        roles_rx=_rx(flt["roles"]),
        roles_need_linux_rx=_rx(flt.get("roles_need_linux", [])),
        title_exclude_rx=_rx(flt.get("title_exclude", [])),
        text_exclude_rx=_rx(flt.get("text_exclude", [])),
        max_experience_years=int(flt.get("max_experience_years", 99)),
        skills_known=known,
        skills_mine=list(sk["mine"]),
        repos=dict(raw.get("repos", {})),
    )


@dataclass
class Env:
    bot_token: str
    owner_id: int
    gigachat_key: str
    gigachat_model: str
    gigachat_scope: str
    gigachat_api_url: str
    data_dir: Path
    config_path: Path
    profile_path: Path
    secret: str


def env() -> Env:
    return Env(
        bot_token=os.environ["BOT_TOKEN"],
        owner_id=int(os.environ["OWNER_ID"]),
        gigachat_key=os.environ.get("GIGACHAT_AUTH_KEY", "").strip(),
        gigachat_model=os.environ.get("GIGACHAT_MODEL", "GigaChat-2").strip(),
        gigachat_scope=os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip(),
        gigachat_api_url=os.environ.get("GIGACHAT_API_URL", "https://api.giga.chat/v1").strip(),
        data_dir=Path(os.environ.get("DATA_DIR", "/data")),
        config_path=Path(os.environ.get("CONFIG_PATH", "/app/config.toml")),
        profile_path=Path(os.environ.get("PROFILE_PATH", "/app/profile.md")),
        secret=os.environ.get("JOBWATCH_SECRET", "").strip(),
    )
