"""Phase 1: file logs, commit and audio facts, mpv log tail on the status page."""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from pikiosk.clock import FakeClock
from pikiosk.config import LoggingConfig
from pikiosk.diag import audio_sink, git_commit, tail
from pikiosk.logging_setup import setup_logging
from pikiosk.telemetry import collect
from pikiosk.web.admin import create_admin_app
from tests.conftest import make_controller
from tests.unit.test_admin import _login, _store


def test_git_commit_reads_refs_without_git(tmp_path: Path) -> None:
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads" / "main").write_text("2fe4c77abcdef0123456789\n", encoding="utf-8")
    assert git_commit(tmp_path) == "2fe4c77ab"
    (git / "refs" / "heads" / "main").unlink()
    (git / "packed-refs").write_text(
        "# pack-refs with: peeled\n1111111aaaaaaa refs/heads/main\n", encoding="utf-8"
    )
    assert git_commit(tmp_path) == "1111111aa"
    (git / "HEAD").write_text("deadbeefcafe0000\n", encoding="utf-8")
    assert git_commit(tmp_path) == "deadbeefc"
    assert git_commit(tmp_path / "nowhere") == ""


def test_tail_and_audio_sink_degrade(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "mpv.log"
    log.write_text("\n".join(f"line {i}" for i in range(30)) + "\n", encoding="utf-8")
    assert tail(log, 3) == ["line 27", "line 28", "line 29"]
    assert tail(tmp_path / "missing.log") == []
    monkeypatch.setattr("pikiosk.diag.subprocess.run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert audio_sink() is None
    payload = collect(mqtt_host="127.0.0.1", commit="abc1234", audio_sink="alsa_output.hdmi")
    assert payload["commit"] == "abc1234"
    assert payload["audio_sink"] == "alsa_output.hdmi"


def test_default_log_file_is_written_and_rotates(tmp_path: Path) -> None:
    target = tmp_path / "logs" / "pikioskd.log"
    setup_logging(LoggingConfig(level="INFO"), default_file=str(target))
    logging.getLogger("pikiosk.test").info("hello file")
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = target.read_text(encoding="utf-8")
    assert "hello file" in text
    assert text[:4].isdigit()
    explicit = tmp_path / "explicit.log"
    setup_logging(LoggingConfig(level="INFO", file=str(explicit)), default_file=str(target))
    logging.getLogger("pikiosk.test").info("explicit wins")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "explicit wins" in explicit.read_text(encoding="utf-8")
    setup_logging(LoggingConfig(level="INFO"))


async def test_controller_keeps_mpv_log_tail(tmp_path: Path, example_config, clock: FakeClock) -> None:
    controller, player, _screen = make_controller(tmp_path, example_config, clock)
    for index in range(12):
        player.emit({"event": "log-message", "level": "warn", "prefix": "ffmpeg", "text": f"w{index}\n"})
    player.emit({"event": "log-message", "level": "error", "prefix": "vo", "text": "Failed to open\n"})
    await controller.drain()
    lines = controller.mpv_log_tail()
    assert len(lines) == 10
    assert lines[-1] == "[error] vo: Failed to open"
    assert lines[0] == "[warn] ffmpeg: w3"
    assert controller._mpv_log == "Failed to open"


async def test_status_page_shows_device_facts(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path)
    controller, _player, _screen = make_controller(tmp_path, store.config, clock)

    def info() -> dict:
        return {
            "version": "1.0.0",
            "commit": "2fe4c77ab",
            "audio_sink": "alsa_output.hdmi",
            "cache_used_gb": 1.5,
            "cache_items": 3,
            "ytdlp_version": "2026.10.10",
            "mpv_log": ["[error] vo: Failed to open"],
        }

    app = create_admin_app(store, controller, info=info)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await _login(client)
        html = await (await client.get("/status")).text()
        assert "2fe4c77ab" in html
        assert "alsa_output.hdmi" in html
        assert "1.5 GB, 3 videos" in html
        assert "Failed to open" in html
        assert "2026.10.10" in html
    finally:
        await client.close()


async def test_status_page_without_info_callback(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path)
    controller, _player, _screen = make_controller(tmp_path, store.config, clock)
    app = create_admin_app(store, controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await _login(client)
        html = await (await client.get("/status")).text()
        assert "Version" in html and "1.0.0" in html
    finally:
        await client.close()
