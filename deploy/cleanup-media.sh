#!/usr/bin/env bash
# Deletes recordings the local Bot API server keeps on disk after three hours.
# Only the media folders are touched, never the server's own database.
set -euo pipefail
find /var/lib/telegram-bot-api -mindepth 3 -type f -mmin +180 \
  \( -path '*/documents/*' -o -path '*/voice/*' -o -path '*/audio/*' -o -path '*/video/*' \
     -o -path '*/video_notes/*' -o -path '*/animations/*' -o -path '*/photos/*' -o -path '*/temp/*' \) -delete
