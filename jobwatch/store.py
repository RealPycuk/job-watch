"""SQLite: вакансии (общие для всех), доставки по пользователям, пользователи и приглашения, здоровье источников."""
import json
import secrets
import time

import aiosqlite

from .models import Vacancy

SCHEMA = """
create table if not exists vacancies(
    id integer primary key autoincrement,
    key text not null unique,            -- source:ext_id
    source text not null, title text not null, company text, url text not null, text text,
    salary text, city text, remote integer, experience integer, published_at real, tags text,
    fingerprint text, created_at real not null, contacts text);
create index if not exists idx_vac_created on vacancies(created_at);
create table if not exists deliveries(
    user_id integer not null, vacancy_id integer not null,
    status text not null,                -- sent / filtered / duplicate / backlog
    reason text, location text, matched text, missing text, fingerprint text,
    saved integer not null default 0, hidden integer not null default 0, created_at real not null,
    primary key(user_id, vacancy_id));
create index if not exists idx_deliv_fp on deliveries(user_id, fingerprint, created_at);
create table if not exists users(
    tg_id integer primary key, name text, step text not null,   -- consent / key / resume / review / active / blocked
    profile text, resume text, gigachat_key text, paused integer not null default 0,
    invited_by integer, created_at real not null, activated_at real);
create table if not exists invites(code text primary key, created_by integer, created_at real, expires_at real, used_by integer);
create table if not exists sources(name text primary key, last_ok real, last_error text, failures integer not null default 0,
                                   last_count integer not null default 0);
create table if not exists kv(k text primary key, v text);
"""


