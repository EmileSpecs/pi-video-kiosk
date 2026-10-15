"""Brightness, blanking, and the idle sleep timer."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from pikiosk.clock import Clock
from pikiosk.config import KioskConfig
from pikiosk.screen import orient
from pikiosk.screen.backlight import Backlight, map_brightness
from pikiosk.screen.output import OutputControl
from pikiosk.state import PlayerState

log = logging.getLogger("pikiosk.screen")

_IDLE_STATES = {PlayerState.IDLE, PlayerState.PAUSED, PlayerState.ERROR}


class ScreenService:
    def __init__(
        self,
        config: KioskConfig,
        backlight: Backlight,
        output: OutputControl,
        clock: Clock,
        *,
        output_name: str | None = None,
    ) -> None:
        self.config = config
        self.backlight = backlight
        self.output = output
        self.clock = clock
        self.output_name = output_name
        self.screen_on = True
        self.brightness = round(config.screen.default_brightness / 100 * 255)
        self._sleep_timer: object | None = None
        self._dim_timer: object | None = None
        self._dimmed = False
        self._lock = asyncio.Lock()
        self.player_state = PlayerState.IDLE
        self.sleep_enabled = config.screen.sleep_enabled
        self.sleep_timeout_min = config.screen.sleep_timeout_min
        self.on_power: Callable[[bool], Awaitable[None]] | None = None
        self.transform: str | None = None
        self.lowest = False  # extra dim: backlight at its lowest step whatever the brightness

    async def apply_orientation(self) -> None:
        """Turn the output per config.screen; remembers the transform for re-enables."""
        try:
            name, transform = await orient.apply(self.config, self.output)
        except Exception:
            log.warning("orientation apply failed", exc_info=True)
            return
        if name and not self.output_name:
            self.output_name = name
        self.transform = transform

    def set_player_state(self, state: PlayerState) -> None:
        self.player_state = state
        self._reschedule()

    def configure(self, *, sleep_enabled: bool, sleep_timeout_min: int) -> None:
        self.sleep_enabled = sleep_enabled
        self.sleep_timeout_min = sleep_timeout_min
        self._reschedule()

    async def turn_on(self, brightness: int | None = None) -> None:
        if brightness is not None:
            self.brightness = brightness
        self.screen_on = True
        self._dimmed = False
        await self._apply(True, self.brightness)
        self._reschedule()
        if self.on_power:
            await self.on_power(True)

    async def turn_off(self) -> None:
        self.screen_on = False
        self._cancel()
        await self._apply(False, self.brightness)
        if self.on_power:
            await self.on_power(False)

    async def set_brightness(self, brightness: int) -> None:
        self.brightness = brightness
        if self.screen_on:
            await self._apply(True, brightness)

    async def _apply(self, on: bool, ha_brightness: int) -> None:
        async with self._lock:
            floor_percent = self.config.screen.min_brightness
            if self.lowest:
                ha_brightness, floor_percent = 1, 0  # maps to sysfs 1
            method = self.config.screen.blank_method
            name = self.output_name or self.config.screen.output_name
            transform = self.transform
            if method in {"output", "both"} and name and name != "auto":
                if on:
                    await self.output.set_on(name, True, transform)
                else:
                    await self.output.set_on(name, False, None)
            if self.backlight.available and method in {"backlight", "both"}:
                device = self.backlight.device
                assert device is not None
                sysfs = map_brightness(
                    ha_brightness if on else 0,
                    device.max_brightness,
                    floor_percent if on else 0,
                )
                await asyncio.to_thread(self.backlight.write, sysfs, on)
            elif self.backlight.available and on:
                device = self.backlight.device
                assert device is not None
                sysfs = map_brightness(ha_brightness, device.max_brightness, floor_percent)
                await asyncio.to_thread(self.backlight.write, sysfs, True)

    def note_activity(self) -> None:
        if self._dimmed and self.screen_on:
            self._dimmed = False
            asyncio.get_running_loop().create_task(self._apply(True, self.brightness))
        self._reschedule()

    def _cancel(self) -> None:
        for handle in (self._sleep_timer, self._dim_timer):
            cancel = getattr(handle, "cancel", None)
            if cancel:
                cancel()
        self._sleep_timer = None
        self._dim_timer = None

    def _reschedule(self) -> None:
        self._cancel()
        if not self._sleep_allowed():
            return
        timeout = self.sleep_timeout_min * 60
        dim = self.config.screen.dim_before_sleep_s
        if dim > 0 and timeout > dim:
            self._dim_timer = self.clock.call_later(timeout - dim, self._dim)
        self._sleep_timer = self.clock.call_later(timeout, self._sleep)

    def _sleep_allowed(self) -> bool:
        return self.sleep_enabled and self.screen_on and self.player_state in _IDLE_STATES

    async def _dim(self) -> None:
        if not self._sleep_allowed():
            return
        self._dimmed = True
        floor = round(self.config.screen.min_brightness / 100 * 255)
        await self._apply(True, max(1, floor))

    async def _sleep(self) -> None:
        if not self._sleep_allowed():
            return
        log.info("idle sleep")
        await self.turn_off()
