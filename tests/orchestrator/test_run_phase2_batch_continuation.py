"""run_phase2 batch continuation.

A per-task escalation must not abort the whole ``--limit N`` batch: a
RuntimeError (e.g. "no file changes" / old_text mismatch) or an expected
HumanGateRequired is recorded and the loop moves on to the next task. A
genuinely unexpected error (e.g. KeyError) still propagates and aborts, so
unknown failures are never silently swallowed.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from sentinel.orchestrator.main import HumanGateRequired, Orchestrator
from tests.test_integration_e2e import _build_config, _make_task, _setup_repo


def _orch_with_three_items(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    orch = Orchestrator(cfg)
    orch._store.write("plan", {"human_approved": True})
    items = [{"id": "P1"}, {"id": "P2"}, {"id": "P3"}]
    tasks = {pid: _make_task(f"task-{pid}", is_phase3=False) for pid in ("P1", "P2", "P3")}
    return orch, items, tasks


def _drive(orch, items, tasks, side_effect, expect_raises=None):
    ran: list[str] = []

    def _fake_run_task(task):
        ran.append(task.task_id)
        side_effect(task)

    ctx = (
        patch.object(orch, "_extract_plan_items", return_value=items),
        patch.object(orch, "_enforce_phase2_dependency_scope_gate"),
        patch.object(orch._task_manager, "load_task_by_plan_id",
                     side_effect=lambda pid: tasks[pid]),
        patch.object(orch, "_record_batch_progress"),
        patch.object(orch, "_run_task", side_effect=_fake_run_task),
    )
    from contextlib import ExitStack

    with ExitStack() as stack:
        for cm in ctx:
            stack.enter_context(cm)
        if expect_raises is not None:
            with pytest.raises(expect_raises):
                orch.run_phase2()
        else:
            orch.run_phase2()
    return ran


def test_runtime_error_on_first_task_does_not_abort_batch(tmp_path):
    orch, items, tasks = _orch_with_three_items(tmp_path)

    def _se(task):
        if task.task_id == "task-P1":
            raise RuntimeError("submission contains no file changes")

    ran = _drive(orch, items, tasks, _se)
    assert ran == ["task-P1", "task-P2", "task-P3"]


def test_human_gate_on_middle_task_does_not_abort_batch(tmp_path):
    orch, items, tasks = _orch_with_three_items(tmp_path)

    def _se(task):
        if task.task_id == "task-P2":
            raise HumanGateRequired(
                gate_type="local_review", payload={"task_id": task.task_id}
            )

    ran = _drive(orch, items, tasks, _se)
    assert ran == ["task-P1", "task-P2", "task-P3"]


def test_unexpected_exception_propagates_and_aborts_batch(tmp_path):
    orch, items, tasks = _orch_with_three_items(tmp_path)

    def _se(task):
        if task.task_id == "task-P2":
            raise KeyError("unexpected")

    ran = _drive(orch, items, tasks, _se, expect_raises=KeyError)
    # Aborted at P2 — P3 never ran; unknown errors are not swallowed.
    assert ran == ["task-P1", "task-P2"]
