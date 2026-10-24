"""Telegram-бот job-watch: собирает вакансии и присылает каждому пользователю подходящие под его профиль.

Доступ — только по приглашению владельца (OWNER_ID): /invite <id> или одноразовая ссылка /invite.
"""
import asyncio
import hashlib
import html
import json
import logging
import random
import time

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup,
                           LinkPreviewOptions, Message)

from . import analyze, config, letter, resume, sources
from .models import Vacancy
from .profile import FORMATS, LEVELS, Profile, compile_profile
from .secret import Vault
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("job-watch")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
BACKFILL_HOURS, BACKFILL_MAX = 72, 10
RESUMES_PER_DAY = 5
e = html.escape

CONSENT = ("📄 <b>Что хранит бот</b>\n\n"
           "• текст твоего резюме и профиль, составленный по нему;\n"
           "• твой ключ GigaChat — в зашифрованном виде;\n"
           "• какие вакансии тебе отправлены, сохранены и скрыты.\n\n"
           "Резюме и тексты вакансий отправляются в GigaChat (Сбер) <b>с твоим ключом</b> — для профиля и писем. "
           "Больше никуда данные не передаются. Удалить всё о себе — команда /delete.")
KEY_HELP = ("🔑 Пришли <b>Authorization Key</b> от GigaChat API — им бот разберёт резюме и будет писать письма.\n\n"
            "Как получить: developers.sber.ru/studio → войти через Сбер ID → создать проект «GigaChat API» "
            "(физлицо) → «Настройки API» → «Получить ключ». Ключ показывается один раз.\n\n"
            "Сообщение с ключом я сразу удалю из чата.")
VACANCY_HELP = ("✍️ Вставь текст вакансии одним сообщением — название, требования, обязанности. "
                "Напишу сопроводительное по нему и твоему резюме.\n\n"
                "Ссылку не открываю: скопируй описание со страницы (hh.ru, сайт компании, пост в канале).")
VACANCY_MIN, VACANCY_MAX = 150, 8000
MODE_TTL = 15 * 60          # «жду резюме / вакансию» после /cv и /letter действует 15 минут
PASTE_GAP = 4.0             # длинный текст Telegram режет на части по 4096 — ждём остальные столько секунд
_sleep = asyncio.sleep
RESUME_HELP = "📎 Пришли резюме файлом (PDF, DOCX, TXT) или вставь текстом одним сообщением."


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[Btn(text=t, callback_data=d) for t, d in row] for row in rows])


