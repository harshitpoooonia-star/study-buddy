"""Ask - chat box on the dashboard, same free Groq path as the Telegram bot.

Runs inside the moodle-mcp process; reaches the MCP tools over localhost HTTP like the bot does, plus
two local tools that read the dashboard's own database (indexed course material, deadlines/notes).
The daily quota is shared with the bot (table bot_ai_usage, AI_DAILY_LIMIT, default 40).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from .common import ENV, MCP_URL, db, now_local

GROQ_KEY = ENV.get("GROQ_API_KEY", "")
GROQ_URL = ENV.get("GROQ_URL", "https://api.groq.com/openai/v1/chat/completions")
MODELS = [m for m in (ENV.get("GROQ_MODEL", "openai/gpt-oss-120b"), ENV.get("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")) if m]
LIMIT = int(ENV.get("AI_DAILY_LIMIT", "40"))
OWNER = int(ENV["OWNER_CHAT_ID"]) if ENV.get("OWNER_CHAT_ID", "").lstrip("-").isdigit() else 0

TOOL_NAMES = [
    "moodle_semester_overview", "moodle_get_upcoming_deadlines", "moodle_list_assignments",
    "moodle_get_assignment_status", "moodle_list_quizzes", "moodle_get_grades", "moodle_list_courses",
    "moodle_get_announcements", "moodle_get_notifications", "mycamu_get_attendance",
    "mycamu_get_timetable", "mycamu_get_exam_schedule", "mycamu_get_announcements", "moodle_read_file",
]
RESULT_CHARS = 3500
FILE_CHARS = 6000
_lock = asyncio.Lock()   # one question at a time: keeps the free tier's per-minute budget safe

LOCAL_TOOLS = [
    {"type": "function", "function": {
        "name": "search_course_content",
        "description": "Full-text search over the student's indexed course material (slide/notes/PDF text, activity "
                       "names, announcements). Fast and free - try this FIRST for syllabus, lecture, formula or "
                       "'where is X' questions. Returns snippets plus file URLs for moodle_read_file.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Keywords, e.g. 'pumping lemma' or 'midterm syllabus'."},
            "course": {"type": "string", "description": "Optional course code, e.g. CSET302."}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_my_plan",
        "description": "The student's upcoming deadlines/exams (next 21 days), own to-dos and notes, and attendance "
                       "risk, straight from the dashboard database.",
        "parameters": {"type": "object", "properties": {}}}},
]


class AskError(Exception):
    pass


# --------------------------------------------------------------------------- quota (shared with the bot)


def used_today() -> int:
    con = db()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS bot_ai_usage (chat_id INTEGER, day TEXT, n INTEGER, PRIMARY KEY (chat_id, day))")
        r = con.execute("SELECT n FROM bot_ai_usage WHERE chat_id=? AND day=?", (OWNER, now_local().strftime("%Y-%m-%d"))).fetchone()
        return r[0] if r else 0
    finally:
        con.close()


def bump() -> None:
    con = db()
    try:
        con.execute("INSERT INTO bot_ai_usage(chat_id, day, n) VALUES(?,?,1) ON CONFLICT(chat_id, day) DO UPDATE SET n=n+1",
                    (OWNER, now_local().strftime("%Y-%m-%d")))
    finally:
        con.close()


# --------------------------------------------------------------------------- tools


def _match_query(q: str) -> str:
    toks = [t for t in re.findall(r"[\w+#]{2,}", q.lower())][:8]
    return " ".join(f'"{t}"*' for t in toks)


def local_search(q: str, course: str = "", limit: int = 6) -> list[dict]:
    from . import common
    con = db()
    try:
        out = []
        fts = _match_query(q)
        if not fts:
            return []
        args: list[Any] = [fts]
        sql = ("SELECT d.kind, d.course, d.title, d.url, snippet(docs_fts, 1, '\x01', '\x02', ' … ', 18) AS snip "
               "FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid WHERE docs_fts MATCH ?")
        if course:
            sql += " AND d.course = ?"
            args.append(course.upper())
        sql += " ORDER BY bm25(docs_fts, 6.0, 1.0) LIMIT ?"
        args.append(limit)
        if common.HAS_FTS:
            try:
                out = [dict(r) for r in con.execute(sql, args)]
            except Exception:  # noqa: BLE001 - malformed MATCH expression
                out = []
        if not out:
            like = f"%{q.strip()[:60]}%"
            out = [dict(r, snip="") for r in con.execute(
                "SELECT kind, course, title, url FROM docs WHERE (title LIKE ? OR body LIKE ?)"
                + (" AND course=?" if course else "") + " LIMIT ?", ([like, like] + ([course.upper()] if course else []) + [limit]))]
        return out
    finally:
        con.close()


def _search_tool(args: dict) -> str:
    rows = local_search(str(args.get("query") or ""), str(args.get("course") or ""))
    if not rows:
        return "No indexed course material matches. Try different keywords, or search_course_materials is unavailable here; use moodle_get_course_contents/moodle_list_courses."
    lines = []
    for r in rows:
        snip = (r.get("snip") or "").replace("\x01", "").replace("\x02", "")
        lines.append(f"- [{r['kind']}] {r['course']} · {r['title']}" + (f"\n  …{snip}" if snip else "")
                     + (f"\n  url: {r['url']}" if r.get("url") else ""))
    return "\n".join(lines)


def _plan_tool() -> str:
    from . import app as A
    now = now_local().timestamp()
    lines = ["Upcoming (next 21 days):"]
    for i in A._all_items(7):
        if i["done"] or not i.get("due_at") or i["due_at"] > now + 21 * 86400:
            continue
        when = A.datetime.fromtimestamp(i["due_at"], A.TZ).strftime("%a %d %b %H:%M")
        lines.append(f"- {when} · {i.get('course_code') or ''} · {i['type']} · {i['title']}" + (" (OVERDUE)" if i["due_at"] < now else ""))
    con = db()
    todos = con.execute("SELECT course_code, kind, text, done FROM notes ORDER BY done, id DESC LIMIT 40").fetchall()
    con.close()
    if todos:
        lines.append("Own notes/to-dos:")
        lines += [f"- [{'x' if t['done'] else ' '}] {t['course_code'] or '-'} · {t['kind']} · {t['text'][:200]}" for t in todos]
    lines.append("Attendance (75% rule):")
    for a in A._attendance_latest():
        lines.append(f"- {a['subject_code']} {a['subject']}: {a['pct']}% ({a['present']}/{a['total']})"
                     + (f", attend next {a['must_attend']}" if a["must_attend"] else f", can skip {a['can_skip']}"))
    return "\n".join(lines)


# --------------------------------------------------------------------------- MCP + Groq


def _clean(s: Any) -> Any:
    if isinstance(s, dict):
        return {k: _clean(v) for k, v in s.items() if k != "title"}
    if isinstance(s, list):
        return [_clean(x) for x in s]
    return s


class _Mcp:
    H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def __init__(self) -> None:
        self.n = 0
        self.c = httpx.AsyncClient(timeout=120, trust_env=False)

    async def rpc(self, method: str, params: dict) -> dict:
        self.n += 1
        r = await self.c.post(MCP_URL, headers=self.H, json={"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        r.raise_for_status()
        d = r.json()
        if "error" in d:
            raise AskError(str(d["error"].get("message", d["error"])))
        return d.get("result", {})

    async def call(self, name: str, **args: Any) -> str:
        res = await self.rpc("tools/call", {"name": name, "arguments": args})
        return "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text").strip()

    async def tools(self) -> list[dict]:
        out = []
        for t in (await self.rpc("tools/list", {})).get("tools", []):
            if t["name"] not in TOOL_NAMES:
                continue
            schema = _clean(t.get("inputSchema") or {"type": "object", "properties": {}})
            for k in ("response_format", "save_copy", "max_chars"):
                schema.get("properties", {}).pop(k, None)
            desc = re.sub(r"\s+", " ", (t.get("description") or "").split("\n\n")[0]).strip()[:300]
            out.append({"type": "function", "function": {"name": t["name"], "description": desc, "parameters": schema}})
        return out


async def _groq(model: str, messages: list[dict], tools: list[dict]) -> dict:
    body: dict[str, Any] = {"model": model, "messages": messages, "tools": tools, "tool_choice": "auto",
                            "temperature": 0.2, "max_completion_tokens": 1100}
    if "gpt-oss" in model:
        body["reasoning_effort"] = "low"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(GROQ_URL,
                         headers={"Authorization": f"Bearer {GROQ_KEY}"}, json=body)
    if r.status_code == 429:
        raise AskError("The free AI tier is busy right now - try again in a minute.")
    if r.status_code >= 400:
        raise AskError(f"AI service error (HTTP {r.status_code}).")
    return r.json()["choices"][0]["message"]


def _system() -> str:
    return (
        f"You are Study Buddy inside the student's dashboard ({ENV.get('STUDENT_PROGRAMME', 'B.Tech')} student, Bennett University, India). "
        f"Now: {now_local():%A %d %B %Y, %I:%M %p} IST. Use the tools; never invent deadlines, marks, classes, "
        f"attendance or course content. If a tool errors or finds nothing, say so. {ENV.get('ATTENDANCE_TARGET', '75')}% attendance minimum per subject. "
        "For syllabus, lecture, formula or 'where is X' questions call search_course_content first, then "
        "moodle_read_file on the best file URL and answer from its text. For 'what should I do / what's due' use "
        "get_my_plan. For a timetable on one day call mycamu_get_timetable with start_date=YYYY-MM-DD and days_ahead=0. "
        "Reply in short Markdown: bullets, **bold** for key items, no tables, no headings. "
        "Dates like 'Thu 15 Oct, 11:59 PM'. Reply in the user's language."
    )


async def ask(question: str, history: list[dict]) -> dict:
    """-> {answer, tools:[names used], used, limit}. Raises AskError with a message safe to show."""
    if not GROQ_KEY:
        raise AskError("Ask is off: add GROQ_API_KEY (free at console.groq.com/keys) to .env on the VPS and restart moodle-mcp.")
    if await asyncio.to_thread(used_today) >= LIMIT:
        raise AskError(f"Daily AI limit reached ({LIMIT}). It resets at midnight IST; search and every page still work.")
    async with _lock:
        mcp = _Mcp()
        try:
            tools = await mcp.tools() + LOCAL_TOOLS
        except (httpx.HTTPError, AskError) as e:
            await mcp.c.aclose()
            raise AskError("Couldn't reach the LMS tools just now.") from e
        allowed = {t["function"]["name"]: set(t["function"]["parameters"].get("properties", {})) for t in tools}
        used_tools: list[str] = []
        last: Exception | None = None
        hist = [{"role": h["role"], "content": str(h.get("content", ""))[:900]} for h in history[-6:]
                if h.get("role") in ("user", "assistant")]
        try:
            for model in MODELS:
                messages = [{"role": "system", "content": _system()}, *hist, {"role": "user", "content": question[:1500]}]
                try:
                    for _ in range(5):
                        msg = await _groq(model, messages, tools)
                        calls = msg.get("tool_calls") or []
                        if not calls:
                            ans = (msg.get("content") or "").strip()
                            if not ans:
                                raise AskError("The AI returned an empty answer.")
                            await asyncio.to_thread(bump)
                            return {"answer": ans, "tools": used_tools, "used": await asyncio.to_thread(used_today), "limit": LIMIT}
                        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
                        for call in calls:
                            fn = call.get("function", {})
                            name = fn.get("name", "")
                            try:
                                args = json.loads(fn.get("arguments") or "{}") or {}
                            except ValueError:
                                args = {}
                            cap = RESULT_CHARS
                            try:
                                if name == "search_course_content":
                                    res = await asyncio.to_thread(_search_tool, args)
                                elif name == "get_my_plan":
                                    res = await asyncio.to_thread(_plan_tool)
                                elif name in allowed:
                                    args = {k: v for k, v in args.items() if k in allowed[name] and v is not None}
                                    if name == "moodle_read_file":
                                        args["max_chars"], cap = FILE_CHARS, FILE_CHARS
                                    res = await mcp.call(name, **args)
                                else:
                                    res = f"Error: unknown tool {name}"
                            except (httpx.HTTPError, AskError) as e:
                                res = f"Error: tool failed ({type(e).__name__})"
                            if name not in used_tools:
                                used_tools.append(name)
                            messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                             "content": res[:cap] + ("\n…[truncated]" if len(res) > cap else "")})
                    raise AskError("The AI needed too many steps - try a more specific question.")
                except AskError as e:
                    last = e
                    continue
                except httpx.HTTPError as e:
                    last = e
                    continue
            raise AskError(str(last) if isinstance(last, AskError) else "Couldn't reach the AI service.")
        finally:
            await mcp.c.aclose()
