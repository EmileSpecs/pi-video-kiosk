#!/bin/sh
# Start labwc from this user's config only, so the Pi desktop panel does not also start.
set -eu
config=${XDG_CONFIG_HOME:-$HOME/.config}/labwc
exec labwc -C "$config"
