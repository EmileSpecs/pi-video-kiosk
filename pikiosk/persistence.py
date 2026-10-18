"""Atomic JSON state file."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pikiosk.config import SCHEMA_VERSION, atomic_write_text
from pikiosk.state import AfterVideo, KioskState, PlayerState

PERSISTED = (
    "volume",
    "muted",
    "brightness",
    "extra_dim",
    "dim_level",
    "grayscale",
    "screen_on",
    "sleep_enabled",
    "sleep_timeout_min",
    "after_video",
    "ui_locked",
    "selected",
    "player",
    "mqtt_ever_connected",
)


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.last_item: dict[str, Any] | None = None
        self.resume_positions: dict[str, float] = {}
        self.published_discovery_topics: list[str] = []
        self.schema_version = SCHEMA_VERSION

    def load(self, base: KioskState) -> KioskState:
        if not self.path.exists():
            return base
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            bad = self.path.with_name(self.path.name + ".bad")
            try:
                self.path.replace(bad)
            except OSError:
                pass
            return base
        if not isinstance(raw, dict):
            self.path.replace(self.path.with_name(self.path.name + ".bad"))
            return base
        self.schema_version = int(raw.get("schema_version") or SCHEMA_VERSION)
        self.last_item = raw.get("last_item") if isinstance(raw.get("last_item"), dict) else None
        positions = raw.get("resume_positions")
        if isinstance(positions, dict):
            self.resume_positions = {
                str(key): float(value)
                for key, value in positions.items()
                if isinstance(value, (int, float))
            }
        topics = raw.get("published_discovery_topics")
        if isinstance(topics, list):
            self.published_discovery_topics = [str(item) for item in topics]
        data = dict(base.__dict__)
        for key in PERSISTED:
            if key not in raw or raw[key] is None:
                continue
            value = raw[key]
            if key == "after_video":
                value = AfterVideo(str(value))
            elif key == "extra_dim":
                value = bool(value)  # briefly stored as a percentage
            elif key == "player":
                try:
                    value = PlayerState(str(value))
                except ValueError:
                    continue
            data[key] = value
        return KioskState(**data)

    def snapshot(self, state: KioskState) -> dict[str, Any]:
        payload: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
        raw = asdict(state)
        for key in PERSISTED:
            value = raw[key]
            if isinstance(value, (PlayerState, AfterVideo)):
                value = value.value
            payload[key] = value
        payload["last_item"] = self.last_item
        payload["resume_positions"] = dict(list(self.resume_positions.items())[-200:])
        payload["published_discovery_topics"] = list(self.published_discovery_topics)
        return payload

    def save(self, state: KioskState) -> None:
        text = json.dumps(self.snapshot(state), separators=(",", ":"))
        atomic_write_text(self.path, text)

    def remember_position(self, video_id: str | None, position: float | None) -> None:
        if not video_id or position is None:
            return
        self.resume_positions[video_id] = position
        if len(self.resume_positions) > 200:
            overflow = len(self.resume_positions) - 200
            for key in list(self.resume_positions)[:overflow]:
                del self.resume_positions[key]
