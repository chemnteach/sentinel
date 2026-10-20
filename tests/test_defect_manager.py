"""
tests/test_defect_manager.py

Unit and integration tests for DefectReport models, StateStore defect allocation,
and DefectManager lifecycle transitions.
"""

from __future__ import annotations

import tempfile
import pytest
from pathlib import Path

from sentinel.defect.models import (
    DefectReport,
    DefectSeverity,
    DefectStatus,
    RCAReport,
    DefectSpec,
)
from sentinel.defect.defect_manager import (
    DefectManager,
    DefectValidationError,
    DefectTransitionError,
)
from sentinel.orchestrator.state_store import StateStore


@pytest.fixture
def tmp_store():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        store = StateStore(p / "state", p)
        yield store


def test_defect_report_serialization():
    d = DefectReport(
        defect_id="DEF-001",
        title="Null pointer when parsing payload",
        description="Crashing in handler.py on invalid JSON",
        symptoms="500 Internal Server Error returned to client",
        stack_trace="Traceback (most recent call last):\n  File 'handler.py', line 42",
        reproduction_steps="Send empty body to POST /process",
        suspected_files=["handler.py"],
        severity="HIGH",
    )
    raw = d.to_dict()
    assert raw["defect_id"] == "DEF-001"
    assert raw["status"] == DefectStatus.PENDING

    restored = DefectReport.from_dict(raw)
    assert restored.defect_id == "DEF-001"
    assert restored.title == d.title
    assert restored.suspected_files == ["handler.py"]


def test_rca_report_serialization():
    rca = RCAReport(
        defect_id="DEF-001",
        symptom_analysis="Unhandled NoneType error on missing 'data' field",
        proximal_mechanism="handler.py:42 accesses payload['data'] without check",
        systemic_factor="Missing input validation boundary at HTTP layer",
        root_cause="Schema validation was not enforced prior to route dispatch",
        blast_radius=["sentinel/api/handler.py", "sentinel/api/router.py"],
        external_dependency=False,
        remediation_options=[
            {
                "id": "OPT-1",
                "strategy": "SURGICAL",
                "description": "Add schema validator middleware before handler",
                "effort": "LOW",
                "risk": "LOW",
            }
        ],
        recommended_option="OPT-1",
    )
    raw = rca.to_dict()
    assert raw["defect_id"] == "DEF-001"
    restored = RCAReport.from_dict(raw)
    assert restored.recommended_option == "OPT-1"
    assert restored.external_dependency is False


def test_defect_manager_submit(tmp_store):
    mgr = DefectManager(tmp_store)

    data = {
        "title": "Index out of range in batch processor",
        "description": "Batch processing fails when batch size is 0",
        "symptoms": "Job hangs and raises IndexError",
        "severity": "CRITICAL",
        "stack_trace": "File 'batch.py', line 100",
        "reproduction_steps": "Run with --batch-size 0",
        "suspected_files": ["batch.py"],
    }
    defect = mgr.submit(data)
    assert defect.defect_id == "DEF-001"
    assert defect.status == DefectStatus.PENDING
    assert defect.severity == "CRITICAL"
    assert len(defect.history) == 1

    # Second submit gets DEF-002
    defect2 = mgr.submit({
        "title": "Second defect",
        "description": "Another bug",
        "symptoms": "Broken UI",
        "severity": "MEDIUM",
    })
    assert defect2.defect_id == "DEF-002"


def test_defect_manager_submit_validation(tmp_store):
    mgr = DefectManager(tmp_store)

    # Missing title
    with pytest.raises(DefectValidationError, match="title"):
        mgr.submit({
            "description": "Missing title",
            "symptoms": "Crash",
            "severity": "HIGH",
        })

    # Missing description
    with pytest.raises(DefectValidationError, match="description"):
        mgr.submit({
            "title": "Valid title",
            "symptoms": "Crash",
            "severity": "HIGH",
        })

    # Missing symptoms
    with pytest.raises(DefectValidationError, match="symptoms"):
        mgr.submit({
            "title": "Valid title",
            "description": "Valid desc",
            "severity": "HIGH",
        })

    # Invalid severity
    with pytest.raises(DefectValidationError, match="severity"):
        mgr.submit({
            "title": "Valid title",
            "description": "Valid desc",
            "symptoms": "Crash",
            "severity": "SUPER_DUPER",
        })


def test_defect_manager_triage(tmp_store):
    mgr = DefectManager(tmp_store)
    defect = mgr.submit({
        "title": "Memory leak during file upload",
        "description": "Buffer is not cleared after streaming",
        "symptoms": "OOM after 100 uploads",
        "severity": "HIGH",
    })

    # Approve triage
    approved = mgr.triage(defect.defect_id, decision="APPROVED", reason="High impact bug")
    assert approved.status == DefectStatus.APPROVED
    assert len(approved.history) == 2

    # Invalid decision
    with pytest.raises(DefectValidationError, match="Invalid triage decision"):
        mgr.triage(defect.defect_id, decision="MAYBE")


def test_defect_manager_lifecycle_transitions(tmp_store):
    mgr = DefectManager(tmp_store)
    defect = mgr.submit({
        "title": "Race condition in cache invalidation",
        "description": "Cache key read before lock acquired",
        "symptoms": "Stale data returned under load",
        "severity": "HIGH",
    })

    mgr.triage(defect.defect_id, decision="APPROVED")

    # APPROVED -> IN_RCA
    mgr.transition(defect.defect_id, DefectStatus.IN_RCA, reason="Starting RCA")
    d = mgr.get(defect.defect_id)
    assert d.status == DefectStatus.IN_RCA

    # IN_RCA -> RCA_COMPLETE
    mgr.transition(defect.defect_id, DefectStatus.RCA_COMPLETE, reason="RCA done")
    d = mgr.get(defect.defect_id)
    assert d.status == DefectStatus.RCA_COMPLETE

    # RCA_COMPLETE -> DIAGNOSED_REPORT_READY (advisory terminal state)
    mgr.transition(defect.defect_id, DefectStatus.DIAGNOSED_REPORT_READY, reason="Advisory brief generated")
    d = mgr.get(defect.defect_id)
    assert d.status == DefectStatus.DIAGNOSED_REPORT_READY

    # Invalid transition from DIAGNOSED_REPORT_READY to PENDING
    with pytest.raises(DefectTransitionError):
        mgr.transition(defect.defect_id, DefectStatus.PENDING, reason="Invalid")
