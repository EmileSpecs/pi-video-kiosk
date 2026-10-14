#!/bin/sh
# Upgrade yt-dlp, smoke-test a known video, and roll back if the test fails.
set -eu
ROOT=${PIKIOSK_ROOT:-/opt/pi-kiosk}
VENV=$ROOT/venv
PY=$VENV/bin/python
PIP=$VENV/bin/pip
YT=$VENV/bin/yt-dlp
CONFIG=${PIKIOSK_CONFIG:-/etc/pi-kiosk/config.yaml}
STATE_DIR=${HOME}/.local/state/pi-kiosk
mkdir -p "$STATE_DIR"

channel=$("$PY" - "$CONFIG" <<'PY'
import sys
from pikiosk.config import load_config
config = load_config(sys.argv[1])
print(config.ytdlp.update_channel)
print(config.ytdlp.test_id)
PY
)
update_channel=$(printf '%s\n' "$channel" | sed -n '1p')
test_id=$(printf '%s\n' "$channel" | sed -n '2p')
old=$("$YT" --version 2>/dev/null || true)

if [ "$update_channel" = "nightly" ]; then
  "$PIP" install -U --pre "yt-dlp[default]"
else
  "$PIP" install -U "yt-dlp[default]"
fi

if ! "$YT" --simulate --quiet "$test_id"; then
  echo "yt-dlp smoke test failed; rolling back to ${old:-unknown}" >&2
  if [ -n "$old" ]; then
    "$PIP" install "yt-dlp==$old"
  fi
  exit 1
fi
"$YT" --version > "$STATE_DIR/ytdlp_version"
echo "yt-dlp $($YT --version)"
