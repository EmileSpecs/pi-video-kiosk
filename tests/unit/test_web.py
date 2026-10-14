from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from pikiosk.catalog import Catalog
from pikiosk.web.server import catalog_version, create_child_app, state_view
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


async def test_ui_config_and_catalog_shape(tmp_path, example_config, clock) -> None:
    ui = example_config.ui.model_copy(update={"columns": 4, "show_titles": True, "locked_screen": "clock"})
    config = example_config.model_copy(update={"ui": ui})
    client, controller, _player = await _client(tmp_path, config, clock)
    try:
        ui = await (await client.get("/api/ui")).json()
        assert ui["columns"] == 4
        assert ui["show_titles"] is True
        assert ui["locked_screen"] == "clock"
        assert ui["parent_unlock"] is False
        assert ui["hold_corner"] == "top_right"
        assert ui["device_name"] == "Kids Kiosk"
        assert ui["catalog_version"] == catalog_version(controller.catalog.items)
        catalog = await (await client.get("/api/catalog")).json()
        baby = catalog[0]
        assert baby["thumb"] == "/thumbs/XqZsoesa55w.jpg"
        assert baby["video_id"] == "XqZsoesa55w"
        assert baby["cached"] is False
        bath = next(item for item in catalog if item["name"] == "Bath song")
        assert bath["thumb"] == "/thumbs/WRVsOCh907o.jpg"
        playlist = next(item for item in catalog if item["type"] == "playlist")
        assert playlist["video_id"] is None
        view = await (await client.get("/api/state")).json()
        assert view["catalog_version"] == ui["catalog_version"]
        assert "download_progress" in view and "error_detail" in view
    finally:
        await client.close()


async def test_thumbnail_proxy_caches_on_disk(tmp_path, example_config, clock) -> None:
    config = example_config.model_copy(
        update={"offline": example_config.offline.model_copy(update={"directory": str(tmp_path / "cache")})}
    )
    controller, _player, _screen = make_controller(tmp_path, config, clock)
    calls: list[str] = []

    async def fetch(url: str) -> bytes | None:
        calls.append(url)
        if "XqZsoesa55w" in url and url.endswith("hqdefault.jpg"):
            return b"JPEGDATA"
        return None

    app = create_child_app(controller, fetch_thumb=fetch)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        first = await client.get("/thumbs/XqZsoesa55w.jpg")
        assert first.status == 200
        assert await first.read() == b"JPEGDATA"
        assert first.headers["Content-Type"].startswith("image/jpeg")
        assert (tmp_path / "cache" / "thumbs" / "XqZsoesa55w.jpg").read_bytes() == b"JPEGDATA"
        second = await client.get("/thumbs/XqZsoesa55w.jpg")
        assert second.status == 200
        assert await second.read() == b"JPEGDATA"
        assert calls == [
            "https://i.ytimg.com/vi/XqZsoesa55w/hq720.jpg",
            "https://i.ytimg.com/vi/XqZsoesa55w/hqdefault.jpg",
        ]
        missing = await client.get("/thumbs/dQw4w9WgXcQ.jpg")
        assert missing.status == 404
        bad = await client.get("/thumbs/..%2Fetc.jpg")
        assert bad.status == 404
    finally:
        await client.close()


async def test_catalog_change_is_pushed_over_websocket(tmp_path, example_config, clock) -> None:
    client, controller, _player = await _client(tmp_path, example_config, clock)
    try:
        async with client.ws_connect("/ws") as socket:
            first = await socket.receive_json()
            before = first["catalog_version"]
            await socket.send_str("heartbeat")
            first_only = example_config.catalog.model_copy(update={"items": example_config.catalog.items[:1]})
            trimmed = example_config.model_copy(update={"catalog": first_only})
            catalog = Catalog(
                trimmed.catalog_items(),
                allowed_hosts=trimmed.playback.allowed_hosts,
                allow_unlisted=True,
                file_roots=trimmed.file_roots(),
            )
            await controller.apply_config(trimmed, catalog)
            pushed = await asyncio.wait_for(socket.receive_json(), 2)
            assert pushed["catalog_version"] != before
            assert pushed["catalog_version"] == catalog_version(catalog.items)
    finally:
        await client.close()


def test_state_view_progress_fields(example_config) -> None:
    from dataclasses import replace

    from pikiosk.state import KioskState, PlayerState

    state = replace(KioskState(), player=PlayerState.LOADING, download_progress=42, selected="Baby Shark")
    view = state_view(state, "abc")
    assert view["download_progress"] == 42
    assert view["selected"] == "Baby Shark"
    assert view["catalog_version"] == "abc"
    assert Path(__file__).exists()
