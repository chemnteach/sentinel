"""
sentinel/orchestrator/todo_schema.py

TODO submission schema and validator for the Sentinel governance system.

The Coding Agent must submit a structured TODO before writing any code.
A valid TODO contains two mandatory sections:
  - implementation_plan: what will be changed and how
  - test_stubs: runnable test stubs that will be implemented before the code

This module validates TODO submissions before they reach the Architect.
Invalid TODOs are rejected immediately with a structured error; they never
reach the review queue.
"""

from __future__ import annotations

import json
import re as _re
import textwrap
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Validation error types
# ---------------------------------------------------------------------------

class TodoValidationError(ValueError):
    """Raised when a TODO submission fails schema validation."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        bullet_list = "\n".join(f"  • {e}" for e in errors)
        super().__init__(
            f"TODO submission rejected — {len(errors)} validation error(s):\n{bullet_list}"
        )


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TestStub:
    """A single test stub submitted as part of a TODO."""

    name: str
    """Test function name. Must start with 'test_' and describe behaviour."""

    acceptance_criterion: str
    """Exact acceptance criterion from the task that this stub verifies."""

    scenario: str
    """Human-readable description of this specific test scenario."""

    given: str
    """Preconditions for the test."""

    when: str
    """The action being tested."""

    then: str
    """The expected observable outcome."""

    stub_code: str
    """
    Runnable Python stub. Must be a valid function definition that raises
    NotImplementedError. Will be replaced with a real implementation after
    TODO approval.
    """

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TestStub":
        required = {"name", "acceptance_criterion", "scenario", "given", "when", "then", "stub_code"}
        missing = required - set(data.keys())
        if missing:
            raise TodoValidationError([f"Test stub missing required field(s): {', '.join(sorted(missing))}"])
        # Every stub field must be a string. A JSON null (or any non-string)
        # would otherwise be accepted here and later crash the validators that
        # call .startswith()/.strip() with an AttributeError instead of a clean
        # validation error.
        non_string = sorted(k for k in required if not isinstance(data[k], str))
        if non_string:
            raise TodoValidationError(
                [f"Test stub field(s) must be strings: {', '.join(non_string)}"]
            )
        return cls(**{k: data[k] for k in required})


@dataclass
class OutOfScopeFinding:
    """An issue noticed during implementation that is outside task scope."""

    file: str
    description: str
    suggested_severity: str  # Critical | P1 | P2 | P3

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OutOfScopeFinding":
        return cls(
            file=data.get("file", ""),
            description=data.get("description", ""),
            suggested_severity=data.get("suggested_severity", "P3"),
        )


@dataclass
class ImplementationPlan:
    """The implementation section of a TODO submission."""

    summary: str
    """One-sentence description of the change."""

    files_to_modify: list[str]
    """Explicit list of files that will be modified."""

    approach: str
    """Detailed description of the implementation approach."""

    edge_cases: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImplementationPlan":
        required = {"summary", "files_to_modify", "approach"}
        missing = required - set(data.keys())
        if missing:
            raise TodoValidationError(
                [f"Implementation plan missing required field(s): {', '.join(sorted(missing))}"]
            )
        return cls(
            summary=data["summary"],
            files_to_modify=data["files_to_modify"],
            approach=data["approach"],
            edge_cases=data.get("edge_cases", []),
            risks=data.get("risks", []),
            dependencies=data.get("dependencies", []),
        )


@dataclass
class TestStubSection:
    """The test stubs section of a TODO submission."""

    test_file: str
    """Path to the test file where stubs will be written."""

    stubs: list[TestStub]
    """One or more test stubs. Must not be empty."""

    coverage_summary: str
    """Description of which acceptance criteria are covered."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TestStubSection":
        required = {"test_file", "stubs", "coverage_summary"}
        missing = required - set(data.keys())
        if missing:
            raise TodoValidationError(
                [f"Test stubs section missing required field(s): {', '.join(sorted(missing))}"]
            )
        stubs = [TestStub.from_dict(s) for s in data["stubs"]]
        if not stubs:
            # The contract requires at least one stub (TDD gate); an empty
            # stubs list is a rejection, not a silently-accepted TODO.
            # (FIX-DESIRED-019)
            raise TodoValidationError(
                ["Test stubs section must contain at least one test stub"]
            )
        return cls(
            test_file=data["test_file"],
            stubs=stubs,
            coverage_summary=data["coverage_summary"],
        )


