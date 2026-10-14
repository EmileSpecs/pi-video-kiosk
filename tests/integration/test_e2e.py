from __future__ import annotations

from pikiosk.mqtt.bridge import MqttBridge
from pikiosk.mqtt.memory import Incoming, MemoryTransport
from pikiosk.state import PlayerState
from tests.conftest import make_controller


async def test_select_eof_blank_then_light_on(tmp_path, example_config, clock) -> None:
    controller, player, screen = make_controller(tmp_path, example_config, clock)
    transport = MemoryTransport()
    bridge = MqttBridge(example_config, controller, transport, clock, controller.store)
    await bridge._on_connect()
    await controller.dispatch("set_after_video", {"mode": "blank"})
    await bridge._on_message(Incoming("pikiosk/kids_kiosk/cmd/select", "Baby Shark"))
    assert controller.state.player == PlayerState.PLAYING
    assert player.loads
    await player.simulate_eof()
    await controller.drain()
    assert controller.state.player == PlayerState.IDLE
    assert screen.screen_on is False
    await bridge._on_message(
        Incoming("pikiosk/kids_kiosk/cmd/screen", '{"state":"ON","brightness":60}')
    )
    assert screen.screen_on is True
    assert controller.state.brightness == 60
