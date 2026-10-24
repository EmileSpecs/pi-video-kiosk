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
    # Extra dim: backlight at its lowest step, and the brightness slider
    # (dim_level, 1..100 %) darkens the picture below that instead.
    extra_dim: bool = False
    dim_level: int = 50
    grayscale: bool = False  # mpv saturation -100; touch grid CSS filter
    play_limit_enabled: bool = False
    play_limit_min: int = 30
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


DIMMEST_LIGHT = 0.1  # extra dim at 1 % lets through a tenth of the backlight's lowest light


def percent_to_ha(percent: int) -> int:
    return max(0, min(255, round(percent / 100 * 255)))


def picture_gain(state: KioskState) -> float:
    """Signal multiplier for the picture: 1.0 unless extra dim is on.

    At 100 % the picture is untouched (the backlight's lowest step alone); at
    1 % it gives a tenth of that light. Pixel values are gamma encoded, so
    light goes roughly with the value to the power 2.2.
    """
    if not state.extra_dim:
        return 1.0
    level = max(1, min(100, int(state.dim_level)))
    light = DIMMEST_LIGHT + (1 - DIMMEST_LIGHT) * (level - 1) / 99
    return float(light ** (1 / 2.2))


def shown_brightness(state: KioskState) -> int:
    """What the brightness slider shows: the dim level while extra dim is on."""
    return percent_to_ha(state.dim_level) if state.extra_dim else state.brightness
