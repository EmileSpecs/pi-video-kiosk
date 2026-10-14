from __future__ import annotations

import asyncio

import pytest

from pikiosk.player.fake_server import FakeMpvServer
from pikiosk.player.mpv_ipc import MpvClient

pytestmark = pytest.mark.skipif(
    not hasattr(asyncio, "start_unix_server"),
    reason="this interpreter has no unix sockets",
)


async def test_round_trip(tmp_path) -> None:
    socket = tmp_path / "mpv.sock"
    server = FakeMpvServer()
    server.titles["https://www.youtube.com/watch?v=XqZsoesa55w"] = "Baby Shark"
    await server.start(str(socket))
    events: list[dict] = []
    client = MpvClient(str(socket), on_event=events.append)
    await client.connect()
    await client.command("loadfile", "https://www.youtube.com/watch?v=XqZsoesa55w", "replace")
    await client.command("set_property", "volume", 40)
    assert await client.command("get_property", "volume") == 40
    await server.simulate_eof()
    await client.close()
    await server.stop()
    assert any(event.get("event") == "file-loaded" for event in events)
    assert any(event.get("reason") == "eof" for event in events)


async def test_hang_times_out(tmp_path) -> None:
    socket = tmp_path / "mpv.sock"
    server = FakeMpvServer()
    await server.start(str(socket))
    client = MpvClient(str(socket), request_timeout=0.05)
    await client.connect()
    server.hang()
    try:
        await client.command("get_property", "volume")
        raised = False
    except Exception:
        raised = True
    assert raised
    assert client.consecutive_timeouts == 1
    await client.close()
    await server.stop()
