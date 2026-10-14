"""pikiosk-ctl talks to the local child API."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pikiosk-ctl")
    parser.add_argument("--url", default="http://127.0.0.1:8088")
    parser.add_argument("command")
    parser.add_argument("--arg", action="append", default=[])
    args = parser.parse_args(argv)
    payload: dict[str, object] = {}
    for item in args.arg:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        payload[key] = value
    body = json.dumps({"command": args.command, "args": payload}).encode()
    request = urllib.request.Request(
        args.url.rstrip("/") + "/api/command",
        data=body,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            sys.stdout.write(response.read().decode())
    except urllib.error.URLError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
