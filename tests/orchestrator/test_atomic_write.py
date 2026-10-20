"""Tests for StateStore.atomic_write / atomic_append — the shared multi-store
atomic transaction primitive."""
from __future__ import annotations

import threading

import pytest

from sentinel.orchestrator.state_store import StateStore, StateStoreError


def _store(tmp_path):
    return StateStore(tmp_path)


# ---------------------------------------------------------------------------
# atomic_write
# ---------------------------------------------------------------------------

def test_atomic_write_two_keys_single_commit(tmp_path, monkeypatch):
    store = _store(tmp_path)
    commits: list[str] = []
    monkeypatch.setattr(store, "commit_state", lambda msg: commits.append(msg))

    store.atomic_write([("findings", [{"id": "F1"}]), ("modules", [{"name": "m"}])])

    assert store.read("findings") == [{"id": "F1"}]
    assert store.read("modules") == [{"name": "m"}]
    assert len(commits) == 1  # one commit for the whole transaction


def test_atomic_write_no_commit_when_disabled(tmp_path, monkeypatch):
    store = _store(tmp_path)
    commits: list[str] = []
    monkeypatch.setattr(store, "commit_state", lambda msg: commits.append(msg))

    store.atomic_write([("findings", [{"id": "F1"}])], commit=False)

    assert store.read("findings") == [{"id": "F1"}]
    assert commits == []


def test_atomic_write_rolls_back_all_on_failure(tmp_path):
    store = _store(tmp_path)
    store.write("findings", [{"orig": True}], commit=False)

    # Second op carries a non-JSON-serializable value → its write raises,
    # so the whole transaction must roll back.
    with pytest.raises(StateStoreError):
        store.atomic_write(
            [("findings", [{"new": 1}]), ("modules", {1, 2})]
        )

    assert store.read("findings") == [{"orig": True}]  # rolled back
    assert store.read("modules") == []  # never written


def test_atomic_write_expected_revisions_raises_on_mismatch(tmp_path):
    store = _store(tmp_path)
    store.write("plan", {"revision": 5, "data": "x"}, commit=False)

    with pytest.raises(StateStoreError):
        store.atomic_write(
            [("plan", {"revision": 6, "data": "y"})],
            expected_revisions={"plan": 3},
        )

    # No write happened — plan is unchanged.
    assert store.read("plan") == {"revision": 5, "data": "x"}


def test_atomic_write_expected_revisions_succeeds_on_match(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.write("plan", {"revision": 5, "data": "x"}, commit=False)
    monkeypatch.setattr(store, "commit_state", lambda msg: None)

    store.atomic_write(
        [("plan", {"revision": 6, "data": "y"})],
        expected_revisions={"plan": 5},
    )

    assert store.read("plan") == {"revision": 6, "data": "y"}


# ---------------------------------------------------------------------------
# atomic_append
# ---------------------------------------------------------------------------

def test_atomic_append_to_existing_list(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(store, "commit_state", lambda msg: None)
    store.write("findings", [{"id": "F1"}], commit=False)

    store.atomic_append("findings", {"id": "F2"})

    assert store.read("findings") == [{"id": "F1"}, {"id": "F2"}]


def test_atomic_append_creates_new_list_when_absent(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(store, "commit_state", lambda msg: None)

    store.atomic_append("findings", {"id": "F1"})

    assert store.read("findings") == [{"id": "F1"}]


def test_atomic_append_raises_when_not_a_list(tmp_path):
    store = _store(tmp_path)
    store.write("plan", {"revision": 1}, commit=False)  # dict-valued store

    with pytest.raises(StateStoreError):
        store.atomic_append("plan", {"id": "F1"})


# ---------------------------------------------------------------------------
# atomic_read_modify_write — multi-key read-modify-write under one lock
# ---------------------------------------------------------------------------

def test_atomic_read_modify_write_applies_transforms(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(store, "commit_state", lambda msg: None)
    store.write("pr_reviews", [{"id": "R1"}], commit=False)

    store.atomic_read_modify_write(
        [
            ("pr_reviews", lambda cur: (cur or []) + [{"id": "R2"}]),
            ("audit_log", lambda cur: (cur or []) + [{"event": "e1"}]),
        ],
        commit=False,
    )

    assert store.read("pr_reviews") == [{"id": "R1"}, {"id": "R2"}]
    assert store.read("audit_log") == [{"event": "e1"}]


def test_atomic_read_modify_write_rolls_back_on_failure(tmp_path):
    store = _store(tmp_path)
    store.write("pr_reviews", [{"orig": True}], commit=False)

    # Second transform returns a non-JSON-serializable value → its write raises,
    # so the whole transaction (including the first key) must roll back.
    with pytest.raises(StateStoreError):
        store.atomic_read_modify_write(
            [
                ("pr_reviews", lambda cur: (cur or []) + [{"new": 1}]),
                ("audit_log", lambda cur: {1, 2}),
            ]
        )

    assert store.read("pr_reviews") == [{"orig": True}]  # rolled back
    assert store.read("audit_log") == []  # never written


def test_atomic_read_modify_write_reads_current_value_inside_lock(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(store, "commit_state", lambda msg: None)
    store.write("pr_reviews", [], commit=False)

    N = 24
    barrier = threading.Barrier(N)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            barrier.wait()
            store.atomic_read_modify_write(
                [("pr_reviews", lambda cur, i=i: (cur or []) + [{"i": i}])],
                commit=False,
            )
        except Exception as exc:  # surface any failure in the assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    result = store.read("pr_reviews")
    assert len(result) == N  # no lost update
    assert sorted(x["i"] for x in result) == list(range(N))


def test_atomic_append_concurrent_no_lost_update(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(store, "commit_state", lambda msg: None)
    store.write("pr_reviews", [], commit=False)

    N = 24
    barrier = threading.Barrier(N)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            barrier.wait()  # maximize contention: everyone reads at the same time
            store.atomic_append("pr_reviews", {"i": i}, commit=False)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    result = store.read("pr_reviews")
    assert len(result) == N  # every append survives — no lost update
    assert sorted(x["i"] for x in result) == list(range(N))

