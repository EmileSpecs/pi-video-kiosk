"""Download approved videos and evict them by least-recently-used size."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any

from pikiosk.cache.ytdlp import YtDlp
from pikiosk.catalog import CatalogError, parse_ref
from pikiosk.clock import Clock
from pikiosk.config import KioskConfig, atomic_write_text

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


ProgressHook = Callable[[float], None]


class CacheManager:
    """Every YouTube play goes through here: hit → path, miss → download → path.

    Downloads for the same video id are shared, so a tap while the background
    pre-cache is already fetching that video simply waits for it.
    """

    def __init__(self, config: KioskConfig, ytdlp: YtDlp, clock: Clock) -> None:
        self.config = config
        self.ytdlp = ytdlp
        self.clock = clock
        self.directory = Path(config.offline.directory).expanduser()
        self.index_path = self.directory / "index.json"
        self.entries: dict[str, CacheEntry] = {}
        self.streaming = False
        self.progress: dict[str, float] = {}
        self.protected: set[str] = set()
        self.pending_titles: dict[str, str] = {}  # learned from playlist scans before the download
        self._inflight: dict[str, asyncio.Task[tuple[str | None, str]]] = {}
        self._watchers: dict[str, list[ProgressHook]] = {}
        self.load()

    def update_config(self, config: KioskConfig) -> None:
        self.config = config
        directory = Path(config.offline.directory).expanduser()
        if directory != self.directory:
            self.directory = directory
            self.index_path = directory / "index.json"
            self.entries = {}
            self.load()

    def downloading(self) -> dict[str, float]:
        return {video_id: self.progress.get(video_id, 0.0) for video_id in self._inflight}

    async def ensure(
        self,
        video_id: str,
        url: str | None = None,
        *,
        progress: ProgressHook | None = None,
    ) -> tuple[str | None, str]:
        """Return a local path for the video, downloading it when needed."""
        cached = self.get(video_id)
        if cached:
            if progress is not None:
                progress(100.0)
            return cached, ""
        if progress is not None:
            self._watchers.setdefault(video_id, []).append(progress)
            progress(self.progress.get(video_id, 0.0))
        task = self._inflight.get(video_id)
        if task is None:
            task = asyncio.get_running_loop().create_task(self._fetch(video_id, url))
            self._inflight[video_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            if progress is not None:
                watchers = self._watchers.get(video_id, [])
                if progress in watchers:
                    watchers.remove(progress)

    async def _fetch(self, video_id: str, url: str | None) -> tuple[str | None, str]:
        self.progress[video_id] = 0.0
        target = url or f"https://www.youtube.com/watch?v={video_id}"
        title: str | None = None
        try:
            options: dict[str, Any] = {
                "playlist": False,
                "limit": None,
                "progress": lambda percent: self._report(video_id, percent),
                "timeout": self.config.playback.download_timeout_s,
                "rate_limit": self.config.offline.rate_limit,
            }
            fmt = self.config.playback.ytdl_format
            download = getattr(self.ytdlp, "download", None)
            if download is not None:
                files, err = await download(target, fmt, str(self.directory), **options)
                paths = [item.path for item in files]
                title = files[0].title if files else None
            else:
                paths, err = await self.ytdlp.download_media(target, fmt, str(self.directory), **options)
        except Exception as exc:  # a crashed subprocess must not wedge playback
            log.warning("download crashed id=%s error=%s", video_id, exc)
            paths, err = [], str(exc)
        finally:
            self._inflight.pop(video_id, None)
        if not paths:
            self.progress.pop(video_id, None)
            self._watchers.pop(video_id, None)
            log.warning("download failed id=%s detail=%s", video_id, err.strip()[:200])
            return None, err
        self.register(video_id, paths[0], title=title)
        self._report(video_id, 100.0)
        self._watchers.pop(video_id, None)
        return paths[0], ""

    def _report(self, video_id: str, percent: float) -> None:
        self.progress[video_id] = percent
        for watcher in list(self._watchers.get(video_id, [])):
            try:
                watcher(percent)
            except Exception:
                log.debug("progress watcher failed", exc_info=True)

    def register(self, video_id: str, path: str, *, title: str | None = None) -> None:
        """Record a file yt-dlp just wrote and keep the cache under its size limit."""
        file = Path(path)
        try:
            size = file.stat().st_size
        except OSError:
            size = 0
        previous = self.entries.get(video_id)
        known = self.pending_titles.pop(video_id, None)
        self.entries[video_id] = CacheEntry(
            video_id=video_id,
            path=str(file),
            title=title or known or (previous.title if previous else video_id),
            duration=previous.duration if previous else None,
            thumbnail=previous.thumbnail if previous else None,
            size=size,
            last_played=previous.last_played if previous else 0,
            downloaded_at=time.time(),
        )
        self._evict(self.protected | {video_id})
        try:
            self.save()
        except OSError:
            log.warning("could not write cache index", exc_info=True)

    def title_for(self, video_id: str) -> str | None:
        """The video's YouTube title, or None when all we have is its id."""
        entry = self.entries.get(video_id)
        if entry is not None and entry.title and entry.title != video_id:
            return entry.title
        return self.pending_titles.get(video_id)

    def note_titles(self, entries: list[dict[str, Any]]) -> None:
        """Remember titles a playlist scan returned; they are free and cover the cache."""
        changed = False
        for item in entries:
            video_id = str(item.get("id") or "")
            title = str(item.get("title") or "").strip()
            if not video_id or not title or title == video_id:
                continue
            entry = self.entries.get(video_id)
            if entry is None:
                self.pending_titles[video_id] = title
            elif entry.title != title and entry.title in {"", video_id}:
                self.entries[video_id] = _with_title(entry, title)
                changed = True
        if changed:
            self._save_quietly()

    async def fetch_title(self, video_id: str) -> str | None:
        """Ask YouTube for the title of a video cached before titles were kept."""
        known = self.title_for(video_id)
        if known:
            return known
        lookup = getattr(self.ytdlp, "title", None)
        if lookup is None:
            return None
        try:
            title = await lookup(video_id)
        except Exception as exc:
            log.debug("title lookup failed id=%s error=%s", video_id, exc)
            return None
        if not title or title == video_id:
            return None
        entry = self.entries.get(video_id)
        if entry is not None:
            self.entries[video_id] = _with_title(entry, title)
            self._save_quietly()
        else:
            self.pending_titles[video_id] = title
        return str(title)

    async def _backfill_titles(self, video_ids: list[str]) -> None:
        for video_id in video_ids:
            if self.streaming:
                return
            if video_id in self.entries and self.title_for(video_id) is None:
                await self.fetch_title(video_id)

    def _save_quietly(self) -> None:
        try:
            self.save()
        except OSError:
            log.warning("could not write cache index", exc_info=True)

    @staticmethod
    def catalog_video_ids(items: list[dict[str, Any]]) -> list[str]:
        """Video ids named directly by catalog items (playlists need a network scan)."""
        found: list[str] = []
        for item in items:
            if item.get("type") != "video":
                continue
            video_id = str(item.get("id") or "")
            if not video_id and item.get("url"):
                try:
                    ref = parse_ref(str(item["url"]))
                except CatalogError:
                    continue
                video_id = ref.id or "" if ref.kind == "video" else ""
            if video_id and video_id not in found:
                found.append(video_id)
        return found

    async def precache(self, items: list[dict[str, Any]]) -> None:
        """Fetch catalog videos in the background so the first tap plays at once.

        Direct video items ignore the download window; playlist items are only
        expanded inside it because that scan costs a request per playlist.
        """
        wanted = self.catalog_video_ids(items)
        self.protected = set(wanted)
        if self.in_window():
            for item in items:
                if item.get("type") == "playlist" and item.get("id"):
                    entries = await self.ytdlp.flat_playlist(str(item["id"]), int(item.get("max_items") or 50))
                    self.note_titles(entries)
                    for entry in entries:
                        video_id = str(entry.get("id") or "")
                        if video_id and video_id not in wanted:
                            wanted.append(video_id)
        for video_id in wanted:
            if self.get(video_id):
                continue
            path, err = await self.ensure(video_id)
            if path:
                log.info("pre-cached id=%s", video_id)
            else:
                log.info("pre-cache skipped id=%s reason=%s", video_id, err.strip()[:120])
        await self._backfill_titles(wanted)

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
        atomic_write_text(self.index_path, json.dumps(payload, indent=2))

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

    async def sync(self, items: list[dict[str, Any]], *, force: bool = False) -> None:
        """Scheduled full sync: expand playlists, fetch everything, evict and forget.

        `force` is the admin "Sync now" button; it ignores the download window.
        """
        if not self.config.offline.enabled and not force:
            return
        if self.streaming:
            log.info("sync paused because a stream is playing")
            return
        if not force and not self.in_window():
            log.info("sync outside download window")
            return
        wanted = self.catalog_video_ids(items)
        for item in items:
            if item.get("type") == "playlist" and item.get("id"):
                entries = await self.ytdlp.flat_playlist(
                    str(item["id"]),
                    int(item.get("max_items") or 50),
                )
                self.note_titles(entries)
                for entry in entries:
                    video_id = str(entry.get("id") or "")
                    if video_id and video_id not in wanted:
                        wanted.append(video_id)
        self.protected = set(wanted)
        for video_id in wanted:
            if self.streaming:
                return
            if self.get(video_id):
                continue
            await self.ensure(video_id)
        await self._backfill_titles(wanted)
        self._evict(set(wanted))
        self._forget_removed(set(wanted))
        self.save()

    def forget(self, video_id: str) -> None:
        """Delete a file that turned out to be unplayable."""
        if video_id in self.entries:
            log.warning("dropping unplayable cache entry id=%s", video_id)
            self._delete(video_id)
            try:
                self.save()
            except OSError:
                log.warning("could not write cache index", exc_info=True)

    def mark_played(self, video_id: str) -> None:
        entry = self.entries.get(video_id)
        if entry:
            self.entries[video_id] = dataclasses.replace(entry, last_played=time.time())

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


def _with_title(entry: CacheEntry, title: str) -> CacheEntry:
    return dataclasses.replace(entry, title=title)


def _parse_hhmm(value: str) -> dtime:
    hour, minute = value.strip().split(":")
    return dtime(int(hour), int(minute))
