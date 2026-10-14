from __future__ import annotations

from pathlib import Path

import pytest

from pikiosk.catalog import Catalog
from pikiosk.clock import FakeClock
from pikiosk.config import KioskConfig, load_config
from pikiosk.controller import Controller
from pikiosk.events import EventBus
from pikiosk.persistence import StateStore
from pikiosk.player.memory import MemoryPlayer
from pikiosk.screen.backlight import Backlight, discover_backlight
from pikiosk.screen.output import RecordingOutput
from pikiosk.screen.screen_service import ScreenService

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config" / "config.example.yaml"


@pytest.fixture
def example_config() -> KioskConfig:
    return load_config(EXAMPLE)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_controller(
    tmp_path: Path,
    config: KioskConfig,
    clock: FakeClock,
    *,
    player: MemoryPlayer | None = None,
) -> tuple[Controller, MemoryPlayer, ScreenService]:
    panel = tmp_path / "backlight" / "rpi_backlight"
    panel.mkdir(parents=True)
    (panel / "max_brightness").write_text("255", encoding="utf-8")
    (panel / "brightness").write_text("100", encoding="utf-8")
    (panel / "bl_power").write_text("0", encoding="utf-8")
    (panel / "actual_brightness").write_text("100", encoding="utf-8")
    backlight = Backlight(discover_backlight(tmp_path / "backlight"))
    screen = ScreenService(config, backlight, RecordingOutput(), clock, output_name="DSI-1")
    memory = player or MemoryPlayer()
    controller = Controller(
        config,
        player=memory,
        screen=screen,
        clock=clock,
        bus=EventBus(),
        store=StateStore(tmp_path / "state.json"),
        catalog=Catalog(
            config.catalog_items(),
            allowed_hosts=config.playback.allowed_hosts,
            allow_unlisted=config.playback.allow_unlisted,
            file_roots=config.file_roots(),
        ),
    )
    return controller, memory, screen
