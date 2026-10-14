from __future__ import annotations

from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from pikiosk.catalog import Catalog
from pikiosk.config import load_config
from pikiosk.config_store import ConfigStore
from pikiosk.web.admin import create_admin_app
from tests.conftest import make_controller

ROOT = Path(__file__).resolve().parents[2]


def _store(tmp_path: Path) -> ConfigStore:
    config = tmp_path / "config.yaml"
    secrets = tmp_path / "secrets.yaml"
    config.write_text((ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    secrets.write_text(
        'mqtt_password: "super-secret-value"\nui_pin: "2468"\nadmin_password: "admin-secret"\n',
        encoding="utf-8",
    )
    return ConfigStore(config, secrets)


async def _admin(tmp_path, clock):
    store = _store(tmp_path)
    controller, _player, _screen = make_controller(tmp_path, store.config, clock)

    async def reload() -> None:
        config = store.reload()
        catalog = Catalog(
            config.catalog_items(),
            allowed_hosts=config.playback.allowed_hosts,
            allow_unlisted=config.playback.allow_unlisted,
            file_roots=config.file_roots(),
        )
        await controller.apply_config(config, catalog)

    app = create_admin_app(store, controller, on_saved=reload)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, store, controller


async def _login(client: TestClient) -> str:
    await client.post("/login", data={"password": "admin-secret"})
    page = await client.get("/catalog")
    text = await page.text()
    marker = 'name="csrf" value="'
    csrf = text.split(marker, 1)[1].split('"', 1)[0]
    return csrf


async def test_anonymous_post_rejected(tmp_path, clock) -> None:
    client, store, _controller = await _admin(tmp_path, clock)
    before = store.config_path.read_text(encoding="utf-8")
    try:
        response = await client.post(
            "/catalog",
            data={"action": "delete", "name": "Baby Shark"},
            allow_redirects=False,
        )
        assert response.status in {302, 303, 401, 403}
        assert store.config_path.read_text(encoding="utf-8") == before
    finally:
        await client.close()


async def test_catalog_save_reloads(tmp_path, clock) -> None:
    client, store, controller = await _admin(tmp_path, clock)
    try:
        csrf = await _login(client)
        response = await client.post(
            "/catalog",
            data={
                "csrf": csrf,
                "action": "add",
                "name": "Extra",
                "type": "video",
                "id": "dQw4w9WgXcQ",
            },
            allow_redirects=False,
        )
        assert response.status in {302, 303}
        assert "notice=Saved" in response.headers["Location"]
        reloaded = load_config(store.config_path, secrets_path=store.secrets_path)
        assert any(item.name == "Extra" for item in reloaded.catalog.items)
        assert "Extra" in controller.catalog.by_name
    finally:
        await client.close()


async def test_bad_form_keeps_files(tmp_path, clock) -> None:
    client, store, _controller = await _admin(tmp_path, clock)
    before = store.config_path.read_text(encoding="utf-8")
    try:
        csrf = await _login(client)
        response = await client.post(
            "/catalog",
            data={"csrf": csrf, "action": "add", "name": "Bad", "type": "video", "id": "nope"},
        )
        assert response.status == 200
        assert "invalid" in (await response.text()).lower() or "id" in (await response.text()).lower()
        assert store.config_path.read_text(encoding="utf-8") == before
    finally:
        await client.close()


async def test_connection_save_reports_permission_error(tmp_path, clock, monkeypatch) -> None:
    client, store, _controller = await _admin(tmp_path, clock)

    def denied(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied", "/etc/pi-kiosk/config.yaml")

    monkeypatch.setattr(store, "save", denied)
    try:
        csrf = await _login(client)
        response = await client.post(
            "/connection",
            data={"csrf": csrf, "device_id": "kids_kiosk", "name": "Kids Kiosk", "host": "127.0.0.1", "port": "1883"},
            allow_redirects=False,
        )
        text = await response.text()
        assert response.status == 200
        assert "Could not write the config files" in text
    finally:
        await client.close()


async def test_connection_test_keeps_form(tmp_path, clock) -> None:
    client, _store, _controller = await _admin(tmp_path, clock)
    try:
        csrf = await _login(client)
        response = await client.post(
            "/connection/test",
            data={
                "csrf": csrf,
                "device_id": "kids_kiosk",
                "name": "Living Room",
                "host": "broker.example",
                "port": "1883",
                "username": "mqtt-user",
                "password": "mqtt-pass",
                "base_topic": "pikiosk",
                "discovery_prefix": "homeassistant",
            },
            allow_redirects=False,
        )
        text = await response.text()
        assert response.status == 200
        assert "connection failed" in text
        assert 'value="broker.example"' in text
        assert 'value="Living Room"' in text
        assert 'value="mqtt-user"' in text
        assert 'value="mqtt-pass"' in text
    finally:
        await client.close()


async def test_status_json_requires_login_and_reports_playback(tmp_path, clock) -> None:
    client, _store, controller = await _admin(tmp_path, clock)
    try:
        anonymous = await client.get("/status.json")
        assert anonymous.status == 401
        await _login(client)
        await controller.dispatch("select", {"name": "Baby Shark"})
        await controller.drain()
        response = await client.get("/status.json")
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        payload = await response.json()
        assert payload["player"] == "playing"
        assert payload["title"] == "Baby Shark"
        assert payload["thumbnail"] == "https://i.ytimg.com/vi/XqZsoesa55w/hqdefault.jpg"
        assert payload["download_progress"] is None
        assert payload["mpv_connected"] is True
        assert payload["info"]["version"]
        page = await (await client.get("/status")).text()
        assert "/status.json" in page
        assert "Baby Shark" in page
    finally:
        await client.close()


async def test_catalog_edit_keeps_position_and_renames(tmp_path, clock) -> None:
    client, store, controller = await _admin(tmp_path, clock)
    try:
        csrf = await _login(client)
        page = await (await client.get("/catalog?edit=Bath%20song")).text()
        assert "Edit “Bath song”" in page
        assert 'name="original" value="Bath song"' in page
        assert 'value="https://youtu.be/WRVsOCh907o"' in page
        before = [item.name for item in store.config.catalog.items]
        response = await client.post(
            "/catalog",
            data={
                "csrf": csrf,
                "action": "add",
                "original": "Bath song",
                "name": "Bath time",
                "type": "video",
                "id": "WRVsOCh907o",
                "start": "5",
                "thumbnail": "https://example.com/bath.jpg",
            },
            allow_redirects=False,
        )
        assert response.status in {302, 303}
        after = [item.name for item in store.config.catalog.items]
        assert after == [name if name != "Bath song" else "Bath time" for name in before]
        edited = next(item for item in store.config.catalog.items if item.name == "Bath time")
        assert edited.id == "WRVsOCh907o" and edited.url is None
        assert edited.start == 5.0
        assert edited.thumbnail == "https://example.com/bath.jpg"
        assert "Bath time" in controller.catalog.by_name
        # The list page shows a thumbnail per video row and confirms removal.
        listing = await (await client.get("/catalog")).text()
        assert 'src="https://example.com/bath.jpg"' in listing
        assert 'src="https://i.ytimg.com/vi/XqZsoesa55w/hqdefault.jpg"' in listing
        assert 'data-confirm="Remove “Bath time” from the kiosk?"' in listing
    finally:
        await client.close()


def test_item_thumbnail_picks_sensible_picture() -> None:
    from pikiosk.config import CatalogItemConfig
    from pikiosk.web.admin import item_thumbnail

    by_id = CatalogItemConfig(name="a", type="video", id="XqZsoesa55w")
    by_url = CatalogItemConfig(name="b", type="video", url="https://youtu.be/WRVsOCh907o?t=3")
    custom = CatalogItemConfig(name="c", type="playlist", id="PLabc", thumbnail="https://x/y.png")
    playlist = CatalogItemConfig(name="d", type="playlist", id="PLabc")
    local = CatalogItemConfig(name="e", type="file", path="/tmp/x.mp4")
    assert item_thumbnail(by_id) == "https://i.ytimg.com/vi/XqZsoesa55w/hqdefault.jpg"
    assert item_thumbnail(by_url) == "https://i.ytimg.com/vi/WRVsOCh907o/hqdefault.jpg"
    assert item_thumbnail(custom) == "https://x/y.png"
    assert item_thumbnail(playlist) is None
    assert item_thumbnail(local) is None


async def test_secrets_not_in_html(tmp_path, clock) -> None:
    client, _store, _controller = await _admin(tmp_path, clock)
    try:
        await _login(client)
        text = await (await client.get("/connection")).text()
        assert "super-secret-value" not in text
        assert "admin-secret" not in text
    finally:
        await client.close()
