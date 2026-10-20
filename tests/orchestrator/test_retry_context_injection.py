"""Step 3 — retry_context injection into the Coder's next implementation prompt.

build_implementation_context must surface task.retry_context (the packet built
on the most recent gate rejection) so the Coder's next attempt sees why the
prior patch was rejected. Mirrors the existing compat_feedback injection test.
"""

from __future__ import annotations

from sentinel.orchestrator.task_manager import Task, TaskManager


class _FakeStore:
    def __init__(self):
        self.tasks = {}
        self.audits = []

    def upsert_task(self, d):
        self.tasks[d["task_id"]] = d

    def append_audit(self, d):
        self.audits.append(d)


def _task(**kw):
    base = dict(
        task_id="T1",
        plan_id="P1",
        test_stubs={"test_file": "tests/test_x.py", "stubs": []},
    )
    base.update(kw)
    return Task(**base)


def test_retry_context_injected_into_implementation_context():
    tm = TaskManager(store=_FakeStore())
    task = _task(
        retry_context=(
            "## Retry context — Attempt 2\n\n"
            "### Why it was rejected\nMissing null check.\n\n"
            "Do NOT repeat the same approach as the previous attempt."
        )
    )
    context = tm.build_implementation_context(task, repo_root="")
    assert "## Retry context — Attempt 2" in context
    assert "Missing null check." in context
    assert "not repeat" in context.lower()


def test_no_retry_context_when_absent():
    tm = TaskManager(store=_FakeStore())
    task = _task()  # retry_context defaults to ""
    context = tm.build_implementation_context(task, repo_root="")
    assert "Retry context" not in context


def test_retry_context_sits_above_compat_feedback():
    tm = TaskManager(store=_FakeStore())
    task = _task(
        compat_feedback="BREAKING: removed public method foo()",
        retry_context="## Retry context — Attempt 3\nprior QA failure",
    )
    context = tm.build_implementation_context(task, repo_root="")
    # Both present, and the fresher retry packet is prepended last → on top.
    assert "## Retry context — Attempt 3" in context
    assert "BACKWARDS COMPATIBILITY FEEDBACK FROM PREVIOUS ATTEMPT" in context
    assert context.index("## Retry context") < context.index(
        "BACKWARDS COMPATIBILITY FEEDBACK"
    )
