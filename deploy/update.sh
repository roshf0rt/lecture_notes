#!/usr/bin/env bash
# Pulls the newest notes-bot branch onto the server and restarts the bot, if there is something new.
# Runs as root from notes-bot-update.timer. Does nothing when the branch has not moved.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/notes-bot}
BRANCH=${BRANCH:-notes-bot}
DATA_DIR=${DATA_DIR:-/var/lib/notes-bot}
ENV_FILE=${ENV_FILE:-/etc/notes-bot/bot.env}
REQUEST="$DATA_DIR/update.request"

# An admin asked for this update from the bot: remember who, and tell them the outcome.
ASKED_BY=""
if [ -f "$REQUEST" ]; then
  ASKED_BY=$(cat "$REQUEST")
  rm -f "$REQUEST"
fi

notify() {
  [ -n "$ASKED_BY" ] || return 0
  local token api
  token=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2- || true)
  api=$(grep '^TELEGRAM_API_URL=' "$ENV_FILE" | cut -d= -f2- || true)
  curl -s -m 20 -X POST "${api:-https://api.telegram.org}/bot$token/sendMessage" \
    -d chat_id="$ASKED_BY" --data-urlencode text="$1" >/dev/null || true
}

cd "$APP_DIR"
git fetch --quiet origin "$BRANCH"
OLD=$(git rev-parse HEAD)
NEW=$(git rev-parse "origin/$BRANCH")
if [ "$OLD" = "$NEW" ]; then
  notify "Новой версии нет, бот уже на последнем коммите (${OLD:0:7})."
  exit 0
fi

# A note being made right now would be cut off by a restart. The bot writes how many are running into
# active.jobs (a stale file older than four hours is ignored); the timer tries again in ten minutes.
ACTIVE="$DATA_DIR/active.jobs"
busy() {
  if pgrep -x ffmpeg >/dev/null; then return 0; fi
  if [ -f "$ACTIVE" ] && [ "$(cat "$ACTIVE" 2>/dev/null || echo 0)" != "0" ] && [ -n "$(find "$ACTIVE" -mmin -240 2>/dev/null)" ]; then
    return 0
  fi
  return 1
}
if busy; then
  echo "Notes are being made; the update waits for the next run."
  notify "Сейчас делаются конспекты, обновление отложено. Оно применится само, когда бот освободится (проверка раз в 10 минут)."
  exit 0
fi

echo "Updating $OLD -> $NEW"
# Dependencies first: if the install fails, the code stays as it was and the next run tries again.
if ! git diff --quiet "$OLD" "$NEW" -- requirements.txt; then
  REQUIREMENTS=$(mktemp)
  git show "$NEW:requirements.txt" > "$REQUIREMENTS"
  "$APP_DIR/venv/bin/pip" install --quiet -r "$REQUIREMENTS"
  rm -f "$REQUIREMENTS"
fi
git reset --quiet --hard "$NEW"
chmod -R a+rX "$APP_DIR"
if [ -z "${SKIP_RESTART:-}" ]; then
  notify "Бот обновлён до ${NEW:0:7} и перезапускается."
  systemctl restart notes-bot
fi
echo "Updated to $NEW"
