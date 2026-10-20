"""P1 wave-3 tests for sentinel.govern.pr verdict derivation.

Covers:
  - PRManifestBuilder.build derives todo_verdict / review_verdict / qa_verdict
    from real task state instead of hard-coding "APPROVED".
  - PRManifest.format_for_human falls back to "UNKNOWN" (not "APPROVED") when a
    verdict field is absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sentinel.govern.pr import PRManifest, PRManifestBuilder
from sentinel.orchestrator.state_machine import TaskState


@dataclass
class FakeTask:
    task_id: str = "task-verdict01"
    plan_id: str = "plan-001"
    state: TaskState = TaskState.DOC_UPDATED
    description: str = "Fix"
    acceptance_criteria: list[str] = field(default_factory=list)
    affected_files: list[str] = field(default_factory=list)
    scope_boundary: str = ""
    test_stubs_approved: bool = True
    todo_approved: bool = True
    doc_updated: bool = True
    docs_updated: list[str] = field(default_factory=list)
    tests_pass: bool = False
    test_results: str = ""
    test_stubs: dict[str, Any] = field(default_factory=dict)


def _build(task: FakeTask) -> PRManifest:
    return PRManifestBuilder().build(
        task=task,
        plan_item={},
        diff_summary="",
        files_changed=[],
    )


# ---------------------------------------------------------------------------
# todo_verdict
# ---------------------------------------------------------------------------


def test_todo_verdict_approved_when_todo_approved_true():
    manifest = _build(FakeTask(todo_approved=True))
    assert manifest.todo_verdict == "APPROVED"


def test_todo_verdict_pending_when_todo_not_approved():
    manifest = _build(FakeTask(todo_approved=False))
    assert manifest.todo_verdict == "PENDING"


# ---------------------------------------------------------------------------
# review_verdict — derived from state advancing past REVIEW_PASS
# ---------------------------------------------------------------------------


def test_review_verdict_approved_at_review_pass():
    manifest = _build(FakeTask(state=TaskState.REVIEW_PASS))
    assert manifest.review_verdict == "APPROVED"


def test_review_verdict_approved_when_advanced_past_review():
    manifest = _build(FakeTask(state=TaskState.PR_CREATED))
    assert manifest.review_verdict == "APPROVED"


def test_review_verdict_pending_when_before_review_gate():
    manifest = _build(FakeTask(state=TaskState.SUBMITTED))
    assert manifest.review_verdict == "PENDING"


def test_review_verdict_pending_on_review_fail():
    manifest = _build(FakeTask(state=TaskState.REVIEW_FAIL))
    assert manifest.review_verdict == "PENDING"


# ---------------------------------------------------------------------------
# qa_verdict — extended passing state set
# ---------------------------------------------------------------------------


def test_qa_verdict_passed_at_qa_pass():
    manifest = _build(FakeTask(state=TaskState.QA_PASS))
    assert manifest.qa_verdict == "PASSED"


def test_qa_verdict_passed_at_human_merged():
    manifest = _build(FakeTask(state=TaskState.HUMAN_MERGED))
    assert manifest.qa_verdict == "PASSED"


def test_qa_verdict_passed_at_complete():
    manifest = _build(FakeTask(state=TaskState.COMPLETE))
    assert manifest.qa_verdict == "PASSED"


def test_qa_verdict_passed_when_escalated_after_qa():
    manifest = _build(FakeTask(state=TaskState.ESCALATED_TO_OPUS))
    assert manifest.qa_verdict == "PASSED"


def test_qa_verdict_unknown_before_qa():
    manifest = _build(FakeTask(state=TaskState.SUBMITTED))
    assert manifest.qa_verdict == "UNKNOWN"


# ---------------------------------------------------------------------------
# format_for_human fallback: absent verdict -> UNKNOWN (not APPROVED)
# ---------------------------------------------------------------------------


def test_format_for_human_absent_verdicts_render_unknown_not_approved():
    manifest = PRManifest(
        task_id="t1",
        plan_id="p1",
        description="d",
        test_stubs_approved=False,  # avoid the TDD-status "APPROVED" token
    )
    rendered = manifest.format_for_human()

    assert "| TODO Review | UNKNOWN |" in rendered
    assert "| Architect Review | UNKNOWN |" in rendered
    assert "| QA Verification | UNKNOWN |" in rendered
    # The old bug rendered these gate rows as APPROVED/PASSED by default.
    assert "| TODO Review | APPROVED |" not in rendered
    assert "| QA Verification | PASSED |" not in rendered


def test_format_for_human_present_verdicts_render_verbatim():
    manifest = PRManifest(
        task_id="t1",
        plan_id="p1",
        description="d",
        todo_verdict="APPROVED",
        review_verdict="PENDING",
        qa_verdict="PASSED",
    )
    rendered = manifest.format_for_human()

    assert "| TODO Review | APPROVED |" in rendered
    assert "| Architect Review | PENDING |" in rendered
    assert "| QA Verification | PASSED |" in rendered
