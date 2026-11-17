#!/usr/bin/env python3
"""Study Buddy collector - snapshots Moodle + MyCamu into study.db.

Runs as a oneshot (systemd timer `study-collect`, every 30 min) or on demand from the API's
/sync. Talks only to the local MCP server, so it shares its 10-minute MyCamu cache and 2-second
request spacing; it never contacts Moodle or MyCamu directly.

    python -m dashboard.collector            # full run
    python -m dashboard.collector --only moodle grades
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, urlparse

from .common import (
    APP_DIR, MCP, TZ, MCPDown, ToolError, course_code, course_title, db, init_db, meta_get, meta_put,
    now_local, now_ts, upsert_doc, week_monday,
)

LOCK = APP_DIR / ".collector.lock"
GRADES_EVERY = 6 * 3600          # grades change slowly; one call per course every 6 h
FORCE: dict[str, bool] = {}
STATUS_REFRESH = 6 * 3600        # re-check submission status of already-submitted items this often
WINDOW_PAST = 21 * 86400         # keep tracking items due up to 3 weeks ago
WINDOW_AHEAD = 120 * 86400


@contextmanager
def tx(con: sqlite3.Connection) -> Iterator[None]:
    """Short write transaction (BEGIN IMMEDIATE so we wait for the lock up front, not mid-write)."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def log(msg: str) -> None:
    print(f"{datetime.now(TZ):%H:%M:%S} {msg}", flush=True)


def _cmid(url: str | None) -> int | None:
    try:
        return int(parse_qs(urlparse(url or "").query)["id"][0])
    except (KeyError, ValueError, IndexError):
        return None


def _table_rows(md: str) -> list[list[str]]:
    rows, lines = [], md.splitlines()
    sep = re.compile(r"^\|\s*:?-{2,}")
    for i, line in enumerate(lines):
        if not line.startswith("|") or sep.match(line):
            continue
        if i + 1 < len(lines) and sep.match(lines[i + 1]):  # header row
            continue
        rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows


def _num(s: str) -> float:
    try:
        return float(re.sub(r"[^\d.]", "", s) or 0)
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- Moodle


