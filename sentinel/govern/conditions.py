"""Structured triage conditions — validation, merge, and enforcement helpers.

Decision Brief exports carry ``conditions_structured`` (a list of typed
constraint items) alongside the backward-compatible ``conditions`` string.
This module validates that payload at the ``sentinel triage --batch`` boundary
and provides the pure helpers the remediation pipeline uses to turn those
constraints into plan/QA/scope enforcement (cycle 52).
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, field_validator, model_validator

log = logging.getLogger(__name__)

CONDITION_TYPES = Literal[
    "REQUIRE_CHANGE",
    "EXCLUDE_CHANGE",
    "REQUIRE_CRITERION",
    "ADD_CRITERION",
    "SCOPE_LIMIT",
    "APPROACH_DIRECTIVE",
]

DECISION_TYPES = Literal[
    "APPROVE",
    "APPROVE_WITH_CONDITIONS",
    "DEFER",
    "EXCLUDE",
]


class ConditionItem(BaseModel):
    """One structured constraint. ``target`` is positional (``change_0``) or a
    comma-joined file list (SCOPE_LIMIT); ``note`` carries the resolved text."""

    type: CONDITION_TYPES
    target: str | None = None
    note: str | None = None

    @field_validator("note")
    @classmethod
    def _note_max_length(cls, v: str | None) -> str | None:
        if v and len(v) > 500:
            return v[:500]
        return v

    @field_validator("target")
    @classmethod
    def _target_max_length(cls, v: str | None) -> str | None:
        if v and len(v) > 200:
            return v[:200]
        return v


class FindingDecision(BaseModel):
    """One per-finding triage decision from a Decision Brief export.

    ``finding_id`` is optional for backward compatibility with the original
    (cycle-44) export shape ``{td_id, decision, conditions, rationale}``.
    """

    finding_id: str | None = None
    td_id: str | None = None
    decision: DECISION_TYPES
    conditions: str | None = None
    conditions_structured: list[ConditionItem] | None = None
    rationale: str | None = None
    conflict: bool = False

    @model_validator(mode="after")
    def _conditions_required_when_awc(self) -> "FindingDecision":
        if self.decision == "APPROVE_WITH_CONDITIONS" and not self.conditions_structured:
            raise ValueError(
                "conditions_structured required for APPROVE_WITH_CONDITIONS"
            )
        return self


class DecisionBatch(BaseModel):
    """A validated batch of finding decisions."""

    decisions: list[FindingDecision]

    @classmethod
    def from_list(cls, data: list[dict]) -> "DecisionBatch":
        return cls(decisions=[FindingDecision(**d) for d in data])


# ---------------------------------------------------------------------------
# Threading helpers (cycle 52 C3) — pure; used by the orchestrator to attach
# conditions to tasks via the pattern → group → findings lineage.
# ---------------------------------------------------------------------------


def get_task_finding_ids(
    task,
    pattern_to_group: dict[str, str],
    group_members: dict[str, list[str]],
) -> list[str]:
    """Resolve the finding_ids a task constrains, via its pattern lineage:
    ``task.pattern_id`` → pattern's ``group_id`` → the group's
    ``member_finding_ids``. Returns [] when the task has no single-pattern
    lineage (pattern_id empty) or the pattern/group is unknown.

    Accepts either a ``Task`` object (``.pattern_id``) or a plain dict.
    """
    pid = getattr(task, "pattern_id", None)
    if pid is None and isinstance(task, dict):
        pid = task.get("pattern_id")
    pid = pid or ""
    if not pid:
        return []
    gid = pattern_to_group.get(pid)
    if not gid:
        return []
    return list(group_members.get(gid, []))


def get_effective_criteria(task) -> list[str]:
    """Acceptance criteria for a task including any added by REQUIRE_CRITERION /
    ADD_CRITERION conditions (cycle 52). Accepts a Task object or a dict; the
    added criterion text is appended only when not already present.
    """
    if isinstance(task, dict):
        base = list(task.get("acceptance_criteria", []) or [])
        conditions = task.get("conditions_structured") or []
    else:
        base = list(getattr(task, "acceptance_criteria", []) or [])
        conditions = getattr(task, "conditions_structured", None) or []
    for c in conditions:
        ctype = c.get("type") if isinstance(c, dict) else getattr(c, "type", None)
        if ctype in ("REQUIRE_CRITERION", "ADD_CRITERION"):
            note = c.get("note", "") if isinstance(c, dict) else getattr(c, "note", "")
            if note and note not in base:
                base.append(note)
    return base


def _normalize_path(p: str) -> str:
    """Normalize a file path for scope comparison — forward slashes, lowercase,
    leading ``./`` stripped (cycle 55). Lets a reviewer's ``assess/reviewer.py``
    match a git/absolute ``sentinel/assess/reviewer.py`` or
    ``D:/Git/Sentinel/sentinel/assess/reviewer.py`` or backslash form."""
    return p.replace("\\", "/").lstrip("./").lower()


def _canonicalize_scope_limit_path(p: str) -> str:
    """Return a stable canonical path form for SCOPE_LIMIT merge results.

    Uses the same normalization basis as ``check_scope_limit`` and prefers the
    shortest equivalent suffix so repo-prefixed and relative forms collapse to
    one shared representation.
    """
    normalized = _normalize_path(p)
    parts = [part for part in normalized.split("/") if part]
    if not parts:
        return normalized
    if len(parts) >= 2 and parts[0] == "sentinel":
        return "/".join(parts[1:])
    return "/".join(parts)


def check_scope_limit(
    conditions: list[dict] | None,
    changed_files: list[str],
) -> list[str]:
    """Check whether ``changed_files`` respects any SCOPE_LIMIT conditions
    (cycle 52). Returns a list of violation messages (empty if none / no scope).
    A changed file is in scope if it suffix-matches any allowed path in either
    direction, after path normalization (cycle 55).
    """
    if not conditions:
        return []
    scope_limits = [
        c.get("target", "")
        for c in conditions
        if c.get("type") == "SCOPE_LIMIT" and c.get("target")
    ]
    if not scope_limits:
        return []

    allowed: set[str] = set()
    for limit in scope_limits:
        for f in str(limit).split(","):
            f = f.strip()
            if f:
                allowed.add(f)
    allowed_norm = {_normalize_path(a) for a in allowed}

    violations: list[str] = []
    for f in changed_files:
        fn = _normalize_path(f)
        if not any(fn.endswith(a) or a.endswith(fn) for a in allowed_norm):
            violations.append(
                f"File '{f}' is outside the approved scope: {', '.join(sorted(allowed))}"
            )
    return violations


def get_changed_files_from_git(
    repo_path: str,
    base_ref: str = "HEAD~1",
    head_ref: str = "HEAD",
) -> list[str]:
    """Files changed between two git refs (``git diff --name-only base head``),
    as repo-relative paths (cycle 55). Returns [] if git is unavailable, the refs
    don't exist (e.g. a repo with a single commit), or anything fails — never
    raises. A reliable, structured source for SCOPE_LIMIT enforcement vs parsing
    prose diff text.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "diff", "--name-only", base_ref, head_ref],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if result.returncode != 0:
            # A non-zero exit is often benign here (missing ref on a single-commit
            # repo) but can also mean git is broken or the path is wrong. Callers
            # tolerate an empty list, so we still return [] — but surface the
            # failure so an empty scope-enforcement set is never silent.
            log.warning(
                "git diff for changed files failed (rc=%s) in %s (%s..%s): %s",
                result.returncode,
                repo_path,
                base_ref,
                head_ref,
                (result.stderr or "").strip(),
            )
            return []
        return [f.strip() for f in result.stdout.splitlines() if f.strip()]
    except Exception:
        # git missing, timeout, or any unexpected failure. Return [] (callers
        # tolerate empty) but log at ERROR so the failure is visible rather than
        # masquerading as "no files changed".
        log.error(
            "Failed to read changed files from git in %s (%s..%s)",
            repo_path,
            base_ref,
            head_ref,
            exc_info=True,
        )
        return []


