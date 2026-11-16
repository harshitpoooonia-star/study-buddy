<p align="center"><img src="assets/banner.png" alt="Study Buddy" width="100%"></p>

# Study Buddy

Your Bennett LMS and ERP, read through MCP: an MCP server (`moodle_mcp.py`), a Telegram bot and a phone-first dashboard on top of it. Everything is read-only.

## The MCP server

An MCP server that gives any MCP client access to your Moodle LMS (`lms.bennett.edu.in`) through Moodle's
official web-service API, the same one the Moodle mobile app uses. It logs in with a
personal **token**, not your password.

## Tools

| Tool | What it does |
|---|---|
| `moodle_semester_overview` | One-shot dashboard: courses, everything due in N days with live submission status, overdue items |
| `moodle_get_upcoming_deadlines` | LMS timeline: assignments, quizzes, forums with due dates; flags "action required" and overdue |
| `moodle_list_courses` | Enrolled courses with IDs and progress |
| `moodle_get_course_contents` | Sections, activities, and every file (slides, PDFs) with URLs |
| `moodle_list_assignments` | Due and cut-off dates, max grade, instructions, brief attachments |
| `moodle_get_assignment_status` | Submitted or not, extension, grade, teacher feedback |
| `moodle_list_quizzes` | Open/close times, time limit, attempts, best grade |
| `moodle_get_grades` | Per-item grades and feedback, or course totals |
| `moodle_get_announcements` | Course Announcements and forums (where faculty post test and deadline notices) |
| `moodle_get_notifications` | New grades, feedback, reminders |
| `moodle_read_file` | Downloads a course file and returns its text (PDF, DOCX, PPTX, code, txt) |
| `moodle_whoami` | Connection check |

All tools are **read-only**. Nothing is submitted, posted, or marked as read.

## Setup (≈5 min)

```bash
git clone https://github.com/harshitpoooonia-star/study-buddy.git
cd study-buddy
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 1. Get your token

**Option A, password login:**
```bash
python get_token.py
```
You type your password into your own terminal. It goes only to the LMS over HTTPS. The script
saves `MOODLE_URL` and `MOODLE_TOKEN` to a local `.env`, which the server loads automatically.

**Option B, if you sign in with Microsoft (SSO) and have no LMS password:**
1. Install the **Moodle app** on your phone, enter `https://lms.bennett.edu.in` and sign in once.
   This creates your mobile web-service key.
2. In a browser, open the LMS, then **Profile → Preferences → Security keys**.
3. Copy the key for **Moodle mobile web service**. Then create a `.env` file next to `moodle_mcp.py`:
   ```
   MOODLE_URL=https://lms.bennett.edu.in
   MOODLE_TOKEN=paste_key_here
   ```

If neither works (error `servicenotavailable` / `enablewsdescription`), web services are switched
off on the site. Ask Bennett IT to enable **Mobile web services**.

### 2. Test it
```bash
python -c "import asyncio, moodle_mcp as m; print(asyncio.run(m.moodle_whoami()))"
```

### 3. Connect an MCP client

**Desktop MCP client:** add this to the client's MCP config file:
```json
{
  "mcpServers": {
    "bennett-lms": {
      "command": "C:\\path\\to\\study-buddy\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\study-buddy\\moodle_mcp.py"]
    }
  }
}
```
(macOS/Linux: `/path/to/study-buddy/.venv/bin/python`.) Restart the client.

**Remote (VPS), so it works from any device:**
1. Copy the folder to the VPS as `~/moodle-mcp` and run `deploy.sh` there:
   ```bash
   scp -r . ubuntu@1.2.3.4:~/moodle-mcp
   ssh -t ubuntu@1.2.3.4 'bash ~/moodle-mcp/deploy.sh'
   ```
   It installs Python deps, a `moodle-mcp` systemd service, and Caddy with free HTTPS on
   `<your-ip>.sslip.io`. Copy your `.env` across too, or the script asks for the token.
2. Copy the printed **connector URL** (`https://<ip>.sslip.io/<secret>/mcp`) into your MCP client's
   custom connector settings. It then works on web, phone and desktop.
3. Oracle Cloud: also add Ingress rules for TCP 80 and 443 in the VCN Security List.

The secret in the URL **is the password**. Every other path returns 404. If it leaks, run on the VPS:
`sed -i '/^MCP_SECRET=/d' ~/moodle-mcp/.env && bash ~/moodle-mcp/deploy.sh` to get a new one.

