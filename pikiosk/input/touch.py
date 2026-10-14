"""evdev touch monitor. The grab/release policy is testable without the device."""

from __future__ import annotations

import logging
from typing import Protocol

from pikiosk.clock import Clock
from pikiosk.config import KioskConfig
from pikiosk.controller import Controller
from pikiosk.screen.screen_service import ScreenService

log = logging.getLogger("pikiosk.input")


class TouchDevice(Protocol):
    def grab(self) -> None: ...

    def ungrab(self) -> None: ...


class FakeTouch:
    def __init__(self) -> None:
        self.grabbed = False
        self.events: list[str] = []

    def grab(self) -> None:
        self.grabbed = True
        self.events.append("grab")

    def ungrab(self) -> None:
        self.grabbed = False
        self.events.append("ungrab")


class InputMonitor:
    def __init__(
        self,
        config: KioskConfig,
        screen: ScreenService,
        controller: Controller,
        clock: Clock,
        device: TouchDevice,
    ) -> None:
        self.config = config
        self.screen = screen
        self.controller = controller
        self.clock = clock
        self.device = device
        self._swallow = False

    def on_screen(self, on: bool) -> None:
        if not on and (self.config.screen.swallow_wake_touch or self.controller.state.ui_locked):
            self.device.grab()
        elif on and not self._swallow:
            self.device.ungrab()

    async def press(self) -> None:
        self.controller.touch("press")
        if not self.screen.screen_on and self.config.screen.wake_on_touch:
            self._swallow = self.config.screen.swallow_wake_touch
            await self.controller.cmd_screen(True)
            if self.controller.state.ui_locked:
                self.device.grab()

    async def release(self) -> None:
        if self._swallow:
            self.clock.call_later(0.3, self._finish_swallow)

    async def _finish_swallow(self) -> None:
        self._swallow = False
        if self.screen.screen_on and not self.controller.state.ui_locked:
            self.device.ungrab()


def open_touch_device(path: str) -> TouchDevice | None:
    try:
        import evdev  # type: ignore[import-not-found]
    except ImportError:
        log.info("evdev is not installed")
        return None
    try:
        if path and path != "auto":
            return evdev.InputDevice(path)  # type: ignore[no-any-return]
        for item in evdev.list_devices():
            device = evdev.InputDevice(item)
            caps = device.capabilities(verbose=False)
            abs_codes = {code for code, _info in caps.get(evdev.ecodes.EV_ABS, [])}
            if evdev.ecodes.ABS_MT_POSITION_X in abs_codes:
                return device  # type: ignore[no-any-return]
    except (OSError, AttributeError) as exc:
        log.warning("touch device unavailable error=%s", exc)
    return None
