"""Git-based app update with an idle check and health-check rollback."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pikiosk import __version__

LAST_UPDATE_NAME = "last_update.json"


class UpdateError(RuntimeError):
    pass


def last_update_path(state_file: Path | None = None) -> Path:
    """Sibling of state.json, or the default XDG state location."""
    if state_file is not None:
        return state_file.with_name(LAST_UPDATE_NAME)
    return Path.home() / ".local" / "state" / "pi-kiosk" / LAST_UPDATE_NAME


def read_last_update(path: Path | None = None) -> dict[str, Any] | None:
    file = path or last_update_path()
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def write_last_update(
    status: str,
    *,
    path: Path | None = None,
    sha: str | None = None,
    version: str | None = None,
    detail: str | None = None,
) -> Path:
    """Record what the update script just did so the admin UI can show it."""
    file = path or last_update_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "status": status,
        "version": version or __version__,
        "sha": (sha or "")[:9] or None,
        "detail": detail,
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    file.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return file


class GitOps(Protocol):
    def head(self) -> str: ...

    def fetch(self) -> None: ...

    def remote_sha(self, ref: str) -> str: ...

    def pull_ff_only(self, ref: str) -> None: ...

    def reset_hard(self, sha: str) -> None: ...


@dataclass(frozen=True)
class UpdateResult:
    status: str
    sha: str | None = None
    detail: str | None = None


class RealGit:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _run(self, *args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=self.root,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise UpdateError(exc.stderr.strip() or exc.stdout.strip() or "git failed") from exc
        return completed.stdout.strip()

    def head(self) -> str:
        return self._run("rev-parse", "HEAD")

    def fetch(self) -> None:
        self._run("fetch", "--prune", "origin")

    def remote_sha(self, ref: str) -> str:
        return self._run("rev-parse", f"origin/{ref}")

    def pull_ff_only(self, ref: str) -> None:
        self._run("pull", "--ff-only", "origin", ref)

    def reset_hard(self, sha: str) -> None:
        self._run("reset", "--hard", sha)


def run_update(
    git: GitOps,
    *,
    ref: str,
    player_state: str,
    only_when_idle: bool,
    install: Callable[[], None],
    health: Callable[[], bool] | None = None,
) -> UpdateResult:
    if only_when_idle and player_state in {"playing", "loading"}:
        return UpdateResult("skipped_busy")
    before = git.head()
    git.fetch()
    remote = git.remote_sha(ref)
    if remote == before:
        return UpdateResult("up_to_date", before)
    try:
        git.pull_ff_only(ref)
    except UpdateError as exc:
        return UpdateResult("failed", before, str(exc))
    install()
    if health is not None and not health():
        git.reset_hard(before)
        install()
        return UpdateResult("rolled_back", before, "health check failed")
    return UpdateResult("updated", git.head())
