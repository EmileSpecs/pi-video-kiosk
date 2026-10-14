#!/usr/bin/env bash
# Idempotent Pi Video Kiosk installer. Run as root: sudo ./install.sh [options]
set -euo pipefail

USER_NAME=kiosk
DISPLAY_KIND=auto
ROTATION=0
NO_UI=0
HW_WATCHDOG=0
OFFLINE=0
DEV=0
REPO=""
REF=main
FORCE=0
HARDEN_SSH=0
PREFIX=/opt/pi-kiosk

usage() {
  cat <<'EOF'
Usage: sudo ./install.sh [options]
  --user NAME          Unix user (default: kiosk)
  --display auto|td2|legacy7
  --rotation 0|90|180|270
  --no-ui              Do not enable the Chromium kiosk
  --hw-watchdog        Set RuntimeWatchdogSec=15
  --offline            Enable the offline cache in a new config
  --dev                Editable install from this checkout
  --repo URL           Clone this git URL into /opt/pi-kiosk
  --ref NAME           Git branch or tag (default: main)
  --force              Continue when this is not a Pi 5 running Bookworm
  --harden-ssh         Disable SSH password login
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) USER_NAME=$2; shift 2 ;;
    --display) DISPLAY_KIND=$2; shift 2 ;;
    --rotation) ROTATION=$2; shift 2 ;;
    --no-ui) NO_UI=1; shift ;;
    --hw-watchdog) HW_WATCHDOG=1; shift ;;
    --offline) OFFLINE=1; shift ;;
    --dev) DEV=1; shift ;;
    --repo) REPO=$2; shift 2 ;;
    --ref) REF=$2; shift 2 ;;
    --force) FORCE=1; shift ;;
    --harden-ssh) HARDEN_SSH=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

warn() { echo "warning: $*" >&2; }

preflight() {
  local model="" codename="" arch
  arch=$(uname -m)
  if [[ -r /proc/device-tree/model ]]; then
    model=$(tr -d '\0' < /proc/device-tree/model)
  fi
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    codename=${VERSION_CODENAME:-}
  fi
  if [[ "$model" != *"Raspberry Pi 5"* || "$codename" != "bookworm" || "$arch" != "aarch64" ]]; then
    warn "expected Raspberry Pi 5, Raspberry Pi OS Bookworm, 64-bit (got: ${model:-unknown}, ${codename:-unknown}, $arch)"
    if [[ "$FORCE" -ne 1 ]]; then
      echo "re-run with --force to continue" >&2
      exit 1
    fi
  fi
  if ! command -v labwc >/dev/null 2>&1; then
    warn "labwc is not installed yet; the apt step will try to leave the desktop session for later"
  fi
}

install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y \
    mpv ffmpeg python3-venv python3-dev python3-pip \
    wlr-randr jq mosquitto-clients libevdev2 fonts-noto-color-emoji \
    curl unzip git ca-certificates
  if ! apt-get install -y chromium; then
    apt-get install -y chromium-browser || warn "Chromium was not installed; the touch UI will not start"
  fi
  if ! command -v deno >/dev/null 2>&1; then
    curl -fsSL https://deno.land/install.sh | CI=1 DENO_INSTALL=/usr/local sh || warn "Deno was not installed; some YouTube pages need it"
  fi
}

ensure_user() {
  if ! id "$USER_NAME" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "$USER_NAME"
  fi
  usermod -aG video,input,render,audio "$USER_NAME" || true
  if getent group autologin >/dev/null 2>&1; then
    usermod -aG autologin "$USER_NAME" || true
  fi
  systemctl set-default graphical.target || true
  if command -v raspi-config >/dev/null 2>&1; then
    # A stub whiptail keeps an unrecognised Trixie option from stopping on a dialog.
    local stub
    stub=$(mktemp -d)
    printf '%s\n' '#!/bin/sh' 'exit 1' > "$stub/whiptail"
    chmod +x "$stub/whiptail"
    PATH="$stub:$PATH" raspi-config nonint do_boot_behaviour B4 || true
    PATH="$stub:$PATH" raspi-config nonint do_wayland W3 || true
    PATH="$stub:$PATH" raspi-config nonint do_blanking 1 || true
    rm -rf "$stub"
  fi
  loginctl enable-linger "$USER_NAME"
}

