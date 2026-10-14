"""Git-based app update with an idle check and health-check rollback."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class UpdateError(RuntimeError):
    pass


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
