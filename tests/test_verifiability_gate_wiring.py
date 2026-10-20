"""Wiring test: the verifiability gate filters ADVISORY findings before the
Fix Planner in continue_phase1_after_triage."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sentinel.orchestrator import main as main_mod
from sentinel.orchestrator.main import HumanGateRequired, Orchestrator


def test_verifiability_gate_filters_advisory_before_planner(monkeypatch):
    orch = Orchestrator.__new__(Orchestrator)

    findings = [
        {"id": "F2-keep", "file": "assess/reviewer.py",
         "finding_kind": "logic_bug", "description": "real bug"},
        {"id": "F2-drop", "file": "assess/reviewer.py",
         "finding_kind": "logic_bug", "description": "advisory concern"},
    ]

    # --- state store: tech_debt approves both; everything else empty ---
    store = MagicMock()
    store.read.side_effect = lambda name: {
        "tech_debt": [
            {"finding_id": "F2-keep", "human_triage": "APPROVE"},
            {"finding_id": "F2-drop", "human_triage": "APPROVE"},
        ],
        "remediation_patterns": [],
        "semantic_groups": [],
        "findings": findings,
        "phase1_assessment": {},
    }.get(name, [])
    orch._store = store

    # --- synthesizer: returns the findings/modules/dependencies dict ---
    synth = MagicMock()
    synth.build_fix_planner_context.return_value = {
        "findings": findings, "modules": [], "dependencies": [],
    }
    orch._synthesizer = synth

    orch._config = MagicMock()
    orch._config.repo_path = "D:/Git/Sentinel"
    orch._audit = MagicMock()

    # --- architect: gate classifies F2-drop ADVISORY; planner returns empty ---
    arch = MagicMock()
    arch.classify_findings.return_value = {"classifications": [
        {"finding_id": "F2-keep", "verdict": "VERIFIABLE", "rationale": "real"},
        {"finding_id": "F2-drop", "verdict": "ADVISORY", "rationale": "not a bug"},
    ]}
    arch.build_remediation_plan.return_value = {
        "plan_version": "1.0", "blocking": [], "required": [],
        "desired": [], "nice_to_have": [], "deferred": [], "notes": "",
        "summary": {},
    }
    orch._architect = arch

    # avoid real file I/O in the gate context builder (classify is mocked anyway)
    monkeypatch.setattr(
        "sentinel.orchestrator.source_retrieval.build_verifiability_context",
        lambda findings, repo_root: "gate-context",
    )
    # capture what findings reach the planner
    captured: dict = {}

    def _recorder(findings, modules, dependencies, patterns=None,
                  registry_blockers=None):
        captured["findings"] = findings
        return "planner-context"

    monkeypatch.setattr(main_mod, "build_fix_planner_context", _recorder)

    # method ends by raising the plan-approval gate
    with pytest.raises(HumanGateRequired):
        orch.continue_phase1_after_triage()

    # 1. the gate ran
    arch.classify_findings.assert_called_once()
    # 2. ADVISORY finding did NOT reach the planner; VERIFIABLE did
    planner_ids = {f["id"] for f in captured["findings"]}
    assert "F2-keep" in planner_ids
    assert "F2-drop" not in planner_ids
    # 3. advisory finding persisted as a review note (not lost)
    write_by_store = {
        c.args[0]: c.args[1]
        for c in store.write.call_args_list if c.args
    }
    assert "advisory_findings" in write_by_store
    assert any(
        a["finding_id"] == "F2-drop" for a in write_by_store["advisory_findings"]
    )
