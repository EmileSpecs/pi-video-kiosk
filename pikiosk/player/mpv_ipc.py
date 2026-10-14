"""Async mpv JSON IPC client."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger("pikiosk.mpv")

EventCallback = Callable[[dict[str, Any]], None]


class MpvError(RuntimeError):
    pass


class MpvClient:
    def __init__(
        self,
        socket_path: str,
        *,
        request_timeout: float = 5.0,
        on_event: EventCallback | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.request_timeout = request_timeout
        self.on_event = on_event
        self.on_disconnect = on_disconnect
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._read_task: asyncio.Task[None] | None = None
        self.consecutive_timeouts = 0
        self.connected = False

    async def connect(self) -> None:
        opener = getattr(asyncio, "open_unix_connection", None)
        if opener is None:
            raise MpvError("unix sockets are unavailable")
        self._reader, self._writer = await opener(self.socket_path)
        self.connected = True
        self._read_task = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        self.connected = False
        if self._read_task:
            self._read_task.cancel()
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MpvError("closed"))
        self._pending.clear()

    async def command(self, *parts: Any) -> Any:
        if self._writer is None:
            raise MpvError("not connected")
        request_id = self._next_id
        self._next_id += 1
        payload = json.dumps({"command": list(parts), "request_id": request_id})
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        self._writer.write((payload + "\n").encode())
        try:
            await self._writer.drain()
        except (ConnectionError, OSError) as exc:
            self._pending.pop(request_id, None)
            raise MpvError(str(exc)) from exc
        try:
            reply = await asyncio.wait_for(future, self.request_timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            self._pending.pop(request_id, None)
            self.consecutive_timeouts += 1
            raise MpvError("timeout") from exc
        self.consecutive_timeouts = 0
        if reply.get("error") not in (None, "success"):
            raise MpvError(str(reply.get("error")))
        return reply.get("data")

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode())
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = message.get("request_id")
                if isinstance(request_id, int) and request_id in self._pending:
                    future = self._pending.pop(request_id)
                    if not future.done():
                        future.set_result(message)
                    continue
                if self.on_event and "event" in message:
                    self.on_event(message)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as exc:
            log.info("mpv socket closed error=%s", exc)
        finally:
            self.connected = False
            if self.on_disconnect:
                self.on_disconnect()
