"""Shared bits for the Study Buddy dashboard: config, SQLite store, MCP client, time helpers."""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

APP_DIR = Path(__file__).resolve().parent.parent  # ~/moodle-mcp


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    p = APP_DIR / ".env"
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
TZ = ZoneInfo(ENV.get("BOT_TZ", "Asia/Kolkata"))
DB_PATH = Path(ENV.get("DASH_DB", str(APP_DIR / "study.db")))
MCP_URL = f"http://127.0.0.1:{ENV.get('MCP_PORT', '8765')}/{ENV.get('MCP_SECRET', '')}/mcp"
ATT_TARGET = float(ENV.get("ATTENDANCE_TARGET", "75"))

COURSE_CODE = re.compile(r"\b([A-Z]{2,5}\d{3,5})\b")


def now_ts() -> int:
    return int(time.time())


def now_local() -> datetime:
    return datetime.now(TZ)


def course_code(name: str | None) -> str:
    m = COURSE_CODE.search(html.unescape(name or ""))
    return m.group(1) if m else ""


def course_title(name: str | None) -> str:
    """'EB93 - CSET302: Automata Theory (ODD Semester 2026-27)' -> 'Automata Theory'."""
    s = html.unescape(name or "")
    if ":" in s:
        s = s.split(":", 1)[1]
    return re.sub(r"\s*\((ODD|EVEN)[^)]*\)\s*$", "", s, flags=re.I).strip()


def week_monday(d: datetime) -> datetime:
    d = d.astimezone(TZ) if d.tzinfo else d.replace(tzinfo=TZ)
    return (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


# --------------------------------------------------------------------------- SQLite

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,            -- moodle:assign:<cmid> / moodle:quiz:<cmid> / moodle:event:<id>
  source TEXT NOT NULL,
  course_code TEXT, course_name TEXT, course_id INTEGER,
  type TEXT NOT NULL,             -- assign / quiz / event
  title TEXT NOT NULL,
  opens_at INTEGER, due_at INTEGER, cutoff_at INTEGER,
  status TEXT,                    -- pending / draft / submitted / graded / attempted / closed / open
  grade TEXT, max_grade REAL,
  action TEXT, action_required INTEGER DEFAULT 0,
  url TEXT, ref_id INTEGER,       -- assign_id / quiz_id for drill-down
  detail TEXT,                    -- JSON blob (attempts, extension, ...)
  status_checked_at INTEGER,
  seen_at INTEGER,                -- last time the collector saw it upstream
  updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS items_due ON items(due_at);

CREATE TABLE IF NOT EXISTS attendance_snap (
  date TEXT, subject_code TEXT, subject TEXT, present INTEGER, total INTEGER, pct REAL,
  PRIMARY KEY (date, subject_code)
);
CREATE TABLE IF NOT EXISTS grade_snap (
  date TEXT, course_code TEXT, item TEXT, grade TEXT, max REAL,
  PRIMARY KEY (date, course_code, item)
);
CREATE TABLE IF NOT EXISTS timetable (
  date TEXT, start TEXT, "end" TEXT, start_ts INTEGER, end_ts INTEGER,
  subject_code TEXT, subject TEXT, faculty TEXT, slot TEXT,
  PRIMARY KEY (date, start, subject_code)
);
CREATE TABLE IF NOT EXISTS timetable_weeks (
  week TEXT PRIMARY KEY, status TEXT, note TEXT, fetched_at INTEGER   -- status: ok / not_finalised / error
);
CREATE TABLE IF NOT EXISTS feed (
  id TEXT PRIMARY KEY, source TEXT, title TEXT, body TEXT, url TEXT, posted_at INTEGER,
  read INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS local_marks (
  item_id TEXT PRIMARY KEY, state TEXT, note TEXT, until INTEGER, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS manual_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, course_code TEXT, due_at INTEGER,
  kind TEXT, remind INTEGER DEFAULT 1, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS credits (course_code TEXT PRIMARY KEY, credits REAL, expected TEXT);
CREATE TABLE IF NOT EXISTS past_terms (term TEXT PRIMARY KEY, sgpa REAL, credits REAL);
-- Shared with the bot (it creates the same tables if it starts first — keep in sync with telegram_bot.py).
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS exams (
  id INTEGER PRIMARY KEY AUTOINCREMENT, course_code TEXT, title TEXT, starts_at INTEGER, ends_at INTEGER,
  venue TEXT, kind TEXT, source TEXT, time_known INTEGER DEFAULT 1, confirmed INTEGER DEFAULT 1,
  created_at INTEGER, UNIQUE(course_code, starts_at)
);
CREATE TABLE IF NOT EXISTS sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, ran_at INTEGER, ok INTEGER, error TEXT, ms INTEGER
);
CREATE INDEX IF NOT EXISTS sync_log_src ON sync_log(source, ran_at);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
-- v3: course hub, notes, search ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS courses (
  id INTEGER PRIMARY KEY, code TEXT, name TEXT, title TEXT, last_seen INTEGER, contents_at INTEGER, hash TEXT
);
CREATE TABLE IF NOT EXISTS modules (
  cmid INTEGER PRIMARY KEY, course_id INTEGER, section_idx INTEGER, section TEXT, pos INTEGER,
  modname TEXT, name TEXT, description TEXT, url TEXT, dates TEXT, first_seen INTEGER, updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS modules_course ON modules(course_id, section_idx, pos);
CREATE TABLE IF NOT EXISTS files (
  url TEXT PRIMARY KEY, cmid INTEGER, course_id INTEGER, filename TEXT, size INTEGER, modified INTEGER,
  first_seen INTEGER, index_state TEXT, indexed_at INTEGER, chars INTEGER, note TEXT
);                                -- index_state: NULL (todo) / ok / empty / skip / error
CREATE INDEX IF NOT EXISTS files_course ON files(course_id);
CREATE TABLE IF NOT EXISTS course_visits (course_id INTEGER PRIMARY KEY, visited_at INTEGER);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, course_code TEXT, kind TEXT NOT NULL,   -- note / todo
  text TEXT NOT NULL, done INTEGER DEFAULT 0, due_at INTEGER, created_at INTEGER, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS docs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, ref TEXT UNIQUE, course TEXT, title TEXT, body TEXT,
  url TEXT, updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS docs_kind ON docs(kind);
CREATE TABLE IF NOT EXISTS login_tokens (
  hash TEXT PRIMARY KEY, expires_at INTEGER, used INTEGER DEFAULT 0
);
"""


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
  title, body, content='docs', content_rowid='id', tokenize='porter unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
  INSERT INTO docs_fts(rowid, title, body) VALUES (new.id, new.title, new.body); END;
CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
  INSERT INTO docs_fts(docs_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body); END;
CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
  INSERT INTO docs_fts(docs_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
  INSERT INTO docs_fts(rowid, title, body) VALUES (new.id, new.title, new.body); END;
"""
HAS_FTS = False


def _migrate(con: sqlite3.Connection) -> None:
    global HAS_FTS
    cols = {r["name"] for r in con.execute("PRAGMA table_info(feed)")}
    for c, ddl in (("course_code", "TEXT"), ("author", "TEXT"), ("src_read", "INTEGER DEFAULT 0")):
        if c not in cols:
            con.execute(f"ALTER TABLE feed ADD COLUMN {c} {ddl}")
    con.execute("CREATE INDEX IF NOT EXISTS feed_posted ON feed(posted_at)")
    try:
        con.executescript(FTS_DDL)
        HAS_FTS = True
    except sqlite3.OperationalError:
        HAS_FTS = False  # no FTS5 in this SQLite build: search falls back to LIKE


def init_db() -> None:
    con = db()
    con.executescript(SCHEMA)
    _migrate(con)
    con.close()
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass


SETTING_DEFAULTS = {"alerts_paused": "0", "digest_time": "07:45", "alert_hours": "24,3", "muted_courses": ""}


def settings_all(con: sqlite3.Connection) -> dict[str, str]:
    out = dict(SETTING_DEFAULTS)
    out.update({r["k"]: r["v"] for r in con.execute("SELECT k, v FROM settings")})
    return out


def meta_get(con: sqlite3.Connection, k: str, default: Any = None) -> Any:
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r["v"])
    except ValueError:
        return r["v"]


def meta_put(con: sqlite3.Connection, k: str, v: Any) -> None:
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, json.dumps(v)))