def collect_moodle(mcp: MCP, con: sqlite3.Connection) -> str:
    now = now_ts()
    seen: dict[str, dict[str, Any]] = {}

    # 1) Assignments (all current courses) - due/cut-off/max grade + assign_id for status.
    # Per current course: the all-courses call includes every past semester and gets clipped.
    courses = _courses(mcp)
    course_names = {c["id"]: c.get("fullname") for c in courses}
    assigns: list[dict] = []
    quizzes: list[dict] = []
    for c in courses:
        assigns += mcp.call_json("moodle_list_assignments", course_id=c["id"], include_brief=False)
        quizzes += mcp.call_json("moodle_list_quizzes", course_id=c["id"], include_attempts=True)
    for a in assigns:
        due = a.get("due") or 0
        if due and not (now - WINDOW_PAST <= due <= now + WINDOW_AHEAD):
            continue
        iid = f"moodle:assign:{a.get('cmid') or a['assign_id']}"
        seen[iid] = {
            "id": iid, "type": "assign", "title": a.get("name") or "Assignment",
            "course_name": a.get("course"), "course_id": a.get("course_id"),
            "opens_at": a.get("opens") or None, "due_at": due or None, "cutoff_at": a.get("cutoff") or None,
            "max_grade": a.get("max_grade"), "ref_id": a["assign_id"],
            "url": f"{_moodle_base()}/mod/assign/view.php?id={a['cmid']}" if a.get("cmid") else None,
        }

    # 2) Quizzes with attempts.
    for q in quizzes:
        closes = q.get("closes") or 0
        if closes and closes < now - WINDOW_PAST:
            continue
        atts = q.get("attempts") or []
        finished = [x for x in atts if x.get("state") == "finished"]
        if finished:
            status = "attempted"
        elif closes and closes < now:
            status = "closed"
        elif q.get("opens") and q["opens"] > now:
            status = "upcoming"
        else:
            status = "open"
        iid = f"moodle:quiz:{q.get('cmid') or q['quiz_id']}"
        seen[iid] = {
            "id": iid, "type": "quiz", "title": q.get("name") or "Quiz",
            "course_name": course_names.get(q.get("course_id")), "course_id": q.get("course_id"),
            "opens_at": q.get("opens") or None, "due_at": closes or None, "max_grade": _float(q.get("max_grade")),
            "status": status, "ref_id": q["quiz_id"],
            "grade": None if q.get("best_grade") is None else str(q["best_grade"]),
            "url": f"{_moodle_base()}/mod/quiz/view.php?id={q['cmid']}" if q.get("cmid") else None,
            "detail": {"attempts": len(atts), "finished": len(finished), "attempts_allowed": q.get("attempts_allowed"),
                       "time_limit_min": q.get("time_limit_min")},
        }

    # 3) Timeline: action-required flags for the above + anything else with a date (forums, ...).
    events = mcp.call_json("moodle_get_upcoming_deadlines", days_ahead=90, include_overdue_days=14, limit=50)
    for e in events:
        cm = _cmid(e.get("url"))
        typ = e.get("type") or ""
        iid = f"moodle:{typ}:{cm}" if typ in ("assign", "quiz") and cm else f"moodle:event:{typ}:{cm or e.get('instance')}"
        row = seen.get(iid)
        if row is None:
            row = seen[iid] = {
                "id": iid, "type": typ if typ in ("assign", "quiz") else "event",
                "title": e.get("activity") or e.get("name") or "Event",
                "course_name": e.get("course"), "course_id": e.get("course_id"), "due_at": e.get("due"),
                "url": e.get("url"), "status": "pending" if e.get("action_required") else "open",
                "detail": {"module": typ},
            }
        row["action"] = e.get("action")
        row["action_required"] = 1 if e.get("action_required") else 0
        if e.get("due") and not row.get("due_at"):
            row["due_at"] = e["due"]

    # 4) Submission status for assignments (skip ones already submitted & checked recently).
    prev = {r["id"]: r for r in con.execute(
        "SELECT id, status, grade, detail, status_checked_at FROM items WHERE type='assign'")}
    checked = 0
    for iid, row in seen.items():
        if row["type"] != "assign" or not row.get("ref_id"):
            continue
        p = prev.get(iid)
        fresh = p and p["status_checked_at"] and now - p["status_checked_at"] < STATUS_REFRESH
        settled = p and p["status"] in ("submitted", "graded")
        if fresh and (settled or not row.get("action_required")):
            row["status"], row["grade"] = p["status"], p["grade"]
            row["detail"] = json.loads(p["detail"] or "{}")
            row["status_checked_at"] = p["status_checked_at"]
            continue
        try:
            st = mcp.call_json("moodle_get_assignment_status", assign_id=row["ref_id"])
            row.update(_assign_status(st))
            row["status_checked_at"] = now
            checked += 1
        except ToolError as e:
            row["status"] = p["status"] if p else ("pending" if row.get("action_required") else "unknown")
            log(f"status {row['ref_id']}: {e}")

    # 5) Unread LMS notifications (count only; markdown keeps the reply small).
    unread = None
    try:
        md = mcp.call("moodle_get_notifications", limit=50, unread_only=True)
        m = re.match(r"# Notifications \((\d+)\)", md)
        unread = int(m.group(1)) if m else 0
    except ToolError as e:
        log(f"notifications: {e}")

    # 6) Write - everything above was network; the write lock is held only for this block.
    with tx(con):
        for row in seen.values():
            if row["type"] == "assign" and not row.get("status"):
                row["status"] = "pending" if row.get("action_required") else "unknown"
            _upsert_item(con, row, now)
        # Items that vanished upstream (deleted/hidden) and are long past: drop.
        con.execute("DELETE FROM items WHERE source='moodle' AND seen_at < ? AND (due_at IS NULL OR due_at < ?)",
                    (now - 2 * 86400, now - WINDOW_PAST))
        if unread is not None:
            meta_put(con, "unread_moodle", unread)

    return f"{len(seen)} items, {checked} status checks"


_COURSES: list[dict] | None = None


def _courses(mcp: MCP) -> list[dict]:
    global _COURSES
    if _COURSES is None:
        _COURSES = mcp.call_json("moodle_list_courses")
    return _COURSES