def format_constraints_for_architect(conditions: list[dict | ConditionItem] | None) -> str:
    """Format ``conditions_structured`` as a plain-English constraints block for
    the Architect's review context (cycle 52). Returns "" when no conditions."""
    if not conditions:
        return ""

    lines = [
        "REMEDIATION CONSTRAINTS",
        "The following constraints were set by the code owner and must be "
        "respected in the plan:",
        "",
    ]
    for c in conditions:
        ctype = c.get("type", "") if isinstance(c, dict) else getattr(c, "type", "")
        note = (c.get("note", "") if isinstance(c, dict) else getattr(c, "note", "")) or ""
        target = (c.get("target", "") if isinstance(c, dict) else getattr(c, "target", "")) or ""
        if ctype == "REQUIRE_CHANGE":
            lines.append(f"- REQUIRED: Include this change in the plan: {note or target}")
        elif ctype == "EXCLUDE_CHANGE":
            lines.append(f"- EXCLUDED: Do NOT implement this change: {note or target}")
        elif ctype == "REQUIRE_CRITERION":
            lines.append(f"- REQUIRED CRITERION: The QA gate must verify: {note}")
        elif ctype == "ADD_CRITERION":
            lines.append(f"- ADDITIONAL CRITERION: Add this to the acceptance criteria: {note}")
        elif ctype == "SCOPE_LIMIT":
            lines.append(f"- SCOPE LIMIT: Edits must be restricted to: {target}")
        elif ctype == "APPROACH_DIRECTIVE":
            lines.append(f"- DIRECTIVE: {note}")
    return "\n".join(lines)


