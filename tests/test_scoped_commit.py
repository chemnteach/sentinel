"""Cycle 58 (Commit 1) — _git_commit_scoped stages only the Coder's edits."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

from sentinel.orchestrator.main import Orchestrator


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def test_scoped_commit_stages_only_changed_files(tmp_path):
    repo = tmp_path
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.dev")
    _git(repo, "config", "user.name", "t")

    a = repo / "a.py"
    b = repo / "b.py"
    a.write_text("a = 1\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-m", "init")

    # Two pending working-tree changes: a.py modified, b.py new.
    a.write_text("a = 2\n", encoding="utf-8")
    b.write_text("b = 1\n", encoding="utf-8")

    # Scoped commit of only b.py — a.py's change must NOT be swept in.
    fake = SimpleNamespace(_config=SimpleNamespace(repo_path=str(repo)))
    Orchestrator._git_commit_scoped(fake, ["b.py"], "scoped: b only")

    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert committed == ["b.py"]
    # a.py's modification is still uncommitted in the working tree.
    assert "a.py" in _git(repo, "diff", "--name-only").stdout


def test_scoped_commit_no_files_is_noop(tmp_path):
    repo = tmp_path
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.dev")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-m", "init")
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    fake = SimpleNamespace(_config=SimpleNamespace(repo_path=str(repo)))
    # Staging a nonexistent file -> nothing staged -> no new commit, no crash.
    Orchestrator._git_commit_scoped(fake, ["does_not_exist.py"], "noop")

    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
