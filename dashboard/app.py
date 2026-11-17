"""Study Buddy Dashboard - a Starlette app mounted inside the moodle-mcp HTTP server at /dash.

No second Python server: moodle_mcp.py routes /dash/... here (see SecretPathGate). Starlette ships
with the `mcp` package, so this adds no dependencies. Every handler's SQLite work runs in the thread
pool, so the MCP server's event loop never waits on the database.

Auth: owner-only, via a one-time login link sent to your Telegram (works out of the box) or the
Telegram Login widget (after /setdomain in @BotFather). Session = signed HttpOnly cookie, 30 days.

Local testing:  python -m dashboard.app --serve      (needs uvicorn)  ·  --mint-cookie
"""

from __future__ import annotations

import hashlib
import hmac
import html
import inspect
import json
import re
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs

import httpx
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .common import (
    APP_DIR, ATT_TARGET, ENV, SETTING_DEFAULTS, TZ, db, init_db, meta_get, meta_put, now_local, now_ts,
    settings_all, week_monday,
)

BOT_TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = int(ENV["OWNER_CHAT_ID"]) if ENV.get("OWNER_CHAT_ID", "").lstrip("-").isdigit() else 0
SECRET = ENV.get("DASH_SECRET", "").encode()
ICS_TOKEN = ENV.get("DASH_ICS_TOKEN", "")
PUBLIC_URL = ENV.get("DASH_PUBLIC_URL", "").rstrip("/")
DEV = ENV.get("DASH_DEV") == "1"
COOKIE = "sb_session"
SESSION_DAYS = 30
SYNC_EVERY = 120
DIST = Path(__file__).resolve().parent / "web" / "dist"
GRADE_POINTS = {"O": 10, "A+": 9, "A": 8, "B+": 7, "B": 6, "C": 5, "P": 4, "F": 0}

if len(SECRET) < 32 and not DEV:
    # Raised (not sys.exit) so the MCP server keeps running with the dashboard disabled.
    raise RuntimeError("DASH_SECRET missing/short in .env - run deploy_dash.sh (it generates one).")

init_db()

# --------------------------------------------------------------------------- plumbing


def endpoint(auth: bool = True) -> Callable:
    """Wrap a sync handler(request, body) -> dict | Response: auth + CSRF, JSON body, thread pool."""
    def deco(fn: Callable) -> Callable:
        async def handler(request: Request) -> Response:
            if auth:
                if not _check_session(request.cookies.get(COOKIE)):
                    raise HTTPException(401, "login required")
                # CSRF: state-changing calls must carry a custom header (forces a CORS preflight cross-site).
                if request.method not in ("GET", "HEAD") and request.headers.get("x-sb") != "1":
                    raise HTTPException(403, "missing header")
            body: dict = {}
            if request.method in ("POST", "PUT") and "json" in request.headers.get("content-type", ""):
                try:
                    body = await request.json()
                except ValueError:
                    raise HTTPException(400, "bad JSON") from None
                if not isinstance(body, dict):
                    raise HTTPException(400, "JSON object expected")
            res = await fn(request, body) if inspect.iscoroutinefunction(fn) else await run_in_threadpool(fn, request, body)
            return res if isinstance(res, Response) else JSONResponse(res)
        return handler
    return deco


