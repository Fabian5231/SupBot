#!/usr/bin/env bash
# Installiert SupBot als systemd-Service unter /opt/supbot.
# Aufruf auf dem VPS als root:  sudo bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/supbot
SERVICE_USER=supbot
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Systempakete"
if command -v apt-get >/dev/null; then
  apt-get update -qq
  apt-get install -y python3 python3-venv python3-pip rsync git
fi

echo "==> Benutzer $SERVICE_USER"
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"

echo "==> Dateien nach $APP_DIR"
mkdir -p "$APP_DIR/data"
rsync -a --delete \
  --exclude '.venv' --exclude 'data' --exclude '.env' \
  --exclude '__pycache__' --exclude '.git' \
  "$SRC_DIR"/ "$APP_DIR"/

echo "==> Virtualenv"
if [ ! -d "$APP_DIR/.venv" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Konfiguration"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  SECRET=$("$APP_DIR/.venv/bin/python" -c "import secrets;print(secrets.token_hex(32))")
  sed -i "s|^SUPBOT_SECRET=.*|SUPBOT_SECRET=$SECRET|" "$APP_DIR/.env"
  echo "    -> $APP_DIR/.env angelegt. Token und Passwort dort eintragen!"
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> systemd"
install -m 644 "$APP_DIR/deploy/supbot.service" /etc/systemd/system/supbot.service
systemctl daemon-reload
systemctl enable supbot
systemctl restart supbot

echo
echo "Fertig. Status:  systemctl status supbot"
echo "Logs:            journalctl -u supbot -f"
echo "Konfiguration:   nano $APP_DIR/.env  &&  systemctl restart supbot"
