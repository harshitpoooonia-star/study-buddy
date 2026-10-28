"""
MyCamu (Bennett ERP, student.bennetterp.camu.in) tools for the Study Buddy MCP server.

Auth is the browser session cookie `connect.sid` (MyCamu login is Microsoft-SSO only).
It lives ONLY in the server's .env as MYCAMU_COOKIE, set with `set-camu-cookie`.
It is never logged, never returned by a tool, and never put in code.

Design notes
- Read-only: six tools, all POST/GET JSON reads.
- Light polling: responses are cached (default 10 min) and calls are spaced >= 2 s apart,
  so a chat asking several questions doesn't hammer the ERP.
- Keep-alive: a background thread calls /api/login/getvalidationstatus once a day
  (plus ~1 min after start) so the rolling session doesn't lapse. The result is recorded and
  surfaced by every tool when the session has expired.
- Student IDs (InId, PrID, CrID, DeptID, SemID, AcYr, CmProgID, StuID) are fetched from
  /api/studentprog/getAllProgressByStudID/{StuID}/null, never hardcoded. StuID itself comes from
  MYCAMU_STUID in .env if set, otherwise it is discovered from the validation-status response.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import httpx
from pydantic import Field

BASE = os.environ.get("MYCAMU_URL", "https://student.bennetterp.camu.in").rstrip("/")
ENV_PATH = Path(__file__).with_name(".env")
STATUS_PATH = Path(__file__).with_name("mycamu_status.json")
CACHE_TTL = int(os.environ.get("MYCAMU_CACHE_SECONDS", "600"))
MIN_GAP = 2.0          # seconds between outbound calls
KEEPALIVE_EVERY = 24 * 3600
OUT_LIMIT = 20_000

ID_KEYS = ["InId", "PrID", "CrID", "DeptID", "SemID", "AcYr", "CmProgID", "StuID"]

EXPIRED_MSG = (
    "MyCamu session expired — paste a fresh connect.sid. "
    "Log in to MyCamu in your browser, copy the connect.sid cookie, then run on your PC:\n"
    "  SET_CAMU_COOKIE.bat   (or: ssh -t -i <oracle.key> ubuntu@<server> ~/moodle-mcp/set-camu-cookie)"
)
NOT_SET_MSG = (
    "MyCamu isn't connected yet — no session cookie on the server. "
    "Run SET_CAMU_COOKIE.bat on your PC and paste your connect.sid when it asks."
)


class CamuError(Exception):
    pass


class CamuExpired(CamuError):
    pass


class CamuInfo(CamuError):
    """MyCamu answered with an explanatory message (e.g. 'Timetable not finalised')."""


# --------------------------------------------------------------------------- cookie + status


def _cookie() -> str:
    # Re-read .env each time so a rotated cookie takes effect without a restart.
    # .env is the single source of truth when it exists (a removed key must mean "no cookie",
    # not a stale value inherited into os.environ at startup).
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if line.startswith("MYCAMU_COOKIE="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
        return ""
    return os.environ.get("MYCAMU_COOKIE", "").strip()


def _env_value(key: str) -> str:
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return os.environ.get(key, "").strip()


_env_lock = threading.Lock()


def _save_rolled_cookie(new_value: str) -> None:
    """If the server rotates connect.sid, persist the new value (atomic replace)."""
    if not new_value or new_value == _cookie():
        return
    with _env_lock:
        lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
        lines = [l for l in lines if not l.startswith("MYCAMU_COOKIE=")] + [f"MYCAMU_COOKIE={new_value}"]
        tmp = ENV_PATH.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(ENV_PATH)


def _write_status(ok: bool, detail: str) -> None:
    try:
        STATUS_PATH.write_text(json.dumps({"ok": ok, "detail": detail, "checked": int(time.time())}))
    except OSError:
        pass


def _read_status() -> dict[str, Any]:
    try:
        return json.loads(STATUS_PATH.read_text())
    except Exception:
        return {}


# --------------------------------------------------------------------------- HTTP


_last_call = 0.0
_gap_lock = threading.Lock()  # works across the server loop and the keep-alive thread's loop
_cache: dict[str, tuple[float, Any]] = {}

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": BASE,
    "Referer": BASE + "/",
    "User-Agent": "StudyBuddy-MCP/1.0 (personal read-only client)",
}


def _looks_expired(resp: httpx.Response, data: Any) -> bool:
    if resp.status_code in (401, 403, 440):
        return True
    ctype = resp.headers.get("content-type", "")
    if "text/html" in ctype:  # redirected to the SSO/login page
        return True
    if isinstance(data, dict):
        blob = json.dumps(data)[:600].lower()
        if any(s in blob for s in ("session expired", "not authenticated", "unauthori", "login again",
                                   "invalid session", "sessionexpired")):
            return True
    return False


async def _request(method: str, path: str, body: dict | None = None, use_cache: bool = True) -> Any:
    global _last_call
    cookie = _cookie()
    if not cookie:
        raise CamuError(NOT_SET_MSG)
    import hashlib
    sess = hashlib.sha256(cookie.encode()).hexdigest()[:12]  # cache per session, never across cookies
    key = f"{sess} {method} {path} {json.dumps(body, sort_keys=True) if body else ''}"
    if use_cache and key in _cache and time.time() - _cache[key][0] < CACHE_TTL:
        return _cache[key][1]

    with _gap_lock:  # reserve the next slot, then sleep outside the lock
        slot = max(time.monotonic(), _last_call + MIN_GAP)
        _last_call = slot
    wait = slot - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)

    async with httpx.AsyncClient(timeout=30, follow_redirects=False, headers=_HEADERS,
                                 cookies={"connect.sid": cookie, "X-App-Type": "student"}) as client:
        resp = await client.request(method, BASE + path, json=body if method == "POST" else None)

    rolled = resp.cookies.get("connect.sid")
    if rolled:
        _save_rolled_cookie(rolled)

    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.is_redirect or _looks_expired(resp, data):
        _write_status(False, f"HTTP {resp.status_code} on {path}")
        raise CamuExpired(EXPIRED_MSG)
    if resp.status_code >= 400:
        msg = None
        if isinstance(data, dict):
            errs = (data.get("output") or {}).get("errors") if isinstance(data.get("output"), dict) else None
            msg = (errs or {}).get("message") if isinstance(errs, dict) else None
            msg = msg or data.get("errorMessage")
        if msg:
            raise CamuInfo(msg)
        raise CamuError(f"MyCamu returned HTTP {resp.status_code} for {path}. Try again later.")
    if data is None:
        raise CamuError(f"MyCamu sent a non-JSON reply for {path}.")
    _write_status(True, "ok")
    if use_cache:
        _cache[key] = (time.time(), data)
    return data


def _unwrap(data: Any) -> Any:
    """MyCamu wraps payloads in output/data layers; peel the common ones."""
    for _ in range(4):
        if isinstance(data, dict):
            for k in ("output", "data", "Data", "result", "res"):
                if k in data and isinstance(data[k], (dict, list)):
                    data = data[k]
                    break
            else:
                return data
        else:
            return data
    return data


def _find_key(obj: Any, names: set[str], depth: int = 0) -> Any:
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in names and isinstance(v, (str, int)) and str(v):
                return v
        for v in obj.values():
            r = _find_key(v, names, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj[:20]:
            r = _find_key(v, names, depth + 1)
            if r is not None:
                return r
    return None


# --------------------------------------------------------------------------- IDs


_ids: dict[str, Any] | None = None
_progress_raw: Any = None


STU_KEYS = {"StuID", "stuId", "StudID", "studentId", "StuId"}


async def validation_status(use_cache: bool = False) -> Any:
    """Touches the session (keeps it rolling). NOTE: MyCamu answers 200 here even with no
    session ({"isvrfd": "", ...}), so this alone does NOT prove the cookie is valid."""
    return await _request("POST", "/api/login/getvalidationstatus", {}, use_cache=use_cache)


async def check_session() -> None:
    """Raise CamuExpired unless the cookie is really logged in.
    getvalidationstatus answers 200 even when logged out, so we touch it (keeps the session
    rolling) and then confirm with the unread-notification count, which is 401 without a session."""
    await validation_status()
    await _request("POST", "/api/notification/get-unread-notification", {}, use_cache=False)
    _write_status(True, "ok")


_sem_label = ""


def _current_record(recs: list[dict]) -> dict:
    """Pick the live semester: academic-year window covers today, not a supplementary entry,
    most recently modified. MyCamu lists newest first, so fall back to the first record."""
    today = datetime.now().strftime("%Y-%m-%d")
    def regular(r: dict) -> bool:
        return "suppl" not in str(r.get("SemName", "")).lower()
    live = [r for r in recs if regular(r)
            and str(r.get("AcyrFrDt", ""))[:10] <= today <= str(r.get("AcyrToDt", "9999"))[:10]]
    pool = live or [r for r in recs if regular(r)] or recs
    return max(pool, key=lambda r: str(r.get("MoAt", "")))


async def get_ids() -> dict[str, Any]:
    """Resolve InId/PrID/... for the current semester from the student's programme records."""
    global _ids, _progress_raw, _sem_label
    if _ids:
        return _ids
    stu = _env_value("MYCAMU_STUID")
    if not stu:
        # Attendance is session-scoped (empty body works) and carries the student's StuID.
        att = await _request("POST", "/api/Attendance/getDtaForStupage", {})
        stu = _find_key(att, STU_KEYS)
    if not stu:
        raise CamuError("Couldn't work out your MyCamu student ID. Add MYCAMU_STUID=<id> to the server .env.")
    prog = await _request("GET", f"/api/studentprog/getAllProgressByStudID/{stu}/null")
    _progress_raw = prog
    recs = _unwrap(prog)
    recs = [recs] if isinstance(recs, dict) else [r for r in (recs or []) if isinstance(r, dict)]
    if not recs:
        raise CamuError("MyCamu returned no programme records for your account.")
    rec = _current_record(recs)
    ids: dict[str, Any] = {k: rec[k] for k in ID_KEYS if rec.get(k)}
    ids["StuID"] = stu
    missing = [k for k in ID_KEYS if k not in ids]
    if missing:
        raise CamuError(f"MyCamu programme record is missing {', '.join(missing)}; the API shape may have changed.")
    _sem_label = f"{rec.get('SemName', '')} ({rec.get('AcYrNm', '')})".strip()
    _ids = ids
    return ids


