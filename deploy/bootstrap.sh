#!/usr/bin/env bash
# One-shot setup for a Debian or Ubuntu server. Run as root:
#   bash <(curl -fsSL https://raw.githubusercontent.com/roshf0rt/lecture_notes/notes-bot/deploy/bootstrap.sh)
# It clones the branch into /opt/notes-bot, asks for the Telegram bot token
# (typed or pasted, not echoed) and your Telegram ID (you become the bot's admin),
# writes /etc/notes-bot/bot.env and starts the service.
# Re-running it updates the code, keeps the env file and restarts the bot.
set -euo pipefail

REPO_URL=https://github.com/roshf0rt/lecture_notes.git
BRANCH=notes-bot
APP_DIR=/opt/notes-bot
ENV_DIR=/etc/notes-bot
ENV_FILE=$ENV_DIR/bot.env

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git ca-certificates

if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  git -C "$APP_DIR" reset --quiet --hard "origin/$BRANCH"
else
  rm -rf "$APP_DIR"
  git clone --quiet --branch "$BRANCH" --depth 1 "$REPO_URL" "$APP_DIR"
fi

install -d -m 700 "$ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
  read -rsp "Telegram bot token (input is hidden): " TOKEN < /dev/tty
  echo
  # A pasted token often brings a tab or a newline with it, and Telegram rejects that.
  TOKEN=$(printf '%s' "$TOKEN" | tr -d '[:space:]')
  if [ -z "$TOKEN" ]; then
    echo "The token is empty, nothing was saved." >&2
    exit 1
  fi
  umask 077
  printf '%s\n' "TELEGRAM_BOT_TOKEN=$TOKEN" "GEMINI_MODEL=gemini-3.5-flash-lite" \
    "DATA_DIR=/var/lib/notes-bot" "ALLOWED_USER_IDS=" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  umask 022
  unset TOKEN
  echo "Saved $ENV_FILE"
else
  echo "Keeping the existing $ENV_FILE"
fi

if ! grep -q '^ADMIN_IDS=[0-9]' "$ENV_FILE"; then
  echo "Your Telegram ID makes you the bot's admin (/admin). @userinfobot shows it; the bot shows it in /admin too."
  read -rp "Your Telegram ID (Enter to skip): " ADMIN < /dev/tty
  ADMIN=$(printf '%s' "$ADMIN" | tr -cd '0-9,')
  if [ -n "$ADMIN" ]; then
    sed -i '/^ADMIN_IDS=/d' "$ENV_FILE"
    echo "ADMIN_IDS=$ADMIN" >> "$ENV_FILE"
    echo "Admin set: $ADMIN"
  fi
  unset ADMIN
fi

bash "$APP_DIR/deploy/install.sh"