@dataclass
class TodoSubmission:
    """
    A complete TODO submission from the Coding Agent.

    Both implementation_plan and test_stubs are mandatory. A TODO without
    test stubs is rejected before it reaches the Architect.
    """

    task_id: str
    implementation_plan: ImplementationPlan
    test_stubs: TestStubSection
    out_of_scope_findings: list[OutOfScopeFinding] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TodoSubmission":
        # Top-level type guard: a valid JSON payload can decode to a non-object
        # (list, string, number, null). Reject those with a structured
        # TodoValidationError instead of letting membership/indexing on a
        # non-mapping raise a bare TypeError/AttributeError deeper down.
        if not isinstance(data, dict):
            raise TodoValidationError(
                [
                    "TODO submission must be a JSON object (mapping); "
                    f"got {type(data).__name__}."
                ]
            )

        errors: list[str] = []

        if "implementation_plan" in data and not isinstance(data["implementation_plan"], dict):
            raise TodoValidationError(["implementation_plan must be a dict"])
        if "test_stubs" in data and not isinstance(data["test_stubs"], dict):
            raise TodoValidationError(["test_stubs must be a dict"])

        if "task_id" not in data or not data["task_id"]:
            errors.append("task_id is required and must not be empty")

        impl_plan = None
        if "implementation_plan" not in data:
            errors.append(
                "implementation_plan is required. "
                "A TODO without an implementation plan will not be reviewed."
            )
        else:
            try:
                impl_plan = ImplementationPlan.from_dict(data["implementation_plan"])
            except TodoValidationError as exc:
                errors.extend(exc.errors)

        test_stubs = None
        if "test_stubs" not in data:
            errors.append(
                "test_stubs is required. "
                "Sentinel enforces test-driven development: test stubs must be "
                "submitted and approved before any implementation begins. "
                "A TODO without test stubs will never reach the Architect."
            )
        else:
            try:
                test_stubs = TestStubSection.from_dict(data["test_stubs"])
            except TodoValidationError as exc:
                errors.extend(exc.errors)

        if errors:
            raise TodoValidationError(errors)

        findings = [
            OutOfScopeFinding.from_dict(f)
            for f in data.get("out_of_scope_findings", [])
        ]

        return cls(
            task_id=data["task_id"],
            implementation_plan=impl_plan,  # type: ignore[arg-type]
            test_stubs=test_stubs,  # type: ignore[arg-type]
            out_of_scope_findings=findings,
        )

    @classmethod
    def from_json(cls, json_str: str) -> "TodoSubmission":
        """Parse and validate a TODO submission from a JSON string."""
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise TodoValidationError([f"TODO submission is not valid JSON: {exc}"]) from exc
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "implementation_plan": {
                "summary": self.implementation_plan.summary,
                "files_to_modify": self.implementation_plan.files_to_modify,
                "approach": self.implementation_plan.approach,
                "edge_cases": self.implementation_plan.edge_cases,
                "risks": self.implementation_plan.risks,
                "dependencies": self.implementation_plan.dependencies,
            },
            "test_stubs": {
                "test_file": self.test_stubs.test_file,
                "stubs": [
                    {
                        "name": s.name,
                        "acceptance_criterion": s.acceptance_criterion,
                        "scenario": s.scenario,
                        "given": s.given,
                        "when": s.when,
                        "then": s.then,
                        "stub_code": s.stub_code,
                    }
                    for s in self.test_stubs.stubs
                ],
                "coverage_summary": self.test_stubs.coverage_summary,
            },
            "out_of_scope_findings": [
                {
                    "file": f.file,
                    "description": f.description,
                    "suggested_severity": f.suggested_severity,
                }
                for f in self.out_of_scope_findings
            ],
        }


# ---------------------------------------------------------------------------
# Validation rules
# ---------------------------------------------------------------------------

def _validate_stub_names(stubs: list[TestStub]) -> list[str]:
    """Stubs must have function names that start with 'test_'."""
    errors = []
    for stub in stubs:
        if not stub.name.startswith("test_"):
            errors.append(
                f"Test stub name '{stub.name}' must start with 'test_'. "
                "Test names should describe the behaviour being verified, "
                "e.g. 'test_raises_ValueError_when_input_is_negative'."
            )
    return errors


def _validate_stub_code(stubs: list[TestStub]) -> list[str]:
    """Stubs must contain 'NotImplementedError' — they are placeholders, not implementations."""
    errors = []
    for stub in stubs:
        if "NotImplementedError" not in stub.stub_code:
            errors.append(
                f"Test stub '{stub.name}' stub_code must raise NotImplementedError. "
                "TODO stubs are placeholders — the implementation comes after Architect approval. "
                "If you have already implemented the test, submit the full implementation "
                "for post-approval submission, not in the TODO."
            )
    return errors


