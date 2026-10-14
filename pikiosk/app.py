"""Process wiring, signals, and the run loop."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from pikiosk import __version__
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
        output = RecordingOutput() if simulate else WlrRandr()
        self.screen = ScreenService(config, self.backlight, output, self.clock)
        if simulate:
            self.player: MemoryPlayer | MpvPlayer = MemoryPlayer()
            self.transport: MemoryTransport | AioTransport = MemoryTransport()
        else:
            socket_path = mpv_socket_path()
            self.player = MpvPlayer(
                socket_path,
                ytdl_format=config.playback.ytdl_format,
                audio_device=config.playback.audio_device,
            )
            self.transport = AioTransport(config)
        self.controller = Controller(
            config,
            player=self.player,
            screen=self.screen,
            clock=self.clock,
            bus=self.bus,
            store=self.store,
            catalog=self.catalog,
            on_restart=self._restart,
            on_reload=self._reload_hook,
            ytdlp=None if simulate else YtDlp(ytdlp_command(), extra_args=ytdlp_runtime_args()),
        )
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
        }

    async def _restart(self) -> None:
        log.info("restart requested")
        self.stop()

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
        self.config = config
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
        await self.bridge.start()
        await self._serve()
        heartbeat = asyncio.create_task(self._heartbeat())
        telemetry = asyncio.create_task(self._telemetry_loop())
        await self._stop.wait()
        heartbeat.cancel()
        telemetry.cancel()
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
