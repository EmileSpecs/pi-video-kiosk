"""Process wiring, signals, and the run loop."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from pikiosk import __version__
from pikiosk.cache.cache_manager import CacheManager
from pikiosk.cache.ytdlp import YtDlp, ytdlp_command, ytdlp_runtime_args
from pikiosk.catalog import Catalog
from pikiosk.clock import SystemClock
from pikiosk.config import KioskConfig
from pikiosk.config_store import ConfigStore
from pikiosk.controller import Controller
from pikiosk.diag import MPV_LOG, audio_sink, git_commit, tail
from pikiosk.events import EventBus
from pikiosk.logging_setup import DEFAULT_LOG_FILE, setup_logging
from pikiosk.mqtt.aio import AioTransport
from pikiosk.mqtt.bridge import MqttBridge
from pikiosk.mqtt.memory import MemoryTransport
from pikiosk.persistence import StateStore
from pikiosk.player.memory import MemoryPlayer
from pikiosk.player.mpv_player import MpvPlayer
from pikiosk.screen.backlight import Backlight, discover_backlight
from pikiosk.screen.output import RecordingOutput, WlrRandr
from pikiosk.screen.screen_service import ScreenService
from pikiosk.telemetry import cache_usage, collect
from pikiosk.updater import last_update_path, read_last_update
from pikiosk.watchdog import Notifier
from pikiosk.web.admin import create_admin_app
from pikiosk.web.server import create_child_app

log = logging.getLogger("pikiosk.app")


def mpv_socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        uid = getattr(os, "getuid", lambda: None)()
        by_uid = Path("/run/user") / str(uid) if uid is not None else None
        runtime = str(by_uid if by_uid is not None and by_uid.is_dir() else Path.home() / ".runtime")
    return str(Path(runtime) / "pi-kiosk" / "mpv.sock")


def ytdlp_args(config: KioskConfig) -> list[str]:
    """Runtime flags (JS runtime, remote components) plus the operator's own."""
    args = ytdlp_runtime_args()
    if config.ytdlp.cookies_file:
        args.extend(["--cookies", str(Path(config.ytdlp.cookies_file).expanduser())])
    args.extend(config.ytdlp.extra_args)
    return args


def make_catalog(config: KioskConfig) -> Catalog:
    return Catalog(
        config.catalog_items(),
        allowed_hosts=config.playback.allowed_hosts,
        allow_unlisted=config.playback.allow_unlisted,
        file_roots=config.file_roots(),
    )


