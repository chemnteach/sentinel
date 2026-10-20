from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sentinel.config import Config
from sentinel.orchestrator.main import Orchestrator
from sentinel.orchestrator.state_machine import TaskState
from sentinel.orchestrator.task_manager import Task


def _build_config(tmp_path: Path) -> Config:
    cfg = Config(
        env={
            "SENTINEL_REPO_PATH": str(tmp_path),
            "SENTINEL_STATE_DIR": "sentinel/state",
        },
        load_dotenv_file=False,
    )
    cfg.self_approve_mode = True
    cfg.local_only_mode = True
    return cfg


def _seed_doc_updated_task(orch: Orchestrator, task_id: str) -> None:
    task = Task(
        task_id=task_id,
        plan_id="FIX-BLOCKING-004",
        state=TaskState.DOC_UPDATED,
        description="Atomicity regression test",
        acceptance_criteria=["Do not persist terminal state before publish success"],
        affected_files=["sentinel/orchestrator/main.py"],
        scope_boundary="main.py only",
        test_stubs_approved=True,
        todo_approved=True,
        doc_updated=True,
        files_changed=["sentinel/orchestrator/main.py"],
    )
    orch._store.upsert_task(task.to_dict())


def test_self_approve_commit_failure_does_not_persist_terminal_state(
    tmp_path: Path,
) -> None:
    cfg = _build_config(tmp_path)
    task_id = "task-commit-failure"

    with Orchestrator(cfg) as orch:
        _seed_doc_updated_task(orch, task_id)

        with (
            patch.object(
                Orchestrator,
                "_git_commit_scoped",
                side_effect=RuntimeError("commit failed"),
            ),
            patch.object(Orchestrator, "_git_push_current_branch") as push_mock,
        ):
            with pytest.raises(RuntimeError, match="commit failed"):
                orch.approve_local_and_push(task_id)
            push_mock.assert_not_called()

        persisted = orch._task_manager.load_task(task_id)
        assert persisted.state == TaskState.DOC_UPDATED
        assert persisted.state not in {TaskState.HUMAN_MERGED, TaskState.COMPLETE}
        assert "publish failed" in persisted.last_human_review_comment.lower()


def test_self_approve_push_failure_does_not_persist_terminal_state(
    tmp_path: Path,
) -> None:
    cfg = _build_config(tmp_path)
    task_id = "task-push-failure"

    with Orchestrator(cfg) as orch:
        _seed_doc_updated_task(orch, task_id)

        with (
            patch.object(Orchestrator, "_git_commit_scoped", return_value=None) as commit_mock,
            patch.object(
                Orchestrator,
                "_git_push_current_branch",
                side_effect=RuntimeError("push failed"),
            ),
        ):
            with pytest.raises(RuntimeError, match="push failed"):
                orch.approve_local_and_push(task_id)
            assert commit_mock.call_count == 1

        persisted = orch._task_manager.load_task(task_id)
        assert persisted.state == TaskState.DOC_UPDATED
        assert persisted.state not in {TaskState.HUMAN_MERGED, TaskState.COMPLETE}
        assert "publish failed" in persisted.last_human_review_comment.lower()
