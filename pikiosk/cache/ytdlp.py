"""yt-dlp subprocess wrapper and error classification."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("pikiosk.ytdlp")

@dataclass(frozen=True)
class ResolvedStream:
    video: str
    audio: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Download:
    """A media file yt-dlp wrote, with the title YouTube gave the video."""

    path: str
    title: str | None = None


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
    if any(token in lowered for token in PLAYER_TOKENS):
        return "player"
    return "extractor"


# Strings mpv itself produces. They mean the file or the output device failed,
# not that YouTube changed, so they never count toward the yt-dlp auto-update.
PLAYER_TOKENS = (
    "loading failed",
    "no audio or video data played",
    "playback did not start",
    "failed to open",
    "can not open",
    "failed to recognize file format",
    "failed to initialize",
    "error opening",
    "audio output",
    "video output",
    "[vo/",
    "[ao/",
    "vo:",
    "ao:",
    "mpv offline",
)


ProgressHook = Callable[[float], None]
_PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
DEFAULT_FORMAT = "bv*[height<=?720][vcodec^=avc1]+ba[acodec^=mp4a]/18/b"


def parse_progress(line: str) -> float | None:
    """Percentage from a `--newline --progress` line, or None for other lines."""
    match = _PROGRESS_RE.search(line)
    if not match:
        return None
    return max(0.0, min(100.0, float(match.group(1))))


class YtDlp:
    def __init__(self, command: list[str], extra_args: list[str] | None = None) -> None:
        self.command = command
        self.extra_args = list(extra_args or [])

    async def run(self, args: list[str]) -> tuple[int, str, str]:
        # Options first, then the request; yt-dlp reads them in any order but
        # this keeps the URL last in logs and in `ps`.
        proc = await asyncio.create_subprocess_exec(
            *self.command,
            *self.extra_args,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    async def run_streaming(
        self,
        args: list[str],
        *,
        on_line: Callable[[str], None] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        """Like run(), but hands each stdout line to on_line as it arrives."""
        proc = await asyncio.create_subprocess_exec(
            *self.command,
            *self.extra_args,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_pipe = getattr(proc, "stdout", None)
        stderr_pipe = getattr(proc, "stderr", None)
        if stdout_pipe is None or stderr_pipe is None:
            stdout, stderr = await proc.communicate()
            return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")
        lines: list[str] = []

        async def pump() -> None:
            while True:
                raw = await stdout_pipe.readline()
                if not raw:
                    return
                line = raw.decode(errors="replace").rstrip("\r\n")
                lines.append(line)
                if on_line is not None:
                    on_line(line)

        async def finish() -> bytes:
            _ignored, err = await asyncio.gather(pump(), stderr_pipe.read())
            await proc.wait()
            return bytes(err)

        try:
            stderr = await asyncio.wait_for(finish(), timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            seconds = int(timeout or 0)
            return 124, "\n".join(lines), f"download timed out after {seconds} s"
        return proc.returncode or 0, "\n".join(lines), stderr.decode(errors="replace")

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

    async def title(self, video_id: str) -> str | None:
        """The video's title without downloading anything (one metadata request)."""
        args = ["--quiet", "--no-warnings", "--no-playlist", "--skip-download", "--print", "title", video_id]
        code, out, _err = await self.run(args)
        if code != 0:
            return None
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        return lines[-1] if lines else None

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
        playlist: bool = False,
        limit: int | None = None,
        progress: ProgressHook | None = None,
        timeout: float | None = None,
        rate_limit: str | None = None,
    ) -> tuple[list[str], str]:
        """Save a real media file and return its path; see download() for titles."""
        files, err = await self.download(
            url,
            fmt,
            directory,
            playlist=playlist,
            limit=limit,
            progress=progress,
            timeout=timeout,
            rate_limit=rate_limit,
        )
        return [item.path for item in files], err

    async def download(
        self,
        url: str,
        fmt: str,
        directory: str,
        *,
        playlist: bool = False,
        limit: int | None = None,
        progress: ProgressHook | None = None,
        timeout: float | None = None,
        rate_limit: str | None = None,
    ) -> tuple[list[Download], str]:
        """Save a real media file and return its path and title.

        mpv cannot open the separate, client-bound streams YouTube serves.
        yt-dlp downloads them, merges picture and sound into one MKV, and mpv
        plays that file. `progress` receives 0-100 while the download runs.
        """
        folder = Path(directory).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        options: dict[str, Any] = {
            "playlist": playlist,
            "limit": limit,
            "progress": progress,
            "timeout": timeout,
            "rate_limit": rate_limit,
        }
        files, err = await self._download_once(url, fmt, folder, **options)
        if files:
            return files, ""
        if fmt != "18/b" and _download_should_retry(err):
            log.info("download retry with format 18 reason=%s", err.strip()[:160])
            files, retry_err = await self._download_once(url, "18/b", folder, **options)
            if files:
                return files, ""
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
        progress: ProgressHook | None,
        timeout: float | None,
        rate_limit: str | None,
    ) -> tuple[list[Download], str]:
        args = [
            "-f",
            fmt,
            "--merge-output-format",
            "mkv",
            "--force-overwrites",
            "--no-warnings",
            "--newline",
            "--progress",
            "-o",
            str(folder / "%(id)s.%(ext)s"),
            # Once the file is in place yt-dlp prints its path, then the title
            # as a JSON string on the next line (the file is named by id).
            "--print",
            "after_move:filepath",
            "--print",
            f"after_move:{TITLE_PREFIX}%(title)j",
        ]
        if rate_limit:
            args.extend(["--limit-rate", rate_limit])
        if playlist:
            args.append("--yes-playlist")
            if limit:
                args.extend(["--playlist-end", str(limit)])
        else:
            args.append("--no-playlist")
        args.append(url)

        def on_line(line: str) -> None:
            percent = parse_progress(line)
            if percent is not None and progress is not None:
                progress(percent)

        code, out, err = await self.run_streaming(args, on_line=on_line, timeout=timeout)
        files = parse_downloads(out)
        if code != 0 or not files:
            detail = (err or _last_error_line(out) or "yt-dlp did not write a file").strip()
            return [], detail
        if progress is not None:
            progress(100.0)
        return files, ""


TITLE_PREFIX = "title:"


def parse_downloads(out: str) -> list[Download]:
    """Pair each printed file path with the title line that follows it."""
    files: list[Download] = []
    expect_title = False
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(TITLE_PREFIX):
            if expect_title:
                expect_title = False
                try:
                    title = json.loads(line[len(TITLE_PREFIX) :])
                except json.JSONDecodeError:
                    continue
                if isinstance(title, str) and title.strip():
                    files[-1] = Download(files[-1].path, title.strip())
            continue
        if Path(line).is_file():
            files.append(Download(line))
            expect_title = True
    return files


def _last_error_line(out: str) -> str:
    for line in reversed(out.splitlines()):
        if line.startswith("ERROR"):
            return line
    return ""


def _download_should_retry(err: str) -> bool:
    lowered = err.lower()
    if "timed out after" in lowered:
        return False
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
