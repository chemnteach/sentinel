"""``commit_state`` bool-return contract (P1 error_signal_loss).

``commit_state`` previously returned ``None`` whether or not the git commit
succeeded, so callers could not tell a committed audit trail from a
skipped/failed one. It now returns ``True`` on a successful commit and
``False`` when the commit is skipped or fails (non-fatal — state is already on
disk). These tests pin that contract by stubbing ``subprocess.run``.
"""

from __future__ import annotations

import subprocess

import pytest

from sentinel.orchestrator.state_store import StateStore


@pytest.fixture
def store(tmp_path):
    return StateStore(state_dir=tmp_path)


def test_commit_state_returns_true_on_success(store, monkeypatch):
    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(
        "sentinel.orchestrator.state_store.subprocess.run", _fake_run
    )
    assert store.commit_state("sentinel: test commit") is True


def test_commit_state_returns_false_on_git_failure(store, monkeypatch):
    def _fake_run(cmd, **kwargs):
        # git commit exits non-zero (e.g. nothing to commit / not a repo).
        raise subprocess.CalledProcessError(
            returncode=1, cmd=cmd, stderr=b"nothing to commit"
        )

    monkeypatch.setattr(
        "sentinel.orchestrator.state_store.subprocess.run", _fake_run
    )
    # Non-fatal: does not raise, but signals failure via the boolean.
    assert store.commit_state("sentinel: test commit") is False


def test_commit_state_failure_is_not_fatal(store, monkeypatch):
    """A git failure must not raise — state is already durably written."""

    def _fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(returncode=128, cmd=cmd, stderr=None)

    monkeypatch.setattr(
        "sentinel.orchestrator.state_store.subprocess.run", _fake_run
    )
    # Should return False rather than propagate the error.
    assert store.commit_state("sentinel: test") is False