## Example questions
- "What's due this week across all my courses, and what haven't I submitted?"
- "Read the CSET302 assignment 2 brief and explain what it wants."
- "Summarise this week's lecture slides for Automata."
- "Any new announcements about tests or vivas?"
- "What's my grade breakdown in Competitive Programming?"

## Security notes
- The token = read access to your LMS account. Keep `.env` private (`.gitignore` it). Revoke any
  time: **Preferences → Security keys → Reset**.
- `moodle_read_file` only fetches URLs on your own LMS host, so it can't be pointed elsewhere.
- Downloaded files go to `./lms_downloads` (change with `MOODLE_DOWNLOAD_DIR`).

## Scaling to other students (roadmap)
This version is single-user (one token in `.env`). For a multi-student assistant:
1. Get the university's OK (IT / SCSET). Ideally they issue an official web service.
2. Each student gets their **own** token through your app's login screen. Store only tokens
   (encrypted), never passwords.
3. Run the HTTP server with OAuth (the MCP SDK's `auth` settings) so each connection maps to that
   student's token.
4. Add rate limiting and caching (course contents rarely change) so you don't hammer the LMS.

Config env vars: `MOODLE_URL`, `MOODLE_TOKEN`, `MOODLE_TZ` (default `Asia/Kolkata`), `MOODLE_DOWNLOAD_DIR`.

## MyCamu (Bennett ERP)

Seven read-only tools: `mycamu_get_attendance`, `mycamu_get_timetable`, `mycamu_get_exam_schedule`,
`mycamu_get_results`, `mycamu_get_announcements`, `mycamu_get_notifications`, `mycamu_status`.

MyCamu uses Microsoft SSO, so the server uses your browser session cookie (`connect.sid`).
It lives only in the server's `.env` as `MYCAMU_COOKIE` (chmod 600). It is never logged and never returned by a tool.

**Set or rotate the cookie** (also do this when a tool says the session expired):
1. Log in at https://student.bennetterp.camu.in, then press F12 → Application → Cookies → copy the `connect.sid` value.
2. Run `ssh -t ubuntu@<your-vps-ip> ~/moodle-mcp/set-camu-cookie` and paste the value when asked (input is hidden).

The server verifies the cookie, then restarts. It pings `getvalidationstatus` once a day to keep the session alive,
saves the rolled cookie if MyCamu rotates it, caches responses for 10 minutes, and spaces calls at least 2 s apart.
Debug (prints response *shapes*, no values): `~/moodle-mcp/.venv/bin/python ~/moodle-mcp/mycamu.py probe`

## Telegram bot (Study Buddy)

A Telegram front-end on top of the MCP server above. It runs as a **separate** systemd service
(`study-bot`, own venv `.venv-bot`, 250 MB memory cap) and reads data only through
`http://127.0.0.1:8765/<MCP_SECRET>/mcp`. So a bot crash can't take the connector down. If the MCP
server goes down, the bot messages you once and again when it's back.

**Install:** message @BotFather on Telegram, send `/newbot`, then run
`ssh -t ubuntu@<your-vps-ip> 'bash ~/moodle-mcp/deploy_bot.sh'`. On the first run it asks for the bot
token (hidden), an optional Groq key, and asks you to press Start in your bot to link your account.
To change any of these later, run `~/moodle-mcp/set-bot-secrets` on the VPS.

| Command | What it shows |
|---|---|
| `/today` `/tomorrow` `/week` | MyCamu timetable (`/today` also lists what's due in 48 h) |
| `/deadlines` | LMS timeline items for the next 14 days, plus overdue ones |
| `/attendance` | Per-subject % with the 75% check |
| `/grades` `/exams` | Moodle course totals · MyCamu exam schedule |
| `/news` `/notices` | Latest LMS notifications · MyCamu announcements |
| `/status` `/alerts on/off` | Health check · pause or resume automatic messages |

**Plain-English questions** ("can I skip IoT tomorrow?") go to Groq's free tier (`openai/gpt-oss-120b`,
falling back to `openai/gpt-oss-20b`). Groq calls the same MCP tools. Without a Groq key, or when
the free limit is used up, a built-in keyword matcher answers instead. The limit per user per day is
set by `AI_DAILY_LIMIT` (default 40).

**Automatic messages:** a morning digest at 07:45 IST (classes, what's due in 7 days, subjects below
75%), deadline pings 24 h and 3 h before, new LMS notifications (checked every 30 min), new course
items (checked every 3 h), and a warning when the MyCamu cookie expires.

Optional `.env` keys: `GROQ_MODEL`, `GROQ_FALLBACK_MODEL`, `DIGEST_TIME` (HH:MM), `AI_DAILY_LIMIT`, `BOT_TZ`.
Manage it with `sudo systemctl status study-bot` and `sudo journalctl -u study-bot -f`.
State (alerts already sent, things already seen) is kept in `bot.db`.

Only the linked owner gets answers. Anyone else gets "private bot" plus their chat ID. Serving other
students needs per-user LMS tokens and MyCamu cookies in the MCP server first (see the roadmap above).

## Study Buddy dashboard

A phone-first web app at `https://<your-host>/dash/`:

- Today: current/next class with a countdown, what's due or examined in 48 h, attendance warnings, unread counts
- Deadlines: timeline/board views, course filter, started/done/snooze marks, your own entries and exams, ICS calendar feed
- Attendance: a gauge per subject with attend-next / can-skip, plus a **What if…** planner (tick classes you'll miss and see every subject's %)
- Timetable: week view, free slots, exams, an exam-week banner
- Grades: LMS marks, credits and the grade you expect per course, SGPA, and CGPA including past semesters
- Bot settings (⋯ menu): turn automatic messages on/off, digest time, reminder hours, muted courses

**How it's wired (one box, 1 GB RAM)**

| Piece | Runs as | Cap |
|---|---|---|
| MCP server **+ dashboard** (`/dash` is routed inside `moodle_mcp.py`; Starlette, no extra deps) | `moodle-mcp` | 300M |
| Telegram bot | `study-bot` | 250M |
| Collector: MCP tools → `study.db`, every 30 min | `study-collect.timer` (oneshot) | 150M |
| Caddy (HTTPS, proxies every path to moodle-mcp) | `caddy` | none |

`study.db` (SQLite, WAL, `busy_timeout=5000`) is the single source of truth. The bot owns the `bot_*` tables.
`settings`, `exams` and `manual_entries` are shared: the dashboard writes settings and the bot re-reads them on every
scheduler tick, with no HTTP between them. On its first start after the merge, the bot copies `bot.db` into
`study.db` and keeps `bot.db.bak` for a week. There is one file to back up:
`sqlite3 study.db ".backup study-backup.db"`.

**Deploy / update:** build the frontend (`cd dashboard/web && npm ci && npm run build`), or take
`dashboard-dist.zip` from the [latest release](https://github.com/harshitpoooonia-star/study-buddy/releases/latest)
and unzip it into `dashboard/web/dist`. Upload the folder as before and run `bash ~/moodle-mcp/deploy_dash.sh`
on the VPS. The script adds 2 GB swap (swappiness 10) if there's none, sets the caps, installs the timer, restarts
moodle-mcp and the bot, runs a first sync, and prints `free -m` plus per-service memory.

**Sign in:** tap *Send login link to Telegram*. Optional: in @BotFather run `/setdomain` with your host to enable the
Telegram Login button too. Only `OWNER_CHAT_ID` can sign in.

**Exam date sheets:** forward the CoE PDF, or the text, to the bot. It keeps only rows for your course codes (from the
timetable, attendance and LMS), shows them, and saves them on ✅. A revised sheet replaces the earlier one for those
courses. Reminders follow your reminder hours, the same as deadlines. Scanned-image PDFs have no text, so forward the
rows as text instead. Outlook stays out (university tenants block Graph access).

**Calendar:** Deadlines → Calendar feed → *Add to Google Calendar*. To revoke the URL, delete `DASH_ICS_TOKEN` from
`~/moodle-mcp/.env` and re-run `deploy_dash.sh`.

**Frontend dev (PC):** `cd dashboard\web && npm install && npm run dev`. It proxies `/dash/api` to 127.0.0.1:8770, so run
`python -m dashboard.app --serve` alongside it.

| | |
|---|---|
| Sync now | the sync chip in the app (max once per 2 min) or `sudo systemctl start study-collect` |
| Logs | `journalctl -u moodle-mcp -f` · `journalctl -u study-bot -f` · `journalctl -u study-collect -n 50` |
| Memory | `systemd-cgtop -m` (per service) · `free -m` (overall) |

## License

MIT. See [LICENSE](LICENSE).
