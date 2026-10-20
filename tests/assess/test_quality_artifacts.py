"""Regression tests for P1 logic_bugs in
``sentinel/orchestrator/quality_artifacts.py``.

Covers:
  * conflict counting toward lane agreement (findings 1 & 6)
  * line-coverage combined-count double-counting (finding 2)
  * architectural-hint override ignoring severity (finding 3)
  * unknown-severity short-circuit / strict-path consistency (findings 4 & 5)
"""

from __future__ import annotations

from sentinel.orchestrator.quality_artifacts import (
    _resolve_severity_disagreement,
    build_line_coverage_map,
    build_quality_scorecard,
)


def _coverage_report(**overrides: object) -> dict:
    base = {
        "module_completeness_pct": 100.0,
        "quick_review_success_pct": 100.0,
        "failed_chunk_entries": 0,
    }
    base.update(overrides)
    return base


def _finding(*, severity: str, description: str) -> dict:
    return {
        "module": "src",
        "skill": "layer2a-quick-review",
        "severity": severity,
        "category": "security",
        "location": "src/app.py:10",
        "description": description,
        "suggested_fix": "fix",
        "status": "open",
    }


# ── Findings 1 & 6: resolved severity_disagreement counts as agreement ────


def test_resolved_severity_disagreement_counts_toward_lane_agreement() -> None:
    """A resolved severity_disagreement is a lane agreement, not an unresolved
    conflict. Previously only duplicate_agreement incremented the counter, so a
    resolved severity_disagreement dropped lane_agreement_pct to 0."""
    finding_conflicts = [
        {"class": "duplicate_agreement", "resolved": True, "severity": "P2"},
        {"class": "severity_disagreement", "resolved": True, "severity": "P2"},
    ]

    scorecard = build_quality_scorecard(
        coverage_report=_coverage_report(),
        retrieval_manifest=[],
        finding_conflicts=finding_conflicts,
    )

    # 2 comparable, both resolved → 100%, not 50%.
    assert scorecard["metrics"]["lane_agreement_pct"] == 100.0


def test_unresolved_severity_disagreement_still_lowers_agreement() -> None:
    """An UNresolved severity_disagreement must NOT count as agreement."""
    finding_conflicts = [
        {"class": "duplicate_agreement", "resolved": True, "severity": "P2"},
        {"class": "severity_disagreement", "resolved": False, "severity": "P2"},
    ]

    scorecard = build_quality_scorecard(
        coverage_report=_coverage_report(),
        retrieval_manifest=[],
        finding_conflicts=finding_conflicts,
    )

    # 1 of 2 comparable resolved → 50%.
    assert scorecard["metrics"]["lane_agreement_pct"] == 50.0


def test_resolved_severity_disagreement_not_double_penalised_in_strict_gate() -> None:
    """In strict mode the lane gate requires 100% agreement; a resolved
    severity_disagreement should let the gate pass (it is an agreement)."""
    finding_conflicts = [
        {"class": "severity_disagreement", "resolved": True, "severity": "P2"},
    ]

    scorecard = build_quality_scorecard(
        coverage_report=_coverage_report(),
        retrieval_manifest=[],
        finding_conflicts=finding_conflicts,
        strict_mode=True,
    )

    assert scorecard["metrics"]["lane_agreement_pct"] == 100.0
    assert scorecard["gates"]["lane_conflict_resolution"] == "pass"


# ── Finding 2: build_line_coverage_map must not double-count ───────────────


def test_line_coverage_no_double_count_when_deterministic_full() -> None:
    """When deterministic review covers 100% (non-strict) the combined count is
    total_lines only — reasoning lines are NOT added on top."""
    coverage_map = build_line_coverage_map(
        module_scope_lines={"m": 100},
        deterministic_success={"m": True},
        reasoning_ranges={"m": [(1, 40)]},
        strict_mode=False,
    )

    entry = coverage_map[0]
    assert entry["combined_reviewed_lines"] == 100
    assert entry["combined_reviewed_lines"] <= entry["total_lines"]
    assert entry["coverage_pct"] == 100.0


def test_line_coverage_strict_uses_reasoning_only() -> None:
    """Strict mode ignores the deterministic pass and counts reasoning ranges."""
    coverage_map = build_line_coverage_map(
        module_scope_lines={"m": 100},
        deterministic_success={"m": True},
        reasoning_ranges={"m": [(1, 40)]},
        strict_mode=True,
    )

    entry = coverage_map[0]
    assert entry["combined_reviewed_lines"] == 40
    assert entry["coverage_pct"] == 40.0


def test_line_coverage_non_strict_falls_back_to_reasoning_when_det_fails() -> None:
    coverage_map = build_line_coverage_map(
        module_scope_lines={"m": 100},
        deterministic_success={"m": False},
        reasoning_ranges={"m": [(1, 40)]},
        strict_mode=False,
    )

    entry = coverage_map[0]
    assert entry["combined_reviewed_lines"] == 40


# ── Finding 3: architectural hint must not override a more-severe det ──────


def test_architectural_hint_does_not_override_more_severe_deterministic() -> None:
    """A reasoning finding tagged only by architectural wording must NOT
    override a deterministic Critical when the reasoning severity is lower."""
    deterministic = _finding(severity="Critical", description="Auth bypass")
    reasoning = _finding(
        severity="P3",
        description="Cross-module architecture coupling note",
    )

    kept, resolution, _resolved, _escalated = _resolve_severity_disagreement(
        deterministic, reasoning
    )

    assert kept["severity"] == "Critical"
    assert resolution != "reasoning_precedence"


def test_architectural_hint_overrides_when_at_least_as_severe() -> None:
    """The architectural override still applies when reasoning severity is at
    least as severe as deterministic (preserves existing behaviour)."""
    deterministic = _finding(severity="P2", description="coupling issue")
    reasoning = _finding(
        severity="P1",
        description="Cross-module architecture coupling increases failure radius",
    )

    kept, resolution, _resolved, _escalated = _resolve_severity_disagreement(
        deterministic, reasoning
    )

    assert kept["severity"] == "P1"
    assert resolution == "reasoning_precedence"


# ── Findings 4 & 5: unknown severity must short-circuit to deterministic ───


def test_unknown_reasoning_severity_falls_back_to_deterministic() -> None:
    """An unrecognised reasoning severity must not be silently treated as P3;
    the resolver returns the deterministic finding unchanged."""
    deterministic = _finding(severity="Critical", description="Auth bypass")
    reasoning = _finding(severity="BOGUS", description="odd severity note")

    kept, resolution, resolved, _escalated = _resolve_severity_disagreement(
        deterministic, reasoning
    )

    assert kept is deterministic
    assert kept["severity"] == "Critical"
    assert resolution == "deterministic_precedence_unknown_severity"
    assert resolved is True


def test_unknown_deterministic_severity_falls_back_to_deterministic() -> None:
    """Symmetric: an unknown deterministic severity also short-circuits to the
    deterministic finding rather than trusting a P3 coercion."""
    deterministic = _finding(severity="WEIRD", description="Auth bypass")
    reasoning = _finding(severity="P1", description="clear high severity issue")

    kept, resolution, _resolved, _escalated = _resolve_severity_disagreement(
        deterministic, reasoning
    )

    assert kept is deterministic
    assert resolution == "deterministic_precedence_unknown_severity"