_COVERAGE_STOP_WORDS: frozenset[str] = frozenset({
    "the", "and", "for", "not", "are", "its",
    "that", "this", "with", "from", "when",
    "each", "all", "any", "was", "has", "have",
    "been", "will", "must", "can", "may", "does",
})


def _significant_tokens(text: str) -> frozenset[str]:
    """Extract significant lowercase tokens from text."""
    # Also split on hyphen/underscore/slash so compound terms like
    # "error-handling", "user_id", and "foo/bar" break into their component
    # tokens instead of staying single tokens that corrupt the overlap check.
    words = _re.sub(r"[`'\"\.,;:!?()\[\]{}\-_/]", " ", text).lower().split()
    return frozenset(
        w for w in words
        if len(w) > 2 and w not in _COVERAGE_STOP_WORDS
    )


_COVERAGE_OVERLAP_THRESHOLD = 0.6
# Fraction of a criterion's tokens that may be absent from a covering stub.
# floor(fraction * len) yields 0 allowed-missing for very short criteria
# (forcing near-complete overlap) and grows for longer criteria (tolerating
# paraphrase). Paired with the 0.6 fraction floor above.
_COVERAGE_MISSING_FRACTION = 0.3


def _validate_acceptance_criteria_coverage(
    stubs: list[TestStub],
    acceptance_criteria: list[str],
) -> list[str]:
    """Every acceptance criterion must be referenced
    by at least one stub.

    A stub covers a criterion when either:
    (a) the criterion text is a verbatim
        case-insensitive substring of the stub's
        acceptance_criterion field (original
        behaviour, fully preserved), or
    (b) at least _COVERAGE_OVERLAP_THRESHOLD of
        the criterion's significant tokens appear
        in the stub's acceptance_criterion field
        (tolerates paraphrase/reordering).
    """
    if not acceptance_criteria:
        return []

    errors = []
    for criterion in acceptance_criteria:
        crit_lower = criterion.strip().lower()
        crit_tokens = _significant_tokens(criterion)

        # Length-normalise the coverage bar so short criteria are not trivially
        # "covered". A flat 0.6 fraction lets a 3-token criterion pass on 2
        # tokens (67%). Instead, cap the number of *missing* tokens allowed as a
        # function of length: short criteria may miss (almost) none, while long
        # criteria tolerate more paraphrase/omission. The 0.6 fraction remains a
        # secondary floor so a long criterion still needs a majority of tokens.
        if crit_tokens:
            n = len(crit_tokens)
            allowed_missing = int(_COVERAGE_MISSING_FRACTION * n)
            min_matched_floor = _COVERAGE_OVERLAP_THRESHOLD * n

        covered = False
        for stub in stubs:
            stub_lower = stub.acceptance_criterion.strip().lower()
            # (a) verbatim substring — original rule
            if crit_lower in stub_lower:
                covered = True
                break
            # (b) token-overlap fallback
            if crit_tokens:
                stub_tokens = _significant_tokens(
                    stub.acceptance_criterion
                )
                matched = len(crit_tokens & stub_tokens)
                if (n - matched) <= allowed_missing and matched >= min_matched_floor:
                    covered = True
                    break

        if not covered:
            errors.append(
                f"Acceptance criterion not covered by "
                f"any test stub: '{criterion}'. "
                "Every acceptance criterion must have "
                "at least one corresponding test stub."
            )
    return errors


def _validate_no_empty_fields(stubs: list[TestStub]) -> list[str]:
    """All required stub fields must be non-empty."""
    errors = []
    for stub in stubs:
        for field_name in ("scenario", "given", "when", "then", "acceptance_criterion"):
            value = getattr(stub, field_name, "").strip()
            if not value:
                errors.append(
                    f"Test stub '{stub.name}' has empty '{field_name}' field. "
                    "All stub fields are required to give the Architect enough context "
                    "to evaluate whether the test correctly specifies the contract."
                )
    return errors


