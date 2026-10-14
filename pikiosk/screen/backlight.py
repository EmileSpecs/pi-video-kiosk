"""sysfs backlight discovery and brightness mapping."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BacklightDevice:
    name: str
    path: Path
    max_brightness: int
    kind: str


def map_brightness(ha_brightness: int, max_brightness: int, min_percent: int) -> int:
    """Map a Home Assistant 0–255 brightness onto the panel's sysfs range."""
    if ha_brightness <= 0 or max_brightness <= 0:
        return 0
    floor = math.ceil(min_percent / 100 * max_brightness)
    mapped = round(ha_brightness / 255 * max_brightness)
    return max(mapped, floor)


def ha_from_sysfs(sysfs_value: int, max_brightness: int) -> int:
    if max_brightness <= 0:
        return 0
    return max(0, min(255, round(sysfs_value / max_brightness * 255)))


def discover_backlight(root: Path, preferred: str = "auto") -> BacklightDevice | None:
    if preferred and preferred != "auto":
        path = root / preferred
        if not path.is_dir():
            return None
        return _device(path, "configured")
    if not root.is_dir():
        return None
    entries = [path for path in root.iterdir() if path.is_dir()]
    if not entries:
        return None

    def score(path: Path) -> tuple[int, str]:
        device_link = path / "device"
        link_text = ""
        try:
            link_text = str(device_link.resolve()).lower()
        except OSError:
            link_text = path.name.lower()
        name = path.name.lower()
        blob = f"{name} {link_text}"
        if "dsi" in blob or "0045" in blob or "10-0045" in blob:
            return (0, name)
        if "rpi_backlight" in blob or "backlight" in name:
            return (1, name)
        return (2, name)

    if len(entries) == 1:
        return _device(entries[0], "only")
    chosen = sorted(entries, key=score)[0]
    kind = "dsi" if score(chosen)[0] == 0 else "named"
    return _device(chosen, kind)


def _device(path: Path, kind: str) -> BacklightDevice:
    max_path = path / "max_brightness"
    try:
        max_brightness = int(max_path.read_text(encoding="utf-8").strip() or "255")
    except (OSError, ValueError):
        max_brightness = 255
    return BacklightDevice(path.name, path, max(1, max_brightness), kind)


class Backlight:
    def __init__(self, device: BacklightDevice | None) -> None:
        self.device = device
        self._last_written = 0

    @property
    def available(self) -> bool:
        return self.device is not None

    def read_actual(self) -> int | None:
        if self.device is None:
            return None
        for name in ("actual_brightness", "brightness"):
            file = self.device.path / name
            try:
                return int(file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
        return self._last_written

    def write(self, brightness: int, power_on: bool) -> None:
        if self.device is None:
            return
        bl_power = self.device.path / "bl_power"
        bright = self.device.path / "brightness"
        if power_on:
            _write(bl_power, "0")
            _write(bright, str(brightness))
            self._last_written = brightness
        else:
            _write(bl_power, "4")


def _write(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
