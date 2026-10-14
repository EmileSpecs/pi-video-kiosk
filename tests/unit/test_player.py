from __future__ import annotations

from dataclasses import replace

from pikiosk.state import PlayerState
from tests.conftest import make_controller


async def test_play_media_loadfile(tmp_path, example_config, clock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    await controller.dispatch("play_media", {"ref": "https://youtu.be/XqZsoesa55w"})
    assert player.loads[0]["target"].endswith("XqZsoesa55w")
    assert controller.state.player == PlayerState.PLAYING


async def test_after_video_modes(tmp_path, example_config, clock) -> None:
    controller, player, screen = make_controller(tmp_path, example_config, clock)
    await controller.dispatch("set_after_video", {"mode": "loop"})
    await controller.dispatch("play_media", {"ref": "XqZsoesa55w"})
    assert player.loop_file is True
    await controller.dispatch("set_after_video", {"mode": "stop"})
    await player.simulate_eof()
    await controller.drain()
    assert controller.state.player == PlayerState.IDLE

    await controller.dispatch("set_after_video", {"mode": "blank"})
    await controller.dispatch("play_media", {"ref": "XqZsoesa55w"})
    await player.simulate_eof()
    await controller.drain()
    assert screen.screen_on is False

    await controller.dispatch("screen", {"on": True})
    await controller.dispatch("set_after_video", {"mode": "next"})
    await controller.dispatch("select", {"name": "Baby Shark"})
    await player.simulate_eof()
    await controller.drain()
    assert controller.state.selected == "Sesame Street Songs"


async def test_playlist_stop_waits_for_end(tmp_path, example_config, clock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    await controller.dispatch("set_after_video", {"mode": "stop"})
    await controller.dispatch("select", {"name": "Sesame Street Songs"})
    assert controller.state.now is not None
    controller._set(
        replace(
            controller.state,
            now=replace(controller.state.now, playlist_index=0, playlist_count=3),
        )
    )
    await player.simulate_eof()
    await controller.drain()
    assert controller.state.player == PlayerState.PLAYING
    controller._set(
        replace(
            controller.state,
            now=replace(controller.state.now, playlist_index=2, playlist_count=3),  # type: ignore[arg-type]
        )
    )
    await player.simulate_eof()
    await controller.drain()
    assert controller.state.player == PlayerState.IDLE


async def test_error_retry_then_idle(tmp_path, example_config, clock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    player.failures_left = 2
    await controller.dispatch("play_media", {"ref": "XqZsoesa55w"})
    assert controller.state.player == PlayerState.ERROR
    await clock.advance(3)
    assert len(player.loads) >= 2
    assert controller.state.player == PlayerState.ERROR
    await clock.advance(10)
    assert controller.state.player == PlayerState.IDLE


async def test_cache_fallback(tmp_path, example_config, clock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    cached = tmp_path / "vid.mkv"
    cached.write_bytes(b"media")
    controller.cache_lookup = lambda video_id: str(cached) if video_id == "XqZsoesa55w" else None
    player.fail_next = True
    await controller.dispatch("play_media", {"ref": "XqZsoesa55w"})
    await controller.drain()
    assert any(load["target"] == str(cached) for load in player.loads)


async def test_reconnect_reapplies_volume(tmp_path, example_config, clock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    await controller.dispatch("set_volume", {"volume": 25})
    await controller.dispatch("set_mute", {"muted": True})
    player.connected = False
    await controller.set_mpv_connected(False)
    player.connected = True
    await controller.set_mpv_connected(True)
    assert player.volume == 25
    assert player.muted is True


async def test_unknown_select(tmp_path, example_config, clock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    await controller.dispatch("select", {"name": "nope"})
    assert player.loads == []
    assert controller.bus.history[-1].type == "error"
