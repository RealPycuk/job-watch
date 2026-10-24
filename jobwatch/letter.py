"""Сопроводительное письмо: GigaChat, а если ключа нет или API недоступно — шаблон из фактов профиля."""
import re
import ssl
import time
import uuid
from pathlib import Path

import aiohttp

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CA_FILE = Path(__file__).resolve().parent.parent / "certs" / "russian_trusted_root_ca.pem"

SYSTEM_PROMPT = """Ты пишешь сопроводительные письма на русском языке от лица кандидата на вакансию.
Правила:
- используй только факты из профиля кандидата; не придумывай опыт, компании, цифры, сроки и навыки;
- 5–7 предложений, деловой живой тон, без канцелярита и штампов вроде «целеустремлённый» и «стрессоустойчивый»;
- начни с «Здравствуйте!», затем: на какую вакансию откликается кандидат, 2–3 требования вакансии, которые он уже
  применял на практике (конкретно, по профилю), честно — чего пока нет и что он готов освоить, если это важно для вакансии;
- вставь ссылки на подходящие репозитории из списка, не больше двух, ссылками как есть;
- без markdown, без списков, без темы письма; закончи подписью из профиля."""


class GigaChat:
    """Клиент GigaChat API. Доверяет только корневому сертификату Минцифры — и только в этих запросах."""

    def __init__(self, session: aiohttp.ClientSession, auth_key: str, *, scope: str = "GIGACHAT_API_PERS",
                 model: str = "GigaChat-2", api_url: str = "https://api.giga.chat/v1",
                 ca_file: Path = CA_FILE, oauth_url: str = OAUTH_URL):
        self.session, self.auth_key, self.scope, self.model = session, auth_key, scope, model
        self.oauth_url = oauth_url
        self.api_url = api_url.rstrip("/")
        self.ssl = ssl.create_default_context(cafile=str(ca_file))
        self._token, self._expires = "", 0.0

    async def token(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        headers = {"Authorization": f"Basic {self.auth_key}", "RqUID": str(uuid.uuid4()),
                   "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        async with self.session.post(self.oauth_url, data={"scope": self.scope}, headers=headers, ssl=self.ssl,
                                     timeout=aiohttp.ClientTimeout(total=30)) as r:
            data = await r.json(content_type=None)
            if r.status != 200 or "access_token" not in data:
                raise RuntimeError(f"GigaChat OAuth: HTTP {r.status} {data.get('message') or data.get('error') or ''}")
        self._token = data["access_token"]
        self._expires = float(data.get("expires_at", 0)) / 1000 or time.time() + 25 * 60
        return self._token

    async def chat(self, system: str, user: str) -> str:
        body = {"model": self.model, "temperature": 0.6, "max_tokens": 700, "stream": False,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        async with self.session.post(f"{self.api_url}/chat/completions", json=body, ssl=self.ssl,
                                     headers={"Authorization": f"Bearer {await self.token()}", "Accept": "application/json"},
                                     timeout=aiohttp.ClientTimeout(total=90)) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"GigaChat: HTTP {r.status} {data.get('message') or ''}")
        return data["choices"][0]["message"]["content"]


def pick_repos(repos: dict[str, str], matched: list[str]) -> list[str]:
    out = []
    for skill in matched:
        url = repos.get(skill)
        if url and url not in out:
            out.append(url)
    return out[:2]


def build_prompt(profile: str, vac: dict, vacancy_text: str, matched: list[str], missing: list[str], repos: list[str]) -> str:
    title = vac["title"] or "название и компанию определи по тексту вакансии"
    return (f"ПРОФИЛЬ КАНДИДАТА:\n{profile.strip()}\n\n"
            f"ВАКАНСИЯ: {title}" + (f" — {vac['company']}" if vac["company"] else "") + "\n"
            f"Текст вакансии:\n{vacancy_text[:5000]}\n\n"
            f"Совпадающие навыки: {', '.join(matched) or 'не определены'}\n"
            f"Навыки из вакансии, которых нет в профиле: {', '.join(missing) or 'нет'}\n"
            f"Репозитории, которые можно упомянуть: {', '.join(repos) or 'нет'}")


def clean(text: str) -> str:
    text = re.sub(r"\*\*|__|^#+\s*", "", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:3500]


def template(summary: str, sign: str, vac: dict, matched: list[str], missing: list[str], repos: list[str]) -> str:
    """Запасной вариант без нейросети: «о себе» и подпись берутся из профиля."""
    company = f" в {vac['company']}" if vac["company"] else ""
    title = f"вакансию «{vac['title']}»" if vac["title"] else "вашу вакансию"
    parts = [f"Здравствуйте! Откликаюсь на {title}{company}."]
    if summary:
        parts.append(summary)
    if matched:
        parts.append(f"Из требований вакансии на практике уже работал с: {', '.join(matched[:6])}.")
    if missing:
        parts.append(f"С {', '.join(missing[:3])} пока знаком меньше и готов быстро разобраться.")
    if repos:
        parts.append("Код можно посмотреть здесь: " + ", ".join(repos) + ".")
    parts.append("Готов выполнить тестовое задание.")
    return " ".join(parts) + (f"\n\n{sign}" if sign else "")


def title_guess(text: str) -> str:
    """Название вакансии из вставленного текста: первая короткая непустая строка, иначе пусто."""
    for line in text.splitlines():
        line = line.strip(" \t•-—:")
        if line:
            return line if len(line) <= 100 and not line.startswith("http") else ""
    return ""


def field(profile: str, name: str) -> str:
    m = re.search(rf"^{name}:\s*(.+)$", profile, re.M)
    return m.group(1).strip() if m else ""
