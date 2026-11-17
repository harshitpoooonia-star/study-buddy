#!/usr/bin/env bash
# One-shot installer for moodle_mcp on an Ubuntu VPS.
#   - Python venv + deps
#   - secret URL key (MCP_SECRET) in .env
#   - systemd service "moodle-mcp" (auto-start, auto-restart)
#   - Caddy reverse proxy with automatic HTTPS on <your-ip>.sslip.io (no domain needed)
#   - opens ports 80/443 in ufw / iptables (Oracle Cloud images block them by default)
# Safe to re-run: keeps your token and secret, updates code, restarts.
#
# Optional:  DOMAIN=lms.example.com bash deploy.sh   (use your own domain instead of sslip.io)
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE=moodle-mcp
PORT=8765
ENV_FILE="$APP_DIR/.env"
USER_NAME="$(id -un)"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*"; exit 1; }

[[ "$(id -u)" -eq 0 ]] && warn "Running as root works, but a normal sudo user is preferred."
command -v sudo >/dev/null || die "sudo is required."
[[ -f "$APP_DIR/moodle_mcp.py" ]] || die "moodle_mcp.py not found next to deploy.sh"

# --------------------------------------------------------------------------- packages
say "Installing system packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip curl poppler-utils \
  debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null

if ! command -v caddy >/dev/null; then
  say "Installing Caddy (HTTPS reverse proxy)"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy >/dev/null
fi