class App:
    def __init__(
        self,
        config: KioskConfig,
        *,
        simulate: bool = False,
        config_path: Path | None = None,
        secrets_path: Path | None = None,
        state_path: Path | None = None,
        backlight_root: Path | None = None,
    ) -> None:
        self.config = config
        self.simulate = simulate
        self.ready = asyncio.Event()
        self._stop = asyncio.Event()
        self.bus = EventBus()
        self.clock = SystemClock()
        self.store = StateStore(state_path or Path("state.json"))
        self.catalog = make_catalog(config)
        root = backlight_root or Path("/sys/class/backlight")
        self.backlight = Backlight(discover_backlight(root, config.screen.backlight_device))
        output = RecordingOutput() if simulate else WlrRandr(config.screen.output_name)
        self.screen = ScreenService(config, self.backlight, output, self.clock)
        self.ytdlp: YtDlp | None = None
        self.cache: CacheManager | None = None
        if simulate:
            self.player: MemoryPlayer | MpvPlayer = MemoryPlayer()
            self.transport: MemoryTransport | AioTransport = MemoryTransport()
        else:
            socket_path = mpv_socket_path()
            self.player = MpvPlayer(
                socket_path,
                ytdl_format=config.playback.ytdl_format,
                audio_device=config.playback.audio_device,
                on_restart=self._restart_mpv,
            )
            self.transport = AioTransport(config)
            self.ytdlp = YtDlp(ytdlp_command(), extra_args=ytdlp_args(config))
            self.cache = CacheManager(config, self.ytdlp, self.clock)
        self.controller = Controller(
            config,
            player=self.player,
            screen=self.screen,
            clock=self.clock,
            bus=self.bus,
            store=self.store,
            catalog=self.catalog,
            cache=self.cache,
            on_restart=self._restart,
            on_reload=self._reload_hook,
            on_sync=self._sync_now,
            on_update_app=None if simulate else self._update_app,
            on_update_ytdlp=None if simulate else self._update_ytdlp,
            ytdlp=self.ytdlp,
        )
        self._precache_task: asyncio.Task[None] | None = None
        self._sync_task: asyncio.Task[None] | None = None
        self.bridge = MqttBridge(config, self.controller, self.transport, self.clock, self.store)
        self.config_store = None
        if config_path and secrets_path:
            self.config_store = ConfigStore(config_path, secrets_path)
        self.notifier = Notifier()
        self._runners: list[web.AppRunner] = []
        self._ytdlp_version = ""
        self.commit = git_commit()

    def status_info(self) -> dict[str, Any]:
        """Facts for the admin Status page that live outside KioskState."""
        telemetry = self.bridge.telemetry
        return {
            "version": __version__,
            "commit": self.commit,
            "audio_sink": telemetry.get("audio_sink"),
            "cache_used_gb": telemetry.get("cache_used_gb"),
            "cache_items": telemetry.get("cache_items"),
            "ytdlp_version": self._ytdlp_version,
            "mpv_log": self.controller.mpv_log_tail() or tail(MPV_LOG, 10),
            "last_update": read_last_update(last_update_path(self.store.path)),
        }

    async def _restart(self) -> None:
        log.info("restart requested")
        self.stop()

    async def _start_user_unit(self, unit: str) -> None:
        """Kick a oneshot user unit without waiting (it may restart us)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "--user", "start", "--no-block", unit
            )
            await asyncio.wait_for(proc.wait(), 10)
            if proc.returncode not in (0, None):
                log.warning("systemctl start %s exited code=%s", unit, proc.returncode)
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("systemctl start %s failed error=%s", unit, exc)

    async def _update_app(self) -> None:
        log.info("app update requested")
        await self._start_user_unit("pikiosk-update.service")

    async def _update_ytdlp(self) -> None:
        log.info("yt-dlp update requested")
        await self._start_user_unit("pikiosk-ytdlp-update.service")

    async def _restart_mpv(self) -> None:
        """mpv stopped answering: let systemd give us a fresh one."""
        log.warning("restarting pikiosk-mpv.service")
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "--user", "restart", "pikiosk-mpv.service"
            )
            await asyncio.wait_for(proc.wait(), 20)
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("mpv restart failed error=%s", exc)

    async def _sync_now(self) -> None:
        if self.cache is None:
            return
        self._start_background(force_sync=True)

    def _start_background(self, *, force_sync: bool = False) -> None:
        """(Re)start the catalog pre-cache; a force_sync also expands playlists."""
        if self.cache is None:
            return
        if self._precache_task is not None and not self._precache_task.done():
            self._precache_task.cancel()
        items = self.config.catalog_items()
        if force_sync:
            self._precache_task = asyncio.create_task(self._guarded(self.cache.sync(items, force=True), "sync"))
        else:
            self._precache_task = asyncio.create_task(self._guarded(self.cache.precache(items), "pre-cache"))

    async def _guarded(self, work: Any, label: str) -> None:
        try:
            await work
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("%s failed", label, exc_info=True)

    async def _sync_loop(self) -> None:
        while True:
            hours = max(1.0, float(self.config.offline.sync_interval_h))
            await asyncio.sleep(hours * 3600)
            if self.cache is not None and self.config.offline.enabled:
                await self._guarded(self.cache.sync(self.config.catalog_items()), "sync")

    async def _reload_hook(self, _config: KioskConfig) -> None:
        if self.config_store:
            await self.reload_saved()

    async def reload_saved(self) -> None:
        if self.config_store is None:
            return
        config = self.config_store.reload()
        catalog = make_catalog(config)
        await self.controller.apply_config(config, catalog)
        reconnect = self.bridge.update_config(config)
        catalog_changed = config.catalog != self.config.catalog
        screen_changed = config.screen != self.config.screen
        self.config = config
        self.screen.config = config
        if screen_changed:
            await self.screen.apply_orientation()
        if self.cache is not None:
            self.cache.update_config(config)
            if self.ytdlp is not None:
                self.ytdlp.extra_args = ytdlp_args(config)
            if catalog_changed:
                self._start_background()
        # A broker change reconnects on its own and publishes discovery after
        # the new session is up. Republishing on the socket that is about to
        # close makes the publish wait until it times out.
        if reconnect or not self.bridge.transport.connected:
            return
        try:
            await self.bridge.publish_discovery()
        except Exception:
            log.warning("discovery republish failed", exc_info=True)

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        setup_logging(self.config.logging, default_file=None if self.simulate else DEFAULT_LOG_FILE)
        log.info("pikioskd starting version=%s commit=%s", __version__, self.commit or "unknown")
        self.ready.set()
        self.notifier.ready()
        if isinstance(self.player, MpvPlayer):
            await self.player.start()
        if not self.simulate:
            await self.screen.apply_orientation()
        await self.bridge.start()
        await self._serve()
        heartbeat = asyncio.create_task(self._heartbeat())
        telemetry = asyncio.create_task(self._telemetry_loop())
        self._start_background()
        self._sync_task = asyncio.create_task(self._sync_loop())
        await self._stop.wait()
        heartbeat.cancel()
        telemetry.cancel()
        for task in (self._precache_task, self._sync_task):
            if task is not None:
                task.cancel()
        self.notifier.stopping()
        self.store.save(self.controller.state)
        await self.bridge.stop()
        for runner in self._runners:
            await runner.cleanup()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(10)
            state = self.controller.state
            self.notifier.watchdog()
            self.notifier.status(
                f"mqtt={'up' if state.mqtt_connected else 'down'} "
                f"mpv={'up' if state.mpv_connected else 'down'} "
                f"state={state.player.value}"
            )

    async def _telemetry_loop(self) -> None:
        while True:
            await self._refresh_telemetry()
            await asyncio.sleep(max(5, self.config.telemetry.interval_s))

    async def _refresh_telemetry(self) -> None:
        ytdlp = self.controller.ytdlp
        if ytdlp is not None and not self._ytdlp_version:
            try:
                self._ytdlp_version = (await ytdlp.version()).strip()
            except Exception:
                log.warning("yt-dlp version failed", exc_info=True)
        used_gb, items = await asyncio.to_thread(cache_usage, self.config.offline.directory)
        sink = None if self.simulate else await asyncio.to_thread(audio_sink)
        payload = await asyncio.to_thread(
            collect,
            mqtt_host=self.config.mqtt.host,
            ytdlp_version=self._ytdlp_version,
            cache_used_gb=used_gb,
            cache_items=items,
            commit=self.commit,
            audio_sink=sink,
        )
        self.bridge.telemetry = payload
        try:
            await self.bridge.publish_telemetry()
        except Exception:
            log.warning("telemetry publish failed", exc_info=True)

    async def _serve(self) -> None:
        child = create_child_app(self.controller)
        runner = web.AppRunner(child)
        await runner.setup()
        await web.TCPSite(runner, self.config.ui.bind, self.config.ui.port).start()
        self._runners.append(runner)
        if self.config.admin.enabled and self.config_store is not None:
            admin = create_admin_app(
                self.config_store,
                self.controller,
                on_saved=self.reload_saved,
                info=self.status_info,
            )
            admin_runner = web.AppRunner(admin)
            await admin_runner.setup()
            bind = "127.0.0.1" if self.simulate else self.config.admin.bind
            await web.TCPSite(admin_runner, bind, self.config.admin.port).start()
            self._runners.append(admin_runner)