class App:
    def __init__(self, env: config.Env, cfg: config.Config, store: Store, http: aiohttp.ClientSession, bot: Bot,
                 vault: Vault):
        self.env, self.cfg, self.store, self.http, self.bot, self.vault = env, cfg, store, http, bot, vault
        self.cycle_lock = asyncio.Lock()
        self.user_locks: dict[int, asyncio.Lock] = {}
        self._giga: dict[int, tuple[str, letter.GigaChat]] = {}
        self.pending: dict[int, dict] = {}         # недособранный длинный текст по пользователям

    def lock(self, uid: int) -> asyncio.Lock:
        return self.user_locks.setdefault(uid, asyncio.Lock())

    def is_owner(self, uid: int) -> bool:
        return uid == self.env.owner_id

    def make_giga(self, key: str) -> letter.GigaChat:
        return letter.GigaChat(self.http, key, scope=self.env.gigachat_scope, model=self.env.gigachat_model,
                               api_url=self.env.gigachat_api_url)

    def giga_for(self, u) -> letter.GigaChat | None:
        key = self.vault.decrypt(u["gigachat_key"]) or (self.env.gigachat_key if self.is_owner(u["tg_id"]) else "")
        if not key:
            return None
        digest = hashlib.sha256(key.encode()).hexdigest()
        cached = self._giga.get(u["tg_id"])
        if not cached or cached[0] != digest:
            cached = (digest, self.make_giga(key))
            self._giga[u["tg_id"]] = cached
        return cached[1]

    async def set_mode(self, uid: int, mode: str):
        """Один режим ожидания ввода за раз: cv — резюме, vac — текст вакансии, '' — ничего."""
        await self.store.kv_set(f"await_cv:{uid}", str(time.time()) if mode == "cv" else "0")
        await self.store.kv_set(f"await_vac:{uid}", str(time.time()) if mode == "vac" else "0")

    async def in_mode(self, uid: int, mode: str) -> bool:
        try:
            return time.time() - float(await self.store.kv_get(f"await_{mode}:{uid}", "0")) < MODE_TTL
        except ValueError:
            return False

    @staticmethod
    def profile_of(u) -> Profile:
        return Profile.from_dict(json.loads(u["profile"] or "{}"))

    async def seed_owner(self):
        """Владелец создаётся сразу активным с правилами из config.toml и фактами из profile.md."""
        if await self.store.user(self.env.owner_id):
            return
        try:
            text = self.env.profile_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        p = Profile.from_config(self.cfg, summary=letter.field(text, "Кратко"), signature=letter.field(text, "Подпись"))
        await self.store.add_user(self.env.owner_id, step="active", name="owner")
        await self.store.update_user(self.env.owner_id, profile=p.to_dict(), resume=text, activated_at=time.time())

    async def say(self, uid: int, text: str, **kw):
        try:
            return await self.bot.send_message(uid, text, link_preview_options=NO_PREVIEW, **kw)
        except Exception as ex:
            log.warning("send to %s failed: %s", uid, ex)

    # ───────────── сбор и рассылка ─────────────

    async def collect(self, users) -> list[Vacancy]:
        extra = []
        for u in users:
            extra += self.profile_of(u).search_queries()
        out = []
        now = time.time()
        for name, fetch in sources.SOURCES.items():
            src = dict(self.cfg.source(name))
            if not src.get("enabled", False):
                continue
            # сайты компаний опрашиваются реже агрегаторов
            every = int(src.get("every_minutes", 0)) * 60
            if every and now - float(await self.store.kv_get(f"fetched:{name}", "0")) < every:
                continue
            if "queries" in src:                     # запросы владельца из конфига + роли всех пользователей
                src["queries"] = list(dict.fromkeys([*src["queries"], *extra]))[:12]
            try:
                found = await fetch(self.http, src)
            except Exception as ex:
                log.warning("source %s failed: %s", name, ex)
                if await self.store.source_failed(name, f"{type(ex).__name__}: {ex}") == 3:
                    await self.say(self.env.owner_id, f"⚠️ Источник «{sources.TITLES[name]}» не отвечает 3 раза подряд: "
                                                      f"<code>{e(str(ex)[:200])}</code>")
                continue
            prev = {r["name"]: r for r in await self.store.sources()}.get(name)
            if prev and prev["failures"] >= 3:
                await self.say(self.env.owner_id, f"✅ Источник «{sources.TITLES[name]}» снова работает")
            await self.store.source_ok(name, len(found))
            await self.store.kv_set(f"fetched:{name}", str(now))
            out += found
        return out

    async def deliver(self, u, m, v: Vacancy, vid: int, fp: str, *, first_run: bool, counter: dict) -> bool:
        uid = u["tg_id"]
        d = analyze.check(self.cfg, m, v)
        if not d.ok:
            await self.store.deliver(uid, vid, status="filtered", reason=d.reason)
            return False
        if fp and await self.store.duplicate_for(uid, fp):
            await self.store.deliver(uid, vid, status="duplicate", fingerprint=fp)
            return False
        if first_run:
            group = (uid, v.source.split(":")[0])
            too_old = v.published_at and v.published_at < time.time() - self.cfg.first_run_max_age_hours * 3600
            if too_old or counter.get(group, 0) >= self.cfg.first_run_max_messages:
                await self.store.deliver(uid, vid, status="backlog", fingerprint=fp)
                return False
            counter[group] = counter.get(group, 0) + 1
        await self.store.deliver(uid, vid, status="sent", fingerprint=fp, location=d.location,
                                 matched=d.matched, missing=d.missing)
        await self.say(uid, self.card(v, d.location, analyze.salary(v), d.matched, d.missing, d.level),
                       reply_markup=self.vac_buttons(vid))
        await asyncio.sleep(1.1)
        return True

    async def cycle(self) -> dict:
        async with self.cycle_lock:
            users = [u for u in await self.store.users("active") if not u["paused"]]
            matchers = {u["tg_id"]: compile_profile(self.profile_of(u)) for u in users}
            vacancies = await self.collect(users)
            new_sources = {v.source for v in vacancies if await self.store.source_is_new(v.source)}
            counter: dict = {}
            counts = {"new": 0, "sent": 0}
            for v in sorted(vacancies, key=lambda x: x.published_at):
                if await self.store.vacancy_id(v.key):
                    continue
                counts["new"] += 1
                if sources.needs_enrich(v) and any(analyze.check(self.cfg, m, v).ok for m in matchers.values()):
                    try:
                        v = await sources.enrich(self.http, v)
                    except Exception as ex:
                        log.warning("enrich %s failed: %s", v.url, ex)
                fp = analyze.fingerprint(v) if v.company else ""
                vid = await self.store.add_vacancy(v, fp)
                for u in users:
                    if await self.deliver(u, matchers[u["tg_id"]], v, vid, fp, first_run=v.source in new_sources,
                                          counter=counter):
                        counts["sent"] += 1
            log.info("cycle: %s vacancies, %s new, %s sent to %s users", len(vacancies), counts["new"], counts["sent"],
                     len(users))
            return counts

    async def backfill(self, uid: int) -> int:
        """Новому пользователю — подходящие вакансии за последние 72 часа из уже собранных."""
        u = await self.store.user(uid)
        m = compile_profile(self.profile_of(u))
        sent = 0
        for row in await self.store.recent_vacancies(time.time() - BACKFILL_HOURS * 3600):
            if await self.store.delivered(uid, row["id"]):
                continue
            v = self.store.to_vacancy(row)
            if sent >= BACKFILL_MAX:
                await self.store.deliver(uid, row["id"], status="backlog", fingerprint=row["fingerprint"] or "")
                continue
            if await self.deliver(u, m, v, row["id"], row["fingerprint"] or "", first_run=False, counter={}):
                sent += 1
        return sent

    async def scheduler(self):
        await asyncio.sleep(5)
        while True:
            try:
                await self.cycle()
            except Exception:
                log.exception("cycle failed")
            await asyncio.sleep(self.cfg.interval_minutes * 60 + random.randint(0, 120))

    # ───────────── карточки ─────────────

    @staticmethod
    def card(v, loc: str, sal: str, matched: list[str], missing: list[str], level: str = "") -> str:
        parts = (f"🏢 {e(v.company)}" if v.company else "", f"📍 {e(loc)}" if loc else "", f"💰 {e(sal)}" if sal else "")
        lines = [f"💼 <b>{e(v.title)}</b>" + (f"  <i>{level}</i>" if level else ""), " · ".join(x for x in parts if x)]
        if matched:
            lines.append(f"🧩 Совпадает: {e(', '.join(matched))}")
        if missing:
            lines.append(f"➕ Не хватает: {e(', '.join(missing))}")
        if cts := analyze.contacts(v):
            lines.append("✉️ " + " · ".join(f"<code>{e(c)}</code>" for c in cts))
        src = sources.TITLES.get(v.source.split(":")[0], v.source)
        if v.source.startswith("telegram:"):
            src += f" @{v.source.split(':', 1)[1]}"
        lines.append(f"🔗 {e(v.url)}\n<i>{e(src)}</i>")
        return "\n".join(x for x in lines if x)

    @staticmethod
    def vac_buttons(vid: int, saved: bool = False) -> InlineKeyboardMarkup:
        return kb([[("✍️ Сопроводительное", f"l:{vid}"), ("📄 Текст", f"f:{vid}")],
                   [("✅ Сохранено" if saved else "⭐ Сохранить", f"s:{vid}"), ("🙈 Скрыть", f"h:{vid}")]])

    # ───────────── письмо ─────────────

    async def write_letter(self, u, row) -> tuple[str, str]:
        v = self.store.to_vacancy(row)
        text = await sources.full_text(self.http, v)
        matched, missing = json.loads(row["matched"] or "[]"), json.loads(row["missing"] or "[]")
        return await self.compose(u, {"title": v.title, "company": v.company}, text, matched, missing)

    async def letter_from_text(self, u, text: str) -> tuple[str, str]:
        """Письмо по тексту вакансии, который пользователь прислал сам (/letter)."""
        matched, missing = analyze.skills(self.cfg, self.profile_of(u).skills, text)
        return await self.compose(u, {"title": letter.title_guess(text), "company": ""}, text, matched, missing)

    async def compose(self, u, vac: dict, text: str, matched: list[str], missing: list[str]) -> tuple[str, str]:
        repos = letter.pick_repos(self.cfg.repos, matched) if self.is_owner(u["tg_id"]) else []
        p = self.profile_of(u)
        facts = "\n\n".join(x for x in (p.summary, u["resume"] or "", f"Подпись: {p.signature}" if p.signature else "") if x)
        giga = self.giga_for(u)
        if giga and facts:
            try:
                out = await giga.chat(letter.SYSTEM_PROMPT, letter.build_prompt(facts, vac, text, matched, missing, repos))
                return letter.clean(out), "GigaChat"
            except Exception as ex:
                log.warning("gigachat failed for %s: %s", u["tg_id"], ex)
                return letter.template(p.summary, p.signature, vac, matched, missing, repos), \
                    f"шаблон (GigaChat: {str(ex)[:80]})"
        return letter.template(p.summary, p.signature, vac, matched, missing, repos), "шаблон"

    # ───────────── профиль ─────────────

    def profile_kb(self, final: bool) -> InlineKeyboardMarkup:
        rows = [[("🏙 Город", "p:city"), ("🧭 Роли", "p:roles")], [("📈 Уровень", "p:level"), ("🏢 Формат", "p:fmt")],
                [("💰 Мин. зарплата", "p:salary"), ("📄 Другое резюме", "p:cv")]]
        if final:
            rows.insert(0, [("✅ Всё верно, начать", "p:done")])
        return kb(rows)

    async def show_profile(self, uid: int, *, final: bool = False, note: str = ""):
        u = await self.store.user(uid)
        head = note or ("Вот как я понял твоё резюме. Поправь, если что-то не так:" if final else "Твой профиль:")
        await self.say(uid, f"{head}\n\n{self.profile_of(u).describe()}", reply_markup=self.profile_kb(final))

    async def edit_profile(self, uid: int, **changes):
        u = await self.store.user(uid)
        p = self.profile_of(u)
        for k, val in changes.items():
            setattr(p, k, val)
        # ручная правка ролей или уровня отменяет «тонкие» регулярки из config.toml
        if "roles" in changes:
            p.roles_rx, p.roles_need_linux_rx = [], []
        if "level" in changes:
            p.title_exclude_rx, p.max_experience_years = [], None
        await self.store.update_user(uid, profile=p.to_dict())

    async def process_resume(self, uid: int, filename: str, data: bytes | None, text: str | None):
        u = await self.store.user(uid)
        giga = self.giga_for(u)
        if not giga:
            await self.store.update_user(uid, step="key")
            await self.say(uid, KEY_HELP)
            return
        day = time.strftime("%Y-%m-%d")
        used = int(await self.store.kv_get(f"cv:{uid}:{day}", "0"))
        if used >= RESUMES_PER_DAY:
            await self.say(uid, "Сегодня резюме уже разбиралось много раз — попробуй завтра.")
            return
        await self.store.kv_set(f"cv:{uid}:{day}", str(used + 1))
        try:
            body = resume.extract_text(filename, data) if data is not None else (text or "")[:resume.MAX_TEXT]
            await self.say(uid, "🔍 Читаю резюме…")
            raw = await giga.chat(resume.profile_prompt(list(self.cfg.skills_known)), body)
            p = resume.parse_profile(raw, list(self.cfg.skills_known))
        except resume.ResumeError as ex:
            await self.say(uid, f"⚠️ {e(str(ex))}\n\n{RESUME_HELP}")
            return
        except Exception as ex:
            log.warning("resume parse failed for %s: %s", uid, ex)
            await self.say(uid, f"⚠️ GigaChat не ответил: <code>{e(str(ex)[:150])}</code>. Попробуй ещё раз чуть позже.")
            return
        if u["step"] == "active":
            await self.store.kv_set(f"prev_profile:{uid}", json.dumps({"profile": u["profile"], "resume": u["resume"]}, ensure_ascii=False))
        await self.store.update_user(uid, profile=p.to_dict(), resume=body, step="review" if u["step"] != "active" else "active")
        await self.show_profile(uid, final=u["step"] != "active", note="" if u["step"] != "active" else "Профиль обновлён:")
        if u["step"] == "active":
            await self.say(uid, "Если это было не резюме — можно вернуть прежний профиль.",
                           reply_markup=kb([[("↩️ Вернуть прежний профиль", "p:undo")]]))


