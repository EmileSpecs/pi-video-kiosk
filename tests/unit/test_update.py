from __future__ import annotations

import subprocess
from pathlib import Path

from pikiosk.updater import RealGit, UpdateError, run_update


class FakeGit:
    def __init__(self) -> None:
        self.sha = "aaa"
        self.remote = "aaa"
        self.pulled = False
        self.reset_to: str | None = None
        self.installed = 0

    def head(self) -> str:
        return self.sha

    def fetch(self) -> None:
        return None

    def remote_sha(self, ref: str) -> str:
        return self.remote

    def pull_ff_only(self, ref: str) -> None:
        self.pulled = True
        self.sha = self.remote

    def reset_hard(self, sha: str) -> None:
        self.reset_to = sha
        self.sha = sha


def test_skip_when_busy() -> None:
    git = FakeGit()
    git.remote = "bbb"
    result = run_update(
        git,
        ref="main",
        player_state="playing",
        only_when_idle=True,
        install=lambda: None,
        health=lambda: True,
    )
    assert result.status == "skipped_busy"
    assert git.pulled is False


def test_rollback_when_unhealthy() -> None:
    git = FakeGit()
    git.remote = "bbb"
    installs: list[str] = []

    def install() -> None:
        installs.append(git.sha)

    result = run_update(
        git,
        ref="main",
        player_state="idle",
        only_when_idle=True,
        install=install,
        health=lambda: False,
    )
    assert result.status == "rolled_back"
    assert git.sha == "aaa"
    assert installs == ["bbb", "aaa"]


def test_real_git_fast_forward(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    clone = tmp_path / "clone"
    _git(origin, "init")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    (origin / "readme.txt").write_text("one", encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "config", "user.email", "kiosk@example.com")
    _git(origin, "config", "user.name", "kiosk")
    _git(origin, "commit", "-m", "one")
    _git(tmp_path, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "kiosk@example.com")
    _git(clone, "config", "user.name", "kiosk")
    (origin / "readme.txt").write_text("two", encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "two")
    git = RealGit(clone)
    result = run_update(
        git,
        ref="main",
        player_state="idle",
        only_when_idle=True,
        install=lambda: None,
        health=lambda: True,
    )
    assert result.status == "updated"
    assert (clone / "readme.txt").read_text(encoding="utf-8") == "two"
    failing = RealGit(clone)
    (origin / "readme.txt").write_text("three", encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "three")
    rolled = run_update(
        failing,
        ref="main",
        player_state="idle",
        only_when_idle=True,
        install=lambda: None,
        health=lambda: False,
    )
    assert rolled.status == "rolled_back"
    assert (clone / "readme.txt").read_text(encoding="utf-8") == "two"


def test_up_to_date() -> None:
    git = FakeGit()
    result = run_update(git, ref="main", player_state="idle", only_when_idle=True, install=lambda: None)
    assert result.status == "up_to_date"


def test_pull_failure() -> None:
    class Boom(FakeGit):
        def pull_ff_only(self, ref: str) -> None:
            raise UpdateError("diverged")

    git = Boom()
    git.remote = "ccc"
    result = run_update(git, ref="main", player_state="idle", only_when_idle=False, install=lambda: None)
    assert result.status == "failed"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