def validate_todo(
    todo: TodoSubmission,
    acceptance_criteria: list[str] | None = None,
) -> list[str]:
    """
    Run all validation rules against a parsed TodoSubmission.

    Returns a list of error strings. An empty list means the TODO is valid.
    Does not raise — callers decide how to handle errors.

    Args:
        todo: Parsed TodoSubmission (already passed from_dict validation).
        acceptance_criteria: Optional list of acceptance criteria from the task.
            When provided, validates that every criterion is covered by at least
            one test stub.
    """
    errors: list[str] = []

    stubs = todo.test_stubs.stubs

    if not stubs:
        errors.append(
            "test_stubs.stubs must contain at least one stub. "
            "Test-driven development requires at least one test to be specified "
            "before implementation begins."
        )
        return errors  # No point running further checks with no stubs

    errors.extend(_validate_stub_names(stubs))
    errors.extend(_validate_stub_code(stubs))
    errors.extend(_validate_no_empty_fields(stubs))

    if acceptance_criteria:
        errors.extend(_validate_acceptance_criteria_coverage(stubs, acceptance_criteria))

    return errors


def parse_and_validate_todo(
    json_str: str,
    acceptance_criteria: list[str] | None = None,
) -> TodoSubmission:
    """
    Parse a JSON string into a TodoSubmission and run full validation.

    Raises TodoValidationError if the submission is invalid. This is the
    primary entry point for the Orchestrator.

    Args:
        json_str: Raw JSON string from the Coding Agent.
        acceptance_criteria: Optional list of acceptance criteria from the task,
            used to verify stub coverage.

    Returns:
        Validated TodoSubmission ready to be stored and sent to the Architect.

    Raises:
        TodoValidationError: If the submission fails any validation check.
    """
    todo = TodoSubmission.from_json(json_str)
    errors = validate_todo(todo, acceptance_criteria=acceptance_criteria)
    if errors:
        raise TodoValidationError(errors)
    return todo


def _fence_safe(value: str) -> str:
    """Neutralize prompt-injection via markdown code fences.

    The fields interpolated into the Architect review prompt come from the
    Coding Agent's (untrusted) response. A triple-backtick sequence would
    terminate the surrounding code-block context and let the Coder inject
    free-form instructions that override the Architect's review behaviour
    (prompt injection, CWE-77). Replacing ``` with ~~~ removes that capability
    while preserving readability.
    """
    return str(value).replace("```", "~~~")


def format_todo_for_architect(todo: TodoSubmission) -> str:
    """
    Format a validated TodoSubmission as a human-readable context block
    for injection into the Architect's review prompt.

    The Architect sees:
    - Task ID
    - Implementation plan (summary, files, approach, edge cases, risks)
    - Test stubs (as runnable code with full Given/When/Then context)
    - Out-of-scope findings (for awareness, not action)

    All Coder-supplied string fields are passed through ``_fence_safe`` so a
    malicious submission cannot break out of the code-block context and inject
    instructions into the Architect's prompt.
    """
    lines = [
        f"## TODO Submission — Task {todo.task_id}",
        "",
        "### Implementation Plan",
        f"**Summary:** {_fence_safe(todo.implementation_plan.summary)}",
        "",
        f"**Files to modify:**",
    ]

    for f in todo.implementation_plan.files_to_modify:
        lines.append(f"  - {f}")

    lines += [
        "",
        f"**Approach:**",
        textwrap.indent(_fence_safe(todo.implementation_plan.approach), "  "),
        "",
    ]

    if todo.implementation_plan.edge_cases:
        lines.append("**Edge cases identified:**")
        for ec in todo.implementation_plan.edge_cases:
            lines.append(f"  - {_fence_safe(ec)}")
        lines.append("")

    if todo.implementation_plan.risks:
        lines.append("**Risks:**")
        for r in todo.implementation_plan.risks:
            lines.append(f"  - {_fence_safe(r)}")
        lines.append("")

    lines += [
        "---",
        "",
        "### Test Stubs",
        f"**Test file:** `{todo.test_stubs.test_file}`",
        f"**Coverage summary:** {_fence_safe(todo.test_stubs.coverage_summary)}",
        "",
    ]

    for stub in todo.test_stubs.stubs:
        lines += [
            f"#### `{stub.name}`",
            f"**Acceptance criterion:** {_fence_safe(stub.acceptance_criterion)}",
            f"**Scenario:** {_fence_safe(stub.scenario)}",
            f"- Given: {_fence_safe(stub.given)}",
            f"- When: {_fence_safe(stub.when)}",
            f"- Then: {_fence_safe(stub.then)}",
            "",
            "```python",
            _fence_safe(stub.stub_code),
            "```",
            "",
        ]

    if todo.out_of_scope_findings:
        lines += [
            "---",
            "",
            "### Out-of-Scope Findings (for awareness — not actioned in this task)",
            "",
        ]
        for finding in todo.out_of_scope_findings:
            lines += [
                f"- **{finding.file}** [{finding.suggested_severity}]: {_fence_safe(finding.description)}",
            ]

    return "\n".join(lines)
