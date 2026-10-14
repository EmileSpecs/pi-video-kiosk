"""Fake backends kept here so `--simulate` and the tests share them.

The implementations live next to the real ones (`MemoryPlayer`, `MemoryTransport`,
`FakeClock`, `FakeTouch`). This module re-exports them.
"""

from pikiosk.clock import FakeClock
from pikiosk.input.touch import FakeTouch
from pikiosk.mqtt.memory import Incoming, MemoryTransport, Published
from pikiosk.player.fake_server import FakeMpvServer
from pikiosk.player.memory import MemoryPlayer
from pikiosk.screen.output import RecordingOutput

__all__ = [
    "FakeClock",
    "FakeMpvServer",
    "FakeTouch",
    "Incoming",
    "MemoryPlayer",
    "MemoryTransport",
    "Published",
    "RecordingOutput",
]
