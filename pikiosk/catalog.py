"""Approved-item catalog and reference parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
PLAYLIST_ID_RE = re.compile(r"^(PL|UU|LL|FL|OL|RD)[A-Za-z0-9_-]+$")
_TIME_RE = re.compile(
    r"^(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?$",
    re.IGNORECASE,
)
_TRACKING = {"si", "pp", "feature"}
Kind = Literal["video", "playlist", "file", "catalog"]


class CatalogError(ValueError):
    """A reference could not be parsed or is not allowed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Ref:
    kind: Kind
    id: str | None = None
    start: float | None = None
    name: str | None = None
    path: str | None = None
    host: str | None = None


@dataclass(frozen=True)
class Playable:
    source: Literal["catalog", "adhoc", "file"]
    catalog_name: str | None
    video_id: str | None
    playlist_id: str | None
    title: str
    start: float | None
    path: str | None
    url: str | None
    shuffle: bool = False
    max_items: int | None = None
    thumbnail: str | None = None


def parse_time(value: str) -> float:
    text = value.strip().lower()
    if text.endswith("s") and text[:-1].isdigit():
        return float(text[:-1])
    if text.isdigit():
        return float(text)
    match = _TIME_RE.match(text)
    if not match or not any(match.groups()):
        raise CatalogError("bad_time", f"unrecognised start time {value!r}")
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m") or 0)
    seconds = int(match.group("s") or 0)
    return float(hours * 3600 + minutes * 60 + seconds)


def _start_from_query(query: dict[str, list[str]]) -> float | None:
    raw = None
    for key in ("t", "start"):
        if query.get(key):
            raw = query[key][0]
            break
    if raw is None:
        return None
    return parse_time(raw)


def _reject_search(text: str) -> None:
    lowered = text.strip().lower()
    if lowered.startswith("ytsearch:") or lowered.startswith("ytdl://") or "ytsearch" in lowered:
        raise CatalogError("not_allowed", "search queries are not allowed")


def parse_ref(text: str, names: set[str] | None = None) -> Ref:
    """Parse a video id, URL, file path, or catalog name. Does not apply host policy."""
    raw = text.strip()
    if not raw:
        raise CatalogError("empty", "empty reference")
    _reject_search(raw)
    if raw in (names or set()):
        return Ref(kind="catalog", name=raw)
    if raw.lower().startswith("file:"):
        path = raw[5:]
        if path.startswith("//"):
            path = path[1:]
        return Ref(kind="file", path=path)
    if "://" in raw or raw.startswith("www."):
        return _parse_url(raw if "://" in raw else f"https://{raw}")
    if PLAYLIST_ID_RE.match(raw):
        return Ref(kind="playlist", id=raw)
    if VIDEO_ID_RE.match(raw):
        return Ref(kind="video", id=raw)
    raise CatalogError("unrecognised", f"unrecognised reference {raw!r}")


def _parse_url(raw: str) -> Ref:
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host in {"", "localhost"} or parsed.scheme not in {"http", "https"}:
        raise CatalogError("not_allowed", f"host {host or parsed.scheme!r} is not allowed")
    query = parse_qs(parsed.query)
    start = _start_from_query(query)
    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts and path_parts[0] in {"shorts", "embed", "live"} and len(path_parts) >= 2:
        video_id = path_parts[1]
        if not VIDEO_ID_RE.match(video_id):
            raise CatalogError("bad_id", f"bad video id {video_id!r}")
        return Ref(kind="video", id=video_id, start=start, host=host)
    playlist = query.get("list", [None])[0]
    video = query.get("v", [None])[0]
    if path_parts and host.endswith("youtu.be"):
        video = path_parts[0]
        playlist = None
        host = "youtu.be"
    if playlist and not video and (not path_parts or path_parts[0] == "playlist"):
        if not PLAYLIST_ID_RE.match(playlist):
            raise CatalogError("bad_id", f"bad playlist id {playlist!r}")
        return Ref(kind="playlist", id=playlist, start=start, host=host)
    if video:
        if not VIDEO_ID_RE.match(video):
            raise CatalogError("bad_id", f"bad video id {video!r}")
        # list= on a watch URL is tracking or a mix; never treat it as the playlist.
        return Ref(kind="video", id=video, start=start, host=host)
    if playlist and PLAYLIST_ID_RE.match(playlist):
        return Ref(kind="playlist", id=playlist, start=start, host=host)
    raise CatalogError("unrecognised", f"unrecognised url {raw!r}")


