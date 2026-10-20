from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from sentinel.orchestrator.quality_artifacts import (
    build_quality_scorecard,
    classify_failed_chunks,
    is_transport_failure,
)
from sentinel.config import Config
from sentinel.orchestrator.main import HumanGateRequired, Orchestrator


# Cycle 41: the exact failed-chunk-registry shape from self-assessment run 007.
_RUN_007_TRANSPORT_ENTRY = {
    "module": "sentinel:workflow",
    "skill": "layer4b-refactoring-advisor",
    "chunk_index": 20,
    "error_type": "AgentError",
    "error": (
        "Agent 'architect' failed on skill 'layer4b-refactoring-advisor': "
        'Server error 503: {"detail":{"error":"Chat API error 504: ",'
        '"error_type":"unknown","retryable":true,"suggestion":"Please try '
        'again later","timestamp":"2026-06-11T08:20:54.700437"}}'
    ),
}
_GENUINE_ENTRY = {
    "module": "sentinel:core",
    "skill": "layer2a-quick-review",
    "chunk_index": 3,
    "error_type": "FindingSchemaValidationError",
    "error": "finding 2 missing required field 'finding_kind'",
}


def test_quality_scorecard_computes_metrics_and_high_confidence_label() -> None:
    coverage_report = {
        "module_completeness_pct": 99.0,
        "quick_review_success_pct": 97.0,
        "failed_chunk_entries": 0,
    }
    retrieval_manifest = [
        {
            "skills": [
                {
                    "skill": "layer2a-quick-review",
                    "snippets": [
                        {
                            "module_path": "src/app.py",
                            "start_line": 1,
                            "end_line": 5,
                            "snippet_hash": "abc",
                            "char_count": 20,
                            "content": "x",
                        }
                    ],
                }
            ]
        }
    ]
    finding_conflicts = [
        {"class": "duplicate_agreement", "resolved": True, "escalated": False, "severity": "P2"},
        {"class": "presence_disagreement", "resolved": True, "escalated": False, "severity": "P3"},
    ]

    scorecard = build_quality_scorecard(
        coverage_report=coverage_report,
        retrieval_manifest=retrieval_manifest,
        finding_conflicts=finding_conflicts,
    )

    assert scorecard["confidence_label"] == "high"
    assert scorecard["metrics"]["module_completeness_pct"] == 99.0
    assert scorecard["metrics"]["lane_agreement_pct"] == 100.0
    assert scorecard["gates"]["lane_conflict_resolution"] == "pass"


def test_quality_scorecard_flags_incomplete_on_unresolved_critical_conflicts() -> None:
    coverage_report = {
        "module_completeness_pct": 100.0,
        "quick_review_success_pct": 100.0,
        "failed_chunk_entries": 0,
    }

    scorecard = build_quality_scorecard(
        coverage_report=coverage_report,
        retrieval_manifest=[],
        finding_conflicts=[
            {
                "class": "severity_disagreement",
                "resolved": False,
                "escalated": True,
                "severity": "Critical",
            }
        ],
    )

    assert scorecard["confidence_label"] == "incomplete"
    assert scorecard["metrics"]["unresolved_conflicts_by_severity"]["Critical"] == 1
    assert scorecard["gates"]["lane_conflict_resolution"] == "fail"
    assert scorecard["gates"]["confidence_floor"] == "fail"


def test_quality_scorecard_strict_mode_requires_100_percent_thresholds() -> None:
    coverage_report = {
        "module_completeness_pct": 99.0,
        "quick_review_success_pct": 100.0,
        "failed_chunk_entries": 0,
    }
    scorecard = build_quality_scorecard(
        coverage_report=coverage_report,
        retrieval_manifest=[],
        finding_conflicts=[],
        strict_mode=True,
    )

    assert scorecard["confidence_label"] == "incomplete"
    assert scorecard["gates"]["review_completeness"] == "fail"
    assert scorecard["gates"]["confidence_floor"] == "fail"


