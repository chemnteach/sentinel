from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from sentinel.orchestrator.quality_artifacts import build_shadow_compare_report
from sentinel.config import Config
from sentinel.orchestrator.main import HumanGateRequired, Orchestrator


def test_shadow_compare_computes_deltas_and_promotion_readiness() -> None:
    legacy_findings = [
        {"severity": "P2"},
        {"severity": "P3"},
    ]
    resolved_findings = [
        {"severity": "P1"},
        {"severity": "P2"},
        {"severity": "P3"},
    ]
    finding_conflicts = [
        {"resolved": True, "severity": "P2"},
        {"resolved": True, "severity": "P3"},
    ]

    report = build_shadow_compare_report(
        legacy_findings=legacy_findings,
        resolved_findings=resolved_findings,
        finding_conflicts=finding_conflicts,
    )

    assert report["delta"]["finding_count_delta"] == 1
    assert report["delta"]["severity_distribution_delta"]["P1"] == 1
    assert report["promotion"]["promotion_ready"] is True
    assert report["rollback"]["recommended"] is False


def test_shadow_compare_recommends_rollback_when_unresolved_critical_conflicts_present() -> None:
    report = build_shadow_compare_report(
        legacy_findings=[{"severity": "P2"}],
        resolved_findings=[{"severity": "Critical"}],
        finding_conflicts=[
            {"resolved": False, "severity": "Critical", "escalated": True}
        ],
    )

    assert report["promotion"]["promotion_ready"] is False
    assert report["rollback"]["recommended"] is True
    assert "critical" in report["rollback"]["reason"].lower()


def test_phase1_writes_shadow_compare_report_when_shadow_mode_enabled(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_ENABLE_DETERMINISTIC_LANE": "true",
        "SENTINEL_ENABLE_RETRIEVAL_REASONING_LANE": "true",
        "SENTINEL_ENABLE_RECONCILIATION": "true",
        "SENTINEL_SHADOW_COMPARE_MODE": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    deterministic_findings = [
        {
            "module": "src",
            "skill": "deterministic-lane",
            "severity": "P2",
            "category": "maintainability",
            "location": "src/app.py:1",
            "description": "Deterministic issue",
            "suggested_fix": "fix",
            "status": "open",
        }
    ]
    reasoning_findings = [
        {
            "module": "src",
            "skill": "layer2a-quick-review",
            "severity": "P2",
            "category": "maintainability",
            "location": "src/app.py:2",
            "description": "Reasoning issue",
            "suggested_fix": "fix",
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
        "skills_run": ["layer2a-quick-review"],
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
                    assert False, "Expected Phase 1 triage gate"
                except HumanGateRequired as gate:
                    assert gate.gate_type == "tech_debt_triage"

    report_path = cfg.state_dir / "shadow_compare_report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "delta" in report
    assert "promotion" in report
    assert "rollback" in report
