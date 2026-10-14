from __future__ import annotations

import shutil
import subprocess

import pytest
from aelix_coding_agent.extensions.api import Extension, ExtensionAPI, _ExtensionRuntime

from aelix_workflow.workspace import GitWorkspace, changed, matches, overlaps


@pytest.mark.parametrize(
    "left, right, expected",
    [
        (["src/auth/**"], ["src/auth/session.py"], True),
        (["src/auth/**"], ["src/billing/**"], False),
        (["docs/*.md"], ["src/app.py"], False),
        (["*.md"], ["src/app.py"], True),
        (["a.py"], ["b.py"], False),
        (["a.py"], ["a.py"], True),
        ([], ["anything"], True),
    ],
)
def test_scope_overlap_is_conservative(left, right, expected):
    assert overlaps(left, right) is expected
    assert overlaps(right, left) is expected


def test_glob_matching():
    assert matches("src/auth/x/y.py", ["src/auth/**"])
    assert not matches("src/auth", ["src/auth/**"])
    assert matches("docs/a.md", ["docs/*.md"])
    assert not matches("docs/sub/a.md", ["docs/*.md"])
    assert matches("docs/sub/a.md", ["docs/**/*.md"])
    assert matches("docs/a.md", ["docs/**/*.md"])
    assert matches("README.md", ["README.md", "x"])


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
async def test_git_snapshots_show_what_changed(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "kept.py").write_text("a\n")
    (tmp_path / "dirty.py").write_text("a\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "init")
    (tmp_path / "dirty.py").write_text("b\n")
    api = ExtensionAPI(Extension(name="t"), _ExtensionRuntime())
    workspace = GitWorkspace(api, str(tmp_path))
    before = await workspace.snapshot()
    assert before is not None and set(before) == {"dirty.py"}
    (tmp_path / "dirty.py").write_text("changed again, longer\n")
    (tmp_path / "kept.py").write_text("now modified\n")
    (tmp_path / "new dir").mkdir()
    (tmp_path / "new dir" / "한글.txt").write_text("x")
    after = await workspace.snapshot()
    assert after is not None
    assert changed(before, after) == {"dirty.py", "kept.py", "new dir/한글.txt"}


async def test_a_directory_outside_git_disables_observation(tmp_path):
    api = ExtensionAPI(Extension(name="t"), _ExtensionRuntime())
    workspace = GitWorkspace(api, str(tmp_path))
    assert await workspace.snapshot() is None
    assert not workspace.enabled


class RecordingApi:
    def __init__(self, outputs):
        self.calls = []
        self.outputs = outputs

    async def exec(self, command, args, *, cwd=None, timeout_ms=None):
        from aelix_coding_agent.extensions.api import ExecResult

        self.calls.append([command, *args])
        return ExecResult(stdout=self.outputs.pop(0), stderr="", code=0, killed=False)


async def test_git_runs_without_locks_or_repository_monitors(tmp_path):
    api = RecordingApi([str(tmp_path) + "\n", ""])
    await GitWorkspace(api, str(tmp_path)).snapshot()
    for call in api.calls:
        assert call[:4] == ["git", "-c", "core.fsmonitor=false", "--no-optional-locks"]
    assert "--ignore-submodules=all" in api.calls[1]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
async def test_a_configured_fsmonitor_never_runs(tmp_path):
    git(tmp_path, "init", "-q")
    hook = tmp_path / "monitor.sh"
    witness = tmp_path / "ran.txt"
    hook.write_text(f"#!/bin/sh\necho ran > '{witness}'\n")
    hook.chmod(0o755)
    git(tmp_path, "config", "core.fsmonitor", str(hook))
    api = ExtensionAPI(Extension(name="t"), _ExtensionRuntime())
    assert await GitWorkspace(api, str(tmp_path)).snapshot() is not None
    assert not witness.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
async def test_paths_are_relative_to_the_session_directory(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    package = tmp_path / "pkg"
    (package / "src").mkdir(parents=True)
    (package / "src" / "a.py").write_text("a\n")
    (tmp_path / "root.txt").write_text("r\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "init")
    api = ExtensionAPI(Extension(name="t"), _ExtensionRuntime())
    workspace = GitWorkspace(api, str(package))
    before = await workspace.snapshot()
    (package / "src" / "a.py").write_text("changed\n")
    (tmp_path / "root.txt").write_text("changed\n")
    after = await workspace.snapshot()
    assert before is not None and after is not None
    assert changed(before, after) == {"src/a.py", ":/root.txt"}
    assert matches("src/a.py", ["src/**"]) and not matches(":/root.txt", ["src/**"])
