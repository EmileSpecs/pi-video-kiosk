from __future__ import annotations

import json

from pikiosk.persistence import StateStore
from pikiosk.state import KioskState, PlayerState


def test_atomic_and_corrupt(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    state = KioskState(volume=12, muted=True, brightness=90)
    store.save(state)
    loaded = StateStore(path).load(KioskState())
    assert loaded.volume == 12
    assert loaded.muted is True
    path.write_text("{", encoding="utf-8")
    recovered = StateStore(path).load(KioskState(volume=40))
    assert recovered.volume == 40
    assert path.with_name("state.json.bad").exists()


def test_resume_roundtrip(tmp_path, example_config, clock) -> None:
    from tests.conftest import make_controller

    controller, _player, _screen = make_controller(tmp_path, example_config, clock)
    controller.store.save(controller.state)
    again = StateStore(tmp_path / "state.json").load(KioskState())
    assert again.volume == controller.state.volume


async def test_resume_on_boot_flag(tmp_path, example_config) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.last_item = {"ref": "XqZsoesa55w", "position": 10}
    store.save(KioskState(volume=33, player=PlayerState.PLAYING))
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert raw["volume"] == 33
    assert raw["last_item"]["position"] == 10
    assert example_config.playback.resume_on_boot is False
