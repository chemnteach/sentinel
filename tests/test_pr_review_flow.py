from __future__ import annotations

import threading
from pathlib import Path

from sentinel.orchestrator.state_machine import TaskState
from sentinel.orchestrator.state_store import StateStore
from sentinel.orchestrator.task_manager import Task, TaskManager


def _store(tmp_path: Path) -> StateStore:
    state_dir = tmp_path / "sentinel" / "state"
    repo_dir = tmp_path
    return StateStore(state_dir=state_dir, repo_path=repo_dir)


def test_record_pr_review_persists_comment(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manager = TaskManager(store)

    task = Task(task_id="task-1", plan_id="plan-1", state=TaskState.PR_CREATED)
    store.upsert_task(task.to_dict())

    manager.record_pr_review(task, decision="REJECTED", comment="Missing edge-case tests")

    reviews = store.read("pr_reviews")
    assert len(reviews) == 1
    assert reviews[0]["task_id"] == "task-1"
    assert reviews[0]["decision"] == "REJECTED"
    assert "edge-case" in reviews[0]["comment"]


def test_reopen_after_pr_rejection_moves_to_in_progress(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manager = TaskManager(store)

    task = Task(task_id="task-2", plan_id="plan-2", state=TaskState.PR_CREATED)
    store.upsert_task(task.to_dict())

    updated = manager.reopen_after_pr_rejection(task, reason="Please split function and add tests")

    assert updated.state == TaskState.IN_PROGRESS
    assert "Please split" in updated.last_human_review_comment

    persisted = manager.load_task("task-2")
    assert persisted.state == TaskState.IN_PROGRESS
    assert "Please split" in persisted.last_human_review_comment


def test_load_task_by_plan_id_returns_existing_task(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manager = TaskManager(store)

    created = manager.create_task(
        plan_id="plan-lookup",
        description="test",
        acceptance_criteria=[],
        affected_files=[],
        scope_boundary="",
    )

    loaded = manager.load_task_by_plan_id("plan-lookup")
    assert loaded is not None
    assert loaded.task_id == created.task_id


def test_record_pr_review_concurrent_no_lost_update(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manager = TaskManager(store)

    task = Task(task_id="task-cc", plan_id="plan-cc", state=TaskState.PR_CREATED)
    store.upsert_task(task.to_dict())

    N = 16
    barrier = threading.Barrier(N)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            snapshot = manager.load_task("task-cc")
            barrier.wait()  # everyone reads pr_reviews at the same time
            manager.record_pr_review(snapshot, decision="APPROVED", comment=f"c{i}")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    reviews = store.read("pr_reviews")
    assert len(reviews) == N  # every review survives — no lost update
    assert sorted(r["comment"] for r in reviews) == sorted(f"c{i}" for i in range(N))

