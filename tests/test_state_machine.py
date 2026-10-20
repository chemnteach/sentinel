"""
tests/test_state_machine.py

Tests for the task lifecycle state machine.

Covers valid transitions, invalid transitions, gate enforcement (TDD gate,
documentation gate), and audit event structure.
"""

from __future__ import annotations

import pytest

from sentinel.orchestrator.state_machine import (
    GateViolationError,
    InvalidTransitionError,
    TaskContext,
    TaskState,
    TaskStateMachine,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_machine(
    state: TaskState = TaskState.PLANNED,
    test_stubs_approved: bool = False,
    todo_approved: bool = False,
    doc_updated: bool = False,
) -> TaskStateMachine:
    ctx = TaskContext(
        task_id="task-test",
        state=state,
        test_stubs_approved=test_stubs_approved,
        todo_approved=todo_approved,
        doc_updated=doc_updated,
    )
    return TaskStateMachine(ctx)


# ---------------------------------------------------------------------------
# Basic transitions
# ---------------------------------------------------------------------------

class TestValidTransitions:
    def test_planned_to_assigned(self) -> None:
        machine = make_machine(TaskState.PLANNED)
        new_state, audit = machine.transition(TaskState.ASSIGNED, actor="orchestrator")
        assert new_state == TaskState.ASSIGNED
        assert machine.current_state == TaskState.ASSIGNED

    def test_assigned_to_todo_submitted(self) -> None:
        machine = make_machine(TaskState.ASSIGNED)
        new_state, _ = machine.transition(TaskState.TODO_SUBMITTED, actor="coder")
        assert new_state == TaskState.TODO_SUBMITTED

    def test_todo_submitted_to_todo_approved(self) -> None:
        machine = make_machine(TaskState.TODO_SUBMITTED)
        new_state, _ = machine.transition(TaskState.TODO_APPROVED, actor="architect")
        assert new_state == TaskState.TODO_APPROVED

    def test_submitted_to_review_pass(self) -> None:
        machine = make_machine(TaskState.SUBMITTED)
        new_state, _ = machine.transition(TaskState.REVIEW_PASS, actor="architect")
        assert new_state == TaskState.REVIEW_PASS

    def test_submitted_to_review_fail(self) -> None:
        machine = make_machine(TaskState.SUBMITTED)
        new_state, _ = machine.transition(TaskState.REVIEW_FAIL, actor="architect")
        assert new_state == TaskState.REVIEW_FAIL

    def test_submitted_to_disputed(self) -> None:
        machine = make_machine(TaskState.SUBMITTED)
        new_state, _ = machine.transition(TaskState.DISPUTED, actor="architect")
        assert new_state == TaskState.DISPUTED

    def test_qa_pass_path(self) -> None:
        machine = make_machine(
            TaskState.QA_PASS,
            doc_updated=True,
        )
        new_state, _ = machine.transition(TaskState.DOC_UPDATED, actor="documentation")
        assert new_state == TaskState.DOC_UPDATED


class TestInvalidTransitions:
    def test_planned_cannot_skip_to_in_progress(self) -> None:
        machine = make_machine(TaskState.PLANNED)
        with pytest.raises(InvalidTransitionError):
            machine.transition(TaskState.IN_PROGRESS, actor="orchestrator")

    def test_complete_is_terminal(self) -> None:
        machine = make_machine(TaskState.COMPLETE)
        with pytest.raises(InvalidTransitionError):
            machine.transition(TaskState.PLANNED, actor="orchestrator")

    def test_cancelled_is_terminal(self) -> None:
        machine = make_machine(TaskState.CANCELLED)
        with pytest.raises(InvalidTransitionError):
            machine.transition(TaskState.PLANNED, actor="orchestrator")

    def test_review_pass_cannot_go_to_in_progress_directly(self) -> None:
        machine = make_machine(TaskState.REVIEW_PASS)
        with pytest.raises(InvalidTransitionError):
            machine.transition(TaskState.IN_PROGRESS, actor="orchestrator")


# ---------------------------------------------------------------------------
# TDD Gate (TODO_APPROVED → IN_PROGRESS)
# ---------------------------------------------------------------------------

class TestTDDGate:
    def test_gate_blocks_when_stubs_not_approved(self) -> None:
        """The TDD gate must block IN_PROGRESS when test_stubs_approved=False."""
        machine = make_machine(
            TaskState.TODO_APPROVED,
            todo_approved=True,
            test_stubs_approved=False,  # Gate should block
        )
        with pytest.raises(GateViolationError) as exc_info:
            machine.transition(TaskState.IN_PROGRESS, actor="orchestrator")
        assert "test_stubs_approved" in str(exc_info.value).lower() or \
               "test stubs" in str(exc_info.value).lower()

    def test_gate_blocks_when_todo_not_approved(self) -> None:
        """The TDD gate must also block if todo_approved=False."""
        machine = make_machine(
            TaskState.TODO_APPROVED,
            todo_approved=False,
            test_stubs_approved=True,
        )
        with pytest.raises(GateViolationError):
            machine.transition(TaskState.IN_PROGRESS, actor="orchestrator")

    def test_gate_passes_when_both_approved(self) -> None:
        """The TDD gate must pass when both approvals are True."""
        machine = make_machine(
            TaskState.TODO_APPROVED,
            todo_approved=True,
            test_stubs_approved=True,
        )
        new_state, audit = machine.transition(TaskState.IN_PROGRESS, actor="orchestrator")
        assert new_state == TaskState.IN_PROGRESS
        assert audit["gate_checked"] is True

    def test_gate_violation_is_subclass_of_invalid_transition(self) -> None:
        machine = make_machine(TaskState.TODO_APPROVED, todo_approved=True)
        with pytest.raises(InvalidTransitionError):
            machine.transition(TaskState.IN_PROGRESS, actor="orchestrator")


# ---------------------------------------------------------------------------
# Documentation Gate (QA_PASS → DOC_UPDATED)
# ---------------------------------------------------------------------------

class TestDocumentationGate:
    def test_gate_blocks_when_doc_not_updated(self) -> None:
        machine = make_machine(TaskState.QA_PASS, doc_updated=False)
        with pytest.raises(GateViolationError):
            machine.transition(TaskState.DOC_UPDATED, actor="documentation")

    def test_gate_passes_when_doc_updated(self) -> None:
        machine = make_machine(TaskState.QA_PASS, doc_updated=True)
        new_state, _ = machine.transition(TaskState.DOC_UPDATED, actor="documentation")
        assert new_state == TaskState.DOC_UPDATED


# ---------------------------------------------------------------------------
# Approval helpers
# ---------------------------------------------------------------------------

class TestApprovalHelpers:
    def test_approve_todo_sets_flag(self) -> None:
        machine = make_machine(TaskState.TODO_SUBMITTED)
        event = machine.approve_todo(actor="architect")
        assert machine._ctx.todo_approved is True
        assert event["event"] == "todo_plan_approved"

    def test_approve_test_stubs_sets_flag(self) -> None:
        machine = make_machine(TaskState.TODO_SUBMITTED)
        event = machine.approve_test_stubs(actor="architect")
        assert machine._ctx.test_stubs_approved is True
        assert event["event"] == "test_stubs_approved"

    def test_mark_doc_updated_sets_flag(self) -> None:
        machine = make_machine(TaskState.QA_PASS)
        event = machine.mark_doc_updated(actor="documentation")
        assert machine._ctx.doc_updated is True
        assert event["event"] == "documentation_updated"

    def test_can_transition_after_approvals(self) -> None:
        """Full happy path: approve both, then transition to IN_PROGRESS."""
        machine = make_machine(TaskState.TODO_APPROVED)
        machine.approve_todo(actor="architect")
        machine.approve_test_stubs(actor="architect")
        new_state, _ = machine.transition(TaskState.IN_PROGRESS, actor="orchestrator")
        assert new_state == TaskState.IN_PROGRESS


# ---------------------------------------------------------------------------
# Audit event structure
# ---------------------------------------------------------------------------

class TestAuditEvents:
    def test_audit_event_contains_required_fields(self) -> None:
        machine = make_machine(TaskState.PLANNED)
        _, audit = machine.transition(TaskState.ASSIGNED, actor="orchestrator", reason="test")
        assert "task_id" in audit
        assert "from_state" in audit
        assert "to_state" in audit
        assert "actor" in audit
        assert "timestamp" in audit
        assert audit["from_state"] == "PLANNED"
        assert audit["to_state"] == "ASSIGNED"
        assert audit["actor"] == "orchestrator"

    def test_audit_event_gate_checked_false_for_ungated_transition(self) -> None:
        machine = make_machine(TaskState.PLANNED)
        _, audit = machine.transition(TaskState.ASSIGNED, actor="orchestrator")
        assert audit["gate_checked"] is False


# ---------------------------------------------------------------------------
# can_transition
# ---------------------------------------------------------------------------

class TestCanTransition:
    def test_returns_true_for_valid_transition(self) -> None:
        machine = make_machine(TaskState.PLANNED)
        ok, reason = machine.can_transition(TaskState.ASSIGNED)
        assert ok is True
        assert reason == ""

    def test_returns_false_for_invalid_transition(self) -> None:
        machine = make_machine(TaskState.PLANNED)
        ok, reason = machine.can_transition(TaskState.COMPLETE)
        assert ok is False
        assert len(reason) > 0

    def test_complete_is_not_terminal_after_s3_6(self) -> None:
        # S3-6: COMPLETE may transition to REVERTED, so it is no longer terminal.
        machine = make_machine(TaskState.COMPLETE)
        assert machine.is_terminal() is False

    def test_is_terminal_for_reverted(self) -> None:
        # REVERTED is the true terminal endpoint of the rollback path.
        machine = make_machine(TaskState.REVERTED)
        assert machine.is_terminal() is True

    def test_is_terminal_for_cancelled(self) -> None:
        machine = make_machine(TaskState.CANCELLED)
        assert machine.is_terminal() is True

    def test_is_not_terminal_for_planned(self) -> None:
        machine = make_machine(TaskState.PLANNED)
        assert machine.is_terminal() is False

    def test_complete_can_transition_to_reverted(self) -> None:
        machine = make_machine(TaskState.COMPLETE)
        ok, _ = machine.can_transition(TaskState.REVERTED)
        assert ok is True

    def test_human_merged_can_transition_to_reverted(self) -> None:
        machine = make_machine(TaskState.HUMAN_MERGED)
        ok, _ = machine.can_transition(TaskState.REVERTED)
        assert ok is True
