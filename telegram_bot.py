#!/usr/bin/env python3
"""
Study Buddy - Telegram bot for Bennett LMS (Moodle) + MyCamu.

It is a *client* of the moodle-mcp server running on the same machine: every bit of data
comes from the same MCP tools the connector serves (http://127.0.0.1:<port>/<MCP_SECRET>/mcp). The bot
runs as its own systemd service (study-bot), so a bot crash never takes the MCP server down;
if the MCP server is down, the bot says so and alerts you once.

Features
  Commands   /today /tomorrow /week /deadlines /attendance /grades /exams /news /notices
             /status /alerts /help
  Free text  answered by Groq's free tier (tool calling over the MCP tools); if Groq is not
             configured or its free limit is hit, a built-in keyword router answers instead.
  Pushes     morning digest (07:45 IST), deadline pings 24h and 3h before, new LMS
             notifications (every 30 min), new course items (every 3 h), MyCamu cookie expiry.

Config (.env next to this file - same file the MCP server uses)
  TELEGRAM_BOT_TOKEN   from @BotFather                    (required; set with set-bot-secrets)
  OWNER_CHAT_ID        your Telegram chat id              (required; set with set-bot-secrets)
  MCP_SECRET           already there (deploy.sh made it)
  GROQ_API_KEY         optional, free at console.groq.com/keys
  GROQ_MODEL           default openai/gpt-oss-120b  (fallback GROQ_FALLBACK_MODEL=openai/gpt-oss-20b)
  DIGEST_TIME          default 07:45      AI_DAILY_LIMIT  default 40      BOT_TZ default Asia/Kolkata
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import datesheet

HERE = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- config


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    p = HERE / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():  # real environment wins
        if k.isupper():
            env[k] = v
    return env


ENV = _load_env()
BOT_TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = int(ENV["OWNER_CHAT_ID"]) if ENV.get("OWNER_CHAT_ID", "").lstrip("-").isdigit() else 0
MCP_URL = f"http://127.0.0.1:{ENV.get('MCP_PORT', '8765')}/{ENV.get('MCP_SECRET', '')}/mcp"
MOODLE_URL = ENV.get("MOODLE_URL", "https://lms.bennett.edu.in").rstrip("/")
GROQ_KEY = ENV.get("GROQ_API_KEY", "")
GROQ_MODELS = [m for m in (ENV.get("GROQ_MODEL", "openai/gpt-oss-120b"),
                           ENV.get("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")) if m]
AI_DAILY_LIMIT = int(ENV.get("AI_DAILY_LIMIT", "40"))
TZ = ZoneInfo(ENV.get("BOT_TZ", "Asia/Kolkata"))

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
for noisy in ("httpx", "httpcore", "apscheduler", "telegram.ext.Application"):
    logging.getLogger(noisy).setLevel(logging.WARNING)  # httpx logs URLs containing the bot token
log = logging.getLogger("study-bot")

# --------------------------------------------------------------------------- MCP client


class MCPDown(Exception):
    """The local MCP server isn't answering."""


class ToolError(Exception):
    """A tool answered with an error message (e.g. LMS down, MyCamu cookie expired)."""


class MCP:
    """Minimal JSON-RPC client for the stateless streamable-HTTP MCP server."""

    HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def __init__(self, url: str):
        self.url = url
        self._n = 0
        self._tools: list[dict] = []
        self._tools_at = 0.0

    async def _rpc(self, method: str, params: dict) -> dict:
        self._n += 1
        try:
            async with httpx.AsyncClient(timeout=120, trust_env=False) as c:
                r = await c.post(self.url, headers=self.HEADERS,
                                 json={"jsonrpc": "2.0", "id": self._n, "method": method, "params": params})
            r.raise_for_status()
            d = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise MCPDown(f"{type(e).__name__}") from e
        if "error" in d:
            raise MCPDown(str(d["error"].get("message", d["error"])))
        return d.get("result", {})

    async def call(self, name: str, **args: Any) -> str:
        res = await self._rpc("tools/call", {"name": name, "arguments": args})
        return "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text").strip()

    async def call_json(self, name: str, **args: Any) -> Any:
        text = await self.call(name, response_format="json", **args)
        if text.startswith("Error"):
            raise ToolError(text)
        try:
            return json.loads(text)
        except ValueError as e:
            raise ToolError(f"Unexpected reply from {name}") from e

    async def tools(self) -> list[dict]:
        if not self._tools or time.time() - self._tools_at > 3600:
            self._tools = (await self._rpc("tools/list", {})).get("tools", [])
            self._tools_at = time.time()
        return self._tools


mcp = MCP(MCP_URL)

# --------------------------------------------------------------------------- storage
# One SQLite file shared with the dashboard (study.db, WAL). The bot owns the bot_* tables; `settings`,
# `exams` and `manual_entries` are shared - the dashboard writes settings, the bot re-reads them on
# every scheduler tick. Keep the shared DDL in sync with dashboard/common.py.

