"""Regression coverage for orchestrator report metrics, fallback paths, and
multi-skill QA feedback selection (FIX-REQUIRED-012, FIX-REQUIRED-013).

These tests are isolated and deterministic — they exercise pure/near-pure
orchestrator units via ``Orchestrator.__new__`` with lightweight fakes, and do
not touch external repositories or the network. Each asserts behaviour that
fails under the pre-fix implementation and passes with the corrected one.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sentinel.orchestrator.main import Orchestrator, _canonical_severity


# ── FIX-REQUIRED-023: severity normalization ─────────────────────────


def test_canonical_severity_normalizes_case_and_flags_unknown():
    assert _canonical_severity("critical") == "Critical"
    assert _canonical_severity("P1") == "P1"
    assert _canonical_severity("  p2 ") == "P2"
    assert _canonical_severity("P3") == "P3"
    assert _canonical_severity("high") is None
    assert _canonical_severity("") is None


# ── FIX-REQUIRED-012 / 002: bounded completeness metrics ─────────────


def test_coverage_report_completeness_never_exceeds_reviewable():
    orch = Orchestrator.__new__(Orchestrator)
    modules = [{"name": "m_na"}, {"name": "m_rev"}]
    # A not-applicable module that is (incorrectly) marked review_complete must
    # not inflate completeness above the reviewable population.
    module_statuses = [
        {"module": "m_na", "skip_reason": "no_source_files", "review_complete": True},
        {"module": "m_rev", "review_complete": True},
    ]
    report = orch._build_coverage_report(
        modules=modules,
        module_statuses=module_statuses,
        failed_chunk_registry=[],
    )
    assert report["reviewable_modules"] == 1
    assert report["genuine_modules_complete"] <= report["reviewable_modules"]
    assert 0 <= report["genuine_module_completeness_pct"] <= 100


# ── FIX-REQUIRED-012 / 001 / 023: report file dedup + severity buckets ─


def _report_store(findings, dependencies):
    store = MagicMock()

    def _read(name):
        return {
            "findings": findings,
            "dependencies": dependencies,
            "analysis_pack": [],
            "tech_debt": [],
            "repositories": [{"name": "repo"}],
        }.get(name, [])

    store.read.side_effect = _read
    return store


def _orch_with_store(store):
    orch = Orchestrator.__new__(Orchestrator)
    orch._store = store
    orch._config = SimpleNamespace(repo_path=Path("/repo"))
    return orch


def test_phase1_report_dedups_current_repo_files_preserving_order():
    # Two non-external edges share the same source file, so the same in-repo
    # file appears as a review target for multiple dependency items.
    dependencies = [
        {"source": "sentinel/a.py", "target": "sentinel/b.py",
         "kind": "python_import", "is_external": False},
        {"source": "sentinel/a.py", "target": "sentinel/c.py",
         "kind": "python_import", "is_external": False},
    ]
    orch = _orch_with_store(_report_store([], dependencies))
    report = orch._build_phase1_assessment_report({"systemic_patterns": []})

    files = report["review_scope_queue"]["current_repo_files"]
    # Each in-repo file appears at most once, first-seen order preserved.
    assert files == list(dict.fromkeys(files))
    assert files.count("sentinel/a.py") == 1
    assert files == ["sentinel/a.py", "sentinel/b.py", "sentinel/c.py"]


def test_phase1_report_counts_malformed_severities_in_unknown_bucket():
    findings = [
        {"id": "F1", "severity": "Critical", "description": "d1"},
        {"id": "F2", "severity": "totally-bogus", "description": "d2"},
        {"id": "F3", "severity": "p1", "description": "d3"},
    ]
    orch = _orch_with_store(_report_store(findings, []))
    report = orch._build_phase1_assessment_report({"systemic_patterns": []})

    summary = report["summary"]
    # No finding silently dropped: the counts reconcile with total findings.
    assert summary["total_findings"] == 3
    counts = summary["severity_counts"]
    assert counts["Critical"] == 1
    assert counts["P1"] == 1  # "p1" normalized
    assert counts.get("unknown") == 1
    assert sum(counts.values()) == 3


# ── FIX-REQUIRED-013 / BLOCKING-005: multi-skill QA feedback ─────────


def test_qa_failure_feedback_selects_failing_skill_not_first():
    per_skill_results = [
        {"feedback": "looks good"},
        {"feedback": "null deref on line 12"},
    ]
    per_skill_verdicts = [
        {"skill": "correctness", "verdict": "PASS"},
        {"skill": "security", "verdict": "FAIL"},
    ]
    feedback = Orchestrator._qa_failure_feedback(per_skill_results, per_skill_verdicts)
    assert "null deref on line 12" in feedback
    assert "[security]" in feedback
    assert "looks good" not in feedback


def test_qa_failure_feedback_multiple_failures_deterministic_and_labelled():
    per_skill_results = [
        {"feedback": "fail A"},
        {"feedback": "pass B"},
        {"feedback": "fail C"},
    ]
    per_skill_verdicts = [
        {"skill": "a", "verdict": "FAIL"},
        {"skill": "b", "verdict": "PASS"},
        {"skill": "c", "verdict": "FAIL"},
    ]
    feedback = Orchestrator._qa_failure_feedback(per_skill_results, per_skill_verdicts)
    # Deterministic execution-order join of the failing skills only.
    assert feedback == "[a] fail A\n\n[c] fail C"


# ── FIX-REQUIRED-013 / 004 / 007: scope classification fallback ─────


def _scope_orch():
    orch = Orchestrator.__new__(Orchestrator)
    orch._config = SimpleNamespace(repo_path=Path("/repo"))
    return orch


def test_scope_changed_files_fallback_emits_degraded_signal(monkeypatch):
    from sentinel.govern import review_policy

    def _boom(repo_path):
        raise review_policy.ReviewPolicyError("git status failed")

    monkeypatch.setattr(review_policy, "classify_changed_files", _boom)
    orch = _scope_orch()
    diff = "diff --git a/x.py b/x.py\n+++ b/x.py\n"
    files = orch._scope_changed_files({}, diff)

    # Fallback still yields diff-parsed files, and the degradation is exposed.
    assert files == ["x.py"]
    assert orch._scope_files_source == "degraded_diff_fallback"


def test_scope_changed_files_unexpected_exception_not_swallowed(monkeypatch):
    from sentinel.govern import review_policy

    def _unexpected(repo_path):
        raise ValueError("unexpected classifier bug")

    monkeypatch.setattr(review_policy, "classify_changed_files", _unexpected)
    orch = _scope_orch()
    with pytest.raises(ValueError, match="unexpected classifier bug"):
        orch._scope_changed_files({}, "diff --git a/x.py b/x.py\n+++ b/x.py\n")


def test_scope_changed_files_prefers_coder_structured_list():
    orch = _scope_orch()
    files = orch._scope_changed_files({"files_changed": ["sentinel/z.py"]}, "")
    assert files == ["sentinel/z.py"]
    assert orch._scope_files_source == "coder_structured"
