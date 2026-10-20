"""Step 4 — retry exhaustion escalates (ESCALATED) instead of crashing.

Before this change the Architect REVIEW_FAIL and Phase-3 compat FAIL paths did
`raise RuntimeError` once review_attempts exceeded 3 — an uncaught crash that
halts the batch. The QA path already escalated correctly (mark_disputed →
escalate_dispute → HumanGateRequired). These tests pin the three exhaustion
paths to the same non-crashing behaviour: a controlled HumanGateRequired and an
ESCALATED task state.

Reuses the real _run_task harness from tests/test_integration_e2e (temp git
repo, real state machine, mocked LLM agents).
"""

from __future__ import annotations

import contextlib
import json

import pytest
from unittest.mock import MagicMock, patch

from sentinel.agents.architect import ArchitectAgent
from sentinel.agents.coder import CoderAgent
from sentinel.agents.docs import DocsAgent
from sentinel.agents.qa import QAAgent
from sentinel.orchestrator.main import HumanGateRequired, Orchestrator
from sentinel.orchestrator.state_machine import TaskState
from sentinel.phase3.compat_checker import CompatChecker

from tests.test_integration_e2e import (
    _EDIT,
    _IMPL_RESULT,
    _TODO,
    _build_config,
    _make_task,
    _setup_repo,
)


@contextlib.contextmanager
def _agents(repo_root, *, review_diff_return, compat_check):
    """Mocked-agent stack with a configurable review_diff verdict."""
    from sentinel.workflow.remediation.apply import apply_edits

    def _fake_implement(self, _context):
        apply_edits([_EDIT], str(repo_root))
        return dict(_IMPL_RESULT)

    with contextlib.ExitStack() as stack:
        e = stack.enter_context
        e(patch.object(CoderAgent, "submit_todo", return_value=dict(_TODO)))
        e(patch.object(CoderAgent, "implement", autospec=True, side_effect=_fake_implement))
        e(patch.object(CoderAgent, "self_review", return_value={"findings": []}))
        e(patch.object(
            ArchitectAgent, "review_todo",
            return_value={
                "implementation_plan_verdict": "APPROVE",
                "test_stubs_verdict": "APPROVE",
            },
        ))
        e(patch.object(ArchitectAgent, "review_diff", return_value=review_diff_return))
        e(patch.object(QAAgent, "verify_fix", return_value={"verdict": "PASS", "tdd_assessment": {}}))
        e(patch.object(
            DocsAgent, "generate_transition_manifest",
            return_value={"docs_updated": [], "doc_updated": True},
        ))
        e(patch.object(CompatChecker, "check", compat_check))
        yield


def test_architect_review_exhaustion_escalates_not_crash(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    compat_spy = MagicMock(return_value={"verdict": "PASS", "violations": []})

    with _agents(
        repo_root,
        review_diff_return={"verdict": "REJECT", "findings": [], "feedback": "still broken"},
        compat_check=compat_spy,
    ):
        orch = Orchestrator(cfg)
        # Pre-load the counter so the first REVIEW_FAIL crosses the >3 cap.
        task = _make_task("task-exh-arch", is_phase3=False)
        task.review_attempts = 3
        orch._store.upsert_task(task.to_dict())

        # Must be a controlled human gate, NOT a RuntimeError crash.
        with pytest.raises(HumanGateRequired) as exc:
            orch._run_task(task)
        assert exc.value.gate_type == "review_failure"

        final = orch._task_manager.load_task("task-exh-arch")
        assert final.state == TaskState.ESCALATED


def test_compat_exhaustion_escalates_not_crash(tmp_path):
    from sentinel.phase3.compat_checker import CompatibilityViolation

    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")

    def _compat_fail(self, task, diff):
        raise CompatibilityViolation(
            task.task_id,
            [{"severity": "BREAKING", "location": "api.py:1", "description": "removed param"}],
        )

    with _agents(
        repo_root,
        review_diff_return={"verdict": "APPROVE", "findings": [], "feedback": ""},
        compat_check=_compat_fail,
    ):
        orch = Orchestrator(cfg)
        task = _make_task("task-exh-compat", is_phase3=True)
        task.review_attempts = 3
        orch._store.upsert_task(task.to_dict())

        with pytest.raises(HumanGateRequired) as exc:
            orch._run_task(task)
        assert exc.value.gate_type == "compat_failure"

        final = orch._task_manager.load_task("task-exh-compat")
        assert final.state == TaskState.ESCALATED


def _submitted_task_no_files(orch, task_id: str, *, review_attempts: int = 2):
    """A task parked in SUBMITTED with no file changes — the exact state that
    trips the ``no file changes`` RuntimeError at the top of the SUBMITTED
    branch in _run_task (implementation block is skipped for SUBMITTED)."""
    task = _make_task(task_id, is_phase3=False)
    task.state = TaskState.SUBMITTED
    task.files_changed = []
    task.review_attempts = review_attempts
    orch._store.upsert_task(task.to_dict())
    return task


def test_no_file_changes_writes_batch_summary_escalated(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    cfg.local_only_mode = False  # ensure the no-file-changes branch is reachable

    orch = Orchestrator(cfg)
    task = _submitted_task_no_files(orch, "task-nofiles-bs", review_attempts=2)

    with pytest.raises(RuntimeError, match="no file changes"):
        orch._run_task(task)

    summary = json.loads((cfg.state_dir / "batch_summary.json").read_text(encoding="utf-8"))
    assert task.task_id in summary["tasks"]
    assert summary["tasks"][task.task_id]["status"] == "ESCALATED"


def test_no_file_changes_escalates_not_left_submitted(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    cfg.local_only_mode = False

    orch = Orchestrator(cfg)
    _submitted_task_no_files(orch, "task-nofiles-esc")

    with pytest.raises(RuntimeError, match="no file changes"):
        orch._run_task(orch._task_manager.load_task("task-nofiles-esc"))

    final = orch._task_manager.load_task("task-nofiles-esc")
    assert final.state == TaskState.ESCALATED
    assert final.state != TaskState.SUBMITTED
