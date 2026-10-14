"""Single place for topic names."""

from __future__ import annotations

from pikiosk.config import KioskConfig


class Topics:
    def __init__(self, config: KioskConfig) -> None:
        self.base = f"{config.mqtt.base_topic}/{config.device.id}"
        self.discovery = config.mqtt.discovery_prefix
        self.device_id = config.device.id
        self.node = f"pikiosk_{config.device.id}"

    def status(self) -> str:
        return f"{self.base}/status"

    def player_status(self) -> str:
        return f"{self.base}/player/status"

    def state(self) -> str:
        return f"{self.base}/state"

    def screen(self) -> str:
        return f"{self.base}/screen/state"

    def telemetry(self) -> str:
        return f"{self.base}/telemetry"

    def event(self) -> str:
        return f"{self.base}/event"

    def command(self, name: str) -> str:
        return f"{self.base}/cmd/{name}"

    def birth(self) -> str:
        return f"{self.discovery}/status"

    def discovery_topic(self, component: str, object_id: str) -> str:
        return f"{self.discovery}/{component}/{self.node}/{object_id}/config"

    def device_discovery_topic(self) -> str:
        return f"{self.discovery}/device/{self.node}/config"
