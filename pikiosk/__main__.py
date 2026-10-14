"""pikioskd entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from pikiosk.app import App
from pikiosk.config import ConfigError, load_config, secrets_path_for


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pikioskd")
    parser.add_argument("--config", default=None)
    parser.add_argument("--check-config", metavar="PATH", default=None)
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--state", default=None)
    args = parser.parse_args(argv)
    path = args.check_config or args.config
    if path is None:
        parser.error("pass --config PATH or --check-config PATH")
    try:
        config = load_config(path)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.check_config:
        return 0
    config_path = Path(path)
    app = App(
        config,
        simulate=args.simulate,
        config_path=config_path,
        secrets_path=secrets_path_for(config_path),
        state_path=Path(args.state) if args.state else Path("state.json"),
    )
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