# --------------------------------------------------------------------------- python env
say "Setting up Python environment"
[[ -x "$APP_DIR/.venv/bin/python" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# --------------------------------------------------------------------------- .env
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
# Merge an uploaded .env from the PC: only fill keys the server doesn't already have, so
# re-deploys never clobber MCP_SECRET (connector URL) or MYCAMU_COOKIE.
if [[ -f "$APP_DIR/.env.upload" ]]; then
  sed -i 's/\r$//' "$APP_DIR/.env.upload"
  while IFS= read -r line; do
    [[ "$line" =~ ^([A-Z_]+)= ]] || continue
    grep -q "^${BASH_REMATCH[1]}=" "$ENV_FILE" || echo "$line" >> "$ENV_FILE"
  done < "$APP_DIR/.env.upload"
  rm -f "$APP_DIR/.env.upload"
fi
sed -i 's/\r$//' "$ENV_FILE"   # .env copied from Windows has CRLF line endings
get_env() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }
set_env() {
  local k="$1" v="$2"
  grep -v -E "^$k=" "$ENV_FILE" > "$ENV_FILE.tmp" || true
  echo "$k=$v" >> "$ENV_FILE.tmp"; mv "$ENV_FILE.tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
}

[[ -n "$(get_env MOODLE_URL)" ]] || set_env MOODLE_URL "https://lms.bennett.edu.in"
if [[ -z "$(get_env MOODLE_TOKEN)" ]]; then
  say "LMS token needed"
  echo "Paste your Moodle token (from Documents\\moodle-mcp\\.env on your PC, or LMS -> Preferences -> Security keys)."
  read -r -s -p "Token (hidden): " TOK; echo
  [[ -n "$TOK" ]] || die "No token given."
  set_env MOODLE_TOKEN "$TOK"
fi
if [[ -z "$(get_env MCP_SECRET)" ]]; then
  set_env MCP_SECRET "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi
set_env MOODLE_DOWNLOAD_DIR "$APP_DIR/lms_downloads"

say "Checking the token against the LMS"
if ! (cd "$APP_DIR" && .venv/bin/python - <<'PY'
import asyncio, sys
import moodle_mcp as m
out = asyncio.run(m.moodle_whoami())
print(out)
sys.exit(0 if out.startswith("Connected") else 1)
PY
); then
  die "Token check failed. Fix MOODLE_TOKEN in $ENV_FILE and re-run."
fi

[[ -f "$APP_DIR/set-camu-cookie" ]] && chmod 700 "$APP_DIR/set-camu-cookie"

# --------------------------------------------------------------------------- systemd
say "Installing systemd service '$SERVICE'"
sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<UNIT
[Unit]
Description=Moodle LMS MCP server (Bennett)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/moodle_mcp.py --http --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=5
TimeoutStopSec=8
KillMode=mixed
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
MemoryMax=300M

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now $SERVICE >/dev/null
sudo systemctl restart $SERVICE
sleep 3
systemctl is-active --quiet $SERVICE || { sudo journalctl -u $SERVICE -n 30 --no-pager; die "Service failed to start."; }
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null || die "Service isn't answering on port $PORT."

# --------------------------------------------------------------------------- firewall
say "Opening ports 80 and 443"
if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow 80/tcp >/dev/null; sudo ufw allow 443/tcp >/dev/null
fi
# Oracle Cloud Ubuntu images ship an iptables REJECT rule; add ACCEPTs ahead of it.
if sudo iptables -S INPUT 2>/dev/null | grep -q -- "-j REJECT"; then
  for p in 80 443; do
    sudo iptables -C INPUT -p tcp --dport $p -m state --state NEW -j ACCEPT 2>/dev/null \
      || sudo iptables -I INPUT 5 -p tcp --dport $p -m state --state NEW -j ACCEPT
  done
  if command -v netfilter-persistent >/dev/null; then sudo netfilter-persistent save >/dev/null 2>&1 || true
  else sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iptables-persistent >/dev/null && sudo netfilter-persistent save >/dev/null 2>&1 || true; fi
fi

# --------------------------------------------------------------------------- caddy
if [[ -z "${DOMAIN:-}" ]]; then
  IP="$(curl -fsS4 https://api.ipify.org || curl -fsS4 https://ifconfig.me)"
  [[ -n "$IP" ]] || die "Couldn't detect this server's public IP. Re-run with DOMAIN=your.domain"
  DOMAIN="${IP//./-}.sslip.io"
fi
say "Configuring HTTPS for $DOMAIN"
sudo mkdir -p /etc/caddy/sites /etc/caddy/site-extra
# site-extra/*.caddy holds extra routes (e.g. the dashboard's /dash) so re-running this keeps them.
sudo tee /etc/caddy/sites/moodle-mcp.caddy >/dev/null <<CADDY
$DOMAIN {
	encode gzip
	import /etc/caddy/site-extra/*.caddy
	reverse_proxy 127.0.0.1:$PORT
	log {
		output discard
	}
}
CADDY
CF=/etc/caddy/Caddyfile
if ! sudo grep -q "import sites/\*.caddy" "$CF" 2>/dev/null; then
  sudo cp "$CF" "$CF.bak.$(date +%s)" 2>/dev/null || true
  # Stock Caddyfile only has the ':80' welcome page - replace it; otherwise append.
  if sudo grep -q "^:80" "$CF" 2>/dev/null && [[ "$(sudo grep -cE '^[^#[:space:]].*\{' "$CF")" -le 1 ]]; then
    echo "import sites/*.caddy" | sudo tee "$CF" >/dev/null
  else
    printf '\nimport sites/*.caddy\n' | sudo tee -a "$CF" >/dev/null
  fi
fi
sudo caddy validate --config "$CF" --adapter caddyfile >/dev/null || die "Caddy config invalid (see $CF)."
sudo systemctl enable --now caddy >/dev/null
sudo systemctl reload caddy || sudo systemctl restart caddy

say "Waiting for the HTTPS certificate (up to ~60s)"
OK=0
for _ in $(seq 1 20); do
  if curl -fsS --max-time 5 "https://$DOMAIN/healthz" >/dev/null 2>&1; then OK=1; break; fi
  sleep 3
done

SECRET="$(get_env MCP_SECRET)"
URL="https://$DOMAIN/$SECRET/mcp"
echo "$URL" > "$APP_DIR/CONNECTOR_URL.txt"; chmod 600 "$APP_DIR/CONNECTOR_URL.txt"

echo
if [[ $OK -eq 1 ]]; then
  printf '\033[1;32m✅ Live at https://%s\033[0m\n' "$DOMAIN"
else
  warn "HTTPS isn't reachable yet. Most likely ports 80/443 are blocked by your cloud provider's firewall."
  warn "Oracle Cloud: Networking -> VCN -> Security List -> add Ingress rules for TCP 80 and 443 from 0.0.0.0/0,"
  warn "then run:  sudo systemctl restart caddy   (or just re-run this script)."
fi
cat <<EOF

────────────────────────────────────────────────────────────
 Your connector URL (KEEP SECRET - it is the password):

   $URL

 Add it as a custom connector in your MCP client
 (Name: Bennett LMS, URL: the line above). Saved in $APP_DIR/CONNECTOR_URL.txt
────────────────────────────────────────────────────────────
 Manage:  sudo systemctl status $SERVICE   |   sudo journalctl -u $SERVICE -f
 New secret URL (if it leaks):  sed -i '/^MCP_SECRET=/d' $ENV_FILE && bash $APP_DIR/deploy.sh
EOF