def canonical_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def thumbnail_url(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class Catalog:
    def __init__(
        self,
        items: list[dict[str, object]],
        *,
        allowed_hosts: list[str],
        allow_unlisted: bool,
        file_roots: list[Path],
    ) -> None:
        self.items = items
        self.allowed_hosts = {host.lower() for host in allowed_hosts}
        self.allow_unlisted = allow_unlisted
        self.file_roots = [root.expanduser().resolve() for root in file_roots]
        self.by_name = {str(item["name"]): item for item in items}

    def names(self) -> list[str]:
        return [str(item["name"]) for item in self.items]

    def parse(self, text: str) -> Ref:
        try:
            ref = parse_ref(text, set(self.by_name))
        except CatalogError:
            raise
        if ref.kind == "file":
            self._check_file(ref.path or "")
            return ref
        if ref.host and ref.host not in self.allowed_hosts:
            raise CatalogError("not_allowed", f"host {ref.host!r} is not allowed")
        return ref

    def _check_file(self, path_text: str) -> Path:
        path = Path(path_text).expanduser().resolve()
        if not any(_is_relative_to(path, root) for root in self.file_roots):
            raise CatalogError("not_allowed", "file is outside the allowed directories")
        return path

    def resolve(self, text: str) -> Playable:
        ref = self.parse(text)
        if ref.kind == "catalog":
            return self._from_item(self.by_name[ref.name or ""], adhoc=False)
        if ref.kind == "file":
            path = self._check_file(ref.path or "")
            return Playable(
                source="file",
                catalog_name=None,
                video_id=None,
                playlist_id=None,
                title=path.name,
                start=ref.start,
                path=str(path),
                url=None,
            )
        if not self.allow_unlisted:
            raise CatalogError("not_allowed", "not_allowed")
        if ref.kind == "video":
            video_id = ref.id or ""
            return Playable(
                source="adhoc",
                catalog_name=None,
                video_id=video_id,
                playlist_id=None,
                title=video_id,
                start=ref.start,
                path=None,
                url=canonical_watch_url(video_id),
            )
        playlist_id = ref.id or ""
        return Playable(
            source="adhoc",
            catalog_name=None,
            video_id=None,
            playlist_id=playlist_id,
            title=playlist_id,
            start=ref.start,
            path=None,
            url=f"https://www.youtube.com/playlist?list={playlist_id}",
        )

    def _from_item(self, item: dict[str, object], *, adhoc: bool) -> Playable:
        name = str(item["name"])
        kind = str(item["type"])
        start = item.get("start")
        start_f = float(start) if isinstance(start, (int, float)) else None
        shuffle = bool(item.get("shuffle", False))
        max_items = item.get("max_items")
        max_i = int(max_items) if isinstance(max_items, int) else None
        thumb = item.get("thumbnail")
        thumbnail = str(thumb) if isinstance(thumb, str) else None
        if kind == "file":
            path = self._check_file(str(item.get("path") or ""))
            return Playable(
                source="catalog",
                catalog_name=name,
                video_id=None,
                playlist_id=None,
                title=name,
                start=start_f,
                path=str(path),
                url=None,
                shuffle=shuffle,
                max_items=max_i,
                thumbnail=thumbnail,
            )
        if item.get("url"):
            ref = parse_ref(str(item["url"]))
            start_f = start_f if start_f is not None else ref.start
        else:
            ref = Ref(
                kind="playlist" if kind == "playlist" else "video",
                id=str(item.get("id") or "") or None,
            )
        if kind == "playlist":
            playlist_id = ref.id
            return Playable(
                source="adhoc" if adhoc else "catalog",
                catalog_name=name,
                video_id=None,
                playlist_id=playlist_id,
                title=name,
                start=start_f,
                path=None,
                url=f"https://www.youtube.com/playlist?list={playlist_id}",
                shuffle=shuffle,
                max_items=max_i,
                thumbnail=thumbnail,
            )
        video_id = ref.id
        return Playable(
            source="adhoc" if adhoc else "catalog",
            catalog_name=name,
            video_id=video_id,
            playlist_id=None,
            title=name,
            start=start_f,
            path=None,
            url=canonical_watch_url(video_id or ""),
            shuffle=shuffle,
            max_items=max_i,
            thumbnail=thumbnail or (thumbnail_url(video_id) if video_id else None),
        )

    def item_thumbnail(self, item: dict[str, object]) -> str:
        if item.get("thumbnail"):
            return str(item["thumbnail"])
        if item.get("type") == "file":
            return ""
        if item.get("id") and item.get("type") == "video":
            return thumbnail_url(str(item["id"]))
        if item.get("url"):
            try:
                ref = parse_ref(str(item["url"]))
            except CatalogError:
                return ""
            if ref.id and ref.kind == "video":
                return thumbnail_url(ref.id)
        return ""
