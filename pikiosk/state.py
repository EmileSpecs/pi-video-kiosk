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
    extra_dim: int = 0  # 0..MAX_EXTRA_DIM percent darker than the backlight alone
    sleep_enabled: bool = True
    sleep_timeout_min: int = 10
    ui_locked: bool = False
    selected: str | None = None
    last_error: str | None = None
    error_detail: str | None = None
    download_progress: int | None = None
    mpv_connected: bool = False
    mpv_error: str | None = None
    mqtt_connected: bool = False
    mqtt_ever_connected: bool = False
    mqtt_error: str | None = None


MAX_EXTRA_DIM = 90


def percent_to_ha(percent: int) -> int:
    return max(0, min(255, round(percent / 100 * 255)))


def extra_dim_gain(percent: int) -> float:
    """Signal multiplier that makes the picture `percent` darker to the eye.

    Pixel values are gamma encoded, so light output goes roughly with the
    value to the power 2.2; scaling values by this gain cuts light by `percent`.
    """
    percent = max(0, min(MAX_EXTRA_DIM, int(percent)))
    return float((1 - percent / 100) ** (1 / 2.2))
