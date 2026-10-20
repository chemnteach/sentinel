"""Atomic-write retry contract for ``StateStore.write`` (Windows AV/lock).

On Windows, a virus scanner or file watcher can transiently lock the
destination file at the exact moment of ``os.replace``, raising
``PermissionError`` (WinError 5). A single such failure must not abort a
multi-hour assessment, so the atomic replace retries with backoff before
surfacing a ``StateStoreError``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.orchestrator.state_store import StateStore, StateStoreError


@pytest.fixture
def store(tmp_path):
    return StateStore(state_dir=tmp_path)


def test_write_retries_transient_replace_lock(store, monkeypatch):
    calls = {"n": 0}
    real_replace = Path.replace

    def _flaky_replace(self, target):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _flaky_replace, raising=True)
    monkeypatch.setattr(
        "sentinel.orchestrator.state_store.time.sleep", lambda _s: None
    )

    store.write("findings", [{"id": "F1"}])

    assert calls["n"] == 3  # failed twice, succeeded on the third
    assert store.read("findings") == [{"id": "F1"}]


def test_write_raises_after_persistent_replace_lock(store, monkeypatch):
    def _always_locked(self, target):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(Path, "replace", _always_locked, raising=True)
    monkeypatch.setattr(
        "sentinel.orchestrator.state_store.time.sleep", lambda _s: None
    )

    with pytest.raises(StateStoreError):
        store.write("findings", [{"id": "F1"}])


def test_write_succeeds_first_try_without_sleep(store, monkeypatch):
    slept = {"n": 0}
    monkeypatch.setattr(
        "sentinel.orchestrator.state_store.time.sleep",
        lambda _s: slept.__setitem__("n", slept["n"] + 1),
    )
    store.write("findings", [{"id": "F1"}])
    assert slept["n"] == 0  # no retry sleep on the happy path
    assert store.read("findings") == [{"id": "F1"}]
