"""Loopback child UI and JSON API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from pikiosk.controller import Controller
from pikiosk.state import KioskState

STATIC = Path(__file__).parent / "static"


def state_view(state: KioskState) -> dict[str, Any]:
    current = state.now
    return {
        "player": state.player.value,
        "selected": state.selected,
        "ui_locked": state.ui_locked,
        "screen_on": state.screen_on,
        "title": current.title if current else "",
        "volume": state.volume,
        "muted": state.muted,
        "thumbnail": current.thumbnail_url if current else None,
        "last_error": state.last_error,
    }


def create_child_app(controller: Controller) -> web.Application:
    app = web.Application()
    app["controller"] = controller
    app.router.add_get("/", _index)
    app.router.add_get("/ws", _ws)
    app.router.add_get("/api/catalog", _catalog)
    app.router.add_get("/api/state", _state)
    app.router.add_post("/api/play", _play)
    app.router.add_post("/api/stop", _stop)
    app.router.add_post("/api/unlock", _unlock)
    app.router.add_post("/api/command", _command)
    app.router.add_get("/healthz", _health)
    app.router.add_static("/static/", STATIC, show_index=False)
    return app


async def _index(_request: web.Request) -> web.StreamResponse:
    page = STATIC / "index.html"
    return web.FileResponse(page)


async def _state(request: web.Request) -> web.Response:
    controller: Controller = request.app["controller"]
    return web.json_response(state_view(controller.state))


async def _catalog(request: web.Request) -> web.Response:
    controller: Controller = request.app["controller"]
    items = []
    for index, item in enumerate(controller.catalog.items):
        items.append(
            {
                "name": item["name"],
                "type": item["type"],
                "thumb": controller.catalog.item_thumbnail(item),
                "index": index,
            }
        )
    return web.json_response(items)


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
    return web.json_response(state_view(controller.state))


async def _stop(request: web.Request) -> web.Response:
    controller: Controller = request.app["controller"]
    if controller.state.ui_locked and not controller.temporarily_unlocked():
        return web.json_response({"error": "locked"}, status=403)
    await controller.dispatch("stop", {})
    return web.json_response(state_view(controller.state))


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
    return web.json_response(state_view(controller.state))


async def _health(request: web.Request) -> web.Response:
    controller: Controller = request.app["controller"]
    body = {"mqtt": controller.state.mqtt_connected, "mpv": controller.state.mpv_connected}
    return web.json_response(body, status=200 if body["mqtt"] and body["mpv"] else 503)


async def _ws(request: web.Request) -> web.WebSocketResponse:
    controller: Controller = request.app["controller"]
    socket = web.WebSocketResponse(heartbeat=30)
    await socket.prepare(request)
    await socket.send_json(state_view(controller.state))

    def push(state: KioskState) -> None:
        if socket.closed:
            return
        task = socket.send_json(state_view(state))
        request.loop.create_task(task)

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
