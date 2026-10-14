"""Home Assistant MQTT discovery payloads, built from one entity table."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pikiosk import __version__
from pikiosk.config import KioskConfig
from pikiosk.mqtt.topics import Topics


@dataclass(frozen=True)
class DiscoveryContext:
    ip: str = "192.168.1.57"
    hw_version: str = "Raspberry Pi 5 Model B Rev 1.0 / Touch Display 2"
    sw_version: str = __version__
    support_url: str = "https://github.com/owner/pi-kiosk"


def _device(config: KioskConfig, ctx: DiscoveryContext) -> dict[str, Any]:
    device: dict[str, Any] = {
        "identifiers": [f"pikiosk_{config.device.id}"],
        "name": config.device.name,
        "manufacturer": config.device.manufacturer,
        "model": config.device.model,
        "sw_version": ctx.sw_version,
        "hw_version": ctx.hw_version,
    }
    if config.device.suggested_area:
        device["suggested_area"] = config.device.suggested_area
    if config.admin.enabled and ctx.ip:
        device["configuration_url"] = f"http://{ctx.ip}:{config.admin.port}"
    return device


def _shared(config: KioskConfig, ctx: DiscoveryContext, *, player: bool) -> dict[str, Any]:
    topics = Topics(config)
    availability: list[dict[str, str]] = [{"topic": topics.status()}]
    if player:
        availability.append({"topic": topics.player_status()})
    return {
        "availability": availability,
        "availability_mode": "all",
        "device": _device(config, ctx),
        "origin": {
            "name": "pi-kiosk",
            "sw_version": ctx.sw_version,
            "support_url": ctx.support_url or config.support_url(),
        },
        "has_entity_name": True,
        "qos": 1,
    }


def _identity(config: KioskConfig, component: str, object_id: str, name: str) -> dict[str, Any]:
    device_id = config.device.id
    payload: dict[str, Any] = {
        "name": name,
        "unique_id": f"pikiosk_{device_id}_{object_id}",
    }
    entity_id = f"{component}.{device_id}_{object_id}"
    if config.mqtt.legacy_object_id:
        payload["object_id"] = f"{device_id}_{object_id}"
    else:
        payload["default_entity_id"] = entity_id
    return payload


def build_component_payloads(
    config: KioskConfig,
    ctx: DiscoveryContext | None = None,
) -> dict[str, dict[str, Any]]:
    context = ctx or DiscoveryContext(support_url=config.support_url())
    topics = Topics(config)
    state = topics.state()
    options = [item.name for item in config.catalog.items]
    built: dict[str, dict[str, Any]] = {}

    def add(component: str, object_id: str, body: dict[str, Any], *, player: bool) -> None:
        payload = _shared(config, context, player=player)
        payload.update(body)
        built[topics.discovery_topic(component, object_id)] = payload

    buttons = [
        ("play", "Play", "mdi:play"),
        ("pause", "Pause", "mdi:pause"),
        ("stop", "Stop", "mdi:stop"),
        ("next", "Next", "mdi:skip-next"),
        ("previous", "Previous", "mdi:skip-previous"),
    ]
    for object_id, name, icon in buttons:
        add(
            "button",
            object_id,
            {
                **_identity(config, "button", object_id, name),
                "command_topic": topics.command(object_id),
                "payload_press": "PRESS",
                "icon": icon,
            },
            player=True,
        )
    add(
        "select",
        "video",
        {
            **_identity(config, "select", "video", "Video"),
            "command_topic": topics.command("select"),
            "state_topic": state,
            "value_template": "{{ value_json.selected }}",
            "options": options,
            "icon": "mdi:playlist-play",
        },
        player=True,
    )
    add(
        "text",
        "play_media",
        {
            **_identity(config, "text", "play_media", "Play URL or ID"),
            "command_topic": topics.command("play_media"),
            "state_topic": state,
            "value_template": "{{ value_json.media_url or '' }}",
            "min": 0,
            "max": 255,
            "mode": "text",
            "icon": "mdi:youtube",
        },
        player=True,
    )
    add(
        "number",
        "volume",
        {
            **_identity(config, "number", "volume", "Volume"),
            "command_topic": topics.command("volume"),
            "state_topic": state,
            "value_template": "{{ value_json.volume }}",
            "min": 0,
            "max": config.playback.max_volume,
            "step": 1,
            "mode": "slider",
            "unit_of_measurement": "%",
            "icon": "mdi:volume-high",
        },
        player=False,
    )
    add(
        "switch",
        "mute",
        {
            **_identity(config, "switch", "mute", "Mute"),
            "command_topic": topics.command("mute"),
            "state_topic": state,
            "value_template": "{{ 'ON' if value_json.muted else 'OFF' }}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "state_on": "ON",
            "state_off": "OFF",
            "icon": "mdi:volume-off",
        },
        player=False,
    )
    add(
        "light",
        "screen",
        {
            **_identity(config, "light", "screen", "Screen"),
            "schema": "json",
            "command_topic": topics.command("screen"),
            "state_topic": topics.screen(),
            "brightness": True,
            "brightness_scale": 255,
            "supported_color_modes": ["brightness"],
            "icon": "mdi:tablet",
        },
        player=False,
    )
    add(
        "number",
        "brightness",
        {
            **_identity(config, "number", "brightness", "Brightness"),
            "command_topic": topics.command("brightness"),
            "state_topic": topics.screen(),
            "value_template": "{{ (value_json.brightness | float(0) / 255 * 100) | round(0) }}",
            "min": 1,
            "max": 100,
            "step": 1,
            "mode": "slider",
            "unit_of_measurement": "%",
            "icon": "mdi:brightness-6",
        },
        player=False,
    )
    add(
        "number",
        "sleep_timeout",
        {
            **_identity(config, "number", "sleep_timeout", "Sleep timeout"),
            "command_topic": topics.command("sleep_timeout"),
            "state_topic": state,
            "value_template": "{{ value_json.sleep_timeout }}",
            "min": 1,
            "max": 240,
            "step": 1,
            "mode": "box",
            "unit_of_measurement": "min",
            "entity_category": "config",
            "icon": "mdi:timer-sand",
        },
        player=False,
    )
    add(
        "switch",
        "sleep_enabled",
        {
            **_identity(config, "switch", "sleep_enabled", "Screen sleep"),
            "command_topic": topics.command("sleep_enabled"),
            "state_topic": state,
            "value_template": "{{ 'ON' if value_json.sleep_enabled else 'OFF' }}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "entity_category": "config",
            "icon": "mdi:sleep",
        },
        player=False,
    )
    add(
        "select",
        "after_video",
        {
            **_identity(config, "select", "after_video", "After video"),
            "command_topic": topics.command("after_video"),
            "state_topic": state,
            "value_template": "{{ value_json.after_video }}",
            "options": ["loop", "next", "stop", "blank"],
            "entity_category": "config",
            "icon": "mdi:repeat",
        },
        player=False,
    )
    add(
        "switch",
        "ui_enabled",
        {
            **_identity(config, "switch", "ui_enabled", "Touch UI"),
            "command_topic": topics.command("ui_enabled"),
            "state_topic": state,
            "value_template": "{{ 'ON' if value_json.ui_enabled else 'OFF' }}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "icon": "mdi:gesture-tap-button",
        },
        player=False,
    )
    add(
        "sensor",
        "state",
        {
            **_identity(config, "sensor", "state", "State"),
            "state_topic": state,
            "value_template": "{{ value_json.state }}",
            "device_class": "enum",
            "options": ["idle", "loading", "playing", "paused", "error"],
            "json_attributes_topic": state,
            "json_attributes_template": (
                "{{ {'media_id': value_json.media_id, 'source': value_json.source, "
                "'playlist_index': value_json.playlist_index, "
                "'playlist_count': value_json.playlist_count, "
                "'offline': value_json.offline, 'last_error': value_json.last_error} | tojson }}"
            ),
            "icon": "mdi:television-play",
        },
        player=True,
    )
    add(
        "sensor",
        "title",
        {
            **_identity(config, "sensor", "title", "Title"),
            "state_topic": state,
            "value_template": "{{ value_json.title[:255] if value_json.title else '' }}",
            "icon": "mdi:format-title",
        },
        player=True,
    )
    add(
        "sensor",
        "position",
        {
            **_identity(config, "sensor", "position", "Position"),
            "state_topic": state,
            "value_template": (
                "{{ value_json.position | round(0) if value_json.position is not none else none }}"
            ),
            "device_class": "duration",
            "unit_of_measurement": "s",
            "icon": "mdi:timer-outline",
        },
        player=True,
    )
    add(
        "sensor",
        "duration",
        {
            **_identity(config, "sensor", "duration", "Duration"),
            "state_topic": state,
            "value_template": (
                "{{ value_json.duration | round(0) if value_json.duration is not none else none }}"
            ),
            "device_class": "duration",
            "unit_of_measurement": "s",
            "icon": "mdi:timer-outline",
        },
        player=True,
    )
    add(
        "sensor",
        "thumbnail",
        {
            **_identity(config, "sensor", "thumbnail", "Thumbnail URL"),
            "state_topic": topics.thumbnail(),
            "icon": "mdi:image",
        },
        player=True,
    )
    add(
        "sensor",
        "cpu_temp",
        {
            **_identity(config, "sensor", "cpu_temp", "CPU temperature"),
            "state_topic": topics.telemetry(),
            "value_template": "{{ value_json.cpu_temp }}",
            "device_class": "temperature",
            "unit_of_measurement": "°C",
            "state_class": "measurement",
            "suggested_display_precision": 1,
            "entity_category": "diagnostic",
        },
        player=False,
    )
    add(
        "sensor",
        "uptime",
        {
            **_identity(config, "sensor", "uptime", "Uptime"),
            "state_topic": topics.telemetry(),
            "value_template": "{{ value_json.boot_time }}",
            "device_class": "timestamp",
            "entity_category": "diagnostic",
            "icon": "mdi:clock-start",
        },
        player=False,
    )
    add(
        "sensor",
        "ip",
        {
            **_identity(config, "sensor", "ip", "IP address"),
            "state_topic": topics.telemetry(),
            "value_template": "{{ value_json.ip }}",
            "entity_category": "diagnostic",
            "icon": "mdi:ip-network",
        },
        player=False,
    )
    add(
        "sensor",
        "ytdlp_version",
        {
            **_identity(config, "sensor", "ytdlp_version", "yt-dlp version"),
            "state_topic": topics.telemetry(),
            "value_template": "{{ value_json.ytdlp_version }}",
            "entity_category": "diagnostic",
            "icon": "mdi:update",
        },
        player=False,
    )
    add(
        "sensor",
        "last_error",
        {
            **_identity(config, "sensor", "last_error", "Last error"),
            "state_topic": state,
            "value_template": "{{ value_json.last_error or '' }}",
            "entity_category": "diagnostic",
            "icon": "mdi:alert",
        },
        player=False,
    )
    add(
        "sensor",
        "cache_used",
        {
            **_identity(config, "sensor", "cache_used", "Cache used"),
            "state_topic": topics.telemetry(),
            "value_template": "{{ value_json.cache_used_gb }}",
            "unit_of_measurement": "GB",
            "entity_category": "diagnostic",
            "icon": "mdi:harddisk",
        },
        player=False,
    )
    maintenance = [
        ("reload_config", "Reload config", "mdi:refresh"),
        ("update_ytdlp", "Update yt-dlp", "mdi:update"),
        ("sync_offline", "Sync offline", "mdi:download"),
        ("restart", "Restart", "mdi:restart"),
        ("update_app", "Update app", "mdi:github"),
    ]
    for object_id, name, icon in maintenance:
        add(
            "button",
            object_id,
            {
                **_identity(config, "button", object_id, name),
                "command_topic": topics.command(object_id),
                "payload_press": "PRESS",
                "entity_category": "config",
                "icon": icon,
            },
            player=False,
        )
    if config.mqtt.image_entity:
        add(
            "image",
            "thumbnail_image",
            {
                **_identity(config, "image", "thumbnail_image", "Thumbnail"),
                "url_topic": f"{topics.base}/thumbnail_url",
                "icon": "mdi:image",
            },
            player=True,
        )
    return built


def build_device_payload(
    config: KioskConfig,
    ctx: DiscoveryContext | None = None,
) -> dict[str, Any]:
    components = build_component_payloads(config, ctx)
    cmps: dict[str, Any] = {}
    for topic, payload in components.items():
        object_id = topic.rstrip("/").split("/")[-2]
        component = topic.rstrip("/").split("/")[-4]
        body = {
            key: value
            for key, value in payload.items()
            if key not in {"availability", "availability_mode", "device", "origin", "qos"}
        }
        body["p"] = component
        cmps[object_id] = body
    context = ctx or DiscoveryContext(support_url=config.support_url())
    return {
        "device": _device(config, context),
        "origin": {
            "name": "pi-kiosk",
            "sw_version": context.sw_version,
            "support_url": context.support_url or config.support_url(),
        },
        "cmps": cmps,
        "qos": 1,
    }
