#!/usr/bin/env bash
# Installs / updates the Study Buddy Telegram bot next to the moodle-mcp server.
#   - its own Python venv (.venv-bot) so bot packages can never break the MCP server
#   - its own systemd service "study-bot" (auto-start, auto-restart, 250 MB memory cap)
#   - talks to the MCP server over 127.0.0.1 only; no new ports, no Caddy changes
# Safe to re-run: keeps your secrets, updates code, restarts only the bot.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE=study-bot
ENV_FILE="$APP_DIR/.env"
USER_NAME="$(id -un)"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*"; exit 1; }

[[ -f "$APP_DIR/telegram_bot.py" ]] || die "telegram_bot.py not found next to deploy_bot.sh"
[[ -f "$ENV_FILE" ]] && grep -q '^MCP_SECRET=' "$ENV_FILE" \
  || die "The MCP server isn't set up here yet. Run deploy.sh first."
systemctl is-active --quiet moodle-mcp || echo "[!] moodle-mcp isn't running right now - the bot will report that until it is."

sed -i 's/\r$//' "$APP_DIR/set-bot-secrets" "$APP_DIR/requirements-bot.txt" 2>/dev/null || true
chmod 700 "$APP_DIR/set-bot-secrets"

say "Setting up the bot's Python environment"
[[ -x "$APP_DIR/.venv-bot/bin/python" ]] || python3 -m venv "$APP_DIR/.venv-bot"
"$APP_DIR/.venv-bot/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv-bot/bin/pip" install -q -r "$APP_DIR/requirements-bot.txt"
"$APP_DIR/.venv-bot/bin/python" -c "import ast,sys; ast.parse(open('$APP_DIR/telegram_bot.py').read())" \
  || die "telegram_bot.py has a syntax error."

if ! grep -q '^TELEGRAM_BOT_TOKEN=.' "$ENV_FILE" || ! grep -q '^OWNER_CHAT_ID=.' "$ENV_FILE"; then
  say "First-time bot setup"
  sudo systemctl stop $SERVICE 2>/dev/null || true
  (cd "$APP_DIR" && .venv-bot/bin/python bot_setup.py) || die "Setup didn't finish. Re-run deploy_bot.sh."
fi

say "Installing systemd service '$SERVICE'"
sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<UNIT
[Unit]
Description=Study Buddy Telegram bot (client of moodle-mcp)
After=network-online.target moodle-mcp.service
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv-bot/bin/python $APP_DIR/telegram_bot.py
Restart=always
RestartSec=10
TimeoutStopSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
MemoryMax=250M
CPUQuota=50%

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE >/dev/null 2>&1
sudo systemctl restart $SERVICE
sleep 6
if ! systemctl is-active --quiet $SERVICE; then
  sudo journalctl -u $SERVICE -n 30 --no-pager
  die "Bot failed to start (log above)."
fi

BOT_USER="$(cd "$APP_DIR" && .venv-bot/bin/python - <<'PY'
import httpx, pathlib
env = dict(l.split("=", 1) for l in pathlib.Path(".env").read_text().splitlines() if "=" in l)
try:
    print(httpx.get(f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN'].strip()}/getMe", timeout=20).json()["result"]["username"])
except Exception:
    print("your bot")
PY
)"

printf '\n\033[1;32m✅ Study Buddy bot is running.\033[0m\n'
cat <<EOF

 Open Telegram → @$BOT_USER → send /today
 Morning digest 07:45 IST · deadline pings 24h + 3h before · new LMS posts every 30 min

 Manage:   sudo systemctl status $SERVICE    |   sudo journalctl -u $SERVICE -f
 Secrets:  ~/moodle-mcp/set-bot-secrets
 The MCP server (moodle-mcp) is separate and was not restarted.
EOF
