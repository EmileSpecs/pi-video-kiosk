"""PlayerBackend on top of the mpv IPC client."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pikiosk.player.base import PlayerListener
from pikiosk.player.mpv_ipc import MpvClient, MpvError

log = logging.getLogger("pikiosk.player")
OBSERVED = ("media-title", "duration", "time-pos", "pause", "playlist-pos", "playlist-count", "path")


class MpvPlayer:
    def __init__(
        self,
        socket_path: str,
        *,
        ytdl_format: str,
        audio_device: str = "auto",
        on_restart: Any = None,
    ) -> None:
        self.socket_path = socket_path
        self.ytdl_format = ytdl_format
        self.audio_device = audio_device
        self.on_restart = on_restart
        self._listeners: list[PlayerListener] = []
        self._client: MpvClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.volume = 40
        self.muted = False
        self.loop_file = False
        self.loop_playlist = False
        self.connected = False
        self._restart_lock = asyncio.Lock()
        self._last_restart: float | None = None
        self._down_reason: str | None = None

    def add_listener(self, listener: PlayerListener) -> None:
        self._listeners.append(listener)

    def _emit(self, event: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            listener(event)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop_client(self) -> None:
        self._stop.set()
        if self._client:
            await self._client.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        delay = 0.5
        while not self._stop.is_set():
            client = MpvClient(
                self.socket_path,
                on_event=self._emit,
                on_disconnect=self._mark_down,
            )
            try:
                await client.connect()
            except (OSError, MpvError) as exc:
                log.warning("mpv connect failed socket=%s error=%s", self.socket_path, exc)
                self.connected = False
                self._emit({"event": "pikiosk-disconnected", "error": f"{self.socket_path}: {exc}"})
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10)
                continue
            self._client = client
            self.connected = True
            delay = 0.5
            self._emit({"event": "pikiosk-connected"})
            try:
                try:
                    await client.command("request_log_messages", "warn")
                except (OSError, MpvError):
                    pass
                try:
                    await self._reapply()
                except (OSError, MpvError) as exc:
                    log.warning("mpv reapply failed error=%s", exc)
                while client.connected and not self._stop.is_set():
                    await asyncio.sleep(0.2)
                    if client.consecutive_timeouts >= 3:
                        await self._request_restart()
                        break
            except (OSError, MpvError) as exc:
                log.warning("mpv session ended error=%s", exc)
                self._down_reason = str(exc)
            finally:
                await client.close()
                self.connected = False

    def _mark_down(self) -> None:
        self.connected = False
        event: dict[str, Any] = {"event": "pikiosk-disconnected"}
        if self._down_reason:
            event["error"] = self._down_reason
            self._down_reason = None
        self._emit(event)

    async def _reapply(self) -> None:
        await self.set_volume(self.volume)
        await self.set_mute(self.muted)
        await self.set_loop_file(self.loop_file)
        if self.audio_device and self.audio_device != "auto":
            await self.set_property("audio-device", self.audio_device)
        # Without these mpv never pushes property-change events, so Home
        # Assistant would show title, duration and position as unknown.
        for index, name in enumerate(OBSERVED, start=1):
            await self._cmd("observe_property", index, name)

    async def _request_restart(self) -> None:
        loop = asyncio.get_running_loop()
        # loop.time() is seconds since boot on Linux, so a plain 0.0 default
        # would swallow the first restart for a minute after power-on.
        if self._last_restart is not None and loop.time() - self._last_restart < 60:
            return
        self._last_restart = loop.time()
        log.warning("mpv hang detected; requesting restart")
        if self.on_restart:
            result = self.on_restart()
            if asyncio.iscoroutine(result):
                await result

    async def _cmd(self, *parts: Any) -> Any:
        if self._client is None or not self._client.connected:
            raise MpvError("mpv offline")
        return await self._client.command(*parts)

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
        target = path or url or ""
        mode = "append" if append else "replace"
        del headers
        if url and not path:
            # Only reached without a downloader (--simulate); mpv's own hook
            # takes the page URL and whatever yt-dlp it finds on PATH.
            await self.set_property("ytdl", "yes")
            await self.set_property("ytdl-format", self.ytdl_format)
            if playlist:
                items = f"1-{max_items}" if max_items else "1-50"
                await self.set_property("ytdl-raw-options", f"yes-playlist=,playlist-items={items}")
        if start:
            await self._cmd("loadfile", target, mode, -1, f"start={start}")
        else:
            await self._cmd("loadfile", target, mode)
        if audio_url:
            try:
                await self._cmd("audio-add", audio_url, "select")
            except MpvError as exc:
                log.warning("audio-add failed error=%s", exc)
        if shuffle:
            await self._cmd("playlist-shuffle")

    async def _apply_headers(self, headers: dict[str, str] | None) -> None:
        if not headers:
            return
        user_agent = headers.get("User-Agent")
        if user_agent:
            await self.set_property("user-agent", user_agent)
        fields = [f"{name}: {value}" for name, value in headers.items() if name != "User-Agent"]
        if fields:
            await self.set_property("http-header-fields", fields)

    async def stop(self) -> None:
        await self._cmd("stop")

    async def pause(self) -> None:
        await self.set_property("pause", True)

    async def resume(self) -> None:
        await self.set_property("pause", False)

    async def set_volume(self, volume: int) -> None:
        self.volume = volume
        await self.set_property("volume", volume)

    async def set_mute(self, muted: bool) -> None:
        self.muted = muted
        await self.set_property("mute", muted)

    async def seek(self, position: float) -> None:
        await self._cmd("seek", position, "absolute")

    async def playlist_next(self) -> None:
        await self._cmd("playlist-next")

    async def playlist_prev(self) -> None:
        await self._cmd("playlist-prev")

    async def set_loop_file(self, enabled: bool) -> None:
        self.loop_file = enabled
        await self.set_property("loop-file", "inf" if enabled else "no")

    async def set_loop_playlist(self, enabled: bool) -> None:
        self.loop_playlist = enabled
        await self.set_property("loop-playlist", "inf" if enabled else "no")

    async def set_property(self, name: str, value: Any) -> None:
        await self._cmd("set_property", name, value)
