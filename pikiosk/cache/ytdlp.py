"""yt-dlp subprocess wrapper and error classification."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger("pikiosk.ytdlp")


def classify_error(text: str) -> str:
    lowered = text.lower()
    if "sign in to confirm" in lowered or "not a bot" in lowered:
        return "bot_check"
    if "age" in lowered and "confirm" in lowered or "age-restricted" in lowered or "age restricted" in lowered:
        return "age_restricted"
    if "not available in your country" in lowered or "geo" in lowered:
        return "geo"
    if "video unavailable" in lowered or "private video" in lowered or "removed" in lowered:
        return "unavailable"
    if any(token in lowered for token in ("network", "timed out", "urlopen", "connection")):
        return "network"
    return "extractor"


class YtDlp:
    def __init__(self, command: list[str], extra_args: list[str] | None = None) -> None:
        self.command = command
        self.extra_args = list(extra_args or [])

    async def run(self, args: list[str]) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *self.command,
            *args,
            *self.extra_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode or 0, stdout.decode(), stderr.decode()

    async def version(self) -> str:
        code, out, err = await self.run(["--version"])
        return (out or err).strip()

    async def simulate(self, video_id: str) -> bool:
        code, _out, _err = await self.run(["--simulate", "--quiet", video_id])
        return code == 0

    async def flat_playlist(self, playlist_id: str, limit: int) -> list[dict[str, Any]]:
        code, out, err = await self.run(
            ["--flat-playlist", "-J", "--playlist-end", str(limit), playlist_id]
        )
        if code != 0:
            log.warning("playlist expand failed error=%s", classify_error(err))
            return []
        try:
            payload = json.loads(out or "{}")
        except json.JSONDecodeError:
            return []
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return []
        return [item for item in entries if isinstance(item, dict)]

    async def dump_json(self, video_id: str) -> dict[str, Any] | None:
        code, out, _err = await self.run(["-J", "--no-playlist", video_id])
        if code != 0:
            return None
        try:
            payload = json.loads(out or "{}")
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
