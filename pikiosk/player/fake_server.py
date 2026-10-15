"""In-process mpv JSON IPC server for tests and --simulate."""

from __future__ import annotations

import asyncio
import json
from typing import Any


class FakeMpvServer:
    def __init__(self) -> None:
        self.properties: dict[str, Any] = {
            "pause": False,
            "volume": 100.0,
            "mute": False,
            "loop-file": "no",
            "loop-playlist": "no",
            "time-pos": 0.0,
            "duration": 120.0,
            "media-title": "",
            "idle-active": True,
            "playlist-pos": 0,
            "playlist-count": 1,
            "path": "",
            "eof-reached": False,
        }
        self.observers: dict[int, str] = {}
        self.titles: dict[str, str] = {}
        self.commands: list[list[Any]] = []
        self._server: asyncio.AbstractServer | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self._hang = False
        self.playlist: list[str] = []
        self.index = 0

    async def start(self, path: str) -> None:
        starter = getattr(asyncio, "start_unix_server", None)
        if starter is None:
            raise RuntimeError("unix sockets are unavailable")
        self._server = await starter(self._handle, path=path)
        self.path = path

    async def stop(self) -> None:
        for writer in list(self._writers):
            writer.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def simulate_eof(self) -> None:
        self.properties["eof-reached"] = True
        self.properties["idle-active"] = True
        await self._event({"event": "end-file", "reason": "eof"})

    async def simulate_error(self, message: str = "failed") -> None:
        await self._event({"event": "end-file", "reason": "error", "file_error": message})

    async def drop_connection(self) -> None:
        for writer in list(self._writers):
            writer.close()
        self._writers.clear()

    def hang(self) -> None:
        self._hang = True

    def unhang(self) -> None:
        self._hang = False

    async def _event(self, payload: dict[str, Any]) -> None:
        for writer in list(self._writers):
            self._send(writer, payload)

    def _send(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        if writer.is_closing():
            return
        writer.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                if self._hang:
                    continue
                try:
                    request = json.loads(line.decode())
                except json.JSONDecodeError:
                    continue
                command = request.get("command") or []
                request_id = request.get("request_id")
                reply = self._dispatch(list(command))
                if request_id is not None:
                    reply["request_id"] = request_id
                self._send(writer, reply)
                await writer.drain()
        finally:
            if writer in self._writers:
                self._writers.remove(writer)
            writer.close()

    def _dispatch(self, command: list[Any]) -> dict[str, Any]:
        self.commands.append(command)
        name = str(command[0]) if command else ""
        if name == "get_property":
            return {"error": "success", "data": self.properties.get(str(command[1]))}
        if name == "set_property":
            self.properties[str(command[1])] = command[2]
            return {"error": "success"}
        if name == "observe_property":
            self.observers[int(command[1])] = str(command[2])
            return {"error": "success"}
        if name == "loadfile":
            target = str(command[1])
            was_playing = not self.properties.get("idle-active", True)
            if len(command) > 2 and command[2] == "append":
                self.playlist.append(target)
                self.properties["playlist-count"] = len(self.playlist)
                return {"error": "success"}
            if was_playing:
                # Like mpv: the replaced file ends with reason "stop" first.
                asyncio.get_running_loop().create_task(self._event({"event": "end-file", "reason": "stop"}))
            self.properties["path"] = target
            self.properties["idle-active"] = False
            self.properties["pause"] = False
            self.properties["media-title"] = self.titles.get(target, target)
            self.properties["time-pos"] = 0.0
            if "playlist?list=" in target:
                self.playlist = [target]
                self.properties["playlist-count"] = 3
                self.properties["playlist-pos"] = 0
            else:
                self.playlist = [target]
                self.properties["playlist-count"] = 1
                self.properties["playlist-pos"] = 0
            self.index = 0
            asyncio.get_running_loop().create_task(self._loaded(target))
            return {"error": "success"}
        if name == "stop":
            self.properties["idle-active"] = True
            asyncio.get_running_loop().create_task(
                self._event({"event": "end-file", "reason": "stop"})
            )
            return {"error": "success"}
        if name == "seek":
            self.properties["time-pos"] = float(command[1])
            return {"error": "success"}
        if name in {"playlist-next", "playlist-prev"}:
            count = int(self.properties.get("playlist-count") or 1)
            pos = int(self.properties.get("playlist-pos") or 0)
            pos = pos + 1 if name == "playlist-next" else max(0, pos - 1)
            if pos >= count:
                asyncio.get_running_loop().create_task(self.simulate_eof())
            else:
                self.properties["playlist-pos"] = pos
            return {"error": "success"}
        return {"error": "success"}

    async def _loaded(self, target: str) -> None:
        await self._event({"event": "start-file"})
        await self._event({"event": "file-loaded"})
        await self._event(
            {
                "event": "property-change",
                "name": "media-title",
                "data": self.properties["media-title"],
            }
        )
        await self._event(
            {"event": "property-change", "name": "duration", "data": self.properties["duration"]}
        )
        await self._event({"event": "property-change", "name": "path", "data": target})