def test_phase1_quality_scorecard_gate_payload_is_complete(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_ENABLE_DETERMINISTIC_LANE": "true",
        "SENTINEL_ENABLE_RETRIEVAL_REASONING_LANE": "true",
        "SENTINEL_ENABLE_RECONCILIATION": "true",
        "SENTINEL_ENABLE_QUALITY_SCORECARD": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    deterministic_findings = [
        {
            "module": "src",
            "skill": "deterministic-lane",
            "severity": "P1",
            "category": "security",
            "location": "src/app.py:1",
            "description": "Authorization mismatch",
            "suggested_fix": "tighten auth",
            "status": "open",
        }
    ]
    reasoning_findings = [
        {
            "module": "src",
            "skill": "layer2c-security-specialist",
            "severity": "Critical",
            "category": "security",
            "location": "src/app.py:1",
            "description": "Authorization mismatch",
            "suggested_fix": "tighten auth",
            "status": "open",
        }
    ]
    status = {
        "module": "src",
        "module_path": "src",
        "chunks_total": 1,
        "chunks_capped": 0,
        "quick_review_failed_chunks": 0,
        "specialist_failed_chunks": 0,
        "findings_count": 1,
        "skills_run": ["layer2c-security-specialist"],
        "specialist_attempted_chunks": {},
        "specialist_failed_chunks_by_skill": {},
        "review_complete": True,
    }

    with Orchestrator(cfg) as orch:
        with patch.object(orch._deterministic_analyzer, "review_module", return_value=deterministic_findings):
            with patch(
                "sentinel.assess.reviewer.ReviewOrchestrator.review_module_with_telemetry",
                return_value=(reasoning_findings, status, []),
            ):
                try:
                    orch.run_phase1([repo])
                    assert False, "Expected quality gate"
                except HumanGateRequired as gate:
                    assert gate.gate_type == "lane_conflict_resolution"
                    payload = gate.payload
                    assert "quality_scorecard_file" in payload
                    assert "coverage_report_file" in payload
                    assert "reconciliation_report_file" in payload
                    assert "finding_conflicts_file" in payload
                    assert "quality_scorecard" in payload

    scorecard_path = cfg.state_dir / "quality_scorecard.json"
    assert scorecard_path.exists()
    scorecard = json.loads(scorecard_path.read_text(encoding="utf-8"))
    assert scorecard["gates"]["lane_conflict_resolution"] == "fail"


# ----------------------------------------------------------------------
# Cycle 41: transient transport failures must not block the Stage-3 gate.
# ----------------------------------------------------------------------


def test_is_transport_failure_classifies_retryable_5xx_and_genuine() -> None:
    # The run-007 retryable 503/504 is transport.
    assert is_transport_failure(_RUN_007_TRANSPORT_ENTRY) is True
    # A schema-validation failure is a genuine review gap, not transport.
    assert is_transport_failure(_GENUINE_ENTRY) is False
    # Timeout / gateway phrasing is transport.
    assert is_transport_failure({"error": "Read timed out after 60s"}) is True
    assert is_transport_failure({"error": "502 Bad Gateway"}) is True
    # A bare number that is not an HTTP status must not false-positive.
    assert is_transport_failure({"error": "expected 503 items, got 502"}) is False


def test_classify_failed_chunks_splits_transport_and_genuine() -> None:
    transport, genuine = classify_failed_chunks(
        [_RUN_007_TRANSPORT_ENTRY, _GENUINE_ENTRY, _RUN_007_TRANSPORT_ENTRY]
    )
    assert transport == 2
    assert genuine == 1
    assert classify_failed_chunks([]) == (0, 0)
    assert classify_failed_chunks(None) == (0, 0)


def test_quality_scorecard_tolerates_transient_transport_failure() -> None:
    """Run-007 shape: 100% genuine completeness, 0 genuine failures, 1
    transient transport failure → strict gate PASSES (was failing before
    cycle 41)."""
    coverage_report = {
        "module_completeness_pct": 92.31,  # raw: held back by the flaky module
        "genuine_module_completeness_pct": 100.0,  # genuine: that module is transport-only
        "quick_review_success_pct": 100.0,
        "failed_chunk_entries": 1,
        "genuine_failed_chunk_entries": 0,
        "transport_failure_chunks": 1,
    }
    scorecard = build_quality_scorecard(
        coverage_report=coverage_report,
        retrieval_manifest=[],
        finding_conflicts=[],
        failed_chunk_registry=[_RUN_007_TRANSPORT_ENTRY],
        strict_mode=True,
    )
    assert scorecard["gates"]["review_completeness"] == "pass"
    assert scorecard["gates"]["confidence_floor"] == "pass"
    assert scorecard["confidence_label"] == "high"
    assert scorecard["metrics"]["transport_failure_chunks"] == 1
    assert scorecard["metrics"]["genuine_failed_chunk_entries"] == 0


def test_quality_scorecard_still_fails_on_genuine_failed_chunk() -> None:
    """A genuine (non-transport) failed chunk must still hard-fail the gate."""
    coverage_report = {
        "module_completeness_pct": 92.31,
        "genuine_module_completeness_pct": 92.31,
        "quick_review_success_pct": 100.0,
        "failed_chunk_entries": 1,
        "genuine_failed_chunk_entries": 1,
        "transport_failure_chunks": 0,
    }
    scorecard = build_quality_scorecard(
        coverage_report=coverage_report,
        retrieval_manifest=[],
        finding_conflicts=[],
        failed_chunk_registry=[_GENUINE_ENTRY],
        strict_mode=True,
    )
    assert scorecard["gates"]["review_completeness"] == "fail"
    assert scorecard["confidence_label"] == "incomplete"


def test_quality_scorecard_fails_when_transport_exceeds_tolerance() -> None:
    """Transport failures above the tolerance (3) indicate a degraded run and
    must fail the gate even with full genuine completeness."""
    coverage_report = {
        "module_completeness_pct": 100.0,
        "genuine_module_completeness_pct": 100.0,
        "quick_review_success_pct": 100.0,
        "failed_chunk_entries": 4,
        "genuine_failed_chunk_entries": 0,
        "transport_failure_chunks": 4,
    }
    scorecard = build_quality_scorecard(
        coverage_report=coverage_report,
        retrieval_manifest=[],
        finding_conflicts=[],
        failed_chunk_registry=[_RUN_007_TRANSPORT_ENTRY] * 4,
        strict_mode=True,
    )
    assert scorecard["gates"]["review_completeness"] == "fail"
    assert scorecard["confidence_label"] == "incomplete"


def test_coverage_report_transport_only_module_counts_as_genuinely_complete(
    tmp_path: Path,
) -> None:
    """Cycle 41 / run-007: a reviewable module incomplete only because of a
    transient transport failure is genuinely complete; genuine completeness is
    100% while the raw figure reflects the flaky module."""
    cfg = _coverage_cfg(tmp_path)
    modules = [{"name": f"m{i}", "path": f"m{i}"} for i in range(13)]
    statuses = [
        {"module": f"m{i}", "review_complete": True, "chunks_total": 2} for i in range(12)
    ] + [
        {"module": "m12", "review_complete": False, "chunks_total": 2, "specialist_failed_chunks": 1},
    ]
    registry = [{**_RUN_007_TRANSPORT_ENTRY, "module": "m12"}]

    with Orchestrator(cfg) as orch:
        report = orch._build_coverage_report(modules, statuses, registry)

    assert report["reviewable_modules"] == 13
    assert report["modules_complete"] == 12
    assert report["module_completeness_pct"] == round(12 / 13 * 100.0, 2)  # raw
    assert report["transport_incomplete_modules"] == 1
    assert report["genuine_modules_complete"] == 13
    assert report["genuine_module_completeness_pct"] == 100.0
    assert report["transport_failure_chunks"] == 1
    assert report["genuine_failed_chunk_entries"] == 0


def test_coverage_report_genuine_failed_module_stays_incomplete(tmp_path: Path) -> None:
    """A module incomplete due to a genuine (non-transport) failure stays a
    genuine gap — genuine completeness reflects it."""
    cfg = _coverage_cfg(tmp_path)
    modules = [{"name": f"m{i}", "path": f"m{i}"} for i in range(13)]
    statuses = [
        {"module": f"m{i}", "review_complete": True, "chunks_total": 2} for i in range(12)
    ] + [
        {"module": "m12", "review_complete": False, "chunks_total": 2, "quick_review_failed_chunks": 1},
    ]
    registry = [{**_GENUINE_ENTRY, "module": "m12"}]

    with Orchestrator(cfg) as orch:
        report = orch._build_coverage_report(modules, statuses, registry)

    assert report["transport_incomplete_modules"] == 0
    assert report["genuine_modules_complete"] == 12
    assert report["genuine_module_completeness_pct"] == round(12 / 13 * 100.0, 2)
    assert report["genuine_failed_chunk_entries"] == 1
    assert report["transport_failure_chunks"] == 0


def _coverage_cfg(tmp_path: Path) -> Config:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    return Config(
        env={
            "SENTINEL_REPO_PATH": str(repo),
            "SENTINEL_STATE_DIR": str(tmp_path / "state"),
            "SENTINEL_LOCAL_ONLY_MODE": "true",
        },
        load_dotenv_file=False,
    )


def test_coverage_report_excludes_not_applicable_modules(tmp_path: Path) -> None:
    """Cycle 39A: no-source / path-skipped modules are reported as
    not-applicable, not counted against completeness — mirrors run 005
    (13 reviewable complete + 3 no-source = 100%, not 81.25%)."""
    cfg = _coverage_cfg(tmp_path)
    modules = [{"name": f"m{i}", "path": f"m{i}"} for i in range(16)]
    statuses = [
        {"module": f"m{i}", "review_complete": True, "chunks_total": 2} for i in range(13)
    ] + [
        {"module": "m13", "review_complete": False, "skip_reason": "no_source_files", "chunks_total": 0},
        {"module": "m14", "review_complete": False, "skip_reason": "no_source_files", "chunks_total": 0},
        {"module": "m15", "review_complete": False, "skip_reason": "path_outside_repo", "chunks_total": 0},
    ]

    with Orchestrator(cfg) as orch:
        report = orch._build_coverage_report(modules, statuses, [])

    assert report["modules_not_applicable"] == 3
    assert report["reviewable_modules"] == 13
    assert report["modules_complete"] == 13
    assert report["modules_incomplete"] == 0
    assert report["module_completeness_pct"] == 100.0


def test_coverage_report_keeps_read_errors_as_incomplete(tmp_path: Path) -> None:
    """Cycle 39A: a read_error is a genuine coverage gap, not n/a — it must
    still count against completeness."""
    cfg = _coverage_cfg(tmp_path)
    modules = [{"name": f"m{i}", "path": f"m{i}"} for i in range(4)]
    statuses = [
        {"module": "m0", "review_complete": True, "chunks_total": 1},
        {"module": "m1", "review_complete": True, "chunks_total": 1},
        {"module": "m2", "review_complete": False, "skip_reason": "read_error: boom", "chunks_total": 0},
        {"module": "m3", "review_complete": False, "skip_reason": "no_source_files", "chunks_total": 0},
    ]

    with Orchestrator(cfg) as orch:
        report = orch._build_coverage_report(modules, statuses, [])

    # m3 (no_source_files) is n/a; m2 (read_error) stays a reviewable gap.
    assert report["modules_not_applicable"] == 1
    assert report["reviewable_modules"] == 3
    assert report["modules_complete"] == 2
    assert report["modules_incomplete"] == 1
    assert report["module_completeness_pct"] == round(2 / 3 * 100.0, 2)