def merge_conditions(condition_lists: list[list[dict] | None]) -> list[dict] | None:
    """Merge several findings' ``conditions_structured`` into one constraint set
    for an aggregated task. Rules: EXCLUDE_CHANGE wins over REQUIRE_CHANGE on the
    same target; criteria and directives are unioned (deduped); SCOPE_LIMIT is the
    intersection of the file sets (most restrictive). Returns None when no input
    list carries conditions.
    """
    lists = [cl for cl in condition_lists if cl]
    if not lists:
        return None
    if len(lists) == 1:
        return [dict(it) for it in lists[0]]

    change_by_target: dict[str, dict] = {}
    criteria_by_key: dict[tuple, dict] = {}
    directive_by_note: dict[str, dict] = {}
    scope_sets: list[set[str]] = []
    for cl in lists:
        this_scope: set[str] | None = None
        for it in cl:
            t = it.get("type")
            if t in ("REQUIRE_CHANGE", "EXCLUDE_CHANGE"):
                tgt = it.get("target")
                prev = change_by_target.get(tgt)
                if prev is None or t == "EXCLUDE_CHANGE":
                    change_by_target[tgt] = dict(it)
            elif t in ("REQUIRE_CRITERION", "ADD_CRITERION"):
                criteria_by_key[(t, it.get("note"))] = dict(it)
            elif t == "APPROACH_DIRECTIVE":
                directive_by_note[str(it.get("note"))] = dict(it)
            elif t == "SCOPE_LIMIT":
                files = {
                    _canonicalize_scope_limit_path(f.strip())
                    for f in str(it.get("target") or "").split(",")
                    if f.strip()
                }
                this_scope = files if this_scope is None else (this_scope | files)
        if this_scope is not None:
            scope_sets.append(this_scope)

    merged: list[dict] = (
        list(change_by_target.values())
        + list(criteria_by_key.values())
        + list(directive_by_note.values())
    )
    if scope_sets:
        inter = set(scope_sets[0])
        for s in scope_sets[1:]:
            inter = inter & s
        if inter:
            merged.append(
                {
                    "type": "SCOPE_LIMIT",
                    "target": ",".join(sorted(_canonicalize_scope_limit_path(p) for p in inter)),
                    "note": None,
                }
            )
        else:
            # At least one input carried a SCOPE_LIMIT but the intersection is
            # empty (disjoint file sets). Omitting SCOPE_LIMIT here would mean
            # "no scope restriction" — the *least* restrictive result, the exact
            # opposite of the most-restrictive merge rule. Emit an explicit
            # empty scope ("none") so downstream enforcement treats the merged
            # task as scoped-to-nothing rather than scoped-to-anything.
            merged.append(
                {
                    "type": "SCOPE_LIMIT",
                    "target": "none",
                    "note": (
                        "Empty intersection of disjoint SCOPE_LIMITs; "
                        "most-restrictive merge yields no permitted scope."
                    ),
                }
            )
    return merged


def review_result_supports_approval(review_result: dict[str, Any] | None) -> bool:
    """Return True only for review results with explicit positive verification.

    Approval-consuming paths must reject local-only fallback outcomes that are
    unsupported, skipped, or otherwise lack affirmative verification metadata.
    """
    if not isinstance(review_result, dict):
        return False

    outcome = str(review_result.get("outcome") or "").strip().lower()
    status = str(review_result.get("status") or "").strip().lower()
    supported = review_result.get("supported")
    verified = review_result.get("verified")
    approved = review_result.get("approved")

    if outcome in {"unsupported", "skipped"}:
        return False
    if status in {"unsupported", "skipped"}:
        return False
    if supported is not True:
        return False
    if verified is not True:
        return False
    if approved is not True:
        return False
    return True
