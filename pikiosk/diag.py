"""Small facts about the running install: commit, audio sink, log tails."""

from __future__ import annotations

import subprocess
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MPV_LOG = "~/.cache/pi-kiosk/mpv.log"


def git_commit(root: Path | None = None) -> str:
    """Short commit of the checkout without running git (not installed everywhere)."""
    base = (root or ROOT) / ".git"
    try:
        head = (base / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not head.startswith("ref:"):
        return head[:9]
    ref = head.split(":", 1)[1].strip()
    try:
        return (base / ref).read_text(encoding="utf-8").strip()[:9]
    except OSError:
        pass
    try:
        for line in (base / "packed-refs").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0][:9]
    except OSError:
        pass
    return ""


def audio_sink() -> str | None:
    """Name of the default PipeWire/PulseAudio sink, or None when unknown."""
    try:
        result = subprocess.run(
            ["pactl", "get-default-sink"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    name = result.stdout.strip()
    return name or None


def tail(path: str | Path, lines: int = 20) -> list[str]:
    file = Path(path).expanduser()
    try:
        with file.open("r", encoding="utf-8", errors="replace") as handle:
            return list(deque((line.rstrip("\n") for line in handle), maxlen=lines))
    except OSError:
        return []