async def http_error(request: Request, exc: HTTPException) -> Response:
    if request.url.path.startswith("/dash/api"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return PlainTextResponse(str(exc.detail), status_code=exc.status_code)


CSP = ("default-src 'self'; script-src 'self' https://telegram.org; frame-src https://oauth.telegram.org; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data: https://*.telegram.org https://t.me; "
       "connect-src 'self'; base-uri 'none'; form-action 'self'")


class Headers:
    """Security headers for everything under /dash (pure ASGI, no BaseHTTPMiddleware overhead)."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        is_api = scope["path"].startswith("/dash/api")

        async def wrapped(msg: dict) -> None:
            if msg["type"] == "http.response.start":
                h = list(msg.get("headers", []))
                names = {k.lower() for k, _ in h}
                for k, v in ((b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                             (b"x-frame-options", b"DENY"), (b"content-security-policy", CSP.encode())):
                    if k not in names:
                        h.append((k, v))
                if is_api and b"cache-control" not in names:
                    h.append((b"cache-control", b"no-store"))
                msg["headers"] = h
            await send(msg)
        await self.app(scope, receive, wrapped)


def _rows(sql: str, *args: Any) -> list[dict]:
    con = db()
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


# --------------------------------------------------------------------------- auth


def _sign(payload: str) -> str:
    return hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()


def _make_session(uid: int) -> str:
    exp = now_ts() + SESSION_DAYS * 86400
    p = f"{uid}.{exp}"
    return f"{p}.{_sign(p)}"


def _check_session(v: str | None) -> int | None:
    try:
        uid, exp, sig = (v or "").split(".")
        if not hmac.compare_digest(sig, _sign(f"{uid}.{exp}")) or int(exp) < now_ts():
            return None
        return int(uid) if OWNER_ID and int(uid) == OWNER_ID else None
    except ValueError:
        return None


def _set_cookie(resp: Response, uid: int) -> None:
    resp.set_cookie(COOKIE, _make_session(uid), max_age=SESSION_DAYS * 86400, path="/dash",
                    httponly=True, secure=not DEV, samesite="lax")


def _bot_username() -> str | None:
    con = db()
    u = meta_get(con, "bot_username")
    con.close()
    if u or not BOT_TOKEN:
        return u
    try:
        u = httpx.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=8).json()["result"]["username"]
        con = db()
        meta_put(con, "bot_username", u)
        con.close()
        return u
    except Exception:  # noqa: BLE001
        return None


@endpoint(auth=False)
def auth_config(request: Request, _: dict) -> dict:
    return {"logged_in": bool(_check_session(request.cookies.get(COOKIE))), "bot_username": _bot_username(),
            "link_login": bool(BOT_TOKEN and OWNER_ID and PUBLIC_URL)}


@endpoint(auth=False)
def send_link(request: Request, _: dict) -> dict:
    if request.headers.get("x-sb") != "1":
        raise HTTPException(403, "missing header")
    if not (BOT_TOKEN and OWNER_ID and PUBLIC_URL):
        raise HTTPException(503, "Login link isn't configured (bot token / owner / DASH_PUBLIC_URL).")
    con = db()
    try:
        if now_ts() - (meta_get(con, "last_link_sent", 0) or 0) < 60:
            raise HTTPException(429, "A link was sent less than a minute ago - check Telegram.")
        meta_put(con, "last_link_sent", now_ts())
        tok = secrets.token_urlsafe(32)
        con.execute("INSERT INTO login_tokens(hash, expires_at) VALUES(?,?)",
                    (hashlib.sha256(tok.encode()).hexdigest(), now_ts() + 600))
    finally:
        con.close()
    link = f"{PUBLIC_URL}/dash/login?t={tok}"
    text = (f"🔐 <b>Study Buddy dashboard login</b>\n\n<a href=\"{html.escape(link)}\">Open dashboard</a>\n\n"
            "Valid for 10 minutes, works once. If you didn't ask for this, ignore it.")
    r = httpx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", timeout=15, json={
        "chat_id": OWNER_ID, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}})
    if r.status_code != 200:
        raise HTTPException(502, "Telegram didn't accept the message.")
    return {"sent": True}


async def login_page(request: Request) -> Response:
    # GET never consumes the token (link previews / scanners); the button POSTs it.
    tok = html.escape(request.query_params.get("t", ""))
    return HTMLResponse(f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Study Buddy</title><style>body{{background:#0e0f11;color:#ebe8e2;font:16px system-ui;display:grid;place-items:center;min-height:100vh;margin:0}}
form{{text-align:center}}button{{background:#f2c14e;color:#1a1405;border:0;border-radius:12px;padding:14px 28px;font:600 16px system-ui}}</style>
<form method=post action="/dash/login"><p>Sign in to Study Buddy</p><input type=hidden name=t value="{tok}"><button>Continue</button></form>""")


async def login_consume(request: Request) -> Response:
    raw = (await request.body())[:2048].decode("utf-8", "ignore")
    tok = (parse_qs(raw).get("t") or [""])[0]

    def use() -> int:
        con = db()
        try:
            return con.execute("UPDATE login_tokens SET used=1 WHERE hash=? AND used=0 AND expires_at>?",
                               (hashlib.sha256(tok.encode()).hexdigest(), now_ts())).rowcount
        finally:
            con.close()
    if await run_in_threadpool(use) != 1 or not OWNER_ID:
        return HTMLResponse("<p style='font:16px system-ui'>Link expired or already used. "
                            "<a href='/dash/'>Get a new one</a></p>", status_code=400)
    resp = RedirectResponse("/dash/", status_code=303)
    _set_cookie(resp, OWNER_ID)
    return resp


