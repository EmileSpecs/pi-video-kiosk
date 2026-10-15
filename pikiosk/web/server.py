"""Loopback child UI and JSON API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import WSMsgType, web

from pikiosk.catalog import VIDEO_ID_RE, CatalogError, parse_ref, thumbnail_url
from pikiosk.controller import Controller
from pikiosk.state import KioskState, picture_gain

log = logging.getLogger("pikiosk.web")
STATIC = Path(__file__).parent / "static"
ThumbFetcher = Callable[[str], Awaitable[bytes | None]]


def catalog_version(items: list[dict[str, object]]) -> str:
    """Short fingerprint of the catalog so the grid knows when to re-fetch."""
    text = json.dumps(items, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def state_view(state: KioskState, version: str = "") -> dict[str, Any]:
    current = state.now
    return {
        "player": state.player.value,
        "selected": state.selected,
        "ui_locked": state.ui_locked,
        "screen_on": state.screen_on,
        "dim": round(1 - picture_gain(state), 3),
        "title": current.title if current else "",
        "volume": state.volume,
        "muted": state.muted,
        "thumbnail": current.thumbnail_url if current else None,
        "last_error": state.last_error,
        "error_detail": state.error_detail,
        "download_progress": state.download_progress,
        "playlist_index": current.playlist_index if current else None,
        "playlist_count": current.playlist_count if current else None,
        "catalog_version": version,
    }


def item_video_id(item: dict[str, object]) -> str | None:
    if item.get("type") != "video":
        return None
    if item.get("id"):
        return str(item["id"])
    if item.get("url"):
        try:
            ref = parse_ref(str(item["url"]))
        except CatalogError:
            return None
        return ref.id if ref.kind == "video" else None
    return None


def create_child_app(controller: Controller, *, fetch_thumb: ThumbFetcher | None = None) -> web.Application:
    app = web.Application()
    app["controller"] = controller
    app["fetch_thumb"] = fetch_thumb or _default_fetch
    app.router.add_get("/", _index)
    app.router.add_get("/ws", _ws)
    app.router.add_get("/api/ui", _ui)
    app.router.add_get("/api/catalog", _catalog)
    app.router.add_get("/api/state", _state)
    app.router.add_post("/api/play", _play)
    app.router.add_post("/api/stop", _stop)
    app.router.add_post("/api/unlock", _unlock)
    app.router.add_post("/api/command", _command)
    app.router.add_get("/thumbs/{video_id}.jpg", _thumb)
    app.router.add_get("/healthz", _health)
    app.router.add_static("/static/", STATIC, show_index=False)
    return app


def _version(controller: Controller) -> str:
    return catalog_version(controller.catalog.items)


async def _index(_request: web.Request) -> web.StreamResponse:
    page = STATIC / "index.html"
    return web.FileResponse(page, headers={"Cache-Control": "no-cache"})


async def _ui(request: web.Request) -> web.Response:
    controller: Controller = request.app["controller"]
    ui = controller.config.ui
    return web.json_response(
        {
            "columns": ui.columns,
            "show_titles": ui.show_titles,
            "locked_screen": ui.locked_screen,
            "parent_unlock": ui.parent_unlock.enabled,
            "hold_corner": ui.parent_unlock.hold_corner,
            "hold_s": ui.parent_unlock.hold_s,
            "device_name": controller.config.device.name,
            "catalog_version": _version(controller),
        }
    )


async def _state(request: web.Request) -> web.Response:
    controller: Controller = request.app["controller"]
    return web.json_response(state_view(controller.state, _version(controller)))


async def _catalog(request: web.Request) -> web.Response:
    controller: Controller = request.app["controller"]
    items = []
    for index, item in enumerate(controller.catalog.items):
        video_id = item_video_id(item)
        thumb = controller.catalog.item_thumbnail(item)
        if video_id and not item.get("thumbnail"):
            thumb = f"/thumbs/{video_id}.jpg"
        items.append(
            {
                "name": item["name"],
                "type": item["type"],
                "thumb": thumb,
                "index": index,
                "video_id": video_id,
                "cached": bool(video_id and controller.cache_lookup and controller.cache_lookup(video_id)),
            }
        )
    return web.json_response(items, headers={"X-Catalog-Version": _version(controller)})


async def _default_fetch(url: str) -> bytes | None:
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.get(url) as response:
            if response.status != 200:
                return None
            return await response.read()
    except (aiohttp.ClientError, OSError, TimeoutError):
        return None
    except Exception:
        log.debug("thumbnail fetch failed url=%s", url, exc_info=True)
        return None


async def _thumb(request: web.Request) -> web.StreamResponse:
    """YouTube thumbnail, kept on disk so the grid still has pictures offline."""
    controller: Controller = request.app["controller"]
    video_id = request.match_info["video_id"]
    if not VIDEO_ID_RE.match(video_id):
        raise web.HTTPNotFound()
    folder = Path(controller.config.offline.directory).expanduser() / "thumbs"
    file = folder / f"{video_id}.jpg"
    headers = {"Cache-Control": "public, max-age=86400"}
    if file.is_file() and file.stat().st_size > 0:
        return web.FileResponse(file, headers=headers)
    fetch: ThumbFetcher = request.app["fetch_thumb"]
    # hq720 is 16:9 without letterbox bars but not every video has one;
    # hqdefault always exists.
    data = await fetch(f"https://i.ytimg.com/vi/{video_id}/hq720.jpg")
    if not data:
        data = await fetch(thumbnail_url(video_id))
    if not data:
        raise web.HTTPNotFound()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = file.with_suffix(".jpg.part")
        tmp.write_bytes(data)
        tmp.replace(file)
    except OSError:
        log.debug("thumbnail not cached id=%s", video_id, exc_info=True)
    return web.Response(body=data, content_type="image/jpeg", headers=headers)


async def _play(request: web.Request) -> web.Response:
    controller: Controller = request.app["controller"]
    if controller.state.ui_locked and not controller.temporarily_unlocked():
        return web.json_response({"error": "locked"}, status=403)
    now = controller.clock.monotonic()
    if now < controller._play_lock_until:
        return web.json_response({"error": "debounce"}, status=429)
    body = await _read_json(request)
    if body is None:
        return web.json_response({"error": "bad_json"}, status=400)
    name = str(body.get("name") or "")
    if name not in controller.catalog.by_name:
        return web.json_response({"error": "not_allowed"}, status=403)
    controller._play_lock_until = now + 2
    await controller.dispatch("select", {"name": name})
    return web.json_response(state_view(controller.state, _version(controller)))


async def _stop(request: web.Request) -> web.Response:
    controller: Controller = request.app["controller"]
    if controller.state.ui_locked and not controller.temporarily_unlocked():
        return web.json_response({"error": "locked"}, status=403)
    await controller.dispatch("stop", {})
    return web.json_response(state_view(controller.state, _version(controller)))


async def _unlock(request: web.Request) -> web.Response:
    controller: Controller = request.app["controller"]
    if not controller.config.ui.parent_unlock.enabled:
        return web.json_response({"error": "disabled"}, status=403)
    body = await _read_json(request)
    if body is None:
        return web.json_response({"error": "bad_json"}, status=400)
    if controller.clock.monotonic() < controller._pin_locked_until:
        return web.json_response({"error": "cooldown"}, status=429)
    ok = controller.check_pin(str(body.get("pin") or ""))
    status = 200 if ok else 403
    return web.json_response({"ok": ok}, status=status)


async def _command(request: web.Request) -> web.Response:
    controller: Controller = request.app["controller"]
    body = await _read_json(request)
    if body is None or "command" not in body:
        return web.json_response({"error": "bad_json"}, status=400)
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    await controller.dispatch(str(body["command"]), args)
    return web.json_response(state_view(controller.state, _version(controller)))


async def _health(request: web.Request) -> web.Response:
    controller: Controller = request.app["controller"]
    body = {"mqtt": controller.state.mqtt_connected, "mpv": controller.state.mpv_connected}
    return web.json_response(body, status=200 if body["mqtt"] and body["mpv"] else 503)


async def _ws(request: web.Request) -> web.WebSocketResponse:
    controller: Controller = request.app["controller"]
    socket = web.WebSocketResponse(heartbeat=30)
    await socket.prepare(request)
    await socket.send_json(state_view(controller.state, _version(controller)))

    def push(state: KioskState) -> None:
        if socket.closed:
            return
        task = socket.send_json(state_view(state, _version(controller)))
        asyncio.get_running_loop().create_task(task)

    controller.subscribe(push)
    try:
        async for message in socket:
            if message.type == WSMsgType.TEXT and message.data == "heartbeat":
                continue
            if message.type in {WSMsgType.CLOSE, WSMsgType.ERROR}:
                break
    finally:
        if push in controller._listeners:
            controller._listeners.remove(push)
    return socket


async def _read_json(request: web.Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    return body
