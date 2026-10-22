"""Общие сетевые помощники для источников: запрос с честным User-Agent и очистка HTML."""
import html
import re
import ssl
from functools import lru_cache
from pathlib import Path

import aiohttp

UA = "job-watch/1.0 (personal job alerts; +https://github.com/RealPycuk/job-watch)"
TIMEOUT = aiohttp.ClientTimeout(total=30)
RU_CA = Path(__file__).resolve().parent.parent / "certs" / "russian_trusted_root_ca.pem"


@lru_cache(maxsize=1)
def ru_ssl() -> ssl.SSLContext:
    """Системные корни + корень Минцифры — только для сайтов, отмеченных russian_ca = true."""
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(str(RU_CA))
    return ctx


async def _get(session: aiohttp.ClientSession, url: str, ssl_ctx: ssl.SSLContext | None = None) -> str:
    kw = {"ssl": ssl_ctx} if ssl_ctx is not None else {}
    async with session.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, **kw) as r:
        r.raise_for_status()
        return await r.text()


def _clean(s: str) -> str:
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s or "", flags=re.S)
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    return re.sub(r"[ \t ]+", " ", s).strip()
