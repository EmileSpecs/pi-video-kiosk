from __future__ import annotations

import pytest

from pikiosk.mqtt.bridge import MqttBridge
from pikiosk.mqtt.discovery import DiscoveryContext
from pikiosk.mqtt.memory import Incoming, MemoryTransport
from pikiosk.state import PlayerState
from tests.conftest import make_controller


async def _settle() -> None:
    import asyncio

    for _ in range(8):
        await asyncio.sleep(0)


@pytest.fixture
def bridge(tmp_path, example_config, clock):
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    transport = MemoryTransport()
    mqtt = MqttBridge(
        example_config,
        controller,
        transport,
        clock,
        controller.store,
        context=DiscoveryContext(),
        telemetry={"cpu_temp": 52.3, "boot_time": "2026-10-13T04:01:12+00:00", "ip": "192.168.1.57"},
    )
    return mqtt, controller, player, transport


async def test_connect_order_and_lwt(bridge) -> None:
    mqtt, _controller, _player, transport = bridge
    await mqtt._on_connect()
    topics = [item.topic for item in transport.published]
    online = topics.index("pikiosk/kids_kiosk/status")
    discovery = topics.index("homeassistant/button/pikiosk_kids_kiosk/play/config")
    assert discovery < online
    assert transport.published[online].payload == "online"
    assert transport.will is not None
    assert transport.will.payload == "offline"
    await transport.disconnect()
    assert transport.published[-1].payload == "offline"
    assert transport.published[-1].topic == "pikiosk/kids_kiosk/status"


async def test_commands_and_retained_ignored(bridge) -> None:
    mqtt, controller, player, transport = bridge
    await mqtt._on_connect()
    await mqtt._on_message(Incoming("pikiosk/kids_kiosk/cmd/play_media", "XqZsoesa55w", retain=True))
    assert player.loads == []
    await mqtt._on_message(Incoming("pikiosk/kids_kiosk/cmd/play_media", "XqZsoesa55w"))
    await _settle()
    assert player.loads
    assert controller.state.player == PlayerState.PLAYING
    await mqtt._on_message(Incoming("pikiosk/kids_kiosk/cmd/volume", "10"))
    await _settle()
    assert controller.state.volume == 10


async def test_birth_republishes(bridge) -> None:
    mqtt, _controller, _player, transport = bridge
    await mqtt._on_connect()
    before = len(transport.published)
    await mqtt._on_message(Incoming("homeassistant/status", "online"))
    assert len(transport.published) > before


async def test_position_throttled(bridge) -> None:
    mqtt, controller, _player, transport = bridge
    await mqtt._on_connect()
    await controller.dispatch("play_media", {"ref": "XqZsoesa55w"})
    await _settle()
    def state_count() -> int:
        return sum(1 for item in transport.published if item.topic == "pikiosk/kids_kiosk/state")
    before = state_count()
    assert controller.state.now is not None
    from dataclasses import replace

    controller._set(replace(controller.state, now=replace(controller.state.now, position=5)))
    await _settle()
    assert state_count() == before
    from pikiosk.clock import FakeClock

    assert isinstance(controller.clock, FakeClock)
    await controller.clock.advance(5)
    controller._set(replace(controller.state, now=replace(controller.state.now, position=9)))
    await _settle()
    assert state_count() == before + 1


async def test_removed_entity_cleared(bridge, example_config) -> None:
    mqtt, _controller, _player, transport = bridge
    await mqtt._on_connect()
    slim = example_config.model_copy(update={"catalog": example_config.catalog.model_copy(update={"items": []})})
    mqtt.update_config(slim)
    mqtt.controller.config = slim
    await mqtt.publish_discovery()
    emptied = [item for item in transport.published if item.payload == ""]
    assert emptied == [] or all(item.retain for item in emptied)
