"""
sentinel/orchestrator/state_machine.py

Task lifecycle state machine for the Sentinel governance system.

Manages valid state transitions for tasks moving through the governed
remediation workflow. Enforces all governance gates, including the TDD
gate that blocks IN_PROGRESS until test stubs have been explicitly approved.

State diagram:
    PLANNED
      → ASSIGNED           (Orchestrator assigns task to Coder)
      → TODO_SUBMITTED     (Coder submits implementation plan + test stubs)
      → TODO_APPROVED      (Architect approves impl plan AND test stubs)
      → IN_PROGRESS        (GATE: requires test_stubs_approved = True)
      → SUBMITTED          (Coder self-reviews and submits diff)
      → REVIEW_PASS        (Architect approves diff)
        or REVIEW_FAIL     (Architect rejects — loops back to IN_PROGRESS)
        or DISPUTED        (Disagreement protocol activated)
      → ESCALATED          (Unresolved dispute — human decision required)
      → QA_PASS            (QA Agent verifies fix)
        or QA_FAIL         (QA Agent rejects — loops back or escalates)
      → DOC_UPDATED        (Documentation Agent gate — mandatory)
      → PR_CREATED         (PR generated with full lineage)
      → HUMAN_MERGED       (Human reviews and merges)
      → COMPLETE
      → REVERTED           (S3-6: merged PR later reverted on the remote)

Error states:
      → BLOCKED            (Dependency task not yet complete)
      → CANCELLED          (Task cancelled by human or superseded)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State definitions
# ---------------------------------------------------------------------------

class TaskState(str, Enum):
    PLANNED          = "PLANNED"
    ASSIGNED         = "ASSIGNED"
    TODO_SUBMITTED   = "TODO_SUBMITTED"
    TODO_APPROVED    = "TODO_APPROVED"
    IN_PROGRESS      = "IN_PROGRESS"
    SUBMITTED        = "SUBMITTED"
    REVIEW_PASS      = "REVIEW_PASS"
    REVIEW_FAIL      = "REVIEW_FAIL"
    DISPUTED         = "DISPUTED"
    ESCALATED        = "ESCALATED"
    QA_PASS          = "QA_PASS"
    QA_FAIL          = "QA_FAIL"
    DOC_UPDATED      = "DOC_UPDATED"
    PR_CREATED       = "PR_CREATED"
    HUMAN_MERGED     = "HUMAN_MERGED"
    COMPLETE         = "COMPLETE"
    BLOCKED          = "BLOCKED"
    CANCELLED        = "CANCELLED"

    # S3-6 — rollback (revert) tracking. A merged/completed task whose PR was
    # later reverted on the remote is moved here so the linked finding can be
    # reopened for remediation.
    REVERTED         = "REVERTED"

    # Phase 4 — agentic-executor escalation ladder.
    ESCALATED_TO_CODEX = "ESCALATED_TO_CODEX"
    ESCALATED_TO_OPUS  = "ESCALATED_TO_OPUS"
    ESCALATED_TO_HUMAN = "ESCALATED_TO_HUMAN"

    # Phase A — GitHub Issues automation dispatch.
    DISPATCHED_TO_GITHUB = "DISPATCHED_TO_GITHUB"


# ---------------------------------------------------------------------------
# Transition error
# ---------------------------------------------------------------------------

class InvalidTransitionError(RuntimeError):
    """Raised when a state transition is not permitted."""

    def __init__(
        self,
        task_id: str,
        from_state: TaskState,
        to_state: TaskState,
        reason: str,
    ) -> None:
        self.task_id = task_id
        self.from_state = from_state
        self.to_state = to_state
        self.reason = reason
        super().__init__(
            f"Task {task_id}: cannot transition {from_state} → {to_state}. {reason}"
        )


class GateViolationError(InvalidTransitionError):
    """
    Raised when a governance gate blocks a transition.

    Gate violations are a subset of invalid transitions that represent a
    failed governance check (as opposed to an invalid workflow sequence).
    They are logged at WARNING level and generate an audit entry.
    """


# ---------------------------------------------------------------------------
# Valid transitions map
# ---------------------------------------------------------------------------

# Maps each state to the set of states it may transition to.
# Any transition not in this map is invalid.
_VALID_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.PLANNED:        {TaskState.ASSIGNED, TaskState.BLOCKED, TaskState.CANCELLED},
    TaskState.ASSIGNED:       {TaskState.TODO_SUBMITTED, TaskState.CANCELLED},
    TaskState.TODO_SUBMITTED: {TaskState.TODO_APPROVED, TaskState.ASSIGNED, TaskState.CANCELLED},
    TaskState.TODO_APPROVED:  {TaskState.IN_PROGRESS, TaskState.CANCELLED},
    TaskState.IN_PROGRESS:    {TaskState.SUBMITTED, TaskState.TODO_SUBMITTED, TaskState.CANCELLED},
    TaskState.SUBMITTED:      {TaskState.REVIEW_PASS, TaskState.REVIEW_FAIL, TaskState.DISPUTED},
    TaskState.REVIEW_PASS:    {TaskState.QA_PASS, TaskState.QA_FAIL},
    TaskState.REVIEW_FAIL:    {TaskState.IN_PROGRESS, TaskState.DISPUTED, TaskState.CANCELLED},
    TaskState.DISPUTED:       {TaskState.IN_PROGRESS, TaskState.ESCALATED},
    TaskState.ESCALATED:      {TaskState.IN_PROGRESS, TaskState.CANCELLED},
    TaskState.QA_PASS:        {TaskState.DOC_UPDATED},
    TaskState.QA_FAIL:        {TaskState.IN_PROGRESS, TaskState.DISPUTED},
    TaskState.DOC_UPDATED:    {TaskState.PR_CREATED, TaskState.IN_PROGRESS},
    TaskState.PR_CREATED:     {TaskState.HUMAN_MERGED, TaskState.IN_PROGRESS},
    TaskState.HUMAN_MERGED:   {TaskState.COMPLETE, TaskState.REVERTED},
    TaskState.BLOCKED:        {TaskState.PLANNED},
    # S3-6: a merged PR that is later reverted on the remote moves the task to
    # REVERTED (actor: human). COMPLETE is otherwise terminal.
    TaskState.COMPLETE:       {TaskState.REVERTED},
    TaskState.CANCELLED:      set(),  # Terminal state
    TaskState.REVERTED:       set(),  # Terminal state — finding reopened separately
    # Phase 4 — escalation ladder. escalate_task() sets these directly; entries
    # keep the machine consistent for any downstream machine-driven transition.
    TaskState.ESCALATED_TO_CODEX: {TaskState.IN_PROGRESS, TaskState.ESCALATED_TO_OPUS, TaskState.CANCELLED},
    TaskState.ESCALATED_TO_OPUS:  {TaskState.IN_PROGRESS, TaskState.ESCALATED_TO_HUMAN, TaskState.CANCELLED},
    TaskState.ESCALATED_TO_HUMAN: {TaskState.IN_PROGRESS, TaskState.CANCELLED},
    # Phase A — GitHub dispatch. dispatch_task() sets this directly; entry keeps
    # the machine consistent for when a returned PR re-enters the lifecycle.
    TaskState.DISPATCHED_TO_GITHUB: {TaskState.SUBMITTED, TaskState.IN_PROGRESS, TaskState.CANCELLED},
}


# ---------------------------------------------------------------------------
# Task context (minimal — full context lives in SQLite)
# ---------------------------------------------------------------------------

class TaskContext:
    """
    Minimal in-memory context for a task, used by the state machine to
    evaluate gate conditions. The Orchestrator populates this from SQLite
    before calling transition().
    """

    def __init__(
        self,
        task_id: str,
        state: TaskState,
        test_stubs_approved: bool = False,
        todo_approved: bool = False,
        doc_updated: bool = False,
        plan_id: str | None = None,
    ) -> None:
        self.task_id = task_id
        self.state = state
        self.test_stubs_approved = test_stubs_approved
        self.todo_approved = todo_approved
        self.doc_updated = doc_updated
        self.plan_id = plan_id


# ---------------------------------------------------------------------------
# Gate checks
# ---------------------------------------------------------------------------

def _gate_todo_approved_to_in_progress(ctx: TaskContext) -> None:
    """
    TDD Gate: TODO_APPROVED → IN_PROGRESS

    The Coding Agent may not begin implementation until both the
    implementation plan AND the test stubs have been explicitly approved
    by the Architect as separate verdicts.

    This gate cannot be bypassed. There is no override mechanism.
    """
    if not ctx.todo_approved:
        raise GateViolationError(
            task_id=ctx.task_id,
            from_state=TaskState.TODO_APPROVED,
            to_state=TaskState.IN_PROGRESS,
            reason=(
                "Implementation plan has not been approved. "
                "The Architect must explicitly approve the implementation plan "
                "before the Coding Agent may begin work."
            ),
        )

    if not ctx.test_stubs_approved:
        raise GateViolationError(
            task_id=ctx.task_id,
            from_state=TaskState.TODO_APPROVED,
            to_state=TaskState.IN_PROGRESS,
            reason=(
                "Test stubs have not been approved (test_stubs_approved = False). "
                "Sentinel enforces test-driven development: the Architect must explicitly "
                "approve the test stubs as a separate verdict from the implementation plan. "
                "The Coding Agent may not begin implementation until test stubs are approved. "
                "There is no override for this gate."
            ),
        )


def _gate_qa_pass_to_doc_updated(ctx: TaskContext) -> None:
    """
    Documentation Gate: QA_PASS → DOC_UPDATED

    Documentation must be updated before a PR can be created.
    The Documentation Agent sets doc_updated = True when done.
    """
    if not ctx.doc_updated:
        raise GateViolationError(
            task_id=ctx.task_id,
            from_state=TaskState.QA_PASS,
            to_state=TaskState.DOC_UPDATED,
            reason=(
                "Documentation has not been updated (doc_updated = False). "
                "The Documentation Agent must update all affected documentation "
                "before the task is eligible for PR creation. "
                "Documentation is a gate, not an afterthought."
            ),
        )


# Maps (from_state, to_state) pairs to their gate check functions.
# Gates are checked after the transition is confirmed to be structurally valid.
_GATES: dict[tuple[TaskState, TaskState], Any] = {
    (TaskState.TODO_APPROVED, TaskState.IN_PROGRESS): _gate_todo_approved_to_in_progress,
    (TaskState.QA_PASS,       TaskState.DOC_UPDATED): _gate_qa_pass_to_doc_updated,
}


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class TaskStateMachine:
    """
    Manages state transitions for a single task.

    Instantiate with a TaskContext loaded from SQLite. Call transition()
    to attempt a state change. The machine validates the transition
    structurally and evaluates any governance gates.

    The machine does not write to SQLite — that is the Orchestrator's
    responsibility. The machine returns the new state and a list of
    audit events; the caller persists them.
    """

    def __init__(self, ctx: TaskContext) -> None:
        self._ctx = ctx

    @property
    def current_state(self) -> TaskState:
        return self._ctx.state

    def can_transition(self, to_state: TaskState) -> tuple[bool, str]:
        """
        Check whether a transition is valid without executing it.

        Returns (True, "") if valid, or (False, reason) if not.
        Does not check gates — use transition() to enforce them.
        """
        valid_targets = _VALID_TRANSITIONS.get(self._ctx.state, set())
        if to_state not in valid_targets:
            return False, (
                f"{self._ctx.state} → {to_state} is not a valid transition. "
                f"Valid transitions from {self._ctx.state}: "
                f"{', '.join(s.value for s in sorted(valid_targets, key=lambda s: s.value)) or 'none (terminal state)'}"
            )
        return True, ""

    def transition(
        self,
        to_state: TaskState,
        actor: str,
        reason: str = "",
    ) -> tuple[TaskState, dict[str, Any]]:
        """
        Attempt to transition the task to a new state.

        Validates the transition sequence, evaluates any governance gates,
        and returns the new state with an audit event dict.

        Args:
            to_state: The target state.
            actor: The agent or human initiating the transition.
            reason: Optional reason or context for the transition.

        Returns:
            (new_state, audit_event) — the caller persists both.

        Raises:
            InvalidTransitionError: If the transition is structurally invalid.
            GateViolationError: If a governance gate blocks the transition.
        """
        from_state = self._ctx.state

        # Structural check
        valid, msg = self.can_transition(to_state)
        if not valid:
            raise InvalidTransitionError(
                task_id=self._ctx.task_id,
                from_state=from_state,
                to_state=to_state,
                reason=msg,
            )

        # Gate check
        gate_fn = _GATES.get((from_state, to_state))
        if gate_fn is not None:
            gate_fn(self._ctx)  # raises GateViolationError if blocked

        # Transition is valid — update in-memory state
        self._ctx.state = to_state

        audit_event = {
            "task_id": self._ctx.task_id,
            "from_state": from_state.value,
            "to_state": to_state.value,
            "actor": actor,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "gate_checked": gate_fn is not None,
        }

        logger.info(
            "Task %s: %s → %s (actor=%s)",
            self._ctx.task_id,
            from_state.value,
            to_state.value,
            actor,
        )

        return to_state, audit_event

    def approve_todo(self, actor: str) -> dict[str, Any]:
        """
        Record explicit Architect approval of the implementation plan.

        Sets todo_approved = True. This does not transition state — it is
        a prerequisite for the TODO_APPROVED → IN_PROGRESS transition.
        """
        self._ctx.todo_approved = True
        event = {
            "task_id": self._ctx.task_id,
            "event": "todo_plan_approved",
            "actor": actor,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("Task %s: implementation plan approved by %s", self._ctx.task_id, actor)
        return event

    def approve_test_stubs(self, actor: str) -> dict[str, Any]:
        """
        Record explicit Architect approval of the test stubs.

        Sets test_stubs_approved = True. This is a separate verdict from
        the implementation plan approval and is independently required for
        the TODO_APPROVED → IN_PROGRESS transition.

        The TDD gate will block IN_PROGRESS until this is called.
        """
        self._ctx.test_stubs_approved = True
        event = {
            "task_id": self._ctx.task_id,
            "event": "test_stubs_approved",
            "actor": actor,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("Task %s: test stubs approved by %s", self._ctx.task_id, actor)
        return event

    def mark_doc_updated(self, actor: str) -> dict[str, Any]:
        """
        Record that the Documentation Agent has updated affected documentation.

        Sets doc_updated = True, satisfying the documentation gate for
        the QA_PASS → DOC_UPDATED transition.
        """
        self._ctx.doc_updated = True
        event = {
            "task_id": self._ctx.task_id,
            "event": "documentation_updated",
            "actor": actor,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("Task %s: documentation updated by %s", self._ctx.task_id, actor)
        return event

    def is_terminal(self) -> bool:
        """Return True if the task is in a terminal state (no further transitions possible)."""
        return not _VALID_TRANSITIONS.get(self._ctx.state)

    def __repr__(self) -> str:
        return (
            f"TaskStateMachine("
            f"task_id={self._ctx.task_id!r}, "
            f"state={self._ctx.state.value!r}, "
            f"test_stubs_approved={self._ctx.test_stubs_approved}, "
            f"todo_approved={self._ctx.todo_approved}"
            f")"
        )
