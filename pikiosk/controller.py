"""Turns commands into player and screen actions and owns KioskState."""

from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from pikiosk.cache.ytdlp import YtDlp, classify_error
from pikiosk.catalog import Catalog, CatalogError, Playable, thumbnail_url
from pikiosk.clock import Clock
from pikiosk.config import KioskConfig
from pikiosk.events import Event, EventBus
from pikiosk.persistence import StateStore
from pikiosk.player.base import PlayerBackend
from pikiosk.screen.screen_service import ScreenService
from pikiosk.state import AfterVideo, KioskState, NowPlaying, PlayerState, percent_to_ha

log = logging.getLogger("pikiosk.controller")

Listener = Callable[[KioskState], None]
Hook = Callable[[], Awaitable[None] | None]


class Controller:
    def __init__(
        self,
        config: KioskConfig,
        *,
        player: PlayerBackend,
        screen: ScreenService,
        clock: Clock,
        bus: EventBus,
        store: StateStore,
        catalog: Catalog,
        cache_lookup: Callable[[str], str | None] | None = None,
        cache: Any | None = None,
        on_restart: Hook | None = None,
        on_update_ytdlp: Hook | None = None,
        ytdlp: YtDlp | None = None,
        on_update_app: Hook | None = None,
        on_sync: Hook | None = None,
        on_reload: Callable[[KioskConfig], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config
        self.player = player
        self.screen = screen
        self.clock = clock
        self.bus = bus
        self.store = store
        self.catalog = catalog
        self.cache = cache
        self.cache_lookup = cache_lookup or (cache.get if cache is not None else None)
        self.on_restart = on_restart
        self.on_update_ytdlp = on_update_ytdlp
        self.ytdlp = ytdlp
        self.on_update_app = on_update_app
        self.on_sync = on_sync
        self.on_reload = on_reload
        self._listeners: list[Listener] = []
        self._incoming: list[dict[str, Any]] = []
        self._retries = 0
        self._extractor_failures = 0
        self._last_heal = 0.0
        self._pending: tuple[float, str, dict[str, Any]] | None = None
        self._play_lock_until = 0.0
        self._unlock_until = 0.0
        self._pin_failures = 0
        self._pin_locked_until = 0.0
        self._mpv_log = ""
        self._mpv_log_lines: deque[str] = deque(maxlen=10)
        self._load_generation = 0
        self._last_playable: Playable | None = None
        self._append_task: asyncio.Task[None] | None = None
        self._managed_queue = False
        self._drain_lock = asyncio.Lock()
        self._drain_task: asyncio.Task[None] | None = None
        base = self._base_state()
        self.state = store.load(base)
        self.state = replace(
            self.state,
            player=PlayerState.IDLE,
            now=None,
            mpv_connected=player.connected,
        )
        screen.brightness = self.state.brightness
        screen.screen_on = self.state.screen_on
        screen.sleep_enabled = self.state.sleep_enabled
        screen.sleep_timeout_min = self.state.sleep_timeout_min
        player.add_listener(self._queue_player_event)
        self.screen.on_power = self._sync_power

    async def _sync_power(self, on: bool) -> None:
        self._set(replace(self.state, screen_on=on))
        if not on:
            self.bus.publish(Event("sleep", {"reason": "screen"}))
        else:
            self.bus.publish(Event("wake", {}))

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def _base_state(self) -> KioskState:
        return KioskState(
            volume=min(self.config.playback.default_volume, self.config.playback.max_volume),
            muted=self.config.playback.start_muted,
            after_video=AfterVideo(self.config.playback.after_video),
            brightness=percent_to_ha(self.config.screen.default_brightness),
            sleep_enabled=self.config.screen.sleep_enabled,
            sleep_timeout_min=self.config.screen.sleep_timeout_min,
            ui_locked=self.config.ui.locked,
        )

    def _set(self, new_state: KioskState) -> None:
        if new_state == self.state:
            return
        self.state = new_state
        self.screen.set_player_state(new_state.player)
        self.screen.configure(
            sleep_enabled=new_state.sleep_enabled,
            sleep_timeout_min=new_state.sleep_timeout_min,
        )
        for listener in list(self._listeners):
            listener(new_state)
        self.clock.call_later(2, self._persist)

    async def _persist(self) -> None:
        self.store.save(self.state)

    def _queue_player_event(self, event: dict[str, Any]) -> None:
        self._incoming.append(event)
        self._kick_drain()

    def _kick_drain(self) -> None:
        task = self._drain_task
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._drain_task = loop.create_task(self._drain_follow())

    async def _drain_follow(self) -> None:
        try:
            await self.drain()
        finally:
            if self._incoming:
                self._drain_task = None
                self._kick_drain()

    async def drain(self) -> None:
        if self._drain_lock.locked():
            return
        async with self._drain_lock:
            while self._incoming:
                event = self._incoming.pop(0)
                await self._on_player_event(event)

    def _error(self, code: str, detail: str | None = None) -> None:
        log.info("command rejected code=%s", code)
        self.bus.publish(Event("error", {"error": code, "detail": detail}))

    async def dispatch(self, name: str, args: dict[str, Any] | None = None) -> None:
        self.screen.note_activity()
        handler = getattr(self, f"cmd_{name}", None)
        if handler is None:
            self._error("unknown_command", name)
            return
        await handler(**(args or {}))
        await self.drain()

    async def cmd_play(self) -> None:
        await self._wake()
        if not self.player.connected:
            self._queue_command("play", {})
            return
        if self.state.player == PlayerState.PAUSED:
            await self.player.resume()
            self._set(replace(self.state, player=PlayerState.PLAYING))
            return
        if self.state.player == PlayerState.PLAYING:
            return
        name = self.state.selected or (self.catalog.names()[0] if self.catalog.names() else None)
        if name is None:
            self._error("empty_catalog")
            return
        await self.cmd_select(name)

    async def cmd_pause(self) -> None:
        if self.state.player != PlayerState.PLAYING:
            return
        await self.player.pause()
        self._set(replace(self.state, player=PlayerState.PAUSED))

    async def cmd_play_pause(self) -> None:
        if self.state.player == PlayerState.PLAYING:
            await self.cmd_pause()
        else:
            await self.cmd_play()

    async def cmd_stop(self) -> None:
        if self.player.connected and self.state.player in {
            PlayerState.PLAYING,
            PlayerState.PAUSED,
            PlayerState.LOADING,
        }:
            await self.player.stop()
            await self.drain()
        self._load_generation += 1  # abandons a download or playlist append in flight
        self._set(replace(self.state, player=PlayerState.IDLE, now=None, last_error=None, download_progress=None))
        self.screen.note_activity()

    async def cmd_next(self) -> None:
        await self._wake()
        current = self.state.now
        if (
            current
            and current.playlist_id
            and current.playlist_index is not None
            and current.playlist_count is not None
            and current.playlist_index < current.playlist_count - 1
        ):
            await self.player.playlist_next()
            await self.drain()
            return
        await self._step_catalog(1)

    async def cmd_previous(self) -> None:
        await self._wake()
        position = self.state.now.position if self.state.now else 0
        if position and position > 5:
            await self.player.seek(0)
            self._set(replace(self.state, now=replace(self.state.now, position=0) if self.state.now else None))
            return
        await self._step_catalog(-1)

    async def cmd_select(self, name: str) -> None:
        if name not in self.catalog.by_name:
            self._error("unknown_name", name)
            return
        self._set(replace(self.state, selected=name))
        await self._wake()
        try:
            playable = self.catalog.resolve(name)
        except CatalogError as exc:
            self._error(exc.code, str(exc))
            return
        await self._load(playable)

    async def cmd_play_media(
        self,
        ref: str = "",
        start: float | None = None,
        volume: int | None = None,
        after_video: str | None = None,
    ) -> None:
        if volume is not None:
            await self.cmd_set_volume(int(volume))
        if after_video:
            await self.cmd_set_after_video(after_video)
        try:
            playable = self.catalog.resolve(ref)
        except CatalogError as exc:
            self._error(exc.code, str(exc))
            return
        if start is not None:
            playable = replace(playable, start=start)
        if self.config.offline.only_offline and playable.video_id and not self._cached(playable.video_id):
            self._error("not_cached", playable.video_id)
            return
        await self._wake()
        await self._load(playable)

    async def cmd_seek(self, position: float) -> None:
        await self.player.seek(float(position))
        if self.state.now:
            self._set(replace(self.state, now=replace(self.state.now, position=float(position))))

    async def cmd_set_volume(self, volume: int) -> None:
        clamped = max(0, min(int(volume), self.config.playback.max_volume))
        await self.player.set_volume(clamped)
        self._set(replace(self.state, volume=clamped))

    async def cmd_set_mute(self, muted: bool) -> None:
        await self.player.set_mute(bool(muted))
        self._set(replace(self.state, muted=bool(muted)))

    async def cmd_screen(self, on: bool, brightness: int | None = None) -> None:
        if brightness is not None:
            self._set(replace(self.state, brightness=int(brightness)))
        if on:
            await self.screen.turn_on(self.state.brightness)
        else:
            await self.screen.turn_off()

    async def cmd_set_brightness(self, percent: int) -> None:
        percent = max(1, min(100, int(percent)))
        level = percent_to_ha(percent)
        self._set(replace(self.state, brightness=level))
        if self.state.screen_on:
            await self.screen.set_brightness(level)
        else:
            self.screen.brightness = level

    async def cmd_set_sleep_enabled(self, enabled: bool) -> None:
        self._set(replace(self.state, sleep_enabled=bool(enabled)))
        self.screen.note_activity()

    async def cmd_set_sleep_timeout(self, minutes: int) -> None:
        minutes = max(1, min(240, int(minutes)))
        self._set(replace(self.state, sleep_timeout_min=minutes))
        self.screen.note_activity()

    async def cmd_set_after_video(self, mode: str) -> None:
        try:
            value = AfterVideo(mode)
        except ValueError:
            self._error("bad_after_video", mode)
            return
        self._set(replace(self.state, after_video=value))
        loop = value == AfterVideo.LOOP
        await self.player.set_loop_file(loop and not self._in_playlist())
        await self.player.set_loop_playlist(loop and self._in_playlist())

    async def cmd_set_ui_locked(self, locked: bool) -> None:
        self._set(replace(self.state, ui_locked=bool(locked)))

    async def cmd_reload_config(self) -> None:
        if self.on_reload:
            await self._maybe(self.on_reload(self.config))

    async def cmd_restart_service(self) -> None:
        await self._maybe(self.on_restart() if self.on_restart else None)

    async def cmd_update_ytdlp(self) -> None:
        await self._maybe(self.on_update_ytdlp() if self.on_update_ytdlp else None)

    async def cmd_update_app(self) -> None:
        await self._maybe(self.on_update_app() if self.on_update_app else None)

    async def cmd_sync_offline(self) -> None:
        await self._maybe(self.on_sync() if self.on_sync else None)

    async def apply_config(self, config: KioskConfig, catalog: Catalog) -> None:
        self.config = config
        self.catalog = catalog
        if self.state.selected and self.state.selected not in catalog.by_name:
            self._set(replace(self.state, selected=None))
        # The grid learns about a new catalog from the next state push, so
        # send one even when nothing in KioskState changed.
        for listener in list(self._listeners):
            listener(self.state)
        self.bus.publish(Event("info", {"config": "reloaded"}))

    async def set_mpv_connected(self, connected: bool, error: str | None = None) -> None:
        self._set(
            replace(
                self.state,
                mpv_connected=connected,
                mpv_error=None if connected else error if error is not None else self.state.mpv_error,
            )
        )
        if connected:
            await self.player.set_volume(self.state.volume)
            await self.player.set_mute(self.state.muted)
            await self._flush_pending()

    async def set_mqtt_connected(self, connected: bool, error: str | None = None) -> None:
        ever = self.state.mqtt_ever_connected or connected
        self._set(
            replace(
                self.state,
                mqtt_connected=connected,
                mqtt_ever_connected=ever,
                mqtt_error=None if connected else error if error is not None else self.state.mqtt_error,
            )
        )

    def touch(self, kind: str = "tap") -> None:
        self.screen.note_activity()
        self.bus.publish(Event("touch", {"kind": kind}))
        policy = self.config.ui.touch_during_playback
        if self.state.player != PlayerState.PLAYING:
            return
        if policy == "pause_toggle":
            self._incoming.append({"event": "client-message", "args": ["pikiosk-touch", "tap"]})
        elif policy == "stop_to_grid" and kind == "tap":
            self._incoming.append({"event": "client-message", "args": ["pikiosk-touch", "stop"]})

    def check_pin(self, pin: str) -> bool:
        now = self.clock.monotonic()
        if now < self._pin_locked_until:
            self._error("pin_cooldown")
            return False
        if pin != self.config.ui.parent_unlock.pin:
            self._pin_failures += 1
            if self._pin_failures >= 3:
                self._pin_failures = 0
                self._pin_locked_until = now + 300
            return False
        self._pin_failures = 0
        self._unlock_until = now + 15 * 60
        self._set(replace(self.state, ui_locked=False))
        return True

    def temporarily_unlocked(self) -> bool:
        return self.clock.monotonic() < self._unlock_until

    def mpv_log_tail(self) -> list[str]:
        """Last warnings and errors mpv reported over IPC, oldest first."""
        return list(self._mpv_log_lines)

    async def _wake(self) -> None:
        if not self.state.screen_on:
            await self.screen.turn_on(self.state.brightness)
        self.screen.note_activity()

    async def _load(self, playable: Playable, *, retrying: bool = False) -> None:
        """One playback path: local file, or cache hit, or download then play."""
        if not self.player.connected:
            self._queue_command("play_media", {"ref": playable.url or playable.catalog_name or ""})
            return
        local = self._cached(playable.video_id) if playable.video_id else None
        if local and not playable.path:
            playable = replace(playable, path=local)
        selected = self.state.selected
        if playable.catalog_name:
            selected = playable.catalog_name
        thumb = playable.thumbnail
        if not thumb and playable.video_id:
            thumb = thumbnail_url(playable.video_id)
        now = NowPlaying(
            source=playable.source,
            catalog_name=playable.catalog_name,
            video_id=playable.video_id,
            playlist_id=playable.playlist_id,
            playlist_index=0,
            playlist_count=playable.max_items if playable.playlist_id else 1,
            title=playable.title,
            duration=None,
            position=0,
            thumbnail_url=thumb,
            local_file=playable.path if local or playable.source == "file" else None,
        )
        if playable.source == "file":
            now = replace(now, local_file=playable.path)
        if not retrying:
            self._retries = 0
        self._last_playable = playable
        self._load_generation += 1
        self._managed_queue = False
        generation = self._load_generation
        self._set(
            replace(
                self.state,
                player=PlayerState.LOADING,
                now=now,
                selected=selected,
                last_error=None,
                error_detail=None,
                download_progress=None,
            )
        )
        # One timer from the tap covers the download and the open.
        self.clock.call_later(
            self.config.playback.download_timeout_s,
            lambda: self._load_timed_out(generation),
        )
        if playable.path:
            await self._start_file(playable.path, playable.start, generation)
            return
        if self._downloader() is None:
            await self._stream_url(playable, generation)
            return
        ids = await self._queue_ids(playable)
        if self._stale(generation):
            return
        if ids is None:
            await self._stream_url(playable, generation)
            return
        if not ids:
            await self._on_error("playlist is empty or unavailable")
            return
        if len(ids) > 1 or playable.playlist_id:
            self._managed_queue = len(ids) > 1
            self._set(
                replace(
                    self.state,
                    now=replace(
                        now,
                        video_id=ids[0],
                        playlist_count=len(ids),
                        thumbnail_url=now.thumbnail_url or thumbnail_url(ids[0]),
                    ),
                )
            )
        path, err = await self._fetch_media(ids[0], playable.url if len(ids) == 1 else None, generation)
        if self._stale(generation):
            return
        if not path:
            await self._on_error(err or "download failed")
            return
        await self._start_file(path, playable.start, generation, playlist_items=len(ids))
        if len(ids) > 1 and not self._stale_generation(generation):
            self._append_task = asyncio.get_running_loop().create_task(self._append_rest(ids[1:], generation))

    def _downloader(self) -> Any:
        if self.cache is not None:
            return self.cache
        if getattr(self.ytdlp, "download_media", None) is not None:
            return self.ytdlp
        return None

    def _stale_generation(self, generation: int) -> bool:
        return generation != self._load_generation

    def _stale(self, generation: int) -> bool:
        return generation != self._load_generation or self.state.player != PlayerState.LOADING

    async def _queue_ids(self, playable: Playable) -> list[str] | None:
        """Video ids to play in order; None when the player must take the URL itself."""
        if not playable.playlist_id:
            return [playable.video_id] if playable.video_id else None
        expand = getattr(self.ytdlp, "flat_playlist", None)
        if expand is None:
            return None
        limit = playable.max_items or 50
        try:
            entries = await expand(playable.playlist_id, limit)
        except Exception as exc:
            log.warning("playlist expand failed id=%s error=%s", playable.playlist_id, exc)
            return []
        ids: list[str] = []
        for entry in entries:
            video_id = str(entry.get("id") or "")
            if video_id and video_id not in ids:
                ids.append(video_id)
        if playable.shuffle:
            random.shuffle(ids)
        return ids[:limit]

    async def _fetch_media(self, video_id: str, url: str | None, generation: int) -> tuple[str | None, str]:
        last = {"percent": -1}

        def on_progress(percent: float) -> None:
            value = int(percent)
            if value == last["percent"] or (value - last["percent"] < 2 and value < 100):
                return
            last["percent"] = value
            if generation == self._load_generation and self.state.player == PlayerState.LOADING:
                self._set(replace(self.state, download_progress=value))

        target = url or f"https://www.youtube.com/watch?v={video_id}"
        if self.cache is not None:
            path, err = await self.cache.ensure(video_id, target, progress=on_progress)
            return (str(path) if path else None), str(err or "")
        folder = Path(self.config.offline.directory).expanduser()
        try:
            paths, err = await self.ytdlp.download_media(  # type: ignore[union-attr]
                target,
                self.config.playback.ytdl_format,
                str(folder),
                playlist=False,
                limit=None,
                progress=on_progress,
                timeout=self.config.playback.download_timeout_s,
            )
        except Exception as exc:
            log.warning("download crashed id=%s error=%s", video_id, exc)
            return None, str(exc)
        if not paths:
            return None, err
        return paths[0], ""

    async def _start_file(
        self,
        path: str,
        start: float | None,
        generation: int,
        *,
        playlist_items: int = 1,
    ) -> None:
        current = self.state.now
        if current is not None:
            self._set(replace(self.state, now=replace(current, local_file=path), download_progress=None))
        await self.player.load(None, path, start=start)
        if self.state.after_video == AfterVideo.LOOP:
            await self.player.set_loop_file(playlist_items <= 1)
            await self.player.set_loop_playlist(playlist_items > 1)
        await self.drain()
        if self.state.player == PlayerState.LOADING and generation == self._load_generation:
            self.clock.call_later(60, lambda: self._open_timed_out(generation))

    async def _stream_url(self, playable: Playable, generation: int) -> None:
        """No downloader available (tests, --simulate): hand mpv the page URL."""
        is_playlist = bool(playable.playlist_id)
        await self.player.load(
            playable.url,
            None,
            start=playable.start,
            playlist=is_playlist,
            max_items=playable.max_items,
            shuffle=playable.shuffle,
        )
        if self.state.after_video == AfterVideo.LOOP:
            await self.player.set_loop_file(not is_playlist)
            await self.player.set_loop_playlist(is_playlist)
        await self.drain()
        if self.state.player == PlayerState.LOADING and generation == self._load_generation:
            self.clock.call_later(60, lambda: self._open_timed_out(generation))

    async def _append_rest(self, ids: list[str], generation: int) -> None:
        """Download the remaining playlist items and append them to mpv's queue."""
        for video_id in ids:
            if self._stale_generation(generation):
                return
            path, err = await self._fetch_media(video_id, None, generation)
            if self._stale_generation(generation):
                return
            if not path:
                log.info("playlist item skipped id=%s reason=%s", video_id, err.strip()[:120])
                current = self.state.now
                if current and current.playlist_count:
                    self._set(replace(self.state, now=replace(current, playlist_count=current.playlist_count - 1)))
                continue
            try:
                await self.player.load(None, path, append=True)
            except Exception as exc:
                log.warning("append failed id=%s error=%s", video_id, exc)
                return

    async def _open_timed_out(self, generation: int) -> None:
        if generation != self._load_generation or self.state.player != PlayerState.LOADING:
            return
        if self.state.download_progress is not None and self.state.download_progress < 100:
            return  # still downloading; the download timer owns this case
        await self._on_error(self._mpv_log or "playback did not start")

    async def _load_timed_out(self, generation: int) -> None:
        if generation != self._load_generation or self.state.player != PlayerState.LOADING:
            return
        progress = self.state.download_progress
        if progress is not None and progress < 100:
            detail = f"download timed out at {progress}%"
        else:
            detail = self._mpv_log or "playback did not start"
        await self._on_error(detail)

    def _cached(self, video_id: str | None) -> str | None:
        if not video_id or self.cache_lookup is None:
            return None
        return self.cache_lookup(video_id)

    def _queue_command(self, name: str, args: dict[str, Any]) -> None:
        self._pending = (self.clock.monotonic() + 30, name, args)

    async def _flush_pending(self) -> None:
        pending = self._pending
        self._pending = None
        if pending is None or self.clock.monotonic() > pending[0]:
            return
        await self.dispatch(pending[1], pending[2])

    async def _step_catalog(self, direction: int) -> None:
        names = self.catalog.names()
        if not names:
            return
        current = self.state.selected
        index = names.index(current) if current in names else -1
        if direction < 0 and index < 0:
            index = 0
        new_index = (index + direction) % len(names)
        await self.cmd_select(names[new_index])

    def _in_playlist(self) -> bool:
        current = self.state.now
        return bool(current and current.playlist_id and (current.playlist_count or 0) > 1)

    async def _on_player_event(self, event: dict[str, Any]) -> None:
        name = str(event.get("event") or "")
        if name == "pikiosk-connected":
            await self.set_mpv_connected(True)
            return
        if name == "pikiosk-disconnected":
            detail = event.get("error")
            await self.set_mpv_connected(False, error=str(detail) if detail else None)
            return
        if name == "log-message":
            text = " ".join(str(event.get("text") or "").split())
            if text:
                level = str(event.get("level") or "")
                prefix = str(event.get("prefix") or "")
                self._mpv_log_lines.append(f"[{level}] {prefix}: {text}"[:300] if prefix else text[:300])
                if level in {"", "error", "fatal"}:
                    self._mpv_log = text[:300]
            return
        if name == "property-change":
            await self._on_property(str(event.get("name")), event.get("data"))
            return
        if name == "file-loaded":
            self._set(replace(self.state, player=PlayerState.PLAYING, mpv_connected=True, download_progress=None))
            current = self.state.now
            if current and current.video_id and self.cache is not None:
                self.cache.mark_played(current.video_id)
            self.bus.publish(Event("video_started", {"title": current.title if current else ""}))
            return
        if name == "end-file":
            await self._on_end(str(event.get("reason") or ""), str(event.get("file_error") or ""))
            return
        if name == "client-message":
            args = event.get("args") or []
            if len(args) >= 2 and args[1] == "tap" and self.config.ui.touch_during_playback == "pause_toggle":
                await self.cmd_play_pause()
            if args and args[-1] == "stop":
                await self.cmd_stop()

    async def _on_property(self, name: str, data: Any) -> None:
        current = self.state.now
        if current is None:
            return
        if name == "media-title" and isinstance(data, str) and data:
            # A cached MKV reports its file name as the title; keep the catalog name.
            if current.local_file and Path(data).name == Path(current.local_file).name:
                return
            self._set(replace(self.state, now=replace(current, title=data)))
        elif name == "pause" and isinstance(data, bool):
            if data and self.state.player == PlayerState.PLAYING:
                self._set(replace(self.state, player=PlayerState.PAUSED))
            elif not data and self.state.player == PlayerState.PAUSED:
                self._set(replace(self.state, player=PlayerState.PLAYING))
        elif name == "duration" and isinstance(data, (int, float)):
            self._set(replace(self.state, now=replace(current, duration=float(data))))
        elif name == "time-pos" and isinstance(data, (int, float)):
            self._set(replace(self.state, now=replace(current, position=float(data))))
            if current.video_id and self.config.playback.resume_position:
                self.store.remember_position(current.video_id, float(data))
        elif name == "playlist-pos" and isinstance(data, int):
            self._set(replace(self.state, now=replace(current, playlist_index=data)))
        elif name == "playlist-count" and isinstance(data, int):
            # While the controller feeds mpv one downloaded file at a time, mpv's
            # count only says how many have arrived so far; ours is the total.
            if not self._managed_queue:
                self._set(replace(self.state, now=replace(current, playlist_count=data)))
        elif name == "path" and isinstance(data, str) and "v=" in data:
            video_id = data.split("v=")[-1].split("&")[0]
            if len(video_id) == 11:
                self._set(
                    replace(
                        self.state,
                        now=replace(current, video_id=video_id, thumbnail_url=thumbnail_url(video_id)),
                    )
                )

    async def _on_end(self, reason: str, detail: str) -> None:
        if reason == "stop":
            self._set(replace(self.state, player=PlayerState.IDLE, now=None))
            return
        if reason == "error":
            await self._on_error(detail or "playback failed")
            return
        if reason != "eof":
            return
        current = self.state.now
        mode = self.state.after_video
        if (
            current
            and current.playlist_id
            and self.config.playback.after_video_scope == "playlist"
            and current.playlist_index is not None
            and current.playlist_count is not None
            and current.playlist_index < current.playlist_count - 1
            and mode in {AfterVideo.STOP, AfterVideo.BLANK}
        ):
            return
        self.bus.publish(Event("video_ended", {}))
        if mode == AfterVideo.LOOP:
            return
        if mode == AfterVideo.NEXT:
            await self.cmd_next()
            return
        self._set(replace(self.state, player=PlayerState.IDLE, now=None))
        self.screen.note_activity()
        if mode == AfterVideo.BLANK:
            await self.screen.turn_off()

    async def _on_error(self, detail: str) -> None:
        if self._mpv_log and detail.strip().lower() in {"loading failed", "playback failed"}:
            detail = f"{detail}: {self._mpv_log}"
        self._mpv_log = ""
        kind = classify_error(detail)
        text = " ".join(detail.split())
        self._set(
            replace(
                self.state,
                player=PlayerState.ERROR,
                last_error=kind,
                error_detail=text[:400] or None,
                download_progress=None,
            )
        )
        self.bus.publish(Event("error", {"error": kind, "detail": detail}))
        log.warning("playback error kind=%s detail=%s", kind, text[:200])
        current = self.state.now
        cached = self._cached(current.video_id if current else None)
        if cached and current and current.local_file != cached:
            await self.player.load(None, cached)
            await self.drain()
            return
        if kind == "extractor":
            self._extractor_failures += 1
            if self._extractor_failures >= 3 and self.clock.monotonic() - self._last_heal > 3600:
                self._extractor_failures = 0
                self._last_heal = self.clock.monotonic()
                await self.cmd_update_ytdlp()
        # A flaky network or a hiccup in mpv deserves one more go; a video that
        # YouTube refuses does not.
        if kind in {"network", "player"} and self._retries < 1 and current is not None:
            self._retries += 1
            self.clock.call_later(3, self._retry)
        elif kind == "player" and current and current.video_id and current.local_file and self.cache is not None:
            # mpv refused the cached file twice: drop it so the next tap re-downloads.
            forget = getattr(self.cache, "forget", None)
            if forget is not None:
                forget(current.video_id)
        self.clock.call_later(10, self._clear_error)

    async def _retry(self) -> None:
        current = self.state.now
        if self.state.player != PlayerState.ERROR or current is None:
            return
        if current.local_file and Path(current.local_file).exists():
            self._set(replace(self.state, player=PlayerState.LOADING, last_error=None, error_detail=None))
            await self.player.load(None, current.local_file)
            await self.drain()
            return
        if self._last_playable is not None:
            await self._load(self._last_playable, retrying=True)

    async def _clear_error(self) -> None:
        if self.state.player == PlayerState.ERROR:
            self._set(replace(self.state, player=PlayerState.IDLE, now=None))

    async def _maybe(self, value: Awaitable[None] | None) -> None:
        if value is not None:
            await value
