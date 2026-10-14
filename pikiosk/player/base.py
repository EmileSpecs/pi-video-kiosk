"""Player backend protocol and shared events."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

PlayerListener = Callable[[dict[str, Any]], None]


class PlayerBackend(Protocol):
    connected: bool

    def add_listener(self, listener: PlayerListener) -> None: ...

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
    ) -> None: ...

    async def stop(self) -> None: ...

    async def pause(self) -> None: ...

    async def resume(self) -> None: ...

    async def set_volume(self, volume: int) -> None: ...

    async def set_mute(self, muted: bool) -> None: ...

    async def seek(self, position: float) -> None: ...

    async def playlist_next(self) -> None: ...

    async def playlist_prev(self) -> None: ...

    async def set_loop_file(self, enabled: bool) -> None: ...

    async def set_loop_playlist(self, enabled: bool) -> None: ...

    async def set_property(self, name: str, value: Any) -> None: ...
