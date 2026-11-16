#!/usr/bin/env bash
# Study Buddy: dashboard + collector + bot update, on the 1 GB Oracle VM. Safe to re-run.
#
#   1. swap: 2 GB swapfile, vm.swappiness=10 (before anything else — 1 GB is tight)
#   2. dashboard runs INSIDE moodle-mcp (no second Python server): moodle_mcp.py routes /dash/... to it.
#      Caddy needs nothing new — it already proxies every path to moodle-mcp.
#   3. memory caps: moodle-mcp 300M (MCP + dashboard), study-bot 250M, Caddy uncapped
#   4. study-collect.timer (oneshot every 30 min) snapshots MCP tools into study.db
#   5. study-bot: pdfplumber for date sheets; on restart it merges bot.db into study.db (bot.db.bak kept a week)
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$APP_DIR/.env"
USER_NAME="$(id -un)"
PORT=8765
SITE=/etc/caddy/sites/moodle-mcp.caddy

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*"; exit 1; }

[[ -f "$APP_DIR/dashboard/app.py" ]] || die "dashboard/ not found next to deploy_dash.sh"
[[ -f "$APP_DIR/dashboard/web/dist/index.html" ]] || die "dashboard/web/dist is missing — the frontend wasn't uploaded."
[[ -x "$APP_DIR/.venv/bin/python" && -f "$ENV_FILE" ]] && grep -q '^MCP_SECRET=' "$ENV_FILE" \
  || die "The MCP server isn't set up here yet. Run deploy.sh first."
[[ -f /etc/systemd/system/moodle-mcp.service ]] || die "moodle-mcp service missing — run deploy.sh first."
sed -i 's/\r$//' "$ENV_FILE" "$APP_DIR/requirements-bot.txt" 2>/dev/null || true

get_env() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }
set_env() {
  local k="$1" v="$2"
  grep -v -E "^$k=" "$ENV_FILE" > "$ENV_FILE.tmp" || true
  echo "$k=$v" >> "$ENV_FILE.tmp"; mv "$ENV_FILE.tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
}

# --------------------------------------------------------------------------- 1. swap
if ! swapon --show --noheadings | grep -q .; then
  say "Adding 2 GB swap (vm.swappiness=10)"
  if [[ ! -f /swapfile ]]; then
    sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
  fi
  sudo swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swap.conf >/dev/null
sudo sysctl -q --system

# --------------------------------------------------------------------------- 2. secrets
DOMAIN="$(sudo head -1 "$SITE" 2>/dev/null | awk '{print $1}')"
[[ -n "$DOMAIN" ]] || die "Couldn't read the HTTPS host from $SITE"
[[ -n "$(get_env DASH_SECRET)" ]]    || set_env DASH_SECRET "$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
[[ -n "$(get_env DASH_ICS_TOKEN)" ]] || set_env DASH_ICS_TOKEN "$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
set_env DASH_PUBLIC_URL "https://$DOMAIN"
grep -q '^TELEGRAM_BOT_TOKEN=.' "$ENV_FILE" && grep -q '^OWNER_CHAT_ID=.' "$ENV_FILE" \
  || warn "TELEGRAM_BOT_TOKEN / OWNER_CHAT_ID not set — dashboard login won't work until the bot is set up (deploy_bot.sh)."

say "Checking the dashboard imports in the MCP server's Python"
for f in common collector app; do
  "$APP_DIR/.venv/bin/python" -c "import ast; ast.parse(open('$APP_DIR/dashboard/$f.py').read())" || die "dashboard/$f.py has a syntax error."
done
(cd "$APP_DIR" && .venv/bin/python -c "import dashboard.app") || die "dashboard.app failed to import (see above)."

# --------------------------------------------------------------------------- 3. remove the old split setup (if v1 was deployed)
if [[ -f /etc/systemd/system/study-dash.service ]]; then
  say "Removing the separate study-dash service (dashboard now lives in moodle-mcp)"
  sudo systemctl disable --now study-dash >/dev/null 2>&1 || true
  sudo rm -f /etc/systemd/system/study-dash.service
fi
if [[ -f /etc/caddy/site-extra/dash.caddy ]]; then
  sudo rm -f /etc/caddy/site-extra/dash.caddy
  sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 && sudo systemctl reload caddy
