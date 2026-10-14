"""Immutable kiosk state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class PlayerState(str, Enum):
    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"
    ERROR = "error"
    OFFLINE = "offline"


class AfterVideo(str, Enum):
    LOOP = "loop"
    NEXT = "next"
    STOP = "stop"
    BLANK = "blank"


@dataclass(frozen=True)
class NowPlaying:
    source: Literal["catalog", "adhoc", "file"]
    catalog_name: str | None
    video_id: str | None
    playlist_id: str | None
    playlist_index: int | None
    playlist_count: int | None
    title: str
    duration: float | None
    position: float | None
    thumbnail_url: str | None
    local_file: str | None


@dataclass(frozen=True)
class KioskState:
    player: PlayerState = PlayerState.IDLE
    now: NowPlaying | None = None
    volume: int = 40
    muted: bool = False
    after_video: AfterVideo = AfterVideo.STOP
    screen_on: bool = True
    brightness: int = 178
    sleep_enabled: bool = True
    sleep_timeout_min: int = 10
    ui_locked: bool = False
    selected: str | None = None
    last_error: str | None = None
    error_detail: str | None = None
    mpv_connected: bool = False
    mpv_error: str | None = None
    mqtt_connected: bool = False
    mqtt_ever_connected: bool = False
    mqtt_error: str | None = None


def percent_to_ha(percent: int) -> int:
    return max(0, min(255, round(percent / 100 * 255)))
