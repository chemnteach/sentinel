"""Atomic task+audit persistence regression tests (FIX-BLOCKING-001..004,
FIX-DESIRED-001).

These verify that TaskManager transition methods persist the task mutation and
their audit records through the store's single atomic ``persist_task_with_audit``
call, and that an injected write failure leaves neither a partial task-state
change nor an orphan audit entry.
"""

from __future__ import annotations

import pytest

from sentinel.orchestrator.state_machine import TaskState
from sentinel.orchestrator.state_store import StateStore, TaskConcurrencyConflictError
from sentinel.orchestrator.task_manager import Task, TaskManager


@pytest.fixture
def store(tmp_path):
    return StateStore(state_dir=tmp_path)


def _seed_task(store: StateStore, **kw) -> Task:
    base = dict(
        task_id="T-atomic",
        plan_id="P1",
        description="d",
        acceptance_criteria=["c"],
        scope_boundary="",
    )
    base.update(kw)
    task = Task(**base)
    store.upsert_task(task.to_dict())
    return task


# ── persist_task_with_audit combined-operation semantics ──────────────


def test_persist_task_with_audit_success_records_both(store):
    task = _seed_task(store, state=TaskState.SUBMITTED)
    task.state = TaskState.REVIEW_PASS
    store.persist_task_with_audit(
        task.to_dict(),
        [{"agent": "architect", "action": "review_verdict", "task_id": task.task_id}],
    )

    reloaded = store.get_task("T-atomic")
    assert reloaded["state"] == "REVIEW_PASS"
    audit = store.read("audit_log")
    assert any(e.get("action") == "review_verdict" for e in audit)


def test_persist_task_with_audit_rolls_back_on_audit_write_failure(store):
    _seed_task(store, state=TaskState.SUBMITTED)
    audit_before = list(store.read("audit_log"))

    original_write = store.write

    def _failing_write(store_name, data, **kw):
        if store_name == "audit_log":
            raise RuntimeError("injected audit write failure")
        return original_write(store_name, data, **kw)

    store.write = _failing_write  # type: ignore[assignment]

    updated = Task(
        task_id="T-atomic", plan_id="P1", state=TaskState.REVIEW_PASS,
        description="d", acceptance_criteria=["c"], scope_boundary="",
    )
    with pytest.raises(RuntimeError, match="injected audit write failure"):
        store.persist_task_with_audit(
            updated.to_dict(),
            [{"agent": "architect", "action": "review_verdict", "task_id": "T-atomic"}],
        )

    store.write = original_write  # type: ignore[assignment]
    # Task reloads in the pre-operation state; no orphan audit entry remains.
    assert store.get_task("T-atomic")["state"] == "SUBMITTED"
    assert store.read("audit_log") == audit_before


def test_persist_task_with_audit_rolls_back_on_task_write_failure(store):
    _seed_task(store, state=TaskState.SUBMITTED)
    audit_before = list(store.read("audit_log"))

    original_write = store.write

    def _failing_write(store_name, data, **kw):
        if store_name == "tasks":
            raise RuntimeError("injected task write failure")
        return original_write(store_name, data, **kw)

    store.write = _failing_write  # type: ignore[assignment]

    updated = Task(
        task_id="T-atomic", plan_id="P1", state=TaskState.REVIEW_PASS,
        description="d", acceptance_criteria=["c"], scope_boundary="",
    )
    with pytest.raises(RuntimeError, match="injected task write failure"):
        store.persist_task_with_audit(
            updated.to_dict(),
            [{"agent": "architect", "action": "review_verdict", "task_id": "T-atomic"}],
        )

    store.write = original_write  # type: ignore[assignment]
    assert store.get_task("T-atomic")["state"] == "SUBMITTED"
    assert store.read("audit_log") == audit_before


# ── TaskManager transition methods route through the atomic call ──────


def test_record_review_verdict_uses_atomic_persist_and_saves_feedback(store):
    tm = TaskManager(store)
    task = _seed_task(store, state=TaskState.SUBMITTED)

    tm.record_review_verdict(task, verdict="REJECT", feedback="missing null check")

    reloaded = store.get_task("T-atomic")
    assert reloaded["state"] == "REVIEW_FAIL"
    # BLOCKING-004: feedback persisted on a stable task field, readable on reload.
    assert reloaded["review_feedback"] == "missing null check"


