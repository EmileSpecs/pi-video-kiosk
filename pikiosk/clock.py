"""Injectable clocks so sleep and retry timers are deterministic in tests."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

Callback = Callable[[], Awaitable[None] | None]


class TimerHandle(Protocol):
    def cancel(self) -> None: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def now(self) -> datetime: ...

    def call_later(self, delay: float, callback: Callback) -> TimerHandle: ...


class _AsyncioHandle:
    def __init__(self, handle: asyncio.TimerHandle) -> None:
        self._handle = handle

    def cancel(self) -> None:
        self._handle.cancel()


class SystemClock:
    def monotonic(self) -> float:
        return asyncio.get_running_loop().time()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def call_later(self, delay: float, callback: Callback) -> TimerHandle:
        loop = asyncio.get_running_loop()

        def wrapper() -> None:
            result = callback()
            if inspect.isawaitable(result):
                asyncio.ensure_future(result, loop=loop)

        return _AsyncioHandle(loop.call_later(delay, wrapper))


@dataclass
class _FakeHandle:
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


@dataclass
class _Scheduled:
    when: float
    callback: Callback
    handle: _FakeHandle


@dataclass
class FakeClock:
    """Controllable monotonic clock. ``advance`` runs due callbacks in order."""

    start: float = 0.0
    _now: datetime = field(default_factory=lambda: datetime(2026, 10, 13, 18, 0, tzinfo=timezone.utc))
    _timers: list[_Scheduled] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.start

    def now(self) -> datetime:
        return self._now

    def call_later(self, delay: float, callback: Callback) -> TimerHandle:
        handle = _FakeHandle()
        self._timers.append(_Scheduled(self.start + delay, callback, handle))
        return handle

    async def advance(self, seconds: float) -> None:
        self.start += seconds
        self._now = self._now.replace()  # keep tz; tests that care set _now
        due = [item for item in self._timers if not item.handle.cancelled and item.when <= self.start]
        due.sort(key=lambda item: item.when)
        for item in due:
            self._timers.remove(item)
            if item.handle.cancelled:
                continue
            result = item.callback()
            if inspect.isawaitable(result):
                await result

    def set_now(self, moment: datetime) -> None:
        self._now = moment