# --------------------------------------------------------------------------- formatting


def _scalar(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool)) or v is None


def _fmt(data: Any, title: str, max_rows: int = 60) -> str:
    """Generic, compact rendering: list-of-dicts -> table of the most common scalar columns."""
    data = _unwrap(data)
    if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
        counts: dict[str, int] = {}
        for r in data[:max_rows]:
            for k, v in r.items():
                if _scalar(v) and not re.search(r"(^_id$|ID$|Id$|^__v$|password|token)", k):
                    if v not in (None, "", " "):
                        counts[k] = counts.get(k, 0) + 1
        cols = [k for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:8]
        if not cols:
            return f"# {title}\n\n```json\n{json.dumps(data[:max_rows], ensure_ascii=False, default=str)[:OUT_LIMIT]}\n```"
        lines = [f"# {title} ({len(data)})", "", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        for r in data[:max_rows]:
            lines.append("| " + " | ".join(str(r.get(c, "")).replace("|", "/").replace("\n", " ")[:60] for c in cols) + " |")
        if len(data) > max_rows:
            lines.append(f"\n…{len(data) - max_rows} more rows")
        return "\n".join(lines)[:OUT_LIMIT]
    if isinstance(data, (dict, list)):
        if not data:
            return f"# {title}\n\nNothing found."
        txt = json.dumps(data, ensure_ascii=False, indent=1, default=str)
        return f"# {title}\n\n```json\n{txt[:OUT_LIMIT]}\n```"
    return f"# {title}\n\n{data}"


def _err(e: Exception) -> str:
    if isinstance(e, CamuError):
        return f"Error: {e}"
    if isinstance(e, httpx.TimeoutException):
        return "Error: MyCamu took too long to respond. Try again in a minute."
    if isinstance(e, httpx.ConnectError):
        return "Error: Couldn't reach MyCamu (student.bennetterp.camu.in)."
    return f"Error: {type(e).__name__}: {e}"


# --------------------------------------------------------------------------- keep-alive


def _keepalive_loop() -> None:
    time.sleep(60 + random.randint(0, 120))
    while True:
        if _cookie():
            try:
                asyncio.run(check_session())
            except CamuExpired:
                pass  # status already recorded
            except Exception as exc:  # network blips: record but don't mark expired
                _write_status(_read_status().get("ok", True), f"keepalive error: {type(exc).__name__}")
        time.sleep(KEEPALIVE_EVERY + random.randint(-1800, 1800))


def start_keepalive() -> None:
    threading.Thread(target=_keepalive_loop, name="mycamu-keepalive", daemon=True).start()


# --------------------------------------------------------------------------- tools


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fmt_attendance(data: Any) -> str:
    d = _unwrap(data)
    if not isinstance(d, dict):
        return _fmt(data, "Attendance")
    subs = d.get("subjectList") or d.get("aSubjectList") or []
    lines = [f"# Attendance — {_sem_label}" if _sem_label else "# Attendance", ""]
    if d.get("OvrAllPrcntg") is not None:
        lines.append(f"**Overall: {d.get('OvrAllPrcntg')}%** ({d.get('OvrAllPCnt', '?')}/{d.get('OvrAllCnt', '?')} classes)"
                     + (f" · this month {d.get('CurMnthPrcntg')}%" if d.get("CurMnthPrcntg") is not None else ""))
        lines.append("")
    if not subs:
        lines.append("No subject-wise attendance on MyCamu yet.")
        return "\n".join(lines)
    lines += ["| Subject | Code | Type | Present | Total | % | 75% check |", "|---|---|---|---|---|---|---|"]
    for sj in subs:
        total = int(_num(sj.get("all")))
        pct = _num(sj.get("prsPercnt"))
        present = round(pct * total / 100) if total else int(_num(sj.get("prsentCnt")))
        if total == 0:
            note = "no classes yet"
        elif pct < 75:
            need = -(-(0.75 * total - present) // 0.25)  # ceil: consecutive classes to reach 75%
            note = f"⚠️ attend next {int(need)}"
        else:
            spare = int((present - 0.75 * total) // 0.75)
            note = f"✅ can miss {spare}" if spare > 0 else "✅ on the line"
        extra = []
        for k, lbl in (("onDutyCnt", "OD"), ("medLeaveCnt", "ML"), ("leaveCnt", "leave")):
            if _num(sj.get(k)):
                extra.append(f"{int(_num(sj.get(k)))} {lbl}")
        name = str(sj.get("SubjNm", "")).strip() + (f" ({', '.join(extra)})" if extra else "")
        lines.append(f"| {name} | {sj.get('SubjCd', '')} | {sj.get('AttType', '')} | {sj.get('prsentCnt', '')} | {total} | {round(pct, 1):g} | {note} |")
    lines.append("\n_75% check uses MyCamu's own percentage; OD/medical rules are applied by the university._")
    return "\n".join(lines)


def _fmt_timetable(data: Any, title: str) -> str:
    d = _unwrap(data)
    slots: list[dict] = []
    def walk(o: Any) -> None:
        if isinstance(o, dict) and ("SubNa" in o or "FrTime" in o):
            slots.append(o)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(d)
    if not slots:
        return f"# {title}\n\nNo classes on MyCamu for this period."
    slots.sort(key=lambda x: str(x.get("start", "")))
    lines = [f"# {title}", "", "| Date | Time | Subject | Code | Faculty | Type |", "|---|---|---|---|---|---|"]
    for x in slots:
        st = str(x.get("start", ""))
        try:
            day = datetime.fromisoformat(st.replace("Z", "+00:00")).strftime("%a %d %b")
        except ValueError:
            day = st[:10]
        lines.append(f"| {day} | {x.get('FrTime', '')}–{x.get('ToTime', '')} | {x.get('SubNa', '')} | {x.get('SubCd', '')} | "
                     f"{x.get('StaffNm', '')} | {x.get('type', '') or x.get('PerNm', '')} |")
    return "\n".join(lines)


def register(mcp: Any, read_only: Any) -> None:
    def _today() -> datetime:
        return datetime.now()

    @mcp.tool(name="mycamu_get_attendance", annotations=read_only)
    async def mycamu_get_attendance() -> str:
        """Attendance from MyCamu (Bennett ERP): overall %, this month's %, and per subject
        present/total/% with a 75% check (how many classes to attend, or how many can be missed).
        Use for 'what's my attendance', 'am I short anywhere', debarment risk."""
        try:
            await get_ids()  # resolves semester label; attendance itself is session-scoped
            data = await _request("POST", "/api/Attendance/getDtaForStupage", {})
            return _fmt_attendance(data)
        except Exception as e:
            return _err(e)

    @mcp.tool(name="mycamu_get_timetable", annotations=read_only)
    async def mycamu_get_timetable(
        days_ahead: Annotated[int, Field(ge=0, le=14, description="Days from the start date to include.")] = 6,
        start_date: Annotated[str | None, Field(description="Optional YYYY-MM-DD start (default today), e.g. to see a past week.",
                                                pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    ) -> str:
        """Class timetable from MyCamu: lectures/labs with date, time, subject, code and faculty.
        During exam weeks MyCamu reports the timetable as not finalised."""
        try:
            ids = await get_ids()
            start = datetime.strptime(start_date, "%Y-%m-%d") if start_date else _today()
            end = start + timedelta(days=days_ahead)
            body = {**ids, "start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d")}
            data = await _request("POST", "/api/Timetable/get", body)
            return _fmt_timetable(data, f"Timetable {start:%a %d %b} – {end:%a %d %b}")
        except CamuInfo as e:
            return f"MyCamu says: {e} (common during mid/end-sem weeks — try a different week with start_date)."
        except Exception as e:
            return _err(e)

    @mcp.tool(name="mycamu_get_exam_schedule", annotations=read_only)
    async def mycamu_get_exam_schedule() -> str:
        """Exam schedule published on MyCamu (date, time, course). Bennett often sends date sheets
        and seating plans by email instead, so an empty result here is normal."""
        try:
            ids = await get_ids()
            data = await _request("POST", "/api/ExamSchdl/getExamSchdl", dict(ids))
            if not _unwrap(data):
                return ("No exam schedule is published on MyCamu for " + (_sem_label or "this semester") +
                        ". Bennett usually emails the CoE date sheet and seating plan — check Outlook.")
            return _fmt(data, "Exam schedule")
        except CamuInfo as e:
            return f"MyCamu says: {e}"
        except Exception as e:
            return _err(e)

    @mcp.tool(name="mycamu_get_results", annotations=read_only)
    async def mycamu_get_results() -> str:
        """Semester history from MyCamu's programme records: each semester/supplementary entry with
        academic year, status and programme. (MyCamu's API here carries no marks/SGPA.)"""
        try:
            await get_ids()
            recs = _unwrap(_progress_raw)
            recs = [r for r in (recs if isinstance(recs, list) else [recs]) if isinstance(r, dict)]
            lines = [f"# Semester history ({len(recs)} records) — current: {_sem_label}", "",
                     "| Semester | Academic year | Status | Programme |", "|---|---|---|---|"]
            for r in recs:
                lines.append(f"| {r.get('SemName', '')} | {r.get('AcYrNm', '')} | {r.get('stustatus', '')} | {r.get('CrName', '')} |")
            lines.append("\n_Marks/grades aren't in this MyCamu endpoint; use moodle_get_grades for course marks._")
            return "\n".join(lines)
        except Exception as e:
            return _err(e)

    @mcp.tool(name="mycamu_get_announcements", annotations=read_only)
    async def mycamu_get_announcements(
        days_back: Annotated[int, Field(ge=1, le=90, description="How many days of announcements.")] = 14,
        raw: Annotated[bool, Field(description="Return full rows as JSON (used by the dashboard).")] = False,
    ) -> str:
        """University announcements posted on MyCamu over the last N days."""
        try:
            ids = await get_ids()
            end = _today()
            start = end - timedelta(days=days_back)
            body = {**ids, "start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d")}
            data = await _request("POST", "/api/Announcement/getbyDayerangeandInId", body)
            if not _unwrap(data):
                return f"No announcements on MyCamu in the last {days_back} days."
            if raw:
                rows = [r for r in (_unwrap(data) if isinstance(_unwrap(data), list) else []) if isinstance(r, dict)]
                keep = [{k: (str(v)[:900] if isinstance(v, str) else v) for k, v in r.items()
                         if _scalar(v) and v not in (None, "", " ") and not re.search(r"(password|token)", k, re.I)}
                        for r in rows[:60]]
                return json.dumps(keep, ensure_ascii=False, default=str)
            return _fmt(data, f"Announcements (last {days_back} days)")
        except CamuInfo as e:
            return f"MyCamu says: {e}"
        except Exception as e:
            return _err(e)

    @mcp.tool(name="mycamu_get_notifications", annotations=read_only)
    async def mycamu_get_notifications() -> str:
        """Count of unread notifications on MyCamu (does not mark them as read)."""
        try:
            data = await _request("POST", "/api/notification/get-unread-notification", {})
            d = data.get("output", {}).get("data") if isinstance(data, dict) and isinstance(data.get("output"), dict) else _unwrap(data)
            if isinstance(d, (int, float)):
                n = int(d)
                return f"You have {n} unread notification{'s' if n != 1 else ''} on MyCamu." if n else "No unread notifications on MyCamu."
            return _fmt(data, "Unread notifications")
        except Exception as e:
            return _err(e)

    @mcp.tool(name="mycamu_status", annotations=read_only)
    async def mycamu_status() -> str:
        """Check whether the MyCamu session is alive (and when it was last verified)."""
        if not _cookie():
            return NOT_SET_MSG
        try:
            await check_session()
            await get_ids()
            return f"MyCamu session is active ✅ — current semester: {_sem_label}"
        except Exception as e:
            st = _read_status()
            when = datetime.fromtimestamp(st["checked"]).strftime("%d %b %H:%M") if st.get("checked") else "never"
            return _err(e) + f"\n(last check: {when})"


# --------------------------------------------------------------------------- CLI: verify / probe


def _cli() -> int:
    """`python mycamu.py check` -> exit 0 if session valid.  `python mycamu.py probe` -> print
    response *shapes* (keys and types only, no values) to help adapt parsers."""
    def shape(o: Any, d: int = 0) -> Any:
        if d > 4:
            return "…"
        if isinstance(o, dict):
            return {k: shape(v, d + 1) for k, v in list(o.items())[:25]}
        if isinstance(o, list):
            return [shape(o[0], d + 1), f"len={len(o)}"] if o else []
        return type(o).__name__

    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    try:
        if cmd == "check":
            asyncio.run(check_session())
            print("MyCamu session is valid ✅")
            return 0
        if cmd == "probe":
            async def run() -> None:
                v = await validation_status()
                print("getvalidationstatus:", json.dumps(shape(v)))
                ids = await get_ids()
                print("ids resolved:", sorted(ids))
                print("progress:", json.dumps(shape(_progress_raw))[:3000])
                for name, path, body in [
                    ("attendance", "/api/Attendance/getDtaForStupage", {}),
                    ("exams", "/api/ExamSchdl/getExamSchdl", ids),
                    ("notifications", "/api/notification/get-unread-notification", {}),
                ]:
                    try:
                        print(name + ":", json.dumps(shape(await _request("POST", path, dict(body))))[:2000])
                    except Exception as exc:
                        print(name + ": ERROR", exc)
            asyncio.run(run())
            return 0
    except CamuExpired:
        print(EXPIRED_MSG)
        return 2
    except Exception as exc:
        print(_err(exc))
        return 1
    print("usage: mycamu.py [check|probe]")
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
