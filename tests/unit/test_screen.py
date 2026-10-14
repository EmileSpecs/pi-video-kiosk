from __future__ import annotations

import math
from pathlib import Path

import pytest

from pikiosk.screen.backlight import discover_backlight, map_brightness
from pikiosk.state import PlayerState


@pytest.mark.parametrize("maximum", [255, 31])
def test_brightness_mapping(maximum: int) -> None:
    full = map_brightness(255, maximum, 5)
    assert full == maximum
    floor = math.ceil(0.05 * maximum)
    assert map_brightness(1, maximum, 5) == max(round(1 / 255 * maximum), floor)


def test_discover_only_and_max(tmp_path: Path) -> None:
    panel = tmp_path / "10-0045"
    panel.mkdir()
    (panel / "max_brightness").write_text("31", encoding="utf-8")
    found = discover_backlight(tmp_path)
    assert found is not None
    assert found.max_brightness == 31


async def test_off_on_restores(tmp_path, example_config, clock) -> None:
    from tests.conftest import make_controller

    controller, _player, screen = make_controller(tmp_path, example_config, clock)
    await controller.cmd_screen(on=False)
    assert controller.state.screen_on is False
    assert (tmp_path / "backlight" / "rpi_backlight" / "bl_power").read_text(encoding="utf-8") == "4"
    await controller.cmd_screen(on=True, brightness=120)
    assert controller.state.screen_on is True
    assert controller.state.brightness == 120
    written = int((tmp_path / "backlight" / "rpi_backlight" / "brightness").read_text(encoding="utf-8"))
    assert written == map_brightness(120, 255, 5)


async def test_sleep_only_when_idle(tmp_path, example_config, clock) -> None:
    from tests.conftest import make_controller

    config = example_config.model_copy(
        update={"screen": example_config.screen.model_copy(update={"sleep_timeout_min": 1, "dim_before_sleep_s": 15})}
    )
    controller, player, screen = make_controller(tmp_path, config, clock)
    screen.set_player_state(PlayerState.PLAYING)
    await clock.advance(120)
    assert screen.screen_on is True
    screen.set_player_state(PlayerState.IDLE)
    await clock.advance(45)
    assert screen.screen_on is True
    await clock.advance(20)
    assert screen.screen_on is False
    assert controller.state.screen_on is False


async def test_wake_touch_is_swallowed(tmp_path, example_config, clock) -> None:
    from pikiosk.input.touch import FakeTouch, InputMonitor
    from tests.conftest import make_controller

    controller, _player, screen = make_controller(tmp_path, example_config, clock)
    touch = FakeTouch()
    monitor = InputMonitor(example_config, screen, controller, clock, touch)
    await controller.cmd_screen(on=False)
    monitor.on_screen(False)
    assert touch.grabbed is True
    await monitor.press()
    assert controller.state.screen_on is True
    await monitor.release()
    assert touch.grabbed is True
    await clock.advance(0.3)
    assert touch.grabbed is False
