"""Tests for the layer5b acceptance-criteria completeness gate (SHADOW MODE)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sentinel.config import Config
from sentinel.orchestrator.main import Orchestrator
from sentinel.orchestrator.state_machine import TaskState
from sentinel.orchestrator.task_manager import Task
from sentinel.orchestrator.task_packet import generate_acceptance_check_context


# --- 1. context assembler (pure unit) ---

def test_generate_acceptance_check_context_formats_multiple_criteria():
    task = Task(
        task_id="t1",
        plan_id="FIX-X",
        acceptance_criteria=["First criterion", "Second criterion", "Third criterion"],
    )
    ctx = generate_acceptance_check_context(
        task, diff_summary="DIFF-HERE", executor_self_report="REPORT-HERE"
    )
    assert "## Original Acceptance Criteria" in ctx
    assert "1. First criterion" in ctx
    assert "2. Second criterion" in ctx
    assert "3. Third criterion" in ctx
    assert "## Diff Summary\nDIFF-HERE" in ctx
    assert "## Executor Self-Report\nREPORT-HERE" in ctx


# --- shadow-mode wiring harness (mirrors test_approve_local_self_approve_atomicity) ---

def _build_config(tmp_path: Path) -> Config:
    cfg = Config(
        env={"SENTINEL_REPO_PATH": str(tmp_path), "SENTINEL_STATE_DIR": "sentinel/state"},
        load_dotenv_file=False,
    )
    cfg.self_approve_mode = True
    cfg.local_only_mode = True
    return cfg


def _seed_doc_updated_task(orch: Orchestrator, task_id: str) -> None:
    task = Task(
        task_id=task_id,
        plan_id="FIX-BLOCKING-002",
        state=TaskState.DOC_UPDATED,
        description="Gate wiring test",
        acceptance_criteria=["Some criterion"],
        affected_files=["sentinel/config.py"],
        scope_boundary="config.py only",
        test_stubs_approved=True,
        todo_approved=True,
        doc_updated=True,
        files_changed=["sentinel/config.py"],
        latest_diff_summary="--- diff ---",
        test_summary="all green",
    )
    orch._store.upsert_task(task.to_dict())


_FAKE_RESULT = {
    "overall_verdict": "PASS",
    "criteria_results": [{"criterion_number": 1, "verdict": "SATISFIED"}],
    "premise_mismatch_flag": False,
}


def _run_self_approve(orch: Orchestrator, task_id: str) -> None:
    # commit/push are no-ops so the self-approve branch reaches COMPLETE.
    with (
        patch.object(Orchestrator, "_git_commit_scoped"),
        patch.object(Orchestrator, "_git_push_current_branch"),
    ):
        orch.approve_local_and_push(task_id)


def test_shadow_gate_called_after_merge_and_logged(tmp_path: Path) -> None:
    with Orchestrator(_build_config(tmp_path)) as orch:
        _seed_doc_updated_task(orch, "task-gate-1")
        with patch.object(
            orch._architect, "check_acceptance_criteria", return_value=_FAKE_RESULT
        ) as gate:
            _run_self_approve(orch, "task-gate-1")
        gate.assert_called_once()
        assert orch._task_manager.load_task("task-gate-1").state == TaskState.COMPLETE
        log = orch._store.read("acceptance_gate_log")
        assert len(log) == 1
        assert log[0]["plan_id"] == "FIX-BLOCKING-002"
        assert log[0]["verdict"] == "PASS"


def test_shadow_gate_exception_does_not_block_completion(tmp_path: Path) -> None:
    with Orchestrator(_build_config(tmp_path)) as orch:
        _seed_doc_updated_task(orch, "task-gate-err")
        with patch.object(
            orch._architect,
            "check_acceptance_criteria",
            side_effect=RuntimeError("gate boom"),
        ):
            _run_self_approve(orch, "task-gate-err")  # must NOT raise
        # Shadow mode never blocks: the task is still COMPLETE.
        assert orch._task_manager.load_task("task-gate-err").state == TaskState.COMPLETE
        assert len(orch._store.read("acceptance_gate_log") or []) == 0


def test_acceptance_gate_log_accumulates_across_tasks(tmp_path: Path) -> None:
    with Orchestrator(_build_config(tmp_path)) as orch:
        with patch.object(
            orch._architect, "check_acceptance_criteria", return_value=_FAKE_RESULT
        ):
            _seed_doc_updated_task(orch, "task-acc-1")
            _run_self_approve(orch, "task-acc-1")
            _seed_doc_updated_task(orch, "task-acc-2")
            _run_self_approve(orch, "task-acc-2")
        log = orch._store.read("acceptance_gate_log")
        assert len(log) == 2  # accumulated, not overwritten
        assert {e["task_id"] for e in log} == {"task-acc-1", "task-acc-2"}