fi
rm -rf "$APP_DIR/.venv-dash"

# --------------------------------------------------------------------------- 4. caps + restart moodle-mcp
say "Memory caps: moodle-mcp 300M, study-bot 250M"
sudo sed -i 's/^MemoryMax=.*/MemoryMax=300M/' /etc/systemd/system/moodle-mcp.service
[[ -f /etc/systemd/system/study-bot.service ]] && sudo sed -i 's/^MemoryMax=.*/MemoryMax=250M/' /etc/systemd/system/study-bot.service

say "Installing study-collect.timer (every 30 min)"
sudo tee /etc/systemd/system/study-collect.service >/dev/null <<UNIT
[Unit]
Description=Study Buddy collector (MCP tools -> study.db)
After=network-online.target moodle-mcp.service

[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m dashboard.collector
TimeoutStartSec=15min
Nice=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
MemoryMax=150M
UNIT
sudo tee /etc/systemd/system/study-collect.timer >/dev/null <<UNIT
[Unit]
Description=Run the Study Buddy collector every 30 minutes

[Timer]
OnBootSec=3min
OnCalendar=*:07/30
RandomizedDelaySec=60
Persistent=true

[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now study-collect.timer >/dev/null 2>&1

command -v pdftotext >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq poppler-utils >/dev/null || warn "poppler-utils not installed (PDF indexing falls back to pypdf)"

say "Restarting moodle-mcp (MCP + dashboard)"
sudo systemctl restart moodle-mcp
# wait up to 30s for it to answer (slower 1 GB VMs take ~6s)
for _ in $(seq 1 30); do curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done
systemctl is-active --quiet moodle-mcp || { sudo journalctl -u moodle-mcp -n 30 --no-pager; die "moodle-mcp failed to start."; }
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null || die "MCP server isn't answering."
curl -fsS "http://127.0.0.1:$PORT/dash/api/ping" >/dev/null \
  || { sudo journalctl -u moodle-mcp -n 20 --no-pager | grep -i dashboard; die "MCP is up but /dash isn't mounted (log above)."; }

# --------------------------------------------------------------------------- 5. bot
if [[ -f /etc/systemd/system/study-bot.service && -x "$APP_DIR/.venv-bot/bin/pip" ]]; then
  say "Updating the bot (date-sheet parser) — it merges bot.db into study.db on start"
  "$APP_DIR/.venv-bot/bin/pip" install -q -r "$APP_DIR/requirements-bot.txt"
  sudo systemctl restart study-bot
  sleep 6
  systemctl is-active --quiet study-bot || { sudo journalctl -u study-bot -n 30 --no-pager; die "study-bot failed to start."; }
  sudo journalctl -u study-bot -n 15 --no-pager -o cat | grep -i "migrated" || true
else
  warn "study-bot isn't installed — run deploy_bot.sh, then re-run this."
fi

# --------------------------------------------------------------------------- first sync + report
say "First sync (~1 min)"
sudo systemctl start study-collect.service || warn "Collector reported errors — journalctl -u study-collect -n 40"
sudo journalctl -u study-collect -n 8 --no-pager -o cat || true

curl -fsS --max-time 8 "https://$DOMAIN/dash/api/ping" >/dev/null 2>&1 || warn "https://$DOMAIN/dash isn't reachable from outside yet — sudo systemctl status caddy"

printf '\n\033[1;32m✅ Study Buddy is updated.\033[0m\n'
cat <<EOF

 Dashboard:  https://$DOMAIN/dash/   (sign in: "Send login link to Telegram")
 Exams:      forward the CoE date sheet PDF/text to the bot → tap Save
 Widget:     optional — @BotFather → /setdomain → $DOMAIN

 Memory:
$(free -m | sed 's/^/   /')

 Per service (MB):
$(systemd-cgtop -m -b -n 1 --raw 2>/dev/null | awk '/moodle-mcp|study-bot|caddy/ {printf "   %-28s %6.0f\n", $1, $4/1048576}')

 Logs:  journalctl -u moodle-mcp -f  ·  journalctl -u study-bot -f  ·  journalctl -u study-collect -n 50
 Watch: systemd-cgtop -m   ·   free -m
EOF
