#!/usr/bin/env python3
"""
moodle_mcp: an MCP server for Moodle LMS (built for Bennett University's
lms.bennett.edu.in, works with any Moodle 3.9+ site that has mobile web services on).

It talks to Moodle's official web-service REST API -- the same one the Moodle mobile
app uses -- with a personal token. It never sees or stores your password.

Environment:
  MOODLE_URL    Site base URL            (default: https://lms.bennett.edu.in)
  MOODLE_TOKEN  Personal web-service token (required; get one with get_token.py)
  MOODLE_TZ     Timezone for displayed dates (default: Asia/Kolkata)
  MOODLE_DOWNLOAD_DIR  Where downloaded course files go (default: ./lms_downloads)

Run:
  python moodle_mcp.py                       # stdio (desktop MCP clients)
  python moodle_mcp.py --http --port 8765    # streamable HTTP (VPS)
"""

from __future__ import annotations

# --- startup diagnostics: record crashes/launches in server.log next to this file ---
import sys as _sys
import traceback as _tb
from pathlib import Path as _P

_LOG = _P(__file__).with_name("server.log")


def _log(msg: str) -> None:
    try:
        import datetime as _dt
        with _LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{_dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def _excepthook(t, v, tb):
    _log("CRASH:\n" + "".join(_tb.format_exception(t, v, tb)))
    _sys.__excepthook__(t, v, tb)


_sys.excepthook = _excepthook
_log(f"launch: python {_sys.version.split()[0]} exe={_sys.executable} cwd={_P.cwd()}")

import argparse
import asyncio
import html
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import logging

import httpx
from pydantic import Field

# httpx logs full request URLs at INFO; file URLs carry the token, so keep it quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a .env next to this file (real env vars win)."""
    env = Path(__file__).with_name(".env")
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

MOODLE_URL = os.environ.get("MOODLE_URL", "https://lms.bennett.edu.in").rstrip("/")
MOODLE_TOKEN = os.environ.get("MOODLE_TOKEN", "").strip()
def _load_tz() -> Any:
    """Windows has no system tz database; fall back to IST if `tzdata` isn't installed."""
    name = os.environ.get("MOODLE_TZ", "Asia/Kolkata")
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=5, minutes=30), "IST")


TZ = _load_tz()
DOWNLOAD_DIR = Path(os.environ.get("MOODLE_DOWNLOAD_DIR", "./lms_downloads")).resolve()
REST_ENDPOINT = f"{MOODLE_URL}/webservice/rest/server.php"
TIMEOUT = 45.0
MAX_TEXT_CHARS = 60_000
CHARACTER_LIMIT = 25_000  # cap on any single tool response

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)

mcp = MCPServer(
    name="moodle_mcp",
    instructions=(
        "Tools for a student's Moodle LMS. Start with moodle_semester_overview for "
        "'what's due' questions, moodle_list_courses to get course IDs, then drill in with "
        "moodle_get_course_contents / moodle_list_assignments / moodle_get_assignment_status. "
        "Use moodle_read_file to read lecture slides, PDFs and assignment briefs. "
        "For attendance, class timetable, exam schedule, results, university announcements and "
        "ERP notifications use the mycamu_* tools (Bennett's MyCamu ERP); if they report an "
        "expired session, tell the user to run ~/moodle-mcp/set-camu-cookie on the server. "
        "All times are shown in the student's local timezone."
    ),
)

# Cached per-process: user id + list of functions the site allows.
_site_info: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Moodle client
# ---------------------------------------------------------------------------


class MoodleError(Exception):
    def __init__(self, errorcode: str, message: str):
        super().__init__(message)
        self.errorcode = errorcode
        self.message = message


def _flatten(prefix: str, value: Any, out: list[tuple[str, str]]) -> None:
    """Encode nested params Moodle-style: courseids[0]=5, options[0][name]=x."""
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}[{k}]" if prefix else str(k), v, out)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _flatten(f"{prefix}[{i}]", v, out)
    elif isinstance(value, bool):
        out.append((prefix, "1" if value else "0"))
    elif value is not None:
        out.append((prefix, str(value)))


async def call(wsfunction: str, **params: Any) -> Any:
    """Call a Moodle web-service function and return parsed JSON."""
    if not MOODLE_TOKEN:
        raise MoodleError(
            "notoken",
            "MOODLE_TOKEN is not set. Run `python get_token.py` once to create a token, "
            "then add it to this server's environment.",
        )
    form: list[tuple[str, str]] = [
        ("wstoken", MOODLE_TOKEN),
        ("wsfunction", wsfunction),
        ("moodlewsrestformat", "json"),
    ]
    _flatten("", params, form)
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.post(
            REST_ENDPOINT,
            content=urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:
        raise MoodleError(
            "badresponse",
            "The LMS returned a non-JSON page. Web services may be disabled on this site, "
            "or MOODLE_URL is wrong.",
        ) from exc
    if isinstance(data, dict) and "exception" in data:
        raise MoodleError(data.get("errorcode", "unknown"), data.get("message", "Unknown error"))
    return data


def _error(e: Exception) -> str:
    if isinstance(e, MoodleError):
        hints = {
            "invalidtoken": "The token is invalid or expired. Generate a new one with get_token.py.",
            "accessexception": "This token isn't allowed to call that function. The site may restrict "
            "the mobile web service; ask LMS admins to enable it.",
            "servicenotavailable": "Web services are disabled on this LMS. Ask IT to enable "
            "'Mobile web services' (the Moodle app uses the same switch).",
            "invalidrecord": "That ID doesn't exist or you can't see it. Re-check IDs with "
            "moodle_list_courses / moodle_get_course_contents.",
            "requireloginerror": "Session/token rejected. Regenerate the token.",
            "notoken": "",
        }
        hint = hints.get(e.errorcode, "")
        return f"Error ({e.errorcode}): {e.message}" + (f"\nHint: {hint}" if hint else "")
    if isinstance(e, httpx.HTTPStatusError):
        return f"Error: LMS returned HTTP {e.response.status_code}. The site may be down; try again later."
    if isinstance(e, httpx.TimeoutException):
        return "Error: The LMS took too long to respond. Try again, or narrow the request."
    if isinstance(e, httpx.ConnectError):
        return f"Error: Could not reach {MOODLE_URL}. Check your internet/VPN and MOODLE_URL."
    return f"Error: {type(e).__name__}: {e}"


async def site_info() -> dict[str, Any]:
    global _site_info
    if _site_info is None:
        _site_info = await call("core_webservice_get_site_info")
    return _site_info


async def my_userid() -> int:
    return int((await site_info())["userid"])


async def my_course_ids() -> list[int]:
    courses = await call("core_enrol_get_users_courses", userid=await my_userid())
    return [c["id"] for c in courses]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def fmt_ts(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), TZ).strftime("%a %d %b %Y, %I:%M %p")


