"""v3 endpoints: course hub, search, feed, notes + planner, Ask, control panel, palette.

Registered by app.py (`routes += v3.build(...)`). Handlers reuse app.py's `endpoint` wrapper (auth, CSRF,
thread pool for sync functions) and its item helpers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.routing import Route

from . import ask as ASK
from . import common
from .common import APP_DIR, ATT_TARGET, DB_PATH, TZ, db, meta_get, meta_put, now_local, now_ts

START = time.time()
MARK_IN, MARK_OUT = "\x01", "\x02"
SOURCES = ("moodle", "attendance", "timetable", "camu_unread", "grades", "courses", "feed", "index")
WEIGHT = {"exam": 4.0, "quiz": 2.0, "assign": 1.5, "event": 0.6}


def build(A: Any) -> list[Route]:
    """A = the dashboard.app module (for endpoint(), _rows(), _all_items(), ...)."""
    endpoint, _rows = A.endpoint, A._rows

    # ------------------------------------------------------------------ courses

    def _visits() -> dict[int, int]:
        return {r["course_id"]: r["visited_at"] for r in _rows("SELECT * FROM course_visits")}

    @endpoint()
    def courses(_: Request, __: dict) -> dict:
        vis = _visits()
        att = {a["subject_code"]: a for a in A._attendance_latest()}
        items = [i for i in A._all_items(0) if not i["done"] and i.get("due_at")]
        out = []
        for c in _rows("SELECT * FROM courses ORDER BY title"):
            cid, seen = c["id"], vis.get(c["id"], 0)
            n_mod = _rows("SELECT COUNT(*) n, SUM(first_seen>? AND first_seen>0) fresh FROM modules WHERE course_id=?", seen, cid)[0]
            n_file = _rows("SELECT COUNT(*) n, SUM(first_seen>? AND first_seen>0) fresh, "
                           "SUM(index_state='ok') idx FROM files WHERE course_id=?", seen, cid)[0]
            code = c["code"] or ""
            nxt = next((i for i in items if i.get("course_code") == code), None)
            a = att.get(code)
            todos = _rows("SELECT COUNT(*) n FROM notes WHERE course_code=? AND kind='todo' AND done=0", code)[0]["n"]
            out.append({"id": cid, "code": code, "title": c["title"], "modules": n_mod["n"] or 0, "files": n_file["n"] or 0,
                        "indexed": n_file["idx"] or 0, "new": (n_mod["fresh"] or 0) + (n_file["fresh"] or 0),
                        "todos": todos, "attendance": a["pct"] if a and a["total"] else None,
                        "next": {"title": nxt["title"], "due_at": nxt["due_at"], "type": nxt["type"]} if nxt else None})
        return {"courses": out, "indexed_state": _index_counts(), "sync": A._sync_state()}

    def _index_counts() -> dict:
        r = _rows("SELECT COUNT(*) total, SUM(index_state='ok') ok, SUM(index_state IS NULL) todo, "
                  "SUM(index_state IN ('empty','skip','error')) skipped FROM files")[0]
        return {k: r[k] or 0 for k in ("total", "ok", "todo", "skipped")}

    @endpoint()
    def course(request: Request, _: dict) -> dict:
        cid = int(request.path_params["cid"])
        c = _rows("SELECT * FROM courses WHERE id=?", cid)
        if not c:
            raise HTTPException(404, "Course not found")
        c = c[0]
        seen = _visits().get(cid, 0)
        files: dict[int, list[dict]] = {}
        for f in _rows("SELECT * FROM files WHERE course_id=? ORDER BY filename", cid):
            files.setdefault(f["cmid"], []).append({
                "url": f["url"], "name": f["filename"], "size": f["size"], "modified": f["modified"],
                "state": f["index_state"], "new": bool(f["first_seen"] and f["first_seen"] > seen)})
        sections: list[dict] = []
        for m in _rows("SELECT * FROM modules WHERE course_id=? ORDER BY section_idx, pos", cid):
            if not sections or sections[-1]["idx"] != m["section_idx"]:
                sections.append({"idx": m["section_idx"], "name": m["section"], "modules": [], "new": 0})
            new = bool(m["first_seen"] and m["first_seen"] > seen)
            fs = files.get(m["cmid"], [])
            sections[-1]["modules"].append({
                "cmid": m["cmid"], "modname": m["modname"], "name": m["name"], "description": (m["description"] or "")[:500],
                "url": m["url"], "dates": json.loads(m["dates"] or "[]"), "new": new, "files": fs})
            sections[-1]["new"] += int(new) + sum(f["new"] for f in fs)
        code = c["code"] or ""
        notes = _rows("SELECT * FROM notes WHERE course_code=? ORDER BY done, id DESC", code) if code else []
        items = [i for i in A._all_items(14) if code and i.get("course_code") == code and not i["done"]][:12]
        feed = _rows("SELECT id, source, title, posted_at, read FROM feed WHERE course_code=? ORDER BY posted_at DESC LIMIT 6", code) if code else []
        att = next((a for a in A._attendance_latest() if a["subject_code"] == code), None)
        return {"id": cid, "code": code, "title": c["title"], "name": c["name"], "sections": sections, "notes": notes,
                "items": items, "feed": feed, "attendance": att, "visited_at": seen, "sync": A._sync_state()}

    @endpoint()
    def course_visit(request: Request, _: dict) -> dict:
        con = db()
        con.execute("INSERT INTO course_visits(course_id, visited_at) VALUES(?,?) ON CONFLICT(course_id) DO UPDATE SET visited_at=excluded.visited_at",
                    (int(request.path_params["cid"]), now_ts()))
        con.close()
        return {"ok": True}

    async def file_view(request: Request) -> Any:
        """Preview: indexed text from the DB when we have it, else read it live through the MCP tool."""
        from starlette.responses import JSONResponse
        if not A._check_session(request.cookies.get(A.COOKIE)):
            raise HTTPException(401, "login required")
        url = request.query_params.get("url", "")
        f = _rows("SELECT * FROM files WHERE url=?", url)
        if not f:
            raise HTTPException(404, "Unknown file")
        f = f[0]
        d = _rows("SELECT body FROM docs WHERE ref=?", f"file:{url}")
        text = (d[0]["body"] if d else "") if f["index_state"] == "ok" else ""
        live = False
        if not text:
            m = ASK._Mcp()
            try:
                out = await m.call("moodle_read_file", file_url=url, max_chars=20000)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(502, "Couldn't read the file from the LMS right now.") from e
            finally:
                await m.c.aclose()
            if out.startswith("Error"):
                raise HTTPException(502, out[:300])
            text = re.sub(r"\A# .*\n", "", out).strip()
            live = True
        return JSONResponse({"name": f["filename"], "size": f["size"], "text": text[:40000], "truncated": len(text) > 40000,
                             "live": live, "lms_url": None})

    # ------------------------------------------------------------------ search + palette

    def fts_query(q: str) -> str:
        toks = re.findall(r"[\w+#]{2,}", q.lower())[:8]
        return " ".join(f'"{t}"*' for t in toks)

    @endpoint()
    def search(request: Request, _: dict) -> dict:
        q = request.query_params.get("q", "").strip()[:100]
        course = request.query_params.get("course", "").strip().upper()
        kind = request.query_params.get("kind", "")
        if len(q) < 2:
            return {"results": [], "q": q}
        rows: list[dict] = []
        con = db()
        try:
            fts = fts_query(q)
            if common.HAS_FTS and fts:
                sql = ("SELECT d.kind, d.ref, d.course, d.title, d.url, COALESCE(m.course_id, f.course_id) AS cid, "
                       f"snippet(docs_fts, 1, '{MARK_IN}', '{MARK_OUT}', ' … ', 20) AS snip "
                       "FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid "
                       "LEFT JOIN modules m ON d.ref = 'mod:' || m.cmid LEFT JOIN files f ON d.ref = 'file:' || f.url "
                       "WHERE docs_fts MATCH ?")
                args: list[Any] = [fts]
                if course:
                    sql += " AND d.course = ?"; args.append(course)
                if kind:
                    sql += " AND d.kind = ?"; args.append(kind)
                sql += " ORDER BY bm25(docs_fts, 6.0, 1.0) LIMIT 30"
                try:
                    rows = [dict(r) for r in con.execute(sql, args)]
                except Exception:  # noqa: BLE001 — bad MATCH syntax
                    rows = []
            if not rows:
                like = f"%{q}%"
                rows = [dict(r, snip="") for r in con.execute(
                    "SELECT d.kind, d.ref, d.course, d.title, d.url, COALESCE(m.course_id, f.course_id) AS cid FROM docs d "
                    "LEFT JOIN modules m ON d.ref = 'mod:' || m.cmid LEFT JOIN files f ON d.ref = 'file:' || f.url "
                    "WHERE (d.title LIKE ? OR d.body LIKE ?)" + (" AND d.course=?" if course else "") + (" AND d.kind=?" if kind else "")
                    + " LIMIT 30", [like, like] + ([course] if course else []) + ([kind] if kind else []))]
        finally:
            con.close()
        for r in rows:
            r["ref"] = r["ref"][:200]
        return {"results": rows, "q": q, "marks": [MARK_IN, MARK_OUT]}

    @endpoint()
    def palette(_: Request, __: dict) -> dict:
        items = [{"id": i["id"], "title": i["title"], "course_code": i.get("course_code"), "due_at": i.get("due_at"),
                  "type": i["type"]} for i in A._all_items(7) if not i["done"]][:120]
        return {"courses": [{"id": c["id"], "code": c["code"], "title": c["title"]} for c in _rows("SELECT id, code, title FROM courses ORDER BY title")],
                "items": items}

    # ------------------------------------------------------------------ feed

    @endpoint()
    def feed(request: Request, _: dict) -> dict:
        qp = request.query_params
        where, args = [], []
        if qp.get("source"):
            where.append("source=?"); args.append(qp["source"])
        if qp.get("course"):
            where.append("course_code=?"); args.append(qp["course"])
        if qp.get("unread") == "1":
            where.append("read=0")
        q = qp.get("q", "").strip()[:80]
        if q:
            where.append("(title LIKE ? OR body LIKE ?)"); args += [f"%{q}%"] * 2
        before = int(qp.get("before", "0") or 0)
        if before:
            where.append("posted_at < ?"); args.append(before)
        rows = _rows("SELECT * FROM feed" + (" WHERE " + " AND ".join(where) if where else "") +
                     " ORDER BY posted_at DESC, id LIMIT 41", *args)
        more = len(rows) > 40
        counts = {r["source"]: r["n"] for r in _rows("SELECT source, COUNT(*) n FROM feed WHERE read=0 GROUP BY source")}
        return {"items": rows[:40], "more": more, "unread": counts, "sync": A._sync_state()}

    @endpoint()
    def feed_read(_: Request, body: dict) -> dict:
        read = 0 if body.get("read") is False else 1
        con = db()
        if body.get("all"):
            src = body.get("source")
            con.execute("UPDATE feed SET read=?" + (" WHERE source=?" if src else ""), [read] + ([src] if src else []))
        else:
            ids = [str(x) for x in (body.get("ids") or [])][:100]
            con.execute(f"UPDATE feed SET read=? WHERE id IN ({','.join('?' * len(ids))})", [read] + ids) if ids else None
        con.close()
        return {"ok": True}

    # ------------------------------------------------------------------ notes + planner

    @endpoint()
    def notes_list(request: Request, _: dict) -> list[dict]:
        code = request.query_params.get("course", "")
        return _rows("SELECT * FROM notes" + (" WHERE course_code=?" if code else "") + " ORDER BY done, id DESC LIMIT 500", *([code] if code else []))

    def _clean_note(body: dict, partial: bool = False) -> dict:
        out: dict[str, Any] = {}
        if "text" in body or not partial:
            t = str(body.get("text", "")).strip()[:2000]
            if not t:
                raise HTTPException(400, "Text is required")
            out["text"] = t
        if "kind" in body or not partial:
            k = body.get("kind", "todo")
            if k not in ("note", "todo"):
                raise HTTPException(400, "kind must be note or todo")
            out["kind"] = k
        if "course_code" in body or not partial:
            c = str(body.get("course_code") or "")[:20]
            if c and not re.fullmatch(r"[A-Z0-9]+", c):
                raise HTTPException(400, "bad course code")
            out["course_code"] = c
        if "done" in body:
            out["done"] = 1 if body["done"] else 0
        if "due_at" in body:
            d = body["due_at"]
            out["due_at"] = int(d) if isinstance(d, (int, float)) and d else None
        return out

    @endpoint()
    def note_add(_: Request, body: dict) -> dict:
        n = _clean_note(body)
        con = db()
        if con.execute("SELECT COUNT(*) FROM notes").fetchone()[0] >= 2000:
            con.close(); raise HTTPException(400, "Too many notes")
        cur = con.execute("INSERT INTO notes(course_code,kind,text,done,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                          (n["course_code"], n["kind"], n["text"], n.get("done", 0), n.get("due_at"), now_ts(), now_ts()))
        nid = cur.lastrowid
        common.upsert_doc(con, "note", f"note:{nid}", n["course_code"], n["text"][:80], n["text"], None)
        con.close()
        return {"id": nid}

    @endpoint()
    def note_update(request: Request, body: dict) -> dict:
        nid = int(request.path_params["nid"])
        n = _clean_note(body, partial=True)
        if not n:
            return {"ok": True}
        con = db()
        sets = ", ".join(f"{k}=?" for k in n)
        con.execute(f"UPDATE notes SET {sets}, updated_at=? WHERE id=?", [*n.values(), now_ts(), nid])
        r = con.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
        if r:
            common.upsert_doc(con, "note", f"note:{nid}", r["course_code"], r["text"][:80], r["text"], None)
        con.close()
        return {"ok": True}

    @endpoint()
    def note_del(request: Request, _: dict) -> dict:
        nid = int(request.path_params["nid"])
        con = db()
        con.execute("DELETE FROM notes WHERE id=?", (nid,))
        con.execute("DELETE FROM docs WHERE ref=?", (f"note:{nid}",))
        con.close()
        return {"ok": True}

    @endpoint()
    def planner(request: Request, _: dict) -> dict:
        weeks = max(1, min(8, int(request.query_params.get("weeks", "4") or 4)))
        now = now_local()
        start = A.week_monday(now)
        days: dict[str, dict] = {}
        for i in range(weeks * 7):
            d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            days[d] = {"date": d, "load": 0.0, "items": [], "classes": 0}
        stamp = lambda ts: datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")   # noqa: E731
        for it in A._all_items(7):
            if it["done"] or not it.get("due_at"):
                continue
            d = days.get(stamp(it["due_at"]))
            if d is None:
                continue
            d["load"] += WEIGHT.get(it["type"], 1.0)
            d["items"].append({"id": it["id"], "title": it["title"], "type": it["type"], "course_code": it.get("course_code"),
                               "due_at": it["due_at"], "overdue": it["due_at"] < now.timestamp()})
        todos = _rows("SELECT * FROM notes WHERE kind='todo' AND done=0 ORDER BY due_at IS NULL, due_at, id DESC LIMIT 100")
        for t in todos:
            if t["due_at"] and (d := days.get(stamp(t["due_at"]))):
                d["load"] += 0.8
                d["items"].append({"id": f"note:{t['id']}", "title": t["text"][:80], "type": "todo", "course_code": t["course_code"],
                                   "due_at": t["due_at"], "overdue": t["due_at"] < now.timestamp()})
        for r in _rows("SELECT date, COUNT(*) n FROM timetable GROUP BY date"):
            if r["date"] in days:
                days[r["date"]]["classes"] = r["n"]
        out = list(days.values())
        for d in out:
            d["items"].sort(key=lambda x: x["due_at"])
            l = d["load"]
            d["level"] = 0 if l == 0 else 1 if l <= 1.6 else 2 if l <= 3.2 else 3 if l <= 5.5 else 4
            d["load"] = round(l, 1)
        return {"today": now.strftime("%Y-%m-%d"), "days": out, "todos": todos,
                "courses": [{"code": c["code"], "title": c["title"]} for c in _rows("SELECT DISTINCT code, title FROM courses WHERE code!='' ORDER BY title")]}

    # ------------------------------------------------------------------ Ask

    async def ask_route(request: Request) -> Any:
        from starlette.responses import JSONResponse
        if not A._check_session(request.cookies.get(A.COOKIE)):
            raise HTTPException(401, "login required")
        if request.headers.get("x-sb") != "1":
            raise HTTPException(403, "missing header")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "bad JSON") from None
        q = str((body or {}).get("q", "")).strip()
        if not q:
            raise HTTPException(400, "Ask something first.")
        try:
            res = await ASK.ask(q, (body or {}).get("history") or [])
        except ASK.AskError as e:
            return JSONResponse({"error": str(e), "used": ASK.used_today(), "limit": ASK.LIMIT}, status_code=200)
        return JSONResponse(res)

    # ------------------------------------------------------------------ control panel

    def _meminfo() -> dict:
        out: dict[str, float] = {}
        try:
            for line in open("/proc/meminfo"):
                k, v = line.split(":", 1)
                out[k] = int(v.split()[0]) / 1024
        except OSError:
            pass
        rss = 0.0
        try:
            for line in open("/proc/self/status"):
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) / 1024
        except OSError:
            pass
        cg = cg_max = None
        try:  # this process's own systemd unit (moodle-mcp.service), not the whole machine
            rel = open("/proc/self/cgroup").read().strip().split("::", 1)[1].strip() if "::" in open("/proc/self/cgroup").read() else ""
            base = "/sys/fs/cgroup" + rel
            cg = int(open(base + "/memory.current").read()) / 1048576
            mx = open(base + "/memory.max").read().strip()
            cg_max = None if mx == "max" else round(int(mx) / 1048576)
        except (OSError, ValueError, IndexError):
            cg = None
        return {"available_mb": round(out.get("MemAvailable", 0)), "total_mb": round(out.get("MemTotal", 0)),
                "swap_used_mb": round(out.get("SwapTotal", 0) - out.get("SwapFree", 0)), "swap_total_mb": round(out.get("SwapTotal", 0)),
                "process_rss_mb": round(rss), "cgroup_mb": round(cg) if cg is not None else None, "cgroup_max_mb": cg_max}

    @endpoint()
    def control(_: Request, __: dict) -> dict:
        day_ago = now_ts() - 86400
        sources = []
        for s in SOURCES:
            last = _rows("SELECT ran_at, ok, error, ms FROM sync_log WHERE source=? ORDER BY id DESC LIMIT 1", s)
            ok = _rows("SELECT MAX(ran_at) t FROM sync_log WHERE source=? AND ok=1", s)[0]["t"]
            agg = _rows("SELECT COUNT(*) n, SUM(ok=0) bad, AVG(ms) ms FROM sync_log WHERE source=? AND ran_at>?", s, day_ago)[0]
            sources.append({"name": s, "last_ok": ok, "last_try": last[0]["ran_at"] if last else None,
                            "ok": bool(last[0]["ok"]) if last else None,
                            "error": last[0]["error"] if last and not last[0]["ok"] else None,
                            "runs24": agg["n"] or 0, "fails24": agg["bad"] or 0, "avg_ms": int(agg["ms"] or 0)})
        camu = [s for s in sources if s["name"] in ("attendance", "timetable")]
        err = next((s["error"] for s in camu if s["error"]), "") or ""
        if not any(s["last_try"] for s in camu):
            camu_state = "unknown"
        elif re.search(r"expired|isn't connected|not set|cookie", err, re.I):
            camu_state = "expired"
        elif any(s["ok"] is False for s in camu):
            camu_state = "error"
        else:
            camu_state = "ok"
        con = db()
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("items", "courses", "modules", "files", "docs", "feed", "notes", "exams", "manual_entries")}
        cursor = {"last_run": meta_get(con, "last_run")}
        con.close()
        try:
            db_mb = round(os.path.getsize(DB_PATH) / 1048576, 1)
        except OSError:
            db_mb = None
        du = shutil.disk_usage(str(APP_DIR))
        return {"sources": sources, "camu": {"state": camu_state, "error": err or None}, "index": _index_counts(),
                "counts": counts, "last_run": cursor["last_run"], "fts": common.HAS_FTS,
                "system": {**_meminfo(), "db_mb": db_mb, "uptime_s": int(time.time() - START),
                           "disk_free_gb": round(du.free / 1e9, 1)},
                "ai": {"configured": bool(ASK.GROQ_KEY), "used": ASK.used_today(), "limit": ASK.LIMIT, "models": ASK.MODELS},
                "ics": bool(A.ICS_TOKEN), "version": "v3"}

    @endpoint()
    def control_action(_: Request, body: dict) -> dict:
        act = body.get("action")
        con = db()
        try:
            if act == "retry_index":
                con.execute("UPDATE files SET index_state=NULL WHERE index_state IN ('error','empty')")
                only = ["index"]
            elif act == "reindex_all":
                con.execute("UPDATE files SET index_state=NULL")
                only = ["index"]
            elif act == "refresh_courses":
                meta_put(con, "contents_at", 0)
                only = ["courses", "index", "feed"]
            else:
                raise HTTPException(400, "unknown action")
            wait = A.SYNC_EVERY - (now_ts() - (meta_get(con, "last_manual_sync", 0) or 0))
            if wait > 0:
                return {"started": False, "queued": True, "note": f"Queued — the next automatic run (≤30 min) will do it. Manual syncs are limited to one per {A.SYNC_EVERY}s."}
            meta_put(con, "last_manual_sync", now_ts())
        finally:
            con.close()
        subprocess.Popen([sys.executable, "-m", "dashboard.collector", "--only", *only], cwd=APP_DIR,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return {"started": True}

    P = "/dash/api"
    return [
        Route(f"{P}/courses", courses),
        Route(P + "/courses/{cid:int}", course),
        Route(P + "/courses/{cid:int}/visit", course_visit, methods=["POST"]),
        Route(f"{P}/file", file_view),
        Route(f"{P}/search", search),
        Route(f"{P}/palette", palette),
        Route(f"{P}/feed", feed, methods=["GET"]),
        Route(f"{P}/feed/read", feed_read, methods=["POST"]),
        Route(f"{P}/notes", notes_list, methods=["GET"]),
        Route(f"{P}/notes", note_add, methods=["POST"]),
        Route(P + "/notes/{nid:int}", note_update, methods=["POST"]),
        Route(P + "/notes/{nid:int}", note_del, methods=["DELETE"]),
        Route(f"{P}/planner", planner),
        Route(f"{P}/ask", ask_route, methods=["POST"]),
        Route(f"{P}/control", control),
        Route(f"{P}/control/action", control_action, methods=["POST"]),
    ]
