"""In-memory player used by controller tests and --simulate without a socket."""

from __future__ import annotations

from typing import Any

from pikiosk.player.base import PlayerListener


class MemoryPlayer:
    def __init__(self) -> None:
        self.connected = True
        self.loads: list[dict[str, Any]] = []
        self.volume = 40
        self.muted = False
        self.loop_file = False
        self.loop_playlist = False
        self.stopped = 0
        self.paused = False
        self.seek_to: float | None = None
        self.next_count = 0
        self.prev_count = 0
        self.properties: dict[str, Any] = {}
        self._listeners: list[PlayerListener] = []
        self.fail_next = False
        self.failures_left = 0
        self.playlist_count = 1
        self.playlist_index = 0

    def add_listener(self, listener: PlayerListener) -> None:
        self._listeners.append(listener)

    def emit(self, event: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            listener(event)

    async def load(
        self,
        url: str | None,
        path: str | None,
        *,
        start: float | None = None,
        playlist: bool = False,
        max_items: int | None = None,
        shuffle: bool = False,
        audio_url: str | None = None,
        append: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        del audio_url, append
        target = path or url or ""
        self.loads.append(
            {
                "target": target,
                "start": start,
                "playlist": playlist,
                "max_items": max_items,
                "shuffle": shuffle,
                "headers": headers,
            }
        )
        if self.fail_next or self.failures_left:
            self.fail_next = False
            if self.failures_left:
                self.failures_left -= 1
            self.emit({"event": "end-file", "reason": "error", "file_error": "loading failed"})
            return
        self.playlist_count = 3 if playlist else 1
        self.playlist_index = 0
        self.paused = False
        self.emit({"event": "file-loaded", "path": target})
        self.emit(
            {
                "event": "property-change",
                "name": "media-title",
                "data": target.rsplit("/", 1)[-1],
            }
        )
        self.emit({"event": "property-change", "name": "duration", "data": 136.5})
        self.emit({"event": "property-change", "name": "playlist-count", "data": self.playlist_count})
        self.emit({"event": "property-change", "name": "playlist-pos", "data": 0})
        self.emit({"event": "property-change", "name": "path", "data": target})

    async def stop(self) -> None:
        self.stopped += 1
        self.emit({"event": "end-file", "reason": "stop"})

    async def pause(self) -> None:
        self.paused = True

    async def resume(self) -> None:
        self.paused = False

    async def set_volume(self, volume: int) -> None:
        self.volume = volume

    async def set_mute(self, muted: bool) -> None:
        self.muted = muted

    async def seek(self, position: float) -> None:
        self.seek_to = position

    async def playlist_next(self) -> None:
        self.next_count += 1
        self.playlist_index += 1
        if self.playlist_index >= self.playlist_count:
            self.emit({"event": "end-file", "reason": "eof"})
        else:
            self.emit({"event": "property-change", "name": "playlist-pos", "data": self.playlist_index})

    async def playlist_prev(self) -> None:
        self.prev_count += 1

    async def set_loop_file(self, enabled: bool) -> None:
        self.loop_file = enabled

    async def set_loop_playlist(self, enabled: bool) -> None:
        self.loop_playlist = enabled

    async def set_property(self, name: str, value: Any) -> None:
        self.properties[name] = value

    async def simulate_eof(self) -> None:
        self.emit({"event": "end-file", "reason": "eof"})

    async def simulate_error(self, message: str = "loading failed") -> None:
        self.emit({"event": "end-file", "reason": "error", "file_error": message})