def rel(ts: int | None) -> str:
    if not ts:
        return ""
    delta = int(ts) - int(time.time())
    days = abs(delta) / 86400
    if days < 1:
        hrs = max(1, round(abs(delta) / 3600))
        span = f"{hrs}h"
    else:
        span = f"{days:.0f}d" if days >= 2 else f"{days:.1f}d"
    return f"in {span}" if delta >= 0 else f"{span} ago"


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str | None, limit: int | None = None) -> str:
    if not s:
        return ""
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "• ", s)
    s = html.unescape(_TAG_RE.sub("", s))
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rstrip() + "…"
    return s


def clip(text: str) -> str:
    if len(text) <= CHARACTER_LIMIT:
        return text
    return (
        text[:CHARACTER_LIMIT]
        + f"\n\n…[truncated at {CHARACTER_LIMIT} chars - narrow the request, e.g. filter by course_id]"
    )


def as_json(obj: Any) -> str:
    return clip(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def with_token(fileurl: str) -> str:
    """Moodle file URLs need the token and the webservice/pluginfile.php path. URLs from the
    web-service API already have /webservice/ (rewrite only plain /pluginfile.php/ links)."""
    parts = urlparse(fileurl)
    path = parts.path
    if "/webservice/pluginfile.php/" not in path and "/tokenpluginfile.php/" not in path:
        path = path.replace("/pluginfile.php/", "/webservice/pluginfile.php/", 1)
    parts = parts._replace(path=path)
    q = parse_qs(parts.query)
    q["token"] = [MOODLE_TOKEN]
    return urlunparse(parts._replace(query=urlencode(q, doseq=True)))


Fmt = Annotated[
    str,
    Field(
        pattern="^(markdown|json)$",
        description="'markdown' (readable, default) or 'json' (full structured data).",
    ),
]
CourseId = Annotated[int, Field(ge=1, description="Moodle course ID (from moodle_list_courses).")]


# ---------------------------------------------------------------------------
# Tools: identity & courses
# ---------------------------------------------------------------------------


@mcp.tool(name="moodle_whoami", annotations=READ_ONLY)
async def moodle_whoami() -> str:
    """Check the LMS connection and show who is logged in.

    Returns the student's name, Moodle user ID, site name/version, and how many web-service
    functions the token can use. Run this first if other tools error out.
    """
    try:
        info = await site_info()
        return (
            f"Connected to **{info.get('sitename')}** ({MOODLE_URL})\n"
            f"- User: {info.get('fullname')} (username `{info.get('username')}`, id {info.get('userid')})\n"
            f"- Moodle release: {info.get('release', '?')}\n"
            f"- Functions available to this token: {len(info.get('functions', []))}"
        )
    except Exception as e:
        return _error(e)


@mcp.tool(name="moodle_list_courses", annotations=READ_ONLY)
async def moodle_list_courses(
    include_past: Annotated[
        bool, Field(description="Include courses that have already ended (default false).")
    ] = False,
    response_format: Fmt = "markdown",
) -> str:
    """List the student's enrolled courses with IDs, codes, progress and dates.

    Use the returned course IDs with every other course-specific tool.
    Courses whose end date has passed are hidden unless include_past=true.
    """
    try:
        courses = await call("core_enrol_get_users_courses", userid=await my_userid())
        now = time.time()
        if not include_past:
            courses = [c for c in courses if not c.get("enddate") or c["enddate"] > now]
        courses.sort(key=lambda c: (c.get("fullname") or "").lower())
        if response_format == "json":
            return as_json(
                [
                    {
                        k: c.get(k)
                        for k in (
                            "id", "shortname", "fullname", "idnumber", "category",
                            "progress", "completed", "startdate", "enddate", "lastaccess",
                        )
                    }
                    for c in courses
                ]
            )
        if not courses:
            return "No current courses found. Try include_past=true."
        lines = [f"# Courses ({len(courses)})", ""]
        for c in courses:
            prog = c.get("progress")
            prog_s = f" · progress {prog:.0f}%" if isinstance(prog, (int, float)) else ""
            lines.append(f"- **{c['fullname']}** - `{c.get('shortname', '')}` (id {c['id']}){prog_s}")
        return "\n".join(lines)
    except Exception as e:
        return _error(e)


@mcp.tool(name="moodle_get_course_contents", annotations=READ_ONLY)
async def moodle_get_course_contents(
    course_id: CourseId,
    section_filter: Annotated[
        str | None,
        Field(description="Only sections whose name contains this text (case-insensitive), e.g. 'Week 5'."),
    ] = None,
    include_descriptions: Annotated[
        bool, Field(description="Include section summaries and activity descriptions.")
    ] = True,
    response_format: Fmt = "markdown",
) -> str:
    """Get a course's full structure: sections, activities (assignments, quizzes, forums, pages,
    URLs), and every attached file with its file URL.

    Pass a file URL to moodle_read_file to read slides/PDFs/docs. Hidden or restricted items
    the student can't see are not returned by Moodle.
    """
    try:
        sections = await call("core_course_get_contents", courseid=course_id)
        if section_filter:
            sf = section_filter.lower()
            sections = [s for s in sections if sf in (s.get("name") or "").lower()]
        if response_format == "json":
            # Slim rows (the raw payload carries huge per-file blobs and would be clipped).
            slim = []
            for s in sections:
                mods = []
                for m in s.get("modules", []):
                    if m.get("uservisible") is False:
                        continue
                    mods.append({
                        "id": m.get("id"), "instance": m.get("instance"), "modname": m.get("modname"),
                        "name": m.get("name"), "url": m.get("url"),
                        "description": strip_html(m.get("description"), 600) if include_descriptions and m.get("description") else "",
                        "dates": [{"label": d.get("label"), "timestamp": d.get("timestamp")} for d in (m.get("dates") or [])],
                        "files": [{"filename": f.get("filename"), "filesize": f.get("filesize"), "fileurl": f.get("fileurl"),
                                   "timemodified": f.get("timemodified")} for f in (m.get("contents") or []) if f.get("type") == "file"],
                        "links": [f.get("fileurl") for f in (m.get("contents") or []) if f.get("type") == "url"],
                    })
                if mods or s.get("summary"):
                    slim.append({"section": s.get("section"), "name": s.get("name"),
                                 "summary": strip_html(s.get("summary"), 600) if include_descriptions else "", "modules": mods})
            return json.dumps(slim, ensure_ascii=False, default=str)  # not clipped: the dashboard parses it
        lines: list[str] = []
        for s in sections:
            mods = s.get("modules", [])
            summ = strip_html(s.get("summary"), 400) if include_descriptions else ""
            if not mods and not summ:
                continue
            lines.append(f"## {s.get('name') or 'Section ' + str(s.get('section'))}")
            if summ:
                lines.append(summ)
            for m in mods:
                if m.get("uservisible") is False:
                    continue
                badge = m.get("modname", "")
                lines.append(f"- [{badge}] **{m.get('name')}** (cmid {m.get('id')}, instance {m.get('instance')})")
                dates = m.get("dates") or []
                for d in dates:
                    lines.append(f"  - {d.get('label', '').rstrip(':')}: {fmt_ts(d.get('timestamp'))}")
                if include_descriptions and m.get("description"):
                    lines.append("  > " + strip_html(m["description"], 300).replace("\n", "\n  > "))
                for f in m.get("contents") or []:
                    if f.get("type") == "file":
                        size = f.get("filesize") or 0
                        lines.append(
                            f"  - 📄 {f.get('filename')} ({size // 1024} KB) - `{f.get('fileurl')}`"
                        )
                    elif f.get("type") == "url":
                        lines.append(f"  - 🔗 {f.get('fileurl')}")
        if not lines:
            return "No visible content found" + (f" matching '{section_filter}'." if section_filter else ".")
        return clip("\n".join(lines))
    except Exception as e:
        return _error(e)


# ---------------------------------------------------------------------------
# Tools: deadlines, assignments, quizzes
# ---------------------------------------------------------------------------


@mcp.tool(name="moodle_get_upcoming_deadlines", annotations=READ_ONLY)
async def moodle_get_upcoming_deadlines(
    days_ahead: Annotated[int, Field(ge=1, le=180, description="Look this many days ahead.")] = 30,
    include_overdue_days: Annotated[
        int, Field(ge=0, le=60, description="Also include items due in the last N days (overdue).")
    ] = 7,
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
    response_format: Fmt = "markdown",
) -> str:
    """List everything with a due date across all courses - assignments, quizzes, forum
    deadlines, etc. - from the LMS timeline, sorted soonest first.

    Items flagged 'action required' are ones Moodle thinks you still need to do (e.g. not yet
    submitted). Use moodle_get_assignment_status for submission details on a specific item.
    """
    try:
        now = int(time.time())
        data = await call(
            "core_calendar_get_action_events_by_timesort",
            timesortfrom=now - include_overdue_days * 86400,
            timesortto=now + days_ahead * 86400,
            limitnum=limit,
        )
        events = data.get("events", [])
        if response_format == "json":
            return as_json(
                [
                    {
                        "name": e.get("name"),
                        "activity": e.get("activityname"),
                        "type": e.get("modulename"),
                        "course": (e.get("course") or {}).get("fullname"),
                        "course_id": (e.get("course") or {}).get("id"),
                        "due": e.get("timesort"),
                        "due_local": fmt_ts(e.get("timesort")),
                        "overdue": e.get("overdue"),
                        "action_required": bool((e.get("action") or {}).get("actionable")),
                        "action": (e.get("action") or {}).get("name"),
                        "url": e.get("url"),
                        "instance": e.get("instance"),
                    }
                    for e in events
                ]
            )
        if not events:
            return f"Nothing due in the next {days_ahead} days. 🎉"
        lines = [f"# Deadlines (next {days_ahead} days)", ""]
        for e in events:
            ts = e.get("timesort")
            course = (e.get("course") or {}).get("fullname", "")
            act = e.get("action") or {}
            flag = " ⚠️ OVERDUE" if e.get("overdue") else ""
            todo = f" - _{act.get('name')}_" if act.get("actionable") else ""
            lines.append(
                f"- **{fmt_ts(ts)}** ({rel(ts)}){flag} · {course} · "
                f"[{e.get('modulename', '')}] {e.get('activityname') or e.get('name')}{todo}"
            )
        return "\n".join(lines)
    except Exception as e:
        return _error(e)


@mcp.tool(name="moodle_list_assignments", annotations=READ_ONLY)
async def moodle_list_assignments(
    course_id: Annotated[
        int | None, Field(ge=1, description="Limit to one course. Omit for all current courses.")
    ] = None,
    upcoming_only: Annotated[
        bool, Field(description="Only assignments whose due date hasn't passed (or has no due date).")
    ] = False,
    include_brief: Annotated[
        bool, Field(description="Include the assignment instructions text and attached brief files.")
    ] = True,
    response_format: Fmt = "markdown",
) -> str:
    """List assignments with due date, cut-off date, max grade, instructions and attached
    brief files. Returns the assignment ID needed by moodle_get_assignment_status.
    """
    try:
        ids = [course_id] if course_id else await my_course_ids()
        if not ids:
            return "No courses found."
        data = await call("mod_assign_get_assignments", courseids=ids)
        now = time.time()
        rows: list[dict[str, Any]] = []
        for c in data.get("courses", []):
            for a in c.get("assignments", []):
                due = a.get("duedate") or 0
                if upcoming_only and due and due < now:
                    continue
                rows.append(
                    {
                        "assign_id": a["id"],
                        "cmid": a.get("cmid"),
                        "course": c.get("fullname"),
                        "course_id": c.get("id"),
                        "name": a.get("name"),
                        "opens": a.get("allowsubmissionsfromdate"),
                        "due": due,
                        "cutoff": a.get("cutoffdate"),
                        "grading_due": a.get("gradingduedate"),
                        "max_grade": a.get("grade"),
                        "team_submission": bool(a.get("teamsubmission")),
                        "intro": strip_html(a.get("intro"), 1500) if include_brief else None,
                        "files": [
                            {"name": f.get("filename"), "url": f.get("fileurl")}
                            for f in (a.get("introattachments") or [])
                        ]
                        if include_brief
                        else None,
                    }
                )
        rows.sort(key=lambda r: (r["due"] or 10**12))
        if response_format == "json":
            return as_json(rows)
        if not rows:
            return "No assignments found."
        lines = [f"# Assignments ({len(rows)})", ""]
        for r in rows:
            lines.append(f"## {r['name']}  ·  {r['course']}")
            lines.append(f"- assign_id **{r['assign_id']}** (cmid {r['cmid']})")
            lines.append(f"- Due: **{fmt_ts(r['due'])}** {('(' + rel(r['due']) + ')') if r['due'] else ''}")
            if r["cutoff"]:
                lines.append(f"- Hard cut-off: {fmt_ts(r['cutoff'])}")
            if r["max_grade"]:
                lines.append(f"- Max grade: {r['max_grade']}")
            if r["team_submission"]:
                lines.append("- Group submission")
            if r.get("intro"):
                lines.append("- Brief: " + r["intro"].replace("\n", "\n  "))
            for f in r.get("files") or []:
                lines.append(f"- 📄 {f['name']} - `{f['url']}`")
            lines.append("")
        return clip("\n".join(lines))
    except Exception as e:
        return _error(e)


@mcp.tool(name="moodle_get_assignment_status", annotations=READ_ONLY)
async def moodle_get_assignment_status(
    assign_id: Annotated[int, Field(ge=1, description="Assignment ID from moodle_list_assignments.")],
    response_format: Fmt = "markdown",
) -> str:
    """Show whether an assignment is submitted, its grading status, grade, teacher feedback,
    files you uploaded, time remaining, and any extension.
    """
    try:
        data = await call("mod_assign_get_submission_status", assignid=assign_id)
        if response_format == "json":
            return as_json(data)
        last = data.get("lastattempt") or {}
        sub = last.get("submission") or (last.get("teamsubmission") or {})
        fb = data.get("feedback") or {}
        lines = [f"# Assignment {assign_id}"]
        lines.append(f"- Submission status: **{sub.get('status', 'nothing submitted')}**")
        if sub.get("timemodified"):
            lines.append(f"- Last modified: {fmt_ts(sub['timemodified'])}")
        lines.append(f"- Grading status: {last.get('gradingstatus', '—')}")
        if last.get("extensionduedate"):
            lines.append(f"- Extension granted until: **{fmt_ts(last['extensionduedate'])}**")
        if last.get("locked"):
            lines.append("- 🔒 Submissions locked")
        if "cansubmit" in last:
            lines.append(f"- Can still submit: {'yes' if last.get('cansubmit') else 'no'}")
        for plugin in sub.get("plugins") or []:
            for area in plugin.get("fileareas") or []:
                for f in area.get("files") or []:
                    lines.append(f"- Your file: {f.get('filename')}")
            for ed in plugin.get("editorfields") or []:
                if ed.get("text"):
                    lines.append(f"- Online text: {strip_html(ed['text'], 300)}")
        if fb:
            grade = fb.get("gradefordisplay") or (fb.get("grade") or {}).get("grade")
            if grade:
                lines.append(f"- **Grade: {strip_html(str(grade))}**")
            if fb.get("gradeddate"):
                lines.append(f"- Graded on: {fmt_ts(fb['gradeddate'])}")
            for plugin in fb.get("plugins") or []:
                for ed in plugin.get("editorfields") or []:
                    if ed.get("text"):
                        lines.append(f"- Feedback: {strip_html(ed['text'], 1500)}")
        return "\n".join(lines)
    except Exception as e:
        return _error(e)


@mcp.tool(name="moodle_list_quizzes", annotations=READ_ONLY)
async def moodle_list_quizzes(
    course_id: Annotated[
        int | None, Field(ge=1, description="Limit to one course. Omit for all current courses.")
    ] = None,
    upcoming_only: Annotated[bool, Field(description="Only quizzes that haven't closed yet.")] = False,
    include_attempts: Annotated[
        bool, Field(description="Also fetch your attempts and best grade for each quiz (slower).")
    ] = True,
    response_format: Fmt = "markdown",
) -> str:
    """List quizzes/tests with open & close times, time limit, attempts allowed, and (optionally)
    your attempts and best grade.
    """
    try:
        ids = [course_id] if course_id else await my_course_ids()
        if not ids:
            return "No courses found."
        data = await call("mod_quiz_get_quizzes_by_courses", courseids=ids)
        quizzes = data.get("quizzes", [])
        now = time.time()
        if upcoming_only:
            quizzes = [q for q in quizzes if not q.get("timeclose") or q["timeclose"] > now]
        quizzes.sort(key=lambda q: q.get("timeclose") or q.get("timeopen") or 10**12)

        async def enrich(q: dict[str, Any]) -> dict[str, Any]:
            row = {
                "quiz_id": q["id"],
                "cmid": q.get("coursemodule"),
                "course_id": q.get("course"),
                "name": q.get("name"),
                "opens": q.get("timeopen"),
                "closes": q.get("timeclose"),
                "time_limit_min": round((q.get("timelimit") or 0) / 60) or None,
                "attempts_allowed": q.get("attempts") or "unlimited",
                "max_grade": q.get("grade"),
                "intro": strip_html(q.get("intro"), 500),
            }
            if include_attempts:
                try:
                    att = await call("mod_quiz_get_user_attempts", quizid=q["id"], status="all")
                    row["attempts"] = [
                        {
                            "attempt": a.get("attempt"),
                            "state": a.get("state"),
                            "finished": a.get("timefinish"),
                            "sumgrades": a.get("sumgrades"),
                        }
                        for a in att.get("attempts", [])
                    ]
                    best = await call("mod_quiz_get_user_best_grade", quizid=q["id"])
                    row["best_grade"] = best.get("grade") if best.get("hasgrade") else None
                except MoodleError:
                    row["attempts"] = None
            return row

        rows = await asyncio.gather(*(enrich(q) for q in quizzes))
        if response_format == "json":
            return as_json(rows)
        if not rows:
            return "No quizzes found."
        lines = [f"# Quizzes ({len(rows)})", ""]
        for r in rows:
            lines.append(f"## {r['name']}  (quiz_id {r['quiz_id']}, course {r['course_id']})")
            lines.append(f"- Opens: {fmt_ts(r['opens'])} · Closes: **{fmt_ts(r['closes'])}** {rel(r['closes'])}")
            if r["time_limit_min"]:
                lines.append(f"- Time limit: {r['time_limit_min']} min")
            lines.append(f"- Attempts allowed: {r['attempts_allowed']} · Max grade: {r['max_grade']}")
            if include_attempts and r.get("attempts") is not None:
                done = [a for a in r["attempts"] if a["state"] == "finished"]
                lines.append(
                    f"- Your attempts: {len(r['attempts'])} ({len(done)} finished)"
                    + (f" · best grade {r['best_grade']}" if r.get("best_grade") is not None else "")
                )
            lines.append("")
        return clip("\n".join(lines))
    except Exception as e:
        return _error(e)


# ---------------------------------------------------------------------------
# Tools: grades, forums, notifications
# ---------------------------------------------------------------------------


@mcp.tool(name="moodle_get_grades", annotations=READ_ONLY)
async def moodle_get_grades(
    course_id: Annotated[
        int | None,
        Field(ge=1, description="Course to show every grade item for. Omit for an overview of all courses."),
    ] = None,
    response_format: Fmt = "markdown",
) -> str:
    """Show grades. With course_id: every graded item (assignments, quizzes, etc.) with grade,
    range, percentage and feedback. Without: the course total for each course.
    """
    try:
        if course_id is None:
            data = await call("gradereport_overview_get_course_grades", userid=await my_userid())
            grades = data.get("grades", [])
            names = {
                c["id"]: c.get("fullname")
                for c in await call("core_enrol_get_users_courses", userid=await my_userid())
            }
            if response_format == "json":
                return as_json([{**g, "course": names.get(g.get("courseid"))} for g in grades])
            lines = ["# Course totals", ""]
            for g in grades:
                lines.append(f"- {names.get(g.get('courseid'), g.get('courseid'))}: **{g.get('grade') or '—'}**")
            return "\n".join(lines)

        data = await call("gradereport_user_get_grade_items", courseid=course_id, userid=await my_userid())
        items = (data.get("usergrades") or [{}])[0].get("gradeitems", [])
        if response_format == "json":
            return as_json(items)
        lines = [f"# Grades - course {course_id}", ""]
        for it in items:
            name = it.get("itemname") or ("Course total" if it.get("itemtype") == "course" else "Category")
            g = it.get("gradeformatted") or "—"
            rng = it.get("rangeformatted") or ""
            pct = it.get("percentageformatted") or ""
            line = f"- **{name}**: {g}" + (f" (range {rng})" if rng else "") + (f" · {pct}" if pct and pct != "-" else "")
            fb = strip_html(it.get("feedback"), 300)
            if fb:
                line += f"\n  - Feedback: {fb}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return _error(e)


@mcp.tool(name="moodle_get_announcements", annotations=READ_ONLY)
async def moodle_get_announcements(
    course_id: CourseId,
    limit: Annotated[int, Field(ge=1, le=50, description="Discussions per forum.")] = 10,
    include_message: Annotated[bool, Field(description="Include each post's first message text.")] = True,
    response_format: Fmt = "markdown",
) -> str:
    """Read a course's forums - the Announcements/News forum where faculty post deadlines,
    test notices and schedule changes, plus any discussion forums. Newest first.
    """
    try:
        forums = await call("mod_forum_get_forums_by_courses", courseids=[course_id])
        # Announcements ('news') first.
        forums.sort(key=lambda f: 0 if f.get("type") == "news" else 1)
        out: list[dict[str, Any]] = []
        for f in forums:
            try:
                d = await call(
                    "mod_forum_get_forum_discussions",
                    forumid=f["id"], sortorder=1, page=0, perpage=limit,
                )
            except MoodleError:
                continue
            for disc in d.get("discussions", []):
                out.append(
                    {
                        "forum": f.get("name"),
                        "forum_type": f.get("type"),
                        "subject": disc.get("subject") or disc.get("name"),
                        "author": disc.get("userfullname"),
                        "posted": disc.get("created") or disc.get("timemodified"),
                        "message": strip_html(disc.get("message"), 1200) if include_message else None,
                        "attachments": [
                            {"name": a.get("filename"), "url": a.get("fileurl")}
                            for a in (disc.get("attachments") or [])
                        ],
                    }
                )
        if response_format == "json":
            return as_json(out)
        if not out:
            return "No forum posts found in this course."
        lines = []
        cur = None
        for p in out:
            if p["forum"] != cur:
                cur = p["forum"]
                lines.append(f"# {cur}")
            lines.append(f"## {p['subject']}\n_{p['author']} · {fmt_ts(p['posted'])}_")
            if p.get("message"):
                lines.append(p["message"])
            for a in p["attachments"]:
                lines.append(f"- 📎 {a['name']} - `{a['url']}`")
            lines.append("")
        return clip("\n".join(lines))
    except Exception as e:
        return _error(e)


@mcp.tool(name="moodle_get_notifications", annotations=READ_ONLY)
async def moodle_get_notifications(
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    unread_only: Annotated[bool, Field(description="Only notifications you haven't read.")] = False,
    response_format: Fmt = "markdown",
) -> str:
    """Show recent LMS notifications: new grades, feedback, assignment reminders, forum posts,
    due-date changes. Does not mark them as read.
    """
    try:
        data = await call(
            "message_popup_get_popup_notifications",
            useridto=await my_userid(), newestfirst=1, limit=limit, offset=0,
        )
        notes = data.get("notifications", [])
        if unread_only:
            notes = [n for n in notes if not n.get("read")]
        if response_format == "json":
            # Slim rows: fullmessagehtml/customdata are ~4 KB each and blow the reply size limit.
            keep = ("id", "subject", "smallmessage", "contexturl", "contexturlname", "timecreated",
                    "read", "component", "eventtype")
            return json.dumps([{**{k: n.get(k) for k in keep},
                                "fullmessage": (n.get("fullmessage") or "")[:1200]} for n in notes],
                              ensure_ascii=False, default=str)  # unclipped: callers parse it
        if not notes:
            return "No notifications."
        lines = [f"# Notifications ({len(notes)})", ""]
        for n in notes:
            dot = "🔵 " if not n.get("read") else ""
            lines.append(f"- {dot}**{n.get('subject')}** - {fmt_ts(n.get('timecreated'))}")
            sm = strip_html(n.get("smallmessage") or n.get("fullmessage"), 250)
            if sm and sm != n.get("subject"):
                lines.append(f"  - {sm}")
        return "\n".join(lines)
    except Exception as e:
        return _error(e)


# ---------------------------------------------------------------------------
# Tools: files
# ---------------------------------------------------------------------------


def _pdftotext(data: bytes) -> str | None:
    """Text via `pdftotext` (poppler-utils) with a 160 MB address-space cap and 60 s timeout; None if unavailable."""
    import shutil
    import subprocess

    exe = shutil.which("pdftotext")
    if not exe:
        return None

    def _cap():
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (160 * 1024 * 1024, 160 * 1024 * 1024))
        except Exception:
            pass

    try:
        r = subprocess.run([exe, "-q", "-layout", "-l", str(PDF_MAX_PAGES), "-", "-"], input=data,
                           capture_output=True, timeout=60, preexec_fn=_cap if os.name == "posix" else None)
    except Exception:
        return "[Could not extract text: PDF too heavy to parse]"
    if r.returncode != 0 and not r.stdout:
        return "[Could not extract text: PDF too heavy or unreadable]"
    return r.stdout.decode("utf-8", errors="replace")


