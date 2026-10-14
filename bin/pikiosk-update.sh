#!/bin/sh
# Fast-forward the kiosk checkout, reinstall, and roll back if it does not come back.
set -eu
ROOT=${PIKIOSK_ROOT:-/opt/pi-kiosk}
CONFIG=${PIKIOSK_CONFIG:-/etc/pi-kiosk/config.yaml}
STATE=${PIKIOSK_STATE:-$HOME/.local/state/pi-kiosk/state.json}
FROM_TIMER=0
if [ "${1:-}" = "--from-timer" ]; then
  FROM_TIMER=1
fi

cd "$ROOT"
PY=$ROOT/venv/bin/python
fields=$("$PY" - "$CONFIG" "$STATE" "$FROM_TIMER" <<'PY'
import json
import sys
from pathlib import Path
from pikiosk.config import load_config
config = load_config(sys.argv[1])
player = "idle"
path = Path(sys.argv[2])
if path.exists():
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("player"):
            player = str(raw["player"])
    except (OSError, json.JSONDecodeError):
        player = "idle"
print(config.update.ref)
print("1" if config.update.only_when_idle else "0")
print("1" if config.update.auto else "0")
print(player)
print(config.admin.port)
print("1" if config.admin.enabled else "0")
print(config.ui.port)
PY
)
ref=$(printf '%s\n' "$fields" | sed -n '1p')
only_idle=$(printf '%s\n' "$fields" | sed -n '2p')
auto=$(printf '%s\n' "$fields" | sed -n '3p')
player=$(printf '%s\n' "$fields" | sed -n '4p')
admin_port=$(printf '%s\n' "$fields" | sed -n '5p')
admin_on=$(printf '%s\n' "$fields" | sed -n '6p')
ui_port=$(printf '%s\n' "$fields" | sed -n '7p')

if [ "$FROM_TIMER" = "1" ] && [ "$auto" != "1" ]; then
  echo "auto update is off"
  exit 0
fi
if [ "$only_idle" = "1" ] && { [ "$player" = "playing" ] || [ "$player" = "loading" ]; }; then
  echo "skipped: player is $player"
  exit 0
fi

before=$(git rev-parse HEAD)
git fetch --prune origin
remote=$(git rev-parse "origin/$ref")
if [ "$remote" = "$before" ]; then
  echo "up to date $before"
  exit 0
fi

rollback() {
  echo "update failed; restoring $before" >&2
  git reset --hard "$before"
  "$ROOT/venv/bin/pip" install -r "$ROOT/requirements.lock"
  "$ROOT/venv/bin/pip" install -e "$ROOT"
  systemctl --user restart pikioskd.service || true
}

if ! git pull --ff-only origin "$ref"; then
  echo "fast-forward failed" >&2
  exit 1
fi
if ! "$ROOT/venv/bin/pip" install -r "$ROOT/requirements.lock"; then
  rollback
  exit 1
fi
if ! "$ROOT/venv/bin/pip" install -e "$ROOT"; then
  rollback
  exit 1
fi

unit_src=$ROOT/system/systemd/user
unit_dst=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
if [ -d "$unit_src" ]; then
  mkdir -p "$unit_dst"
  changed=0
  for unit in pikiosk-mpv.service pikiosk-ui.service pikiosk-session.target; do
    if [ -f "$unit_src/$unit" ] && ! cmp -s "$unit_src/$unit" "$unit_dst/$unit" 2>/dev/null; then
      cp "$unit_src/$unit" "$unit_dst/$unit"
      changed=1
    fi
  done
  for unit in pikioskd.service pikiosk-ytdlp-update.service pikiosk-ytdlp-update.timer pikiosk-update.service pikiosk-update.timer; do
    if [ -f "$unit_src/$unit" ]; then
      cp "$unit_src/$unit" "$unit_dst/$unit"
    fi
  done
  systemctl --user daemon-reload || true
fi

systemctl --user restart pikioskd.service
if [ "${changed:-0}" = "1" ]; then
  systemctl --user restart pikiosk-session.target || true
fi

if [ "$admin_on" = "1" ]; then
  url="http://127.0.0.1:${admin_port}/healthz"
else
  url="http://127.0.0.1:${ui_port}/healthz"
fi
if ! "$ROOT/bin/wait-for-http" "$url" 30; then
  rollback
  exit 1
fi
echo "updated $(git rev-parse HEAD)"