class Store:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    @classmethod
    async def open(cls, path, owner_id: int = 0) -> "Store":
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await cls._migrate_v1(db, owner_id)
        await db.executescript(SCHEMA)
        cols = {r["name"] for r in await (await db.execute("pragma table_info(vacancies)")).fetchall()}
        if "contacts" not in cols:                       # v2 → v2.1: контакты HR
            await db.execute("alter table vacancies add column contacts text")
        await db.commit()
        return cls(db)

    @staticmethod
    async def _migrate_v1(db: aiosqlite.Connection, owner_id: int) -> None:
        """v1 (один пользователь): статус хранился прямо в vacancies — переносим в deliveries владельца."""
        cols = {r["name"] for r in await (await db.execute("pragma table_info(vacancies)")).fetchall()}
        if "status" not in cols:
            return
        await db.execute("alter table vacancies rename to vacancies_v1")
        await db.executescript(SCHEMA)
        await db.execute(
            "insert into vacancies(id, key, source, title, company, url, text, salary, city, remote, experience, "
            "published_at, tags, fingerprint, created_at) select id, key, source, title, company, url, text, salary, city, "
            "remote, experience, published_at, '[]', fingerprint, created_at from vacancies_v1")
        await db.execute(
            "insert into deliveries(user_id, vacancy_id, status, reason, location, matched, missing, fingerprint, saved, "
            "hidden, created_at) select ?, id, status, reason, location, matched, missing, fingerprint, saved, hidden, "
            "created_at from vacancies_v1", (owner_id,))
        await db.execute("drop table vacancies_v1")
        await db.commit()

    async def close(self):
        await self.db.close()

    # ───────────── вакансии ─────────────

    async def vacancy_id(self, key: str) -> int | None:
        row = await (await self.db.execute("select id from vacancies where key=?", (key,))).fetchone()
        return row["id"] if row else None

    async def source_is_new(self, source: str) -> bool:
        return await (await self.db.execute("select 1 from vacancies where source=? limit 1", (source,))).fetchone() is None

    async def add_vacancy(self, v: Vacancy, fingerprint: str) -> int:
        cur = await self.db.execute(
            "insert or ignore into vacancies(key, source, title, company, url, text, salary, city, remote, experience, "
            "published_at, tags, fingerprint, created_at, contacts) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (v.key, v.source, v.title, v.company, v.url, v.text, v.salary, v.city,
             None if v.remote is None else int(v.remote), v.experience_years, v.published_at,
             json.dumps(v.tags, ensure_ascii=False), fingerprint, time.time(), json.dumps(v.contacts, ensure_ascii=False)))
        await self.db.commit()
        return cur.lastrowid or await self.vacancy_id(v.key)

    async def get_vacancy(self, vid: int):
        return await (await self.db.execute("select * from vacancies where id=?", (vid,))).fetchone()

    async def recent_vacancies(self, since: float):
        return await (await self.db.execute(
            "select * from vacancies where coalesce(nullif(published_at, 0), created_at) > ? order by published_at",
            (since,))).fetchall()

    @staticmethod
    def to_vacancy(row) -> Vacancy:
        return Vacancy(source=row["source"], ext_id=row["key"].split(":", 1)[-1], title=row["title"], url=row["url"],
                       text=row["text"] or "", company=row["company"] or "", salary=row["salary"] or "",
                       city=row["city"] or "", remote=None if row["remote"] is None else bool(row["remote"]),
                       experience_years=row["experience"], published_at=row["published_at"] or 0,
                       tags=json.loads(row["tags"] or "[]"), contacts=json.loads(row["contacts"] or "[]"))

    # ───────────── доставки ─────────────

    async def delivered(self, user_id: int, vid: int) -> bool:
        return await (await self.db.execute("select 1 from deliveries where user_id=? and vacancy_id=?",
                                            (user_id, vid))).fetchone() is not None

    async def duplicate_for(self, user_id: int, fingerprint: str, days: int = 14) -> bool:
        return await (await self.db.execute(
            "select 1 from deliveries where user_id=? and fingerprint=? and status='sent' and created_at>?",
            (user_id, fingerprint, time.time() - days * 86400))).fetchone() is not None

    async def deliver(self, user_id: int, vid: int, *, status: str, reason: str = "", location: str = "",
                      matched=None, missing=None, fingerprint: str = ""):
        await self.db.execute(
            "insert or ignore into deliveries(user_id, vacancy_id, status, reason, location, matched, missing, fingerprint, "
            "created_at) values(?,?,?,?,?,?,?,?,?)",
            (user_id, vid, status, reason, location, json.dumps(matched or [], ensure_ascii=False),
             json.dumps(missing or [], ensure_ascii=False), fingerprint, time.time()))
        await self.db.commit()

    async def delivery(self, user_id: int, vid: int):
        return await (await self.db.execute(
            "select v.*, d.status, d.reason, d.location, d.matched, d.missing, d.saved, d.hidden from deliveries d "
            "join vacancies v on v.id=d.vacancy_id where d.user_id=? and d.vacancy_id=?", (user_id, vid))).fetchone()

    async def mark(self, user_id: int, vid: int, *, saved: bool | None = None, hidden: bool | None = None):
        if saved is not None:
            await self.db.execute("update deliveries set saved=? where user_id=? and vacancy_id=?", (int(saved), user_id, vid))
        if hidden is not None:
            await self.db.execute("update deliveries set hidden=? where user_id=? and vacancy_id=?", (int(hidden), user_id, vid))
        await self.db.commit()

    async def saved(self, user_id: int, limit: int = 20):
        return await (await self.db.execute(
            "select v.*, d.location, d.matched, d.missing, d.saved from deliveries d join vacancies v on v.id=d.vacancy_id "
            "where d.user_id=? and d.saved=1 and d.hidden=0 order by d.created_at desc limit ?", (user_id, limit))).fetchall()

    async def stats(self, user_id: int, since: float):
        return await (await self.db.execute(
            "select status, coalesce(reason,'') reason, count(*) n from deliveries where user_id=? and created_at>? "
            "group by status, reason order by n desc", (user_id, since))).fetchall()

    # ───────────── пользователи и приглашения ─────────────

    async def user(self, tg_id: int):
        return await (await self.db.execute("select * from users where tg_id=?", (tg_id,))).fetchone()

    async def users(self, step: str | None = None):
        q, args = "select * from users", ()
        if step:
            q, args = q + " where step=?", (step,)
        return await (await self.db.execute(q + " order by created_at", args)).fetchall()

    async def add_user(self, tg_id: int, *, step: str, invited_by: int | None = None, name: str = "") -> bool:
        cur = await self.db.execute("insert or ignore into users(tg_id, name, step, invited_by, created_at) values(?,?,?,?,?)",
                                    (tg_id, name, step, invited_by, time.time()))
        await self.db.commit()
        return cur.rowcount == 1

    async def update_user(self, tg_id: int, **fields):
        if not fields:
            return
        if "profile" in fields and not isinstance(fields["profile"], (str, type(None))):
            fields["profile"] = json.dumps(fields["profile"], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in fields)
        await self.db.execute(f"update users set {sets} where tg_id=?", (*fields.values(), tg_id))
        await self.db.commit()

    async def delete_user(self, tg_id: int):
        await self.db.execute("delete from deliveries where user_id=?", (tg_id,))
        await self.db.execute("delete from users where tg_id=?", (tg_id,))
        await self.db.commit()

    async def create_invite(self, created_by: int, days: int = 7) -> str:
        code = secrets.token_urlsafe(9)
        await self.db.execute("insert into invites(code, created_by, created_at, expires_at) values(?,?,?,?)",
                              (code, created_by, time.time(), time.time() + days * 86400))
        await self.db.commit()
        return code

    async def use_invite(self, code: str, tg_id: int) -> int | None:
        """Погасить приглашение; возвращает, кто его создал, или None, если код недействителен."""
        row = await (await self.db.execute("select * from invites where code=? and used_by is null and expires_at>?",
                                           (code, time.time()))).fetchone()
        if not row:
            return None
        cur = await self.db.execute("update invites set used_by=? where code=? and used_by is null", (tg_id, code))
        await self.db.commit()
        return row["created_by"] if cur.rowcount == 1 else None

    # ───────────── источники, kv ─────────────

    async def source_ok(self, name: str, count: int):
        await self.db.execute(
            "insert into sources(name, last_ok, failures, last_count) values(?,?,0,?) "
            "on conflict(name) do update set last_ok=excluded.last_ok, failures=0, last_count=excluded.last_count",
            (name, time.time(), count))
        await self.db.commit()

    async def source_failed(self, name: str, error: str) -> int:
        await self.db.execute(
            "insert into sources(name, last_error, failures) values(?,?,1) "
            "on conflict(name) do update set last_error=excluded.last_error, failures=failures+1", (name, error[:300]))
        await self.db.commit()
        row = await (await self.db.execute("select failures from sources where name=?", (name,))).fetchone()
        return row["failures"]

    async def sources(self):
        return await (await self.db.execute("select * from sources order by name")).fetchall()

    async def kv_get(self, k: str, default: str = "") -> str:
        row = await (await self.db.execute("select v from kv where k=?", (k,))).fetchone()
        return row["v"] if row else default

    async def kv_set(self, k: str, v: str):
        await self.db.execute("insert into kv(k, v) values(?,?) on conflict(k) do update set v=excluded.v", (k, v))
        await self.db.commit()