PDF_MAX_PAGES = 150  # hard cap: odd-font readings can make pypdf balloon past the 300M service cap


def _extract_text(data: bytes, filename: str, content_type: str, max_chars: int | None = None) -> str | None:
    name = filename.lower()
    try:
        if name.endswith(".pdf") or "pdf" in content_type:
            # Prefer poppler's pdftotext in a memory-capped child process: some readings (odd fonts)
            # make pypdf use 300 MB+ and the kernel would kill the whole MCP server.
            out = _pdftotext(data)
            if out is not None:
                pages = out.split("\f")
                text = "\n".join(f"--- page {i} ---\n{t}" for i, t in enumerate(pages, 1) if t.strip())
                return text[: max_chars * 2] if max_chars else text
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            parts, total, n = [], 0, len(reader.pages)
            for i, page in enumerate(reader.pages, 1):
                if i > PDF_MAX_PAGES or (max_chars and total > max_chars):
                    parts.append(f"--- stopped at page {i - 1} of {n} ---")
                    break
                t = page.extract_text() or ''
                total += len(t)
                parts.append(f"--- page {i} ---\n{t}")
            return "\n".join(parts)
        if name.endswith(".docx"):
            import docx

            d = docx.Document(io.BytesIO(data))
            parts = [p.text for p in d.paragraphs]
            for t in d.tables:
                for row in t.rows:
                    parts.append(" | ".join(c.text for c in row.cells))
            return "\n".join(parts)
        if name.endswith((".xlsx", ".xlsm")):
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            parts = []
            for ws in wb.worksheets:
                parts.append(f"--- sheet: {ws.title} ---")
                for row in ws.iter_rows(values_only=True):
                    if any(v not in (None, "") for v in row):
                        parts.append(" | ".join("" if v is None else str(v) for v in row))
            return "\n".join(parts)
        if name.endswith(".pptx"):
            from pptx import Presentation

            prs = Presentation(io.BytesIO(data))
            parts = []
            for i, slide in enumerate(prs.slides, 1):
                texts = [sh.text_frame.text for sh in slide.shapes if sh.has_text_frame]
                parts.append(f"--- slide {i} ---\n" + "\n".join(t for t in texts if t.strip()))
            return "\n".join(parts)
        if name.endswith((".txt", ".md", ".csv", ".tsv", ".py", ".c", ".cpp", ".java", ".js", ".json", ".html", ".xml")) \
                or content_type.startswith("text/"):
            return data.decode("utf-8", errors="replace")
    except ImportError as exc:
        return f"[Install {exc.name} to extract text from this file type]"
    except Exception as exc:  # corrupt / scanned files
        return f"[Could not extract text: {type(exc).__name__}: {exc}]"
    return None


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
MAX_IMAGE_SIDE = 1568


