"""End-to-end integration test for the structured-conditions pipeline (cycle 54).

Traces one finding's triage conditions through every governance stage using the
real cycle-52 code paths (no mocks — the only external dependency, LLM, is not
exercised here because every stage under test is deterministic):

  1. export shape validation        (DecisionBatch)
  2. triage --batch write-through   (real CLI -> tech_debt.json)
  3. threading join                 (get_task_finding_ids + merge_conditions)
  4. Architect constraint injection (format_constraints_for_architect)
  5a. QA criteria addition          (get_effective_criteria)
  5b. SCOPE_LIMIT enforcement        (check_scope_limit)

Note vs the original spec: the merge helper is ``merge_conditions(condition_lists)``
(single arg), and ``_apply_batch_decisions`` is 3-arg, so Stage 2 runs the real
``triage --batch`` CLI.
"""
from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from sentinel.cli import main
from sentinel.govern.conditions import (
    DecisionBatch,
    check_scope_limit,
    format_constraints_for_architect,
    get_effective_criteria,
    get_task_finding_ids,
    merge_conditions,
)

_FULL_CONDITIONS = [
    {"type": "REQUIRE_CHANGE", "target": "change_0", "note": "Add null check"},
    {"type": "EXCLUDE_CHANGE", "target": "change_1", "note": "Too broad"},
    {"type": "ADD_CRITERION", "target": None, "note": "must add unit test for the null path"},
    {"type": "SCOPE_LIMIT", "target": "assess/reviewer.py", "note": None},
    {"type": "APPROACH_DIRECTIVE", "target": None, "note": "do not change the public API"},
]


# STAGE 1 — Export shape validation ------------------------------------------

def test_pipeline_stage1_decision_export_validates():
    """DecisionBatch validates a well-formed decisions list and rejects invalid ones."""
    valid = [{
        "finding_id": "F2-aaa", "td_id": "TD-0001",
        "decision": "APPROVE_WITH_CONDITIONS",
        "conditions": "Require change_0; Exclude change_1",
        "conditions_structured": _FULL_CONDITIONS,
        "rationale": None, "conflict": False,
    }]
    batch = DecisionBatch.from_list(valid)
    assert len(batch.decisions) == 1
    dec = batch.decisions[0]
    assert dec.decision == "APPROVE_WITH_CONDITIONS"
    assert len(dec.conditions_structured) == 5
    types = [c.type for c in dec.conditions_structured]
    for t in ("REQUIRE_CHANGE", "EXCLUDE_CHANGE", "ADD_CRITERION", "SCOPE_LIMIT", "APPROACH_DIRECTIVE"):
        assert t in types

    with pytest.raises((ValidationError, Exception)):
        DecisionBatch.from_list([{"finding_id": "F2-bbb", "decision": "INVALID"}])


# STAGE 2 — triage --batch write-through (real CLI) --------------------------

def test_pipeline_stage2_batch_writes_conditions(tmp_path):
    """`triage --batch` applies the decision and writes conditions_structured
    through to tech_debt.json (AWC collapses to APPROVE for human_triage)."""
    state_dir = f"{tmp_path}/state"
    os.makedirs(state_dir, exist_ok=True)
    tech_debt = [{
        "id": "TD-0001", "finding_id": "F2-aaa", "module": "sentinel:assess",
        "severity": "P1", "skill": "layer2a", "description": "test finding",
        "suggested_fix": "", "effort_estimate": "S", "risk_of_deferral": "",
        "human_triage": None, "status": "pending",
    }]
    td_path = tmp_path / "state" / "tech_debt.json"
    td_path.write_text(json.dumps(tech_debt), encoding="utf-8")

    decisions = [{
        "finding_id": "F2-aaa", "td_id": "TD-0001",
        "decision": "APPROVE_WITH_CONDITIONS", "conditions": "Require change_0",
        "conditions_structured": [
            {"type": "REQUIRE_CHANGE", "target": "change_0", "note": "Add null check"},
            {"type": "SCOPE_LIMIT", "target": "assess/reviewer.py", "note": None},
        ],
        "rationale": None, "conflict": False,
    }]
    dec_path = tmp_path / "decisions.json"
    dec_path.write_text(json.dumps(decisions), encoding="utf-8")

    result = CliRunner().invoke(
        main, ["triage", "--batch", str(dec_path), "--state-dir", state_dir]
    )
    assert result.exit_code == 0, result.output

    item = json.loads(td_path.read_text(encoding="utf-8"))[0]
    assert item["human_triage"] == "APPROVE"
    cs = item["conditions_structured"]
    assert cs is not None and len(cs) == 2
    types = [c["type"] for c in cs]
    assert "REQUIRE_CHANGE" in types and "SCOPE_LIMIT" in types


# STAGE 3 — Threading join ----------------------------------------------------

