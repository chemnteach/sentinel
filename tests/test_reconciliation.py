from __future__ import annotations

from sentinel.orchestrator.quality_artifacts import reconcile_findings


def _finding(*, lane: str, severity: str, description: str, location: str = "src/app.py:10") -> dict:
    return {
        "module": "src",
        "skill": "deterministic-lane" if lane == "deterministic" else "layer2a-quick-review",
        "severity": severity,
        "category": "security",
        "location": location,
        "description": description,
        "suggested_fix": "fix",
        "status": "open",
    }


def test_reconciliation_classifies_duplicate_agreement() -> None:
    deterministic = [_finding(lane="deterministic", severity="P2", description="same issue")]
    reasoning = [_finding(lane="reasoning", severity="P2", description="same issue")]

    resolved, conflicts, report = reconcile_findings(deterministic, reasoning)

    assert len(resolved) == 1
    assert len(conflicts) == 1
    assert conflicts[0]["class"] == "duplicate_agreement"
    assert conflicts[0]["resolved"] is True
    assert report["summary"]["resolved_count"] == 1


def test_reconciliation_uses_deterministic_precedence_for_policy_conflicts() -> None:
    deterministic = [
        _finding(
            lane="deterministic",
            severity="P1",
            description="XML policy violation: insecure flag enabled",
        )
    ]
    reasoning = [
        _finding(
            lane="reasoning",
            severity="P3",
            description="XML policy violation: insecure flag enabled",
        )
    ]

    resolved, conflicts, _ = reconcile_findings(deterministic, reasoning)

    assert resolved[0]["severity"] == "P1"
    assert conflicts[0]["resolution"] == "deterministic_precedence"


def test_reconciliation_uses_reasoning_precedence_for_architectural_conflicts() -> None:
    deterministic = [
        _finding(
            lane="deterministic",
            severity="P2",
            description="Cross-module architecture coupling increases failure radius",
        )
    ]
    reasoning = [
        _finding(
            lane="reasoning",
            severity="P1",
            description="Cross-module architecture coupling increases failure radius",
        )
    ]

    resolved, conflicts, _ = reconcile_findings(deterministic, reasoning)

    assert resolved[0]["severity"] == "P1"
    assert conflicts[0]["resolution"] == "reasoning_precedence"


def test_reconciliation_escalates_unresolved_high_severity_conflict() -> None:
    deterministic = [_finding(lane="deterministic", severity="P1", description="Auth control mismatch")]
    reasoning = [_finding(lane="reasoning", severity="Critical", description="Auth control mismatch")]

    _, conflicts, report = reconcile_findings(deterministic, reasoning)

    assert conflicts[0]["class"] == "severity_disagreement"
    assert conflicts[0]["escalated"] is True
    assert conflicts[0]["resolved"] is False
    assert report["summary"]["unresolved_count"] == 1


def test_reconciliation_marks_presence_disagreement_when_only_one_lane_reports() -> None:
    deterministic = [_finding(lane="deterministic", severity="P2", description="Only deterministic")]

    resolved, conflicts, report = reconcile_findings(deterministic, [])

    assert len(resolved) == 1
    assert conflicts[0]["class"] == "presence_disagreement"
    assert report["summary"]["kept_count"] == 1


# ── FIX-BLOCKING-094: duplicate same-key findings within a lane ──────────


def test_reconciliation_preserves_duplicate_same_key_findings_in_a_lane() -> None:
    """Two findings in the same lane that share a _finding_key must not be
    silently overwritten; both are preserved in the resolved output."""
    deterministic = [
        _finding(lane="deterministic", severity="P2", description="dup issue"),
        _finding(lane="deterministic", severity="P1", description="dup issue"),
    ]

    resolved, conflicts, report = reconcile_findings(deterministic, [])

    # Both duplicate findings survive rather than one clobbering the other.
    assert len(resolved) == 2
    # An explicit duplicate-key conflict is surfaced deterministically.
    dup_conflicts = [c for c in conflicts if c.get("class") == "duplicate_key_conflict"]
    assert len(dup_conflicts) == 1
    assert dup_conflicts[0]["lane"] == "deterministic"
    assert dup_conflicts[0]["count"] == 2


def test_reconciliation_duplicate_key_counts_reflect_all_inputs() -> None:
    """Summary counts reflect every input finding, not just the last per key."""
    reasoning = [
        _finding(lane="reasoning", severity="P3", description="same"),
        _finding(lane="reasoning", severity="P3", description="same"),
    ]

    resolved, _, report = reconcile_findings([], reasoning)

    assert len(resolved) == 2
    assert report["summary"]["resolved_count"] == 2


# ── FIX-BLOCKING-110: unknown severity must not be silently coerced to P3 ─


def test_reconciliation_preserves_unknown_severity_raw_value() -> None:
    """An unrecognized severity is preserved raw and flagged, not rewritten to P3."""
    deterministic = [_finding(lane="deterministic", severity="BOGUS", description="odd sev")]

    resolved, _, _ = reconcile_findings(deterministic, [])

    finding = resolved[0]
    assert finding["severity"] != "P3"
    assert finding.get("raw_severity") == "BOGUS"
    assert finding.get("severity_validation") == "unknown_severity"


def test_shadow_compare_does_not_count_unknown_severity_as_p3() -> None:
    from sentinel.orchestrator.quality_artifacts import build_shadow_compare_report

    legacy = [_finding(lane="deterministic", severity="BOGUS", description="odd")]
    report = build_shadow_compare_report(
        legacy_findings=legacy,
        resolved_findings=[],
        finding_conflicts=[],
    )

    dist = report["baseline"]["severity_distribution"]
    assert dist.get("P3", 0) == 0
    assert dist.get("unknown", 0) == 1
