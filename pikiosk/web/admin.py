"""Parent admin site. Server-rendered forms that write config and hot-reload."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError

from pikiosk import __version__
from pikiosk.catalog import Catalog, CatalogError, parse_ref, thumbnail_url
from pikiosk.config import CatalogConfig, CatalogItemConfig, ConfigError, KioskConfig
from pikiosk.config_store import ConfigStore
from pikiosk.controller import Controller
from pikiosk.diag import git_commit
from pikiosk.web.auth import COOKIE, new_session, sign, unsign

log = logging.getLogger("pikiosk.admin")
TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"
NAV = [
    ("status", "Status"),
    ("connection", "Connection"),
    ("catalog", "Catalog"),
    ("playback", "Playback"),
    ("screen", "Screen"),
    ("updates", "Updates"),
    ("offline", "Offline"),
    ("touch", "Touch"),
]


def create_admin_app(
    store: ConfigStore,
    controller: Controller,
    *,
    on_saved: Callable[[], Awaitable[None]] | None = None,
    info: Callable[[], dict[str, Any]] | None = None,
) -> web.Application:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
    )
    app = web.Application()
    app["store"] = store
    app["controller"] = controller
    app["env"] = env
    app["on_saved"] = on_saved
    app["info"] = info or (lambda: {"version": __version__, "commit": git_commit()})
    app["failures"] = {}
    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login_post)
    app.router.add_post("/logout", logout)
    app.router.add_get("/", status_page)
    app.router.add_get("/status", status_page)
    app.router.add_get("/status.json", status_json)
    app.router.add_get("/connection", connection_page)
    app.router.add_post("/connection", connection_post)
    app.router.add_post("/connection/test", connection_test)
    app.router.add_get("/catalog", catalog_page)
    app.router.add_post("/catalog", catalog_post)
    app.router.add_get("/playback", playback_page)
    app.router.add_post("/playback", playback_post)
    app.router.add_get("/screen", screen_page)
    app.router.add_post("/screen", screen_post)
    app.router.add_get("/updates", updates_page)
    app.router.add_post("/updates", updates_post)
    app.router.add_get("/offline", offline_page)
    app.router.add_post("/offline", offline_post)
    app.router.add_get("/touch", touch_page)
    app.router.add_post("/touch", touch_post)
    app.router.add_get("/healthz", health)
    app.router.add_get("/admin.css", css)
    return app


def _session(request: web.Request) -> dict[str, Any] | None:
    store: ConfigStore = request.app["store"]
    token = request.cookies.get(COOKIE, "")
    if not token:
        return None
    return unsign(token, store.config.admin.password or "pikiosk")


def _require(request: web.Request) -> dict[str, Any] | web.Response:
    session = _session(request)
    if session is None or not session.get("ok"):
        raise web.HTTPFound("/login")
    return session


def _render(
    request: web.Request,
    template: str,
    session: dict[str, Any],
    **extra: Any,
) -> web.Response:
    env: Environment = request.app["env"]
    store: ConfigStore = request.app["store"]
    controller: Controller = request.app["controller"]
    html = env.get_template(template).render(
        config=store.config,
        state=controller.state,
        csrf=session.get("csrf", ""),
        nav=NAV,
        thumb_for=item_thumbnail,
        active=template.replace(".html", ""),
        error=extra.pop("error", None),
        notice=extra.pop("notice", None) or request.query.get("notice"),
        **extra,
    )
    return web.Response(text=html, content_type="text/html")


def _done(path: str, notice: str = "Saved.") -> web.HTTPFound:
    return web.HTTPFound(f"{path}?notice={quote(notice)}")


def _check_csrf(request: web.Request, data: Any, session: dict[str, Any]) -> None:
    if str(data.get("csrf") or "") != str(session.get("csrf") or ""):
        raise web.HTTPForbidden(text="bad csrf")


async def _save(request: web.Request, config: KioskConfig, secrets: dict[str, Any]) -> str | None:
    store: ConfigStore = request.app["store"]
    try:
        store.save(config, secrets)
    except OSError as exc:
        return f"Could not write the config files: {exc}"
    except ConfigError as exc:
        return str(exc)
    except Exception as exc:
        log.exception("config save failed")
        return f"Could not save config: {exc}"
    callback = request.app["on_saved"]
    if callback:
        try:
            await callback()
        except Exception as exc:
            log.exception("config reload failed")
            return f"Saved, but reload failed: {exc}"
    return None


def _catalog(config: KioskConfig) -> Catalog:
    return Catalog(
        config.catalog_items(),
        allowed_hosts=config.playback.allowed_hosts,
        allow_unlisted=config.playback.allow_unlisted,
        file_roots=config.file_roots(),
    )


async def login_page(request: web.Request) -> web.Response:
    env: Environment = request.app["env"]
    html = env.get_template("login.html").render(error=request.query.get("error"))
    return web.Response(text=html, content_type="text/html")


async def login_post(request: web.Request) -> web.Response:
    store: ConfigStore = request.app["store"]
    data = await request.post()
    host = request.remote or "local"
    failures: dict[str, int] = request.app["failures"]
    if failures.get(host, 0) >= 5:
        raise web.HTTPTooManyRequests(text="slow down")
    if str(data.get("password") or "") != store.config.admin.password:
        failures[host] = failures.get(host, 0) + 1
        raise web.HTTPFound("/login?error=1")
    failures[host] = 0
    response = web.HTTPFound("/")
    response.set_cookie(
        COOKIE,
        sign(new_session(), store.config.admin.password or "pikiosk"),
        httponly=True,
        samesite="Lax",
        path="/",
    )
    raise response


async def logout(request: web.Request) -> web.Response:
    response = web.HTTPFound("/login")
    response.del_cookie(COOKIE, path="/")
    raise response


def item_thumbnail(item: CatalogItemConfig) -> str | None:
    """Picture for a catalog row: the configured one, else YouTube's for video items."""
    if item.thumbnail:
        return item.thumbnail
    if item.type != "video":
        return None
    if item.id:
        return thumbnail_url(item.id)
    if item.url:
        try:
            ref = parse_ref(item.url)
        except CatalogError:
            return None
        if ref.kind == "video" and ref.id:
            return thumbnail_url(ref.id)
    return None


