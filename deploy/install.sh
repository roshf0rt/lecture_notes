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
apt-get install -y -qq python3 python3-venv ffmpeg libpango-1.0-0 libpangoft2-1.0-0 fonts-inter fonts-noto-color-emoji
# Optional: silences a WeasyPrint warning. Not every release ships this package.
apt-get install -y -qq libharfbuzz-subset0 || echo "note: libharfbuzz-subset0 is not available, continuing"

id -u notes-bot >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin notes-bot
install -d -o notes-bot -g notes-bot -m 700 "$DATA_DIR"

python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
chmod -R a+rX "$APP_DIR"

install -m 644 "$APP_DIR/deploy/notes-bot.service" /etc/systemd/system/notes-bot.service

# Files over 20 MB: a local Bot API server, if bootstrap.sh was given an api_id and api_hash.
API_ENV=/etc/notes-bot/api.env
LOGGED_OUT_MARK="$DATA_DIR/.logged-out-of-cloud-api"
if [ -f "$API_ENV" ]; then
  echo "Setting up the local Bot API server (files up to 2 GB)"
  apt-get install -y -qq docker.io curl
  systemctl enable --now docker
  install -d -o 101 -g 101 -m 755 /var/lib/telegram-bot-api
  docker pull -q aiogram/telegram-bot-api:latest
  install -m 644 "$APP_DIR/deploy/notes-bot-api.service" /etc/systemd/system/notes-bot-api.service
  install -d /etc/systemd/system/notes-bot.service.d
  install -m 644 "$APP_DIR/deploy/local-api.conf" /etc/systemd/system/notes-bot.service.d/local-api.conf
  install -m 644 "$APP_DIR/deploy/notes-bot-cleanup.service" /etc/systemd/system/notes-bot-cleanup.service
  install -m 644 "$APP_DIR/deploy/notes-bot-cleanup.timer" /etc/systemd/system/notes-bot-cleanup.timer
  chmod +x "$APP_DIR/deploy/cleanup-media.sh"
  grep -q '^TELEGRAM_API_URL=' "$ENV_FILE" || echo "TELEGRAM_API_URL=http://127.0.0.1:8081" >> "$ENV_FILE"
  systemctl daemon-reload
  systemctl enable --quiet --now notes-bot-api
  for _ in $(seq 1 40); do
    curl -s -o /dev/null http://127.0.0.1:8081/ && break
    sleep 1
  done
  if [ ! -f "$LOGGED_OUT_MARK" ]; then
    # A bot works with one Bot API server at a time: leave Telegram's cloud one once.
    systemctl stop notes-bot || true
    TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
    curl -s "https://api.telegram.org/bot${TOKEN}/logOut" | head -c 200 || true
    echo
    unset TOKEN
    touch "$LOGGED_OUT_MARK"
  fi
  systemctl enable --quiet --now notes-bot-cleanup.timer
fi
systemctl daemon-reload
systemctl enable --quiet notes-bot
# restart, not start: on an update the old code is still running
systemctl restart notes-bot
sleep 3
systemctl --no-pager status notes-bot | head -15