def _prepare_image(data: bytes, filename: str) -> tuple[bytes, str]:
    """Downscale big images so they fit comfortably in a model's context; JPEG unless PNG is small."""
    try:
        from PIL import Image as PILImage

        im = PILImage.open(io.BytesIO(data))
        im.load()
        if max(im.size) > MAX_IMAGE_SIDE or len(data) > 1_500_000:
            im.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            out = io.BytesIO()
            im.save(out, "JPEG", quality=85, optimize=True)
            return out.getvalue(), "jpeg"
        fmt = (im.format or "png").lower()
        return data, ("jpeg" if fmt == "jpg" else fmt)
    except Exception:
        ext = filename.lower().rsplit(".", 1)[-1]
        return data, ("jpeg" if ext in ("jpg", "jpeg") else ext)


@mcp.tool(
    name="moodle_read_file",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
    ),
    structured_output=False,
)
async def moodle_read_file(
    file_url: Annotated[
        str,
        Field(
            min_length=10,
            description="A Moodle file URL (contains /pluginfile.php/) from course contents, "
            "assignment briefs or forum attachments.",
        ),
    ],
    save_copy: Annotated[
        bool, Field(description="Also save the file to MOODLE_DOWNLOAD_DIR on the machine running this server.")
    ] = False,
    max_chars: Annotated[int, Field(ge=1000, le=MAX_TEXT_CHARS, description="Max text to return.")] = 20_000,
) -> Any:
    """Download a course file (lecture slides, PDF notes, assignment brief, etc.) and return its
    contents so it can be summarised, explained, or used to help with coursework.

    Text is extracted from PDF, DOCX, PPTX, XLSX and plain-text/code files. Images (PNG, JPG,
    GIF, WebP) are returned as images so they can be looked at directly. Scanned (image-only)
    PDFs have no extractable text.
    """
    try:
        host = urlparse(MOODLE_URL).netloc
        if urlparse(file_url).netloc != host or "pluginfile.php" not in file_url:
            return f"Error: file_url must be a {host} pluginfile.php URL from another moodle_ tool."
        if not MOODLE_TOKEN:
            raise MoodleError("notoken", "MOODLE_TOKEN is not set.")
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.get(with_token(file_url))
        if resp.status_code == 404:
            return ("Error: The LMS says this file doesn't exist (404). It may have been replaced - "
                    "re-fetch the link with moodle_get_course_contents and try again.")
        if resp.status_code == 403:
            return ("Error: The LMS refused this file (403). It may be hidden or restricted for students, "
                    "or not yet released.")
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype:
            err = resp.json()
            raise MoodleError(err.get("errorcode", "fileerror"), err.get("error") or err.get("message", "File error"))
        filename = Path(urlparse(file_url).path).name or "file"
        from urllib.parse import unquote

        filename = unquote(filename)
        saved = ""
        if save_copy:
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w.\- ()]", "_", filename)
            path = DOWNLOAD_DIR / safe
            path.write_bytes(resp.content)
            saved = f"\nSaved to: {path}"
        header = f"# {filename} ({len(resp.content) // 1024} KB){saved}\n"
        if filename.lower().endswith(IMAGE_EXTS) or ctype.startswith("image/"):
            from mcp.server.mcpserver import Image

            img, fmt = _prepare_image(resp.content, filename)
            return [header + "\n(Image attached below.)", Image(data=img, format=fmt)]
        text = _extract_text(resp.content, filename, ctype, max_chars)
        if text is None:
            return header + "\n(Binary file type - no text extraction available.)"
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n…[truncated; {len(text)} chars total - raise max_chars to read more]"
        return header + "\n" + text
    except Exception as e:
        return _error(e)