def status_payload(controller: Controller, info: dict[str, Any]) -> dict[str, Any]:
    """Everything the Status page shows, as JSON for the 5 s poll."""
    state = controller.state
    now = state.now
    title = ""
    subtitle = ""
    if now is not None:
        # Parents know the catalog name; mpv's media title goes underneath.
        title = now.catalog_name or now.title
        bits = []
        if now.title and now.title != title:
            bits.append(now.title)
        if now.playlist_count:
            bits.append(f"{(now.playlist_index or 0) + 1} of {now.playlist_count}")
        if now.local_file:
            bits.append("from cache")
        subtitle = " · ".join(bits)
    return {
        "player": state.player.value,
        "title": title,
        "subtitle": subtitle,
        "thumbnail": now.thumbnail_url if now else None,
        "download_progress": state.download_progress if state.player.value == "loading" else None,
        "last_error": state.last_error,
        "error_detail": state.error_detail,
        "volume": state.volume,
        "muted": state.muted,
        "screen_on": state.screen_on,
        "mqtt_connected": state.mqtt_connected,
        "mqtt_error": state.mqtt_error,
        "mpv_connected": state.mpv_connected,
        "mpv_error": state.mpv_error,
        "info": info,
    }


async def status_page(request: web.Request) -> web.Response:
    session = _require(request)
    assert isinstance(session, dict)
    controller: Controller = request.app["controller"]
    info: dict[str, Any] = request.app["info"]()
    status = status_payload(controller, info)
    return _render(
        request,
        "status.html",
        session,
        info=info,
        status=status,
        status_json=json.dumps(status).replace("</", "<\\/"),
    )


async def status_json(request: web.Request) -> web.Response:
    session = _session(request)
    if session is None or not session.get("ok"):
        raise web.HTTPUnauthorized(text="sign in first")
    controller: Controller = request.app["controller"]
    info: dict[str, Any] = request.app["info"]()
    return web.json_response(
        status_payload(controller, info),
        headers={"Cache-Control": "no-store"},
    )


