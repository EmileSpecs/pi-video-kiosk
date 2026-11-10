"""PipeWire/PulseAudio sink helpers for USB speakers on a DSI kiosk."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

log = logging.getLogger("pikiosk.audio")

_NULL_MARKERS = ("auto_null", "null", "monitor")
_USB_MARKERS = ("usb", "uac", "headset", "headphone", "dongle")


@dataclass(frozen=True)
class Sink:
    name: str
    state: str = ""
    description: str = ""


@dataclass(frozen=True)
class AudioRoute:
    sink: str | None
    changed: bool
    detail: str = ""


def _run(*args: str) -> str:
    try:
        completed = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def list_sinks() -> list[Sink]:
    """Short sink list from pactl, richest description we can get cheaply."""
    short = _run("pactl", "list", "short", "sinks")
    if not short:
        return []
    sinks: list[Sink] = []
    for line in short.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[1].strip()
        state = parts[4].strip() if len(parts) > 4 else ""
        sinks.append(Sink(name=name, state=state))
    # Descriptions help match "USB Audio Device" when the name is opaque.
    detail = _run("pactl", "list", "sinks")
    if detail:
        current: str | None = None
        descriptions: dict[str, str] = {}
        for line in detail.splitlines():
            stripped = line.strip()
            if stripped.startswith("Name:"):
                current = stripped.split(":", 1)[1].strip()
            elif current and stripped.startswith("Description:"):
                descriptions[current] = stripped.split(":", 1)[1].strip()
                current = None
        sinks = [
            Sink(name=s.name, state=s.state, description=descriptions.get(s.name, ""))
            for s in sinks
        ]
    return sinks


def default_sink() -> str | None:
    name = _run("pactl", "get-default-sink")
    return name or None


def set_default_sink(name: str) -> bool:
    try:
        completed = subprocess.run(
            ["pactl", "set-default-sink", name],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("set-default-sink failed error=%s", exc)
        return False
    if completed.returncode != 0:
        log.warning("set-default-sink failed stderr=%s", (completed.stderr or "").strip())
        return False
    return True


def unmute_sink(name: str, volume_percent: int = 80) -> None:
    """Make sure the sink itself is not muted at 0 % under mpv's volume."""
    _run("pactl", "set-sink-mute", name, "0")
    # Soft volume on the sink; mpv still applies its own percentage on top.
    level = max(0, min(100, volume_percent))
    _run("pactl", "set-sink-volume", name, f"{level}%")


def _is_null(sink: Sink) -> bool:
    blob = f"{sink.name} {sink.description}".lower()
    return any(marker in blob for marker in _NULL_MARKERS)


def _is_usb(sink: Sink) -> bool:
    blob = f"{sink.name} {sink.description}".lower()
    return any(marker in blob for marker in _USB_MARKERS)


def pick_sink(sinks: list[Sink], preferred: str | None = None) -> Sink | None:
    """Choose a real output. Explicit name wins; else USB; else first non-null."""
    if not sinks:
        return None
    if preferred and preferred != "auto":
        for sink in sinks:
            if sink.name == preferred:
                return sink
    usable = [s for s in sinks if not _is_null(s)]
    if not usable:
        return None
    for sink in usable:
        if _is_usb(sink):
            return sink
    running = [s for s in usable if s.state.upper() == "RUNNING"]
    return (running or usable)[0]


def ensure_route(preferred: str = "auto") -> AudioRoute:
    """Point PipeWire at a usable sink (USB when present). No-op if already right."""
    sinks = list_sinks()
    if not sinks:
        return AudioRoute(None, False, "no sinks")
    chosen = pick_sink(sinks, preferred if preferred != "auto" else None)
    if chosen is None:
        return AudioRoute(default_sink(), False, "only null sinks")
    current = default_sink()
    changed = False
    if current != chosen.name:
        if not set_default_sink(chosen.name):
            return AudioRoute(current, False, f"could not select {chosen.name}")
        changed = True
        log.info("audio default sink %s -> %s", current or "none", chosen.name)
    unmute_sink(chosen.name)
    return AudioRoute(chosen.name, changed, "usb" if _is_usb(chosen) else "ok")
