"""
sentinel/orchestrator/task_manager.py

Task lifecycle management for the Sentinel governed remediation phase.

All state is persisted as JSON via StateStore. No SQLite. State files
are committed to the repository after each significant step; git history
provides the complete audit trail.

Responsibilities:
  - Creating atomic tasks from approved plan items
  - Assembling agent context for each workflow step
  - Coordinating the TODO submission and approval workflow
  - Enforcing state machine transitions
  - Writing all state changes to JSON via StateStore
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from sentinel.assess.finding_status import BundleStatus
from sentinel.orchestrator.state_machine import (
    GateViolationError,
    TaskContext,
    TaskState,
    TaskStateMachine,
)
from sentinel.orchestrator.state_store import StateStore, TaskConcurrencyConflictError
from sentinel.orchestrator.todo_schema import (
    TodoSubmission,
    TodoValidationError,
    format_todo_for_architect,
    parse_and_validate_todo,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Task dataclass
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """
    A single atomic task decomposed from the approved remediation plan.

    Each task maps to exactly one plan item and results in exactly one PR.
    """

    task_id: str
    plan_id: str
    state: TaskState = TaskState.PLANNED

    description: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    affected_files: list[str] = field(default_factory=list)
    scope_boundary: str = ""
    required_skills: list[str] = field(default_factory=list)

    test_stubs: dict[str, Any] | None = None
    test_stubs_approved: bool = False
    todo_approved: bool = False
    doc_updated: bool = False

    assigned_agent: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    last_human_review_comment: str = ""
    latest_diff_summary: str = ""

    # Git HEAD SHA captured when the Coder enters IN_PROGRESS, before any edits.
    # Used by the semantic verification gate (SENTINEL_VERIFICATION_GATE) to run
    # a targeted --changed-since re-review after QA. Empty until first captured;
    # from_dict tolerates its absence in task JSON written before this field.
    pre_fix_sha: str = ""

    # cycle 58 — the Coder's implement() result, persisted so it survives to
    # approval time (approve_local_and_push runs in a separate CLI invocation
    # and reloads the task from JSON). files_changed drives the scoped commit;
    # the test fields feed the PR body. Empty defaults are backward compatible
    # with task JSON written before cycle 58 (from_dict tolerates missing keys).
    files_changed: list[str] = field(default_factory=list)
    test_results: str = ""
    test_summary: str = ""
    tests_pass: bool = False

    # cycle 59 — structured feedback from a failed Phase 3 backwards-compat
    # gate (Layer 7c), injected into the Coder's next implementation context.
    compat_feedback: str = ""

    # Phase 2 retry loop (Plan Section 5) — the structured retry-context packet
    # built on the most recent gate rejection (Architect / compat / QA), so the
    # Coder's next attempt sees why the last diff was rejected and is told not to
    # repeat it. Empty until a gate first fails. Injected by
    # build_implementation_context. Backward compatible: from_dict tolerates its
    # absence in task JSON written before this field existed.
    retry_context: str = ""

    # Phase 3 — populated only for FER-derived tasks
    is_phase3: bool = False
    source_fer_id: str = ""
    compat_check_passed: bool = False

    # Pattern lineage — populated when a plan item maps to a single
    # Stage 5b remediation pattern. Empty string when the item spans
    # multiple patterns or no pattern.
    pattern_id: str = ""

    # Structured triage conditions threaded from approved tech-debt items via
    # the pattern → group → findings lineage (cycle 52). None when the task has
    # no finding lineage or its findings carried no conditions.
    conditions_structured: list[dict[str, Any]] | None = None

    # S2-2 — bundle lineage. When this task's plan item belonged to a
    # remediation bundle (Stage 5d), bundle_id names it and bundle_status
    # tracks the bundle's lifecycle. None / UNDECIDED for tasks with no bundle
    # (backward compatible: from_dict tolerates their absence).
    bundle_id: str | None = None
    bundle_status: str = BundleStatus.UNDECIDED

    # Verification routing — list of skill names QA runs to verify this
    # task. Empty list means no automated verification (FIX-BLOCKING-*
    # registry blockers; resolution is human-judged). Default empty
    # mirrors the pattern_id precedent: executor supplies the value at
    # create_task time via key-presence disambiguation; the dataclass
    # default provides a safe fallback for direct construction in tests.
    verification_required: list[str] = field(default_factory=list)

    # Phase 4 — escalation: the structured packet handed to the next executor
    # (or human) when this task is escalated. None until escalated.
    escalation_packet: str | None = None

    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    review_attempts: int = 0

    # Number of dispute-resolution rounds this task has been through. Bounds the
    # dispute → resolve → _run_task re-entry loop so a task that keeps failing
    # cannot cycle forever; once it exceeds the cap it escalates to a human.
    # Backward compatible: from_dict tolerates its absence (defaults to 0).
    dispute_rounds: int = 0

    # Monotonic optimistic-concurrency token, incremented on every guarded
    # transition persist. Compared before writing so a transition computed
    # against a stale snapshot is rejected rather than clobbering newer state
    # (FIX-BLOCKING-008). Robust to coarse wall-clock resolution. Backward
    # compatible: from_dict tolerates its absence (defaults to 0).
    revision: int = 0

    # Latest gate feedback persisted on the task record so retry/context-building
    # code can read it after a reload instead of scanning the audit log
    # (FIX-BLOCKING-004). Empty until the corresponding gate first provides
    # feedback. Backward compatible: from_dict tolerates their absence.
    review_feedback: str = ""
    qa_feedback: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        """Deserialise from a state file dict."""
        data = dict(data)
        data["state"] = TaskState(data["state"])
        return cls(**data)


# ---------------------------------------------------------------------------
# TaskManager
# ---------------------------------------------------------------------------


def _read_source_files(
    affected_files: list[str],
    repo_root: str,
    max_lines_per_file: int = 300,
    target_ranges: dict[str, tuple[int, int]] | None = None,
    full_file_threshold: int = 1500,
) -> str:
    """Read source content for the files the Coder must modify (cycle 56).

    Returns a formatted block per readable file::

        --- <file_path> ---
        <source, up to max_lines_per_file lines>
        ---

    Files that cannot be read are skipped; each file is capped to stay within
    context limits. Returns "" when nothing can be read.
    """
    from pathlib import Path

    sections: list[str] = []
    for rel_path in affected_files:
        path = (Path(repo_root) / rel_path) if repo_root else Path(rel_path)
        if not path.exists() and repo_root:
            # Subtree assessment: affected_files are relative to sentinel/, so a
            # bare path like govern/conditions.py misses. Try the sentinel/
            # subtree, mirroring source_retrieval._normalize_path. Without this
            # the file is silently skipped and the Coder, shown no source,
            # hallucinates the function body (run-011 batch unblock).
            path = Path(repo_root) / "sentinel" / rel_path
        if not path.exists():
            path = Path(rel_path)
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        original = len(lines)
        if original > max_lines_per_file:
            if original <= full_file_threshold:
                # Small enough to include in full — no truncation needed
                pass
            else:
                target = (target_ranges or {}).get(rel_path)
                if target:
                    start_line, end_line = target
                    center = ((start_line - 1) + (end_line - 1)) // 2
                    half = max_lines_per_file // 2
                    slice_start = max(0, center - half)
                    slice_end = min(len(lines), slice_start + max_lines_per_file)
                    # always return a full window, even when centered near EOF
                    if slice_end - slice_start < max_lines_per_file:
                        slice_start = max(0, slice_end - max_lines_per_file)
                    lines = lines[slice_start:slice_end]
                    lines.append(
                        f"# ... (showing lines {slice_start + 1}-{slice_end} of {original})"
                    )
                else:
                    lines = lines[:max_lines_per_file]
                    lines.append(f"# ... ({original - max_lines_per_file} more lines truncated)")
        sections.append(f"--- {rel_path} ---\n" + "\n".join(lines) + "\n---")

    if not sections:
        return ""
    return "\n\nSOURCE FILES TO MODIFY:\n" + "\n\n".join(sections)


def _get_git_diff(repo_root: str, files: list[str] | None = None) -> str:
    """Get the git diff of working-tree changes (cycle 57).

    Used to give the Architect the *real* diff the Coder applied (via
    ``apply_edits``) rather than the Coder's prose ``diff_summary``. When
    ``files`` is given the diff is scoped to those paths. Returns "" on any
    failure (no git, not a repo, clean tree) so callers can fall back to prose.
    """
    import subprocess

    cmd = ["git", "-C", repo_root, "diff", "--no-color"]
    if files:
        cmd += ["--"] + list(files)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30, check=True,
        )
        return result.stdout or ""
    except subprocess.CalledProcessError as exc:
        # git exited non-zero (e.g. not a repo, bad pathspec). This is a
        # read-only diff for review context, so a failure is non-fatal —
        # callers fall back to the Coder's prose. Surface the signal at
        # WARNING rather than swallowing it silently.
        logger.warning(
            "git diff failed (rc=%s): %s",
            exc.returncode,
            (exc.stderr or "").strip() if isinstance(exc.stderr, str) else exc.stderr,
        )
        return ""
    except Exception as exc:
        # No git, timeout, encoding failure, etc. Still non-fatal for a
        # review-context diff; log so the loss of signal is visible.
        logger.warning("git diff unavailable: %s", exc)
        return ""


def _is_tracked_at_head(repo_root: str, rel_path: str) -> bool:
    """True if ``rel_path`` is a file git already tracks (exists in the index).

    Used to distinguish a newly-created file (untracked — invisible to
    ``git diff``, so it must be shown in full) from a modification to an
    existing file (already represented in the diff). ``git ls-files
    --error-unmatch`` returns 0 for a tracked path and non-zero otherwise.
    On any git failure we return ``False`` (treat as new) so the file is still
    shown to the reviewer rather than silently dropped.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "ls-files", "--error-unmatch", "--", rel_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception as exc:
        logger.warning("git ls-files check failed for %s: %s", rel_path, exc)
        return False


