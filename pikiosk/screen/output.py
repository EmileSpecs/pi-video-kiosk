"""wlr-randr wrapper. Tests use the recording implementation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("pikiosk.output")


@dataclass(frozen=True)
class OutputInfo:
    name: str
    width: int  # native mode, before any transform
    height: int
    transform: str = "normal"

    @property
    def native_portrait(self) -> bool:
        return self.height > self.width


class OutputControl(Protocol):
    async def set_on(self, name: str, on: bool, transform: str | None = None) -> None: ...

    async def discover_name(self) -> str | None: ...

    async def describe(self) -> OutputInfo | None: ...


class RecordingOutput:
    def __init__(self, name: str | None = "DSI-1", *, width: int = 720, height: int = 1280) -> None:
        self.name = name
        self.width = width
        self.height = height
        self.calls: list[tuple[str, bool, str | None]] = []

    async def set_on(self, name: str, on: bool, transform: str | None = None) -> None:
        self.calls.append((name, on, transform))

    async def discover_name(self) -> str | None:
        return self.name

    async def describe(self) -> OutputInfo | None:
        if self.name is None:
            return None
        return OutputInfo(self.name, self.width, self.height)


def wayland_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for wlr-randr.

    pikioskd starts from default.target before labwc exports WAYLAND_DISPLAY
    into the systemd user manager, so look for the socket ourselves.
    """
    env = dict(os.environ if environ is None else environ)
    if env.get("WAYLAND_DISPLAY"):
        return env
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid() if hasattr(os, 'getuid') else 0}"
    env.setdefault("XDG_RUNTIME_DIR", runtime)
    try:
        sockets = sorted(
            p.name for p in Path(runtime).iterdir() if p.name.startswith("wayland-") and not p.name.endswith(".lock")
        )
    except OSError:
        sockets = []
    if sockets:
        env["WAYLAND_DISPLAY"] = sockets[0]
    return env


def parse_outputs(payload: Any) -> list[OutputInfo]:
    """Outputs from `wlr-randr --json` (a list in current releases, a dict in old ones)."""
    found: list[OutputInfo] = []
    entries: list[tuple[str, Any]] = []
    if isinstance(payload, list):
        entries = [(str(item.get("name", "")), item) for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict):
        entries = [(str(name), item) for name, item in payload.items() if isinstance(item, dict)]
    for name, item in entries:
        if not name:
            continue
        width = height = 0
        modes = item.get("modes") or []
        current = next((m for m in modes if isinstance(m, dict) and m.get("current")), None)
        if current is None and modes and isinstance(modes[0], dict):
            current = modes[0]
        if isinstance(current, dict):
            width = int(current.get("width") or 0)
            height = int(current.get("height") or 0)
        found.append(OutputInfo(name, width, height, str(item.get("transform") or "normal")))
    return found


def pick_output(outputs: list[OutputInfo], wanted: str = "auto") -> OutputInfo | None:
    if wanted and wanted != "auto":
        return next((o for o in outputs if o.name == wanted), None)
    dsi = next((o for o in outputs if o.name.startswith("DSI-")), None)
    return dsi or (outputs[0] if outputs else None)


class WlrRandr:
    def __init__(self, output_name: str = "auto") -> None:
        self.output_name = output_name

    async def _outputs(self) -> list[OutputInfo]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "wlr-randr",
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=wayland_env(),
            )
        except OSError:
            return []
        stdout, _ = await proc.communicate()
        try:
            payload = json.loads(stdout.decode() or "[]")
        except json.JSONDecodeError:
            return []
        return parse_outputs(payload)

    async def discover_name(self) -> str | None:
        info = pick_output(await self._outputs(), self.output_name)
        return info.name if info else None

    async def describe(self) -> OutputInfo | None:
        return pick_output(await self._outputs(), self.output_name)

    async def set_on(self, name: str, on: bool, transform: str | None = None) -> None:
        args = ["wlr-randr", "--output", name, "--on" if on else "--off"]
        if transform and on:
            args.extend(["--transform", transform])
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=wayland_env(),
            )
        except OSError as exc:
            log.warning("wlr-randr failed error=%s", exc)
            return
        _, err = await proc.communicate()
        code = getattr(proc, "returncode", 0)
        if code:
            detail = (err or b"").decode(errors="replace").strip()[:200]
            log.warning("wlr-randr %s failed code=%s error=%s", " ".join(args[1:]), code, detail)
