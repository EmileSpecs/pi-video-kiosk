"""Retained state documents published to MQTT."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pikiosk.state import KioskState, PlayerState


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def player_payload(state: KioskState, *, now: datetime | None = None) -> dict[str, Any]:
    moment = now or datetime.now(timezone.utc)
    current = state.now
    visible = state.player.value
    if state.player == PlayerState.OFFLINE:
        visible = "idle"
    title = ""
    position = None
    duration = None
    thumbnail = None
    media_id = None
    media_url = None
    source = None
    playlist_index = None
    playlist_count = None
    offline = False
    if current is not None and state.player not in {PlayerState.IDLE, PlayerState.OFFLINE}:
        title = current.title
        position = current.position
        duration = current.duration
        thumbnail = current.thumbnail_url
        media_id = current.video_id
        media_url = (
            f"https://www.youtube.com/watch?v={current.video_id}" if current.video_id else None
        )
        if current.playlist_id and not current.video_id:
            media_url = f"https://www.youtube.com/playlist?list={current.playlist_id}"
        source = current.source
        playlist_index = current.playlist_index
        playlist_count = current.playlist_count
        offline = current.local_file is not None
    return {
        "state": visible,
        "title": title,
        "position": position,
        "duration": duration,
        "thumbnail": thumbnail,
        "media_id": media_id,
        "media_url": media_url,
        "source": source,
        "selected": state.selected,
        "playlist_index": playlist_index,
        "playlist_count": playlist_count,
        "offline": offline,
        "volume": state.volume,
        "muted": state.muted,
        "after_video": state.after_video.value,
        "sleep_enabled": state.sleep_enabled,
        "sleep_timeout": state.sleep_timeout_min,
        "ui_enabled": not state.ui_locked,
        "last_error": state.last_error,
        "updated": _iso(moment),
    }


def screen_payload(state: KioskState) -> dict[str, Any]:
    return {
        "state": "ON" if state.screen_on else "OFF",
        "brightness": state.brightness,
        "color_mode": "brightness",
    }


def without_position(payload: dict[str, Any]) -> dict[str, Any]:
    copy = dict(payload)
    copy.pop("position", None)
    copy.pop("updated", None)
    return copy
