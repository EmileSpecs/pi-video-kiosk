from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer

from pikiosk.web.server import create_child_app
from tests.conftest import make_controller


async def _client(tmp_path, example_config, clock):
    controller, player, screen = make_controller(tmp_path, example_config, clock)
    app = create_child_app(controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, controller, player


async def test_catalog_and_locked_play(tmp_path, example_config, clock) -> None:
    client, controller, _player = await _client(tmp_path, example_config, clock)
    try:
        catalog = await (await client.get("/api/catalog")).json()
        assert catalog[0]["name"] == "Baby Shark"
        await controller.dispatch("set_ui_locked", {"locked": True})
        denied = await client.post("/api/play", json={"name": "Baby Shark"})
        assert denied.status == 403
        stopped = await client.post("/api/stop")
        assert stopped.status == 403
    finally:
        await client.close()


async def test_debounce(tmp_path, example_config, clock) -> None:
    client, _controller, player = await _client(tmp_path, example_config, clock)
    try:
        first = await client.post("/api/play", json={"name": "Baby Shark"})
        second = await client.post("/api/play", json={"name": "Bath song"})
        assert first.status == 200
        assert second.status == 429
        assert len(player.loads) == 1
    finally:
        await client.close()


async def test_pin_cooldown(tmp_path, example_config, clock) -> None:
    config = example_config.model_copy(
        update={
            "ui": example_config.ui.model_copy(
                update={
                    "parent_unlock": example_config.ui.parent_unlock.model_copy(
                        update={"enabled": True, "pin": "2468"}
                    )
                }
            )
        }
    )
    client, controller, _player = await _client(tmp_path, config, clock)
    try:
        for _ in range(3):
            response = await client.post("/api/unlock", json={"pin": "0000"})
            assert response.status == 403
        cooled = await client.post("/api/unlock", json={"pin": "2468"})
        assert cooled.status == 429
        await clock.advance(300)
        ok = await client.post("/api/unlock", json={"pin": "2468"})
        assert ok.status == 200
        assert controller.state.ui_locked is False
    finally:
        await client.close()