# ---------------------------------------------------------------------------
# Workflow tool
# ---------------------------------------------------------------------------


@mcp.tool(name="moodle_semester_overview", annotations=READ_ONLY)
async def moodle_semester_overview(
    days_ahead: Annotated[int, Field(ge=1, le=180, description="Deadline window in days.")] = 30,
) -> str:
    """One-shot dashboard: every current course, plus all assignments and quizzes due in the
    window with live submission status, and overdue items. Best first call for
    "what do I have due?" / "plan my week" questions.
    """
    try:
        info = await site_info()
        uid = int(info["userid"])
        courses = await call("core_enrol_get_users_courses", userid=uid)
        now = time.time()
        courses = [c for c in courses if not c.get("enddate") or c["enddate"] > now]
        if not courses:
            return "No current courses."
        cname = {c["id"]: c["fullname"] for c in courses}
        ids = list(cname)
        horizon = now + days_ahead * 86400

        assigns_data, quiz_data = await asyncio.gather(
            call("mod_assign_get_assignments", courseids=ids),
            call("mod_quiz_get_quizzes_by_courses", courseids=ids),
            return_exceptions=True,
        )

        items: list[dict[str, Any]] = []
        if not isinstance(assigns_data, Exception):
            cands = []
            for c in assigns_data.get("courses", []):
                for a in c.get("assignments", []):
                    due = a.get("duedate") or 0
                    if due and (now - 14 * 86400) <= due <= horizon:
                        cands.append((c["id"], a))

            async def status(cid: int, a: dict[str, Any]) -> dict[str, Any]:
                st = "unknown"
                try:
                    s = await call("mod_assign_get_submission_status", assignid=a["id"])
                    last = s.get("lastattempt") or {}
                    sub = last.get("submission") or last.get("teamsubmission") or {}
                    st = sub.get("status", "new")
                    if last.get("extensionduedate"):
                        a = {**a, "duedate": last["extensionduedate"]}
                except MoodleError:
                    pass
                return {"kind": "assignment", "course": cname.get(cid), "name": a["name"],
                        "due": a["duedate"], "status": st, "id": a["id"]}

            items += await asyncio.gather(*(status(cid, a) for cid, a in cands))

        if not isinstance(quiz_data, Exception):
            for q in quiz_data.get("quizzes", []):
                close = q.get("timeclose") or 0
                if close and now <= close <= horizon:
                    items.append({"kind": "quiz", "course": cname.get(q.get("course")), "name": q["name"],
                                  "due": close, "status": "open" if (q.get("timeopen") or 0) <= now else
                                  f"opens {fmt_ts(q.get('timeopen'))}", "id": q["id"]})

        items.sort(key=lambda i: i["due"])
        done_states = {"submitted"}
        overdue = [i for i in items if i["kind"] == "assignment" and i["due"] < now and i["status"] not in done_states]
        upcoming = [i for i in items if i["due"] >= now]

        lines = [f"# Semester overview - {info.get('fullname')}", f"_As of {fmt_ts(int(now))}_", ""]
        lines.append(f"**Current courses ({len(courses)}):** " + ", ".join(sorted(cname.values())))
        lines.append("")
        if overdue:
            lines.append("## ⚠️ Overdue / not submitted")
            for i in overdue:
                lines.append(f"- {i['course']} · **{i['name']}** - was due {fmt_ts(i['due'])} · status: {i['status']} (assign_id {i['id']})")
            lines.append("")
        lines.append(f"## Due in the next {days_ahead} days")
        if not upcoming:
            lines.append("Nothing due. 🎉")
        for i in upcoming:
            mark = "✅" if i["status"] in done_states else "⬜"
            lines.append(
                f"- {mark} **{fmt_ts(i['due'])}** ({rel(i['due'])}) · {i['course']} · "
                f"[{i['kind']}] {i['name']} - {i['status']} (id {i['id']})"
            )
        if isinstance(assigns_data, Exception) or isinstance(quiz_data, Exception):
            lines.append("\n_Note: some data couldn't be loaded: "
                         + "; ".join(_error(x) for x in (assigns_data, quiz_data) if isinstance(x, Exception)) + "_")
        lines.append("\n_Faculty sometimes post deadlines only in announcements - check moodle_get_announcements for each course._")
        return clip("\n".join(lines))
    except Exception as e:
        return _error(e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MyCamu (Bennett ERP) tools - attendance, timetable, exams, results, announcements
# ---------------------------------------------------------------------------

import mycamu  # noqa: E402

mycamu.register(mcp, READ_ONLY)


class SecretPathGate:
    """ASGI middleware: only requests to /<MCP_SECRET>/... get through (path is rewritten to
    strip the secret). Everything else gets a bare 404, so the server is invisible without it.
    /dash/... goes to the Study Buddy dashboard (its own owner-only login) when it's installed."""

    def __init__(self, app: Any, secret: str, dash: Any = None):
        self.app = app
        self.secret = secret.encode()
        self.dash = dash

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path: str = scope.get("path", "")
        if self.dash is not None and scope["type"] == "http" and (path == "/dash" or path.startswith("/dash/")):
            return await self.dash(scope, receive, send)
        if path == "/healthz":  # for uptime checks; reveals nothing
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain")]})
            return await send({"type": "http.response.body", "body": b"ok"})
        first, _, rest = path.lstrip("/").partition("/")
        import hmac
        if not hmac.compare_digest(first.encode(), self.secret):
            await send({"type": "http.response.start", "status": 404,
                        "headers": [(b"content-type", b"text/plain")]})
            return await send({"type": "http.response.body", "body": b"Not Found"})
        new_path = "/" + rest
        scope = {**scope, "path": new_path, "raw_path": new_path.encode()}
        await self.app(scope, receive, send)


def serve_http(host: str, port: int) -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    secret = os.environ.get("MCP_SECRET", "").strip()
    if len(secret) < 24 or not re.fullmatch(r"[A-Za-z0-9_-]+", secret):
        sys.exit("Refusing to start HTTP mode: set MCP_SECRET (24+ chars, letters/digits/-/_). "
                 "deploy.sh generates one for you.")
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        # Auth is the secret path; Host/Origin checks would block the reverse proxy.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host=host,
    )
    print(f"moodle_mcp HTTP on {host}:{port}  (endpoint: /<MCP_SECRET>/mcp)", file=sys.stderr)
    dash = None
    if (Path(__file__).parent / "dashboard" / "app.py").exists():
        try:  # the dashboard must never stop the MCP server from starting
            from dashboard.app import app as dash
            print("dashboard mounted at /dash", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            _log(f"dashboard disabled: {type(e).__name__}: {e}")
            print(f"dashboard disabled: {e}", file=sys.stderr)
    uvicorn.run(SecretPathGate(app, secret, dash), host=host, port=port, log_level="warning",
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")


def main() -> None:
    p = argparse.ArgumentParser(description="Moodle LMS MCP server")
    p.add_argument("--http", action="store_true", help="Serve streamable HTTP instead of stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    if not MOODLE_TOKEN:
        print("warning: MOODLE_TOKEN not set - tools will return a setup error.", file=sys.stderr)
    _log(f"imports ok; token set={bool(MOODLE_TOKEN)}; starting {'http' if args.http else 'stdio'}")
    if args.http:
        mycamu.start_keepalive()  # daily MyCamu session ping (server only)
        serve_http(args.host, args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
