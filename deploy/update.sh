#!/usr/bin/env bash
# Pulls the newest notes-bot branch onto the server and restarts the bot, if there is something new.
# Runs as root from notes-bot-update.timer. Does nothing when the branch has not moved.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/notes-bot}
BRANCH=${BRANCH:-notes-bot}

cd "$APP_DIR"
git fetch --quiet origin "$BRANCH"
OLD=$(git rev-parse HEAD)
NEW=$(git rev-parse "origin/$BRANCH")
if [ "$OLD" = "$NEW" ]; then
  exit 0
fi

# A recording being converted right now would be cut off by a restart. Try again in ten minutes.
if pgrep -x ffmpeg >/dev/null; then
  echo "A recording is being converted; the update waits for the next run."
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
  systemctl restart notes-bot
fi
echo "Updated to $NEW"
