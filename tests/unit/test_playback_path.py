"""Phase 2 and 3: cache hit → play, miss → download → index → play, playlists,
progress, timeouts, retries, observed properties."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pikiosk.cache.cache_manager import CacheManager
from pikiosk.cache.ytdlp import Download, YtDlp, classify_error, parse_downloads, parse_progress
from pikiosk.clock import FakeClock
from pikiosk.player.mpv_player import OBSERVED, MpvPlayer
from pikiosk.state import KioskState, PlayerState
from tests.conftest import make_controller
from tests.unit.test_more import StubClient

BABY = "XqZsoesa55w"
BATH = "WRVsOCh907o"


class FakeCache:
    """Stands in for CacheManager: instant hits, scripted downloads."""

    def __init__(self, files: dict[str, str] | None = None, downloads: dict[str, str] | None = None) -> None:
        self.files = dict(files or {})
        self.downloads = dict(downloads or {})
        self.fail: dict[str, str] = {}
        self.calls: list[str] = []
        self.played: list[str] = []
        self.forgotten: list[str] = []
        self.block: asyncio.Event | None = None
        self.titles: dict[str, str] = {}

    def get(self, video_id: str) -> str | None:
        return self.files.get(video_id)

    def title_for(self, video_id: str) -> str | None:
        return self.titles.get(video_id)

    def note_titles(self, entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            if entry.get("id") and entry.get("title") and entry["title"] != entry["id"]:
                self.titles[str(entry["id"])] = str(entry["title"])

    async def ensure(self, video_id: str, url: str | None = None, *, progress: Any = None) -> tuple[str | None, str]:
        self.calls.append(video_id)
        if video_id in self.files:
            return self.files[video_id], ""
        if progress:
            progress(10.0)
            progress(11.0)  # below the 2 % step, must not publish
            progress(60.0)
        if self.block is not None:
            await self.block.wait()
        if video_id in self.fail:
            return None, self.fail[video_id]
        path = self.downloads[video_id]
        self.files[video_id] = path
        if progress:
            progress(100.0)
        return path, ""

    def mark_played(self, video_id: str) -> None:
        self.played.append(video_id)

    def forget(self, video_id: str) -> None:
        self.forgotten.append(video_id)
        self.files.pop(video_id, None)


class FakeYtDlp:
    def __init__(self, playlists: dict[str, list[str]] | None = None, titles: dict[str, str] | None = None) -> None:
        self.playlists = playlists or {}
        self.titles = titles or {}

    async def flat_playlist(self, playlist_id: str, limit: int) -> list[dict[str, Any]]:
        return [
            {"id": video_id, "title": self.titles.get(video_id, video_id)}
            for video_id in self.playlists.get(playlist_id, [])[:limit]
        ]


def _media(tmp_path: Path, name: str) -> str:
    file = tmp_path / f"{name}.mkv"
    file.write_bytes(b"media")
    return str(file)


def test_parse_progress_and_player_class() -> None:
    assert parse_progress("[download]  43.2% of    7.25MiB at   19.64MiB/s ETA 00:00") == 43.2
    assert parse_progress("[download] 100% of 7.25MiB in 00:00:00 at 19.64MiB/s") == 100.0
    assert parse_progress("[youtube] XqZsoesa55w: Downloading webpage") is None
    assert classify_error("loading failed: Can not open external file") == "player"
    assert classify_error("no audio or video data played") == "player"
    assert classify_error("[vo/gpu] Failed to initialize") == "player"
    assert classify_error("playback did not start") == "player"
    assert classify_error("ERROR: [youtube] abc: Requested format is not available") == "extractor"
    assert classify_error("download timed out after 300 s") == "network"


async def test_run_streaming_reports_progress_and_times_out(tmp_path: Path) -> None:
    script = tmp_path / "fake_ytdlp.py"
    script.write_text(
        """
import sys, time
from pathlib import Path
args = sys.argv[1:]
if "--hang" in args:
    time.sleep(30)
