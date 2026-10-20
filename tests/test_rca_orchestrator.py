"""
tests/test_rca_orchestrator.py

Unit tests for RCAOrchestrator, mocking ArchitectAgent and verifying
3-tier RCA and spec generation pipeline.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from sentinel.defect.models import DefectReport, DefectStatus
from sentinel.defect.defect_manager import DefectManager
from sentinel.defect.context_slicer import ContextSlicer
from sentinel.defect.rca_orchestrator import RCAOrchestrator, HumanRCAGateRequired, RCAError
from sentinel.orchestrator.state_store import StateStore


@pytest.fixture
def test_setup():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        store = StateStore(root / "state", root)
        defect_mgr = DefectManager(store)
        slicer = ContextSlicer(root, store)

        mock_architect = MagicMock()
        mock_architect.analyze_root_cause.return_value = {
            "defect_id": "DEF-001",
            "symptom_analysis": "Crash on empty payload",
            "proximal_mechanism": "handler.py:42 null dereference",
            "systemic_factor": "No middleware schema check",
            "root_cause": "Missing input validation boundary",
            "blast_radius": ["handler.py"],
            "external_dependency": False,
            "remediation_options": [
                {
                    "id": "OPT-1",
                    "strategy": "SURGICAL",
                    "title": "Add null check",
                    "effort": "LOW",
                    "risk": "LOW",
                }
            ],
            "recommended_option": "OPT-1",
        }
        mock_architect.write_bug_spec.return_value = {
            "defect_id": "DEF-001",
            "selected_option": "OPT-1",
            "affected_symbols": ["process_data"],
            "reproduction_test_stubs": {
                "test_file": "tests/test_repro.py",
                "test_code": "def test_repro(): pass",
            },
            "proposed_diff": "--- a/handler.py\n+++ b/handler.py\n@@ -42,1 +42,3 @@\n+ if not payload: return None",
            "acceptance_criteria": ["Handles empty payload gracefully"],
            "task_definitions": [
                {
                    "task_id": "DEF-001-T1",
                    "title": "Apply null guard in handler.py",
                }
            ],
        }

        orchestrator = RCAOrchestrator(
            defect_manager=defect_mgr,
            architect=mock_architect,
            slicer=slicer,
            store=store,
        )

        yield root, store, defect_mgr, mock_architect, orchestrator


def test_rca_orchestrator_run_flow(test_setup):
    root, store, defect_mgr, mock_architect, orchestrator = test_setup

    defect = defect_mgr.submit({
        "title": "Null pointer on empty payload",
        "description": "Handler crashes when body is empty",
        "symptoms": "500 Internal Error",
        "severity": "HIGH",
    })
    defect_mgr.triage(defect.defect_id, decision="APPROVED")

    # Running RCA should complete Layer 8a and 8b, then raise HumanRCAGateRequired
    with pytest.raises(HumanRCAGateRequired) as exc_info:
        orchestrator.run(defect.defect_id)

    assert exc_info.value.defect_id == defect.defect_id
    payload = exc_info.value.payload
    assert payload["root_cause"] == "Missing input validation boundary"
    assert "proposed_diff" in payload

    # Verify state persistence
    rca_list = store.read("rca_reports")
    assert len(rca_list) == 1
    assert rca_list[0]["defect_id"] == defect.defect_id

    spec_list = store.read("defect_specs")
    assert len(spec_list) == 1
    assert spec_list[0]["defect_id"] == defect.defect_id

    # Verify defect status advanced to DIAGNOSED_REPORT_READY
    updated_defect = defect_mgr.get(defect.defect_id)
    assert updated_defect.status == DefectStatus.DIAGNOSED_REPORT_READY
