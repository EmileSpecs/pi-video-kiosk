from __future__ import annotations

import math
from pathlib import Path

import pytest

from pikiosk.screen.backlight import discover_backlight, map_brightness
from pikiosk.state import KioskState, PlayerState, extra_dim_gain


@pytest.mark.parametrize("maximum", [255, 31])
def test_brightness_mapping(maximum: int) -> None:
    full = map_brightness(255, maximum, 5)
    assert full == maximum
    floor = math.ceil(0.05 * maximum)
    assert map_brightness(1, maximum, 5) == max(round(1 / 255 * maximum), floor)


def test_on_reaches_the_lowest_backlight_step() -> None:
    # Touch Display 2 reports max 31: 1 % must be step 1, not the old 5 % floor (2).
    assert map_brightness(3, 31, 1) == 1
    assert map_brightness(3, 31, 0) == 1  # never 0 while on
    assert map_brightness(3, 31, 5) == 2


def test_extra_dim_gain() -> None:
    assert extra_dim_gain(0) == 1.0
    assert extra_dim_gain(90) ** 2.2 == pytest.approx(0.1)  # 90 % less light
    assert extra_dim_gain(500) == extra_dim_gain(90)


async def test_extra_dim_darkens_video_and_grid(tmp_path, example_config, clock) -> None:
    from pikiosk.mqtt.bridge import MqttBridge
    from pikiosk.mqtt.memory import Incoming, MemoryTransport
    from pikiosk.mqtt.state_payload import screen_payload
    from pikiosk.persistence import StateStore
    from pikiosk.web.server import state_view
    from tests.conftest import make_controller

    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    bridge = MqttBridge(example_config, controller, MemoryTransport(), clock, controller.store)
    await bridge._on_message(Incoming("pikiosk/kids_kiosk/cmd/extra_dim", "70"))
    assert controller.state.extra_dim == 70
    assert player.properties["contrast"] == round((extra_dim_gain(70) - 1) * 100)
    assert screen_payload(controller.state)["extra_dim"] == 70
    assert state_view(controller.state)["dim"] == round(1 - extra_dim_gain(70), 3)

    await controller.dispatch("set_extra_dim", {"percent": 200})
    assert controller.state.extra_dim == 90  # clamped

    # mpv restarts with default settings; the dim is applied again on connect.
    player.properties.pop("contrast")
    await controller.set_mpv_connected(True)
    assert player.properties["contrast"] == round((extra_dim_gain(90) - 1) * 100)

    store = StateStore(tmp_path / "state.json")
    store.save(controller.state)
    assert store.load(KioskState()).extra_dim == 90

    await controller.dispatch("set_extra_dim", {"percent": 0})
    assert player.properties["contrast"] == 0
    assert state_view(controller.state)["dim"] == 0


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
