#!/usr/bin/env bash
# One-time TopHat server bootstrap for a fresh Ubuntu VM (GCP e2-micro, Oracle, etc.).
# Installs Python + deps, the systemd service, and Caddy (automatic HTTPS), then starts.
# Safe to re-run (idempotent).
#
# Usage (on the VM, as your normal sudo user):
#   sudo DOMAIN=tophat.example.com REPO=https://github.com/you/ProjectTopHat.git \
#        bash /opt/tophat/deploy/setup.sh
#
# DOMAIN must already resolve (DNS A record) to this VM's external IP and ports
# 80/443 must be open in the cloud firewall, or Caddy can't obtain a TLS cert.
set -euo pipefail

APP_DIR=/opt/tophat
RUN_USER="${SUDO_USER:-$(whoami)}"
DOMAIN="${DOMAIN:?set DOMAIN=your.domain (must point at this VM)}"
REPO="${REPO:-}"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

echo ">> [1/7] system packages"
apt-get update -y
apt-get install -y python3-venv python3-pip git curl debian-keyring debian-archive-keyring apt-transport-https

echo ">> [2/7] caddy (HTTPS reverse proxy)"
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  apt-get update -y
  apt-get install -y caddy
fi

echo ">> [3/7] repo at $APP_DIR (owner: $RUN_USER)"
if [ ! -d "$APP_DIR/.git" ]; then
  [ -n "$REPO" ] || { echo "first run needs REPO=<git url>"; exit 1; }
  git clone "$REPO" "$APP_DIR"
fi
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

echo ">> [4/7] python venv + dependencies"
sudo -u "$RUN_USER" bash -lc \
  "cd '$APP_DIR' && python3 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt"

echo ">> [5/7] data dir + .env"
sudo -u "$RUN_USER" mkdir -p "$APP_DIR/data"
if [ ! -f "$APP_DIR/.env" ]; then
  sudo -u "$RUN_USER" cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  cat >> "$APP_DIR/.env" <<'EOF'

# --- server (added by setup.sh) ---
TOPHAT_BROKER=live
TOPHAT_HOST=127.0.0.1
TOPHAT_PORT=8800
TOPHAT_HTTPS=1
# Login is created on first start if no users exist — set a strong password:
TOPHAT_ADMIN_EMAIL=change-me@example.com
TOPHAT_ADMIN_PASSWORD=change-this-now
EOF
  chown "$RUN_USER":"$RUN_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "   !! EDIT $APP_DIR/.env (PROJECTX creds + admin login), then: sudo systemctl restart tophat"
fi

echo ">> [6/7] systemd service 'tophat'"
cat > /etc/systemd/system/tophat.service <<EOF
[Unit]
Description=TopHat NQ dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python -m tophat.server
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable tophat
systemctl restart tophat

echo ">> [7/7] caddy config for $DOMAIN"
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
    reverse_proxy 127.0.0.1:8800
}
EOF
systemctl reload caddy || systemctl restart caddy

# Let the deploy user restart the service without a password (GitHub Actions CD).
# GCP default users already have passwordless sudo; this makes it explicit/portable.
echo "$RUN_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart tophat" \
  > /etc/sudoers.d/tophat-deploy
chmod 440 /etc/sudoers.d/tophat-deploy

echo
echo ">> done -> https://$DOMAIN"
echo "   logs:   journalctl -u tophat -f"
echo "   status: systemctl status tophat"
