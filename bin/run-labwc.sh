#!/bin/sh
# Pi OS sets the GPU environment in setup_env. Source that, then start labwc
# from this user's config only so the desktop panel is not merged in.
if [ -r /usr/bin/setup_env ]; then
  # shellcheck disable=SC1091
  . /usr/bin/setup_env
fi
if command -v raspi-config >/dev/null 2>&1; then
  if raspi-config nonint is_pi && ! raspi-config nonint gpu_has_mmu; then
    export WLR_RENDERER=pixman
  fi
fi
config=${XDG_CONFIG_HOME:-$HOME/.config}/labwc
exec /usr/bin/labwc -C "$config"
