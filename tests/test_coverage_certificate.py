from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from sentinel.config import Config
from sentinel.orchestrator.main import HumanGateRequired, Orchestrator
from sentinel.orchestrator.quality_artifacts import (
    build_coverage_certificate,
    build_line_coverage_map,
)


def test_line_coverage_map_marks_full_coverage_with_deterministic_lane() -> None:
    coverage_map = build_line_coverage_map(
        module_scope_lines={"m": 10},
        deterministic_success={"m": True},
        reasoning_ranges={"m": [(1, 3)]},
    )

    assert len(coverage_map) == 1
    entry = coverage_map[0]
    assert entry["module"] == "m"
    assert entry["total_lines"] == 10
    assert entry["combined_reviewed_lines"] == 10
    assert entry["coverage_pct"] == 100.0
    assert entry["uncovered_ranges"] == []


def test_line_coverage_map_strict_mode_requires_reasoning_ranges() -> None:
    coverage_map = build_line_coverage_map(
        module_scope_lines={"m": 10},
        deterministic_success={"m": True},
        reasoning_ranges={"m": [(1, 3)]},
        strict_mode=True,
    )

    entry = coverage_map[0]
    assert entry["deterministic_reviewed_lines"] == 10
    assert entry["reasoning_reviewed_lines"] == 3
    assert entry["combined_reviewed_lines"] == 3
    assert entry["coverage_pct"] == 30.0


def test_line_coverage_map_reports_gaps_when_not_fully_reviewed() -> None:
    coverage_map = build_line_coverage_map(
        module_scope_lines={"m": 10},
        deterministic_success={"m": False},
        reasoning_ranges={"m": [(1, 3), (5, 5)]},
    )

    entry = coverage_map[0]
    assert entry["reasoning_reviewed_lines"] == 4
    assert entry["combined_reviewed_lines"] == 4
    assert entry["coverage_pct"] == 40.0
    assert entry["uncovered_ranges"] == [[4, 4], [6, 10]]


def test_coverage_certificate_fails_with_uncovered_lines() -> None:
    certificate = build_coverage_certificate(
        line_coverage_map=[
            {
                "module": "m",
                "total_lines": 10,
                "deterministic_reviewed_lines": 0,
                "reasoning_reviewed_lines": 4,
                "combined_reviewed_lines": 4,
                "coverage_pct": 40.0,
                "uncovered_ranges": [[5, 10]],
            }
        ],
        threshold_pct=100.0,
    )

    assert certificate["status"] == "fail"
    assert certificate["metrics"]["coverage_pct"] == 40.0
    assert certificate["metrics"]["uncovered_line_count"] == 6
    assert certificate["metrics"]["modules_with_gaps"] == ["m"]


def test_phase1_full_coverage_gate_blocks_when_lines_uncovered(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    long_file = "\n".join([f"line {i} with token" for i in range(1, 1201)]) + "\n"
    (repo / "src" / "big.cs").write_text(long_file, encoding="utf-8")

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_ENABLE_RETRIEVAL_REASONING_LANE": "true",
        "SENTINEL_ENFORCE_FULL_COVERAGE_GATE": "true",
        "SENTINEL_FULL_COVERAGE_THRESHOLD_PCT": "100",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    forced_status = {
        "module": "src",
        "module_path": "src",
        "chunks_total": 1,
        "chunks_capped": 0,
        "quick_review_failed_chunks": 0,
        "specialist_failed_chunks": 0,
        "findings_count": 0,
        "skills_run": ["layer2a-quick-review"],
        "specialist_attempted_chunks": {},
        "specialist_failed_chunks_by_skill": {},
        "review_complete": True,
    }

    with Orchestrator(cfg) as orch:
        with patch(
            "sentinel.assess.reviewer.ReviewOrchestrator.review_module_with_telemetry",
            return_value=([], forced_status, []),
        ):
            try:
                orch.run_phase1([repo])
                assert False, "Expected full coverage gate"
            except HumanGateRequired as gate:
                assert gate.gate_type == "full_coverage"
                assert "coverage_certificate_file" in gate.payload

    cert = json.loads((cfg.state_dir / "coverage_certificate.json").read_text(encoding="utf-8"))
    assert cert["status"] == "fail"


def test_phase1_full_coverage_gate_passes_with_deterministic_lane(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    long_file = "\n".join([f"line {i} with token" for i in range(1, 1201)]) + "\n"
    (repo / "src" / "big.cs").write_text(long_file, encoding="utf-8")

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_ENABLE_DETERMINISTIC_LANE": "true",
        "SENTINEL_ENABLE_RETRIEVAL_REASONING_LANE": "true",
        "SENTINEL_ENFORCE_FULL_COVERAGE_GATE": "true",
        "SENTINEL_FULL_COVERAGE_THRESHOLD_PCT": "100",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    forced_status = {
        "module": "src",
        "module_path": "src",
        "chunks_total": 1,
        "chunks_capped": 0,
        "quick_review_failed_chunks": 0,
        "specialist_failed_chunks": 0,
        "findings_count": 0,
        "skills_run": ["layer2a-quick-review"],
        "specialist_attempted_chunks": {},
        "specialist_failed_chunks_by_skill": {},
        "review_complete": True,
    }

    with Orchestrator(cfg) as orch:
        with patch(
            "sentinel.assess.reviewer.ReviewOrchestrator.review_module_with_telemetry",
            return_value=([], forced_status, []),
        ):
            try:
                orch.run_phase1([repo])
                assert False, "Expected Phase 1 triage gate"
            except HumanGateRequired as gate:
                assert gate.gate_type == "tech_debt_triage"

    cert = json.loads((cfg.state_dir / "coverage_certificate.json").read_text(encoding="utf-8"))
    assert cert["status"] == "pass"
    assert cert["metrics"]["coverage_pct"] == 100.0