def test_pipeline_stage3_conditions_thread_to_task():
    """conditions thread from tech_debt findings to a Task via the pattern lineage
    (get_task_finding_ids) and merge (merge_conditions: EXCLUDE wins, union, intersect)."""
    pattern_to_group = {"P1": "G1"}
    group_members = {"G1": ["F2-aaa", "F2-bbb"]}
    fid_conditions = {
        "F2-aaa": [
            {"type": "REQUIRE_CHANGE", "target": "change_0", "note": "Add null check"},
            {"type": "SCOPE_LIMIT", "target": "assess/reviewer.py", "note": None},
        ],
        "F2-bbb": [
            {"type": "EXCLUDE_CHANGE", "target": "change_0", "note": "Too broad"},
            {"type": "ADD_CRITERION", "target": None, "note": "must add unit test"},
        ],
    }

    fids = get_task_finding_ids({"pattern_id": "P1"}, pattern_to_group, group_members)
    assert fids == ["F2-aaa", "F2-bbb"]

    merged = merge_conditions([fid_conditions[f] for f in fids])
    assert merged is not None
    types = [c["type"] for c in merged]
    assert "EXCLUDE_CHANGE" in types       # EXCLUDE wins over REQUIRE for change_0
    assert "REQUIRE_CHANGE" not in types
    assert "ADD_CRITERION" in types        # union from F2-bbb
    assert "SCOPE_LIMIT" in types          # from F2-aaa


# STAGE 4 — Architect constraint injection -----------------------------------

def test_pipeline_stage4_architect_sees_constraints():
    """format_constraints_for_architect produces the REMEDIATION CONSTRAINTS block
    that build_architect_todo_context injects into the Architect's context."""
    block = format_constraints_for_architect(_FULL_CONDITIONS)
    assert "REMEDIATION CONSTRAINTS" in block
    assert "REQUIRED" in block and "Add null check" in block
    assert "EXCLUDED" in block and "Too broad" in block
    assert "SCOPE LIMIT" in block and "assess/reviewer.py" in block
    assert "DIRECTIVE" in block and "do not change" in block

    assert format_constraints_for_architect(None) == ""
    assert format_constraints_for_architect([]) == ""


# STAGE 5a — QA effective criteria -------------------------------------------

def test_pipeline_stage5a_qa_criteria():
    """get_effective_criteria adds REQUIRE/ADD_CRITERION conditions to the QA
    acceptance-criteria benchmark, deduped."""
    class MockTask:
        acceptance_criteria = ["existing criterion"]
        conditions_structured = [
            {"type": "ADD_CRITERION", "target": None, "note": "must add unit test for null path"},
            {"type": "REQUIRE_CRITERION", "target": None, "note": "no regressions in existing tests"},
            {"type": "SCOPE_LIMIT", "target": "assess/reviewer.py", "note": None},
        ]

    criteria = get_effective_criteria(MockTask())
    assert len(criteria) == 3
    assert "existing criterion" in criteria
    assert "must add unit test for null path" in criteria
    assert "no regressions in existing tests" in criteria


# STAGE 5b — SCOPE_LIMIT enforcement -----------------------------------------

def test_pipeline_stage5b_scope_gate():
    """check_scope_limit passes in-scope changes and flags out-of-scope files."""
    conditions = [{"type": "SCOPE_LIMIT", "target": "assess/reviewer.py", "note": None}]
    assert check_scope_limit(conditions, ["assess/reviewer.py"]) == []

    violations = check_scope_limit(conditions, ["assess/reviewer.py", "orchestrator/main.py"])
    assert len(violations) == 1
    assert "orchestrator/main.py" in violations[0]

    assert check_scope_limit(None, ["any/file.py"]) == []


# COMBINED — full narrative ---------------------------------------------------

def test_pipeline_full_narrative():
    """Trace one finding's conditions through every stage and verify the correct
    artifact at each. Reads as the story of a finding through governance."""
    raw_decision = {
        "finding_id": "F2-aaa", "td_id": "TD-0001",
        "decision": "APPROVE_WITH_CONDITIONS",
        "conditions": "Require change_0; Scope to reviewer.py",
        "conditions_structured": [
            {"type": "REQUIRE_CHANGE", "target": "change_0",
             "note": "Add null check before accessing finding_kind"},
            {"type": "ADD_CRITERION", "target": None,
             "note": "unit test covers None finding_kind input"},
            {"type": "SCOPE_LIMIT", "target": "assess/reviewer.py", "note": None},
        ],
        "rationale": None, "conflict": False,
    }

    # Stage 1: validates cleanly
    batch = DecisionBatch.from_list([raw_decision])
    assert batch.decisions[0].decision == "APPROVE_WITH_CONDITIONS"

    # Stage 3: threads to one task (single finding -> conditions pass through)
    merged = merge_conditions([raw_decision["conditions_structured"]])
    assert merged is not None and len(merged) == 3

    # Stage 4: Architect sees the block
    block = format_constraints_for_architect(merged)
    assert "REQUIRED" in block and "Add null check" in block

    # Stage 5a: QA gains the added criterion
    class MockTask:
        acceptance_criteria = []
        conditions_structured = merged

    criteria = get_effective_criteria(MockTask())
    assert "unit test covers None finding_kind input" in criteria

    # Stage 5b: in-scope passes, out-of-scope fails
    assert check_scope_limit(merged, ["assess/reviewer.py"]) == []
    violations = check_scope_limit(merged, ["assess/reviewer.py", "orchestrator/main.py"])
    assert len(violations) == 1
