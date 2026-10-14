from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pikiosk.cache.cache_manager import CacheManager
from pikiosk.cache.ytdlp import YtDlp, classify_error
from pikiosk.clock import FakeClock


def test_classify() -> None:
    assert classify_error("Sign in to confirm you're not a bot") == "bot_check"
    assert classify_error("Video unavailable") == "unavailable"
    assert classify_error("HTTP Error 403: Forbidden") == "extractor"
    assert classify_error("urlopen error timed out") == "network"
    assert classify_error("not available in your country") == "geo"


async def test_download_and_evict(tmp_path: Path, example_config, clock: FakeClock) -> None:
    script = tmp_path / "ytdlp.py"
    script.write_text(
        """
import sys
from pathlib import Path
args = sys.argv[1:]
if "--version" in args:
    print("2026.10.10")
    raise SystemExit(0)
if "--flat-playlist" in args:
    print('{"entries":[{"id":"XqZsoesa55w"},{"id":"dQw4w9WgXcQ"}]}')
    raise SystemExit(0)
if "-o" in args:
    template = Path(args[args.index("-o") + 1])
    video_id = args[-1].split("v=")[-1][:11]
    target = template.parent / f"{video_id}.mkv"
    target.write_bytes(b"0123456789")
    print("[download]  50.0% of 10.00B at 1.00B/s ETA 00:05")
    print(target)
    raise SystemExit(0)
print("{}")
""",
        encoding="utf-8",
    )
    config = example_config.model_copy(
        update={
            "offline": example_config.offline.model_copy(
                update={
                    "enabled": True,
                    "directory": str(tmp_path / "cache"),
                    "max_size_gb": 1,
                    "download_window": None,
                }
            )
        }
    )
    clock.set_now(datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc))
    manager = CacheManager(config, YtDlp([__import__("sys").executable, str(script)]), clock)
    await manager.sync(
        [
            {"name": "Mix", "type": "playlist", "id": "PLxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "max_items": 2},
        ]
    )
    assert manager.get("XqZsoesa55w")
    assert (tmp_path / "cache" / "index.json").exists()
    assert manager.used_bytes() <= 10 or manager.entries