def upsert_doc(con: sqlite3.Connection, kind: str, ref: str, course: str, title: str, body: str, url: str | None) -> None:
    """Search document. An empty body never wipes text already indexed for the same ref."""
    con.execute(
        "INSERT INTO docs(kind,ref,course,title,body,url,updated_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(ref) DO UPDATE SET kind=excluded.kind, course=excluded.course, title=excluded.title, "
        "body=CASE WHEN excluded.body='' THEN docs.body ELSE excluded.body END, url=excluded.url, updated_at=excluded.updated_at",
        (kind, ref, course or "", title or "", body or "", url, now_ts()))


# --------------------------------------------------------------------------- MCP client


class MCPDown(Exception):
    """The local MCP server isn't answering."""


class ToolError(Exception):
    """A tool answered with an error (LMS down, MyCamu cookie expired, ...)."""


class MCP:
    """Same minimal JSON-RPC client the bot uses (stateless streamable-HTTP, JSON responses)."""

    HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def __init__(self, url: str = MCP_URL):
        self.url = url
        self._n = 0
        self.client = httpx.Client(timeout=150, trust_env=False)

    def _rpc(self, method: str, params: dict) -> dict:
        self._n += 1
        try:
            r = self.client.post(self.url, headers=self.HEADERS,
                                 json={"jsonrpc": "2.0", "id": self._n, "method": method, "params": params})
            r.raise_for_status()
            d = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise MCPDown(type(e).__name__) from e
        if "error" in d:
            raise MCPDown(str(d["error"].get("message", d["error"])))
        return d.get("result", {})

    def call(self, name: str, **args: Any) -> str:
        res = self._rpc("tools/call", {"name": name, "arguments": args})
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text").strip()
        if res.get("isError") or text.startswith("Error"):
            raise ToolError(text[:500] or f"{name} failed")
        return text

    def call_json(self, name: str, **args: Any) -> Any:
        text = self.call(name, response_format="json", **args)
        try:
            return json.loads(text)
        except ValueError as e:
            raise ToolError(f"Unexpected reply from {name}: {text[:200]}") from e

    def close(self) -> None:
        self.client.close()