def _moodle_base() -> str:
    from .common import ENV
    return ENV.get("MOODLE_URL", "https://lms.bennett.edu.in").rstrip("/")


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _assign_status(st: dict) -> dict[str, Any]:
    last = st.get("lastattempt") or {}
    sub = last.get("submission") or last.get("teamsubmission") or {}
    raw = (sub.get("status") or "new").lower()
    grading = (last.get("gradingstatus") or "").lower()
    fb = st.get("feedback") or {}
    grade = fb.get("gradefordisplay") or ((fb.get("grade") or {}).get("grade") if isinstance(fb.get("grade"), dict) else None)
    if grade:
        grade = re.sub(r"<[^>]+>", "", str(grade)).strip()
    if grading == "graded" or grade:
        status = "graded"
    elif raw == "submitted":
        status = "submitted"
    elif raw == "draft":
        status = "draft"
    else:
        status = "pending"
    return {
        "status": status, "grade": grade or None,
        "detail": {"submission": raw, "grading": grading or None,
                   "extension_due": last.get("extensionduedate") or None,
                   "can_submit": last.get("cansubmit"), "locked": bool(last.get("locked")),
                   "submitted_at": sub.get("timemodified") if raw == "submitted" else None},
    }


def _upsert_item(con: sqlite3.Connection, r: dict[str, Any], now: int) -> None:
    name = r.get("course_name") or ""
    con.execute(
        """INSERT INTO items(id,source,course_code,course_name,course_id,type,title,opens_at,due_at,cutoff_at,
               status,grade,max_grade,action,action_required,url,ref_id,detail,status_checked_at,seen_at,updated_at)
           VALUES(:id,'moodle',:cc,:cn,:course_id,:type,:title,:opens_at,:due_at,:cutoff_at,:status,:grade,:max_grade,
               :action,:action_required,:url,:ref_id,:detail,:status_checked_at,:now,:now)
           ON CONFLICT(id) DO UPDATE SET course_code=excluded.course_code, course_name=excluded.course_name,
               course_id=excluded.course_id, type=excluded.type, title=excluded.title, opens_at=excluded.opens_at,
               due_at=excluded.due_at, cutoff_at=excluded.cutoff_at, status=excluded.status, grade=excluded.grade,
               max_grade=excluded.max_grade, action=excluded.action, action_required=excluded.action_required,
               url=excluded.url, ref_id=excluded.ref_id, detail=excluded.detail,
               status_checked_at=COALESCE(excluded.status_checked_at, items.status_checked_at),
               seen_at=excluded.seen_at,
               updated_at=CASE WHEN items.status IS excluded.status AND items.due_at IS excluded.due_at
                               THEN items.updated_at ELSE excluded.updated_at END""",
        {
            "id": r["id"], "cc": course_code(name), "cn": course_title(name), "course_id": r.get("course_id"),
            "type": r["type"], "title": r["title"], "opens_at": r.get("opens_at"), "due_at": r.get("due_at"),
            "cutoff_at": r.get("cutoff_at"), "status": r.get("status"), "grade": r.get("grade"),
            "max_grade": _float(r.get("max_grade")), "action": r.get("action"),
            "action_required": r.get("action_required", 0), "url": r.get("url"), "ref_id": r.get("ref_id"),
            "detail": json.dumps(r.get("detail") or {}), "status_checked_at": r.get("status_checked_at"), "now": now,
        },
    )


# --------------------------------------------------------------------------- MyCamu


def collect_attendance(mcp: MCP, con: sqlite3.Connection) -> str:
    md = mcp.call("mycamu_get_attendance")
    rows = [r for r in _table_rows(md) if len(r) >= 6]
    if not rows:
        raise ToolError(md.splitlines()[0][:200] if md else "empty attendance")
    today = now_local().strftime("%Y-%m-%d")
    with tx(con):
        _write_attendance(con, md, rows, today)
    return f"{len(rows)} subjects"


