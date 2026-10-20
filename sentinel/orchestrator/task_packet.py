"""
sentinel/orchestrator/task_packet.py

Task packet generator — Phase 3 of the agentic-executor architecture.

Renders one task into the structured markdown packet an agentic executor (Codex
/ Claude Code subagent) receives: bug description, affected files, scope
boundary, acceptance criteria, the hard gates it must satisfy, and the report
format. Pure formatting — no side effects, stdlib only.
"""
from __future__ import annotations

from typing import Any

_DEFAULT_SCOPE = (
    "Edit ONLY the affected files above plus your test file. No other files."
)

_HARD_GATES = """**Hard gates (all required):**
1. Write a test in tests/ that FAILS on the current buggy code first.
2. Fix the bug, editing ONLY the files listed in Affected files above
   (+ your test file).
3. Run the full suite:
   .venv/Scripts/python.exe -m pytest tests/ -q --no-cov
   Your new test must be green AND zero regressions.
4. Use the agentic loop: read the cited function + its callers, run the tests,
   iterate. Do not one-shot.

Report: SUCCESS/FAIL, files changed, test added, final suite counts,
root cause + fix, iterations."""


def generate_packet(task: Any) -> str:
    """Generate the structured markdown task packet for an agentic executor."""
    affected = task.affected_files or []
    affected_block = (
        "\n".join(f"- {f}" for f in affected) if affected else "- (none listed)"
    )

    criteria = task.acceptance_criteria or []
    criteria_block = (
        "\n".join(f"{i}. {c}" for i, c in enumerate(criteria, start=1))
        if criteria else "(none specified)"
    )

    # Preserve an explicit empty scope_boundary ("" means "no extra scope", a
    # deliberate choice) — only fall back to the default when the boundary is
    # genuinely unset (None or missing attribute). (FIX-DESIRED-003)
    scope_boundary = getattr(task, "scope_boundary", None)
    scope = _DEFAULT_SCOPE if scope_boundary is None else scope_boundary

    return (
        f"## Task: {task.plan_id}\n\n"
        f"**Bug description:**\n{task.description}\n\n"
        f"**Affected files:**\n{affected_block}\n\n"
        f"**Scope boundary:**\n{scope}\n\n"
        f"**Acceptance criteria:**\n{criteria_block}\n\n"
        f"{_HARD_GATES}\n"
    )


def generate_acceptance_check_context(
    task: Any,
    diff_summary: str,
    executor_self_report: str,
) -> str:
    """Build the context for the acceptance-criteria completeness gate: the
    original criteria + the diff + what the executor claimed it did.
    """
    criteria_text = "\n".join(
        f"{i+1}. {c}" for i, c in enumerate(task.acceptance_criteria)
    )
    return (
        f"## Original Acceptance Criteria\n{criteria_text}\n\n"
        f"## Diff Summary\n{diff_summary}\n\n"
        f"## Executor Self-Report\n{executor_self_report}\n"
    )
