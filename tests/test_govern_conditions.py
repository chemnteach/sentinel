"""Tests for structured triage conditions (sentinel/govern/conditions.py) and
their validation at the triage --batch boundary (cycle 52)."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from sentinel.cli import main
from sentinel.govern.conditions import (
    ConditionItem,
    DecisionBatch,
    FindingDecision,
    check_scope_limit,
    format_constraints_for_architect,
    get_changed_files_from_git,
    get_effective_criteria,
    get_task_finding_ids,
    merge_conditions,
)


def test_valid_approve_parses():
    fd = FindingDecision(finding_id="F2-x", decision="APPROVE")
    assert fd.decision == "APPROVE"
    assert fd.conditions_structured is None


def test_awc_requires_conditions():
    with pytest.raises(ValidationError):
        FindingDecision(
            finding_id="F2-x",
            decision="APPROVE_WITH_CONDITIONS",
            conditions_structured=None,
        )


def test_awc_with_conditions_valid():
    fd = FindingDecision(
        finding_id="F2-x",
        decision="APPROVE_WITH_CONDITIONS",
        conditions_structured=[ConditionItem(type="REQUIRE_CHANGE", target="change_0", note="do it")],
    )
    assert len(fd.conditions_structured) == 1


def test_condition_item_note_truncated():
    item = ConditionItem(type="APPROACH_DIRECTIVE", note="x" * 600)
    assert len(item.note) == 500


def test_batch_from_list_invalid_decision():
    with pytest.raises(ValidationError):
        DecisionBatch.from_list([{"finding_id": "F2-x", "decision": "INVALID"}])


def _seed_tech_debt(tmp_path):
    import os
    state_dir = f"{tmp_path}/state"
    os.makedirs(state_dir, exist_ok=True)
    with open(f"{state_dir}/tech_debt.json", "w", encoding="utf-8") as f:
        json.dump([{"id": "TD-0001", "severity": "P1", "description": "a", "human_triage": None}], f)
    return state_dir


def test_batch_apply_writes_conditions_structured(tmp_path):
    runner = CliRunner()
    state_dir = _seed_tech_debt(tmp_path)
    decisions_path = f"{tmp_path}/decisions.json"
    with open(decisions_path, "w", encoding="utf-8") as f:
        json.dump([{
            "finding_id": "F2-a", "td_id": "TD-0001",
            "decision": "APPROVE_WITH_CONDITIONS",
            "conditions": "Require change_0",
            "conditions_structured": [
                {"type": "REQUIRE_CHANGE", "target": "change_0", "note": "Refactor the wrapper"}
            ],
            "rationale": None, "conflict": False,
        }], f)

    result = runner.invoke(main, ["triage", "--batch", decisions_path, "--state-dir", state_dir])

    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "state" / "tech_debt.json").read_text(encoding="utf-8"))
    item = data[0]
    assert item["human_triage"] == "APPROVE"   # AWC collapses to APPROVE
    assert item["conditions_structured"] == [
        {"type": "REQUIRE_CHANGE", "target": "change_0", "note": "Refactor the wrapper"}
    ]


# --- cycle 52 C3 — threading helpers ---------------------------------------

def test_merge_conditions_single_returns_it():
    cl = [{"type": "REQUIRE_CHANGE", "target": "change_0", "note": "x"}]
    assert merge_conditions([cl]) == cl


def test_merge_conditions_empty_returns_none():
    assert merge_conditions([]) is None
    assert merge_conditions([None, None]) is None


def test_merge_conditions_exclude_wins():
    a = [{"type": "REQUIRE_CHANGE", "target": "change_0", "note": "r"}]
    b = [{"type": "EXCLUDE_CHANGE", "target": "change_0", "note": "e"}]
    merged = merge_conditions([a, b])
    changes = [it for it in merged if it["target"] == "change_0"]
    assert len(changes) == 1
    assert changes[0]["type"] == "EXCLUDE_CHANGE"


def test_merge_conditions_criteria_union():
    a = [{"type": "REQUIRE_CRITERION", "target": "criterion_0", "note": "crit A"}]
    b = [{"type": "REQUIRE_CRITERION", "target": "criterion_1", "note": "crit B"}]
    merged = merge_conditions([a, b])
    notes = {it["note"] for it in merged if it["type"] == "REQUIRE_CRITERION"}
    assert notes == {"crit A", "crit B"}


def test_merge_conditions_scope_intersection():
    a = [{"type": "SCOPE_LIMIT", "target": "a.py,b.py", "note": None}]
    b = [{"type": "SCOPE_LIMIT", "target": "b.py,c.py", "note": None}]
    merged = merge_conditions([a, b])
    scope = next(it for it in merged if it["type"] == "SCOPE_LIMIT")
    assert scope["target"] == "b.py"


def test_get_task_finding_ids_via_pattern_lineage():
    task = {"pattern_id": "P1"}
    pattern_to_group = {"P1": "G1"}
    group_members = {"G1": ["F2-a", "F2-b"]}
    assert get_task_finding_ids(task, pattern_to_group, group_members) == ["F2-a", "F2-b"]
    # no pattern lineage -> empty
    assert get_task_finding_ids({"pattern_id": ""}, pattern_to_group, group_members) == []


# --- cycle 52 C4 — Architect constraint formatting + injection --------------

def test_format_constraints_empty():
    assert format_constraints_for_architect(None) == ""
    assert format_constraints_for_architect([]) == ""


def test_format_constraints_require_change():
    out = format_constraints_for_architect([{"type": "REQUIRE_CHANGE", "target": "change_0", "note": "do X"}])
    assert "REQUIRED: Include" in out


def test_format_constraints_exclude_change():
    out = format_constraints_for_architect([{"type": "EXCLUDE_CHANGE", "target": "change_1", "note": "no Y"}])
    assert "EXCLUDED: Do NOT implement" in out


def test_format_constraints_scope_limit():
    out = format_constraints_for_architect([{"type": "SCOPE_LIMIT", "target": "a.py", "note": None}])
    assert "SCOPE LIMIT" in out


def test_format_constraints_all_types():
    conds = [
        {"type": "REQUIRE_CHANGE", "target": "change_0", "note": "a"},
        {"type": "EXCLUDE_CHANGE", "target": "change_1", "note": "b"},
        {"type": "REQUIRE_CRITERION", "target": "criterion_0", "note": "c"},
        {"type": "ADD_CRITERION", "target": None, "note": "d"},
        {"type": "SCOPE_LIMIT", "target": "x.py", "note": None},
        {"type": "APPROACH_DIRECTIVE", "target": None, "note": "e"},
    ]
    out = format_constraints_for_architect(conds)
    for label in ("REQUIRED: Include", "EXCLUDED: Do NOT", "REQUIRED CRITERION",
                  "ADDITIONAL CRITERION", "SCOPE LIMIT", "DIRECTIVE"):
        assert label in out


def test_architect_injects_constraints():
    from sentinel.orchestrator import todo_schema as ts
    from sentinel.orchestrator.task_manager import Task, TaskManager
    task = Task(
        task_id="task-1", plan_id="p1", description="d",
        acceptance_criteria=["c1"], scope_boundary="mod",
        conditions_structured=[{"type": "REQUIRE_CHANGE", "target": "change_0", "note": "do X"}],
    )
    stub = ts.TestStub(name="test_x", acceptance_criterion="c1", scenario="s",
                       given="g", when="w", then="t", stub_code="def test_x():\n    assert True")
    todo = ts.TodoSubmission(
        task_id="task-1",
        implementation_plan=ts.ImplementationPlan(summary="s", files_to_modify=["a.py"], approach="ap"),
        test_stubs=ts.TestStubSection(test_file="t.py", stubs=[stub], coverage_summary="cov"),
    )
    ctx = TaskManager(None).build_architect_todo_context(task, todo)
    assert "REMEDIATION CONSTRAINTS" in ctx
    assert "do X" in ctx


# --- cycle 52 C5 — QA effective criteria + SCOPE_LIMIT ----------------------

def test_qa_effective_criteria_base_only():
    task = {"acceptance_criteria": ["a", "b"], "conditions_structured": None}
    assert get_effective_criteria(task) == ["a", "b"]


def test_qa_effective_criteria_adds_from_conditions():
    task = {
        "acceptance_criteria": ["base"],
        "conditions_structured": [{"type": "ADD_CRITERION", "target": None, "note": "added crit"}],
    }
    assert get_effective_criteria(task) == ["base", "added crit"]


def test_qa_effective_criteria_no_dupes():
    task = {
        "acceptance_criteria": ["same crit"],
        "conditions_structured": [{"type": "REQUIRE_CRITERION", "target": "criterion_0", "note": "same crit"}],
    }
    assert get_effective_criteria(task) == ["same crit"]


def test_check_scope_limit_no_conditions():
    assert check_scope_limit(None, ["foo.py"]) == []
    assert check_scope_limit([], ["foo.py"]) == []


def test_check_scope_limit_within_scope():
    conds = [{"type": "SCOPE_LIMIT", "target": "foo.py", "note": None}]
    assert check_scope_limit(conds, ["foo.py"]) == []


def test_check_scope_limit_outside_scope():
    conds = [{"type": "SCOPE_LIMIT", "target": "foo.py", "note": None}]
    violations = check_scope_limit(conds, ["bar.py"])
    assert len(violations) == 1
    assert "bar.py" in violations[0]


def test_check_scope_limit_multiple_files():
    conds = [{"type": "SCOPE_LIMIT", "target": "foo.py,bar.py", "note": None}]
    violations = check_scope_limit(conds, ["foo.py", "baz.py"])
    assert len(violations) == 1
    assert "baz.py" in violations[0]


# --- cycle 55 — git-sourced changed files + path normalization --------------

def test_get_changed_files_from_git_no_repo():
    assert get_changed_files_from_git("/no/such/repo/path/xyz") == []


def test_get_changed_files_from_git_real_repo():
    out = get_changed_files_from_git("D:/Git/Sentinel")
    assert isinstance(out, list)
    assert all(isinstance(x, str) for x in out)


def test_check_scope_limit_normalizes_paths():
    conds = [{"type": "SCOPE_LIMIT", "target": "assess/reviewer.py", "note": None}]
    assert check_scope_limit(conds, ["sentinel/assess/reviewer.py"]) == []


def test_check_scope_limit_absolute_path():
    conds = [{"type": "SCOPE_LIMIT", "target": "assess/reviewer.py", "note": None}]
    assert check_scope_limit(conds, ["D:/Git/Sentinel/sentinel/assess/reviewer.py"]) == []


def test_check_scope_limit_backslash():
    conds = [{"type": "SCOPE_LIMIT", "target": "assess/reviewer.py", "note": None}]
    assert check_scope_limit(conds, ["sentinel\\assess\\reviewer.py"]) == []


def test_check_scope_limit_violation_after_normalize():
    conds = [{"type": "SCOPE_LIMIT", "target": "assess/reviewer.py", "note": None}]
    violations = check_scope_limit(
        conds, ["sentinel/assess/reviewer.py", "sentinel/orchestrator/main.py"]
    )
    assert len(violations) == 1
    assert "main.py" in violations[0]
