"""Subscribe to commands and publish discovery plus state."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pikiosk.clock import Clock
from pikiosk.config import KioskConfig
from pikiosk.controller import Controller
from pikiosk.events import Event
from pikiosk.mqtt.discovery import DiscoveryContext, build_component_payloads, build_device_payload
from pikiosk.mqtt.memory import Incoming, MqttTransport, MqttWill
from pikiosk.mqtt.state_payload import player_payload, screen_payload, without_position
from pikiosk.mqtt.topics import Topics
from pikiosk.persistence import StateStore

log = logging.getLogger("pikiosk.mqtt")


class MqttBridge:
    def __init__(
        self,
        config: KioskConfig,
        controller: Controller,
        transport: MqttTransport,
        clock: Clock,
        store: StateStore,
        *,
        context: DiscoveryContext | None = None,
        telemetry: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.controller = controller
        self.transport = transport
        self.clock = clock
        self.store = store
        self.context = context or DiscoveryContext(support_url=config.support_url())
        self.telemetry = telemetry or {}
        self.topics = Topics(config)
        self._stop = asyncio.Event()
        self._last_comparable: dict[str, Any] | None = None
        self._last_position_at = -1e9
        self._task: asyncio.Task[None] | None = None
        self._pending_out: dict[str, tuple[str, bool, int]] = {}
        self._reconnect = asyncio.Event()
        controller.subscribe(self._on_state)
        controller.bus.subscribe(self._on_event)

    def update_config(self, config: KioskConfig) -> None:
        self.config = config
        self.topics = Topics(config)
        if getattr(self.transport, "config", None) is not None:
            self.transport.config = config  # type: ignore[attr-defined]
        self._reconnect.set()

    async def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop.set()
        if self.transport.connected:
            await self.transport.publish(self.topics.status(), "offline", retain=True, qos=1)
            await self.transport.disconnect()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def run(self) -> None:
        delay = self.config.mqtt.reconnect.min_delay_s
        while not self._stop.is_set():
            error: str | None = None
            try:
                await self._on_connect()
                delay = self.config.mqtt.reconnect.min_delay_s
                if self._reconnect.is_set():
                    await self.transport.disconnect()
                else:
                    await self._pump()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("mqtt disconnected error=%s", exc)
                error = str(exc)
            await self.controller.set_mqtt_connected(False, error=error)
            if self._stop.is_set():
                break
            if self._reconnect.is_set():
                self._reconnect.clear()
                delay = self.config.mqtt.reconnect.min_delay_s
                continue
            try:
                await asyncio.wait_for(self._reconnect.wait(), timeout=delay)
            except (asyncio.TimeoutError, TimeoutError):
                delay = min(delay * 2, self.config.mqtt.reconnect.max_delay_s)
            self._reconnect.clear()

    async def _pump(self) -> None:
        incoming = self.transport.messages()
        message_task = asyncio.create_task(self._next_message(incoming))
        wake_task = asyncio.create_task(self._reconnect.wait())
        try:
            while not self._stop.is_set():
                done, _pending = await asyncio.wait(
                    {message_task, wake_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if wake_task in done or self._stop.is_set():
                    return
                message = message_task.result()
                if message is None:
                    return
                await self._on_message(message)
                message_task = asyncio.create_task(self._next_message(incoming))
        finally:
            for task in (message_task, wake_task):
                if not task.done():
                    task.cancel()
            if self.transport.connected:
                await self.transport.disconnect()

    async def _next_message(self, incoming: Any) -> Incoming | None:
        try:
            message = await incoming.__anext__()
        except StopAsyncIteration:
            return None
        return message if isinstance(message, Incoming) else None

    async def _on_connect(self) -> None:
        if not self.transport.connected:
            await self.transport.connect(
                MqttWill(self.topics.status(), "offline", retain=True, qos=1)
            )
        await self.transport.subscribe(f"{self.topics.base}/cmd/#")
        await self.transport.subscribe(self.topics.birth())
        await self.publish_discovery()
        await self.publish_state(force=True)
        await self.publish_screen()
        await self.publish_telemetry()
        await self._publish(self.topics.status(), "online", retain=True, qos=1)
        player = "online" if self.controller.state.mpv_connected else "offline"
        await self._publish(self.topics.player_status(), player, retain=True, qos=1)
        await self._flush_out()
        await self.controller.set_mqtt_connected(True)

    async def publish_discovery(self) -> None:
        if self.config.mqtt.discovery_mode == "device":
            payload = build_device_payload(self.config, self.context)
            topic = self.topics.device_discovery_topic()
            await self._retain(topic, payload)
            current = {topic}
        else:
            payloads = build_component_payloads(self.config, self.context)
            current = set(payloads)
            for topic, payload in payloads.items():
                await self._retain(topic, payload)
        previous = set(self.store.published_discovery_topics)
        for topic in previous - current:
            await self.transport.publish(topic, "", retain=True, qos=1)
        self.store.published_discovery_topics = sorted(current)

    async def publish_state(self, *, force: bool = False) -> None:
        payload = player_payload(self.controller.state, now=self.clock.now())
        comparable = without_position(payload)
        elapsed = self.clock.monotonic() - self._last_position_at
        if (
            not force
            and comparable == self._last_comparable
            and elapsed < self.config.mqtt.position_publish_interval_s
        ):
            return
        self._last_comparable = comparable
        self._last_position_at = self.clock.monotonic()
        await self._retain(self.topics.state(), payload)

    async def publish_screen(self) -> None:
        await self._retain(self.topics.screen(), screen_payload(self.controller.state))

    async def publish_telemetry(self) -> None:
        if self.telemetry:
            await self._retain(self.topics.telemetry(), self.telemetry)

    async def _retain(self, topic: str, payload: Any) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
        await self._publish(topic, text, retain=True, qos=1)

    async def _publish(self, topic: str, payload: str, *, retain: bool, qos: int) -> None:
        if not self.transport.connected:
            self._pending_out[topic] = (payload, retain, qos)
            return
        self._pending_out.pop(topic, None)
        await self.transport.publish(topic, payload, retain=retain, qos=qos)

    async def _flush_out(self) -> None:
        pending = list(self._pending_out.items())
        self._pending_out.clear()
        for topic, (payload, retain, qos) in pending:
            await self.transport.publish(topic, payload, retain=retain, qos=qos)

    def _on_state(self, _state: Any) -> None:
        loop = asyncio.get_running_loop()
        loop.create_task(self.publish_state())
        loop.create_task(self.publish_screen())

    def _on_event(self, event: Event) -> None:
        payload = {"type": event.type, "data": event.data}
        loop = asyncio.get_running_loop()
        loop.create_task(
            self._publish(
                self.topics.event(),
                json.dumps(payload, separators=(",", ":")),
                retain=False,
                qos=1,
            )
        )

    async def _on_message(self, message: Incoming) -> None:
        if message.topic == self.topics.birth() and message.payload == "online":
            await self._on_connect()
            return
        if not message.topic.startswith(f"{self.topics.base}/cmd/"):
            return
        if message.retain:
            log.info("ignored retained command topic=%s", message.topic)
            return
        name = message.topic.rsplit("/", 1)[-1]
        await self._dispatch_command(name, message.payload)

    async def _dispatch_command(self, name: str, payload: str) -> None:
        log.info("command name=%s source=mqtt", name)
        if name == "json":
            try:
                body = json.loads(payload)
            except json.JSONDecodeError:
                self.controller._error("bad_json")
                return
            command = str(body.get("command") or "")
            args = body.get("args") if isinstance(body.get("args"), dict) else {}
            await self.controller.dispatch(command, args)
            return
        buttons = {
            "play",
            "pause",
            "stop",
            "next",
            "previous",
            "play_pause",
            "reload_config",
            "update_ytdlp",
            "sync_offline",
            "restart",
            "update_app",
        }
        if name in buttons:
            mapping = {
                "reload_config": "reload_config",
                "update_ytdlp": "update_ytdlp",
                "sync_offline": "sync_offline",
                "restart": "restart_service",
                "update_app": "update_app",
            }
            await self.controller.dispatch(mapping.get(name, name), {})
            return
        if name == "select":
            await self.controller.dispatch("select", {"name": payload})
            return
        if name == "play_media":
            media: dict[str, Any]
            if payload.startswith("{"):
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    self.controller._error("bad_json")
                    return
                media = parsed if isinstance(parsed, dict) else {"ref": payload}
                if "ref" not in media:
                    media = {"ref": payload}
            else:
                media = {"ref": payload}
            await self.controller.dispatch("play_media", media)
            return
        if name == "seek":
            await self.controller.dispatch("seek", {"position": float(payload)})
            return
        if name == "volume":
            await self.controller.dispatch("set_volume", {"volume": int(float(payload))})
            return
        if name == "mute":
            await self.controller.dispatch("set_mute", {"muted": payload == "ON"})
            return
        if name == "screen":
            body = json.loads(payload)
            on = str(body.get("state", "ON")).upper() == "ON"
            brightness = body.get("brightness")
            await self.controller.dispatch(
                "screen",
                {"on": on, "brightness": brightness},
            )
            return
        if name == "sleep_timeout":
            await self.controller.dispatch("set_sleep_timeout", {"minutes": int(payload)})
            return
        if name == "sleep_enabled":
            await self.controller.dispatch("set_sleep_enabled", {"enabled": payload == "ON"})
            return
        if name == "after_video":
            await self.controller.dispatch("set_after_video", {"mode": payload})
            return
        if name == "ui_enabled":
            await self.controller.dispatch("set_ui_locked", {"locked": payload != "ON"})
            return
        self.controller._error("unknown_command", name)
