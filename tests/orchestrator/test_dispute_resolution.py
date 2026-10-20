"""FIX 2 — a resolved dispute restarts implementation with the agreed approach.

Before this change, `_handle_dispute` on `resolved=True` did `task.state =
task.state` (a literal no-op) and returned — the agreed approach was dropped and
the task never re-implemented. And the unresolved path called `escalate_dispute`
directly from SUBMITTED (no `mark_disputed` first), which is not a valid
transition and raised InvalidTransitionError.

These tests pin both paths:
- resolved → task reset to IN_PROGRESS, retry_context persisted with
  gate_name="dispute_resolution", and _run_task re-invoked to continue.
- unresolved → still escalates to ESCALATED via a HumanGateRequired.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from sentinel.agents.architect import ArchitectAgent
from sentinel.agents.coder import CoderAgent
from sentinel.orchestrator.main import HumanGateRequired, Orchestrator
from sentinel.orchestrator.state_machine import TaskState

from tests.test_integration_e2e import _build_config, _make_task, _setup_repo

_TRIGGER = {"verdict": "DISPUTE", "findings": [], "feedback": "architect disagrees"}


def _submitted_task(orch, task_id):
    task = _make_task(task_id, is_phase3=False)
    task.state = TaskState.SUBMITTED
    task.files_changed = list(task.affected_files)
    orch._store.upsert_task(task.to_dict())
    return task


def test_resolved_dispute_resets_to_in_progress(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    orch = Orchestrator(cfg)
    task = _submitted_task(orch, "task-disp-resolved")

    with patch.object(
        ArchitectAgent, "build_position_statement",
        side_effect=[{"issue": "x"}, {"resolved": True, "agreed_approach": "use a guard clause"}],
    ), patch.object(
        CoderAgent, "build_position_statement", return_value={"issue": "y"},
    ), patch.object(Orchestrator, "_run_task") as run_task_mock:
        orch._handle_dispute(task, _TRIGGER)

    # Implementation was restarted (not returned immediately).
    run_task_mock.assert_called_once()

    final = orch._task_manager.load_task("task-disp-resolved")
    assert final.state == TaskState.IN_PROGRESS


def test_resolved_dispute_persists_retry_context(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    orch = Orchestrator(cfg)
    task = _submitted_task(orch, "task-disp-retry")

    with patch.object(
        ArchitectAgent, "build_position_statement",
        side_effect=[{"issue": "x"}, {"resolved": True, "agreed_approach": "use a guard clause"}],
    ), patch.object(
        CoderAgent, "build_position_statement", return_value={"issue": "y"},
    ), patch.object(Orchestrator, "_run_task"):
        orch._handle_dispute(task, _TRIGGER)

    final = orch._task_manager.load_task("task-disp-retry")
    assert "dispute_resolution" in (final.retry_context or "")
    # The agreed approach is carried into the retry packet.
    assert "guard clause" in (final.retry_context or "")


def test_unresolved_dispute_still_escalates(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    orch = Orchestrator(cfg)
    task = _submitted_task(orch, "task-disp-unresolved")

    with patch.object(
        ArchitectAgent, "build_position_statement",
        side_effect=[{"issue": "x"}, {"resolved": False, "counter": "still broken"}],
    ), patch.object(
        CoderAgent, "build_position_statement", return_value={"issue": "y"},
    ):
        with pytest.raises(HumanGateRequired) as exc:
            orch._handle_dispute(task, _TRIGGER)
        assert exc.value.gate_type == "dispute_escalation"

    final = orch._task_manager.load_task("task-disp-unresolved")
    assert final.state == TaskState.ESCALATED