SHARED_DDL = """
CREATE TABLE IF NOT EXISTS bot_users (chat_id INTEGER PRIMARY KEY, name TEXT, role TEXT, added INTEGER);
CREATE TABLE IF NOT EXISTS bot_sent (key TEXT PRIMARY KEY, at INTEGER);
CREATE TABLE IF NOT EXISTS bot_kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS bot_ai_usage (chat_id INTEGER, day TEXT, n INTEGER, PRIMARY KEY (chat_id, day));
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS exams (
  id INTEGER PRIMARY KEY AUTOINCREMENT, course_code TEXT, title TEXT, starts_at INTEGER, ends_at INTEGER,
  venue TEXT, kind TEXT, source TEXT, time_known INTEGER DEFAULT 1, confirmed INTEGER DEFAULT 1,
  created_at INTEGER, UNIQUE(course_code, starts_at)
);
CREATE TABLE IF NOT EXISTS manual_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, course_code TEXT, due_at INTEGER,
  kind TEXT, remind INTEGER DEFAULT 1, created_at INTEGER
);
"""
SETTING_DEFAULTS = {"alerts_paused": "0", "digest_time": ENV.get("DIGEST_TIME", "07:45"),
                    "alert_hours": "24,3", "muted_courses": ""}


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=5, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SHARED_DDL)
        self._migrate_bot_db(path.with_name("bot.db"))

    def _migrate_bot_db(self, old: Path) -> None:
        """One-time copy of the pre-merge bot.db into study.db; the old file is kept as bot.db.bak
        for a week, then removed."""
        bak = old.with_name("bot.db.bak")
        if bak.exists() and time.time() - bak.stat().st_mtime > 7 * 86400:
            bak.unlink(missing_ok=True)
        if not old.exists() or self.get("migrated_bot_db") == "1":
            return
        self.db.execute("ATTACH DATABASE ? AS old", (str(old),))
        try:
            self.db.execute("BEGIN IMMEDIATE")
            tables = {r[0] for r in self.db.execute("SELECT name FROM old.sqlite_master WHERE type='table'")}
            for t in ("users", "sent", "kv", "ai_usage"):
                if t in tables:
                    self.db.execute(f"INSERT OR IGNORE INTO bot_{t} SELECT * FROM old.{t}")
            if "kv" in tables:  # alerts on/off now lives in the shared settings table
                row = self.db.execute("SELECT v FROM old.kv WHERE k='alerts_paused'").fetchone()
                if row:
                    self.db.execute("INSERT OR IGNORE INTO settings(k,v,updated_at) VALUES('alerts_paused',?,?)",
                                    (row[0], int(time.time())))
            self.db.execute("INSERT OR REPLACE INTO bot_kv VALUES('migrated_bot_db','1')")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        finally:
            self.db.execute("DETACH DATABASE old")
        old.rename(bak)
        os.utime(bak)  # the week starts now
        log.info("migrated bot.db -> %s (old file kept as bot.db.bak)", self.path.name)

    def get(self, k: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT v FROM bot_kv WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def put(self, k: str, v: str) -> None:
        self.db.execute("INSERT INTO bot_kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def setting(self, k: str) -> str:
        row = self.db.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        return row[0] if row and row[0] is not None else SETTING_DEFAULTS.get(k, "")

    def set_setting(self, k: str, v: str) -> None:
        self.db.execute("INSERT INTO settings(k,v,updated_at) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET "
                        "v=excluded.v, updated_at=excluded.updated_at", (k, v, int(time.time())))

    def was_sent(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM bot_sent WHERE key=?", (key,)).fetchone() is not None

    def mark_sent(self, key: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO bot_sent(key, at) VALUES(?, ?)", (key, int(time.time())))

    def prune(self, days: int = 45) -> None:
        self.db.execute("DELETE FROM bot_sent WHERE at < ?", (int(time.time()) - days * 86400,))

    def ai_used(self, chat_id: int) -> int:
        row = self.db.execute("SELECT n FROM bot_ai_usage WHERE chat_id=? AND day=?",
                              (chat_id, _now().strftime("%Y-%m-%d"))).fetchone()
        return row[0] if row else 0

    def ai_bump(self, chat_id: int) -> None:
        self.db.execute("INSERT INTO bot_ai_usage(chat_id, day, n) VALUES(?, ?, 1) "
                        "ON CONFLICT(chat_id, day) DO UPDATE SET n = n + 1",
                        (chat_id, _now().strftime("%Y-%m-%d")))

    def ensure_owner(self, chat_id: int) -> None:
        self.db.execute("INSERT OR IGNORE INTO bot_users(chat_id, name, role, added) VALUES(?, '', 'owner', ?)",
                        (chat_id, int(time.time())))

    def rows(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        cur = self.db.execute(sql, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


store = Store(Path(ENV.get("DASH_DB", str(HERE / "study.db"))))

# --------------------------------------------------------------------------- time + text helpers


def _now() -> datetime:
    return datetime.now(TZ)


def fmt_due(ts: int) -> str:
    d = datetime.fromtimestamp(int(ts), TZ)
    if d.hour == 0 and d.minute == 0:  # "Fri 12:00 AM" really means end of Thursday
        return (d - timedelta(minutes=1)).strftime("%a %d %b") + ", midnight"
    return d.strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def rel(ts: int) -> str:
    s = int(ts) - int(time.time())
    a = abs(s)
    if a < 3600:
        span = f"{max(1, a // 60)}m"
    elif a < 86400:
        span = f"{a // 3600}h"
    else:
        h = a % 86400 // 3600
        span = f"{a // 86400}d" + (f" {h}h" if h else "")
    return f"in {span}" if s >= 0 else f"{span} ago"


def short_course(name: str | None) -> str:
    n = name or ""
    n = re.sub(r"\s*\((?:ODD|EVEN|Odd|Even)[^)]*\)\s*$", "", n)          # "(ODD Semester 2026-27)"
    n = re.sub(r"^(?:[A-Z]+\d*\s*[,-]\s*)+(?=[A-Z]{3,}\d{3})", "", n)   # "EB91, EB93 - " section prefixes
    n = re.sub(r"^\d+\w*\s*-\s*", "", n)                                   # "3rd - "
    return n.replace(": ", " ").strip().rstrip(":")


def esc(s: Any) -> str:
    return html.escape(str(s), quote=False)


_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITAL = re.compile(r"(?<![\w*])_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)")
_STAR = re.compile(r"(?<![\w*])\*(?![\s*])([^*\n]+?)(?<![\s*])\*(?![\w*])")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def _inline(s: str) -> str:
    codes: list[str] = []

    def keep(m: re.Match) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    s = _CODE.sub(keep, s)
    s = esc(s)
    s = _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
    s = _BOLD.sub(r"<b>\1</b>", s)
    s = _ITAL.sub(r"<i>\1</i>", s)
    s = _STAR.sub(r"<b>\1</b>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{esc(codes[int(m.group(1))])}</code>", s)


def _is_sep(line: str) -> bool:
    t = line.strip()
    return bool(t) and "-" in t and set(t) <= set("|-: ")


def md_to_html(md: str) -> str:
    """Markdown from the MCP tools / the AI -> Telegram HTML (Telegram has no tables)."""
    out: list[str] = []
    lines = md.splitlines()
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        if _is_sep(line):
            continue
        if line.startswith("|") and line.endswith("|") and len(line) > 2:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if i + 1 < len(lines) and _is_sep(lines[i + 1]):  # header row
                out.append("<i>" + esc(" · ".join(cells)) + "</i>")
            else:
                out.append("• " + " · ".join(_inline(c) for c in cells if c))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            out.append(f"<b>{_inline(m.group(2))}</b>")
            continue
        m = re.match(r"^(\s*)[-*]\s+(.*)", line)
        if m:
            out.append(("   " if m.group(1) else "") + "• " + _inline(m.group(2)))
            continue
        if line.startswith(">"):
            out.append("<i>" + _inline(line.lstrip("> ")) + "</i>")
            continue
        out.append(_inline(line))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def chunks(text: str, limit: int = 3800) -> list[str]:
    """Split on line boundaries (tags never span lines in our HTML)."""
    parts: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:  # pathological single line
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts or [""]


async def send(bot: Any, chat_id: int, html_text: str) -> None:
    for part in chunks(html_text):
        try:
            await bot.send_message(chat_id, part, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except BadRequest:  # malformed HTML: fall back to plain text
            plain = html.unescape(re.sub(r"<[^>]+>", "", part))
            await bot.send_message(chat_id, plain, disable_web_page_preview=True)


# --------------------------------------------------------------------------- renderers


def _table_rows(md: str) -> list[list[str]]:
    rows = []
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("|") and not _is_sep(line) and not (i + 1 < len(lines) and _is_sep(lines[i + 1])):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows


def render_attendance(md: str, only_low: bool = False) -> str:
    if md.startswith("Error") or "|" not in md:
        return md_to_html(md)
    title = md.splitlines()[0].lstrip("# ").strip()
    overall = next((l for l in md.splitlines() if l.startswith("**Overall")), "")
    out = [f"📊 <b>{esc(title)}</b>"]
    if overall:
        out.append(_inline(overall))
    out.append("")
    rows = _table_rows(md)
    low = 0
    for r in rows:
        if len(r) < 7:
            continue
        subj, code, _typ, present, total, pct, note = r[:7]
        bad = note.startswith("⚠")
        low += bad
        if only_low and not bad:
            continue
        icon = "⚠️" if bad else ("✅" if note.startswith("✅") else "▫️")
        note = note.lstrip("⚠️✅ ").strip()
        out.append(f"{icon} <b>{esc(subj)}</b> ({esc(code)}) - {esc(pct)}% ({esc(present)}/{esc(total)}) · {esc(note)}")
    if only_low and not low:
        return ""
    if not rows:
        return md_to_html(md)
    return "\n".join(out)


def render_timetable(md: str) -> str:
    if not md.startswith("#"):
        return "🗓 " + md_to_html(md)
    title = md.splitlines()[0].lstrip("# ").strip()
    rows = _table_rows(md)
    if not rows:
        return "🗓 " + md_to_html(md)
    out = [f"🗓 <b>{esc(title)}</b>"]
    day = None
    for r in rows:
        if len(r) < 6:
            continue
        d, t, subj, code, fac, typ = r[:6]
        if d != day:
            day = d
            out.append(f"\n<b>{esc(d)}</b>")
        extra = " · ".join(x for x in (esc(typ), esc(fac)) if x)
        out.append(f"{esc(t)}  {esc(subj)}" + (f" <i>({esc(code)})</i>" if code else "") + (f"\n     {extra}" if extra else ""))
    return "\n".join(out)


def render_deadlines(items: list[dict], title: str) -> str:
    if not items:
        return f"✅ <b>{esc(title)}</b>\nNothing pending. 🎉"
    out = [f"📌 <b>{esc(title)}</b>", ""]
    for e in items:
        due = e.get("due") or 0
        flag = "⚠️ OVERDUE · " if e.get("overdue") else ""
        kind = {"assign": "📝", "quiz": "❓", "forum": "💬"}.get(e.get("type", ""), "•")
        name = e.get("activity") or e.get("name")
        link = f' · <a href="{html.escape(e["url"])}">open</a>' if e.get("url") else ""
        out.append(f"{kind} <b>{esc(name)}</b> - {esc(short_course(e.get('course')))}")
        out.append(f"     {flag}{esc(fmt_due(due))} ({esc(rel(due))})"
                   + (f" · {esc(e['action'])}" if e.get("action") else "") + link)
    return "\n".join(out)


def render_notifications(notes: list[dict], title: str = "Latest LMS notifications") -> str:
    if not notes:
        return "No LMS notifications."
    out = [f"🔔 <b>{esc(title)}</b>", ""]
    for n in notes:
        out.append(_notif_line(n, body=False))
    return "\n".join(out)


def _post_body(n: dict) -> str:
    full = n.get("fullmessage") or ""
    parts = re.split(r"\n-{10,}\n", full)
    body = parts[1] if len(parts) >= 3 else ""
    body = re.sub(r"\n{2,}", "\n", body).strip()
    return body[:600] + ("…" if len(body) > 600 else "")


def _notif_line(n: dict, body: bool) -> str:
    comp = n.get("component", "")
    icon = {"mod_forum": "📢", "mod_assign": "📝", "mod_quiz": "❓", "moodle": "🎓"}.get(comp, "🔔")
    subj = n.get("contexturlname") or n.get("subject") or "Notification"
    course = short_course((n.get("subject") or "").split("):")[0] + ")") if "):" in (n.get("subject") or "") else ""
    when = datetime.fromtimestamp(int(n.get("timecreated") or 0), TZ).strftime("%d %b, %I:%M %p")
    line = f"{icon} <b>{esc(subj)}</b>" + (f" - {esc(course)}" if course else "") + f"\n     <i>{esc(when)}</i>"
    if n.get("contexturl"):
        line += f' · <a href="{html.escape(n["contexturl"])}">open</a>'
    if body and comp == "mod_forum":
        b = _post_body(n)
        if b:
            line += "\n" + esc(b)
    elif body:
        sm = n.get("smallmessage") or ""
        if sm and sm != subj:
            line += "\n" + esc(sm[:300])
    return line


# --------------------------------------------------------------------------- data fetchers


async def get_deadlines(days: int, overdue_days: int = 0) -> list[dict]:
    items = await mcp.call_json("moodle_get_upcoming_deadlines", days_ahead=days,
                                include_overdue_days=overdue_days, limit=50)
    return sorted(items, key=lambda e: e.get("due") or 0)


async def timetable_md(start: datetime, days_ahead: int) -> str:
    return await mcp.call("mycamu_get_timetable", days_ahead=days_ahead, start_date=start.strftime("%Y-%m-%d"))


def friendly_error(e: Exception) -> str:
    if isinstance(e, MCPDown):
        return ("⚠️ The LMS server on your VPS isn't answering. It restarts itself automatically; "
                "if this keeps happening run <code>sudo systemctl status moodle-mcp</code> on the server.")
    if isinstance(e, ToolError):
        return "⚠️ " + md_to_html(str(e))
    log.exception("unexpected error", exc_info=e)
    return f"⚠️ Something went wrong ({esc(type(e).__name__)})."


# --------------------------------------------------------------------------- access control


def is_owner(update: Update) -> bool:
    return bool(update.effective_chat) and update.effective_chat.id == OWNER_ID


async def guard(update: Update) -> bool:
    if is_owner(update):
        return True
    chat = update.effective_chat
    if chat and update.effective_message:
        await update.effective_message.reply_text(
            f"This is a private Study Buddy bot. Your chat ID is {chat.id}.")
        log.info("ignored message from non-owner chat %s", chat.id)
    return False


# --------------------------------------------------------------------------- commands

HELP = (
    "🎓 <b>Study Buddy</b>\n\n"
    "/today - today's classes + what's due in 48h\n"
    "/tomorrow - tomorrow's classes\n"
    "/week - timetable for the next 7 days\n"
    "/deadlines - everything due in 14 days (+ overdue)\n"
    "/attendance - attendance with the 75% check\n"
    "/grades - course totals\n"
    "/exams - your saved exams + MyCamu's schedule\n"
    "/news - latest LMS notifications\n"
    "/notices - MyCamu announcements (7 days)\n"
    "/status - server, MyCamu and AI status\n"
    "/alerts on|off - pause or resume automatic messages\n\n"
    "📄 <b>Forward the CoE date sheet</b> (PDF or text) and I'll pick out your exams and remind you.\n\n"
    "Or just ask: <i>“can I skip IoT tomorrow?”</i>, <i>“automata midsem syllabus”</i>, "
    "<i>“IoT lecture 5 slides”</i>"
)


async def run_and_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE, coro: Any) -> None:
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        text = await coro
    except Exception as e:  # noqa: BLE001
        text = friendly_error(e)
    await send(ctx.bot, update.effective_chat.id, text)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await send(ctx.bot, update.effective_chat.id, HELP)


async def _today_text(offset: int = 0, with_due: bool = True) -> str:
    day = _now() + timedelta(days=offset)
    parts = [render_timetable(await timetable_md(day, 0))]
    if with_due:
        try:
            soon = [e for e in await get_deadlines(2) if not e.get("overdue")]
            parts.append(render_deadlines(soon, "Due in the next 48 hours"))
        except ToolError as e:
            parts.append(friendly_error(e))
    return "\n\n".join(parts)


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard(update):
        await run_and_reply(update, ctx, _today_text(0))


async def cmd_tomorrow(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard(update):
        await run_and_reply(update, ctx, _today_text(1, with_due=False))


async def _week_text() -> str:
    return render_timetable(await timetable_md(_now(), 6))


async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard(update):
        await run_and_reply(update, ctx, _week_text())


async def _deadlines_text() -> str:
    return render_deadlines(await get_deadlines(14, 7), "Due in the next 14 days")


async def cmd_deadlines(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard(update):
        await run_and_reply(update, ctx, _deadlines_text())


async def _attendance_text() -> str:
    return render_attendance(await mcp.call("mycamu_get_attendance"))


async def cmd_attendance(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard(update):
        await run_and_reply(update, ctx, _attendance_text())


async def _tool_text(name: str, icon: str = "", **args: Any) -> str:
    return (icon + " " if icon else "") + md_to_html(await mcp.call(name, **args))


async def cmd_grades(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard(update):
        await run_and_reply(update, ctx, _tool_text("moodle_get_grades", "🏅"))


async def _exams_text() -> str:
    saved = upcoming_exams(120)
    head = render_exams(saved, "Your exams") if saved else (
        "📝 No saved exams yet - forward the CoE date sheet (PDF or text) to me.")
    return head + "\n\n" + await _tool_text("mycamu_get_exam_schedule", "🧾")


async def cmd_exams(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard(update):
        await run_and_reply(update, ctx, _exams_text())


async def cmd_notices(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard(update):
        await run_and_reply(update, ctx, _tool_text("mycamu_get_announcements", "📣", days_back=7))


async def _news_text() -> str:
    return render_notifications(await mcp.call_json("moodle_get_notifications", limit=10))


async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard(update):
        await run_and_reply(update, ctx, _news_text())


async def _status_text(chat_id: int) -> str:
    lines = ["🩺 <b>Status</b>", ""]
    try:
        who = await mcp.call("moodle_whoami")
        lines.append(("✅ " if who.startswith("Connected") else "⚠️ ") + "LMS: " + _inline(who.splitlines()[0]))
        camu = await mcp.call("mycamu_status")
        lines.append(("✅ " if "active" in camu else "⚠️ ") + "MyCamu: " + esc(camu.splitlines()[0]))
    except MCPDown:
        lines.append("❌ MCP server on the VPS isn't answering.")
    if GROQ_KEY:
        lines.append(f"🤖 AI: Groq free tier ({esc(GROQ_MODELS[0])}) · used today {store.ai_used(chat_id)}/{AI_DAILY_LIMIT}")
    else:
        lines.append("🤖 AI: off (no GROQ_API_KEY) - keyword answers only")
    paused = store.setting("alerts_paused") == "1"
    hrs = " + ".join(f"{h}h" for h in alert_hours())
    lines.append(f"🔔 Alerts: {'paused' if paused else 'on'} · digest at {store.setting('digest_time')} · "
                 f"deadline pings {hrs or 'off'}")
    muted = muted_courses()
    if muted:
        lines.append(f"🔇 Muted: {', '.join(sorted(muted))}")
    lines.append("<i>Change these on the dashboard (⋯ → Bot settings).</i>")
    return "\n".join(lines)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard(update):
        await run_and_reply(update, ctx, _status_text(update.effective_chat.id))


async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    arg = (ctx.args[0].lower() if ctx.args else "")
    if arg in ("off", "pause", "stop"):
        store.set_setting("alerts_paused", "1")
        msg = "🔕 Automatic messages paused. /alerts on to resume."
    elif arg in ("on", "resume", "start"):
        store.set_setting("alerts_paused", "0")
        msg = "🔔 Automatic messages are on."
    else:
        msg = "Alerts are " + ("paused" if store.setting("alerts_paused") == "1" else "on") + ". Use /alerts on or /alerts off."
    await update.effective_message.reply_text(msg)


# --------------------------------------------------------------------------- free text: keyword router

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _day_offset(t: str) -> int | None:
    if "day after tomorrow" in t:
        return 2
    if re.search(r"\btomorrow\b|\btmrw\b|\btmr\b", t):
        return 1
    if re.search(r"\btoday\b|\btonight\b|\bnow\b", t):
        return 0
    for i, d in enumerate(WEEKDAYS):
        if re.search(rf"\b{d}\b|\b{d[:3]}\b", t):
            return (i - _now().weekday()) % 7
    return None


# --------------------------------------------------------------------------- course materials (files, slides, syllabus)

_FILE = re.compile(r"^\s+- 📄 (.+?) \((\d+) KB\) - `([^`]+)`")
_course_cache: list[Any] = [0.0, []]
_contents_cache: dict[int, tuple[float, list[dict]]] = {}
_STOP = {"give", "what", "whats", "show", "send", "with", "from", "about", "this", "that", "course",
         "subject", "syllabus", "notes", "slides", "slide", "lecture", "lectures", "file", "files", "please",
         "the", "and", "for", "mid", "endsem", "exam", "exams", "test", "pdf", "ppt", "material",
         "materials", "tutorial", "tutorials", "solution", "solutions", "where", "when", "find", "need",
         "want", "have", "does", "will", "sem", "semester", "upload", "uploaded", "latest", "new", "share"}


_ALIASES = {"ai": "artificial", "ml": "machine", "aiml": "artificial", "toc": "automata", "atc": "automata",
            "flat": "automata", "cp": "competitive", "pe": "prompt", "psee": "employability", "c": "solving"}


def _norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\bmid[\s-]?(?:sem(?:ester)?|terms?)\b|\bmst\b|\bmte\b|\bmidsem\b|\bmidterm\b", " mid ", s)
    s = re.sub(r"\bend[\s-]?(?:sem(?:ester)?|terms?)\b|\bete\b", " endsem ", s)
    return s


async def all_courses() -> list[dict]:
    if not _course_cache[1] or time.time() - _course_cache[0] > 3600:
        _course_cache[1] = await mcp.call_json("moodle_list_courses")
        _course_cache[0] = time.time()
    return _course_cache[1]


def match_courses(query: str, courses: list[dict]) -> list[dict]:
    """Courses whose name/code matches any meaningful word in the query (typo-tolerant)."""
    import difflib
    raw = re.findall(r"[a-z0-9]+", _norm(query))
    words = [_ALIASES.get(w, w) for w in raw if w not in _STOP and (len(w) >= 3 or w in _ALIASES)]
    hits = []
    for c in courses:
        name = (c.get("fullname") or "").lower()
        cwords = re.findall(r"[a-z0-9]+", name)
        if any(w in cwords or (len(w) >= 5 and w in name)
               or (len(w) >= 5 and difflib.get_close_matches(w, cwords, n=1, cutoff=0.8))
               for w in words):
            hits.append(c)
    return hits


async def course_items(cid: int) -> list[dict]:
    hit = _contents_cache.get(cid)
    if hit and time.time() - hit[0] < 900:
        return hit[1]
    md = await mcp.call("moodle_get_course_contents", course_id=cid, include_descriptions=False)
    if md.startswith("Error"):
        raise ToolError(md)
    items: list[dict] = []
    section = ""
    cur: dict | None = None
    for line in md.splitlines():
        if line.startswith("## "):
            section = html.unescape(line[3:].strip())
            continue
        m = _ITEM.match(line)
        if m:
            cur = {"mod": m.group(1), "name": html.unescape(m.group(2)), "cmid": int(m.group(3)),
                   "section": section, "files": []}
            items.append(cur)
            continue
        f = _FILE.match(line)
        if f and cur is not None:
            cur["files"].append({"name": f.group(1), "kb": int(f.group(2)), "url": f.group(3)})
    _contents_cache[cid] = (time.time(), items)
    return items


async def search_materials(course: str, keywords: str, limit: int = 8) -> tuple[list[dict], list[tuple[dict, dict, int]]]:
    courses = await all_courses()
    picked = match_courses(course, courses) if course.strip() else courses
    toks = [w for w in re.findall(r"[a-z0-9]+", _norm(keywords)) if len(w) >= 3 or w.isdigit()]
    toks = [w for w in toks if w not in {"the", "and", "for", "give", "what", "show", "send", "please", "of"}]
    scored: list[tuple[dict, dict, int]] = []
    for c in picked:
        try:
            items = await course_items(int(c["id"]))
        except ToolError:
            continue
        cname = set(re.findall(r"[a-z0-9]+", (c.get("fullname") or "").lower()))
        ctoks = [w for w in toks if w not in cname]  # "cset302" / "automata" shouldn't score items
        for it in items:
            if it["mod"] == "forum":
                continue
            hay = _norm(" ".join([it["name"], it["section"], *[f["name"] for f in it["files"]]]))
            score = sum(1 for w in ctoks if re.search(rf"\b{re.escape(w)}", hay))
            scored.append((c, it, score))
    scored.sort(key=lambda x: (x[2], x[1]["cmid"]), reverse=True)
    if scored and scored[0][2] > 0:
        scored = [s for s in scored if s[2] == scored[0][2]] + [s for s in scored if 0 < s[2] < scored[0][2]]
    return picked, scored[:limit]


def _item_link(it: dict) -> str:
    return f"{MOODLE_URL}/mod/{it['mod']}/view.php?id={it['cmid']}"


async def materials_tool(course: str = "", keywords: str = "") -> str:
    picked, hits = await search_materials(course, keywords or course)
    if course.strip() and not picked:
        names = ", ".join(sorted({short_course(c.get("fullname")) for c in await all_courses()}))
        return f"No course matches '{course}'. Current courses: {names}"
    if not hits:
        return "No files found in that course."
    lines = []
    for c, it, score in hits:
        lines.append(f"- {short_course(c.get('fullname'))} · {it['name']} ({it['section']}) [match {score}]")
        for f in it["files"][:3]:
            lines.append(f"  file: {f['name']} ({f['kb']} KB) url: {f['url']}")
    return "\n".join(lines)


async def materials_answer(text: str) -> str:
    """Keyword-mode answer for 'automata midsem syllabus', 'iot lecture 5 slides', etc."""
    picked, hits = await search_materials(text, text, limit=6)
    if not picked:  # no course named: look across every course, e.g. "midsem syllabus"
        _, every = await search_materials("", text, limit=8)
        every = [h for h in every if h[2] > 0]
        if every:
            out = ["📚 <b>Matching files across your courses</b>"]
            for oc, oi, _ in every:
                out.append(f'• <a href="{html.escape(_item_link(oi))}">{esc(oi["name"])}</a> - '
                           f'{esc(short_course(oc.get("fullname")))}')
            out.append("\nName the course to see a file's contents, e.g. <i>automata midsem syllabus</i>.")
            return "\n".join(out)
        return ("Which course? Try e.g. <i>automata midsem syllabus</i> or <i>IoT lecture 5</i>.\n"
                "Courses: " + esc(", ".join(sorted({short_course(c.get('fullname')) for c in await all_courses()}))))
    if not hits:
        return "I couldn't find matching files in " + esc(short_course(picked[0].get("fullname"))) + "."
    c, it, score = hits[0]
    out = [f"📚 <b>{esc(it['name'])}</b> - {esc(short_course(c.get('fullname')))}",
           f'<i>{esc(it["section"])}</i> · <a href="{html.escape(_item_link(it))}">open on LMS</a>']
    readable = [f for f in it["files"] if re.search(r"\.(pdf|docx|pptx|txt)$", f["name"], re.I) and f["kb"] < 15000]
    if score > 0 and readable and re.search(r"syllabus|btlap|plan|notice|announcement", text, re.I):
        body = await mcp.call("moodle_read_file", file_url=readable[0]["url"], max_chars=3000)
        if not body.startswith("Error"):
            body = re.sub(r"^# .*\n|--- (page|slide) \d+ ---\n?", "", body, flags=re.M).strip()
            out.append("\n" + esc(body[:3000]))
    others = hits[1:5] if score > 0 else hits[:5]
    if others:
        out.append("\n<b>Also in the course</b>" if score > 0 else "\n<b>Latest items</b>")
        for oc, oi, _ in others:
            out.append(f'• <a href="{html.escape(_item_link(oi))}">{esc(oi["name"])}</a> <i>({esc(oi["section"])})</i>')
    return "\n".join(out)


MATERIALS_RE = re.compile(r"syllabus|slides?\b|notes\b|\bppt|\bpdf\b|tutorial|lecture[\s-]?\d|material|btlap|\bfiles?\b")


async def keyword_answer(text: str) -> str:
    t = text.lower()
    off = _day_offset(t)
    if MATERIALS_RE.search(t):
        return await materials_answer(text)
    if re.search(r"attend|bunk|skip|miss(ed)? class|\b75\b|debar|short(age)?\b", t):
        return await _attendance_text()
    if re.search(r"exam|date ?sheet|mid ?sem|end ?sem|seating", t):
        return await _tool_text("mycamu_get_exam_schedule", "🧾")
    if re.search(r"time ?table|class|lecture|schedule|period", t):
        if re.search(r"week", t) and off is None:
            return await _week_text()
        return render_timetable(await timetable_md(_now() + timedelta(days=off or 0), 0))
    if re.search(r"quiz|\btest\b", t):
        return await _tool_text("moodle_list_quizzes", "❓", upcoming_only=True, include_attempts=False)
    if re.search(r"grade|marks|score|result|cgpa|sgpa", t):
        return await _tool_text("moodle_get_grades", "🏅")
    if re.search(r"deadline|\bdue\b|assignment|submi|pending|homework|\blab\b|todo|to do", t):
        days = 7 if "week" in t else (2 if off is not None else 14)
        return render_deadlines(await get_deadlines(days, 7), f"Due in the next {days} days")
    if re.search(r"notice|announce|circular", t):
        return await _tool_text("mycamu_get_announcements", "📣", days_back=7)
    if re.search(r"notif|new post|update|news", t):
        return await _news_text()
    if off is not None:
        return await _today_text(off, with_due=off == 0)
    return "I didn't catch that. " + HELP


# --------------------------------------------------------------------------- free text: Groq (free tier)

AI_TOOL_NAMES = [
    "moodle_semester_overview", "moodle_get_upcoming_deadlines", "moodle_list_assignments",
    "moodle_get_assignment_status", "moodle_list_quizzes", "moodle_get_grades", "moodle_list_courses",
    "moodle_get_announcements", "moodle_get_notifications", "mycamu_get_attendance",
    "mycamu_get_timetable", "mycamu_get_exam_schedule", "mycamu_get_announcements", "moodle_read_file",
]
TOOL_RESULT_CHARS = 3500  # Groq free tier allows ~8K tokens/min, so keep tool output small
FILE_RESULT_CHARS = 6000

MATERIALS_TOOL = {"type": "function", "function": {
    "name": "search_course_materials",
    "description": "Find course files on the LMS - syllabus / midterm (mid-sem) syllabus, lecture slides, "
                   "notes, tutorials and solutions, BTLAP, assessment plans. Returns matching items with "
                   "file URLs; pass a URL to moodle_read_file to read it.",
    "parameters": {"type": "object", "properties": {
        "course": {"type": "string", "description": "Course name or code, e.g. 'automata' or 'CSET302'."},
        "keywords": {"type": "string", "description": "What to look for, e.g. 'midterm syllabus', 'lecture 5', 'tutorial 6 solution'."},
    }, "required": ["course"]}}}


class AIUnavailable(Exception):
    pass


def _clean_schema(s: Any) -> Any:
    if isinstance(s, dict):
        return {k: _clean_schema(v) for k, v in s.items() if k != "title"}
    if isinstance(s, list):
        return [_clean_schema(x) for x in s]
    return s


async def ai_tools() -> list[dict]:
    out = []
    for t in await mcp.tools():
        if t["name"] not in AI_TOOL_NAMES:
            continue
        schema = _clean_schema(t.get("inputSchema") or {"type": "object", "properties": {}})
        schema.get("properties", {}).pop("response_format", None)
        desc = re.sub(r"\s+", " ", (t.get("description") or "").split("\n\n")[0]).strip()[:300]
        if t["name"] == "moodle_read_file":
            schema.get("properties", {}).pop("save_copy", None)
            schema.get("properties", {}).pop("max_chars", None)
        out.append({"type": "function", "function": {"name": t["name"], "description": desc, "parameters": schema}})
    out.append(MATERIALS_TOOL)
    return out


def _system_prompt() -> str:
    return (
        f"You are Study Buddy, a Telegram assistant for a {ENV.get('STUDENT_PROGRAMME', 'B.Tech')} student at Bennett University (India). "
        f"Current date/time: {_now().strftime('%A %d %B %Y, %I:%M %p')} IST. "
        "Answer using the tools: Moodle LMS (courses, deadlines, assignments, quizzes, grades, forum "
        "announcements) and MyCamu ERP (attendance, timetable, exam schedule, university notices). "
        "Never invent deadlines, marks, classes or attendance - state only what tools return, and say "
        f"so if a tool errors or returns nothing. Attendance rule: {ENV.get('ATTENDANCE_TARGET', '75')}% minimum per subject. "
        "For timetable on a specific day call mycamu_get_timetable with start_date=YYYY-MM-DD and days_ahead=0. "
        "For a syllabus (mid-sem = midterm), slides, notes, tutorials or any course file: call "
        "search_course_materials, then moodle_read_file on the best file URL, and answer from its text "
        "(include the key points, not just a link). "
        "Reply for Telegram: short, plain sentences or bullets (•), **bold** for key items, no tables, "
        "no headings. Show dates like 'Thu 15 Oct, 11:59 PM'. Reply in the user's language."
    )


async def _groq(model: str, messages: list[dict], tools: list[dict]) -> dict:
    body: dict[str, Any] = {"model": model, "messages": messages, "tools": tools, "tool_choice": "auto",
                            "temperature": 0.2, "max_completion_tokens": 900}
    if "gpt-oss" in model:
        body["reasoning_effort"] = "low"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.groq.com/openai/v1/chat/completions",
                         headers={"Authorization": f"Bearer {GROQ_KEY}"}, json=body)
    if r.status_code == 429:
        raise AIUnavailable("rate limit")
    if r.status_code >= 400:
        raise AIUnavailable(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()["choices"][0]["message"]


async def ai_answer(text: str, history: list[dict]) -> str:
    tools = await ai_tools()
    allowed = {t["function"]["name"]: set(t["function"]["parameters"].get("properties", {})) for t in tools}
    last_err: Exception | None = None
    for model in GROQ_MODELS:
        messages: list[dict] = [{"role": "system", "content": _system_prompt()}, *history,
                                {"role": "user", "content": text}]
        try:
            for _ in range(5):
                msg = await _groq(model, messages, tools)
                calls = msg.get("tool_calls") or []
                if not calls:
                    answer = (msg.get("content") or "").strip()
                    if answer:
                        return answer
                    raise AIUnavailable("empty answer")
                messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
                for call in calls:
                    fn = call.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}") or {}
                    except ValueError:
                        args = {}
                    cap = TOOL_RESULT_CHARS
                    if name == "search_course_materials":
                        result = await materials_tool(str(args.get("course") or ""), str(args.get("keywords") or ""))
                    elif name in allowed:
                        args = {k: v for k, v in args.items() if k in allowed[name] and v is not None}
                        if name == "moodle_read_file":
                            args["max_chars"] = FILE_RESULT_CHARS
                            cap = FILE_RESULT_CHARS
                        result = await mcp.call(name, **args)
                    else:
                        result = f"Error: unknown tool {name}"
                    if len(result) > cap:
                        result = result[:cap] + "\n…[truncated]"
                    messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
            raise AIUnavailable("too many tool rounds")
        except AIUnavailable as e:
            last_err = e
            log.warning("groq %s unavailable: %s", model, e)
            continue
        except httpx.HTTPError as e:
            last_err = e
            continue
    raise AIUnavailable(str(last_err))


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    chat_id = update.effective_chat.id
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    msg = update.effective_message
    if getattr(msg, "forward_origin", None) or re.match(r"(?i)\s*(date\s?sheet|exam (schedule|time ?table))", text):
        rows = datesheet.parse_text(text, await my_course_codes())
        if rows:
            await offer_exams(update, ctx, rows, "the forwarded text")
            return
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    note = ""
    try:
        if MATERIALS_RE.search(text.lower()):
            # Course files: the direct search is exact and uses no AI quota, so always take it.
            await send(ctx.bot, chat_id, await materials_answer(text))
            return
        if GROQ_KEY and store.ai_used(chat_id) < AI_DAILY_LIMIT:
            history: list[dict] = ctx.chat_data.setdefault("history", [])
            try:
                answer = await ai_answer(text, history)
                store.ai_bump(chat_id)
                history.extend([{"role": "user", "content": text[:500]},
                                {"role": "assistant", "content": answer[:800]}])
                del history[:-6]
                await send(ctx.bot, chat_id, md_to_html(answer))
                return
            except AIUnavailable:
                note = "\n\n<i>(AI free limit reached or busy - quick answer instead.)</i>"
        elif GROQ_KEY:
            note = f"\n\n<i>(Daily AI limit of {AI_DAILY_LIMIT} reached - quick answer instead.)</i>"
        out = await keyword_answer(text)
    except Exception as e:  # noqa: BLE001
        out = friendly_error(e)
    await send(ctx.bot, chat_id, out + note)


# --------------------------------------------------------------------------- exam date sheets

PDF_MAX = 8 * 1024 * 1024


def upcoming_exams(days: int) -> list[dict]:
    now = int(time.time())
    return store.rows("SELECT * FROM exams WHERE confirmed=1 AND starts_at > ? AND starts_at < ? ORDER BY starts_at",
                      now, now + days * 86400)


def _exam_when(x: dict) -> str:
    st = datetime.fromtimestamp(x["starts_at"], TZ)
    if not x.get("time_known"):
        return st.strftime("%a %d %b") + " · time not on the sheet"
    out = st.strftime("%a %d %b, %I:%M %p").replace(" 0", " ")
    if x.get("ends_at"):
        out += datetime.fromtimestamp(x["ends_at"], TZ).strftime(" – %I:%M %p").replace(" 0", " ")
    return out


def render_exams(rows: list[dict], title: str) -> str:
    out = [f"📝 <b>{esc(title)}</b>", ""] if title else ["", ""]
    for x in rows:
        venue = f" · {esc(x['venue'])}" if x.get("venue") else ""
        out.append(f"📝 <b>{esc(x['course_code'])}</b> {esc(x.get('title') or '')}")
        out.append(f"     {esc(_exam_when(x))} ({esc(rel(x['starts_at']))}){venue}")
    return "\n".join(out)


async def my_course_codes() -> dict[str, str]:
    """Course codes you actually take: attendance + timetable + LMS items (from study.db), else LMS courses."""
    codes: dict[str, str] = {}
    for sql in ("SELECT subject_code c, subject n FROM attendance_snap",
                "SELECT subject_code c, subject n FROM timetable",
                "SELECT course_code c, course_name n FROM items"):
        try:
            for r in store.rows(sql):
                if r["c"]:
                    codes.setdefault(r["c"], r["n"] or r["c"])
        except sqlite3.OperationalError:  # dashboard collector hasn't created these tables yet
            pass
    if not codes:
        for c in await all_courses():
            code = code_of(c.get("fullname"))
            if code:
                codes.setdefault(code, short_course(c.get("fullname")))
    return codes


async def offer_exams(update: Update, ctx: ContextTypes.DEFAULT_TYPE, rows: list[dict], source: str) -> None:
    ctx.chat_data["pending_exams"] = rows
    kinds = {r["kind"] for r in rows}
    lines = [f"📄 Found <b>{len(rows)}</b> exam{'s' if len(rows) != 1 else ''} for your courses in {esc(source)}"
             + (f" ({esc(', '.join(sorted(kinds)))})" if kinds - {'exam'} else "") + ":", ""]
    for r in rows:
        lines.append(f"• <b>{esc(r['course_code'])}</b> {esc(r['title'])}\n   {esc(_exam_when(r))}"
                     + (f" · {esc(r['venue'])}" if r.get("venue") else ""))
    if any(not r["time_known"] for r in rows):
        lines.append("\n<i>Rows without a time are set to the session start (FN 9:30 / AN 2:00) or 9:00.</i>")
    lines.append("\nSave them? Reminders then come like deadline pings, and they show on the dashboard + calendar feed.")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Save", callback_data="ex:save"),
                                InlineKeyboardButton("✖️ Discard", callback_data="ex:discard")]])
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb,
                                              disable_web_page_preview=True)


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    doc = update.effective_message.document
    if not doc or (doc.file_size or 0) > PDF_MAX:
        await update.effective_message.reply_text("That PDF is too big (max 8 MB).")
        return
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        f = await ctx.bot.get_file(doc.file_id)
        data = bytes(await f.download_as_bytearray())
        codes = await my_course_codes()
        rows = await asyncio.to_thread(datesheet.parse_pdf, data, codes)
    except Exception as e:  # noqa: BLE001
        log.exception("date sheet parse failed")
        await update.effective_message.reply_text(f"⚠️ Couldn't read that PDF ({type(e).__name__}).")
        return
    if not rows:
        await update.effective_message.reply_text(
            "I couldn't find any of your courses with a date in that PDF. If it's a scanned image, "
            "copy the rows for your courses as text and forward that instead.")
        return
    await offer_exams(update, ctx, rows, doc.file_name or "the PDF")


async def on_exam_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not is_owner(update):
        return
    rows = ctx.chat_data.pop("pending_exams", None)
    if q.data == "ex:discard" or not rows:
        await q.answer("Discarded" if rows else "Nothing pending - forward the sheet again.")
        await q.edit_message_reply_markup(None)
        return
    now = int(time.time())
    store.db.execute("BEGIN IMMEDIATE")
    try:
        for code in {r["course_code"] for r in rows}:  # a revised sheet replaces the old one for that course
            store.db.execute("DELETE FROM exams WHERE course_code=? AND source='datesheet' AND starts_at > ?", (code, now))
        for r in rows:
            store.db.execute(
                "INSERT OR REPLACE INTO exams(course_code,title,starts_at,ends_at,venue,kind,source,time_known,confirmed,created_at) "
                "VALUES(?,?,?,?,?,?,'datesheet',?,1,?)",
                (r["course_code"], r["title"], r["starts_at"], r["ends_at"], r.get("venue"), r["kind"],
                 1 if r["time_known"] else 0, now))
        store.db.execute("COMMIT")
    except Exception:
        store.db.execute("ROLLBACK")
        raise
    await q.answer("Saved")
    await q.edit_message_reply_markup(None)
    hrs = " and ".join(f"{h}h" for h in alert_hours()) or "no"
    await q.message.reply_text(f"✅ Saved {len(rows)} exam{'s' if len(rows) != 1 else ''}. I'll remind you {hrs} before each. "
                               "/exams to see them.")


# --------------------------------------------------------------------------- scheduled jobs


def alerts_on() -> bool:
    return bool(OWNER_ID) and store.setting("alerts_paused") != "1"


def alert_hours() -> list[int]:
    """Deadline ping thresholds (hours before), largest first - from the shared settings table."""
    hrs = {int(h) for h in store.setting("alert_hours").split(",") if h.strip().isdigit() and 0 < int(h) <= 72}
    return sorted(hrs, reverse=True)


def muted_courses() -> set[str]:
    return {c for c in store.setting("muted_courses").split(",") if c}


_CODE_RE = re.compile(r"\b([A-Z]{2,5}\d{3,5})\b")


def code_of(text: str | None) -> str:
    m = _CODE_RE.search(html.unescape(text or ""))
    return m.group(1) if m else ""


async def _mcp_outage(ctx: ContextTypes.DEFAULT_TYPE, down: bool) -> None:
    was = store.get("mcp_down") == "1"
    if down and not was:
        store.put("mcp_down", "1")
        await send(ctx.bot, OWNER_ID, "❌ <b>LMS server on your VPS stopped answering.</b>\n"
                                      "systemd will restart it; I'll tell you when it's back.")
    elif not down and was:
        store.put("mcp_down", "0")
        await send(ctx.bot, OWNER_ID, "✅ LMS server is answering again.")


async def _job(ctx: ContextTypes.DEFAULT_TYPE, name: str, fn: Any) -> None:
    try:
        await fn(ctx)
        await _mcp_outage(ctx, False)
    except MCPDown:
        await _mcp_outage(ctx, True)
    except ToolError as e:
        log.warning("%s: %s", name, str(e)[:200])
    except Forbidden:
        log.warning("%s: owner has blocked the bot", name)
    except Exception:  # noqa: BLE001
        log.exception("%s failed", name)


async def digest_tick(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every minute; sends the digest once a day at the time set on the dashboard. A restart
    later than 2 h after that time skips the day instead of sending a stale 'good morning'."""
    if not alerts_on():
        return
    now = _now()
    try:
        hh, mm = (int(x) for x in store.setting("digest_time").split(":"))
    except ValueError:
        hh, mm = 7, 45
    due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    key = f"digest:{now:%Y-%m-%d}"
    if now < due or store.was_sent(key):
        return
    store.mark_sent(key)
    if (now - due).total_seconds() > 2 * 3600:
        return
    await digest(ctx)


async def digest(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    now = _now()
    parts = [f"☀️ <b>Good morning!</b> {now.strftime('%A %d %b')}"]
    parts.append(render_timetable(await timetable_md(now, 0)))
    exams = upcoming_exams(7)
    if exams:
        parts.append(render_exams(exams, "Exams in the next 7 days"))
    try:
        week = [e for e in await get_deadlines(7, 3)]
        parts.append(render_deadlines(week, "Due in the next 7 days"))
    except ToolError as e:
        parts.append(friendly_error(e))
    att_md = await mcp.call("mycamu_get_attendance")
    if att_md.startswith("Error"):
        parts.append("⚠️ " + md_to_html(att_md))
    else:
        low = render_attendance(att_md, only_low=True)
        if low:
            parts.append(low.replace("📊", "📉", 1))
    await send(ctx.bot, OWNER_ID, "\n\n".join(p for p in parts if p))


def _bucket_for(due: float, now: float, hours: list[int]) -> int | None:
    """Smallest configured threshold the item is already inside, e.g. 2.5 h left with [24, 3] -> 3."""
    left = (due - now) / 3600
    inside = [h for h in hours if left <= h]
    return min(inside) if inside else None


async def _ping_once(ctx: ContextTypes.DEFAULT_TYPE, key: str, bucket: int, hours: list[int], text: str) -> None:
    if store.was_sent(f"{key}:{bucket}h"):
        return
    for h in hours:  # entering a tighter window also covers the wider ones
        if h >= bucket:
            store.mark_sent(f"{key}:{h}h")
    await send(ctx.bot, OWNER_ID, text)


def _head(bucket: int, what: str = "Due") -> str:
    return (f"🚨 <b>{what} in under {bucket} hours</b>" if bucket <= 6 else
            f"⏰ <b>{what} within {bucket} hours</b>")


async def deadline_alerts(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not alerts_on():
        return
    hours = alert_hours()
    if not hours:
        return
    now = time.time()
    muted = muted_courses()
    for e in await get_deadlines(max(2, -(-max(hours) // 24)), 0):
        due = int(e.get("due") or 0)
        if e.get("overdue") or due <= now or code_of(e.get("course")) in muted:
            continue
        bucket = _bucket_for(due, now, hours)
        if bucket is None:
            continue
        key = f"dl:{e.get('type')}:{e.get('instance')}:{due}"
        await _ping_once(ctx, key, bucket, hours, _head(bucket) + "\n\n" + render_deadlines([e], "").split("\n", 2)[-1])
    # Exams (from forwarded date sheets) and your own entries from the dashboard.
    for x in upcoming_exams(3):
        if x["course_code"] in muted:
            continue
        bucket = _bucket_for(x["starts_at"], now, hours)
        if bucket is not None:
            await _ping_once(ctx, f"exam:{x['id']}:{x['starts_at']}", bucket, hours,
                             _head(bucket, "Exam") + "\n\n" + render_exams([x], "").split("\n", 2)[-1])
    for m in store.rows("SELECT * FROM manual_entries WHERE remind=1 AND due_at > ? AND due_at < ?",
                        int(now), int(now) + max(hours) * 3600):
        if (m.get("course_code") or "") in muted:
            continue
        bucket = _bucket_for(m["due_at"], now, hours)
        if bucket is not None:
            course = f" - {esc(m['course_code'])}" if m.get("course_code") else ""
            await _ping_once(ctx, f"manual:{m['id']}:{m['due_at']}", bucket, hours,
                             _head(bucket) + f"\n\n📌 <b>{esc(m['title'])}</b>{course}\n     "
                             f"{esc(fmt_due(m['due_at']))} ({esc(rel(m['due_at']))})")
    store.prune()


async def new_notifications(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    notes = await mcp.call_json("moodle_get_notifications", limit=12)
    ids = [int(n.get("id") or 0) for n in notes]
    if not ids:
        return
    last = store.get("notif_last_id")
    store.put("notif_last_id", str(max(ids + [int(last or 0)])))
    if last is None or not alerts_on():  # first run just remembers where we are
        return
    muted = muted_courses()
    fresh = sorted((n for n in notes if int(n.get("id") or 0) > int(last) and code_of(n.get("subject")) not in muted),
                   key=lambda n: int(n["id"]))
    for n in fresh[:8]:
        await send(ctx.bot, OWNER_ID, "🆕 " + _notif_line(n, body=True))
    if len(fresh) > 8:
        await send(ctx.bot, OWNER_ID, f"…and {len(fresh) - 8} more. /news to see them.")


_ITEM = re.compile(r"^- \[(\w+)\] \*\*(.+?)\*\* \(cmid (\d+)")


async def new_course_items(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    courses = await mcp.call_json("moodle_list_courses")
    for c in courses:
        cid = c.get("id")
        md = await mcp.call("moodle_get_course_contents", course_id=cid, include_descriptions=False)
        if md.startswith("Error"):
            continue
        items: dict[str, tuple[str, str, str]] = {}
        section = ""
        for line in md.splitlines():
            if line.startswith("## "):
                section = line[3:].strip()
            m = _ITEM.match(line)
            if m:
                items[m.group(3)] = (m.group(1), m.group(2), section)
        key = f"cm:{cid}"
        seen_raw = store.get(key)
        seen = set(json.loads(seen_raw)) if seen_raw else set()
        store.put(key, json.dumps(sorted(seen | set(items))))
        if seen_raw is None or not alerts_on() or code_of(c.get("fullname")) in muted_courses():
            continue  # first sight of this course (or muted): remember, don't announce
        new = [(cm, *items[cm]) for cm in items if cm not in seen and items[cm][0] != "forum"]
        if not new:
            continue
        lines = [f"🆕 <b>New on LMS</b> - {esc(short_course(c.get('fullname')))}"]
        for cm, mod, name, sec in new[:10]:
            url = f"{MOODLE_URL}/mod/{mod}/view.php?id={cm}"
            icon = {"assign": "📝", "quiz": "❓", "resource": "📄", "folder": "📁", "url": "🔗"}.get(mod, "•")
            lines.append(f'{icon} <a href="{html.escape(url)}">{esc(name)}</a>' + (f" <i>({esc(sec)})</i>" if sec else ""))
        await send(ctx.bot, OWNER_ID, "\n".join(lines))
        await asyncio.sleep(1)


async def camu_health(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    st = await mcp.call("mycamu_status")
    bad = "active" not in st
    was = store.get("camu_bad") == "1"
    if bad and not was:
        store.put("camu_bad", "1")
        await send(ctx.bot, OWNER_ID, "⚠️ <b>MyCamu session expired</b> - attendance and timetable won't load.\n"
                                      "Log in to MyCamu in Chrome, then run <b>~/moodle-mcp/set-camu-cookie</b> on the server.")
    elif not bad and was:
        store.put("camu_bad", "0")
        await send(ctx.bot, OWNER_ID, "✅ MyCamu is connected again.")


# --------------------------------------------------------------------------- startup


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("today", "Today's classes + what's due"),
        BotCommand("tomorrow", "Tomorrow's classes"),
        BotCommand("week", "Timetable for 7 days"),
        BotCommand("deadlines", "Due in 14 days"),
        BotCommand("attendance", "Attendance + 75% check"),
        BotCommand("grades", "Course totals"),
        BotCommand("exams", "Exam schedule"),
        BotCommand("news", "Latest LMS notifications"),
        BotCommand("notices", "MyCamu announcements"),
        BotCommand("status", "Server / MyCamu / AI status"),
        BotCommand("alerts", "Pause or resume automatic messages"),
        BotCommand("help", "What I can do"),
    ])
    if OWNER_ID and store.get("hello_sent") != "1":
        store.put("hello_sent", "1")
        try:
            await send(app.bot, OWNER_ID, "✅ <b>Study Buddy is live.</b> Morning digest at "
                                          f"{store.setting('digest_time')} IST.\n\n" + HELP)
        except Exception:  # noqa: BLE001
            log.warning("couldn't greet the owner (send /start to the bot once)")
    log.info("bot ready; owner=%s ai=%s", bool(OWNER_ID), bool(GROQ_KEY))


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("update failed: %s", ctx.error, exc_info=ctx.error)


def main() -> None:
    if not BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN missing in .env - run ~/moodle-mcp/set-bot-secrets")
    if not OWNER_ID:
        sys.exit("OWNER_CHAT_ID missing in .env - run ~/moodle-mcp/set-bot-secrets")
    if not ENV.get("MCP_SECRET"):
        sys.exit("MCP_SECRET missing in .env - deploy the MCP server first (deploy.sh)")
    store.ensure_owner(OWNER_ID)

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    for name, fn in [("start", cmd_start), ("help", cmd_start), ("today", cmd_today),
                     ("tomorrow", cmd_tomorrow), ("week", cmd_week), ("deadlines", cmd_deadlines),
                     ("attendance", cmd_attendance), ("grades", cmd_grades), ("exams", cmd_exams),
                     ("news", cmd_news), ("notices", cmd_notices), ("status", cmd_status),
                     ("alerts", cmd_alerts)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.Document.PDF, on_document))
    app.add_handler(CallbackQueryHandler(on_exam_button, pattern=r"^ex:"))
    app.add_error_handler(on_error)

    jq = app.job_queue

    def wrap(name: str, fn: Any) -> Any:
        async def runner(ctx: ContextTypes.DEFAULT_TYPE) -> None:
            await _job(ctx, name, fn)
        return runner

    jq.run_repeating(wrap("digest", digest_tick), interval=60, first=20, name="digest")
    jq.run_repeating(wrap("deadlines", deadline_alerts), interval=600, first=45, name="deadlines")
    jq.run_repeating(wrap("notifications", new_notifications), interval=1800, first=90, name="notifications")
    jq.run_repeating(wrap("course_items", new_course_items), interval=3 * 3600, first=240, name="course_items")
    jq.run_repeating(wrap("camu_health", camu_health), interval=12 * 3600, first=600, name="camu_health")

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