def _connection_form(data: Any, config: KioskConfig) -> dict[str, Any]:
    def posted(key: str, fallback: str) -> str:
        if key in data:
            return str(data.get(key) or "")
        return fallback

    return {
        "device_id": posted("device_id", config.device.id),
        "name": posted("name", config.device.name),
        "host": posted("host", config.mqtt.host),
        "port": posted("port", str(config.mqtt.port)),
        "username": posted("username", config.mqtt.username or ""),
        "password": posted("password", ""),
        "base_topic": posted("base_topic", config.mqtt.base_topic),
        "discovery_prefix": posted("discovery_prefix", config.mqtt.discovery_prefix),
        "tls": data.get("tls") == "on",
    }


async def connection_page(request: web.Request) -> web.Response:
    session = _require(request)
    assert isinstance(session, dict)
    return _render(request, "connection.html", session, notice=request.query.get("notice"))


async def connection_post(request: web.Request) -> web.Response:
    session = _require(request)
    assert isinstance(session, dict)
    data = await request.post()
    _check_csrf(request, data, session)
    store: ConfigStore = request.app["store"]
    controller: Controller = request.app["controller"]
    device_id = str(data.get("device_id") or store.config.device.id)
    form = _connection_form(data, store.config)
    if controller.state.mqtt_ever_connected and device_id != store.config.device.id:
        return _render(
            request,
            "connection.html",
            session,
            error="device id is locked after the first connection",
            form=form,
        )
    secrets = dict(store.secrets)
    password = str(data.get("password") or "")
    if password:
        secrets["mqtt_password"] = password
    try:
        config = store.config.model_copy(
            update={
                "device": store.config.device.model_copy(
                    update={
                        "id": device_id,
                        "name": str(data.get("name") or store.config.device.name),
                    }
                ),
                "mqtt": store.config.mqtt.model_copy(
                    update={
                        "host": str(data.get("host") or ""),
                        "port": int(str(data.get("port") or "1883")),
                        "username": str(data.get("username") or "") or None,
                        "password": secrets.get("mqtt_password", ""),
                        "base_topic": str(data.get("base_topic") or "pikiosk"),
                        "discovery_prefix": str(data.get("discovery_prefix") or "homeassistant"),
                        "tls": store.config.mqtt.tls.model_copy(
                            update={"enabled": data.get("tls") == "on"}
                        ),
                    }
                ),
            }
        )
        config = KioskConfig.model_validate(config.model_dump())
    except (ValidationError, ConfigError, ValueError) as exc:
        return _render(request, "connection.html", session, error=str(exc), form=form)
    error = await _save(request, config, secrets)
    if error:
        return _render(request, "connection.html", session, error=error, form=form)
    raise _done("/connection")


async def connection_test(request: web.Request) -> web.Response:
    session = _require(request)
    assert isinstance(session, dict)
    data = await request.post()
    _check_csrf(request, data, session)
    store: ConfigStore = request.app["store"]
    form = _connection_form(data, store.config)
    notice = None
    error = None
    try:
        port = int(form["port"] or "1883")
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(form["host"], port), 3)
        writer.close()
        await writer.wait_closed()
        notice = "connection ok"
    except ValueError:
        error = "port must be a number"
    except OSError as exc:
        error = f"connection failed: {exc}"
    return _render(request, "connection.html", session, notice=notice, error=error, form=form)


async def catalog_page(request: web.Request) -> web.Response:
    session = _require(request)
    assert isinstance(session, dict)
    store: ConfigStore = request.app["store"]
    editing = request.query.get("edit") or None
    form: dict[str, Any] | None = None
    if editing:
        match = next((item for item in store.config.catalog.items if item.name == editing), None)
        if match is None:
            editing = None
        else:
            form = match.model_dump()
    return _render(request, "catalog.html", session, editing=editing, form=form)