template = Path(args[args.index("-o") + 1])
target = template.parent / "XqZsoesa55w.mkv"
target.write_bytes(b"0123456789")
print("[download]  25.0% of 10.00B at 1.00B/s ETA 00:05", flush=True)
print("[download] 100% of 10.00B in 00:00:00 at 1.00B/s", flush=True)
print(target)
print('title:"Baby Shark Dance | #babyshark"')
""",
        encoding="utf-8",
    )
    ytdlp = YtDlp([sys.executable, str(script)])
    seen: list[float] = []
    files, err = await ytdlp.download(
        "https://www.youtube.com/watch?v=XqZsoesa55w",
        "18/b",
        str(tmp_path / "cache"),
        progress=seen.append,
        timeout=20,
    )
    assert err == ""
    assert [item.path for item in files] == [str(tmp_path / "cache" / "XqZsoesa55w.mkv")]
    assert files[0].title == "Baby Shark Dance | #babyshark"
    assert seen[:2] == [25.0, 100.0]
    paths, err = await ytdlp.download_media(
        "https://www.youtube.com/watch?v=XqZsoesa55w", "18/b", str(tmp_path / "cache"), timeout=20
    )
    assert paths == [str(tmp_path / "cache" / "XqZsoesa55w.mkv")]
    slow = YtDlp([sys.executable, str(script), "--hang"])
    paths, err = await slow.download_media(
        "https://www.youtube.com/watch?v=XqZsoesa55w",
        "18/b",
        str(tmp_path / "cache2"),
        timeout=0.5,
    )
    assert paths == []
    assert "timed out" in err
    assert classify_error(err) == "network"


def test_parse_downloads_pairs_paths_with_titles(tmp_path: Path) -> None:
    first = _media(tmp_path, "aaaaaaaaaaa")
    second = _media(tmp_path, "bbbbbbbbbbb")
    out = "\n".join(
        [
            "[download] 100% of 1.00B",
            first,
            'title:"First \\u2013 song"',
            second,
            "title:not json",
            'title:"orphan"',
        ]
    )
    files = parse_downloads(out)
    assert [(item.path, item.title) for item in files] == [(first, "First – song"), (second, None)]


async def test_cache_keeps_titles_from_downloads_and_playlist_scans(
    tmp_path: Path, example_config, clock: FakeClock
) -> None:
    class _Yt:
        def __init__(self) -> None:
            self.lookups: list[str] = []

        async def download(self, url: str, fmt: str, directory: str, **kwargs: Any) -> tuple[list[Download], str]:
            video_id = url.split("v=")[-1]
            target = Path(directory) / f"{video_id}.mkv"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
            title = "Baby Shark Dance" if video_id == BABY else None
            return [Download(str(target), title)], ""

        async def flat_playlist(self, playlist_id: str, limit: int) -> list[dict[str, Any]]:
            return [{"id": BATH, "title": "Bath Song"}, {"id": "ppppppppppp", "title": "Playlist Only"}]

        async def title(self, video_id: str) -> str | None:
            self.lookups.append(video_id)
            return "Looked Up" if video_id == "ooooooooooo" else None

    config = example_config.model_copy(
        update={"offline": example_config.offline.model_copy(update={"directory": str(tmp_path / "cache")})}
    )
    ytdlp = _Yt()
    manager = CacheManager(config, ytdlp, clock)  # type: ignore[arg-type]
    # A file cached before titles were kept.
    (tmp_path / "cache").mkdir()
    old = _media(tmp_path / "cache", "ooooooooooo")
    manager.register("ooooooooooo", old)
    assert manager.title_for("ooooooooooo") is None

    await manager.ensure(BABY)
    assert manager.title_for(BABY) == "Baby Shark Dance"  # printed by yt-dlp
    await manager.ensure(BATH)
    assert manager.title_for(BATH) is None  # nothing printed, not scanned yet

    await manager.sync(config.catalog_items() + [{"type": "video", "id": "ooooooooooo"}], force=True)
    assert manager.title_for(BATH) == "Bath Song"  # filled in from the playlist scan
    assert manager.title_for("ppppppppppp") == "Playlist Only"
    assert manager.title_for("ooooooooooo") == "Looked Up"  # backfilled with one request
    assert ytdlp.lookups == ["ooooooooooo"]

    reloaded = CacheManager(config, ytdlp, clock)  # type: ignore[arg-type]
    assert reloaded.title_for(BATH) == "Bath Song"
    assert reloaded.title_for("ooooooooooo") == "Looked Up"
    assert await reloaded.fetch_title(BATH) == "Bath Song"  # no second request
    assert ytdlp.lookups == ["ooooooooooo"]


async def test_cache_manager_shares_inflight_downloads(tmp_path: Path, example_config, clock: FakeClock) -> None:
    gate = asyncio.Event()
    calls: list[str] = []

    class _Yt:
        async def download_media(self, url: str, fmt: str, directory: str, **kwargs: Any) -> tuple[list[str], str]:
            calls.append(url)
            kwargs["progress"](40.0)
            await gate.wait()
            target = Path(directory) / f"{url.split('v=')[-1]}.mkv"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"0123456789")
            return [str(target)], ""

        async def flat_playlist(self, playlist_id: str, limit: int) -> list[dict[str, Any]]:
            return [{"id": BATH}]

    config = example_config.model_copy(
        update={"offline": example_config.offline.model_copy(update={"directory": str(tmp_path / "cache")})}
    )
    manager = CacheManager(config, _Yt(), clock)  # type: ignore[arg-type]
    seen_a: list[float] = []
    seen_b: list[float] = []
    first = asyncio.create_task(manager.ensure(BABY, progress=seen_a.append))
    await asyncio.sleep(0)
    second = asyncio.create_task(manager.ensure(BABY, progress=seen_b.append))
    await asyncio.sleep(0)
    assert manager.downloading() == {BABY: 40.0}
    gate.set()
    path_a, _ = await first
    path_b, _ = await second
    assert path_a == path_b
    assert calls == [f"https://www.youtube.com/watch?v={BABY}"]
    assert seen_a[-1] == 100.0 and 40.0 in seen_b
    assert manager.get(BABY) == path_a
    assert manager.entries[BABY].size == 10
    assert (tmp_path / "cache" / "index.json").exists()
    # Second ensure is a pure hit.
    again, _ = await manager.ensure(BABY)
    assert again == path_a and len(calls) == 1
    # forget() drops the file so the next ensure downloads again.
    manager.forget(BABY)
    assert manager.get(BABY) is None
    assert not Path(path_a).exists()


async def test_precache_fetches_catalog_videos_and_playlists_in_window(
    tmp_path: Path, example_config, clock: FakeClock
) -> None:
    calls: list[str] = []

    class _Yt:
        async def download_media(self, url: str, fmt: str, directory: str, **kwargs: Any) -> tuple[list[str], str]:
            video_id = url.split("v=")[-1]
            calls.append(video_id)
            target = Path(directory) / f"{video_id}.mkv"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
            return [str(target)], ""

        async def flat_playlist(self, playlist_id: str, limit: int) -> list[dict[str, Any]]:
            return [{"id": "ppppppppppp"}]

    config = example_config.model_copy(
        update={
            "offline": example_config.offline.model_copy(
                update={"directory": str(tmp_path / "cache"), "download_window": "01:00-06:00"}
            )
        }
    )
    clock.set_now(datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc))
    manager = CacheManager(config, _Yt(), clock)  # type: ignore[arg-type]
    items = config.catalog_items()
    assert CacheManager.catalog_video_ids(items) == [BABY, BATH]
    await manager.precache(items)
    assert calls == [BABY, BATH]
    assert manager.protected == {BABY, BATH}
    clock.set_now(datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc))
    await manager.precache(items)
    assert calls == [BABY, BATH, "ppppppppppp"]


async def test_cache_hit_plays_without_downloading(tmp_path: Path, example_config, clock: FakeClock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    cache = FakeCache(files={BABY: _media(tmp_path, BABY)})
    controller.cache = cache
    controller.cache_lookup = cache.get
    await controller.dispatch("select", {"name": "Baby Shark"})
    assert cache.calls == []
    assert player.loads[0]["target"] == cache.files[BABY]
    assert controller.state.player == PlayerState.PLAYING
    assert controller.state.now is not None
    assert controller.state.now.local_file == cache.files[BABY]
    assert controller.state.now.title == "Baby Shark"
    assert cache.played == [BABY]
    # mpv reports the file name as media-title; the catalog name must survive.
    player.emit({"event": "property-change", "name": "media-title", "data": f"{BABY}.mkv"})
    await controller.drain()
    assert controller.state.now.title == "Baby Shark"


async def test_cache_miss_downloads_with_progress_then_plays(tmp_path: Path, example_config, clock: FakeClock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    cache = FakeCache(downloads={BABY: _media(tmp_path, BABY)})
    controller.cache = cache
    seen: list[KioskState] = []
    controller.subscribe(seen.append)
    await controller.dispatch("play_media", {"ref": BABY})
    progress = [state.download_progress for state in seen if state.player == PlayerState.LOADING]
    assert 10 in progress and 60 in progress and 11 not in progress
    assert controller.state.player == PlayerState.PLAYING
    assert controller.state.download_progress is None
    assert player.loads[0]["target"] == cache.files[BABY]
    assert controller.state.now is not None and controller.state.now.local_file == cache.files[BABY]
    assert cache.calls == [BABY]


async def test_download_failure_sets_error_without_retry(tmp_path: Path, example_config, clock: FakeClock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    cache = FakeCache()
    cache.fail[BABY] = "ERROR: [youtube] XqZsoesa55w: Video unavailable"
    controller.cache = cache
    await controller.dispatch("play_media", {"ref": BABY})
    assert controller.state.player == PlayerState.ERROR
    assert controller.state.last_error == "unavailable"
    assert player.loads == []
    await clock.advance(3)
    assert cache.calls == [BABY]
    await clock.advance(10)
    assert controller.state.player == PlayerState.IDLE


async def test_download_timeout_runs_from_the_tap(tmp_path: Path, example_config, clock: FakeClock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    cache = FakeCache(downloads={BABY: _media(tmp_path, BABY)})
    cache.block = asyncio.Event()
    controller.cache = cache
    task = asyncio.create_task(controller.dispatch("play_media", {"ref": BABY}))
    await asyncio.sleep(0)
    assert controller.state.player == PlayerState.LOADING
    assert controller.state.download_progress == 60
    await clock.advance(60)
    assert controller.state.player == PlayerState.LOADING  # the open timer defers to the download
    await clock.advance(example_config.playback.download_timeout_s)
    assert controller.state.player == PlayerState.ERROR
    assert controller.state.error_detail == "download timed out at 60%"
    cache.block.set()
    await task
    assert player.loads == []  # the late download must not start playback


async def test_stop_during_download_abandons_the_load(tmp_path: Path, example_config, clock: FakeClock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    cache = FakeCache(downloads={BABY: _media(tmp_path, BABY)})
    cache.block = asyncio.Event()
    controller.cache = cache
    task = asyncio.create_task(controller.dispatch("play_media", {"ref": BABY}))
    await asyncio.sleep(0)
    await controller.cmd_stop()
    assert controller.state.player == PlayerState.IDLE
    cache.block.set()
    await task
    assert player.loads == []


async def test_playlist_downloads_first_then_appends_rest(tmp_path: Path, example_config, clock: FakeClock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    ids = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
    cache = FakeCache(downloads={video_id: _media(tmp_path, video_id) for video_id in ids})
    cache.fail["bbbbbbbbbbb"] = "Private video"
    controller.cache = cache
    controller.ytdlp = FakeYtDlp({"PLxxxxxxxxxxxxxxxxxxxxxxxxxxxx": ids})  # type: ignore[assignment]
    await controller.dispatch("select", {"name": "Sesame Street Songs"})
    assert controller.state.player == PlayerState.PLAYING
    assert player.loads[0]["target"] == cache.files["aaaaaaaaaaa"]
    assert controller.state.now is not None
    assert controller.state.now.video_id == "aaaaaaaaaaa"
    assert controller.state.now.playlist_count == 3
    assert controller._append_task is not None
    await controller._append_task
    assert [load["target"] for load in player.loads] == [cache.files["aaaaaaaaaaa"], cache.files["ccccccccccc"]]
    assert controller.state.now.playlist_count == 2  # the private one was dropped
    assert cache.calls == ids


async def test_next_and_previous_move_inside_the_playlist(tmp_path: Path, example_config, clock: FakeClock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    ids = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
    cache = FakeCache(downloads={video_id: _media(tmp_path, video_id) for video_id in ids + [BABY]})
    controller.cache = cache
    controller.ytdlp = FakeYtDlp({"PLxxxxxxxxxxxxxxxxxxxxxxxxxxxx": ids})  # type: ignore[assignment]
    await controller.dispatch("select", {"name": "Sesame Street Songs"})
    assert controller._append_task is not None
    await controller._append_task
    assert controller.state.now is not None and controller.state.now.playlist_index == 0

    await controller.dispatch("next", {})
    now = controller.state.now
    assert now is not None and now.playlist_index == 1
    assert now.video_id == "bbbbbbbbbbb"  # follows the file mpv moved to
    assert now.local_file == cache.files["bbbbbbbbbbb"]
    assert now.thumbnail_url == "https://i.ytimg.com/vi/bbbbbbbbbbb/hqdefault.jpg"
    assert now.catalog_name == "Sesame Street Songs"
    assert controller.state.selected == "Sesame Street Songs"  # not the next catalog entry

    await controller.dispatch("previous", {})
    now = controller.state.now
    assert now is not None and now.playlist_index == 0 and now.video_id == "aaaaaaaaaaa"
    assert player.prev_count == 1

    # At the first item, previous leaves the playlist and steps the catalog.
    await controller.dispatch("previous", {})
    assert controller.state.selected == "Baby Shark"


async def test_replacing_a_playing_item_keeps_now_playing(tmp_path: Path, example_config, clock: FakeClock) -> None:
    """mpv ends the replaced file with reason "stop"; that must not blank the new item."""
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    ids = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
    cache = FakeCache(downloads={video_id: _media(tmp_path, video_id) for video_id in ids + [BABY]})
    controller.cache = cache
    controller.ytdlp = FakeYtDlp({"PLxxxxxxxxxxxxxxxxxxxxxxxxxxxx": ids})  # type: ignore[assignment]

    # The reported sequence: play, next, then pick a playlist while it plays.
    await controller.dispatch("select", {"name": "Baby Shark"})
    assert player.active
    await controller.dispatch("next", {})
    assert controller.state.player == PlayerState.PLAYING
    assert controller.state.now is not None and controller.state.now.title == "Sesame Street Songs"
    assert controller._append_task is not None
    await controller._append_task
    await controller.dispatch("select", {"name": "Sesame Street Songs"})
    assert controller._append_task is not None
    await controller._append_task

    assert controller.state.player == PlayerState.PLAYING
    now = controller.state.now
    assert now is not None
    assert now.title == "Sesame Street Songs"
    assert now.video_id == "aaaaaaaaaaa"
    assert now.thumbnail_url
    assert player.stopped == 0  # nothing asked mpv to stop

    # Moving inside the playlist also ends the old file with "stop".
    await controller.dispatch("next", {})
    assert controller.state.player == PlayerState.PLAYING
    assert controller.state.now is not None and controller.state.now.playlist_index == 1

    # A real stop still goes idle.
    await controller.dispatch("stop", {})
    assert controller.state.player == PlayerState.IDLE
    assert controller.state.now is None
    assert player.stopped == 1


async def test_titles_come_from_youtube_not_file_names(tmp_path: Path, example_config, clock: FakeClock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    ids = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
    cache = FakeCache(downloads={video_id: _media(tmp_path, video_id) for video_id in ids})
    controller.cache = cache
    titles = {"aaaaaaaaaaa": "Elmo's Song", "bbbbbbbbbbb": "Rubber Duckie"}
    controller.ytdlp = FakeYtDlp({"PLxxxxxxxxxxxxxxxxxxxxxxxxxxxx": ids}, titles)  # type: ignore[assignment]
    await controller.dispatch("select", {"name": "Sesame Street Songs"})
    assert controller._append_task is not None
    await controller._append_task
    now = controller.state.now
    assert now is not None
    assert now.catalog_name == "Sesame Street Songs"
    assert now.title == "Elmo's Song"  # the scan's title, not the catalog name

    # mpv reports the cached file's name as media-title; that is never a title.
    player.emit({"event": "property-change", "name": "media-title", "data": "aaaaaaaaaaa.mkv"})
    await controller.drain()
    assert controller.state.now is not None and controller.state.now.title == "Elmo's Song"

    await controller.dispatch("next", {})
    now = controller.state.now
    assert now is not None and now.video_id == "bbbbbbbbbbb"
    assert now.title == "Rubber Duckie"  # follows the item mpv moved to

    await controller.dispatch("next", {})
    now = controller.state.now
    assert now is not None and now.video_id == "ccccccccccc"
    assert now.title == "Sesame Street Songs"  # unknown title: catalog name, not the old title
    player.emit({"event": "property-change", "name": "media-title", "data": "ccccccccccc.mkv"})
    await controller.drain()
    assert controller.state.now is not None and controller.state.now.title == "Sesame Street Songs"


async def test_single_video_title_from_cache(tmp_path: Path, example_config, clock: FakeClock) -> None:
    controller, _player, _screen = make_controller(tmp_path, example_config, clock)
    cache = FakeCache(files={BABY: _media(tmp_path, BABY)})
    cache.titles[BABY] = "Baby Shark Dance"
    controller.cache = cache
    controller.cache_lookup = cache.get
    await controller.dispatch("select", {"name": "Baby Shark"})
    now = controller.state.now
    assert now is not None
    assert now.catalog_name == "Baby Shark"
    assert now.title == "Baby Shark Dance"


async def test_file_loaded_rebuilds_now_playing_if_it_was_cleared(
    tmp_path: Path, example_config, clock: FakeClock
) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    cache = FakeCache(files={BABY: _media(tmp_path, BABY)})
    controller.cache = cache
    controller.cache_lookup = cache.get
    await controller.dispatch("select", {"name": "Baby Shark"})
    # A stray late "stop" (an actual stop that raced a new load) wipes the state...
    controller._stop_requested = True
    player.emit({"event": "end-file", "reason": "stop"})
    await controller.drain()
    controller._stop_requested = False
    assert controller.state.now is None
    # ...but mpv then tells us the file we asked for is playing.
    player.emit({"event": "file-loaded", "path": cache.files[BABY]})
    await controller.drain()
    assert controller.state.player == PlayerState.PLAYING
    now = controller.state.now
    assert now is not None
    assert now.title == "Baby Shark"
    assert now.video_id == BABY
    assert now.local_file == cache.files[BABY]


async def test_next_waits_for_an_item_still_downloading(tmp_path: Path, example_config, clock: FakeClock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    ids = ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    cache = FakeCache(downloads={video_id: _media(tmp_path, video_id) for video_id in ids})
    controller.cache = cache
    controller.ytdlp = FakeYtDlp({"PLxxxxxxxxxxxxxxxxxxxxxxxxxxxx": ids})  # type: ignore[assignment]
    cache.block = asyncio.Event()
    cache.files["aaaaaaaaaaa"] = cache.downloads["aaaaaaaaaaa"]  # first is a hit, second blocks
    await controller.dispatch("select", {"name": "Sesame Street Songs"})
    assert controller.state.player == PlayerState.PLAYING
    await controller.dispatch("next", {})
    assert player.next_count == 0
    assert controller.state.selected == "Sesame Street Songs"
    cache.block.set()
    assert controller._append_task is not None
    await controller._append_task
    await controller.dispatch("next", {})
    assert player.next_count == 1
    assert controller.state.now is not None and controller.state.now.video_id == "bbbbbbbbbbb"


async def test_player_error_retries_once_then_forgets(tmp_path: Path, example_config, clock: FakeClock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    cache = FakeCache(files={BABY: _media(tmp_path, BABY)})
    controller.cache = cache
    controller.cache_lookup = cache.get
    player.failures_left = 2
    await controller.dispatch("play_media", {"ref": BABY})
    assert controller.state.player == PlayerState.ERROR
    assert controller.state.last_error == "player"
    await clock.advance(3)
    assert len(player.loads) == 2
    assert controller.state.player == PlayerState.ERROR
    assert cache.forgotten == [BABY]


async def test_mpv_player_observes_properties_on_connect(tmp_path: Path) -> None:
    player = MpvPlayer(str(tmp_path / "mpv.sock"), ytdl_format="best")
    stub = StubClient()
    player._client = stub  # type: ignore[assignment]
    player.connected = True
    await player._reapply()
    observed = [call[2] for call in stub.calls if call[0] == "observe_property"]
    assert observed == list(OBSERVED)
    assert not any(call[1] == "ytdl-format" for call in stub.calls if call[0] == "set_property")
    await player.load(None, "/tmp/x.mkv", append=True)
    assert stub.calls[-1] == ("loadfile", "/tmp/x.mkv", "append")


async def test_pause_property_tracks_state(tmp_path: Path, example_config, clock: FakeClock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    await controller.dispatch("play_media", {"ref": BABY})
    player.emit({"event": "property-change", "name": "pause", "data": True})
    await controller.drain()
    assert controller.state.player == PlayerState.PAUSED
    player.emit({"event": "property-change", "name": "pause", "data": False})
    await controller.drain()
    assert controller.state.player == PlayerState.PLAYING


def test_ytdlp_args_honour_cookies_and_extra(example_config) -> None:
    from pikiosk.app import ytdlp_args

    config = example_config.model_copy(
        update={
            "ytdlp": example_config.ytdlp.model_copy(
                update={"cookies_file": "~/cookies.txt", "extra_args": ["--sleep-requests", "1"]}
            )
        }
    )
    args = ytdlp_args(config)
    assert "--cookies" in args
    assert args[-2:] == ["--sleep-requests", "1"]
    assert "cookies.txt" in args[args.index("--cookies") + 1]
