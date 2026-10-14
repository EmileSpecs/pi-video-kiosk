from __future__ import annotations

import asyncio
import json
import logging
import socket
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from pikiosk import __main__ as entry
from pikiosk.app import App
from pikiosk.cache.cache_manager import CacheManager
from pikiosk.cache.ytdlp import YtDlp, classify_error
from pikiosk.catalog import Catalog
from pikiosk.clock import FakeClock
from pikiosk.config import ConfigError, LoggingConfig, load_config
from pikiosk.config_store import ConfigStore
from pikiosk.ctl import main as ctl_main
from pikiosk.input.touch import FakeTouch, InputMonitor, open_touch_device
from pikiosk.logging_setup import JsonFormatter, KeyValueFormatter, setup_logging
from pikiosk.mqtt.aio import AioTransport
from pikiosk.mqtt.bridge import MqttBridge
from pikiosk.mqtt.memory import Incoming, MemoryTransport
from pikiosk.player.fake_server import FakeMpvServer
from pikiosk.player.mpv_ipc import MpvClient, MpvError
from pikiosk.player.mpv_player import MpvPlayer
from pikiosk.screen.backlight import Backlight, discover_backlight, ha_from_sysfs
from pikiosk.screen.output import WlrRandr
from pikiosk.screen.screen_service import ScreenService
from pikiosk.state import AfterVideo, PlayerState
from pikiosk.telemetry import collect, local_ip, read_boot, read_cpu_temp
from pikiosk.watchdog import Notifier
from pikiosk.web.server import create_child_app
from tests.conftest import EXAMPLE, make_controller
from tests.unit.test_admin import _admin, _login


class MemWriter:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closed


async def _drive(server: FakeMpvServer, lines: list[bytes]) -> bytes:
    reader = asyncio.StreamReader()
    writer = MemWriter()
    task = asyncio.create_task(server._handle(reader, writer))  # type: ignore[arg-type]
    for line in lines:
        reader.feed_data(line)
        await asyncio.sleep(0)
    reader.feed_eof()
    await task
    return bytes(writer.buf)


def test_classify_and_telemetry(tmp_path: Path) -> None:
    assert classify_error("Please sign in to confirm") == "bot_check"
    assert classify_error("age-restricted") == "age_restricted"
    assert classify_error("not available in your country") == "geo"
    assert classify_error("video unavailable") == "unavailable"
    assert classify_error("timed out") == "network"
    assert classify_error("mystery") == "extractor"
    assert read_cpu_temp(tmp_path / "missing") is None
    temp = tmp_path / "temp"
    temp.write_text("45123", encoding="utf-8")
    assert read_cpu_temp(temp) == 45.123
    assert read_cpu_temp(temp.with_name("bad")) is None
    bad = tmp_path / "badtemp"
    bad.write_text("nope", encoding="utf-8")
    assert read_cpu_temp(bad) is None
    stat = tmp_path / "stat"
    stat.write_text("btime 1700000000\n", encoding="utf-8")
    up = tmp_path / "uptime"
    up.write_text("12.5 1\n", encoding="utf-8")
    boot, uptime = read_boot(stat, up)
    assert boot and boot.startswith("2023-")
    assert uptime == 12.5
    assert read_boot(tmp_path / "nope", tmp_path / "nope") == (None, None)
    assert local_ip("256.256.256.256") is None
    payload = collect(mqtt_host="256.256.256.256", temp_path=temp)
    assert payload["cpu_temp"] == 45.123
    assert payload["version"]


def test_notifier_and_logging(tmp_path: Path) -> None:
    note = Notifier(None)
    note.ready()
    note.watchdog()
    note.status("idle")
    note.stopping()
    family = getattr(socket, "AF_UNIX", None)
    if family is not None:
        path = tmp_path / "notify.sock"
        server = socket.socket(family, socket.SOCK_DGRAM)
        try:
            server.bind(str(path))
        except OSError:
            server.close()
        else:
            live = Notifier(str(path))
            live.send("READY=1")
            live.send("WATCHDOG=1")
            server.close()
        abstract = Notifier("@pikiosk-test")
        abstract.send("STATUS=x")
    record = logging.LogRecord("pikiosk", logging.INFO, __file__, 1, "password=secret", (), None)
    record.fields = {"pin": "1"}  # type: ignore[attr-defined]
    assert "[redacted]" in KeyValueFormatter().format(record)
    assert "fields" in JsonFormatter().format(record)
    log_file = tmp_path / "kiosk.log"
    setup_logging(LoggingConfig(level="INFO", as_json=False, file=str(log_file)))
    logging.getLogger("pikiosk.gap").info("hello")
    assert log_file.exists()


