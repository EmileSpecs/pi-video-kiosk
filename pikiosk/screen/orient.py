"""Turn the panel so video is landscape (or portrait, if asked).

The Touch Display 2 is natively portrait and the original 7" display is
natively landscape, so the compositor transform depends on which one is
attached. labwc's autostart runs this before the kiosk session starts and
pikioskd re-applies it when the Screen settings change.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Literal

from pikiosk.config import KioskConfig, load_config
from pikiosk.screen.output import OutputControl, OutputInfo, WlrRandr

log = logging.getLogger("pikiosk.screen")
Orientation = Literal["landscape", "portrait"]
_ROTATIONS = {0: "normal", 90: "90", 180: "180", 270: "270"}


def transform_for(
    orientation: Orientation,
    *,
    flip: bool = False,
    native_portrait: bool = False,
    rotation: int | None = None,
) -> str:
    """wlr-randr transform that shows `orientation` on a panel of the given native shape."""
    # 0 is what older configs carry and means "work it out", not "force normal".
    if rotation:
        return _ROTATIONS.get(rotation, "normal")
    turned = (orientation == "portrait") != native_portrait
    if turned:
        return "270" if flip else "90"
    return "180" if flip else "normal"


def transform_for_config(config: KioskConfig, info: OutputInfo | None) -> str:
    screen = config.screen
    native_portrait = info.native_portrait if info and info.width and info.height else False
    return transform_for(
        screen.orientation,
        flip=screen.flip,
        native_portrait=native_portrait,
        rotation=screen.rotation,
    )


async def apply(config: KioskConfig, output: OutputControl) -> tuple[str | None, str]:
    """Apply the configured orientation. Returns (output name, transform)."""
    info = await output.describe()
    transform = transform_for_config(config, info)
    name = info.name if info else None
    if config.screen.output_name != "auto":
        name = config.screen.output_name
    if not name:
        log.warning("no display output found; orientation not applied")
        return None, transform
    if info is not None and info.transform == transform:
        return name, transform
    log.info(
        "orientation=%s flip=%s native=%s -> transform=%s on %s",
        config.screen.orientation,
        config.screen.flip,
        "portrait" if info and info.native_portrait else "landscape",
        transform,
        name,
    )
    await output.set_on(name, True, transform)
    return name, transform


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pikiosk-orient", description=__doc__)
    parser.add_argument("--config", default="/etc/pi-kiosk/config.yaml")
    parser.add_argument("--print", action="store_true", help="only print the transform")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = load_config(Path(args.config))
    output = WlrRandr(config.screen.output_name)
    if args.print:
        info = asyncio.run(output.describe())
        print(transform_for_config(config, info))
        return 0
    name, transform = asyncio.run(apply(config, output))
    if name is None:
        return 1
    print(f"{name} {transform}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