@endpoint(auth=False)
def login_widget(_: Request, data: dict) -> Response:
    """Telegram Login widget: hash = HMAC_SHA256(check_string, SHA256(bot_token))."""
    if not BOT_TOKEN:
        raise HTTPException(503, "bot token not configured")
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data) if k != "hash" and data[k] is not None)
    want = hmac.new(hashlib.sha256(BOT_TOKEN.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(data.get("hash", "")), want):
        raise HTTPException(401, "bad signature")
    if now_ts() - int(data.get("auth_date", 0)) > 86400:
        raise HTTPException(401, "login too old")
    if int(data.get("id", 0)) != OWNER_ID:
        raise HTTPException(403, "This dashboard is private.")
    resp = JSONResponse({"ok": True})
    _set_cookie(resp, OWNER_ID)
    return resp


@endpoint()
def logout(_: Request, __: dict) -> Response:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/dash")
    return resp


# --------------------------------------------------------------------------- shared helpers


def _sync_state() -> dict:
    con = db()
    out: dict[str, Any] = {"last_run": meta_get(con, "last_run")}
    for src in ("moodle", "attendance", "timetable", "camu_unread", "grades", "courses", "feed", "index"):
        last = con.execute("SELECT ran_at, ok, error FROM sync_log WHERE source=? ORDER BY id DESC LIMIT 1", (src,)).fetchone()
        ok = con.execute("SELECT MAX(ran_at) FROM sync_log WHERE source=? AND ok=1", (src,)).fetchone()[0]
        out[src] = {"last_ok": ok, "last_try": last["ran_at"] if last else None,
                    "ok": bool(last["ok"]) if last else None, "error": last["error"] if last and not last["ok"] else None}
    con.close()
    return out


def _bucket(due: int | None, now: datetime) -> str:
    if not due:
        return "later"
    d = datetime.fromtimestamp(due, TZ)
    if due < now.timestamp():
        return "overdue"
    if d.date() == now.date():
        return "today"
    if d < now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=7 - now.weekday()):
        return "week"
    return "later"


DONE = {"submitted", "graded", "attempted"}


def _with_mark(r: dict, marks: dict[str, dict]) -> dict:
    m = marks.get(r["id"])
    r["mark"] = m["state"] if m else None
    r["mark_note"] = m["note"] if m else None
    r["snoozed_until"] = m["until"] if m and m["state"] == "snoozed" else None
    return r


def _item_out(r: dict, marks: dict[str, dict], now: datetime) -> dict:
    r = _with_mark(dict(r), marks)
    r["detail"] = json.loads(r.get("detail") or "{}")
    r["done"] = r.get("status") in DONE or r["mark"] == "done"
    b = _bucket(r.get("due_at"), now)
    r["bucket"] = "past" if b == "overdue" and (r["done"] or r.get("status") == "closed") else b
    return r


def _manual_out(e: dict, marks: dict[str, dict], now: datetime) -> dict:
    r = _with_mark({"id": f"manual:{e['id']}", "source": "manual", "type": e.get("kind") or "task", "title": e["title"],
                    "course_code": e.get("course_code"), "course_name": None, "due_at": e.get("due_at"), "url": None,
                    "detail": {}, "manual_id": e["id"]}, marks)
    r["done"] = r["mark"] == "done"
    r["status"] = "done" if r["done"] else "pending"
    b = _bucket(e.get("due_at"), now)
    r["bucket"] = "past" if b == "overdue" and r["done"] else b
    return r


def _exam_out(e: dict, marks: dict[str, dict], now: datetime) -> dict:
    ts = now.timestamp()
    r = _with_mark({"id": f"exam:{e['id']}", "source": "exam", "type": "exam", "title": e.get("title") or "Exam",
                    "course_code": e.get("course_code"), "course_name": None, "due_at": e.get("starts_at"),
                    "ends_at": e.get("ends_at"), "url": None, "exam_id": e["id"],
                    "detail": {"venue": e.get("venue"), "kind": e.get("kind"), "time_known": bool(e.get("time_known"))}},
                   marks)
    over = (e.get("ends_at") or (e.get("starts_at") or 0) + 3 * 3600) < ts
    r["done"] = over or r["mark"] == "done"
    r["status"] = "done" if over else "upcoming_exam"
    b = _bucket(e.get("starts_at"), now)
    r["bucket"] = "past" if b == "overdue" else b
    return r


def _all_items(include_past_days: int = 21) -> list[dict]:
    now = now_local()
    marks = {r["item_id"]: r for r in _rows("SELECT * FROM local_marks")}
    since = now_ts() - include_past_days * 86400
    items = [_item_out(r, marks, now) for r in _rows(
        "SELECT * FROM items WHERE due_at IS NULL OR due_at >= ?", since)]
    items += [_manual_out(e, marks, now) for e in _rows(
        "SELECT * FROM manual_entries WHERE due_at IS NULL OR due_at >= ?", since)]
    items += [_exam_out(e, marks, now) for e in _rows(
        "SELECT * FROM exams WHERE confirmed=1 AND (starts_at IS NULL OR starts_at >= ?)", since)]
    items.sort(key=lambda i: i.get("due_at") or 9e12)
    return items