def test_record_qa_verdict_persists_feedback_on_reload(store):
    tm = TaskManager(store)
    task = _seed_task(store, state=TaskState.REVIEW_PASS)

    tm.record_qa_verdict(task, verdict="FAIL", feedback="assertion still red")

    reloaded = store.get_task("T-atomic")
    assert reloaded["state"] == "QA_FAIL"
    assert reloaded["qa_feedback"] == "assertion still red"


def test_review_verdict_failure_leaves_no_partial_state(store):
    """Injected audit failure during a real transition leaves pre-op state."""
    tm = TaskManager(store)
    _seed_task(store, state=TaskState.SUBMITTED)
    task = Task.from_dict(store.get_task("T-atomic"))
    audit_before = list(store.read("audit_log"))

    original_write = store.write

    def _failing_write(store_name, data, **kw):
        if store_name == "audit_log":
            raise RuntimeError("boom")
        return original_write(store_name, data, **kw)

    store.write = _failing_write  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="boom"):
        tm.record_review_verdict(task, verdict="APPROVE", feedback="ok")

    store.write = original_write  # type: ignore[assignment]
    assert store.get_task("T-atomic")["state"] == "SUBMITTED"
    assert store.read("audit_log") == audit_before


# ── FIX-DESIRED-001: guardrail that transitions use the atomic path ───


def test_transition_methods_do_not_sequence_persist_then_audit(store, monkeypatch):
    """Transition methods must call persist_task_with_audit, not a separate
    upsert_task followed by append_audit."""
    tm = TaskManager(store)
    task = _seed_task(store, state=TaskState.SUBMITTED)

    calls: list[str] = []
    monkeypatch.setattr(
        store, "persist_task_with_audit",
        lambda *a, **k: calls.append("atomic"),
    )
    monkeypatch.setattr(store, "upsert_task", lambda *a, **k: calls.append("upsert"))
    monkeypatch.setattr(store, "append_audit", lambda *a, **k: calls.append("append"))

    tm.record_review_verdict(task, verdict="APPROVE", feedback="ok")

    assert calls == ["atomic"]
    assert "upsert" not in calls and "append" not in calls


# ── FIX-BLOCKING-008: optimistic concurrency on transitions ──────────


def test_stale_transition_raises_concurrency_conflict(store):
    tm = TaskManager(store)
    _seed_task(store, state=TaskState.SUBMITTED)

    # Two callers read the same version of the task.
    caller_a = Task.from_dict(store.get_task("T-atomic"))
    caller_b = Task.from_dict(store.get_task("T-atomic"))

    # Caller A wins.
    tm.record_review_verdict(caller_a, verdict="APPROVE", feedback="a")
    assert store.get_task("T-atomic")["state"] == "REVIEW_PASS"

    # Caller B started from the same stale version → must not overwrite.
    with pytest.raises(TaskConcurrencyConflictError):
        tm.record_review_verdict(caller_b, verdict="REJECT", feedback="b")
    # Winner's state is preserved.
    assert store.get_task("T-atomic")["state"] == "REVIEW_PASS"


def test_current_snapshot_transition_still_succeeds(store):
    tm = TaskManager(store)
    task = _seed_task(store, state=TaskState.SUBMITTED)
    # A caller operating on a current snapshot is unaffected by the guard.
    task = tm.record_review_verdict(task, verdict="APPROVE", feedback="ok")
    assert store.get_task("T-atomic")["state"] == "REVIEW_PASS"


# ── FIX-REQUIRED-001 / FIX-DESIRED-002: record_pr_review freshness ───


def test_record_pr_review_uses_stored_state_and_rejects_stale(store):
    tm = TaskManager(store)
    _seed_task(store, state=TaskState.PR_CREATED)
    snapshot = Task.from_dict(store.get_task("T-atomic"))

    # Another actor transitions the task after the snapshot was taken.
    fresh = Task.from_dict(store.get_task("T-atomic"))
    tm.reopen_after_pr_rejection(fresh, reason="changes")

    # Recording a review from the stale snapshot must be rejected.
    with pytest.raises(TaskConcurrencyConflictError):
        tm.record_pr_review(snapshot, decision="APPROVED", comment="lgtm")

    # A review from the current task records state_at_review from stored state.
    current = tm.load_task("T-atomic")
    tm.record_pr_review(current, decision="REJECTED", comment="needs work")
    reviews = store.read("pr_reviews")
    assert reviews[-1]["state_at_review"] == "IN_PROGRESS"

