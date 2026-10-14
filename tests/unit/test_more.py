from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from pikiosk.cache.cache_manager import CacheEntry, CacheManager
from pikiosk.cache.ytdlp import YtDlp
from pikiosk.clock import FakeClock
from pikiosk.logging_setup import setup_logging
from pikiosk.player.mpv_player import MpvPlayer
from pikiosk.screen.backlight import discover_backlight, map_brightness
from pikiosk.state import PlayerState
from pikiosk.telemetry import collect, read_boot, read_cpu_temp
from pikiosk.watchdog import Notifier
from pikiosk.web.admin import create_admin_app
from tests.conftest import make_controller
from tests.unit.test_admin import _login, _store


class StubClient:
    def __init__(self) -> None:
        self.connected = True
        self.consecutive_timeouts = 0
        self.calls: list[tuple] = []

    async def command(self, *parts):
        self.calls.append(parts)
        if parts and parts[0] == "get_property":
            return 1
        return None

    async def close(self) -> None:
        self.connected = False


async def test_mpv_player_commands(tmp_path: Path) -> None:
    player = MpvPlayer(str(tmp_path / "mpv.sock"), ytdl_format="best")
    stub = StubClient()
    player._client = stub
    player.connected = True
    await player.set_volume(12)
    await player.set_mute(True)
    await player.load(
        "https://www.youtube.com/watch?v=XqZsoesa55w",
        None,
        start=3,
        playlist=True,
        max_items=5,
        shuffle=True,
    )
    await player.pause()
    await player.resume()
    await player.seek(4)
    await player.playlist_next()
    await player.playlist_prev()
    await player.set_loop_file(True)
    await player.set_loop_playlist(True)
    await player.stop()
    assert player.volume == 12
    assert any(call[0] == "loadfile" for call in stub.calls)


async def test_controller_transport_controls(tmp_path, example_config, clock: FakeClock) -> None:
    controller, player, screen = make_controller(tmp_path, example_config, clock)
    await controller.dispatch("play_media", {"ref": "XqZsoesa55w"})
    await controller.dispatch("pause", {})
    assert controller.state.player == PlayerState.PAUSED
    await controller.dispatch("play", {})
    assert controller.state.player == PlayerState.PLAYING
    await controller.dispatch("play_pause", {})
    assert controller.state.player == PlayerState.PAUSED
    await controller.dispatch("seek", {"position": 30})
    assert player.seek_to == 30
    await controller.dispatch("set_mute", {"muted": True})
    assert controller.state.muted is True
    await controller.dispatch("set_sleep_enabled", {"enabled": False})
    await controller.dispatch("set_sleep_timeout", {"minutes": 12})
    assert controller.state.sleep_timeout_min == 12
    await controller.dispatch("set_volume", {"volume": 200})
    assert controller.state.volume == example_config.playback.max_volume
    await controller.dispatch("next", {})
    await controller.dispatch("previous", {})
    if controller.state.now is not None:
        from dataclasses import replace

        controller._set(replace(controller.state, now=replace(controller.state.now, position=20)))
        await controller.dispatch("previous", {})
        assert player.seek_to == 0
    await controller.dispatch("stop", {})
    assert controller.state.player == PlayerState.IDLE
    await controller.dispatch("screen", {"on": False})
    assert screen.screen_on is False
    restarted = {"n": 0}

    async def hook() -> None:
        restarted["n"] += 1

    controller.on_restart = hook
    controller.on_update_ytdlp = hook
    controller.on_update_app = hook
    controller.on_sync = hook
    await controller.dispatch("restart_service", {})
    await controller.dispatch("update_ytdlp", {})
    await controller.dispatch("update_app", {})
    await controller.dispatch("sync_offline", {})
    assert restarted["n"] == 4
    await controller.dispatch("nope", {})
    await controller.dispatch("set_after_video", {"mode": "nope"})
    blocked = example_config.model_copy(
        update={"offline": example_config.offline.model_copy(update={"only_offline": True})}
    )
    controller.config = blocked
    await controller.dispatch("play_media", {"ref": "dQw4w9WgXcQ"})
    assert any(event.data.get("error") == "not_cached" for event in controller.bus.history)