place_tree() {
  if [[ "$DEV" -eq 1 ]]; then
    PREFIX=$(cd "$(dirname "$0")" && pwd)
    return
  fi
  if [[ -n "$REPO" && ! -d "$PREFIX/.git" ]]; then
    git clone --branch "$REF" "$REPO" "$PREFIX"
  elif [[ -d "$PREFIX/.git" && -n "$REPO" ]]; then
    git -C "$PREFIX" -c safe.directory="$PREFIX" fetch origin "$REF" || warn "could not fetch $REF"
    git -C "$PREFIX" -c safe.directory="$PREFIX" pull --ff-only origin "$REF" || warn "could not fast-forward $REF"
  elif [[ ! -f "$PREFIX/pyproject.toml" ]]; then
    echo "no checkout at $PREFIX; pass --repo URL or --dev" >&2
    exit 1
  fi
  chown -R "$USER_NAME:$USER_NAME" "$PREFIX"
}

fix_perms() {
  install -d -o root -g "$USER_NAME" -m 2770 /etc/pi-kiosk
  chown root:"$USER_NAME" /etc/pi-kiosk
  chmod 2770 /etc/pi-kiosk
  local path
  shopt -s nullglob
  for path in /etc/pi-kiosk/*; do
    if [[ -f "$path" ]]; then
      chown root:"$USER_NAME" "$path"
      chmod 0660 "$path"
    fi
  done
}

install_venv() {
  if [[ ! -d "$PREFIX/venv" ]]; then
    sudo -u "$USER_NAME" python3 -m venv "$PREFIX/venv"
  fi
  sudo -u "$USER_NAME" "$PREFIX/venv/bin/pip" install -U pip
  if [[ -f "$PREFIX/requirements.lock" ]]; then
    sudo -u "$USER_NAME" "$PREFIX/venv/bin/pip" install -r "$PREFIX/requirements.lock"
  fi
  if [[ "$DEV" -eq 1 ]]; then
    sudo -u "$USER_NAME" "$PREFIX/venv/bin/pip" install -e "$PREFIX"
  else
    sudo -u "$USER_NAME" "$PREFIX/venv/bin/pip" install "$PREFIX"
  fi
  sudo -u "$USER_NAME" "$PREFIX/venv/bin/pip" install -U "yt-dlp[default]" || warn "yt-dlp install failed"
  chown -R "$USER_NAME:$USER_NAME" "$PREFIX/venv"
}

write_config() {
  install -d -o root -g "$USER_NAME" -m 0770 /etc/pi-kiosk
  if [[ ! -f /etc/pi-kiosk/config.yaml ]]; then
    cp "$PREFIX/config/config.example.yaml" /etc/pi-kiosk/config.yaml
    chown root:"$USER_NAME" /etc/pi-kiosk/config.yaml
    chmod 0660 /etc/pi-kiosk/config.yaml
    if [[ -n "$REPO" ]]; then
      "$PREFIX/venv/bin/python" - "$REPO" <<'PY'
import sys
from pathlib import Path
path = Path("/etc/pi-kiosk/config.yaml")
text = path.read_text(encoding="utf-8")
text = text.replace("repo: null", f"repo: {sys.argv[1]}", 1)
path.write_text(text, encoding="utf-8")
PY
    fi
    if [[ "$ROTATION" != "0" ]]; then
      "$PREFIX/venv/bin/python" - "$ROTATION" <<'PY'
import sys
from pathlib import Path
path = Path("/etc/pi-kiosk/config.yaml")
text = path.read_text(encoding="utf-8")
path.write_text(text.replace("rotation: 0", f"rotation: {sys.argv[1]}", 1), encoding="utf-8")
PY
    fi
    if [[ "$DISPLAY_KIND" == "legacy7" ]]; then
      "$PREFIX/venv/bin/python" - <<'PY'
from pathlib import Path
path = Path("/etc/pi-kiosk/config.yaml")
text = path.read_text(encoding="utf-8")
old = "bestvideo[height<=?720]"
new = "bestvideo[height<=?480]"
path.write_text(text.replace(old, new), encoding="utf-8")
PY
    fi
    if [[ "$OFFLINE" -eq 1 ]]; then
      "$PREFIX/venv/bin/python" - <<'PY'
from pathlib import Path
path = Path("/etc/pi-kiosk/config.yaml")
text = path.read_text(encoding="utf-8")
path.write_text(text.replace("offline:\n  enabled: false", "offline:\n  enabled: true", 1), encoding="utf-8")
PY
    fi
  else
    echo "keeping existing /etc/pi-kiosk/config.yaml"
  fi
  if [[ ! -f /etc/pi-kiosk/secrets.yaml ]]; then
    cp "$PREFIX/config/secrets.example.yaml" /etc/pi-kiosk/secrets.yaml
  fi
  fix_perms
  password=$("$PREFIX/venv/bin/python" - <<'PY'
from pathlib import Path
import secrets
path = Path("/etc/pi-kiosk/secrets.yaml")
text = path.read_text(encoding="utf-8")
if "admin_password:" not in text or "change-me" in text.split("admin_password:", 1)[-1].splitlines()[0]:
    generated = secrets.token_urlsafe(12)
    if "admin_password:" in text:
        lines = []
        for line in text.splitlines():
            if line.startswith("admin_password:"):
                lines.append(f'admin_password: "{generated}"')
            else:
                lines.append(line)
        text = "\n".join(lines) + "\n"
    else:
        text += f'\nadmin_password: "{generated}"\n'
    path.write_text(text, encoding="utf-8")
    print(generated)
PY
)
  if [[ -n "${password:-}" ]]; then
    echo "Admin password (shown once): $password"
    echo "Open http://<this-pi>:8090 after the service starts."
  fi
  fix_perms
  cat > /etc/sudoers.d/pikiosk << EOF
$USER_NAME ALL=(root) NOPASSWD: $PREFIX/bin/pikiosk-fix-perms
EOF
  chmod 0440 /etc/sudoers.d/pikiosk
  install -d -o "$USER_NAME" -g "$USER_NAME" -m 0700 "/home/$USER_NAME/.local/state/pi-kiosk"
  install -d -o "$USER_NAME" -g "$USER_NAME" -m 0700 "/home/$USER_NAME/.cache/pi-kiosk/videos"
}

install_udev_and_cursor() {
  install -m 0644 "$PREFIX/system/udev/90-pikiosk-backlight.rules" /etc/udev/rules.d/90-pikiosk-backlight.rules
  udevadm control --reload || true
  udevadm trigger --subsystem-match=backlight || true
  theme=/usr/share/icons/pikiosk-invisible
  install -d "$theme/cursors"
  if [[ -f "$PREFIX/system/cursor/pikiosk-invisible/index.theme" ]]; then
    install -m 0644 "$PREFIX/system/cursor/pikiosk-invisible/index.theme" "$theme/index.theme"
  fi
  "$PREFIX/venv/bin/python" - "$theme/cursors/left_ptr" <<'PY'
import struct
import sys
from pathlib import Path
# One transparent pixel so the pointer is invisible.
path = Path(sys.argv[1])
header = struct.pack("<4sIII", b"Xcur", 16, 0x10000, 1)
toc = struct.pack("<III", 0xFFFD0002, 24, 16 + 12)
image = struct.pack("<IIIIIIIII", 36, 0xFFFD0002, 24, 1, 1, 1, 0, 0, 0)
pixel = struct.pack("<I", 0)
path.write_bytes(header + toc + image + pixel)
PY
}

install_mpv_and_labwc() {
  home=$(getent passwd "$USER_NAME" | cut -d: -f6)
  install -d -o "$USER_NAME" -g "$USER_NAME" "$home/.config/mpv"
  install -m 0644 -o "$USER_NAME" -g "$USER_NAME" "$PREFIX/system/mpv/mpv.conf" "$home/.config/mpv/mpv.conf"
  install -m 0644 -o "$USER_NAME" -g "$USER_NAME" "$PREFIX/system/mpv/input.conf" "$home/.config/mpv/input.conf"
  if [[ "$PREFIX" != "/opt/pi-kiosk" ]]; then
    sed -i "s#/opt/pi-kiosk#$PREFIX#g" "$home/.config/mpv/mpv.conf"
  fi
  install -d -o "$USER_NAME" -g "$USER_NAME" "$home/.config/labwc"
  install -m 0755 -o "$USER_NAME" -g "$USER_NAME" "$PREFIX/system/labwc/autostart" "$home/.config/labwc/autostart"
  install -m 0644 -o "$USER_NAME" -g "$USER_NAME" "$PREFIX/system/labwc/environment" "$home/.config/labwc/environment"
  rc="$home/.config/labwc/rc.xml"
  fragment=$(cat "$PREFIX/system/labwc/rc.xml.fragment")
  if [[ -f "$rc" ]]; then
    cp "$rc" "$rc.bak"
    if grep -q "pikiosk-start" "$rc"; then
      "$PREFIX/venv/bin/python" - "$rc" "$PREFIX/system/labwc/rc.xml.fragment" <<'PY'
import sys
from pathlib import Path
rc = Path(sys.argv[1])
fragment = Path(sys.argv[2]).read_text(encoding="utf-8")
text = rc.read_text(encoding="utf-8")
start = text.find("<!-- pikiosk-start -->")
end = text.find("<!-- pikiosk-end -->")
if start != -1 and end != -1:
    text = text[:start] + fragment + text[end + len("<!-- pikiosk-end -->"):]
else:
    text = text.replace("</labwc_config>", fragment + "\n</labwc_config>")
rc.write_text(text, encoding="utf-8")
PY
    else
      printf '\n%s\n' "$fragment" >> "$rc"
    fi
  else
    printf '<labwc_config>\n%s\n</labwc_config>\n' "$fragment" > "$rc"
  fi
  chown "$USER_NAME:$USER_NAME" "$rc"
  systemctl disable --now squeekboard.service >/dev/null 2>&1 || true
}

install_units() {
  dest="/home/$USER_NAME/.config/systemd/user"
  install -d -o "$USER_NAME" -g "$USER_NAME" "$dest"
  for unit in "$PREFIX"/system/systemd/user/*; do
    name=$(basename "$unit")
    sed "s#/opt/pi-kiosk#$PREFIX#g" "$unit" > "$dest/$name"
    chown "$USER_NAME:$USER_NAME" "$dest/$name"
  done
  uid=$(id -u "$USER_NAME")
  run_user() {
    sudo -u "$USER_NAME" XDG_RUNTIME_DIR="/run/user/$uid" systemctl --user "$@" || true
  }
  run_user daemon-reload
  run_user enable pikioskd.service pikiosk-ytdlp-update.timer pikiosk-update.timer
  if [[ "$NO_UI" -eq 0 ]]; then
    run_user enable --now pikiosk-mpv.service
    run_user enable pikiosk-ui.service
  fi
  install -d /etc/systemd/journald.conf.d
  install -m 0644 "$PREFIX/system/journald/pikiosk.conf" /etc/systemd/journald.conf.d/pikiosk.conf
  if [[ "$HW_WATCHDOG" -eq 1 ]]; then
    install -d /etc/systemd/system.conf.d
    install -m 0644 "$PREFIX/system/systemd/pikiosk-watchdog.conf" /etc/systemd/system.conf.d/pikiosk.conf
  fi
  if [[ "$HARDEN_SSH" -eq 1 ]]; then
    install -d /etc/ssh/sshd_config.d
    cat > /etc/ssh/sshd_config.d/pikiosk.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF
    systemctl reload ssh || systemctl reload sshd || true
  fi
}

configure_desktop() {
  if [[ "$NO_UI" -eq 1 ]]; then
    return
  fi
  install -d /usr/share/wayland-sessions
  cat > /usr/share/wayland-sessions/pikiosk.desktop << EOF
[Desktop Entry]
Name=Pi Video Kiosk
Comment=Locked-down video kiosk
Exec=$PREFIX/bin/run-labwc.sh
TryExec=labwc
Type=Application
DesktopNames=labwc;wlroots
EOF
  if [[ -d /etc/lightdm ]]; then
    install -d /etc/lightdm/lightdm.conf.d
    cat > /etc/lightdm/lightdm.conf.d/50-pikiosk.conf << EOF
[Seat:*]
autologin-user=$USER_NAME
autologin-user-timeout=0
autologin-session=pikiosk
user-session=pikiosk
EOF
    systemctl restart lightdm || true
  else
    cat > /etc/systemd/system/pikiosk-labwc.service << EOF
[Unit]
Description=Pi Video Kiosk display
After=systemd-user-sessions.service
Conflicts=getty@tty1.service

[Service]
User=$USER_NAME
PAMName=login
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
StandardInput=tty
Restart=always
RestartSec=2
ExecStart=$PREFIX/bin/run-labwc.sh

[Install]
WantedBy=graphical.target
EOF
    systemctl daemon-reload
    systemctl enable --now pikiosk-labwc.service || true
  fi
}

summary() {
  echo
  echo "Pi Video Kiosk installed at $PREFIX"
  echo "Config: /etc/pi-kiosk/config.yaml"
  echo "Secrets: /etc/pi-kiosk/secrets.yaml"
  echo "Display option: $DISPLAY_KIND  rotation: $ROTATION"
  if [[ -d /sys/class/backlight ]]; then
    echo "Backlight devices: $(ls /sys/class/backlight | tr '\n' ' ')"
  fi
  echo "Logs: journalctl --user -u pikioskd -f"
  echo "Manual update: sudo -u $USER_NAME $PREFIX/bin/pikiosk-update.sh"
  if [[ -x "$PREFIX/venv/bin/pikioskd" ]]; then
    if "$PREFIX/venv/bin/pikioskd" --check-config /etc/pi-kiosk/config.yaml; then
      echo "config check: ok"
    else
      echo "config check failed" >&2
    fi
  fi
}

preflight
install_packages
ensure_user
place_tree
install_venv
write_config
install_udev_and_cursor
install_mpv_and_labwc
install_units
configure_desktop
summary