def _catalog_item_form(data: Any) -> dict[str, Any]:
    """Build a catalog item dict from the add/edit form."""
    item: dict[str, Any] = {
        "name": str(data.get("name") or ""),
        "type": str(data.get("type") or "video"),
    }
    if data.get("id"):
        item["id"] = str(data.get("id")).strip()
    elif data.get("url"):
        item["url"] = str(data.get("url")).strip()
    elif data.get("path"):
        item["path"] = str(data.get("path")).strip()
    if data.get("start"):
        item["start"] = float(str(data.get("start")))
    if data.get("max_items"):
        item["max_items"] = int(str(data.get("max_items")))
    if data.get("thumbnail"):
        item["thumbnail"] = str(data.get("thumbnail")).strip()
    item["shuffle"] = data.get("shuffle") == "on"
    return item


async def catalog_post(request: web.Request) -> web.Response:
    session = _require(request)
    assert isinstance(session, dict)
    data = await request.post()
    _check_csrf(request, data, session)
    store: ConfigStore = request.app["store"]
    items = [item.model_dump() for item in store.config.catalog.items]
    action = str(data.get("action") or "add")
    name = str(data.get("name") or "")
    original = str(data.get("original") or "") or None
    editing = original if action == "add" else None
    form: dict[str, Any] | None = None
    if action == "delete":
        items = [item for item in items if item["name"] != name]
    elif action == "move":
        direction = -1 if data.get("direction") == "up" else 1
        index = next((i for i, item in enumerate(items) if item["name"] == name), -1)
        target = index + direction
        if index >= 0 and 0 <= target < len(items):
            items[index], items[target] = items[target], items[index]
    else:
        try:
            form = _catalog_item_form(data)
        except ValueError as exc:
            return _render(
                request, "catalog.html", session, error=f"invalid number: {exc}", editing=editing
            )
        index = next((i for i, item in enumerate(items) if item["name"] == original), -1)
        if original and index >= 0:
            # Editing keeps the row where it was; a rename must not collide.
            items = [
                existing
                for i, existing in enumerate(items)
                if i == index or existing["name"] != name
            ]
            index = next(i for i, item in enumerate(items) if item["name"] == original)
            items[index] = form
        else:
            items = [existing for existing in items if existing["name"] != name]
            items.append(form)
    try:
        catalog = CatalogConfig.model_validate({"items": items})
        config = store.config.model_copy(update={"catalog": catalog})
        config = KioskConfig.model_validate(config.model_dump())
    except (ValidationError, ValueError) as exc:
        return _render(request, "catalog.html", session, error=str(exc), editing=editing, form=form)
    error = await _save(request, config, dict(store.secrets))
    if error:
        return _render(request, "catalog.html", session, error=error, editing=editing, form=form)
    raise _done("/catalog")


async def playback_page(request: web.Request) -> web.Response:
    session = _require(request)
    assert isinstance(session, dict)
    return _render(request, "playback.html", session)


async def playback_post(request: web.Request) -> web.Response:
    session = _require(request)
    assert isinstance(session, dict)
    data = await request.post()
    _check_csrf(request, data, session)
    store: ConfigStore = request.app["store"]
    try:
        playback = store.config.playback.model_copy(
            update={
                "max_volume": int(str(data.get("max_volume"))),
                "default_volume": int(str(data.get("default_volume"))),
                "after_video": str(data.get("after_video") or "stop"),
                "allow_unlisted": data.get("allow_unlisted") == "on",
            }
        )
        config = store.config.model_copy(update={"playback": playback})
        config = KioskConfig.model_validate(config.model_dump())
    except (ValidationError, ValueError) as exc:
        return _render(request, "playback.html", session, error=str(exc))
    error = await _save(request, config, dict(store.secrets))
    if error:
        return _render(request, "playback.html", session, error=error)
    raise _done("/playback")


async def screen_page(request: web.Request) -> web.Response:
    session = _require(request)
    assert isinstance(session, dict)
    return _render(request, "screen.html", session)


