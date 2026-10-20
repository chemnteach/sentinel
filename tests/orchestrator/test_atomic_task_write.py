"""Tests for the atomic multi-store transaction primitives used by the
governance atomicity cluster: state_store.persist_task_with_audit(extra_operations)
and task_manager.atomic_task_write."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentinel.orchestrator.state_store import StateStore, StateStoreError
from sentinel.orchestrator.task_manager import Task, TaskManager
from sentinel.orchestrator.state_machine import TaskState


def _store(tmp_path) -> StateStore:
    return StateStore(tmp_path)


def _seed_task(store: StateStore, **overrides) -> Task:
    data = {
        "task_id": "task-1",
        "plan_id": "P-1",
        "state": "PR_CREATED",
        "test_stubs_approved": True,
        "todo_approved": True,
        "revision": 0,
    }
    data.update(overrides)
    store.write("tasks", [data], commit=False)
    return Task.from_dict(data)


# ---------------------------------------------------------------------------
# Layer 1 — state_store.persist_task_with_audit(extra_operations)
# ---------------------------------------------------------------------------

def test_persist_task_with_extra_operations_writes_all(tmp_path):
    store = _store(tmp_path)
    store.write("tasks", [{"task_id": "task-1", "state": "PR_CREATED", "revision": 0}], commit=False)

    store.persist_task_with_audit(
        {"task_id": "task-1", "state": "COMPLETE", "revision": 1},
        [{"action": "completed"}],
        extra_operations=[("pr_reviews", [{"task_id": "task-1", "decision": "APPROVED"}])],
    )

    assert store.get_task("task-1")["state"] == "COMPLETE"
    assert store.read("pr_reviews") == [{"task_id": "task-1", "decision": "APPROVED"}]
    assert any(e.get("action") == "completed" for e in store.read("audit_log"))


def test_persist_task_with_extra_operations_rolls_back_all_on_failure(tmp_path):
    store = _store(tmp_path)
    store.write("tasks", [{"task_id": "task-1", "state": "PR_CREATED", "revision": 0}], commit=False)
    store.write("pr_reviews", [{"orig": True}], commit=False)

    # A non-serializable extra value makes its write raise → whole txn rolls back.
    with pytest.raises(Exception):
        store.persist_task_with_audit(
            {"task_id": "task-1", "state": "COMPLETE", "revision": 1},
            [{"action": "completed"}],
            extra_operations=[("pr_reviews", {1, 2})],
        )

    # Task unchanged, pr_reviews unchanged — no partial state.
    assert store.get_task("task-1")["state"] == "PR_CREATED"
    assert store.read("pr_reviews") == [{"orig": True}]


# ---------------------------------------------------------------------------
# Layer 2 — task_manager.atomic_task_write
# ---------------------------------------------------------------------------

def test_atomic_task_write_applies_transition_chain(tmp_path):
    store = _store(tmp_path)
    tm = TaskManager(store)
    task = _seed_task(store, state="PR_CREATED")

    result = tm.atomic_task_write(
        task,
        [TaskState.HUMAN_MERGED, TaskState.COMPLETE],
        commit=False,
    )

    assert result.state == TaskState.COMPLETE
    assert store.get_task("task-1")["state"] == "COMPLETE"
    # revision advanced exactly once for the whole atomic write
    assert store.get_task("task-1")["revision"] == 1


def test_atomic_task_write_persists_extra_operations(tmp_path):
    store = _store(tmp_path)
    tm = TaskManager(store)
    task = _seed_task(store, state="PR_CREATED")
    review = {"task_id": "task-1", "decision": "APPROVED"}

    tm.atomic_task_write(
        task,
        [TaskState.HUMAN_MERGED, TaskState.COMPLETE],
        extra_operations=[("pr_reviews", [review])],
        commit=False,
    )

    assert store.get_task("task-1")["state"] == "COMPLETE"
    assert store.read("pr_reviews") == [review]


def test_atomic_task_write_rejects_stale_revision(tmp_path):
    store = _store(tmp_path)
    tm = TaskManager(store)
    task = _seed_task(store, state="PR_CREATED", revision=0)
    # Bump the stored revision so the caller's snapshot is stale.
    store.write("tasks", [{"task_id": "task-1", "state": "PR_CREATED", "revision": 5}], commit=False)

    from sentinel.orchestrator.state_store import TaskConcurrencyConflictError
    with pytest.raises(TaskConcurrencyConflictError):
        tm.atomic_task_write(task, [TaskState.HUMAN_MERGED], commit=False)


def test_atomic_task_write_rejects_invalid_transition(tmp_path):
    store = _store(tmp_path)
    tm = TaskManager(store)
    task = _seed_task(store, state="PR_CREATED")

    # PR_CREATED cannot go directly to COMPLETE.
    with pytest.raises(Exception):
        tm.atomic_task_write(task, [TaskState.COMPLETE], commit=False)
    # No partial mutation — still PR_CREATED at revision 0.
    assert store.get_task("task-1")["state"] == "PR_CREATED"
    assert store.get_task("task-1")["revision"] == 0


# ---------------------------------------------------------------------------
# Site-level atomicity — record_pr_approved (site 4)
# ---------------------------------------------------------------------------

def test_record_pr_approved_leaves_no_partial_state_on_crash(tmp_path, monkeypatch):
    from sentinel.config import Config
    from sentinel.orchestrator.main import Orchestrator

    repo = tmp_path / "repo"
    repo.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tasks.json").write_text(
        json.dumps([{
            "task_id": "task-1", "plan_id": "P-1", "state": "PR_CREATED",
            "pr_number": 7, "revision": 0,
        }]), encoding="utf-8",
    )
    cfg = Config(
        env={
            "SENTINEL_REPO_PATH": str(repo),
            "SENTINEL_LOCAL_ONLY_MODE": "true",
            "SENTINEL_STATE_DIR": str(state_dir),
        },
        load_dotenv_file=False,
    )
    orch = Orchestrator(cfg)

    # Force the single atomic persist to fail — nothing must land.
    def _boom(*a, **k):
        raise RuntimeError("simulated crash mid-transaction")

    monkeypatch.setattr(orch._store, "persist_task_with_audit", _boom)

    with orch:
        with pytest.raises(RuntimeError):
            orch.record_pr_approved("task-1")

    # Task unchanged and no orphan PR review row survived.
    assert orch._store.get_task("task-1")["state"] == "PR_CREATED"
    assert orch._store.read("pr_reviews") == []


# ---------------------------------------------------------------------------
# In-memory restore on persist failure (F2-5bd375 / F2-56ba8 / F2-1fdfb6)
# ---------------------------------------------------------------------------

def test_atomic_task_write_restores_in_memory_task_on_persist_failure(tmp_path, monkeypatch):
    """If persist_task_with_audit throws after in-memory transitions are applied,
    the caller's task object must be restored to its pre-transition state so it
    does not hold a dirty state/revision that diverges from the on-disk record."""
    store = _store(tmp_path)
    tm = TaskManager(store)
    task = _seed_task(store, state="PR_CREATED", revision=3)

    original_state = task.state
    original_revision = task.revision
    original_updated_at = task.updated_at

    monkeypatch.setattr(store, "persist_task_with_audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        tm.atomic_task_write(task, [TaskState.HUMAN_MERGED, TaskState.COMPLETE], commit=False)

    # In-memory task must be fully restored — no dirty fields.
    assert task.state == original_state
    assert task.revision == original_revision
    assert task.updated_at == original_updated_at
    # Store is also unchanged (persist never ran).
    assert store.get_task("task-1")["state"] == "PR_CREATED"
    assert store.get_task("task-1")["revision"] == 3


def test_non_atomic_helper_restores_in_memory_task_on_persist_failure(tmp_path, monkeypatch):
    """complete_task (and other non-atomic helpers) must restore task.state and
    task.revision if _persist_with_audit raises, so the caller is not left with
    a task object whose state diverges from the on-disk record."""
    store = _store(tmp_path)
    tm = TaskManager(store)
    task = _seed_task(store, state="HUMAN_MERGED", revision=2)

    original_state = task.state
    original_revision = task.revision

    monkeypatch.setattr(store, "persist_task_with_audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        tm.complete_task(task)

    assert task.state == original_state
    assert task.revision == original_revision
    # Store unchanged.
    assert store.get_task("task-1")["state"] == "HUMAN_MERGED"


def test_record_review_verdict_restores_on_persist_failure(tmp_path, monkeypatch):
    """record_review_verdict must restore task.state and task.review_feedback
    if _persist_with_audit raises (FIX-CRITICAL run-020 / F2-41563e18a482)."""
    store = _store(tmp_path)
    tm = TaskManager(store)
    task = _seed_task(store, state="SUBMITTED", revision=1)
    task.state = TaskState.REVIEW_FAIL  # already in a reviewable state
    store.write("tasks", [task.to_dict()], commit=False)
    # Re-read so revision matches store
    task = TaskManager(store).load_task("task-1")

    original_state = task.state
    original_feedback = task.review_feedback

    monkeypatch.setattr(store, "persist_task_with_audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        tm.record_review_verdict(task, "APPROVE", feedback="looks good")

    assert task.state == original_state
    assert task.review_feedback == original_feedback


# ---------------------------------------------------------------------------
# Callable extra_operations — read inside lock (F2-0a2795810618)
# ---------------------------------------------------------------------------

def test_callable_extra_operations_applied_inside_lock(tmp_path):
    """A callable passed as an extra_operations value must be invoked with the
    CURRENT store value inside the lock — so concurrent appends cannot lose
    updates (FIX-CRITICAL run-020 / F2-0a2795810618)."""
    store = _store(tmp_path)
    store.write("pr_reviews", [{"id": "existing"}], commit=False)
    store.write("tasks", [{"task_id": "task-1", "state": "PR_CREATED", "revision": 0}], commit=False)

    new_review = {"id": "new_review"}
    store.persist_task_with_audit(
        {"task_id": "task-1", "state": "COMPLETE", "revision": 1},
        [],
        extra_operations=[
            ("pr_reviews", lambda existing: (existing if isinstance(existing, list) else []) + [new_review]),
        ],
    )

    reviews = store.read("pr_reviews")
    assert len(reviews) == 2
    assert reviews[0]["id"] == "existing"
    assert reviews[1]["id"] == "new_review"

