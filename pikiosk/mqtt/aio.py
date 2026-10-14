"""aiomqtt transport used on the device."""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator

import aiomqtt

from pikiosk.config import KioskConfig
from pikiosk.mqtt.memory import Incoming, MqttWill


class AioTransport:
    def __init__(self, config: KioskConfig) -> None:
        self.config = config
        self.connected = False
        self._client: aiomqtt.Client | None = None

    async def connect(self, will: MqttWill) -> None:
        tls_context = None
        if self.config.mqtt.tls.enabled:
            tls_context = ssl.create_default_context(cafile=self.config.mqtt.tls.ca_file)
        self._client = aiomqtt.Client(
            hostname=self.config.mqtt.host,
            port=self.config.mqtt.port,
            username=self.config.mqtt.username,
            password=self.config.mqtt.password,
            identifier=self.config.mqtt.resolved_client_id(self.config.device.id),
            keepalive=self.config.mqtt.keepalive,
            will=aiomqtt.Will(will.topic, will.payload, qos=will.qos, retain=will.retain),
            tls_context=tls_context,
            tls_insecure=self.config.mqtt.tls.insecure if tls_context else None,
        )
        await self._client.__aenter__()
        self.connected = True

    async def publish(self, topic: str, payload: str, *, retain: bool, qos: int) -> None:
        if self._client is None:
            raise RuntimeError("not connected")
        await self._client.publish(topic, payload, qos=qos, retain=retain)

    async def subscribe(self, topic: str) -> None:
        if self._client is None:
            raise RuntimeError("not connected")
        await self._client.subscribe(topic, qos=1)

    async def disconnect(self) -> None:
        self.connected = False
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

    async def messages(self) -> AsyncIterator[Incoming]:
        if self._client is None:
            return
        async for message in self._client.messages:
            payload = message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload)
            yield Incoming(str(message.topic), payload, bool(message.retain), int(message.qos))