class TaskManager:
    """
    Manages the full lifecycle of tasks in the governed remediation phase.

    All state changes are persisted to JSON via StateStore. All events are
    appended to audit_log.json. The TaskManager is the only component that
    writes task state.
    """

    def __init__(self, store: StateStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Task creation
    # ------------------------------------------------------------------

    def create_task(
        self,
        plan_id: str,
        description: str,
        acceptance_criteria: list[str],
        affected_files: list[str],
        scope_boundary: str,
        required_skills: list[str] | None = None,
        task_id: str | None = None,
        # Phase 3 optional fields
        source_fer_id: str = "",
        is_phase3: bool = False,
        test_stubs: dict | None = None,
        # Pattern lineage — empty string when item has no single-pattern lineage
        pattern_id: str = "",
        # Verification routing — None means caller did not supply; coalesced
        # to empty list. Orchestrator-side key-presence disambiguation at the
        # _extract_plan_items call site resolves the actual value before
        # reaching here (see Q8c lock in cycle 6 design pass).
        verification_required: list[str] | None = None,
        # Structured triage conditions threaded from approved tech-debt items
        # (cycle 52). None when the task has no finding lineage / no conditions.
        conditions_structured: list[dict] | None = None,
        # S2-2 — bundle lineage from the plan item. Optional / backward-compatible.
        bundle_id: str | None = None,
        bundle_status: str = BundleStatus.UNDECIDED,
    ) -> Task:
        """Create a new task from an approved plan item (PLANNED state)."""
        task_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        task = Task(
            task_id=task_id,
            plan_id=plan_id,
            description=description,
            acceptance_criteria=acceptance_criteria,
            affected_files=affected_files,
            scope_boundary=scope_boundary,
            required_skills=required_skills or [],
            is_phase3=is_phase3,
            source_fer_id=source_fer_id,
            test_stubs=test_stubs,
            pattern_id=pattern_id,
            verification_required=verification_required or [],
            conditions_structured=conditions_structured,
            bundle_id=bundle_id,
            bundle_status=bundle_status,
        )
        self._persist(task)
        self._audit(
            task_id=task_id,
            agent="orchestrator",
            action="task_created",
            details={"plan_id": plan_id, "description": description},
        )
        logger.info("Created task %s from plan %s", task_id, plan_id)
        return task

    def load_task(self, task_id: str) -> Task:
        """Load a task from the state store. Raises KeyError if not found."""
        data = self._store.get_task(task_id)
        if data is None:
            raise KeyError(f"Task '{task_id}' not found in state store")
        return Task.from_dict(data)

    def load_task_by_plan_id(self, plan_id: str) -> Task | None:
        """Load an existing task by plan_id, returning None when absent."""
        tasks = self._store.read("tasks")
        for item in tasks:
            if item.get("plan_id") == plan_id:
                return Task.from_dict(item)
        return None

    # ------------------------------------------------------------------
    # Assignment
    # ------------------------------------------------------------------

    def assign_task(self, task: Task, agent: str = "coder") -> Task:
        """Assign a PLANNED task to the Coding Agent."""
        machine = self._machine(task)
        _, audit = machine.transition(TaskState.ASSIGNED, actor="orchestrator")
        task.state = TaskState.ASSIGNED
        task.assigned_agent = agent
        self._persist(task)
        self._audit_event(audit)
        return task

    def build_coding_context(self, task: Task) -> str:
        """Assemble the context injected into the Coding Agent on assignment."""
        criteria_block = "\n".join(
            f"  {i+1}. {c}" for i, c in enumerate(task.acceptance_criteria)
        )
        files_block = "\n".join(f"  - {f}" for f in task.affected_files)
        skills_block = (
            ", ".join(task.required_skills) if task.required_skills else "None specified"
        )

        return (
            f"## Task Assignment — {task.task_id}\n\n"
            f"**Description:** {task.description}\n\n"
            f"**Acceptance Criteria:**\n{criteria_block}\n\n"
            f"**Affected Files (your scope boundary):**\n{files_block}\n\n"
            f"**Scope Boundary (what NOT to change):**\n{task.scope_boundary}\n\n"
            f"**Required Skills:** {skills_block}\n\n"
            "---\n\n"
            "### TDD Requirement\n\n"
            "Your TODO must include both sections:\n"
            "1. **Implementation Plan** — what you will change and how\n"
            "2. **Test Stubs** — runnable stubs (raising NotImplementedError) for every "
            "acceptance criterion listed above\n\n"
            "The Orchestrator rejects TODOs missing either field.\n"
            "The Architect reviews plan and stubs as **separate verdicts**. "
            "You may not begin implementation until BOTH are approved.\n\n"
            "After approval: tests first (red), then implementation (green), "
            "then refactor within scope. Submit with all tests passing.\n"
        )

    # ------------------------------------------------------------------
    # TODO submission and approval
    # ------------------------------------------------------------------

    def receive_todo(self, task: Task, todo_json: str) -> TodoSubmission:
        """
        Receive and validate a TODO submission from the Coding Agent.

        Raises TodoValidationError if test stubs are absent or the TODO
        is malformed. A rejected TODO never reaches the Architect.
        """
        try:
            todo = parse_and_validate_todo(
                todo_json,
                acceptance_criteria=task.acceptance_criteria,
            )
        except TodoValidationError:
            self._audit(
                task_id=task.task_id,
                agent="coder",
                action="todo_rejected",
                details={"reason": "Schema validation failed"},
            )
            raise

        task.test_stubs = todo.to_dict()["test_stubs"]
        machine = self._machine(task)
        _, audit = machine.transition(TaskState.TODO_SUBMITTED, actor="coder")
        task.state = TaskState.TODO_SUBMITTED
        self._persist_with_audit(
            task,
            [
                self._audit_event_record(audit),
                self._audit_record(
                    task.task_id,
                    "coder",
                    "todo_submitted",
                    {
                        "stub_count": len(todo.test_stubs.stubs),
                        "files_to_modify": todo.implementation_plan.files_to_modify,
                        "out_of_scope_findings": len(todo.out_of_scope_findings),
                    },
                ),
            ],
        )
        logger.info(
            "Task %s: TODO received with %d stub(s)",
            task.task_id,
            len(todo.test_stubs.stubs),
        )
        return todo

    def build_architect_todo_context(self, task: Task, todo: TodoSubmission) -> str:
        """Assemble context for the Architect's TODO review."""
        from sentinel.govern.conditions import format_constraints_for_architect

        todo_block = format_todo_for_architect(todo)
        criteria_block = "\n".join(
            f"  {i+1}. {c}" for i, c in enumerate(task.acceptance_criteria)
        )
        # cycle 52 — owner-set structured conditions become hard plan constraints,
        # rendered prominently before the TODO so the Architect treats them as rules.
        constraints = format_constraints_for_architect(task.conditions_structured)
        constraints_block = f"**{constraints}**\n\n" if constraints else ""
        return (
            f"## TODO Review Request — Task {task.task_id}\n\n"
            f"**Task description:** {task.description}\n\n"
            f"**Acceptance criteria (your review benchmark):**\n{criteria_block}\n\n"
            f"**Scope boundary:** {task.scope_boundary}\n\n"
            f"{constraints_block}"
            "---\n\n"
            f"{todo_block}\n\n"
            "---\n\n"
            "## Your Review Task\n\n"
            "Evaluate the TODO submission above and provide **two explicit verdicts**.\n\n"
            "**Calibration (read first).** This gate decides whether implementation "
            "may *begin* — it is not a final code review, and the diff will be "
            "reviewed again after implementation. **Default to APPROVE.** Issue "
            "REVISE only for a concrete deficiency that would let a broken "
            "implementation pass later verification:\n"
            "- an acceptance criterion with no corresponding test stub, or\n"
            "- a stub whose expected outcome (the `Then`) is missing, wrong, or so "
            "vague it would assert nothing.\n"
            "Do NOT issue REVISE for additional edge cases you would like to see, "
            "naming, wording, style, or optional improvements — record those in the "
            "feedback field and still APPROVE. A stub set that adequately covers the "
            "listed acceptance criteria is an APPROVE, even if a more exhaustive set "
            "is imaginable.\n\n"
            "### Verdict 1: Implementation Plan\n"
            "- Does the approach correctly address the task?\n"
            "- Are the right files in scope?\n"
            "- Are edge cases complete? Risks acknowledged?\n"
            "- Verdict: APPROVE or REVISE (with specific feedback)\n\n"
            "### Verdict 2: Test Stubs\n"
            "- Does each stub map to a specific acceptance criterion?\n"
            "- Do the Given/When/Then specifications correctly describe the contract?\n"
            "- Are error conditions and edge cases represented?\n"
            "- Would these stubs, once implemented, give genuine confidence?\n"
            "- Verdict: APPROVE or REVISE (with specific feedback)\n\n"
            "**Both verdicts must be APPROVE before implementation begins.**\n"
            "Return your response as JSON:\n"
            "{\n"
            '  "implementation_plan_verdict": "APPROVE" | "REVISE",\n'
            '  "implementation_plan_feedback": "<feedback or empty string>",\n'
            '  "test_stubs_verdict": "APPROVE" | "REVISE",\n'
            '  "test_stubs_feedback": "<feedback or empty string>"\n'
            "}\n"
        )

    def record_todo_approval(
        self,
        task: Task,
        impl_verdict: str,
        stubs_verdict: str,
        impl_feedback: str = "",
        stubs_feedback: str = "",
        actor: str = "architect",
    ) -> Task:
        """
        Record the Architect's TODO review verdicts.

        Both verdicts must be APPROVE to advance to TODO_APPROVED.
        Either REVISE returns the task to ASSIGNED for resubmission.
        """
        machine = self._machine(task)
        both_approved = (
            impl_verdict.upper() == "APPROVE"
            and stubs_verdict.upper() == "APPROVE"
        )

        if both_approved:
            plan_event = machine.approve_todo(actor=actor)
            stubs_event = machine.approve_test_stubs(actor=actor)
            task.todo_approved = True
            task.test_stubs_approved = True
            _, transition_audit = machine.transition(TaskState.TODO_APPROVED, actor=actor)
            task.state = TaskState.TODO_APPROVED

            self._persist_with_audit(
                task,
                [
                    self._audit_event_record(plan_event),
                    self._audit_event_record(stubs_event),
                    self._audit_event_record(transition_audit),
                ],
            )
            logger.info("Task %s: TODO approved", task.task_id)
        else:
            _, transition_audit = machine.transition(TaskState.ASSIGNED, actor=actor)
            task.state = TaskState.ASSIGNED
            self._persist_with_audit(
                task,
                [
                    self._audit_event_record(transition_audit),
                    self._audit_record(
                        task.task_id,
                        actor,
                        "todo_revision_requested",
                        {
                            "implementation_plan_verdict": impl_verdict,
                            "implementation_plan_feedback": impl_feedback,
                            "test_stubs_verdict": stubs_verdict,
                            "test_stubs_feedback": stubs_feedback,
                        },
                    ),
                ],
            )
            logger.info(
                "Task %s: TODO revision requested (impl=%s, stubs=%s)",
                task.task_id, impl_verdict, stubs_verdict,
            )

        return task

    # ------------------------------------------------------------------
    # Implementation and review
    # ------------------------------------------------------------------

    def start_implementation(self, task: Task) -> Task:
        """
        Advance task to IN_PROGRESS.

        The TDD gate raises GateViolationError if test_stubs_approved is
        False. This error must not be caught and suppressed.
        """
        machine = self._machine(task)
        try:
            _, audit = machine.transition(TaskState.IN_PROGRESS, actor="orchestrator")
        except GateViolationError:
            logger.warning(
                "Task %s: TDD gate blocked IN_PROGRESS — stubs_approved=%s",
                task.task_id,
                task.test_stubs_approved,
            )
            raise  # Never suppress — governance violation

        task.state = TaskState.IN_PROGRESS
        self._persist_with_audit(task, [self._audit_event_record(audit)])
        return task

    def build_implementation_context(self, task: Task, repo_root: str = "") -> str:
        """Assemble context for the Coding Agent's implementation phase.

        cycle 56: includes the actual source of ``task.affected_files`` (so the
        Coder edits real code, not blind), and prepends owner-set
        ``conditions_structured`` as hard implementation constraints.
        """
        stubs_block: str
        if task.test_stubs and task.test_stubs.get("stubs"):
            lines = [
                f"**Test file:** `{task.test_stubs.get('test_file', 'tests/')}`",
                "",
                "Implement these approved stubs first, then write the implementation:",
                "",
            ]
            for stub in task.test_stubs["stubs"]:
                lines += ["```python", stub.get("stub_code", "# stub missing"), "```", ""]
            stubs_block = "\n".join(lines)
        else:
            stubs_block = (
                "⚠️  No approved test stubs found — this should not happen. "
                "Halt and notify the Orchestrator."
            )

        files_block = "\n".join(f"  - {f}" for f in task.affected_files)

        reviewer_feedback_block = ""
        if task.last_human_review_comment:
            reviewer_feedback_block = (
                "\n---\n\n"
                "### Human PR Review Feedback (must address before resubmission)\n\n"
                f"{task.last_human_review_comment}\n"
            )

        context = (
            f"## Implementation — Task {task.task_id}\n\n"
            "Your TODO has been approved. You may now begin implementation.\n\n"
            "**Implementation order (mandatory):**\n"
            "1. Implement the approved test stubs — tests must fail before production code\n"
            "2. Write implementation to make tests pass\n"
            "3. Refactor within scope only\n"
            "4. Self-review using Layer 2a Quick Review\n"
            "5. Submit with all tests passing\n\n"
            f"**Scope boundary (do not modify anything outside this):**\n{task.scope_boundary}\n\n"
            f"**Affected files:**\n{files_block}\n\n"
            "---\n\n"
            f"### Approved Test Stubs (implement these first)\n\n{stubs_block}\n"
            f"{reviewer_feedback_block}"
        )

        # cycle 56 — feed the actual source of the affected files so the Coder
        # can emit exact edit operations against real content.
        # Bug D fix — center the source slice on the target function when the
        # task carries line-range info for an affected file (functions near EOF
        # otherwise fall outside the head-slice). The Task model exposes no
        # line-range field today, so this resolves to None → head-slice.
        raw_ranges = getattr(task, "line_ranges", None)
        target_ranges = dict(raw_ranges) if raw_ranges else None
        source_section = _read_source_files(
            task.affected_files or [], repo_root, target_ranges=target_ranges
        )
        # Note: source omitted if unreadable — _read_source_files returns "" when
        # no affected file can be read, so the context is emitted without a
        # SOURCE FILES block rather than failing.
        if source_section:
            context += source_section

        # cycle 56 — owner-set structured conditions are hard implementation
        # constraints, rendered before the task (same block the Architect sees).
        from sentinel.govern.conditions import format_constraints_for_architect
        constraints = format_constraints_for_architect(task.conditions_structured)
        if constraints:
            context = f"**{constraints}**\n\n" + context

        # cycle 59 — surface a prior Phase 3 backwards-compat failure so the
        # Coder fixes the breaking change on the next attempt.
        if task.compat_feedback:
            context = (
                "BACKWARDS COMPATIBILITY FEEDBACK FROM PREVIOUS ATTEMPT:\n"
                + task.compat_feedback
                + "\n\n"
                + context
            )

        # Phase 2 retry loop — the structured retry-context packet built on the
        # most recent gate rejection (Architect / compat / QA). Prepended last so
        # it sits at the very top of the prompt: the Coder sees why the previous
        # patch was rejected and is instructed not to repeat that approach.
        if task.retry_context:
            context = task.retry_context + "\n\n---\n\n" + context

        return context

    def build_diff_review_context(
        self, task: Task, diff_summary: str = "", repo_root: str = ""
    ) -> str:
        """Assemble context for the Architect's code-diff review (cycle 57).

        The Architect now reviews the *real* git diff of the working tree
        (the edits the Coder actually applied via ``apply_edits``), scoped to
        ``task.affected_files``. When the tree is clean — e.g. ``local_only_mode``,
        or the edits were rolled back after a failed test run — it falls back to
        the Coder's prose ``diff_summary`` so review never runs context-less.
        """
        context = f"## Task: {task.description}\n\n"

        real_diff = _get_git_diff(repo_root=repo_root, files=task.affected_files or [])
        if real_diff:
            context += "## ACTUAL GIT DIFF\n```diff\n" + real_diff[:8000] + "\n```\n\n"
        else:
            context += (
                "## NO ACTUAL DIFF — working tree clean\n"
                "## (edits not applied or rolled back).\n"
                "## Unverified Coder narration follows —\n"
                "## do not treat as an implemented change:\n\n"
                + (diff_summary or "")
                + "\n\n"
            )

        # Append any NEW files the Coder wrote that are not in affected_files
        # (untracked files are invisible to git diff but the Architect needs to
        # see them to evaluate regression coverage). Only genuinely new files
        # are shown here: a file already tracked at HEAD is a *modification* and
        # is already represented in the git diff above, so including its full
        # body would double-show it and mislabel it as "NEW FILE".
        from pathlib import Path

        test_files_shown = set()
        for f in (task.files_changed or []):
            if f in test_files_shown:
                continue
            if f in (task.affected_files or []):
                continue  # already in the git diff
            if _is_tracked_at_head(repo_root, f):
                continue  # a modified (not new) file — already in the diff
            full_path = Path(repo_root) / f
            if full_path.exists():
                try:
                    test_content = full_path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                    context += (
                        f"## NEW FILE: {f}\n"
                        f"```python\n"
                        f"{test_content[:8000]}\n"
                        f"```\n\n"
                    )
                    test_files_shown.add(f)
                except OSError as e:
                    logger.warning("Could not read new test file %s: %s", f, e)

        context += "## Acceptance Criteria\n\n" + "\n".join(
            f"  - {c}" for c in task.acceptance_criteria
        )
        return context

    def record_implementation_result(self, task: Task, impl_result: dict) -> Task:
        """Persist the Coder's implement() result onto the task (cycle 58).

        ``files_changed``/``test_results``/``test_summary``/``tests_pass`` only
        exist on the in-memory ``impl_result`` dict during ``_run_task``; this
        writes them to the task JSON so they are available later at approval
        time (scoped commit + PR body), which runs in a separate CLI process.
        """
        task.files_changed = list(impl_result.get("files_changed", []) or [])
        task.test_results = impl_result.get("test_results", "") or ""
        task.test_summary = impl_result.get("test_summary", "") or ""
        task.tests_pass = bool(impl_result.get("tests_pass", False))
        self._persist(task)
        return task

    def record_compat_failure(self, task: Task, compat_result: dict) -> Task:
        """Record a Phase 3 backwards-compat FAIL and send the task back (cycle 59).

        REVIEW_PASS cannot transition straight to IN_PROGRESS, so this routes
        through QA_FAIL — the legal send-back state for a verification-layer
        gate. The ``_run_task`` retry then advances QA_FAIL → IN_PROGRESS via
        ``start_implementation`` and re-implements with ``compat_feedback`` in
        the Coder's context.
        """
        feedback = (
            "BACKWARDS COMPATIBILITY CHECK FAILED\n\n"
            + compat_result.get("summary", "")
            + "\n\nIssues:\n"
            + "\n".join(f"- {i}" for i in compat_result.get("issues", []))
        )
        machine = self._machine(task)
        _, audit = machine.transition(
            TaskState.QA_FAIL, actor="qa", reason="backwards-compatibility FAIL"
        )
        task.state = TaskState.QA_FAIL
        task.compat_feedback = feedback
        self._persist(task)
        self._audit_event(audit)
        self._audit(
            task_id=task.task_id,
            agent="qa",
            action="compat_check_failed",
            details={
                "summary": compat_result.get("summary", ""),
                "issues": compat_result.get("issues", []),
            },
        )
        return task

    def submit_for_review(self, task: Task, diff_summary: str) -> Task:
        """Advance from IN_PROGRESS to SUBMITTED after Coder self-review."""
        machine = self._machine(task)
        _, audit = machine.transition(TaskState.SUBMITTED, actor="coder")
        task.state = TaskState.SUBMITTED
        task.latest_diff_summary = diff_summary
        self._persist(task)
        self._audit_event(audit)
        self._audit(
            task_id=task.task_id,
            agent="coder",
            action="submitted_for_review",
            details={"diff_summary": diff_summary},
        )
        return task

    def record_review_verdict(
        self,
        task: Task,
        verdict: str,
        feedback: str = "",
        actor: str = "architect",
    ) -> Task:
        """Record the Architect's code review verdict: APPROVE or REJECT."""
        normalized = verdict.upper()
        if normalized not in {"APPROVE", "REJECT"}:
            raise ValueError(
                f"invalid verdict {verdict!r}; expected APPROVE or REJECT"
            )
        machine = self._machine(task)
        to_state = (
            TaskState.REVIEW_PASS if normalized == "APPROVE"
            else TaskState.REVIEW_FAIL
        )
        _, audit = machine.transition(to_state, actor=actor)
        saved_state = task.state
        saved_feedback = task.review_feedback
        task.state = to_state
        if feedback:
            task.review_feedback = feedback
        try:
            self._persist_with_audit(
                task,
                [
                    self._audit_event_record(audit),
                    self._audit_record(
                        task.task_id,
                        actor,
                        "review_verdict",
                        {"verdict": verdict, "feedback": feedback},
                    ),
                ],
            )
        except Exception:
            task.state = saved_state
            task.review_feedback = saved_feedback
            raise
        return task

    def record_qa_verdict(
        self,
        task: Task,
        verdict: str,
        tdd_assessment: dict[str, Any] | None = None,
        per_skill_verdicts: list[dict] | None = None,
        feedback: str = "",
        actor: str = "qa",
    ) -> Task:
        """Record the QA Agent's verification verdict."""
        normalized = verdict.upper()
        if normalized not in {"PASS", "FAIL"}:
            raise ValueError(
                f"invalid verdict {verdict!r}; expected PASS or FAIL"
            )
        machine = self._machine(task)
        to_state = (
            TaskState.QA_PASS if normalized == "PASS"
            else TaskState.QA_FAIL
        )
        _, audit = machine.transition(to_state, actor=actor)
        saved_state = task.state
        saved_feedback = task.qa_feedback
        task.state = to_state
        if feedback:
            task.qa_feedback = feedback
        try:
            self._persist_with_audit(
                task,
                [
                    self._audit_event_record(audit),
                    self._audit_record(
                        task.task_id,
                        actor,
                        "qa_verdict",
                        {
                            "verdict": verdict,
                            "tdd_assessment": tdd_assessment or {},
                            "per_skill_verdicts": per_skill_verdicts or [],
                            "feedback": feedback,
                        },
                    ),
                ],
            )
        except Exception:
            task.state = saved_state
            task.qa_feedback = saved_feedback
            raise
        return task

    def mark_documentation_updated(
        self, task: Task, docs_updated: list[str] | None = None, actor: str = "documentation"
    ) -> Task:
        """Record documentation updated and advance to DOC_UPDATED."""
        machine = self._machine(task)
        doc_event = machine.mark_doc_updated(actor=actor)
        saved_state = task.state
        saved_doc_updated = task.doc_updated
        task.doc_updated = True
        _, audit = machine.transition(TaskState.DOC_UPDATED, actor=actor)
        task.state = TaskState.DOC_UPDATED
        try:
            self._persist_with_audit(
                task,
                [
                    self._audit_event_record(doc_event),
                    self._audit_event_record(audit),
                    self._audit_record(
                        task.task_id,
                        actor,
                        "documentation_updated",
                        {"docs_updated": docs_updated or []},
                    ),
                ],
            )
        except Exception:
            task.state = saved_state
            task.doc_updated = saved_doc_updated
            raise
        return task

    def create_pr(self, task: Task, pr_url: str | None = None) -> Task:
        """Advance task to PR_CREATED after documentation gate passes."""
        machine = self._machine(task)
        _, audit = machine.transition(TaskState.PR_CREATED, actor="orchestrator")
        saved_state = task.state
        saved_pr_url = task.pr_url
        task.state = TaskState.PR_CREATED
        task.pr_url = pr_url
        try:
            self._persist_with_audit(
                task,
                [
                    self._audit_event_record(audit),
                    self._audit_record(
                        task.task_id,
                        "orchestrator",
                        "pr_created",
                        {"pr_url": pr_url},
                    ),
                ],
            )
        except Exception:
            task.state = saved_state
            task.pr_url = saved_pr_url
            raise
        logger.info("Task %s: PR created (%s)", task.task_id, pr_url or "no URL")
        return task

    def update_pr_metadata(
        self,
        task: Task,
        pr_url: str | None = None,
        pr_number: int | None = None,
    ) -> Task:
        """Persist PR metadata discovered after task creation."""
        saved_pr_url = task.pr_url
        saved_pr_number = task.pr_number
        if pr_url:
            task.pr_url = pr_url
        if pr_number is not None:
            task.pr_number = pr_number
        try:
            self._persist_with_audit(task, [])
        except Exception:
            task.pr_url = saved_pr_url
            task.pr_number = saved_pr_number
            raise
        return task

    def build_pr_review_record(
        self,
        task: Task,
        decision: str,
        comment: str = "",
        actor: str = "human",
    ) -> dict[str, Any]:
        """Build (without persisting) a PR review record for ``atomic_task_write``.

        Mirrors the review dict that ``record_pr_review`` appends, derived from
        the given task snapshot. Used by the atomic PR-approval / rejection paths
        so the review row commits in the same transaction as the state change.
        The atomic write enforces the per-task revision guard, so the freshness
        check that ``record_pr_review`` performs is preserved at commit time.
        """
        return {
            "task_id": task.task_id,
            "plan_id": task.plan_id,
            "decision": decision.upper(),
            "comment": comment,
            "state_at_review": task.state.value,
            "pr_url": task.pr_url,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reviewer": actor,
        }

    def record_pr_review(
        self,
        task: Task,
        decision: str,
        comment: str = "",
        actor: str = "human",
    ) -> None:
        """Record a human PR review decision with optional comment.

        Derives ``state_at_review`` from the current stored task rather than the
        caller's (possibly stale) snapshot, and rejects a stale snapshot with a
        concurrency conflict so a review is never appended with misleading state
        metadata (FIX-REQUIRED-001 / FIX-DESIRED-002).
        """
        stored = self._store.get_task(task.task_id)
        if stored is None:
            raise KeyError(f"Task '{task.task_id}' not found in state store")
        if int(stored.get("revision", 0)) != int(task.revision):
            raise TaskConcurrencyConflictError(
                f"Task {task.task_id} changed since the review snapshot was read "
                f"(snapshot revision={task.revision}, "
                f"stored={stored.get('revision', 0)}); refusing to record a "
                "review with stale state."
            )
        current = Task.from_dict(stored)
        review = {
            "task_id": current.task_id,
            "plan_id": current.plan_id,
            "decision": decision.upper(),
            "comment": comment,
            "state_at_review": current.state.value,
            "pr_url": current.pr_url,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reviewer": actor,
        }
        # Site 2: the review row and its audit entry commit in one atomic
        # read-modify-write so (a) a crash can never leave an orphan review
        # without its audit record and (b) both lists are read INSIDE the
        # lock, closing the lost-update window a read-before-lock would leave
        # open for concurrent reviewers.
        audit_event = self._audit_record(
            current.task_id, actor, "pr_review_decision", review
        )
        self._store.atomic_read_modify_write(
            [
                (
                    "pr_reviews",
                    lambda existing: (
                        existing if isinstance(existing, list) else []
                    )
                    + [review],
                ),
                (
                    "audit_log",
                    lambda existing: (
                        existing if isinstance(existing, list) else []
                    )
                    + [audit_event],
                ),
            ],
            commit=False,
        )

    def reopen_after_pr_rejection(
        self,
        task: Task,
        reason: str,
        actor: str = "human",
    ) -> Task:
        """Move PR_CREATED task back to IN_PROGRESS after human rejection."""
        machine = self._machine(task)
        _, audit = machine.transition(
            TaskState.IN_PROGRESS,
            actor=actor,
            reason="Human rejected PR",
        )
        saved_state = task.state
        saved_comment = task.last_human_review_comment
        task.state = TaskState.IN_PROGRESS
        task.last_human_review_comment = reason
        try:
            self._persist_with_audit(task, [self._audit_event_record(audit)])
        except Exception:
            task.state = saved_state
            task.last_human_review_comment = saved_comment
            raise
        return task

    def reopen_after_local_review(
        self,
        task: Task,
        reason: str,
        actor: str = "human",
    ) -> Task:
        """Reopen task for local review feedback from DOC_UPDATED or PR_CREATED."""
        machine = self._machine(task)
        _, audit = machine.transition(
            TaskState.IN_PROGRESS,
            actor=actor,
            reason="Local reviewer requested changes",
        )
        saved_state = task.state
        saved_comment = task.last_human_review_comment
        task.state = TaskState.IN_PROGRESS
        task.last_human_review_comment = reason
        try:
            self._persist_with_audit(task, [self._audit_event_record(audit)])
        except Exception:
            task.state = saved_state
            task.last_human_review_comment = saved_comment
            raise
        return task

    def record_human_merged(self, task: Task) -> Task:
        """Record that a human has merged the PR."""
        machine = self._machine(task)
        _, audit = machine.transition(TaskState.HUMAN_MERGED, actor="human")
        saved_state = task.state
        task.state = TaskState.HUMAN_MERGED
        try:
            self._persist_with_audit(task, [self._audit_event_record(audit)])
        except Exception:
            task.state = saved_state
            raise
        return task

    def complete_task(self, task: Task) -> Task:
        """Mark a task as COMPLETE."""
        machine = self._machine(task)
        _, audit = machine.transition(TaskState.COMPLETE, actor="orchestrator")
        saved_state = task.state
        task.state = TaskState.COMPLETE
        try:
            self._persist_with_audit(task, [self._audit_event_record(audit)])
        except Exception:
            task.state = saved_state
            raise
        logger.info("Task %s: COMPLETE", task.task_id)
        return task

    def escalate_dispute(self, task: Task, actor: str = "orchestrator") -> Task:
        """Escalate a dispute to human review."""
        machine = self._machine(task)
        _, audit = machine.transition(TaskState.ESCALATED, actor=actor)
        saved_state = task.state
        task.state = TaskState.ESCALATED
        try:
            self._persist_with_audit(task, [self._audit_event_record(audit)])
        except Exception:
            task.state = saved_state
            raise
        logger.warning("Task %s: ESCALATED for human decision", task.task_id)
        return task

    def mark_disputed(self, task: Task, actor: str = "orchestrator") -> Task:
        """Move a task into DISPUTED before potential escalation."""
        machine = self._machine(task)
        _, audit = machine.transition(TaskState.DISPUTED, actor=actor)
        saved_state = task.state
        task.state = TaskState.DISPUTED
        try:
            self._persist_with_audit(task, [self._audit_event_record(audit)])
        except Exception:
            task.state = saved_state
            raise
        logger.warning("Task %s: marked DISPUTED", task.task_id)
        return task

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _machine(self, task: Task) -> TaskStateMachine:
        ctx = TaskContext(
            task_id=task.task_id,
            state=task.state,
            test_stubs_approved=task.test_stubs_approved,
            todo_approved=task.todo_approved,
            doc_updated=task.doc_updated,
            plan_id=task.plan_id,
        )
        return TaskStateMachine(ctx)

    def _persist(self, task: Task) -> None:
        task.updated_at = datetime.now(timezone.utc).isoformat()
        self._store.upsert_task(task.to_dict())

    def _persist_with_audit(
        self,
        task: Task,
        audit_records: list[dict[str, Any]],
        *,
        check_freshness: bool = True,
    ) -> None:
        """Atomically persist a task transition and its audit records.

        Replaces the previous ``_persist(...)`` then ``_audit...`` sequencing so
        a store failure cannot leave a task-state change without its audit
        trail (or vice versa). Routes through the store's single atomic
        task+audit call.

        With ``check_freshness=True`` (default), the caller's snapshot
        ``updated_at`` is passed as an optimistic-concurrency token: if the
        stored task changed since the snapshot was read, the store raises
        ``TaskConcurrencyConflictError`` and nothing is written. Every guarded
        transition helper must persist through this method (FIX-BLOCKING-008 /
        FIX-DESIRED-001).
        """
        expected = task.revision if check_freshness else None
        saved_updated_at = task.updated_at
        saved_revision = task.revision
        task.updated_at = datetime.now(timezone.utc).isoformat()
        task.revision = int(task.revision) + 1
        try:
            self._store.persist_task_with_audit(
                task.to_dict(), audit_records, expected_revision=expected
            )
        except Exception:
            task.updated_at = saved_updated_at
            task.revision = saved_revision
            raise

    def atomic_task_write(
        self,
        task: Task,
        transitions: list[TaskState | tuple[TaskState, str]],
        *,
        actor: str = "orchestrator",
        reason: str | None = None,
        extra_operations: list[tuple[str, Any]] | None = None,
        extra_audit_events: list[dict[str, Any]] | None = None,
        commit: bool = True,
        commit_message: str | None = None,
        check_freshness: bool = True,
    ) -> Task:
        """Apply a transition chain and persist task + audit + extra stores atomically.

        The shared governance transaction primitive. Runs each target in
        ``transitions`` in-memory on a single state machine (gate validation
        included), collecting audit records **without persisting**, then performs
        ONE all-or-nothing write via ``persist_task_with_audit`` — so the task
        state change, its audit trail, and any ``extra_operations`` (other state
        stores such as ``pr_reviews`` / ``disputes``) commit together or roll
        back together under a single lock and one git commit.

        The per-task optimistic-concurrency guard is preserved: with
        ``check_freshness=True`` the caller's snapshot revision is passed as the
        expected revision and a stale snapshot raises
        ``TaskConcurrencyConflictError`` with nothing written. On an invalid
        transition, the state-machine raises before any persist, leaving no
        partial state.

        Args:
            task: The task to transition (mutated in place through the chain).
            transitions: Ordered targets to move through — each a ``TaskState``
                (attributed to ``actor``) or a ``(TaskState, actor)`` pair to
                attribute an individual transition to a different actor.
            actor: Default audit actor for plain-``TaskState`` transitions.
            reason: Optional reason attached to every transition.
            extra_operations: ``(key, value)`` pairs for other state stores to
                commit in the same transaction.
            extra_audit_events: Additional pre-built audit records (e.g. a PR
                review decision) to append in the same atomic write.
            commit / commit_message: Git-commit control for the transaction.
            check_freshness: Enforce the per-task revision guard (default True).

        Returns:
            The task at its final transitioned state.
        """
        expected = task.revision if check_freshness else None
        saved_state = task.state
        saved_updated_at = task.updated_at
        saved_revision = task.revision
        machine = self._machine(task)
        audit_records: list[dict[str, Any]] = []
        try:
            for spec in transitions:
                if isinstance(spec, tuple):
                    target, transition_actor = spec
                else:
                    target, transition_actor = spec, actor
                audit_records.append(
                    self._staged_transition(machine, task, target, transition_actor, reason)
                )
            audit_records.extend(extra_audit_events or [])
            task.updated_at = datetime.now(timezone.utc).isoformat()
            task.revision = int(task.revision) + 1
            self._store.persist_task_with_audit(
                task.to_dict(),
                audit_records,
                expected_revision=expected,
                extra_operations=extra_operations,
                commit=commit,
                commit_message=commit_message,
            )
        except Exception:
            task.state = saved_state
            task.updated_at = saved_updated_at
            task.revision = saved_revision
            raise
        return task

    def _staged_transition(
        self,
        machine: TaskStateMachine,
        task: Task,
        target: TaskState,
        actor: str,
        reason: str | None,
    ) -> dict[str, Any]:
        """Apply one transition in-memory (no persist); return its audit record.

        INTERNAL — only ``atomic_task_write`` may call this. Applying a
        transition outside the atomic persist would bypass the durability and
        per-task concurrency guarantees the governance layer depends on, so this
        is deliberately not public API.
        """
        _, audit = machine.transition(target, actor=actor, reason=reason)
        task.state = target
        return self._audit_event_record(audit)

    def _audit(
        self,
        task_id: str,
        agent: str,
        action: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._store.append_audit(self._audit_record(task_id, agent, action, details))

    @staticmethod
    def _audit_record(
        task_id: str,
        agent: str,
        action: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build (without persisting) a plain audit record."""
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": agent,
            "action": action,
            "task_id": task_id,
            "details": details or {},
        }

    def _audit_event(self, event: dict[str, Any]) -> None:
        self._store.append_audit(self._audit_event_record(event))

    @staticmethod
    def _audit_event_record(event: dict[str, Any]) -> dict[str, Any]:
        """Build (without persisting) a state-transition audit record."""
        return {
            "timestamp": event.get("timestamp", datetime.now(timezone.utc).isoformat()),
            "agent": event.get("actor", "unknown"),
            "action": (
                f"state_transition_{event.get('from_state', '?')}_to_"
                f"{event.get('to_state', event.get('event', '?'))}"
            ),
            "task_id": event.get("task_id", ""),
            "details": event,
        }


def escalate_task(
    task: Task,
    level: str,
    reason: str,
    repo_root: str,
    state_store,
    prior_attempt_summary: str | None = None,
    prior_failing_tests: str | None = None,
    prior_files_attempted: list[str] | None = None,
) -> None:
    """Escalate a failed task to the next executor level (Phase 4).

    ``level`` is ``"codex" | "opus" | "human"``. Builds an ``escalation_packet``
    = the full task spec (``generate_packet``) plus a ``## Prior executor
    context`` section describing what the prior executor tried, which files it
    attempted, and which tests failed — so the fallback executor does not repeat
    the same failed approach. Sets ``task.state`` to ``ESCALATED_TO_<LEVEL>`` and
    persists via ``state_store``. ``repo_root`` is reserved for the future
    worktree-aware escalation path.
    """
    from sentinel.orchestrator.task_packet import generate_packet

    # Resolve and validate the target state BEFORE mutating the task, so an
    # invalid/None level cannot leave the task with a dirty escalation_packet
    # and a half-applied state. (FIX-CRITICAL run-019: validate-before-mutate)
    state_by_level = {
        "codex": TaskState.ESCALATED_TO_CODEX,
        "opus": TaskState.ESCALATED_TO_OPUS,
        "human": TaskState.ESCALATED_TO_HUMAN,
    }
    level_key = level.lower() if isinstance(level, str) else None
    if level_key not in state_by_level:
        raise ValueError(
            f"invalid escalation level {level!r}; expected one of "
            f"{sorted(state_by_level)}"
        )
    target_state = state_by_level[level_key]

    # Capture the optimistic-concurrency token BEFORE any mutation so a
    # concurrent change to the stored task is detected rather than clobbered.
    # (FIX-CRITICAL run-019: missing expected_revision guard)
    expected_revision = task.revision

    packet = generate_packet(task)

    prior_context = (
        f"\n\n## Prior executor context\n\n"
        f"**Escalated to:** {level.upper()}\n"
        f"**Reason:** {reason}\n\n"
        f"The previous executor exhausted its retries without producing a "
        f"passing fix.\n"
    )

    if prior_files_attempted:
        prior_context += (
            "\n**Files the prior executor attempted to edit:**\n"
            + "\n".join(f"- {f}" for f in prior_files_attempted)
            + "\n"
        )

    if prior_attempt_summary:
        prior_context += (
            f"\n**What the prior executor tried:**\n{prior_attempt_summary}\n"
        )

    if prior_failing_tests:
        prior_context += (
            f"\n**Failing tests from last attempt:**\n"
            f"```\n{prior_failing_tests[:3000]}\n```\n"
            f"\nDo not repeat the same approach. Read the failing assertions "
            f"carefully and try a fundamentally different fix.\n"
        )

    prior_state = task.state
    # All validation passed — now mutate the task.
    task.escalation_packet = packet + prior_context
    task.state = target_state
    task.updated_at = datetime.now(timezone.utc).isoformat()
    task.revision = int(task.revision) + 1

    escalation_audit = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "orchestrator",
        "action": (
            f"state_transition_{getattr(prior_state, 'value', prior_state)}_to_"
            f"{task.state.value}"
        ),
        "task_id": task.task_id,
        "details": {
            "event": "task_escalated",
            "level": level_key,
            "reason": reason,
            "from_state": getattr(prior_state, "value", str(prior_state)),
            "to_state": task.state.value,
            "prior_files_attempted": prior_files_attempted or [],
        },
    }

    # Atomically persist the escalation fields and the escalation audit event so
    # an escalated state can never be recorded without its audit trail, and vice
    # versa (FIX-BLOCKING-003), and reject a stale snapshot rather than clobber
    # a concurrent update (FIX-CRITICAL run-019).
    state_store.persist_task_with_audit(
        task.to_dict(), [escalation_audit], expected_revision=expected_revision
    )