def _attendance_latest() -> list[dict]:
    rows = _rows("""SELECT a.* FROM attendance_snap a
                    JOIN (SELECT MAX(date) d FROM attendance_snap) m ON a.date=m.d ORDER BY a.subject""")
    t = ATT_TARGET / 100
    for r in rows:
        p, n = r["present"], r["total"]
        if n == 0:
            r["must_attend"], r["can_skip"] = 0, 0
        elif p / n < t:
            r["must_attend"] = max(0, -int(-(t * n - p) // (1 - t)))   # ceil((t·n − p) / (1 − t))
            r["can_skip"] = 0
        else:
            r["must_attend"] = 0
            r["can_skip"] = max(0, int(p / t - n + 1e-9))
    return rows


def _course_names() -> dict[str, str]:
    names = {r["subject_code"]: r["subject"] for r in _attendance_latest()}
    for r in _rows("SELECT DISTINCT course_code, course_name FROM items WHERE course_code != ''"):
        names.setdefault(r["course_code"], r["course_name"] or r["course_code"])
    return names


# --------------------------------------------------------------------------- data endpoints


@endpoint()
def today(_: Request, __: dict) -> dict:
    now = now_local()
    ts = now.timestamp()
    day = now.strftime("%Y-%m-%d")
    classes = _rows("SELECT * FROM timetable WHERE date=? ORDER BY start_ts", day)
    nxt = _rows("SELECT * FROM timetable WHERE start_ts>? ORDER BY start_ts LIMIT 1", ts)
    horizon = ts + 48 * 3600
    due_soon = [i for i in _all_items(7) if i.get("due_at") and (
        (i["bucket"] == "overdue" and not i["done"]) or ts <= i["due_at"] <= horizon)
        and not (i["snoozed_until"] and i["snoozed_until"] > ts)]
    warnings = []
    for a in _attendance_latest():
        if a["total"] and a["pct"] < ATT_TARGET:
            warnings.append({"code": a["subject_code"], "subject": a["subject"], "pct": a["pct"], "level": "danger",
                             "text": f"attend {a['must_attend']}"})
        elif a["total"] and a["can_skip"] <= 2:
            warnings.append({"code": a["subject_code"], "subject": a["subject"], "pct": a["pct"], "level": "warn",
                             "text": "on the line" if a["can_skip"] == 0 else f"can skip {a['can_skip']}"})
    con = db()
    unread = {"moodle": meta_get(con, "unread_moodle", 0), "mycamu": meta_get(con, "unread_mycamu", 0)}
    wk = con.execute("SELECT status, note FROM timetable_weeks WHERE week=?",
                     (week_monday(now).strftime("%Y-%m-%d"),)).fetchone()
    overall = meta_get(con, "attendance_overall")
    con.close()
    return {"now": int(ts), "date": day, "classes": classes, "next": nxt[0] if nxt else None, "due_soon": due_soon,
            "attendance_warnings": warnings, "attendance_overall": overall, "unread": unread,
            "timetable_status": dict(wk) if wk else None, "sync": _sync_state()}


@endpoint()
def items(request: Request, _: dict) -> dict:
    rng = request.query_params.get("range", "all")
    course = request.query_params.get("course", "")
    all_ = _all_items()
    out = [i for i in all_ if (not course or (i.get("course_code") or "") == course)
           and (rng == "all" or i["bucket"] == rng)]
    names = _course_names()
    codes = sorted({i["course_code"] for i in all_ if i.get("course_code")})
    return {"items": out, "courses": [{"code": c, "name": names.get(c, "")} for c in codes], "sync": _sync_state()}


@endpoint()
def attendance(_: Request, __: dict) -> dict:
    latest = _attendance_latest()
    hist: dict[str, list] = {}
    for r in _rows("SELECT date, subject_code, pct FROM attendance_snap ORDER BY date"):
        hist.setdefault(r["subject_code"], []).append({"date": r["date"], "pct": r["pct"]})
    upcoming = _rows("SELECT date, start, \"end\", start_ts, subject_code, subject, faculty FROM timetable "
                     "WHERE start_ts>? ORDER BY start_ts", now_ts())
    remaining: dict[str, int] = {}
    for u in upcoming:
        remaining[u["subject_code"]] = remaining.get(u["subject_code"], 0) + 1
    for r in latest:
        r["history"] = hist.get(r["subject_code"], [])
        k = remaining.get(r["subject_code"], 0)
        r["upcoming_classes"] = k
        r["if_attend_all"] = round(100 * (r["present"] + k) / (r["total"] + k), 1) if (r["total"] + k) else None
        r["if_skip_all"] = round(100 * r["present"] / (r["total"] + k), 1) if (r["total"] + k) else None
    con = db()
    out = {"subjects": latest, "overall": meta_get(con, "attendance_overall"), "semester": meta_get(con, "semester"),
           "target": ATT_TARGET, "upcoming": upcoming,
           "timetable_until": upcoming[-1]["date"] if upcoming else None, "sync": _sync_state()}
    con.close()
    return out


def _free_slots(classes: list[dict], day_start: str = "08:20", day_end: str = "16:55", min_gap: int = 50) -> list[dict]:
    def mins(s: str) -> int:
        return int(s[:2]) * 60 + int(s[3:5])
    gaps, cur = [], mins(day_start)
    for c in sorted(classes, key=lambda c: c["start"]):
        if mins(c["start"]) - cur >= min_gap:
            gaps.append({"start": f"{cur // 60:02d}:{cur % 60:02d}", "end": c["start"]})
        cur = max(cur, mins(c["end"]))
    if mins(day_end) - cur >= min_gap:
        gaps.append({"start": f"{cur // 60:02d}:{cur % 60:02d}", "end": day_end})
    return gaps


@endpoint()
def timetable(request: Request, _: dict) -> dict:
    week = request.query_params.get("week", "")
    try:
        base = datetime.strptime(week, "%Y-%m-%d").replace(tzinfo=TZ) if week else now_local()
    except ValueError:
        raise HTTPException(400, "week must be YYYY-MM-DD") from None
    mon = week_monday(base)
    key, end = mon.strftime("%Y-%m-%d"), (mon + timedelta(days=6)).strftime("%Y-%m-%d")
    rows = _rows("SELECT * FROM timetable WHERE date BETWEEN ? AND ? ORDER BY start_ts", key, end)
    exams = [e for e in _all_items(14) if e["type"] == "exam" and e.get("due_at")]
    days = []
    for i in range(7):
        d = (mon + timedelta(days=i)).strftime("%Y-%m-%d")
        cls = [r for r in rows if r["date"] == d]
        ex = [e for e in exams if datetime.fromtimestamp(e["due_at"], TZ).strftime("%Y-%m-%d") == d]
        days.append({"date": d, "classes": cls, "free": _free_slots(cls) if cls else [], "exams": ex})
    wk = _rows("SELECT * FROM timetable_weeks WHERE week=?", key)
    return {"week": key, "days": days, "status": wk[0] if wk else None,
            "today": now_local().strftime("%Y-%m-%d"), "now": now_ts(), "sync": _sync_state()}


@endpoint()
def set_mark(_: Request, body: dict) -> dict:
    iid = str(body.get("item_id", ""))[:120]
    state = body.get("state")
    if not iid:
        raise HTTPException(400, "item_id required")
    con = db()
    try:
        if state in (None, "", "clear"):
            con.execute("DELETE FROM local_marks WHERE item_id=?", (iid,))
        elif state in ("started", "done", "snoozed"):
            until = now_ts() + max(1, min(int(body.get("hours", 24)), 24 * 14)) * 3600 if state == "snoozed" else None
            con.execute("INSERT OR REPLACE INTO local_marks VALUES(?,?,?,?,?)",
                        (iid, state, (body.get("note") or "")[:500] or None, until, now_ts()))
        else:
            raise HTTPException(400, "state must be started/done/snoozed/clear")
    finally:
        con.close()
    return {"ok": True}


@endpoint()
def list_entries(_: Request, __: dict) -> list[dict]:
    return _rows("SELECT * FROM manual_entries ORDER BY COALESCE(due_at, 9e12)")


@endpoint()
def add_entry(_: Request, body: dict) -> dict:
    title = str(body.get("title") or "").strip()[:200]
    if not title:
        raise HTTPException(400, "title required")
    due = body.get("due_at")
    con = db()
    try:
        cur = con.execute("INSERT INTO manual_entries(title,course_code,due_at,kind,remind,created_at) VALUES(?,?,?,?,?,?)",
                          (title, str(body.get("course_code") or "")[:20] or None, int(due) if due else None,
                           str(body.get("kind") or "task")[:20], 1 if body.get("remind", True) else 0, now_ts()))
    finally:
        con.close()
    return {"id": cur.lastrowid}


@endpoint()
def del_entry(request: Request, _: dict) -> dict:
    eid = int(request.path_params["eid"])
    con = db()
    con.execute("DELETE FROM manual_entries WHERE id=?", (eid,))
    con.execute("DELETE FROM local_marks WHERE item_id=?", (f"manual:{eid}",))
    con.close()
    return {"ok": True}


@endpoint()
def del_exam(request: Request, _: dict) -> dict:
    eid = int(request.path_params["eid"])
    con = db()
    con.execute("DELETE FROM exams WHERE id=?", (eid,))
    con.execute("DELETE FROM local_marks WHERE item_id=?", (f"exam:{eid}",))
    con.close()
    return {"ok": True}


@endpoint()
def sync(_: Request, body: dict) -> dict:
    only = [x for x in (body.get("only") or []) if x in ("moodle", "attendance", "timetable", "camu_unread", "grades", "courses", "feed", "index", "mycamu")]
    con = db()
    try:
        wait = SYNC_EVERY - (now_ts() - (meta_get(con, "last_manual_sync", 0) or 0))
        if wait > 0:
            raise HTTPException(429, f"Synced recently - try again in {wait}s.")
        meta_put(con, "last_manual_sync", now_ts())
    finally:
        con.close()
    subprocess.Popen([sys.executable, "-m", "dashboard.collector", *(["--only", *only] if only else [])], cwd=APP_DIR,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return {"started": True}


@endpoint()
def health(_: Request, __: dict) -> dict:
    errs = _rows("SELECT source, ran_at, error FROM sync_log WHERE ok=0 ORDER BY id DESC LIMIT 10")
    return {"sync": _sync_state(), "recent_errors": errs,
            "ics_url": f"{PUBLIC_URL}/dash/ics/{ICS_TOKEN}.ics" if ICS_TOKEN else None,
            "widget_url": f"{PUBLIC_URL}/dash/widget/{ICS_TOKEN}.json" if ICS_TOKEN else None}


async def ping(_: Request) -> Response:
    return PlainTextResponse("ok")


# --------------------------------------------------------------------------- bot settings (shared table)


@endpoint()
def get_settings(_: Request, __: dict) -> dict:
    con = db()
    s = settings_all(con)
    con.close()
    names = _course_names()
    return {"alerts_paused": s["alerts_paused"] == "1", "digest_time": s["digest_time"],
            "alert_hours": [int(x) for x in s["alert_hours"].split(",") if x.strip().isdigit()],
            "muted_courses": [c for c in s["muted_courses"].split(",") if c],
            "courses": [{"code": c, "name": n} for c, n in sorted(names.items())]}


@endpoint()
def put_settings(_: Request, body: dict) -> dict:
    upd: dict[str, str] = {}
    if "alerts_paused" in body:
        upd["alerts_paused"] = "1" if body["alerts_paused"] else "0"
    if "digest_time" in body:
        t = str(body["digest_time"])
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            raise HTTPException(400, "digest_time must be HH:MM")
        upd["digest_time"] = t
    if "alert_hours" in body:
        hrs = sorted({int(h) for h in body["alert_hours"] if str(h).isdigit() and 1 <= int(h) <= 72}, reverse=True)[:3]
        upd["alert_hours"] = ",".join(map(str, hrs))
    if "muted_courses" in body:
        upd["muted_courses"] = ",".join(c for c in (str(x)[:20] for x in body["muted_courses"]) if re.fullmatch(r"[A-Z0-9]+", c))
    unknown = set(body) - set(SETTING_DEFAULTS)
    if unknown:
        raise HTTPException(400, f"unknown settings: {', '.join(sorted(unknown))}")
    con = db()
    for k, v in upd.items():
        con.execute("INSERT INTO settings(k,v,updated_at) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, "
                    "updated_at=excluded.updated_at", (k, v, now_ts()))
    con.close()
    return {"ok": True}


# --------------------------------------------------------------------------- grades + SGPA/CGPA


def _grades_payload() -> dict:
    names = _course_names()
    snap = _rows("SELECT g.* FROM grade_snap g JOIN (SELECT MAX(date) d FROM grade_snap) m ON g.date=m.d")
    by_course: dict[str, list[dict]] = {}
    for g in snap:
        by_course.setdefault(g["course_code"], []).append(g)
    plan = {r["course_code"]: r for r in _rows("SELECT * FROM credits")}
    codes = sorted(set(names) | set(by_course) | set(plan))
    courses, pts, crs = [], 0.0, 0.0
    for c in codes:
        items = by_course.get(c, [])
        total = next((g for g in items if g["item"].lower() == "course total"), None)
        p = plan.get(c) or {}
        cr, exp = p.get("credits"), p.get("expected")
        if cr and exp in GRADE_POINTS:
            pts += cr * GRADE_POINTS[exp]
            crs += cr
        courses.append({"code": c, "name": names.get(c, c), "credits": cr, "expected": exp,
                        "total": total["grade"] if total else None, "total_max": total["max"] if total else None,
                        "items": [g for g in items if g is not total and g["grade"] not in ("—", "-", "")]})
    sgpa = round(pts / crs, 2) if crs else None
    past = _rows("SELECT * FROM past_terms ORDER BY term")
    cp = sum((t["sgpa"] or 0) * (t["credits"] or 0) for t in past)
    cc = sum(t["credits"] or 0 for t in past)
    cgpa_past = round(cp / cc, 2) if cc else None
    cgpa = round((cp + pts) / (cc + crs), 2) if (cc + crs) else None
    return {"courses": courses, "scale": GRADE_POINTS, "sgpa": sgpa, "credits_counted": crs, "past": past,
            "cgpa_past": cgpa_past, "cgpa": cgpa, "sync": _sync_state()}


@endpoint()
def grades(_: Request, __: dict) -> dict:
    return _grades_payload()


@endpoint()
def grades_plan(_: Request, body: dict) -> dict:
    con = db()
    try:
        for c in body.get("courses", [])[:40]:
            code = str(c.get("code", ""))[:20]
            if not re.fullmatch(r"[A-Z0-9]+", code):
                continue
            cr = c.get("credits")
            cr = float(cr) if cr not in (None, "") and 0 <= float(cr) <= 30 else None
            exp = c.get("expected") if c.get("expected") in GRADE_POINTS else None
            con.execute("INSERT INTO credits(course_code,credits,expected) VALUES(?,?,?) ON CONFLICT(course_code) "
                        "DO UPDATE SET credits=excluded.credits, expected=excluded.expected", (code, cr, exp))
        if "past" in body:
            con.execute("DELETE FROM past_terms")
            for t in body["past"][:16]:
                term = str(t.get("term", "")).strip()[:30]
                try:
                    sg, cr = float(t.get("sgpa")), float(t.get("credits"))
                except (TypeError, ValueError):
                    continue
                if term and 0 <= sg <= 10 and 0 < cr <= 60:
                    con.execute("INSERT OR REPLACE INTO past_terms VALUES(?,?,?)", (term, sg, cr))
    finally:
        con.close()
    return _grades_payload()


# --------------------------------------------------------------------------- ICS feed


def _ics_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    if len(line.encode()) <= 74:
        return line
    out, cur = [], b""
    for ch in line:
        e = ch.encode()
        if len(cur) + len(e) > 73:
            out.append(cur.decode())
            cur = b" "
        cur += e
    out.append(cur.decode())
    return "\r\n".join(out)


def _ics_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_body() -> str:
    stamp = _ics_dt(now_ts())
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Study Buddy//Dashboard//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:Study Buddy deadlines", "X-WR-TIMEZONE:Asia/Kolkata",
             "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    for i in _all_items(60):
        due = i.get("due_at")
        if not due:
            continue
        exam = i["type"] == "exam"
        start, end = (due, i.get("ends_at") or due + 3 * 3600) if exam else (due - 1800, due)
        code = f"[{i['course_code']}] " if i.get("course_code") else ""
        label = ("📝 Exam: " if exam else "✅ " if i["done"] else "") + code + i["title"] + (" (quiz)" if i.get("type") == "quiz" else "")
        desc = [f"Status: {i.get('status') or '—'}"] + [x for x in (i.get("course_name"), i.get("url")) if x]
        if exam and i["detail"].get("venue"):
            desc.append(f"Venue: {i['detail']['venue']}")
        lines += ["BEGIN:VEVENT", f"UID:{i['id'].replace(':', '-')}@studybuddy", f"DTSTAMP:{stamp}",
                  f"DTSTART:{_ics_dt(start)}", f"DTEND:{_ics_dt(end)}", f"SUMMARY:{_ics_text(label)}",
                  f"DESCRIPTION:{_ics_text(chr(10).join(desc))}"]
        if i.get("url"):
            lines.append(f"URL:{i['url']}")
        if not i["done"]:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Due soon", "TRIGGER:-PT24H", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"


async def ics(request: Request) -> Response:
    token = request.path_params["token"]
    if not ICS_TOKEN or not hmac.compare_digest(token, ICS_TOKEN):
        raise HTTPException(404, "Not Found")
    body = await run_in_threadpool(_ics_body)
    return Response(body, media_type="text/calendar; charset=utf-8",
                    headers={"Cache-Control": "max-age=900", "Content-Disposition": "inline; filename=study-buddy.ics"})


# --------------------------------------------------------------------------- iOS widget (Scriptable)


def _widget_body() -> dict:
    """Compact snapshot for the home-screen / lock-screen widget."""
    now = now_local()
    ts = now.timestamp()
    day = now.strftime("%Y-%m-%d")
    classes = _rows("SELECT start, \"end\", start_ts, end_ts, subject, subject_code FROM timetable "
                    "WHERE date=? ORDER BY start_ts", day)
    cur = next((c for c in classes if c["start_ts"] <= ts < c["end_ts"]), None)
    nxt = _rows("SELECT date, start, \"end\", start_ts, end_ts, subject, subject_code FROM timetable "
                "WHERE start_ts>? ORDER BY start_ts LIMIT 1", ts)
    items = [i for i in _all_items(14) if not i["done"] and i.get("due_at")
             and not (i.get("snoozed_until") and i["snoozed_until"] > ts)]
    overdue = [i for i in items if i["bucket"] == "overdue"]
    upcoming = sorted((i for i in items if i["due_at"] >= ts), key=lambda i: i["due_at"])
    pick = lambda i: {"title": i.get("title"), "code": i.get("course_code") or "", "type": i.get("type"),
                      "due_at": int(i["due_at"]), "url": i.get("url")}
    low = [{"code": a["subject_code"], "subject": a["subject"], "pct": a["pct"]}
           for a in _attendance_latest() if a["total"] and a["pct"] < ATT_TARGET]
    con = db()
    overall = meta_get(con, "attendance_overall")
    unread = (meta_get(con, "unread_moodle", 0) or 0) + (meta_get(con, "unread_mycamu", 0) or 0)
    con.close()
    return {"now": int(ts), "app_url": f"{PUBLIC_URL}/dash/",
            "current": cur, "next": nxt[0] if nxt else None, "classes_today": len(classes),
            "classes_left": sum(1 for c in classes if c["start_ts"] > ts),
            "overdue": len(overdue), "due_48h": sum(1 for i in upcoming if i["due_at"] <= ts + 48 * 3600),
            "due_week": sum(1 for i in upcoming if i["due_at"] <= ts + 7 * 86400),
            "deadlines": [pick(i) for i in (overdue[:2] + upcoming)[:5]],
            "attendance": (overall or {}).get("pct") if isinstance(overall, dict) else overall,
            "attendance_target": ATT_TARGET, "attendance_low": low[:3], "unread": unread}


async def widget(request: Request) -> Response:
    token = request.path_params["token"]
    if not ICS_TOKEN or not hmac.compare_digest(token, ICS_TOKEN):
        raise HTTPException(404, "Not Found")
    body = await run_in_threadpool(_widget_body)
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- app


class SPAStatic(StaticFiles):
    async def get_response(self, path: str, scope: Any) -> Response:
        resp = await super().get_response(path, scope)
        if path in ("", ".", "index.html", "sw.js", "manifest.webmanifest"):
            resp.headers["Cache-Control"] = "no-cache"
        elif path.startswith("assets/"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


A = "/dash/api"
routes = [
    Route("/dash", lambda r: RedirectResponse("/dash/", status_code=308)),
    Route(f"{A}/ping", ping),
    Route(f"{A}/auth/config", auth_config),
    Route(f"{A}/auth/send-link", send_link, methods=["POST"]),
    Route(f"{A}/auth/telegram", login_widget, methods=["POST"]),
    Route(f"{A}/auth/logout", logout, methods=["POST"]),
    Route("/dash/login", login_page, methods=["GET"]),
    Route("/dash/login", login_consume, methods=["POST"]),
    Route(f"{A}/today", today),
    Route(f"{A}/items", items),
    Route(f"{A}/attendance", attendance),
    Route(f"{A}/timetable", timetable),
    Route(f"{A}/marks", set_mark, methods=["POST"]),
    Route(f"{A}/entries", list_entries, methods=["GET"]),
    Route(f"{A}/entries", add_entry, methods=["POST"]),
    Route(A + "/entries/{eid:int}", del_entry, methods=["DELETE"]),
    Route(A + "/exams/{eid:int}", del_exam, methods=["DELETE"]),
    Route(f"{A}/sync", sync, methods=["POST"]),
    Route(f"{A}/health", health),
    Route(f"{A}/settings", get_settings, methods=["GET"]),
    Route(f"{A}/settings", put_settings, methods=["POST"]),
    Route(f"{A}/grades", grades, methods=["GET"]),
    Route(f"{A}/grades/plan", grades_plan, methods=["POST"]),
    Route("/dash/ics/{token}.ics", ics),
    Route("/dash/widget/{token}.json", widget),
]
if DIST.is_dir():
    from . import v3  # noqa: E402 - needs the helpers above

routes[0:0] = v3.build(sys.modules[__name__])
routes.append(Mount("/dash", SPAStatic(directory=DIST, html=True), name="web"))

app = Headers(Starlette(routes=routes, exception_handlers={HTTPException: http_error}))


def _cli() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mint-cookie", action="store_true", help="print a session cookie (local testing)")
    ap.add_argument("--serve", action="store_true", help="serve on 127.0.0.1:8770 (local testing only)")
    a = ap.parse_args()
    if a.mint_cookie:
        print(f"{COOKIE}={_make_session(OWNER_ID)}")
    if a.serve:
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=8770, log_level="warning")


if __name__ == "__main__":
    _cli()
