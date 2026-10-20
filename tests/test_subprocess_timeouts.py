"""P1 timeout_handling_gap — every git subprocess.run call passes a timeout.

These tests intercept ``subprocess.run`` via monkeypatch and assert that the
``timeout`` kwarg is present (and non-None) on each git invocation. They guard
against a regression where a git operation hangs indefinitely with no deadline.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import sentinel.cli as cli
import sentinel.orchestrator.main as main_mod
import sentinel.orchestrator.state_store as state_store_mod
from sentinel.orchestrator.main import Orchestrator
from sentinel.orchestrator.state_store import StateStore


class _Recorder:
    """Captures the kwargs of every subprocess.run call and returns a stub."""

    def __init__(self, stdout: str = "", returncode: int = 0):
        self.calls: list[dict] = []
        self._stdout = stdout
        self._returncode = returncode

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stdout=self._stdout, stderr="", returncode=self._returncode
        )

    def assert_all_have_timeout(self) -> None:
        assert self.calls, "expected at least one subprocess.run call"
        for kwargs in self.calls:
            assert "timeout" in kwargs, f"missing timeout in {kwargs}"
            assert kwargs["timeout"] is not None


def test_orchestrator_git_read_ops_pass_timeout(monkeypatch):
    rec = _Recorder(stdout="main\n")
    monkeypatch.setattr(main_mod.subprocess, "run", rec)

    fake = SimpleNamespace(_config=SimpleNamespace(repo_path="/repo"))
    Orchestrator._git_current_branch(fake)
    Orchestrator._git_head_sha(fake, "/repo")

    rec.assert_all_have_timeout()
    assert all(kw["timeout"] == 30 for kw in rec.calls)


def test_orchestrator_git_push_passes_timeout(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(main_mod.subprocess, "run", rec)

    fake = SimpleNamespace(_config=SimpleNamespace(repo_path="/repo"))
    Orchestrator._git_push_branch(fake, "sentinel/task-1")

    rec.assert_all_have_timeout()
    # Push operations get the longer 120s deadline.
    assert rec.calls[0]["timeout"] == 120


def test_orchestrator_git_commit_all_passes_timeout(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(main_mod.subprocess, "run", rec)

    fake = SimpleNamespace(_config=SimpleNamespace(repo_path="/repo"))
    Orchestrator._git_commit_all(fake, "msg")

    # git add + git commit both bounded.
    assert len(rec.calls) == 2
    rec.assert_all_have_timeout()


def test_cli_changed_source_files_passes_timeout(monkeypatch, tmp_path):
    rec = _Recorder(stdout="")
    monkeypatch.setattr(cli.subprocess, "run", rec)

    cli._changed_source_files_since(tmp_path, "abc123")

    # rev-parse verify + diff, both with a 30s deadline.
    assert len(rec.calls) == 2
    rec.assert_all_have_timeout()
    assert all(kw["timeout"] == 30 for kw in rec.calls)


def test_state_store_commit_state_passes_timeout(monkeypatch, tmp_path):
    rec = _Recorder()
    monkeypatch.setattr(state_store_mod.subprocess, "run", rec)

    store = StateStore(state_dir=tmp_path)
    # Force the git-commit path regardless of whether the state dir is in a repo.
    store._repo = tmp_path
    store.commit_state("checkpoint")

    # git add + git commit both bounded at 60s.
    assert len(rec.calls) == 2
    rec.assert_all_have_timeout()
    assert all(kw["timeout"] == 60 for kw in rec.calls)
