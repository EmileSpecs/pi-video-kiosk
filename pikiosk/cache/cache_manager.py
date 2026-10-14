"""Download approved videos and evict them by least-recently-used size."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any

from pikiosk.cache.ytdlp import YtDlp
from pikiosk.clock import Clock
from pikiosk.config import KioskConfig

log = logging.getLogger("pikiosk.cache")


@dataclass
class CacheEntry:
    video_id: str
    path: str
    title: str
    duration: float | None
    thumbnail: str | None
    size: int
    last_played: float
    downloaded_at: float


class CacheManager:
    def __init__(self, config: KioskConfig, ytdlp: YtDlp, clock: Clock) -> None:
        self.config = config
        self.ytdlp = ytdlp
        self.clock = clock
        self.directory = Path(config.offline.directory).expanduser()
        self.index_path = self.directory / "index.json"
        self.entries: dict[str, CacheEntry] = {}
        self.streaming = False
        self.progress: dict[str, float] = {}
        self.load()

    def load(self) -> None:
        if not self.index_path.exists():
            return
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        for video_id, item in raw.items():
            if isinstance(item, dict):
                self.entries[video_id] = CacheEntry(
                    video_id=video_id,
                    path=str(item.get("path") or ""),
                    title=str(item.get("title") or video_id),
                    duration=item.get("duration") if isinstance(item.get("duration"), (int, float)) else None,
                    thumbnail=item.get("thumbnail") if isinstance(item.get("thumbnail"), str) else None,
                    size=int(item.get("size") or 0),
                    last_played=float(item.get("last_played") or 0),
                    downloaded_at=float(item.get("downloaded_at") or 0),
                )

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            entry.video_id: {
                "path": entry.path,
                "title": entry.title,
                "duration": entry.duration,
                "thumbnail": entry.thumbnail,
                "size": entry.size,
                "last_played": entry.last_played,
                "downloaded_at": entry.downloaded_at,
            }
            for entry in self.entries.values()
        }
        text = json.dumps(payload, indent=2)
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.index_path)

    def get(self, video_id: str) -> str | None:
        entry = self.entries.get(video_id)
        if entry is None:
            return None
        path = Path(entry.path)
        if not path.exists() or path.suffix == ".part":
            return None
        return str(path)

    def used_bytes(self) -> int:
        return sum(entry.size for entry in self.entries.values())

    def in_window(self, moment: datetime | None = None) -> bool:
        window = self.config.offline.download_window
        if not window or "-" not in window:
            return True
        start_text, end_text = window.split("-", 1)
        start = _parse_hhmm(start_text)
        end = _parse_hhmm(end_text)
        current = (moment or self.clock.now()).time()
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end

    async def sync(self, items: list[dict[str, Any]]) -> None:
        if not self.config.offline.enabled:
            return
        if self.streaming:
            log.info("sync paused because a stream is playing")
            return
        if not self.in_window():
            log.info("sync outside download window")
            return
        wanted: list[str] = []
        for item in items:
            if item.get("type") == "playlist" and item.get("id"):
                entries = await self.ytdlp.flat_playlist(
                    str(item["id"]),
                    int(item.get("max_items") or 50),
                )
                for entry in entries:
                    video_id = str(entry.get("id") or "")
                    if video_id:
                        wanted.append(video_id)
            elif item.get("type") == "video":
                video_id = str(item.get("id") or "")
                if not video_id and item.get("url"):
                    video_id = str(item["url"]).rstrip("/").split("/")[-1].split("?")[0]
                if video_id:
                    wanted.append(video_id)
        for video_id in wanted:
            if self.streaming:
                return
            if self.get(video_id):
                continue
            await self._download(video_id)
        self._evict(set(wanted))
        self._forget_removed(set(wanted))
        self.save()

    async def _download(self, video_id: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        output = self.directory / f"{video_id}.mkv"
        self.progress[video_id] = 0.0
        args = [
            "-f",
            self.config.playback.ytdl_format,
            "--merge-output-format",
            "mkv",
            "-o",
            str(output),
            "--no-playlist",
            video_id,
        ]
        if self.config.offline.rate_limit:
            args.extend(["--limit-rate", self.config.offline.rate_limit])
        code, _out, err = await self.ytdlp.run(args)
        self.progress[video_id] = 1.0 if code == 0 else 0.0
        if code != 0 or not output.exists():
            log.warning("download failed id=%s detail=%s", video_id, err.strip()[:200])
            return
        self.entries[video_id] = CacheEntry(
            video_id=video_id,
            path=str(output),
            title=video_id,
            duration=None,
            thumbnail=None,
            size=output.stat().st_size,
            last_played=0,
            downloaded_at=time.time(),
        )

    def mark_played(self, video_id: str) -> None:
        entry = self.entries.get(video_id)
        if entry:
            self.entries[video_id] = CacheEntry(
                video_id=entry.video_id,
                path=entry.path,
                title=entry.title,
                duration=entry.duration,
                thumbnail=entry.thumbnail,
                size=entry.size,
                last_played=time.time(),
                downloaded_at=entry.downloaded_at,
            )

    def _evict(self, protected: set[str]) -> None:
        limit = int(self.config.offline.max_size_gb * 1024 * 1024 * 1024)
        if self.used_bytes() <= limit:
            return
        ordered = sorted(self.entries.values(), key=lambda entry: (entry.last_played, entry.downloaded_at))
        for entry in ordered:
            if self.used_bytes() <= limit:
                break
            if entry.video_id in protected:
                continue
            self._delete(entry.video_id)
        if self.used_bytes() > limit:
            log.warning("cache still over size after sparing catalog items")
            for entry in ordered:
                if self.used_bytes() <= limit:
                    break
                self._delete(entry.video_id)

    def _forget_removed(self, wanted: set[str]) -> None:
        cutoff = time.time() - self.config.offline.delete_after_days * 86400
        for video_id, entry in list(self.entries.items()):
            if video_id not in wanted and entry.downloaded_at < cutoff:
                self._delete(video_id)

    def _delete(self, video_id: str) -> None:
        entry = self.entries.pop(video_id, None)
        if entry is None:
            return
        path = Path(entry.path)
        if path.exists():
            path.unlink()


def _parse_hhmm(value: str) -> dtime:
    hour, minute = value.strip().split(":")
    return dtime(int(hour), int(minute))
