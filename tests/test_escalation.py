"""Tests for the Phase-4 task escalation ladder (with prior-attempt context)."""
from __future__ import annotations

import pytest

from sentinel.orchestrator.state_machine import TaskState
from sentinel.orchestrator.state_store import StateStore
from sentinel.orchestrator.task_manager import Task, escalate_task


@pytest.fixture
def store(tmp_path):
    return StateStore(state_dir=tmp_path)


def _task(**kw):
    base = dict(
        task_id="t1",
        plan_id="FIX-BLOCKING-001",
        description="A hardcoded credential default in config.py.",
        affected_files=["sentinel/config.py"],
        acceptance_criteria=["No hardcoded default"],
        scope_boundary="",
    )
    base.update(kw)
    return Task(**base)


def test_escalate_to_codex_sets_state(store):
    t = _task()
    escalate_task(t, "codex", "first executor", ".", store)
    assert t.state == TaskState.ESCALATED_TO_CODEX


def test_escalate_to_opus_sets_state(store):
    t = _task()
    escalate_task(t, "opus", "codex failed", ".", store)
    assert t.state == TaskState.ESCALATED_TO_OPUS


def test_escalate_to_human_sets_state(store):
    t = _task()
    escalate_task(t, "human", "both executors failed", ".", store)
    assert t.state == TaskState.ESCALATED_TO_HUMAN


def test_escalation_packet_contains_reason(store):
    t = _task()
    escalate_task(t, "opus", "old_text mismatch after 3 retries", ".", store)
    assert t.escalation_packet is not None
    assert "old_text mismatch after 3 retries" in t.escalation_packet
    assert "## Prior executor context" in t.escalation_packet
    assert "## Task: FIX-BLOCKING-001" in t.escalation_packet  # base packet embedded


def test_packet_contains_prior_files_attempted(store):
    t = _task()
    escalate_task(
        t, "opus", "r", ".", store,
        prior_files_attempted=["sentinel/config.py", "sentinel/other.py"],
    )
    assert "Files the prior executor attempted to edit" in t.escalation_packet
    assert "- sentinel/config.py" in t.escalation_packet
    assert "- sentinel/other.py" in t.escalation_packet


def test_packet_contains_failing_tests_truncated_to_3000(store):
    t = _task()
    big = "X" * 5000
    escalate_task(t, "opus", "r", ".", store, prior_failing_tests=big)
    assert "Failing tests from last attempt" in t.escalation_packet
    assert ("X" * 3000) in t.escalation_packet        # truncated payload present
    assert ("X" * 3001) not in t.escalation_packet    # but no more than 3000


def test_packet_contains_do_not_repeat_when_failing_tests_present(store):
    t = _task()
    escalate_task(t, "opus", "r", ".", store, prior_failing_tests="E   assert 1 == 2")
    assert "Do not repeat the same approach" in t.escalation_packet


def test_escalation_persists_and_survives_reload(store):
    t = _task()
    escalate_task(t, "codex", "escalation-reason-marker", ".", store)
    reloaded = Task.from_dict(store.get_task("t1"))
    assert reloaded.state == TaskState.ESCALATED_TO_CODEX
    assert "escalation-reason-marker" in (reloaded.escalation_packet or "")


# ── Issue 4: optimistic-concurrency guard ────────────────────────────
def test_escalate_task_rejects_stale_snapshot(store):
    """A concurrent modification (stored revision advanced past the caller's
    snapshot) must make escalation refuse rather than clobber. (Issue 4)"""
    from sentinel.orchestrator.state_store import TaskConcurrencyConflictError

    t = _task()
    store.upsert_task(t.to_dict())
    # Another actor advances the stored task's revision after our snapshot.
    stored = store.get_task("t1")
    stored["revision"] = int(stored.get("revision", 0)) + 1
    store.upsert_task(stored)

    with pytest.raises(TaskConcurrencyConflictError):
        escalate_task(t, "codex", "reason", ".", store)


# ── Issue 5: validate level before mutating the task ─────────────────
def test_escalate_task_invalid_level_leaves_task_unmutated(store):
    """An invalid level must raise ValueError BEFORE any field is mutated, so
    the task is not left with a dirty escalation_packet / half-applied state.
    (Issue 5)"""
    t = _task()
    initial_packet = t.escalation_packet
    initial_state = t.state

    with pytest.raises(ValueError):
        escalate_task(t, "bogus-level", "reason", ".", store)

    assert t.escalation_packet == initial_packet  # not mutated
    assert t.state == initial_state


def test_escalate_task_none_level_raises_value_error(store):
    """A None level is rejected cleanly (ValueError), not AttributeError."""
    t = _task()
    with pytest.raises(ValueError):
        escalate_task(t, None, "reason", ".", store)