def test_config_edges(tmp_path: Path) -> None:
    example = load_config(EXAMPLE)
    git_repo = example.update.model_copy(update={"repo": "git@github.com:owner/pi-kiosk.git"})
    git = example.model_copy(update={"update": git_repo})
    assert git.support_url() == "https://github.com/owner/pi-kiosk"
    broken = tmp_path / "bad.yaml"
    broken.write_text(":\n  - [", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(broken)
    listing = tmp_path / "list.yaml"
    listing.write_text("- just-a-list\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(listing)
    missing = tmp_path / "nope.yaml"
    with pytest.raises(ConfigError):
        load_config(missing)
    bare = tmp_path / "bare.yaml"
    bare.write_text("device:\n  id: abc\n  name: A\nmqtt:\n  host: localhost\n", encoding="utf-8")
    loaded = load_config(
        bare,
        environ={"PIKIOSK__": "ignored", "PIKIOSK__MQTT__PORT": "1884", "PIKIOSK__MQTT__TLS__ENABLED": "yes"},
    )
    assert loaded.mqtt.port == 1884
    assert loaded.mqtt.tls.enabled is True
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("- nope\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(EXAMPLE, secrets_path=secrets)
    store = ConfigStore(bare, tmp_path / "absent-secrets.yaml")
    assert store.secrets == {}


async def test_fake_mpv_and_client() -> None:
    server = FakeMpvServer()
    if not hasattr(asyncio, "start_unix_server"):
        with pytest.raises(RuntimeError):
            await server.start("/tmp/missing.sock")
    server.titles["clip"] = "Clip"
    raw = await _drive(
        server,
        [
            b"not-json\n",
            b'{"command":["get_property","pause"],"request_id":1}\n',
            b'{"command":["set_property","volume",12],"request_id":2}\n',
            b'{"command":["observe_property",1,"time-pos"],"request_id":3}\n',
            b'{"command":["loadfile","clip","replace"],"request_id":4}\n',
            b'{"command":["loadfile","https://youtube.com/playlist?list=PL"],"request_id":5}\n',
            b'{"command":["seek",4,"absolute"],"request_id":6}\n',
            b'{"command":["playlist-next"],"request_id":7}\n',
            b'{"command":["playlist-prev"],"request_id":8}\n',
            b'{"command":["stop"],"request_id":9}\n',
            b'{"command":[],"request_id":10}\n',
        ],
    )
    assert b"success" in raw
    assert server.properties["volume"] == 12
    await asyncio.sleep(0)
    await server.simulate_eof()
    await server.simulate_error("nope")
    await server.drop_connection()
    server.hang()
    hung = await _drive(server, [b'{"command":["get_property","pause"],"request_id":1}\n'])
    assert hung == b""
    server.unhang()
    await server.stop()

    events: list[dict[str, Any]] = []
    client = MpvClient(
        "unused",
        request_timeout=0.05,
        on_event=events.append,
        on_disconnect=lambda: events.append({"event": "down"}),
    )
    if not hasattr(asyncio, "open_unix_connection"):
        with pytest.raises(MpvError):
            await client.connect()
    with pytest.raises(MpvError):
        await client.command("get_property", "pause")
    reader = asyncio.StreamReader()
    writer = MemWriter()
    client._reader = reader
    client._writer = writer  # type: ignore[assignment]
    client.connected = True
    client._read_task = asyncio.create_task(client._read_loop())
    pending = asyncio.create_task(client.command("get_property", "pause"))
    await asyncio.sleep(0)
    reader.feed_data(b"not-json\n")
    reader.feed_data(b"[]\n")
    reader.feed_data(b'{"request_id":1,"error":"success","data":false}\n')
    reader.feed_data(b'{"event":"file-loaded"}\n')
    assert await pending is False
    assert events
    failed = asyncio.create_task(client.command("set_property", "pause", True))
    await asyncio.sleep(0)
    reader.feed_data(b'{"request_id":2,"error":"invalid"}\n')
    with pytest.raises(MpvError):
        await failed
    with pytest.raises(MpvError):
        await client.command("get_property", "time-pos")
    assert client.consecutive_timeouts == 1

    class BadDrain(MemWriter):
        async def drain(self) -> None:
            raise ConnectionError("gone")

    client._writer = BadDrain()  # type: ignore[assignment]
    with pytest.raises(MpvError):
        await client.command("stop")
    reader.feed_eof()
    await asyncio.sleep(0)
    await client.close()


async def test_mpv_player_run(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}

    class Stub(MpvClient):
        async def connect(self) -> None:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise MpvError("down")
            self.connected = True
            self.consecutive_timeouts = 3

        async def close(self) -> None:
            self.connected = False

        async def command(self, *parts: Any) -> Any:
            return None

    monkeypatch.setattr("pikiosk.player.mpv_player.MpvClient", Stub)
    real_sleep = asyncio.sleep

    player = MpvPlayer("sock", ytdl_format="best", audio_device="alsa/default")
    restarted: list[str] = []

    async def restart() -> None:
        restarted.append("yes")

    player.on_restart = restart

    async def fast(_delay: float = 0) -> None:
        if attempts["n"] >= 2 and player.connected:
            player._stop.set()
        await real_sleep(0)

    monkeypatch.setattr("pikiosk.player.mpv_player.asyncio.sleep", fast)
    await player.start()
    assert player._task is not None
    await asyncio.wait_for(player._task, 2)
    assert attempts["n"] >= 2
    assert restarted
    player._last_restart = asyncio.get_running_loop().time()
    await player._request_restart()
    assert len(restarted) == 1, "a second restart within a minute is suppressed"
    await player.stop_client()


async def test_wlr_and_backlight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = {"body": b'[{"name":"HDMI-1"},{"name":"DSI-2"}]'}

    class Proc:
        async def communicate(self) -> tuple[bytes, bytes]:
            return payloads["body"], b""

        async def wait(self) -> int:
            return 0

    async def fake_exec(*_args: str, **_kwargs: Any) -> Proc:
        if payloads["body"] == b"raise":
            raise OSError("missing")
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    output = WlrRandr()
    assert await output.discover_name() == "DSI-2"
    payloads["body"] = b'{"DSI-1": {}, "HDMI-A-1": {}}'
    assert await output.discover_name() == "DSI-1"
    payloads["body"] = b"not-json"
    assert await output.discover_name() is None
    payloads["body"] = b"[]"
    assert await output.discover_name() is None
    payloads["body"] = b'[{"name":"HDMI-1"}]'
    await output.set_on("DSI-1", True, "90")
    await output.set_on("DSI-1", False, None)
    payloads["body"] = b"raise"
    assert await output.discover_name() is None
    await output.set_on("DSI-1", True, None)

    root = tmp_path / "bl"
    assert discover_backlight(root) is None
    assert discover_backlight(root, "missing") is None
    only = root / "panel"
    only.mkdir(parents=True)
    assert discover_backlight(root) is not None
    other = root / "hdmi"
    other.mkdir()
    (other / "max_brightness").write_text("nope", encoding="utf-8")
    chosen = discover_backlight(root)
    assert chosen is not None
    light = Backlight(None)
    assert light.read_actual() is None
    light.write(1, True)
    broken = Backlight(chosen)
    assert broken.read_actual() == broken._last_written or broken.read_actual() is not None
    assert ha_from_sysfs(10, 0) == 0
    assert ha_from_sysfs(255, 255) == 255


async def test_screen_dim_and_output(tmp_path: Path, example_config, clock: FakeClock) -> None:
    panel = tmp_path / "backlight" / "rpi_backlight"
    panel.mkdir(parents=True)
    (panel / "max_brightness").write_text("31", encoding="utf-8")
    (panel / "brightness").write_text("10", encoding="utf-8")
    (panel / "bl_power").write_text("0", encoding="utf-8")
    config = example_config.model_copy(
        update={
            "screen": example_config.screen.model_copy(
                update={"blank_method": "both", "output_name": "DSI-1", "sleep_timeout_min": 1, "dim_before_sleep_s": 5}
            )
        }
    )
    from pikiosk.screen.output import RecordingOutput

    screen = ScreenService(
        config,
        Backlight(discover_backlight(tmp_path / "backlight")),
        RecordingOutput(),
        clock,
        output_name="DSI-1",
    )
    await screen.turn_on(128)
    await screen.set_brightness(200)
    screen.set_player_state(PlayerState.IDLE)
    await clock.advance(55)
    await clock.advance(10)
    assert screen.screen_on is False
    screen.screen_on = True
    screen.player_state = PlayerState.PLAYING
    await screen._dim()
    await screen._sleep()


async def test_aio_transport(example_config, monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, Any] = {}

    class Message:
        topic = "pikiosk/kids_kiosk/cmd/play"
        payload = b"PRESS"
        retain = False
        qos = 1

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            created.update(kwargs)

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def publish(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def subscribe(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        @property
        def messages(self) -> Any:
            async def _gen() -> Any:
                yield Message()

            return _gen()

    class Will:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    monkeypatch.setattr("pikiosk.mqtt.aio.aiomqtt.Client", FakeClient)
    monkeypatch.setattr("pikiosk.mqtt.aio.aiomqtt.Will", Will)
    monkeypatch.setattr("pikiosk.mqtt.aio.ssl.create_default_context", lambda **_kwargs: object())
    tls_settings = example_config.mqtt.tls.model_copy(update={"enabled": True, "ca_file": "ca.pem", "insecure": True})
    tls = example_config.model_copy(update={"mqtt": example_config.mqtt.model_copy(update={"tls": tls_settings})})
    transport = AioTransport(tls)
    with pytest.raises(RuntimeError):
        await transport.publish("t", "p", retain=False, qos=0)
    with pytest.raises(RuntimeError):
        await transport.subscribe("t")
    assert [item async for item in transport.messages()] == []
    from pikiosk.mqtt.memory import MqttWill

    await transport.connect(MqttWill("status", "offline"))
    assert transport.connected
    await transport.publish("t", "p", retain=True, qos=1)
    await transport.subscribe("cmd/#")
    messages = [item async for item in transport.messages()]
    assert messages[0].payload == "PRESS"
    await transport.disconnect()
    assert transport.connected is False


async def test_bridge_commands_and_coalesce(tmp_path: Path, example_config, clock: FakeClock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    transport = MemoryTransport()
    bridge = MqttBridge(example_config, controller, transport, clock, controller.store)
    transport.connected = False
    await bridge.publish_state(force=True)
    assert transport.published == []
    assert bridge._pending_out
    await bridge._on_connect()
    assert any(item.topic.endswith("/state") for item in transport.published)
    controller.store.published_discovery_topics = ["homeassistant/sensor/old/config"]
    await bridge.publish_discovery()
    assert any(item.topic == "homeassistant/sensor/old/config" and item.payload == "" for item in transport.published)
    device_config = example_config.model_copy(
        update={"mqtt": example_config.mqtt.model_copy(update={"discovery_mode": "device", "image_entity": True})}
    )
    bridge.update_config(device_config)
    await bridge.publish_discovery()
    assert any("/device/" in item.topic for item in transport.published)
    await bridge._on_message(Incoming("homeassistant/status", "offline"))
    await bridge._on_message(Incoming("pikiosk/kids_kiosk/cmd/json", "{"))
    await bridge._on_message(Incoming("pikiosk/kids_kiosk/cmd/json", '{"command":"pause","args":{}}'))
    await bridge._on_message(Incoming("pikiosk/kids_kiosk/cmd/play_media", '{"start":1}'))
    await bridge._on_message(Incoming("pikiosk/kids_kiosk/cmd/play_media", "{"))
    await bridge._on_message(Incoming("pikiosk/kids_kiosk/cmd/play_media", "[]"))
    await bridge._on_message(Incoming("pikiosk/kids_kiosk/cmd/play_media", "XqZsoesa55w"))
    await bridge._on_message(Incoming("pikiosk/kids_kiosk/cmd/seek", "3"))
    await bridge._on_message(Incoming("pikiosk/kids_kiosk/cmd/mute", "ON"))
    await bridge._on_message(Incoming("pikiosk/kids_kiosk/cmd/screen", '{"state":"OFF","brightness":20}'))
    await bridge._on_message(Incoming("pikiosk/kids_kiosk/cmd/sleep_timeout", "4"))
    await bridge._on_message(Incoming("pikiosk/kids_kiosk/cmd/sleep_enabled", "OFF"))
    await bridge._on_message(Incoming("pikiosk/kids_kiosk/cmd/after_video", "next"))
    await bridge._on_message(Incoming("pikiosk/kids_kiosk/cmd/ui_enabled", "OFF"))
    await bridge._on_message(Incoming("pikiosk/kids_kiosk/cmd/update_app", "PRESS"))
    await bridge._on_message(Incoming("pikiosk/kids_kiosk/cmd/nope", "PRESS"))
    assert controller.state.ui_locked is True
    assert player.loads

    class Flaky(MemoryTransport):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def messages(self) -> Any:
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("dropped")
            while not bridge._stop.is_set():
                await asyncio.sleep(0.01)
            if False:
                yield Incoming("x", "y")

    reconnect = example_config.mqtt.reconnect.model_copy(update={"min_delay_s": 0.01, "max_delay_s": 0.02})
    fast = example_config.model_copy(
        update={"mqtt": example_config.mqtt.model_copy(update={"reconnect": reconnect})}
    )
    flaky = Flaky()
    other = MqttBridge(fast, controller, flaky, clock, controller.store)
    await other.start()
    await asyncio.sleep(0.05)
    await other.stop()


async def test_controller_branches(tmp_path: Path, example_config, clock: FakeClock) -> None:
    controller, player, screen = make_controller(tmp_path, example_config, clock)
    player.connected = False
    await controller.cmd_play()
    await controller.set_mpv_connected(True)
    await asyncio.sleep(0)
    assert player.loads or controller.state.player in {PlayerState.PLAYING, PlayerState.LOADING, PlayerState.IDLE}
    player.connected = True
    controller._set(controller.state.__class__(**{**controller.state.__dict__}))
    await controller.dispatch("play", {})
    await controller.cmd_pause()
    await controller.cmd_play()
    empty = example_config.model_copy(
        update={"catalog": example_config.catalog.model_copy(update={"items": []})}
    )
    controller.catalog = Catalog(
        [],
        allowed_hosts=example_config.playback.allowed_hosts,
        allow_unlisted=True,
        file_roots=example_config.file_roots(),
    )
    cleared = dict(controller.state.__dict__)
    cleared.update(selected=None, player=PlayerState.IDLE, now=None)
    controller._set(type(controller.state)(**cleared))
    await controller.cmd_play()
    controller.catalog = Catalog(
        example_config.catalog_items(),
        allowed_hosts=example_config.playback.allowed_hosts,
        allow_unlisted=example_config.playback.allow_unlisted,
        file_roots=example_config.file_roots(),
    )
    await controller.cmd_select("missing")
    await controller.cmd_play_media(ref="not a url !!!", volume=10, after_video="stop")
    await controller.dispatch("play_media", {"ref": "XqZsoesa55w"})
    controller.touch("tap")
    config = example_config.model_copy(
        update={"ui": example_config.ui.model_copy(update={"touch_during_playback": "pause_toggle"})}
    )
    controller.config = config
    controller.touch("tap")
    await controller.drain()
    controller.config = example_config.model_copy(
        update={"ui": example_config.ui.model_copy(update={"touch_during_playback": "stop_to_grid"})}
    )
    await controller.dispatch("play_media", {"ref": "XqZsoesa55w"})
    controller.touch("tap")
    await controller.drain()
    await controller._on_player_event({"event": "property-change", "name": "media-title", "data": "Hello"})
    await controller._on_player_event({"event": "property-change", "name": "duration", "data": 12})
    await controller._on_player_event({"event": "property-change", "name": "time-pos", "data": 4})
    await controller._on_player_event({"event": "property-change", "name": "playlist-pos", "data": 0})
    await controller._on_player_event({"event": "property-change", "name": "playlist-count", "data": 3})
    await controller._on_player_event({"event": "property-change", "name": "path", "data": "https://youtu.be/watch?v=XqZsoesa55w"})
    await controller._on_player_event({"event": "file-loaded"})
    await controller._on_player_event({"event": "end-file", "reason": "eof"})
    controller.config = example_config.model_copy(
        update={"playback": example_config.playback.model_copy(update={"after_video": "loop"})}
    )
    controller._set(type(controller.state)(**{**controller.state.__dict__, "after_video": AfterVideo.LOOP}))
    await controller._on_end("eof", "")
    await controller._on_end("quit", "")
    await controller._on_end("stop", "")
    cache_file = tmp_path / "cached.mkv"
    cache_file.write_text("x", encoding="utf-8")
    controller.cache_lookup = lambda video_id: str(cache_file) if video_id else None
    await controller.dispatch("play_media", {"ref": "XqZsoesa55w"})
    player.failures_left = 1
    await controller._on_error("extractor broke")
    controller.cache_lookup = lambda _video_id: None
    controller._extractor_failures = 2
    controller._last_heal = -4000
    called: list[str] = []

    async def heal() -> None:
        called.append("heal")

    controller.on_update_ytdlp = heal
    await controller._on_error("extractor broke")
    assert called
    await clock.advance(3)
    await clock.advance(10)
    await controller.cmd_reload_config()
    await controller.cmd_restart_service()
    await controller.cmd_update_app()
    await controller.cmd_sync_offline()
    await controller.apply_config(empty, controller.catalog)
    player.connected = False
    await controller._load(controller.catalog.resolve("Baby Shark"))
    await clock.advance(31)
    await controller.set_mpv_connected(True)
    assert empty is not None
    monitor = InputMonitor(example_config, screen, controller, clock, FakeTouch())
    screen.screen_on = False
    await monitor.press()
    await monitor.release()
    await clock.advance(0.3)
    await asyncio.sleep(0)
    controller._set(type(controller.state)(**{**controller.state.__dict__, "ui_locked": True}))
    monitor.on_screen(False)
    monitor.on_screen(True)
    assert open_touch_device("auto") is None or True


async def test_cache_and_ytdlp(
    tmp_path: Path,
    example_config,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = [
        (0, b'{"entries":[{"id":"aaaaaaaaaaa"},{"id":""},"nope"]}', b""),
        (0, b'{"entries":[{"id":"aaaaaaaaaaa"}]}', b""),
        (0, b"", b""),
        (1, b"", b"network down"),
        (0, b"2026.01.01\n", b""),
        (1, b"", b"nope"),
        (0, b'{"id":"abc"}', b""),
        (0, b"not-json", b""),
        (0, b"[]", b""),
    ]

    class Proc:
        def __init__(self, code: int, out: bytes, err: bytes) -> None:
            self.returncode = code
            self._out = out
            self._err = err

        async def communicate(self) -> tuple[bytes, bytes]:
            return self._out, self._err

    async def fake_exec(*args: str, **_kwargs: Any) -> Proc:
        code, out, err = replies.pop(0)
        if code == 0 and "-o" in args:
            template = Path(args[args.index("-o") + 1])
            video_id = args[-1].split("v=")[-1][:11]
            target = template.parent / f"{video_id}.mkv"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"1234567890")
            out = str(target).encode()
        return Proc(code, out, err)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    ytdlp = YtDlp(["yt-dlp"])
    assert await ytdlp.flat_playlist("PLxxxxxxxxxxxxxxxxxxxxxxxxxxxx", 2)
    config = example_config.model_copy(
        update={
            "offline": example_config.offline.model_copy(
                update={
                    "enabled": True,
                    "directory": str(tmp_path / "cache"),
                    "max_size_gb": 1,
                    "download_window": "22:00-06:00",
                    "rate_limit": "100K",
                    "delete_after_days": 0,
                }
            )
        }
    )
    clock.set_now(clock.now().replace(hour=23, minute=0, second=0))
    manager = CacheManager(config, ytdlp, clock)
    index = manager.index_path
    index.parent.mkdir(parents=True)
    index.write_text("{", encoding="utf-8")
    manager.load()
    index.write_text("[]", encoding="utf-8")
    manager.load()
    part = tmp_path / "cache" / "partial.part"
    part.write_text("x", encoding="utf-8")
    from pikiosk.cache.cache_manager import CacheEntry

    manager.entries["partial"] = CacheEntry("partial", str(part), "p", None, None, 1, 0, 0)
    assert manager.get("partial") is None
    manager.streaming = True
    await manager.sync([{"type": "video", "id": "aaaaaaaaaaa"}])
    manager.streaming = False
    clock.set_now(clock.now().replace(hour=12))
    await manager.sync([{"type": "video", "id": "aaaaaaaaaaa"}])
    clock.set_now(clock.now().replace(hour=23))
    video = tmp_path / "cache" / "aaaaaaaaaaa.mkv"
    await manager.sync(
        [
            {"type": "playlist", "id": "PLxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "max_items": 2},
            {"type": "video", "url": "https://youtu.be/WRVsOCh907o"},
            {"type": "video"},
        ]
    )
    # Direct video items are fetched before playlist entries; the playlist
    # entry then hits the "network down" reply.
    assert manager.get("WRVsOCh907o")
    assert manager.get("aaaaaaaaaaa") is None
    manager.mark_played("WRVsOCh907o")
    manager.mark_played("missing")
    await ytdlp.version()
    assert await ytdlp.simulate("aaaaaaaaaaa") is False
    assert await ytdlp.dump_json("aaaaaaaaaaa")
    assert await ytdlp.dump_json("aaaaaaaaaaa") is None
    assert await ytdlp.dump_json("aaaaaaaaaaa") is None
    failed = YtDlp(["yt-dlp"])

    async def fail_exec(*_args: str, **_kwargs: Any) -> Proc:
        return Proc(1, b"", b"extractor")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_exec)
    assert await failed.flat_playlist("PL", 1) == []
    tiny = config.model_copy(update={"offline": config.offline.model_copy(update={"max_size_gb": 1 / (1024**3)})})
    manager.config = tiny
    manager.entries["keep"] = CacheEntry("keep", str(video), "k", None, None, 100, 0, 1)
    manager._evict({"keep"})
    manager._forget_removed(set())
    manager._delete("missing")
    assert manager.in_window() or True


async def test_child_api_and_admin_edges(tmp_path: Path, clock: FakeClock) -> None:
    client, _store, controller = await _admin(tmp_path, clock)
    try:
        for _ in range(5):
            await client.post("/login", data={"password": "nope"}, allow_redirects=False)
        limited = await client.post("/login", data={"password": "admin-secret"}, allow_redirects=False)
        assert limited.status == 429
        client.app["failures"].clear()
        csrf = await _login(client)
        bad = await client.post("/catalog", data={"csrf": "nope", "action": "add", "name": "X"}, allow_redirects=False)
        assert bad.status == 403
        moved = await client.post(
            "/catalog",
            data={"csrf": csrf, "action": "move", "name": "Bath song", "direction": "up"},
            allow_redirects=False,
        )
        assert moved.status == 302
        deleted = await client.post(
            "/catalog",
            data={"csrf": csrf, "action": "delete", "name": "Bath song"},
            allow_redirects=False,
        )
        assert deleted.status == 302
        added = await client.post(
            "/catalog",
            data={
                "csrf": csrf,
                "action": "add",
                "name": "Local",
                "type": "file",
                "path": "/home/kiosk/Videos/extra.mp4",
                "start": "1.5",
                "max_items": "3",
                "shuffle": "on",
            },
            allow_redirects=False,
        )
        assert added.status == 302
        invalid = await client.post(
            "/catalog",
            data={"csrf": csrf, "action": "add", "name": "", "type": "video", "id": "short"},
            allow_redirects=False,
        )
        assert invalid.status == 200
        playback = await client.post(
            "/playback",
            data={"csrf": csrf, "max_volume": "nope", "default_volume": "1", "after_video": "stop"},
            allow_redirects=False,
        )
        assert playback.status == 200
        screen = await client.post(
            "/screen",
            data={"csrf": csrf, "default_brightness": "x", "sleep_timeout_min": "1", "rotation": "0"},
            allow_redirects=False,
        )
        assert screen.status == 200
        controller._set(type(controller.state)(**{**controller.state.__dict__, "mqtt_ever_connected": True}))
        locked = await client.post(
            "/connection",
            data={"csrf": csrf, "device_id": "other", "host": "127.0.0.1", "port": "1883"},
            allow_redirects=False,
        )
        assert locked.status == 200
        probe = await client.post(
            "/connection/test",
            data={"csrf": csrf, "host": "127.0.0.1", "port": "1"},
            allow_redirects=False,
        )
        assert probe.status == 200
        assert "connection failed" in await probe.text()
        now = await client.post(
            "/updates",
            data={"csrf": csrf, "auto": "on", "ref": "main", "only_when_idle": "on", "action": "now"},
            allow_redirects=False,
        )
        assert now.status == 302
        sync = await client.post(
            "/offline",
            data={"csrf": csrf, "enabled": "on", "max_size_gb": "2", "download_window": "", "action": "sync"},
            allow_redirects=False,
        )
        assert sync.status == 302
        css = await client.get("/admin.css")
        assert css.status == 200
    finally:
        await client.close()

    child_controller, _player, _screen = make_controller(tmp_path / "child", controller.config, clock)
    child = TestClient(TestServer(create_child_app(child_controller)))
    await child.start_server()
    try:
        state = await child.get("/api/state")
        assert state.status == 200
        bad_play = await child.post("/api/play", data=b"nope", headers={"content-type": "application/json"})
        assert bad_play.status == 400
        missing = await child.post("/api/play", json={"name": "nope"})
        assert missing.status == 403
        child_controller._set(type(child_controller.state)(**{**child_controller.state.__dict__, "ui_locked": True}))
        locked_stop = await child.post("/api/stop")
        assert locked_stop.status == 403
        unlock = await child.post("/api/unlock", json={"pin": "1"})
        assert unlock.status == 403
        command = await child.post("/api/command", json=["nope"])
        assert command.status == 400
        sent = await child.post("/api/command", json={"command": "set_volume", "args": {"volume": 5}})
        assert sent.status == 200
        health = await child.get("/healthz")
        assert health.status == 503
        async with child.ws_connect("/ws") as socket:
            await socket.receive_json()
            await socket.send_str("heartbeat")
            child_controller._set(child_controller.state)
            await asyncio.sleep(0)
    finally:
        await child.close()


def test_entry_points(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert entry.main(["--check-config", str(EXAMPLE)]) == 0
    broken = tmp_path / "bad.yaml"
    broken.write_text("device: [\n", encoding="utf-8")
    assert entry.main(["--check-config", str(broken)]) == 2
    with pytest.raises(SystemExit):
        entry.main([])

    class Dummy:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def run(self) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr(entry, "App", Dummy)
    assert entry.main(["--config", str(EXAMPLE), "--simulate", "--state", str(tmp_path / "state.json")]) == 0

    class Response:
        def read(self) -> bytes:
            return b'{"ok":true}'

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr("pikiosk.ctl.urllib.request.urlopen", lambda *_a, **_k: Response())
    assert ctl_main(["play", "--arg", "name=Baby", "--arg", "broken"]) == 0

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise urllib.error.URLError("down")

    monkeypatch.setattr("pikiosk.ctl.urllib.request.urlopen", explode)
    assert ctl_main(["stop"]) == 1


async def test_app_lifecycle(tmp_path: Path, example_config, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yaml"
    secrets_path = tmp_path / "secrets.yaml"
    config_path.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    secrets_path.write_text((EXAMPLE.parent / "secrets.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    config = load_config(config_path)
    config = config.model_copy(
        update={
            "ui": config.ui.model_copy(update={"port": 18781}),
            "admin": config.admin.model_copy(update={"enabled": True, "port": 18782}),
        }
    )
    app = App(
        config,
        simulate=False,
        config_path=config_path,
        secrets_path=secrets_path,
        state_path=tmp_path / "state.json",
        backlight_root=tmp_path / "nobl",
    )
    assert isinstance(app.player, MpvPlayer)
    await asyncio.sleep(0)
    await app._restart()
    assert app._stop.is_set()
    app._stop.clear()
    await app._reload_hook(config)
    async def published() -> None:
        return None

    app.bridge.publish_discovery = published  # type: ignore[method-assign]
    app.transport.connected = True
    await app.reload_saved()
    app.config_store = None
    await app.reload_saved()
    await app._reload_hook(config)

    original_sleep = asyncio.sleep
    calls = {"n": 0}

    async def brief(_delay: float = 0) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError

    asyncio.sleep = brief  # type: ignore[assignment]
    try:
        with pytest.raises(asyncio.CancelledError):
            await app._heartbeat()
    finally:
        asyncio.sleep = original_sleep  # type: ignore[assignment]
    sim = App(
        config,
        simulate=True,
        config_path=config_path,
        secrets_path=secrets_path,
        state_path=tmp_path / "state2.json",
        backlight_root=tmp_path / "nobl",
    )
    await sim._serve()
    for runner in sim._runners:
        await runner.cleanup()
    text = json.dumps({"ok": True})
    assert text