def _write_attendance(con: sqlite3.Connection, md: str, rows: list[list[str]], today: str) -> None:
    con.execute("DELETE FROM attendance_snap WHERE date=?", (today,))
    for r in rows:
        subj, code, _typ, present, total, pct = r[:6]
        subj = re.sub(r"\s*\((\d+ (OD|ML|leave)(, )?)+\)$", "", subj).strip()
        t = int(_num(total))
        p_raw = _num(present)
        pc = _num(pct)
        p = int(round(pc * t / 100)) if t else int(p_raw)  # MyCamu's own % is authoritative
        con.execute("INSERT OR REPLACE INTO attendance_snap VALUES(?,?,?,?,?,?)",
                    (today, code or subj[:12], subj, p, t, pc))
    m = re.search(r"\*\*Overall: ([\d.]+)%\*\* \((\d+)/(\d+)", md)
    if m:
        meta_put(con, "attendance_overall", {"pct": float(m.group(1)), "present": int(m.group(2)), "total": int(m.group(3))})
    sem = re.match(r"# Attendance - (.+)", md)
    if sem:
        meta_put(con, "semester", sem.group(1).strip())


def _parse_tt_date(label: str, start: datetime) -> datetime | None:
    # 'Mon 28 Sep' -> date; year inferred from the requested window (handles Dec -> Jan).
    try:
        d = datetime.strptime(f"{label} {start.year}", "%a %d %b %Y")
    except ValueError:
        try:
            d = datetime.strptime(label[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if (d - start.replace(tzinfo=None)).days < -200:
        d = d.replace(year=start.year + 1)
    return d.replace(tzinfo=TZ)


def collect_timetable(mcp: MCP, con: sqlite3.Connection) -> str:
    this_mon = week_monday(now_local())
    fetched = []
    for wk in (this_mon, this_mon + timedelta(days=7)):
        fetched.append((wk, mcp.call("mycamu_get_timetable", days_ahead=6, start_date=wk.strftime("%Y-%m-%d"))))
    out = []
    with tx(con):
        for wk, md in fetched:
            key, end = wk.strftime("%Y-%m-%d"), (wk + timedelta(days=6)).strftime("%Y-%m-%d")
            con.execute("DELETE FROM timetable WHERE date BETWEEN ? AND ?", (key, end))
            if md.startswith("MyCamu says"):
                con.execute("INSERT OR REPLACE INTO timetable_weeks VALUES(?,?,?,?)",
                            (key, "not_finalised", md.split(":", 1)[1].split("(")[0].strip(), now_ts()))
                out.append(f"{key}: not finalised")
                continue
            n = 0
            for d_label, times, subj, code, fac, typ in (r[:6] for r in _table_rows(md) if len(r) >= 6):
                d = _parse_tt_date(d_label, wk)
                tm = re.match(r"(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})", times)
                if not d or not tm:
                    continue
                st, en = (x.zfill(5) for x in tm.groups())
                sdt = d.replace(hour=int(st[:2]), minute=int(st[3:]))
                edt = d.replace(hour=int(en[:2]), minute=int(en[3:]))
                con.execute("INSERT OR REPLACE INTO timetable VALUES(?,?,?,?,?,?,?,?,?)",
                            (d.strftime("%Y-%m-%d"), st, en, int(sdt.timestamp()), int(edt.timestamp()),
                             code, subj, fac, typ))
                n += 1
            con.execute("INSERT OR REPLACE INTO timetable_weeks VALUES(?,?,?,?)", (key, "ok", None, now_ts()))
            out.append(f"{key}: {n} classes")
    return "; ".join(out)


_GRADE_LINE = re.compile(r"^- \*\*(.+?)\*\*: (.+?)(?: \(range (.+?)\))?(?: · (.+))?$")


def collect_grades(mcp: MCP, con: sqlite3.Connection) -> str:
    """Per current course: every grade item (markdown is small; JSON grade items get clipped)."""
    if now_ts() - (meta_get(con, "grades_at", 0) or 0) < GRADES_EVERY and not FORCE.get("grades"):
        return "fresh, skipped"
    today = now_local().strftime("%Y-%m-%d")
    best: dict[tuple[str, str], tuple] = {}
    for c in _courses(mcp):
        code = course_code(c.get("fullname"))
        if not code:
            continue
        md = mcp.call("moodle_get_grades", course_id=c["id"])
        for line in md.splitlines():
            m = _GRADE_LINE.match(line)
            if not m:
                continue
            name, grade, rng, _pct = m.groups()
            mx = None
            if rng:
                parts = re.findall(r"[\d.]+", rng)
                mx = float(parts[-1]) if parts else None
            key = (code, name.strip())
            # Same course code appears as several LMS courses (batch sections); keep a real grade over "—".
            if key not in best or best[key][3] in ("—", "-", ""):
                best[key] = (today, code, name.strip(), grade.strip(), mx)
    rows = list(best.values())
    with tx(con):
        con.execute("DELETE FROM grade_snap WHERE date=?", (today,))
        con.executemany("INSERT OR REPLACE INTO grade_snap VALUES(?,?,?,?,?)", rows)
        meta_put(con, "grades_at", now_ts())
    return f"{len(rows)} grade items"


def collect_camu_unread(mcp: MCP, con: sqlite3.Connection) -> str:
    txt = mcp.call("mycamu_get_notifications")
    m = re.search(r"(\d+) unread", txt)
    n = int(m.group(1)) if m else 0
    meta_put(con, "unread_mycamu", n)
    return f"{n} unread"


# --------------------------------------------------------------------------- v3: course hub, search index, feed

CONTENTS_EVERY = 2 * 3600        # course structure changes rarely
INDEX_PER_RUN = 5                # files text-indexed per run (each is a PDF/PPTX parse inside the MCP process)
INDEX_BUDGET_S = 170
INDEX_MAX_BYTES = 8 * 1024 * 1024
INDEX_MAX_CHARS = 40_000
READABLE = re.compile(r"\.(pdf|docx|pptx|xlsx|txt|md|csv|py|c|cpp|java|ipynb)$", re.I)
SEED_AGE = 3 * 86400             # on the very first feed sync only items this recent count as unread


def collect_courses(mcp: MCP, con: sqlite3.Connection) -> str:
    """Per current course: sections -> modules -> files, written only when something changed."""
    import hashlib
    now = now_ts()
    if now - (meta_get(con, "contents_at", 0) or 0) < CONTENTS_EVERY and not FORCE.get("courses"):
        return "fresh, skipped"
    courses = _courses(mcp)
    fetched = []
    for c in courses:
        code = course_code(c.get("fullname"))
        sections = mcp.call_json("moodle_get_course_contents", course_id=c["id"])
        fetched.append((c, code, sections, hashlib.sha1(json.dumps(sections, sort_keys=True).encode()).hexdigest()))
    new_mods = new_files = changed = 0
    with tx(con):
        live_ids = {c["id"] for c in courses}
        for c, code, sections, h in fetched:
            row = con.execute("SELECT hash FROM courses WHERE id=?", (c["id"],)).fetchone()
            con.execute(
                "INSERT INTO courses(id,code,name,title,last_seen,contents_at,hash) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET code=excluded.code, name=excluded.name, title=excluded.title, "
                "last_seen=excluded.last_seen, contents_at=excluded.contents_at, hash=excluded.hash",
                (c["id"], code, html_unescape(c.get("fullname")), course_title(c.get("fullname")), now, now, h))
            if row and row["hash"] == h:
                continue
            changed += 1
            # First time we see a course: everything is baseline (first_seen=0), not "new".
            baseline = con.execute("SELECT 1 FROM modules WHERE course_id=? LIMIT 1", (c["id"],)).fetchone() is None
            seen_cm: set[int] = set()
            seen_files: set[str] = set()
            for si, sec in enumerate(sections):
                sname = sec.get("name") or f"Section {sec.get('section', si)}"
                for pos, m in enumerate(sec.get("modules") or []):
                    cm = m.get("id")
                    if not cm or m.get("modname") in ("label",) and not m.get("description"):
                        continue
                    seen_cm.add(cm)
                    dates = json.dumps(m.get("dates") or [])
                    ex = con.execute("SELECT 1 FROM modules WHERE cmid=?", (cm,)).fetchone()
                    if not ex:
                        new_mods += 0 if baseline else 1
                    con.execute(
                        "INSERT INTO modules(cmid,course_id,section_idx,section,pos,modname,name,description,url,dates,first_seen,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cmid) DO UPDATE SET course_id=excluded.course_id, "
                        "section_idx=excluded.section_idx, section=excluded.section, pos=excluded.pos, modname=excluded.modname, "
                        "name=excluded.name, description=excluded.description, url=excluded.url, dates=excluded.dates, updated_at=excluded.updated_at",
                        (cm, c["id"], si, sname, pos, m.get("modname"), m.get("name") or "", m.get("description") or "",
                         m.get("url"), dates, 0 if baseline else now, now))
                    upsert_doc(con, "module", f"mod:{cm}", code, m.get("name") or "",
                               f"{sname}. {m.get('description') or ''}", m.get("url"))
                    for f in m.get("files") or []:
                        u = f.get("fileurl")
                        if not u:
                            continue
                        seen_files.add(u)
                        exf = con.execute("SELECT modified FROM files WHERE url=?", (u,)).fetchone()
                        if not exf:
                            new_files += 0 if baseline else 1
                        con.execute(
                            "INSERT INTO files(url,cmid,course_id,filename,size,modified,first_seen) VALUES(?,?,?,?,?,?,?) "
                            "ON CONFLICT(url) DO UPDATE SET cmid=excluded.cmid, filename=excluded.filename, size=excluded.size, "
                            "index_state=CASE WHEN files.modified IS NOT excluded.modified THEN NULL ELSE files.index_state END, "
                            "modified=excluded.modified",
                            (u, cm, c["id"], f.get("filename") or "", f.get("filesize") or 0, f.get("timemodified") or 0,
                             0 if baseline else now))
                        upsert_doc(con, "file", f"file:{u}", code, f.get("filename") or "", f"{m.get('name') or ''} · {sname}", u)
            # Anything no longer visible in this course goes away (and out of search).
            for r in con.execute("SELECT cmid FROM modules WHERE course_id=?", (c["id"],)).fetchall():
                if r["cmid"] not in seen_cm:
                    con.execute("DELETE FROM modules WHERE cmid=?", (r["cmid"],))
                    con.execute("DELETE FROM docs WHERE ref=?", (f"mod:{r['cmid']}",))
            for r in con.execute("SELECT url FROM files WHERE course_id=?", (c["id"],)).fetchall():
                if r["url"] not in seen_files:
                    con.execute("DELETE FROM files WHERE url=?", (r["url"],))
                    con.execute("DELETE FROM docs WHERE ref=?", (f"file:{r['url']}",))
        # Ended courses disappear from the hub (their rows stay out of the way).
        for r in con.execute("SELECT id FROM courses").fetchall():
            if r["id"] not in live_ids:
                con.execute("DELETE FROM courses WHERE id=?", (r["id"],))
        meta_put(con, "contents_at", now)
    return f"{len(courses)} courses ({changed} changed), {new_mods} new items, {new_files} new files"


def _hid(*parts: Any) -> str:
    import hashlib
    return hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:12]


def html_unescape(s: str | None) -> str:
    import html as _h
    return _h.unescape(s or "")


def collect_index(mcp: MCP, con: sqlite3.Connection) -> str:
    """Extract text from a few not-yet-indexed course files per run so search can look inside them."""
    t0 = time.monotonic()
    todo = con.execute(
        "SELECT f.url, f.filename, f.size, c.code FROM files f LEFT JOIN courses c ON c.id=f.course_id "
        "WHERE f.index_state IS NULL ORDER BY f.modified DESC, f.first_seen DESC LIMIT 60").fetchall()
    done = 0
    for r in todo:
        if done >= INDEX_PER_RUN or time.monotonic() - t0 > INDEX_BUDGET_S:
            break
        state, text, note = "ok", "", None
        if not READABLE.search(r["filename"] or "") or (r["size"] or 0) > INDEX_MAX_BYTES:
            state, note = "skip", "type or size"
        else:
            # Mark first: if parsing this file crashes the MCP server, we don't retry it forever.
            with tx(con):
                con.execute("UPDATE files SET index_state='error', note='crashed while parsing' WHERE url=?", (r["url"],))
            try:
                out = mcp.call("moodle_read_file", file_url=r["url"], max_chars=INDEX_MAX_CHARS)
                body = re.sub(r"\A# .*\n", "", out).strip()
                body = re.sub(r"--- (page|slide) \d+ ---\n?", " ", body)
                if "Binary file type" in body[:200] or len(body) < 40:
                    state, note = "empty", "no extractable text"
                else:
                    text = re.sub(r"\s+", " ", body)[:INDEX_MAX_CHARS]
            except ToolError as e:
                state, note = "error", str(e)[:200]
        with tx(con):
            con.execute("UPDATE files SET index_state=?, indexed_at=?, chars=?, note=? WHERE url=?",
                        (state, now_ts(), len(text), note, r["url"]))
            if text:
                con.execute("UPDATE docs SET body=? WHERE ref=?", (f"{text}", f"file:{r['url']}"))
        done += 1
    left = con.execute("SELECT COUNT(*) FROM files WHERE index_state IS NULL").fetchone()[0]
    return f"{done} files indexed, {left} waiting"


def _ts_any(v: Any) -> int:
    """Epoch seconds from an int/float/ISO string; 0 if unknown."""
    if isinstance(v, (int, float)):
        return int(v / 1000) if v > 1e11 else int(v)
    if isinstance(v, str) and v.strip():
        s = v.strip()
        if s.isdigit():
            return _ts_any(int(s))
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int((d if d.tzinfo else d.replace(tzinfo=TZ)).timestamp())
        except ValueError:
            for fmt in ("%d-%m-%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%Y-%m-%d"):
                try:
                    return int(datetime.strptime(s[:19], fmt).replace(tzinfo=TZ).timestamp())
                except ValueError:
                    continue
    return 0


def collect_feed(mcp: MCP, con: sqlite3.Connection) -> str:
    """Moodle notifications + course announcements + MyCamu announcements -> one feed."""
    rows: list[tuple] = []   # (id, source, title, body, url, posted_at, src_read, course_code, author)
    errs: list[str] = []
    try:
        for n in mcp.call_json("moodle_get_notifications", limit=30):
            body = (n.get("fullmessage") or n.get("smallmessage") or "").strip()
            rows.append((f"moodle:n:{n.get('id')}", "moodle", (n.get("subject") or "")[:300], body[:1200],
                         n.get("contexturl"), _ts_any(n.get("timecreated")), 1 if n.get("read") else 0,
                         course_code(n.get("subject")), None))
    except ToolError as e:
        errs.append(f"notifications: {e}")
    for c in _courses(mcp):
        code = course_code(c.get("fullname"))
        try:
            for a in mcp.call_json("moodle_get_announcements", course_id=c["id"], limit=5):
                posted = _ts_any(a.get("posted"))
                title = (a.get("subject") or "")[:300]
                atts = a.get("attachments") or []
                body = (a.get("message") or "") + ("".join(f"\n📎 {x.get('name')}" for x in atts))
                uid = f"moodle:a:{c['id']}:{_hid(title, posted)}"
                rows.append((uid, "course", title, body[:1500], atts[0]["url"] if atts and atts[0].get("url") else None,
                             posted, 0, code, a.get("author")))
        except ToolError as e:
            errs.append(f"{code} announcements: {e}")
    try:
        raw = mcp.call("mycamu_get_announcements", days_back=30, raw=True)
        data = json.loads(raw) if raw.startswith("[") else []
        for r in data:
            keys = {k.lower(): k for k in r}
            def pick(*pats: str) -> str:
                for pat in pats:
                    for lk, k in keys.items():
                        if re.search(pat, lk) and isinstance(r[k], str):
                            return r[k]
                return ""
            title = pick(r"title|subject|heading|name") or next((v for v in r.values() if isinstance(v, str) and len(v) > 8), "Announcement")
            body = pick(r"message|desc|content|body|text|detail")
            posted = _ts_any(pick(r"date|time|created|posted|start") or next((v for k, v in r.items() if re.search("date|time", k, re.I)), 0))
            uid = f"camu:a:{_hid(title[:80], posted)}"
            rows.append((uid, "mycamu", title[:300], body[:1500], None, posted, 0, "", pick(r"author|by|sender|from") or None))
    except (ToolError, ValueError) as e:
        errs.append(f"mycamu announcements: {e}")
    first = con.execute("SELECT COUNT(*) FROM feed").fetchone()[0] == 0
    cutoff = now_ts() - SEED_AGE
    with tx(con):
        for fid, src, title, body, url, posted, sread, code, author in rows:
            ex = con.execute("SELECT read FROM feed WHERE id=?", (fid,)).fetchone()
            read = ex["read"] if ex else (1 if (sread or (first and posted and posted < cutoff)) else 0)
            con.execute(
                "INSERT INTO feed(id,source,title,body,url,posted_at,read,course_code,author,src_read) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title, body=excluded.body, url=excluded.url, "
                "posted_at=excluded.posted_at, course_code=excluded.course_code, author=excluded.author, src_read=excluded.src_read",
                (fid, src, title, body, url, posted, read, code, author, sread))
            upsert_doc(con, "feed", f"feed:{fid}", code, title, body, url)
        con.execute("DELETE FROM feed WHERE posted_at < ?", (now_ts() - 120 * 86400,))
    if errs and not rows:
        raise ToolError("; ".join(errs)[:400])
    return f"{len(rows)} posts" + (f" (partial: {'; '.join(errs)[:160]})" if errs else "")


# --------------------------------------------------------------------------- runner

SOURCES: dict[str, tuple[str, Callable[[MCP, sqlite3.Connection], str]]] = {
    "moodle": ("moodle", collect_moodle),
    "attendance": ("mycamu", collect_attendance),
    "timetable": ("mycamu", collect_timetable),
    "camu_unread": ("mycamu", collect_camu_unread),
    "grades": ("moodle", collect_grades),
    "courses": ("moodle", collect_courses),
    "feed": ("moodle", collect_feed),
    "index": ("moodle", collect_index),
}


def run(only: set[str] | None = None) -> int:
    init_db()
    LOCK.touch(exist_ok=True)
    with open(LOCK) as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another collector run is in progress; skipping")
            return 0
        con = db()
        mcp = MCP()
        failures = 0
        camu_dead = False
        try:
            for name, (group, fn) in SOURCES.items():
                if only and name not in only and group not in only:
                    continue
                if group == "mycamu" and camu_dead:
                    _log_sync(con, name, False, "skipped: MyCamu session expired", 0)
                    continue
                t0 = time.monotonic()
                try:
                    msg = fn(mcp, con)
                    _log_sync(con, name, True, None, int((time.monotonic() - t0) * 1000))
                    log(f"{name}: {msg}")
                except (ToolError, MCPDown) as e:
                    failures += 1
                    err = str(e)
                    _log_sync(con, name, False, err[:500], int((time.monotonic() - t0) * 1000))
                    log(f"{name}: FAILED {err[:200]}")
                    if isinstance(e, MCPDown):
                        log("MCP server unreachable; stopping this run")
                        break
                    if group == "mycamu" and ("expired" in err.lower() or "isn't connected" in err):
                        camu_dead = True
                except Exception as e:  # noqa: BLE001 - never let one source kill the run
                    failures += 1
                    _log_sync(con, name, False, f"{type(e).__name__}: {e}"[:500], int((time.monotonic() - t0) * 1000))
                    log(f"{name}: CRASHED {type(e).__name__}: {e}")
            con.execute("DELETE FROM sync_log WHERE ran_at < ?", (now_ts() - 30 * 86400,))
            con.execute("DELETE FROM login_tokens WHERE expires_at < ?", (now_ts() - 86400,))
            meta_put(con, "last_run", now_ts())
        finally:
            mcp.close()
            con.close()
        return 1 if failures else 0


def _log_sync(con: sqlite3.Connection, source: str, ok: bool, error: str | None, ms: int) -> None:
    con.execute("INSERT INTO sync_log(source,ran_at,ok,error,ms) VALUES(?,?,?,?,?)",
                (source, now_ts(), 1 if ok else 0, error, ms))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="sources or groups: moodle attendance timetable camu_unread mycamu")
    a = ap.parse_args()
    for k in ("grades", "courses"):
        if a.only and k in a.only:
            FORCE[k] = True
    sys.exit(run(set(a.only) if a.only else None))


if __name__ == "__main__":
    main()