def build_router(app: App) -> Router:
    r = Router()

    async def access(ev) -> object | None:
        """Пользователь из БД или None (и вежливый отказ), если доступа нет."""
        uid = ev.from_user.id
        u = await app.store.user(uid)
        if u and u["step"] != "blocked":
            return u
        msg = (f"🔒 Бот работает по приглашениям.\nТвой ID: <code>{uid}</code> — отправь его владельцу бота, "
               f"чтобы он открыл доступ.")
        if isinstance(ev, CallbackQuery):
            await ev.answer("Нет доступа", show_alert=True)
        else:
            await ev.answer(msg)
        return None

    async def next_step(uid: int):
        u = await app.store.user(uid)
        if u["step"] == "consent":
            await app.say(uid, CONSENT, reply_markup=kb([[("✅ Согласен", "c:ok")]]))
        elif u["step"] == "key":
            await app.say(uid, KEY_HELP)
        elif u["step"] == "resume":
            await app.say(uid, RESUME_HELP)
        elif u["step"] == "review":
            await app.show_profile(uid, final=True)

    # ───────────── вход ─────────────

    @r.message(CommandStart())
    async def start(m: Message, command: CommandObject):
        uid = m.from_user.id
        arg = (command.args or "").strip()
        if arg.startswith("inv_") and not await app.store.user(uid):
            inviter = await app.store.use_invite(arg[4:], uid)
            if inviter:
                await app.store.add_user(uid, step="consent", invited_by=inviter, name=m.from_user.full_name or "")
                who = f"@{m.from_user.username}" if m.from_user.username else e(m.from_user.full_name or str(uid))
                await app.say(app.env.owner_id, f"👤 По приглашению зашёл {who} (<code>{uid}</code>)")
            else:
                await m.answer("Ссылка-приглашение недействительна или уже использована.")
                return
        if not (u := await access(m)):
            return
        if u["step"] == "active":
            await m.answer("👋 Я присылаю новые вакансии под твой профиль.\n\n"
                           "/profile — профиль и фильтры\n/saved — сохранённые\n/status — статистика\n"
                           "/pause и /continue — остановить и продолжить\n/cv — загрузить новое резюме\n"
                           "/key — сменить ключ GigaChat\n/delete — удалить мои данные"
                           + ("\n\n<b>Админ:</b> /invite [id], /revoke id, /users, /check" if app.is_owner(uid) else ""))
        else:
            await next_step(uid)

    @r.callback_query(F.data == "c:ok")
    async def consent(c: CallbackQuery):
        if not (u := await access(c)):
            return
        if u["step"] == "consent":
            await app.store.update_user(c.from_user.id, step="key")
        await c.answer()
        await next_step(c.from_user.id)

    # ───────────── администратор ─────────────

    @r.message(Command("invite"))
    async def invite(m: Message, command: CommandObject):
        if not app.is_owner(m.from_user.id):
            return
        arg = (command.args or "").strip()
        if arg:
            if not arg.isdigit():
                await m.answer("Формат: /invite 123456789 — или просто /invite для ссылки-приглашения")
                return
            if await app.store.add_user(int(arg), step="consent", invited_by=m.from_user.id):
                await m.answer(f"✅ Доступ открыт для <code>{arg}</code>. Пусть напишет боту /start.")
            else:
                await m.answer("Этот пользователь уже есть.")
            return
        code = await app.store.create_invite(m.from_user.id)
        me = await app.bot.me()
        await m.answer(f"🔗 Одноразовая ссылка на 7 дней:\nhttps://t.me/{me.username}?start=inv_{code}")

    @r.message(Command("revoke"))
    async def revoke(m: Message, command: CommandObject):
        if not app.is_owner(m.from_user.id):
            return
        arg = (command.args or "").strip()
        if not arg.isdigit() or int(arg) == app.env.owner_id:
            await m.answer("Формат: /revoke 123456789")
            return
        await app.store.delete_user(int(arg))
        await m.answer(f"🗑 Доступ закрыт, данные <code>{arg}</code> удалены.")

    @r.message(Command("users"))
    async def users(m: Message):
        if not app.is_owner(m.from_user.id):
            return
        lines = ["👥 <b>Пользователи</b>"]
        for u in await app.store.users():
            p = app.profile_of(u)
            sent = sum(row["n"] for row in await app.store.stats(u["tg_id"], time.time() - 7 * 86400) if row["status"] == "sent")
            lines.append(f"<code>{u['tg_id']}</code> {e(u['name'] or '')} · {u['step']}{' · пауза' if u['paused'] else ''}"
                         f" · {e(p.title or '—')} · за неделю {sent}")
        await m.answer("\n".join(lines))

    @r.message(Command("check"))
    async def check(m: Message):
        if not app.is_owner(m.from_user.id):
            return
        if app.cycle_lock.locked():
            await m.answer("Уже проверяю, подожди немного.")
            return
        await m.answer("🔎 Проверяю источники…")
        c = await app.cycle()
        await m.answer(f"Готово: новых {c['new']}, отправлено {c['sent']}.")

    # ───────────── пользователь ─────────────

    @r.message(Command("status"))
    async def status(m: Message):
        if not (u := await access(m)):
            return
        lines = []
        if app.is_owner(m.from_user.id):
            lines.append("📡 <b>Источники</b>")
            for s in await app.store.sources():
                ok = time.strftime("%d.%m %H:%M", time.localtime(s["last_ok"])) if s["last_ok"] else "—"
                err = f" · ошибок подряд: {s['failures']}" if s["failures"] else ""
                lines.append(f"{'✅' if not s['failures'] else '⚠️'} {sources.TITLES.get(s['name'], s['name'])}: "
                             f"{s['last_count']} шт., {ok}{err}")
            lines.append("")
        lines.append("📊 <b>За 7 дней</b>")
        names = {"sent": "отправлено", "filtered": "отсеяно", "duplicate": "дубли", "backlog": "старые"}
        for row in await app.store.stats(u["tg_id"], time.time() - 7 * 86400):
            lines.append(f"{names.get(row['status'], row['status'])}{f' ({row[1]})' if row['reason'] else ''}: {row['n']}")
        lines.append(f"\n{'⏸ на паузе' if u['paused'] else '▶️ работает'}, письма: "
                     f"{'GigaChat' if app.giga_for(u) else 'шаблон'}")
        await m.answer("\n".join(lines))

    @r.message(Command("profile"))
    async def profile(m: Message):
        if (u := await access(m)) and u["step"] == "active":
            await app.show_profile(m.from_user.id)

    @r.message(Command("saved"))
    async def saved(m: Message):
        if not (u := await access(m)):
            return
        rows = await app.store.saved(u["tg_id"])
        if not rows:
            await m.answer("Сохранённых вакансий пока нет.")
        for row in rows:
            v = app.store.to_vacancy(row)
            await m.answer(app.card(v, row["location"], row["salary"], json.loads(row["matched"]), json.loads(row["missing"])),
                           reply_markup=app.vac_buttons(row["id"], saved=True), link_preview_options=NO_PREVIEW)

    @r.message(Command("pause", "continue"))
    async def pause(m: Message):
        if not await access(m):
            return
        on = m.text.startswith("/pause")
        await app.store.update_user(m.from_user.id, paused=int(on))
        await m.answer("⏸ Рассылка на паузе. /continue — продолжить." if on else "▶️ Рассылка продолжается.")

    @r.message(Command("cv"))
    async def cv(m: Message):
        if (u := await access(m)) and u["step"] in ("active", "review"):
            await app.store.update_user(m.from_user.id, step="resume" if u["step"] != "active" else "active")
            await app.set_mode(m.from_user.id, "cv")
            await m.answer(RESUME_HELP)

    @r.message(Command("letter"))
    async def letter_cmd(m: Message):
        if (u := await access(m)) and u["step"] == "active":
            await app.set_mode(m.from_user.id, "vac")
            await m.answer(VACANCY_HELP)

    async def send_letter(m: Message, u, text: str):
        uid = u["tg_id"]
        if app.lock(uid).locked():
            await m.answer("Уже пишу письмо, подожди")
            return
        note = await m.answer("✍️ Пишу письмо…")
        async with app.lock(uid):
            out, how = await app.letter_from_text(u, text)
        try:
            await note.delete()
        except Exception:
            pass
        await m.answer(f"{e(out)}\n\n<i>— {e(how)}</i>", link_preview_options=NO_PREVIEW,
                       reply_markup=kb([[("🔁 Другой вариант", "lt:again")]]))

    @r.callback_query(F.data == "lt:again")
    async def letter_again(c: CallbackQuery):
        if not (u := await access(c)):
            return
        text = await app.store.kv_get(f"vac_text:{c.from_user.id}")
        if not text:
            await c.answer("Текст вакансии не сохранился — пришли его ещё раз через /letter", show_alert=True)
            return
        await c.answer()
        await send_letter(c.message, u, text)

    @r.message(Command("key"))
    async def key_cmd(m: Message):
        if await access(m):
            await app.store.kv_set(f"await_key:{m.from_user.id}", "1")
            await m.answer(KEY_HELP)

    @r.message(Command("delete"))
    async def delete(m: Message):
        if await access(m):
            await m.answer("Удалить профиль, резюме, ключ и историю вакансий? Вернуться можно будет только по новому приглашению.",
                           reply_markup=kb([[("🗑 Да, удалить", "del:yes"), ("Отмена", "del:no")]]))

    @r.callback_query(F.data.startswith("del:"))
    async def delete_yes(c: CallbackQuery):
        await c.answer()
        if c.data == "del:yes" and not app.is_owner(c.from_user.id):
            await app.store.delete_user(c.from_user.id)
            await c.message.edit_text("🗑 Все твои данные удалены.")
        elif c.data == "del:yes":
            await c.message.edit_text("Владельца удалить нельзя — профиль можно поменять через /profile.")
        else:
            await c.message.edit_text("Отменено.")

    # ───────────── правка профиля ─────────────

    @r.callback_query(F.data.startswith("p:"))
    async def profile_btn(c: CallbackQuery):
        if not (u := await access(c)):
            return
        uid, action = c.from_user.id, c.data[2:]
        await c.answer()
        prompts = {"city": "🏙 Напиши свой город (например: Санкт-Петербург).",
                   "roles": "🧭 Напиши роли через запятую — по ним ищутся вакансии (например: devops, sre, linux).",
                   "salary": "💰 Минимальная зарплата в рублях в месяц числом, или 0 — не важно."}
        if action in prompts:
            await app.store.kv_set(f"edit:{uid}", action)
            await app.say(uid, prompts[action])
        elif action == "level":
            await app.say(uid, "📈 Твой уровень:", reply_markup=kb([[(n, f"lv:{k}") for k, n in LEVELS.items()]]))
        elif action == "fmt":
            await fmt_menu(uid, c.message)
        elif action == "cv":
            await app.set_mode(uid, "cv")
            await app.say(uid, RESUME_HELP)
        elif action == "undo":
            prev = await app.store.kv_get(f"prev_profile:{uid}")
            if not prev:
                await c.answer("Прежнего профиля нет", show_alert=True)
                return
            prev = json.loads(prev)
            await app.store.update_user(uid, profile=json.loads(prev["profile"] or "{}"), resume=prev["resume"] or "")
            await app.store.kv_set(f"prev_profile:{uid}", "")
            await c.message.delete()
            await app.show_profile(uid, note="↩️ Вернул прежний профиль:")
        elif action == "done" and u["step"] == "review":
            await app.store.update_user(uid, step="active", activated_at=time.time())
            await app.say(uid, "✅ Готово! Новые вакансии буду присылать по мере появления. Сначала — подходящие за последние 3 дня:")
            n = await app.backfill(uid)
            if not n:
                await app.say(uid, "За последние 3 дня подходящих не нашлось — пришлю, как только появятся.")

    async def fmt_menu(uid: int, msg: Message | None = None):
        p = app.profile_of(await app.store.user(uid))
        rows = [[(("✅ " if f in p.home_formats else "▫️ ") + n, f"fm:{f}") for f, n in FORMATS.items()],
                [(("✅" if p.remote_other_cities else "▫️") + " Удалённо в других городах", "fm:other")],
                [("⬅️ Готово", "fm:done")]]
        text = f"🏢 Какие форматы подходят в твоём городе ({e(p.city or 'не указан')})?"
        if msg:
            try:
                await msg.edit_text(text, reply_markup=kb(rows))
                return
            except Exception:
                pass
        await app.say(uid, text, reply_markup=kb(rows))

    @r.callback_query(F.data.startswith("fm:"))
    async def fmt_btn(c: CallbackQuery):
        if not (u := await access(c)):
            return
        uid, f = c.from_user.id, c.data[3:]
        await c.answer()
        p = app.profile_of(u)
        if f == "done":
            await c.message.delete()
            await app.show_profile(uid, final=u["step"] == "review")
            return
        if f == "other":
            await app.edit_profile(uid, remote_other_cities=not p.remote_other_cities)
        elif f in FORMATS:
            new = [x for x in FORMATS if (x in p.home_formats) != (x == f)]
            await app.edit_profile(uid, home_formats=new or [f])
        await fmt_menu(uid, c.message)

    @r.callback_query(F.data.startswith("lv:"))
    async def level_btn(c: CallbackQuery):
        if not (u := await access(c)) or c.data[3:] not in LEVELS:
            return
        await app.edit_profile(c.from_user.id, level=c.data[3:])
        await c.answer("Уровень обновлён")
        await c.message.delete()
        await app.show_profile(c.from_user.id, final=u["step"] == "review")

    # ───────────── вакансии ─────────────

    async def vac_of(c: CallbackQuery):
        try:
            row = await app.store.delivery(c.from_user.id, int(c.data.split(":", 1)[1]))
        except ValueError:
            row = None
        if not row:
            await c.answer("Вакансия не найдена", show_alert=True)
        return row

    @r.callback_query(F.data.regexp(r"^[sfhl]:\d+$"))
    async def vac_btn(c: CallbackQuery):
        if not (u := await access(c)) or not (row := await vac_of(c)):
            return
        act, uid = c.data[0], c.from_user.id
        if act == "s":
            new = not row["saved"]
            await app.store.mark(uid, row["id"], saved=new)
            await c.message.edit_reply_markup(reply_markup=app.vac_buttons(row["id"], saved=new))
            await c.answer("Сохранено" if new else "Убрано из сохранённых")
        elif act == "h":
            await app.store.mark(uid, row["id"], hidden=True)
            await c.message.delete()
            await c.answer("Скрыто")
        elif act == "f":
            await c.answer()
            text = await sources.full_text(app.http, app.store.to_vacancy(row))
            await c.message.reply(e(text[:3800]) or "Текста нет", link_preview_options=NO_PREVIEW)
        elif act == "l":
            if app.lock(uid).locked():
                await c.answer("Уже пишу письмо, подожди", show_alert=True)
                return
            await c.answer("✍️ Пишу письмо…")
            async with app.lock(uid):
                text, how = await app.write_letter(u, row)
            await c.message.reply(f"{e(text)}\n\n<i>— {e(how)}</i>", link_preview_options=NO_PREVIEW,
                                  reply_markup=kb([[("🔁 Другой вариант", f"l:{row['id']}")]]))

    # ───────────── свободный ввод: ключ, резюме, правки ─────────────

    @r.message(F.document)
    async def document(m: Message):
        if not (u := await access(m)):
            return
        uid = m.from_user.id
        if u["step"] != "resume" and not await app.in_mode(uid, "cv"):
            await m.answer("Чтобы обновить резюме, отправь /cv и затем файл.")
            return
        if (m.document.file_size or 0) > resume.MAX_FILE:
            await m.answer("Файл больше 5 МБ.")
            return
        await app.set_mode(uid, "")
        buf = await app.bot.download(m.document)
        async with app.lock(uid):
            await app.process_resume(uid, m.document.file_name or "resume", buf.read(), None)

    async def gather(m: Message, u, kind: str, t: str):
        """Части одного длинного текста приходят отдельными сообщениями подряд — собираем их и обрабатываем вместе."""
        uid = u["tg_id"]
        b = app.pending.get(uid)
        if b and b["kind"] == kind:
            b["parts"].append(t)
            b["task"].cancel()
        else:
            b = app.pending[uid] = {"kind": kind, "parts": [t]}
        b["m"] = m
        b["task"] = asyncio.create_task(flush(uid, b))

    async def flush(uid: int, b: dict):
        await _sleep(PASTE_GAP)
        if app.pending.get(uid) is b:
            del app.pending[uid]
        t, m = "\n".join(b["parts"]), b["m"]
        u = await app.store.user(uid)
        if not u:
            return
        try:
            if b["kind"] == "cv":
                if len(t) < 300:
                    await m.answer("Слишком коротко для резюме — пришли файл или полный текст.")
                    return
                await app.set_mode(uid, "")
                async with app.lock(uid):
                    await app.process_resume(uid, "resume.txt", None, t)
            else:
                if len(t) < VACANCY_MIN:
                    await m.answer("Это слишком коротко для вакансии. " + VACANCY_HELP)
                    return
                await app.set_mode(uid, "")
                await app.store.kv_set(f"vac_text:{uid}", t[:VACANCY_MAX])
                await send_letter(m, u, t[:VACANCY_MAX])
        except Exception:
            log.exception("text from %s failed", uid)

    @r.message(F.text & ~F.text.startswith("/"))
    async def text(m: Message):
        if not (u := await access(m)):
            return
        uid, t = m.from_user.id, m.text.strip()
        if u["step"] == "key" or await app.store.kv_get(f"await_key:{uid}") == "1":
            try:
                await m.delete()                       # ключ не должен оставаться в переписке
            except Exception:
                pass
            try:
                await app.make_giga(t).token()
            except Exception as ex:
                await app.say(uid, f"⚠️ GigaChat не принял ключ: <code>{e(str(ex)[:150])}</code>\nПроверь и пришли ещё раз.")
                return
            await app.store.update_user(uid, gigachat_key=app.vault.encrypt(t),
                                        step="resume" if u["step"] == "key" else u["step"])
            await app.store.kv_set(f"await_key:{uid}", "0")
            await app.say(uid, "✅ Ключ принят и сохранён в зашифрованном виде.")
            if u["step"] == "key":
                await app.say(uid, RESUME_HELP)
            return
        if (b := app.pending.get(uid)) and not b["task"].done():
            await gather(m, u, b["kind"], t)              # продолжение того же длинного текста
            return
        edit = await app.store.kv_get(f"edit:{uid}")
        if edit:
            await app.store.kv_set(f"edit:{uid}", "")
            if edit == "city":
                await app.edit_profile(uid, city=t[:60])
            elif edit == "roles":
                roles = [x.strip().lower()[:40] for x in t.replace(";", ",").split(",") if x.strip()][:8]
                if not roles:
                    await m.answer("Не понял роли — напиши через запятую.")
                    return
                await app.edit_profile(uid, roles=roles)
            elif edit == "salary":
                digits = "".join(ch for ch in t if ch.isdigit())
                await app.edit_profile(uid, salary_min=int(digits) if digits and int(digits) >= 10000 else None)
            await app.show_profile(uid, final=u["step"] == "review")
            return
        if u["step"] == "resume" or await app.in_mode(uid, "cv"):
            await gather(m, u, "cv", t)
            return
        if u["step"] != "active":
            await next_step(uid)
            return
        # текст вакансии: после /letter или просто длинное сообщение
        if await app.in_mode(uid, "vac") or len(t) >= 300:
            await gather(m, u, "vac", t)
            return
        await m.answer("Чтобы написать сопроводительное, отправь /letter и затем текст вакансии.")

    return r


async def main():
    env = config.env()
    cfg = config.load(env.config_path)
    env.data_dir.mkdir(parents=True, exist_ok=True)
    store = await Store.open(env.data_dir / "job-watch.db", owner_id=env.owner_id)
    http = aiohttp.ClientSession()
    bot = Bot(env.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    app = App(env, cfg, store, http, bot, Vault(env.secret))
    await app.seed_owner()
    dp = Dispatcher()
    dp.include_router(build_router(app))
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in (
        ("letter", "письмо по тексту вакансии"), ("profile", "профиль и фильтры"), ("saved", "сохранённые"),
        ("status", "статистика"), ("pause", "пауза"), ("continue", "продолжить"), ("cv", "новое резюме"), ("key", "сменить ключ GigaChat"),
        ("delete", "удалить мои данные"))])
    enabled = [n for n in sources.SOURCES if cfg.source(n).get("enabled")]
    log.info("job-watch started: sources=%s, owner=%s, users=%s", ",".join(enabled), env.owner_id,
             len(await store.users("active")))
    task = asyncio.create_task(app.scheduler())
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await http.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
