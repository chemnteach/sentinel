"""
tests/test_rca_formatter.py

Unit tests for RCAReportFormatter (Markdown report generation and .patch export).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
import pytest

from sentinel.defect.models import DefectReport, RCAReport, DefectSpec
from sentinel.reports.rca_formatter import RCAReportFormatter


@pytest.fixture
def sample_data():
    defect = DefectReport(
        defect_id="DEF-001",
        title="Null pointer when parsing empty payload",
        description="Handler crashes when body is empty",
        symptoms="500 Internal Error",
        severity="HIGH",
        reproduction_steps="POST /api/process with {}",
    )
    rca = RCAReport(
        defect_id="DEF-001",
        symptom_analysis="TypeError on payload['data']",
        proximal_mechanism="handler.py:42 accesses None as dict",
        systemic_factor="No schema validation middleware",
        root_cause="Missing input validation boundary at route entry",
        blast_radius=["src/handler.py", "src/router.py"],
        external_dependency=False,
        remediation_options=[
            {
                "id": "OPT-1",
                "strategy": "SURGICAL",
                "title": "Add null check",
                "description": "Guard against None payload",
                "effort": "LOW",
                "risk": "LOW",
                "tradeoffs": "Fast, minimal change",
            }
        ],
        recommended_option="OPT-1",
    )
    spec = DefectSpec(
        defect_id="DEF-001",
        selected_option="OPT-1",
        affected_symbols=["process_data"],
        reproduction_test_stubs={
            "test_file": "tests/test_repro.py",
            "test_code": "def test_repro():\n    assert process_data({}) is None\n",
        },
        proposed_diff="--- a/src/handler.py\n+++ b/src/handler.py\n@@ -42,1 +42,3 @@\n+ if not payload: return None\n",
        acceptance_criteria=["Empty body returns 200 with empty response"],
    )
    return defect, rca, spec


def test_format_markdown_report(sample_data):
    defect, rca, spec = sample_data
    formatter = RCAReportFormatter(repo_path=".")

    md = formatter.format_markdown(defect, rca, spec)

    assert "# Root Cause Analysis & Remediation Brief: DEF-001" in md
    assert "Null pointer when parsing empty payload" in md
    assert "3-Tier Causal Decomposition" in md
    assert "Tier 1 — Proximal Mechanism" in md
    assert "Tier 2 — Intermediate / Systemic Factor" in md
    assert "Tier 3 — Root Architectural Cause" in md
    assert "Proposed Unified Diff / Patch" in md
    assert "Reproduction Test Stubs" in md
    assert "Acceptance Criteria" in md


def test_write_artifacts(sample_data):
    defect, rca, spec = sample_data
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        formatter = RCAReportFormatter(repo_path=root)

        report_path, patch_path = formatter.write_artifacts(defect, rca, spec)

        assert report_path.exists()
        assert report_path.name == "RCA_DEF-001_Report.md"
        assert patch_path.exists()
        assert patch_path.name == "DEF-001.patch"

        patch_content = patch_path.read_text(encoding="utf-8")
        assert "--- a/src/handler.py" in patch_content
