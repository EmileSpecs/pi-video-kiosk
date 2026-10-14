"""wlr-randr wrapper. Tests use the recording implementation."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol

log = logging.getLogger("pikiosk.output")


class OutputControl(Protocol):
    async def set_on(self, name: str, on: bool, transform: str | None = None) -> None: ...

    async def discover_name(self) -> str | None: ...


class RecordingOutput:
    def __init__(self, name: str | None = "DSI-1") -> None:
        self.name = name
        self.calls: list[tuple[str, bool, str | None]] = []

    async def set_on(self, name: str, on: bool, transform: str | None = None) -> None:
        self.calls.append((name, on, transform))

    async def discover_name(self) -> str | None:
        return self.name


class WlrRandr:
    async def discover_name(self) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "wlr-randr",
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None
        stdout, _ = await proc.communicate()
        try:
            payload = json.loads(stdout.decode() or "[]")
        except json.JSONDecodeError:
            return None
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and str(item.get("name", "")).startswith("DSI-"):
                    return str(item["name"])
        if isinstance(payload, dict):
            for name in payload:
                if str(name).startswith("DSI-"):
                    return str(name)
        return None

    async def set_on(self, name: str, on: bool, transform: str | None = None) -> None:
        args = ["wlr-randr", "--output", name, "--on" if on else "--off"]
        if transform and on:
            args.extend(["--transform", transform])
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("wlr-randr failed error=%s", exc)
            return
        await proc.wait()
