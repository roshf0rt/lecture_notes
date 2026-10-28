#!/usr/bin/env bash
# One-shot setup for a Debian or Ubuntu server. Run as root:
#   bash <(curl -fsSL https://raw.githubusercontent.com/roshf0rt/lecture_notes/notes-bot/deploy/bootstrap.sh)
# It clones the branch into /opt/notes-bot, asks for the Telegram bot token
# (typed or pasted, not echoed), writes /etc/notes-bot/bot.env and starts the service.
# Re-running it updates the code and keeps the existing env file.
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
  if [ -z "$TOKEN" ]; then
    echo "The token is empty, nothing was saved." >&2
    exit 1
  fi
  read -rp "Your Telegram user ID, so only you can use the bot (empty = everyone): " ALLOWED < /dev/tty
  umask 077
  cat > "$ENV_FILE" <<EOF
TELEGRAM_BOT_TOKEN=$TOKEN
GEMINI_MODEL=gemini-3.5-flash-lite
DATA_DIR=/var/lib/notes-bot
ALLOWED_USER_IDS=$ALLOWED
EOF
  chmod 600 "$ENV_FILE"
  umask 022
  unset TOKEN ALLOWED
  echo "Saved $ENV_FILE"
else
  echo "Keeping the existing $ENV_FILE"
fi

bash "$APP_DIR/deploy/install.sh"
