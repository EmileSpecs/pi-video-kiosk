#!/usr/bin/env bash
# Remove the kiosk units, udev rule, and labwc snippet. Keeps /etc/pi-kiosk unless --purge.
set -euo pipefail

PURGE=0
USER_NAME=kiosk
PREFIX=/opt/pi-kiosk
if [[ "${1:-}" == "--purge" ]]; then
  PURGE=1
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

uid=$(id -u "$USER_NAME" 2>/dev/null || true)
if [[ -n "$uid" ]]; then
  sudo -u "$USER_NAME" XDG_RUNTIME_DIR="/run/user/$uid" systemctl --user disable --now \
    pikioskd.service pikiosk-ui.service pikiosk-mpv.service \
    pikiosk-ytdlp-update.timer pikiosk-update.timer || true
  rm -f "/home/$USER_NAME/.config/systemd/user/pikiosk-"* \
    "/home/$USER_NAME/.config/systemd/user/pikioskd.service" \
    "/home/$USER_NAME/.config/systemd/user/pikiosk-session.target"
  rc="/home/$USER_NAME/.config/labwc/rc.xml"
  if [[ -f "$rc.bak" ]]; then
    mv "$rc.bak" "$rc"
  fi
fi

rm -f /etc/udev/rules.d/90-pikiosk-backlight.rules
rm -f /etc/systemd/journald.conf.d/pikiosk.conf
rm -rf /usr/share/icons/pikiosk-invisible
udevadm control --reload || true

if [[ -d "$PREFIX" && "$PREFIX" != "$(pwd)" ]]; then
  rm -rf "$PREFIX"
fi

if [[ "$PURGE" -eq 1 ]]; then
  rm -rf /etc/pi-kiosk
fi

echo "Pi Video Kiosk units removed. /etc/pi-kiosk was kept unless --purge was passed."
