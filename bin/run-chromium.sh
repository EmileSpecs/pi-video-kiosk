#!/bin/sh
# Chromium kiosk for the child grid. The binary name differs between images.
set -eu
bin=""
for candidate in chromium chromium-browser; do
  if command -v "$candidate" >/dev/null 2>&1; then
    bin=$candidate
    break
  fi
done
if [ -z "$bin" ]; then
  echo "chromium is not installed" >&2
  exit 1
fi
dir=${HOME}/.local/share/pi-kiosk/chromium
mkdir -p "$dir/Default"
prefs=$dir/Default/Preferences
if [ -f "$prefs" ]; then
  sed -i \
    -e 's/"exited_cleanly":false/"exited_cleanly":true/' \
    -e 's/"exit_type":"Crashed"/"exit_type":"Normal"/' \
    "$prefs" || true
fi
exec "$bin" \
  --kiosk \
  --ozone-platform=wayland \
  --app=http://127.0.0.1:8088/ \
  --noerrdialogs \
  --disable-infobars \
  --no-first-run \
  --incognito \
  --disable-pinch \
  --overscroll-history-navigation=0 \
  --disable-features=Translate,TouchpadOverscrollHistoryNavigation,MediaRouter \
  --disable-session-crashed-bubble \
  --check-for-update-interval=31536000 \
  --autoplay-policy=no-user-gesture-required \
  --password-store=basic \
  --user-data-dir="$dir"
