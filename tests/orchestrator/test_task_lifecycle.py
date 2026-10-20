"""Verdict validation for the review/QA gates (P1 error_signal_loss).

``record_review_verdict`` and ``record_qa_verdict`` previously mapped ANY
non-"APPROVE"/non-"PASS" string (typos, empty string, unexpected casing of an
unknown word) silently to the FAIL state. A typo therefore looked like a
legitimate rejection. These tests pin the new fail-loud behavior: an
unrecognized verdict raises ``ValueError`` and never mutates task state.
"""

from __future__ import annotations

import pytest

from sentinel.orchestrator.state_machine import TaskState
from sentinel.orchestrator.state_store import StateStore
from sentinel.orchestrator.task_manager import Task, TaskManager


@pytest.fixture
def manager(tmp_path):
    return TaskManager(StateStore(state_dir=tmp_path))


def _seed(store_manager: TaskManager, state: TaskState) -> Task:
    task = Task(
        task_id="T-verdict",
        plan_id="P1",
        state=state,
        description="d",
        acceptance_criteria=["c"],
        scope_boundary="",
    )
    store_manager._store.upsert_task(task.to_dict())
    return task


# ── record_review_verdict ─────────────────────────────────────────────


def test_review_verdict_approve_passes(manager):
    task = _seed(manager, TaskState.SUBMITTED)
    result = manager.record_review_verdict(task, "APPROVE")
    assert result.state == TaskState.REVIEW_PASS


def test_review_verdict_reject_fails(manager):
    task = _seed(manager, TaskState.SUBMITTED)
    result = manager.record_review_verdict(task, "REJECT")
    assert result.state == TaskState.REVIEW_FAIL


def test_review_verdict_is_case_insensitive(manager):
    task = _seed(manager, TaskState.SUBMITTED)
    result = manager.record_review_verdict(task, "approve")
    assert result.state == TaskState.REVIEW_PASS


@pytest.mark.parametrize("bad", ["approved", "PASS", "", "reject!", "yes"])
def test_review_verdict_invalid_raises_and_does_not_mutate(manager, bad):
    task = _seed(manager, TaskState.SUBMITTED)
    with pytest.raises(ValueError, match="invalid verdict"):
        manager.record_review_verdict(task, bad)
    # State unchanged and nothing persisted as a spurious REVIEW_FAIL.
    assert task.state == TaskState.SUBMITTED
    assert manager._store.get_task("T-verdict")["state"] == "SUBMITTED"


# ── record_qa_verdict ─────────────────────────────────────────────────


def test_qa_verdict_pass_passes(manager):
    task = _seed(manager, TaskState.REVIEW_PASS)
    result = manager.record_qa_verdict(task, "PASS")
    assert result.state == TaskState.QA_PASS


def test_qa_verdict_fail_fails(manager):
    task = _seed(manager, TaskState.REVIEW_PASS)
    result = manager.record_qa_verdict(task, "FAIL")
    assert result.state == TaskState.QA_FAIL


def test_qa_verdict_is_case_insensitive(manager):
    task = _seed(manager, TaskState.REVIEW_PASS)
    result = manager.record_qa_verdict(task, "pass")
    assert result.state == TaskState.QA_PASS


@pytest.mark.parametrize("bad", ["APPROVE", "passed", "", "failed?", "ok"])
def test_qa_verdict_invalid_raises_and_does_not_mutate(manager, bad):
    task = _seed(manager, TaskState.REVIEW_PASS)
    with pytest.raises(ValueError, match="invalid verdict"):
        manager.record_qa_verdict(task, bad)
    assert task.state == TaskState.REVIEW_PASS
    assert manager._store.get_task("T-verdict")["state"] == "REVIEW_PASS"