async def screen_post(request: web.Request) -> web.Response:
    session = _require(request)
    assert isinstance(session, dict)
    data = await request.post()
    _check_csrf(request, data, session)
    store: ConfigStore = request.app["store"]
    try:
        screen = store.config.screen.model_copy(
            update={
                "default_brightness": int(str(data.get("default_brightness"))),
                "sleep_timeout_min": int(str(data.get("sleep_timeout_min"))),
                "rotation": int(str(data.get("rotation") or "0")),
            }
        )
        config = store.config.model_copy(update={"screen": screen})
        config = KioskConfig.model_validate(config.model_dump())
    except (ValidationError, ValueError) as exc:
        return _render(request, "screen.html", session, error=str(exc))
    error = await _save(request, config, dict(store.secrets))
    if error:
        return _render(request, "screen.html", session, error=error)
    raise _done("/screen")


async def updates_page(request: web.Request) -> web.Response:
    session = _require(request)
    assert isinstance(session, dict)
    return _render(request, "updates.html", session)


async def updates_post(request: web.Request) -> web.Response:
    session = _require(request)
    assert isinstance(session, dict)
    data = await request.post()
    _check_csrf(request, data, session)
    store: ConfigStore = request.app["store"]
    controller: Controller = request.app["controller"]
    update = store.config.update.model_copy(
        update={
            "auto": data.get("auto") == "on",
            "ref": str(data.get("ref") or "main"),
            "only_when_idle": data.get("only_when_idle") == "on",
        }
    )
    config = store.config.model_copy(update={"update": update})
    error = await _save(request, config, dict(store.secrets))
    if error:
        return _render(request, "updates.html", session, error=error)
    if data.get("action") == "now":
        await controller.cmd_update_app()
        raise _done("/updates", "Update started.")
    raise _done("/updates")


async def offline_page(request: web.Request) -> web.Response:
    session = _require(request)
    assert isinstance(session, dict)
    return _render(request, "offline.html", session)


async def offline_post(request: web.Request) -> web.Response:
    session = _require(request)
    assert isinstance(session, dict)
    data = await request.post()
    _check_csrf(request, data, session)
    store: ConfigStore = request.app["store"]
    controller: Controller = request.app["controller"]
    offline = store.config.offline.model_copy(
        update={
            "enabled": data.get("enabled") == "on",
            "max_size_gb": float(str(data.get("max_size_gb") or "20")),
            "download_window": str(data.get("download_window") or "") or None,
            "only_offline": data.get("only_offline") == "on",
        }
    )
    config = store.config.model_copy(update={"offline": offline})
    error = await _save(request, config, dict(store.secrets))
    if error:
        return _render(request, "offline.html", session, error=error)
    if data.get("action") == "sync":
        await controller.cmd_sync_offline()
        raise _done("/offline", "Sync started.")
    raise _done("/offline")


async def touch_page(request: web.Request) -> web.Response:
    session = _require(request)
    assert isinstance(session, dict)
    return _render(request, "touch.html", session)


async def touch_post(request: web.Request) -> web.Response:
    session = _require(request)
    assert isinstance(session, dict)
    data = await request.post()
    _check_csrf(request, data, session)
    store: ConfigStore = request.app["store"]
    ui = store.config.ui.model_copy(
        update={
            "columns": int(str(data.get("columns") or "3")),
            "show_titles": data.get("show_titles") == "on",
            "locked_screen": str(data.get("locked_screen") or "black"),
        }
    )
    config = store.config.model_copy(update={"ui": ui})
    error = await _save(request, config, dict(store.secrets))
    if error:
        return _render(request, "touch.html", session, error=error)
    raise _done("/touch")


async def health(request: web.Request) -> web.Response:
    controller: Controller = request.app["controller"]
    ok = controller.state.mqtt_connected and controller.state.mpv_connected
    return web.json_response(
        {"mqtt": controller.state.mqtt_connected, "mpv": controller.state.mpv_connected},
        status=200 if ok else 503,
    )


async def css(_request: web.Request) -> web.Response:
    file = STATIC / "admin.css"
    if not file.exists():
        return web.Response(text="", content_type="text/css")
    return web.Response(text=file.read_text(encoding="utf-8"), content_type="text/css")


def dump_preview(config: KioskConfig) -> str:
    return yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
