"""Stand-alone fake mpv IPC server."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pikiosk.player.fake_server import FakeMpvServer


async def _main(path: str) -> None:
    server = FakeMpvServer()
    await server.start(path)
    print(f"fake mpv listening on {path}")
    await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("socket")
    args = parser.parse_args()
    asyncio.run(_main(args.socket))


if __name__ == "__main__":
    main()
