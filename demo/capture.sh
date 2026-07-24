#!/usr/bin/env bash
# capture.sh <url> <frames> — snapshot the live dashboard repeatedly while a
# workflow runs, then assemble the frames into an mp4 + gif demo reel.
URL="${1:?url}"; N="${2:-30}"
OUT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/frames"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
mkdir -p "$OUT"; rm -f "$OUT"/*.png
for i in $(seq 1 "$N"); do
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
    --window-size=1600,950 --virtual-time-budget=2200 \
    --screenshot="$OUT/$(printf 'f%03d' "$i").png" "$URL" >/dev/null 2>&1
done
echo "captured $(ls "$OUT"/*.png 2>/dev/null | wc -l | tr -d ' ') frames"
