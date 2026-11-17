"""Parse a CoE exam date sheet (PDF or pasted text) into exam rows for *your* courses.

Used by the Telegram bot: forward the PDF (or the text) to the bot, it calls parse_pdf()/parse_text()
with the course codes it knows (timetable/attendance/LMS), shows the rows, and saves them on confirm.

Date sheets vary a lot, so this is deliberately forgiving:
  - rows come from pdfplumber tables first, then plain text lines
  - a date (or time) seen in an earlier row carries forward to rows that don't repeat it
    (merged cells in the PDF come through as empty)
  - dates: 16-11-2026, 16/11/26, 16.11.2026, 2026-11-16, 16 Nov 2026, 16th November, Nov 16, 2026
  - times: 10:00 AM - 1:00 PM, 10.00-13.00, 2 PM; sessions FN/AN/Morning/Evening (time marked unknown)
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Kolkata")
CODE = re.compile(r"\b([A-Z]{2,5})\s?-?\s?(\d{3,5}[A-Z]?)\b")
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*"

D_NUM = re.compile(r"\b(\d{1,2})([./-])(\d{1,2})\2(\d{4}|\d{2})\b")  # same separator twice
D_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
D_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s.\-/]*{MON}[\s.,\-/']*(\d{{4}}|\d{{2}})?\b", re.I)
D_MDY = re.compile(rf"\b{MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*(\d{{4}})?\b", re.I)

AMPM = r"\s*([ap])\.?\s?m\.?"
T_RANGE = re.compile(rf"\b(\d{{1,2}})(?:[:.](\d{{2}}))?(?:{AMPM})?\s*(?:-|–|—|to)\s*(\d{{1,2}})(?:[:.](\d{{2}}))?(?:{AMPM})?", re.I)
T_ONE = re.compile(rf"\b(\d{{1,2}})(?:[:.](\d{{2}}))?{AMPM}", re.I)
SESSIONS = [(re.compile(r"\b(FN|forenoon|morning)\b", re.I), (9, 30)),
            (re.compile(r"\b(AN|afternoon)\b"), (14, 0)),
            (re.compile(r"\bevening\b", re.I), (17, 0))]
KIND = [(re.compile(r"mid[\s-]?(term|sem)", re.I), "midsem"), (re.compile(r"end[\s-]?(term|sem)", re.I), "endsem"),
        (re.compile(r"\b(re-?appear|supplementary|backlog)\b", re.I), "supplementary"),
        (re.compile(r"\bpractical|lab exam\b", re.I), "practical")]


def norm_code(prefix: str, num: str) -> str:
    return f"{prefix}{num}".upper()


def find_codes(text: str) -> list[str]:
    return list(dict.fromkeys(norm_code(a, b) for a, b in CODE.findall(text.upper())))


def _year(y: str | None, mo: int, d: int, today: date) -> int | None:
    if y:
        y_i = int(y)
        return y_i + 2000 if y_i < 100 else y_i
    # No year: pick the one that puts the date nearest the future (Dec sheets list Jan exams).
    for cand in (today.year, today.year + 1):
        try:
            if date(cand, mo, d) >= today - timedelta(days=30):
                return cand
        except ValueError:
            return None
    return today.year + 1


def parse_date(text: str, today: date) -> date | None:
    for rx, order in ((D_ISO, "ymd"), (D_NUM, "dmy"), (D_DMY, "dMy"), (D_MDY, "Mdy")):
        for m in rx.finditer(text):
            try:
                if order == "ymd":
                    y, mo, d = int(m[1]), int(m[2]), int(m[3])
                elif order == "dmy":
                    d, mo = int(m[1]), int(m[3])
                    y = _year(m[4], mo, d, today)
                elif order == "dMy":
                    d, mo = int(m[1]), MONTHS[m[2][:3].lower()]
                    y = _year(m[3], mo, d, today)
                else:
                    mo, d = MONTHS[m[1][:3].lower()], int(m[2])
                    y = _year(m[3], mo, d, today)
                if not y:
                    continue
                out = date(y, mo, d)
            except (ValueError, KeyError):
                continue
            if today - timedelta(days=60) <= out <= today + timedelta(days=240):
                return out
    return None


def _hm(h: str, m: str | None, ap: str | None, ref_ap: str | None = None) -> tuple[int, int] | None:
    hh, mm = int(h), int(m or 0)
    ap = (ap or ref_ap or "").lower()
    if ap == "p" and hh < 12:
        hh += 12
    elif ap == "a" and hh == 12:
        hh = 0
    elif not ap and 1 <= hh <= 7:  # "2:00-5:00" on an exam sheet is afternoon
        hh += 12
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def parse_time(text: str) -> tuple[tuple[int, int], tuple[int, int] | None, bool] | None:
    """-> (start, end or None, time_known)."""
    t = D_NUM.sub(" ", text)  # don't read dates as times
    for m in T_RANGE.finditer(t):
        h1, m1, ap1, h2, m2, ap2 = m.groups()
        if not (m1 or ap1 or m2 or ap2):  # "1-2" with no minutes or am/pm: probably not a time
            continue
        # "10-11:30 AM": the end's am/pm applies to the start when the start isn't later than the end.
        start = _hm(h1, m1, ap1, ref_ap=ap2 if not ap1 and ap2 and int(h1) <= int(h2) else None)
        end = _hm(h2, m2, ap2, ref_ap=ap1)
        if start and end:
            if end <= start and not ap2 and end[0] + 12 < 24:
                end = (end[0] + 12, end[1])
            return start, end, True
    m = T_ONE.search(t)
    if m:
        st = _hm(m[1], m[2], m[3])
        if st:
            return st, None, True
    for rx, hm in SESSIONS:
        if rx.search(text):
            return hm, None, False
    return None


def _title_for(code: str, names: dict[str, str], cells: list[str]) -> str:
    if names.get(code):
        return names[code]
    for c in cells:  # the longest non-code, non-date cell on the row is usually the course name
        c2 = CODE.sub("", c).strip(" -:|")
        if len(c2) > 6 and not parse_date(c2, date.today()) and not re.fullmatch(r"[\d:.\sAPMapm\-–to]+", c2):
            return c2[:80]
    return code


def parse_rows(rows: list[list[str]], codes: dict[str, str], today: date | None = None,
               doc_text: str = "") -> list[dict[str, Any]]:
    """rows: table rows (list of cells) or single-cell text lines. codes: {code: course name} of
    your courses; empty = accept any course code found."""
    today = today or datetime.now(TZ).date()
    kind = next((k for rx, k in KIND if rx.search(doc_text)), "exam")
    head_time = parse_time(doc_text[:600]) if doc_text else None  # "Time: 10:00 AM – 1:00 PM" in a header
    last_date: date | None = None
    last_time = head_time
    out: dict[tuple[str, date], dict] = {}
    for cells in rows:
        cells = [re.sub(r"\s+", " ", c or "").strip() for c in cells]
        line = " | ".join(c for c in cells if c)
        if not line:
            continue
        d = parse_date(line, today)
        t = parse_time(line)
        if d:
            last_date = d
        if t:
            last_time = t
        found = [c for c in find_codes(line) if not codes or c in codes]
        if not found:
            continue
        d = d or last_date
        if not d:
            continue
        tm = t or last_time or ((9, 0), None, False)
        (sh, sm), end, known = tm
        start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=TZ)
        end_dt = datetime(d.year, d.month, d.day, end[0], end[1], tzinfo=TZ) if end else start + timedelta(hours=3)
        venue = next((m.group(0) for m in re.finditer(r"\b(room|hall|block|lab|LHC|N-?\d|A-?\d)[\w\s-]{0,12}", line, re.I)), None)
        for code in found:
            out[(code, d)] = {"course_code": code, "title": _title_for(code, codes, cells), "date": d.isoformat(),
                              "starts_at": int(start.timestamp()), "ends_at": int(end_dt.timestamp()),
                              "time_known": bool(known), "venue": venue, "kind": kind}
    return sorted(out.values(), key=lambda r: r["starts_at"])


def parse_text(text: str, codes: dict[str, str], today: date | None = None) -> list[dict[str, Any]]:
    return parse_rows([[ln] for ln in text.splitlines()], codes, today, doc_text=text)


def pdf_rows(data: bytes, max_pages: int = 30) -> tuple[list[list[str]], str]:
    import pdfplumber  # imported lazily: only the bot's venv has it
    rows: list[list[str]] = []
    texts: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:max_pages]:
            text = page.extract_text() or ""
            texts.append(text)
            tables = page.extract_tables() or []
            if tables:
                for tb in tables:
                    rows.extend([[c or "" for c in r] for r in tb if r])
            else:
                rows.extend([[ln] for ln in text.splitlines()])
    return rows, "\n".join(texts)


def parse_pdf(data: bytes, codes: dict[str, str], today: date | None = None) -> list[dict[str, Any]]:
    rows, text = pdf_rows(data)
    found = parse_rows(rows, codes, today, doc_text=text)
    if not found and text:  # tables came out mangled - try the raw text
        found = parse_text(text, codes, today)
    return found
