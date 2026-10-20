"""
sentinel/phase3/compat_checker.py

Backwards compatibility gate for Sentinel Phase 3.

Runs the Layer 7c (Backwards Compatibility Checker) skill as a mandatory
gate between Architect REVIEW_PASS and QA verification for all Phase 3
tasks. A FAIL verdict returns the task to IN_PROGRESS; the Coding Agent
must resolve all violations before the gate is re-attempted.

This module is intended to be called by ``Orchestrator._run_task`` when
a task is flagged as Phase 3 (is_phase3=True). Phase 2 tasks do not pass
through this gate. As of cycle 24, no production caller wires this gate
into the Phase-3 lifecycle (Phase 3 itself is shelf-code); the module
remains for future Phase-3 activation.
"""

from __future__ import annotations

import logging
from typing import Any

from sentinel.agents.qa import QAAgent
from sentinel.orchestrator.state_store import StateStore
from sentinel.orchestrator.task_manager import Task

log = logging.getLogger(__name__)

# Required keys in a valid Layer 7c response
_REQUIRED_KEYS = frozenset({"verdict", "checks_performed", "violations"})


class CompatibilityViolation(Exception):
    """
    Raised when Layer 7c returns a FAIL verdict.

    Contains the full violation list so the caller (``Orchestrator._run_task``
    when Phase 3 is wired) can feed it back to the Coding Agent for
    remediation.
    """

    def __init__(self, task_id: str, violations: list[dict[str, Any]]) -> None:
        self.task_id = task_id
        self.violations = violations
        msg_lines = [
            f"Backwards compatibility FAIL for task {task_id}:",
        ] + [
            f"  [{v.get('severity', '?')}] {v.get('location', '?')}: {v.get('description', '?')}"
            for v in violations
        ]
        super().__init__("\n".join(msg_lines))


class CompatCheckerError(RuntimeError):
    """Raised when the compat checker pipeline fails unexpectedly."""


class CompatChecker:
    """
    Runs Layer 7c backwards compatibility checking for Phase 3 tasks.

    Intended to be called by ``Orchestrator._run_task`` after Architect
    REVIEW_PASS for tasks where is_phase3=True. The checker is a pure
    gate — it has no retry logic. A FAIL returns control to the caller,
    which re-routes to the Coding Agent for remediation.
    """

    def __init__(self, qa: QAAgent, store: StateStore) -> None:
        self._qa = qa
        self._store = store

    def check(self, task: Task, diff_summary: str) -> dict[str, Any]:
        """
        Run the Layer 7c backwards compatibility check for a Phase 3 task.

        Args:
            task: The task that passed Architect review.
            diff_summary: The Coding Agent's submitted diff.

        Returns:
            The full Layer 7c result dict on PASS.

        Raises:
            CompatibilityViolation: If verdict is FAIL (with violations list).
            CompatCheckerError: If the response is malformed.
        """
        log.info("CompatChecker: running Layer 7c for task %s", task.task_id)

        context = self._build_context(task, diff_summary)
        result = self._qa.check_backwards_compat(context)

        self._validate_result(task.task_id, result)

        verdict = result.get("verdict", "FAIL").upper()
        violations = result.get("violations", [])

        # Persist the compat check record. The verdict is authoritative, so we
        # record the outcome before any blocking exception is raised — the audit
        # trail must exist even when the gate blocks the task.
        self._store.append_to_list("reviews", {
            "task_id": task.task_id,
            "agent": "qa",
            "skill_used": "layer7c-backwards-compatibility-checker",
            "verdict": verdict,
            "violations": violations if isinstance(violations, list) else [],
            "warnings": result.get("warnings", []),
            "cross_repo_impact": result.get("cross_repo_impact", {}),
        })

        # A FAIL verdict is authoritative: it ALWAYS blocks progression,
        # regardless of whether any individual violation is "BREAKING". A FAIL
        # with an empty/missing/malformed violations payload is itself a
        # malformed response and must block rather than be silently accepted.
        if verdict == "FAIL":
            if (
                not isinstance(violations, list)
                or not violations
                or not all(isinstance(v, dict) for v in violations)
            ):
                raise CompatCheckerError(
                    f"Layer 7c returned FAIL for task {task.task_id} but the "
                    f"violations payload is empty or malformed: {violations!r}. "
                    "A FAIL verdict must enumerate at least one violation."
                )
            log.warning(
                "CompatChecker: FAIL for task %s — %d violation(s)",
                task.task_id,
                len(violations),
            )
            raise CompatibilityViolation(
                task_id=task.task_id,
                violations=violations,
            )

        if result.get("warnings"):
            log.info(
                "CompatChecker: PASS with %d warning(s) for task %s",
                len(result["warnings"]),
                task.task_id,
            )

        log.info("CompatChecker: PASS for task %s", task.task_id)
        return result

    def _build_context(self, task: Task, diff_summary: str) -> str:
        """Build the context string for the Layer 7c call."""
        # Load dependency graph for cross-repo impact analysis
        dependencies = self._store.read("dependencies")
        cross_repo_deps = [
            d for d in dependencies
            if d.get("type") == "cross_repo" or float(d.get("coupling_score", 0)) > 0
        ]

        acceptance_criteria = "\n".join(
            f"  {i+1}. {c}" for i, c in enumerate(task.acceptance_criteria)
        )
        affected_files = ", ".join(task.affected_files) or "(none listed)"

        return (
            f"## Backwards Compatibility Check — Task {task.task_id}\n\n"
            f"**FER:** {getattr(task, 'source_fer_id', 'N/A')}\n\n"
            f"**Task description:** {task.description}\n\n"
            f"**Files in scope:** {affected_files}\n\n"
            f"**Acceptance criteria:**\n{acceptance_criteria}\n\n"
            f"---\n\n"
            f"### Diff Summary\n\n{diff_summary}\n\n"
            f"---\n\n"
            f"### Cross-Repository Dependencies (from Phase 1 analysis)\n\n"
            f"The following cross-repo dependencies exist in the codebase. "
            f"Check whether any of the modified files affect these:\n\n"
            + (
                "\n".join(
                    f"  - {d.get('from_module', '?')} → {d.get('to_module', '?')} "
                    f"({d.get('type', '?')})"
                    for d in cross_repo_deps[:20]
                ) or "  (no cross-repo dependencies recorded)"
            )
            + "\n\n---\n\n"
            "Perform the backwards compatibility check per the Layer 7c skill instructions. "
            "Focus specifically on whether existing callers of modified interfaces are still valid."
        )

    def _validate_result(self, task_id: str, result: dict[str, Any]) -> None:
        """Validate the Layer 7c response structure."""
        missing = _REQUIRED_KEYS - set(result.keys())
        if missing:
            raise CompatCheckerError(
                f"Layer 7c response for task {task_id} missing required keys: "
                f"{sorted(missing)}. Got: {sorted(result.keys())}"
            )

        verdict = result.get("verdict", "").upper()
        if verdict not in {"PASS", "FAIL"}:
            raise CompatCheckerError(
                f"Layer 7c returned invalid verdict '{verdict}' for task {task_id}. "
                "Must be PASS or FAIL."
            )
