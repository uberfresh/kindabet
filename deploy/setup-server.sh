#!/usr/bin/env bash
# One-shot Hetzner server bootstrap for Kinda Bet.
# Run as root on a fresh Ubuntu 24.04 instance:
#   curl -fsSL https://raw.githubusercontent.com/<you>/<repo>/main/deploy/setup-server.sh | bash
# or after rsync:
#   sudo bash /opt/kindabet/deploy/setup-server.sh
#
# Idempotent: safe to re-run.

set -euo pipefail

APP_USER="kindabet"
APP_DIR="/opt/kindabet"
DOMAIN="${KINDABET_DOMAIN:-}"   # optional: export KINDABET_DOMAIN=yourdomain.tr beforehand

if [[ "$EUID" -ne 0 ]]; then
  echo "Run as root."
  exit 1
fi

echo "==> System packages"
apt-get update -y
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    rsync git curl ca-certificates \
    debian-keyring debian-archive-keyring apt-transport-https

echo "==> Google Chrome (headless, for TOTO scraper)"
if ! command -v google-chrome >/dev/null; then
  curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/trusted.gpg.d/google-chrome.gpg
  echo 'deb [arch=amd64] https://dl.google.com/linux/chrome/deb/ stable main' > /etc/apt/sources.list.d/google-chrome.list
  apt-get update -y
  apt-get install -y google-chrome-stable
fi

echo "==> Caddy (reverse proxy + auto SSL)"
if ! command -v caddy >/dev/null; then
  curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

echo "==> App user $APP_USER"
if ! id "$APP_USER" >/dev/null 2>&1; then
  adduser --system --group --home "$APP_DIR" --no-create-home --shell /bin/bash "$APP_USER"
fi

echo "==> $APP_DIR layout"
mkdir -p "$APP_DIR" "$APP_DIR/data"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Python venv"
if [[ ! -d "$APP_DIR/.venv" ]]; then
  sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
fi
if [[ -f "$APP_DIR/requirements.txt" ]]; then
  sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
fi

echo "==> Swap (Chrome can spike — give it 2GB)"
if ! swapon --show | grep -q '/swapfile'; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> systemd unit"
if [[ -f "$APP_DIR/deploy/kindabet.service" ]]; then
  cp "$APP_DIR/deploy/kindabet.service" /etc/systemd/system/kindabet.service
  systemctl daemon-reload
  systemctl enable kindabet
fi

echo "==> systemd timer (hourly auto-refresh)"
if [[ -f "$APP_DIR/deploy/kindabet-refresh.service" ]]; then
  cp "$APP_DIR/deploy/kindabet-refresh.service" /etc/systemd/system/kindabet-refresh.service
  cp "$APP_DIR/deploy/kindabet-refresh.timer"   /etc/systemd/system/kindabet-refresh.timer
  systemctl daemon-reload
  systemctl enable --now kindabet-refresh.timer
fi

echo "==> Passwordless sudo for service restart (so the deploy user can hit it)"
cat > /etc/sudoers.d/kindabet-restart <<EOF
$APP_USER ALL=(root) NOPASSWD: /bin/systemctl restart kindabet, /bin/systemctl status kindabet
EOF
chmod 440 /etc/sudoers.d/kindabet-restart

echo "==> Caddy config"
if [[ -f "$APP_DIR/deploy/Caddyfile" ]]; then
  if [[ -n "$DOMAIN" ]]; then
    sed "s/kindabet.example.com/$DOMAIN/" "$APP_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile
  else
    cp "$APP_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
    echo "  -> Edit /etc/caddy/Caddyfile and replace 'kindabet.example.com' with your domain."
  fi
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
fi

echo "==> UFW firewall (80/443/22 only)"
if command -v ufw >/dev/null; then
  ufw --force reset >/dev/null
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow 22/tcp
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw --force enable
fi

echo
echo "==> DONE. Next:"
echo "  1. systemctl start kindabet"
echo "  2. systemctl status kindabet"
echo "  3. Visit https://your-domain (Caddy will provision SSL on first request)"
