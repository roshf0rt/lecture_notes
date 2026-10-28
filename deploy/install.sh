#!/usr/bin/env bash
# Run as root on the server, after the project is unpacked into /opt/notes-bot.
# Needs /etc/notes-bot/bot.env with at least TELEGRAM_BOT_TOKEN and DATA_DIR=/var/lib/notes-bot.
set -euo pipefail

APP_DIR=/opt/notes-bot
DATA_DIR=/var/lib/notes-bot
ENV_FILE=/etc/notes-bot/bot.env

if [ ! -f "$ENV_FILE" ]; then
  echo "Create $ENV_FILE first (see .env.example in the project)." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv libpango-1.0-0 libpangoft2-1.0-0 fonts-inter fonts-noto-color-emoji
# Optional: silences a WeasyPrint warning. Not every release ships this package.
apt-get install -y -qq libharfbuzz-subset0 || echo "note: libharfbuzz-subset0 is not available, continuing"

id -u notes-bot >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin notes-bot
install -d -o notes-bot -g notes-bot -m 700 "$DATA_DIR"

python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

install -m 644 "$APP_DIR/deploy/notes-bot.service" /etc/systemd/system/notes-bot.service
systemctl daemon-reload
systemctl enable --now notes-bot
systemctl --no-pager status notes-bot | head -15
