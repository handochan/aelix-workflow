"""Declared write scopes: conflict tests for scheduling and git-observed changes.

A scope is a list of project-relative globs. An empty scope means "anything", so an
undeclared writer conflicts with every other writer and is never checked for overreach.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from functools import lru_cache
from typing import Any

Snapshot = dict[str, tuple[str, int, int]]
_WILDCARD = re.compile(r"[*?\[]")


def _prefix(glob: str) -> list[str]:
    segments = glob.split("/")
    for index, segment in enumerate(segments):
        if _WILDCARD.search(segment):
            return segments[:index]
    return segments


def overlaps(left: Iterable[str], right: Iterable[str]) -> bool:
    """Conservative: two scopes overlap unless their literal prefixes diverge."""
    left, right = list(left), list(right)
    if not left or not right:
        return True
    for a in left:
        for b in right:
            pa, pb = _prefix(a), _prefix(b)
            shorter = min(len(pa), len(pb))
            if pa[:shorter] == pb[:shorter]:
                return True
    return False


@lru_cache(maxsize=256)
def _pattern(glob: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    while index < len(glob):
        if glob.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif glob.startswith("**", index):
            parts.append(".*")
            index += 2
        elif glob[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif glob[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(glob[index]))
            index += 1
    return re.compile("".join(parts))


def matches(path: str, scope: Iterable[str]) -> bool:
    return any(_pattern(glob).fullmatch(path) for glob in scope)


def changed(before: Snapshot, after: Snapshot) -> set[str]:
    return {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}


# Read-only and lock-free: never take index.lock under running children, never run a
# repository-configured fsmonitor command, and stay out of submodules.
_GIT = ["-c", "core.fsmonitor=false", "--no-optional-locks"]


class GitWorkspace:
    """Observe the working tree through ``git status``; disabled outside a repository.

    Only constructed for a trusted project: ``git status`` can still run clean filters that
    the repository's own configuration names.
    """

    def __init__(self, api: Any, cwd: str) -> None:
        self.api = api
        self.cwd = cwd
        self.enabled = True
        self._root: str | None = None
        self._prefix = ""

    async def _git(self, *args: str) -> str | None:
        try:
            result = await self.api.exec("git", [*_GIT, *args], cwd=self.cwd, timeout_ms=15_000)
        except Exception:
            return None
        return result.stdout if result.code == 0 else None

    async def snapshot(self) -> Snapshot | None:
        if not self.enabled:
            return None
        if self._root is None:
            root = await self._git("rev-parse", "--show-toplevel")
            if not root or not root.strip():
                self.enabled = False
                return None
            self._root = os.path.realpath(root.strip())
            relative = os.path.relpath(os.path.realpath(self.cwd), self._root)
            self._prefix = "" if relative == "." else relative.replace(os.sep, "/") + "/"
        output = await self._git(
            "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all"
        )
        if output is None:
            self.enabled = False
            return None
        state: Snapshot = {}
        fields = output.split("\0")
        index = 0
        while index < len(fields):
            record = fields[index]
            index += 1
            if len(record) < 4:
                continue
            status, path = record[:2], record[3:]
            if status[0] in "RC":
                index += 1  # A rename or copy is followed by its source path.
            try:
                stat = os.stat(os.path.join(self._root, path))
                observed = (status, stat.st_mtime_ns, stat.st_size)
            except OSError:
                observed = (status, 0, -1)
            # Porcelain paths are relative to the repository root; scopes are relative to
            # the session directory. A path outside it can never match a scope.
            if path.startswith(self._prefix):
                state[path[len(self._prefix) :]] = observed
            else:
                state[":/" + path] = observed
        return state
