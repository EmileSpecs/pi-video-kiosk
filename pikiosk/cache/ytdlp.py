"""yt-dlp subprocess wrapper and error classification."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("pikiosk.ytdlp")

@dataclass(frozen=True)
class ResolvedStream:
    video: str
    audio: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


def ytdlp_command() -> list[str]:
    override = os.environ.get("PIKIOSK_YTDLP")
    for candidate in (override, "/opt/pi-kiosk/venv/bin/yt-dlp", shutil.which("yt-dlp")):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return [candidate]
    return ["yt-dlp"]


def ytdlp_runtime_args() -> list[str]:
    args = ["--remote-components", "ejs:github"]
    for candidate in ("/usr/local/bin/deno", "/usr/bin/deno", shutil.which("deno")):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            args.extend(["--js-runtimes", f"deno:{candidate}"])
            break
    return args


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

    async def resolve_streams(
        self,
        url: str,
        fmt: str,
        *,
        playlist: bool,
        limit: int | None,
    ) -> tuple[list[ResolvedStream], str]:
        args = ["-J", "--no-warnings", "-f", fmt]
        if playlist:
            args.append("--yes-playlist")
            if limit:
                args.extend(["--playlist-end", str(limit)])
        else:
            args.append("--no-playlist")
        args.append(url)
        code, out, err = await self.run(args)
        if code != 0 and fmt != "bv*+ba/b" and "format" in (err or out).lower():
            retried: list[str] = []
            replace_next = False
            for arg in args:
                if replace_next:
                    retried.append("bv*+ba/b")
                    replace_next = False
                    continue
                retried.append(arg)
                if arg == "-f":
                    replace_next = True
            code, out, err = await self.run(retried)
        if code != 0:
            return [], (err or out).strip()
        try:
            payload = json.loads(out or "{}")
        except json.JSONDecodeError:
            return [], (err or "yt-dlp did not return JSON").strip()
        if not isinstance(payload, dict):
            return [], (err or "yt-dlp did not return JSON").strip()
        entries = payload.get("entries")
        items = entries if isinstance(entries, list) else [payload]
        streams: list[ResolvedStream] = []
        for item in items:
            if isinstance(item, dict):
                pair = stream_pair(item)
                if pair:
                    streams.append(pair)
        if not streams:
            return [], (err or "yt-dlp returned no playable URL").strip()
        return streams, ""

    async def download_media(
        self,
        url: str,
        fmt: str,
        directory: str,
        *,
        playlist: bool,
        limit: int | None,
    ) -> tuple[list[str], str]:
        """Save a real media file and return its path.

        mpv cannot play the separate streams YouTube returns. yt-dlp downloads
        them, merges picture and sound, and mpv then opens that file.
        """
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        paths, err = await self._download_once(url, fmt, folder, playlist=playlist, limit=limit)
        if paths:
            return paths, ""
        if _download_should_retry(err):
            paths, retry_err = await self._download_once(
                url,
                "18/b",
                folder,
                playlist=playlist,
                limit=limit,
                extractor_args="youtube:player_client=web_embedded",
            )
            if paths:
                return paths, ""
            return [], (retry_err or err).strip()
        return [], err.strip()

    async def _download_once(
        self,
        url: str,
        fmt: str,
        folder: Path,
        *,
        playlist: bool,
        limit: int | None,
        extractor_args: str | None = None,
    ) -> tuple[list[str], str]:
        args = [
            "-f",
            fmt,
            "--merge-output-format",
            "mkv",
            "--force-overwrites",
            "--no-warnings",
            "--no-progress",
            "-o",
            str(folder / "%(id)s.%(ext)s"),
            "--print",
            "after_move:filepath",
        ]
        if extractor_args:
            args.extend(["--extractor-args", extractor_args])
        if playlist:
            args.append("--yes-playlist")
            if limit:
                args.extend(["--playlist-end", str(limit)])
        else:
            args.append("--no-playlist")
        args.append(url)
        code, out, err = await self.run(args)
        paths = [line.strip() for line in out.splitlines() if line.strip() and Path(line.strip()).is_file()]
        if code != 0 or not paths:
            return [], (err or out or "yt-dlp did not write a file").strip()
        keep = set(paths)
        for old in folder.iterdir():
            if old.is_file() and str(old) not in keep:
                try:
                    old.unlink()
                except OSError:
                    pass
        return paths, ""


def _download_should_retry(err: str) -> bool:
    lowered = err.lower()
    return any(
        token in lowered
        for token in ("403", "forbidden", "format", "sabr", "nsig", "signature", "only images")
    )


def stream_pair(item: dict[str, Any]) -> ResolvedStream | None:
    requested = item.get("requested_formats")
    source = requested[0] if isinstance(requested, list) and requested and isinstance(requested[0], dict) else item
    headers = _http_headers(source if isinstance(source, dict) else item)
    if not headers:
        headers = _http_headers(item)
    if isinstance(requested, list) and requested and isinstance(requested[0], dict):
        video = requested[0].get("url")
        audio = requested[1].get("url") if len(requested) > 1 and isinstance(requested[1], dict) else None
        if isinstance(video, str) and video.startswith("http"):
            return ResolvedStream(
                video,
                audio if isinstance(audio, str) and audio.startswith("http") else None,
                headers,
            )
    direct = item.get("url")
    if isinstance(direct, str) and direct.startswith("http"):
        return ResolvedStream(direct, None, headers)
    return None


def _http_headers(item: dict[str, Any]) -> dict[str, str]:
    raw = item.get("http_headers")
    if not isinstance(raw, dict):
        return {}
    headers: dict[str, str] = {}
    for key in ("User-Agent", "Referer", "Cookie"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            headers[key] = value
    return headers