async def test_admin_pages(tmp_path, clock: FakeClock) -> None:
    store = _store(tmp_path)
    controller, _player, _screen = make_controller(tmp_path, store.config, clock)
    async def saved() -> None:
        return None

    app = create_admin_app(store, controller, on_saved=saved)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        bad = await client.post("/login", data={"password": "no"}, allow_redirects=False)
        assert bad.status in {302, 303}
        csrf = await _login(client)
        for path in ("/status", "/connection", "/playback", "/screen", "/updates", "/offline", "/touch"):
            response = await client.get(path)
            assert response.status == 200
        saved = await client.post(
            "/connection",
            data={
                "csrf": csrf,
                "device_id": store.config.device.id,
                "name": "Kids Kiosk",
                "host": "broker.local",
                "port": "1883",
                "username": "kiosk",
                "password": "",
                "base_topic": "pikiosk",
                "discovery_prefix": "homeassistant",
            },
            allow_redirects=False,
        )
        assert saved.status in {302, 303}
        assert "super-secret-value" not in await (await client.get("/connection")).text()
        for path, fields in (
            ("/playback", {"default_volume": "20", "max_volume": "40", "after_video": "stop", "csrf": csrf}),
            ("/screen", {"default_brightness": "50", "sleep_timeout_min": "8", "rotation": "90", "csrf": csrf}),
            ("/updates", {"ref": "main", "csrf": csrf, "auto": "on", "only_when_idle": "on"}),
            ("/offline", {"max_size_gb": "5", "download_window": "01:00-06:00", "csrf": csrf}),
            ("/touch", {"columns": "2", "locked_screen": "black", "csrf": csrf}),
        ):
            response = await client.post(path, data=fields, allow_redirects=False)
            assert response.status in {200, 302, 303}
        await client.post("/logout", allow_redirects=False)
    finally:
        await client.close()


def test_telemetry_and_logging(tmp_path: Path, capsys) -> None:
    temp = tmp_path / "temp"
    temp.write_text("52300", encoding="utf-8")
    stat = tmp_path / "stat"
    stat.write_text("btime 1700000000\n", encoding="utf-8")
    up = tmp_path / "uptime"
    up.write_text("12.5 0\n", encoding="utf-8")
    assert read_cpu_temp(temp) == 52.3
    boot, uptime = read_boot(stat, up)
    assert boot is not None and uptime == 12.5
    payload = collect(mqtt_host="127.0.0.1", temp_path=temp, ytdlp_version="1", cache_items=2)
    assert payload["cpu_temp"] == 52.3
    from pikiosk.config import LoggingConfig

    setup_logging(LoggingConfig(level="INFO", as_json=False))
    logging.getLogger("pikiosk.test").info("hello")
    setup_logging(LoggingConfig(level="INFO", as_json=True))
    logging.getLogger("pikiosk.test").info("password=secret")
    Notifier(path=None).ready()
    Notifier(path=None).watchdog()
    Notifier(path=None).status("ok")


async def test_refresh_telemetry_publishes(tmp_path: Path, example_config, monkeypatch) -> None:
    monkeypatch.setattr("pikiosk.telemetry.local_ip", lambda host, port=80: "192.168.1.10")
    video = tmp_path / "videos"
    video.mkdir()
    (video / "clip.mp4").write_bytes(b"1234")
    config = example_config.model_copy(
        update={"offline": example_config.offline.model_copy(update={"directory": str(video)})}
    )
    from pikiosk.app import App

    app = App(config, simulate=True, state_path=tmp_path / "state.json")

    class _Yt:
        def __init__(self) -> None:
            self.calls = 0

        async def version(self) -> str:
            self.calls += 1
            return "2026.10.1\n"

    ytdlp = _Yt()
    app.controller.ytdlp = ytdlp  # type: ignore[assignment]
    app.transport.connected = True
    await app._refresh_telemetry()
    await app._refresh_telemetry()
    assert ytdlp.calls == 1
    assert app.bridge.telemetry["ytdlp_version"] == "2026.10.1"
    assert app.bridge.telemetry["ip"] == "192.168.1.10"
    assert app.bridge.telemetry["cache_items"] == 1
    assert any(
        item.topic.endswith("/telemetry") and "2026.10.1" in item.payload
        for item in app.transport.published
    )


def test_cache_window_and_evict(tmp_path: Path, example_config) -> None:
    clock = FakeClock()
    clock.set_now(datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc))
    config = example_config.model_copy(
        update={
            "offline": example_config.offline.model_copy(
                update={
                    "enabled": True,
                    "directory": str(tmp_path),
                    "max_size_gb": 10 / (1024**3),
                    "download_window": "01:00-06:00",
                }
            )
        }
    )
    manager = CacheManager(config, YtDlp(["yt-dlp"]), clock)
    assert manager.in_window() is False
    clock.set_now(datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc))
    assert manager.in_window() is True
    big = tmp_path / "old.mkv"
    big.write_bytes(b"0123456789abcdef")
    manager.entries["old"] = CacheEntry("old", str(big), "old", None, None, big.stat().st_size, 0, 1)
    keep = tmp_path / "keep.mkv"
    keep.write_bytes(b"x")
    manager.entries["keep"] = CacheEntry("keep", str(keep), "keep", None, None, 1, 5, 2)
    manager._evict({"keep"})
    assert "old" not in manager.entries
    assert map_brightness(0, 255, 5) == 0
    assert discover_backlight(tmp_path / "missing") is None


def test_backlight_preference(tmp_path: Path) -> None:
    named = tmp_path / "other"
    dsi = tmp_path / "dsi_panel"
    named.mkdir()
    dsi.mkdir()
    (named / "max_brightness").write_text("255", encoding="utf-8")
    (dsi / "max_brightness").write_text("31", encoding="utf-8")
    found = discover_backlight(tmp_path)
    assert found is not None
    assert found.max_brightness == 31
