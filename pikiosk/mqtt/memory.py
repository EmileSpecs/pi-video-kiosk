"""In-memory MQTT transport for tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class MqttWill:
    topic: str
    payload: str
    retain: bool = True
    qos: int = 1


@dataclass(frozen=True)
class Published:
    topic: str
    payload: str
    retain: bool
    qos: int


@dataclass(frozen=True)
class Incoming:
    topic: str
    payload: str
    retain: bool = False
    qos: int = 0


class MqttTransport(Protocol):
    connected: bool

    async def connect(self, will: MqttWill) -> None: ...

    async def publish(self, topic: str, payload: str, *, retain: bool, qos: int) -> None: ...

    async def subscribe(self, topic: str) -> None: ...

    async def disconnect(self) -> None: ...

    def messages(self) -> AsyncIterator[Incoming]: ...


class MemoryTransport:
    def __init__(self) -> None:
        self.published: list[Published] = []
        self.subscriptions: list[str] = []
        self.will: MqttWill | None = None
        self.connected = False
        self._queue: asyncio.Queue[Incoming | None] = asyncio.Queue()

    async def connect(self, will: MqttWill) -> None:
        self.will = will
        self.connected = True

    async def publish(self, topic: str, payload: str, *, retain: bool, qos: int) -> None:
        self.published.append(Published(topic, payload, retain, qos))

    async def subscribe(self, topic: str) -> None:
        self.subscriptions.append(topic)

    async def disconnect(self) -> None:
        self.connected = False
        if self.will is not None:
            self.published.append(
                Published(self.will.topic, self.will.payload, self.will.retain, self.will.qos)
            )
        await self._queue.put(None)

    def inject(self, message: Incoming) -> None:
        self._queue.put_nowait(message)

    async def messages(self) -> AsyncIterator[Incoming]:
        while True:
            item = await self._queue.get()
            if item is None:
                break
            yield item
