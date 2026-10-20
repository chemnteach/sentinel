"""Tests for Phase 1d persistent status tracking (StatusManager /
finding_status.json). Fixture-driven — no reads from D:/Sentinel-Runs/."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import sentinel.config as config_module
from sentinel.assess.finding_status import StatusManager, StatusValue
from sentinel.cli import main


@pytest.fixture(autouse=True)
def _reset_config_singleton():
    config_module._config = None
    yield
    config_module._config = None


def _td(fid, skill, sev="P1", triage=None, conditions=""):
    return {
        "id": f"TD-{fid}",
        "finding_id": fid,
        "severity": sev,
        "module": f"mod:{fid}.py",
        "skill": skill,
        "description": f"desc {fid}",
        "human_triage": triage,
        "conditions": conditions,
        "conditions_structured": None,
        "rationale": "",
        "effort_estimate": "medium",
        "suggested_fix": f"fix {fid}",
        "status": "pending_triage",
    }


def _write_state(tmp_path, tech_debt, groups):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "tech_debt.json").write_text(json.dumps(tech_debt), encoding="utf-8")
    (state / "semantic_groups.json").write_text(json.dumps(groups), encoding="utf-8")
    return state


# 4 groups: g1 (2 members, canonical=quick-review), g2, g3, g4
_GROUPS = [
    {"group_id": "g1", "member_finding_ids": ["F-a", "F-b"], "member_count": 2, "severity": "P1"},
    {"group_id": "g2", "member_finding_ids": ["F-c"], "member_count": 1, "severity": "P2"},
    {"group_id": "g3", "member_finding_ids": ["F-d"], "member_count": 1, "severity": "P1"},
    {"group_id": "g4", "member_finding_ids": ["F-e"], "member_count": 1, "severity": "Critical"},
]


def _untriaged_td():
    return [
        _td("F-a", "layer2a-quick-review"),
        _td("F-b", "layer2b-correctness-specialist"),
        _td("F-c", "layer2c-security-specialist", sev="P2"),
        _td("F-d", "layer3d-error-handling-specialist"),
        _td("F-e", "layer2a-quick-review", sev="Critical"),
    ]


def _triaged_td():
    return [
        _td("F-a", "layer2a-quick-review", triage="APPROVE"),
        _td("F-b", "layer2b-correctness-specialist", triage="APPROVE"),
        _td("F-c", "layer2c-security-specialist", sev="P2", triage="DEFER"),
        _td("F-d", "layer3d-error-handling-specialist", triage="EXCLUDE"),
        _td("F-e", "layer2a-quick-review", sev="Critical",
            triage="APPROVE_WITH_CONDITIONS", conditions="Public API frozen"),
    ]


def _mgr(state):
    return StatusManager(
        state, state / "semantic_groups.json", state / "tech_debt.json"
    )


# 1. initializes all UNTRIAGED when no prior triage
def test_init_all_untriaged_without_prior_triage(tmp_path):
    state = _write_state(tmp_path, _untriaged_td(), _GROUPS)
    mgr = _mgr(state)
    assert len(mgr.get_summary()) >= 1
    assert all(r.status == StatusValue.UNTRIAGED for r in mgr.get_by_status(StatusValue.UNTRIAGED))
    assert mgr.get_summary()["UNTRIAGED"] == 4
    assert mgr.get_summary()["total"] == 4


# 2. bootstrap_from_triage maps all four decision values correctly
def test_bootstrap_from_triage_maps_all_decisions(tmp_path):
    state = _write_state(tmp_path, _triaged_td(), _GROUPS)
    mgr = _mgr(state)
    n = mgr.bootstrap_from_triage(state / "tech_debt.json")
    assert n == 4  # all four groups carry a real decision
    assert mgr.get("g1").status == StatusValue.APPROVED
    assert mgr.get("g2").status == StatusValue.DEFERRED
    assert mgr.get("g3").status == StatusValue.EXCLUDE
    assert mgr.get("g4").status == StatusValue.APPROVED_WITH_CONDITIONS
    assert mgr.get("g4").approval_type == "APPROVE_WITH_CONDITIONS"
    assert mgr.get("g4").conditions == "Public API frozen"


# 3. update() persists across a fresh StatusManager instantiation
def test_update_persists_across_reload(tmp_path):
    state = _write_state(tmp_path, _untriaged_td(), _GROUPS)
    _mgr(state).update("g1", StatusValue.COMPLETE, task_id="task-1")
    reloaded = _mgr(state)
    assert reloaded.get("g1").status == StatusValue.COMPLETE
    assert reloaded.get("g1").task_id == "task-1"


# 4. bulk_update() updates multiple groups and returns correct count
def test_bulk_update_returns_count(tmp_path):
    state = _write_state(tmp_path, _untriaged_td(), _GROUPS)
    mgr = _mgr(state)
    n = mgr.bulk_update(["g1", "g2", "does-not-exist"], StatusValue.QUEUED)
    assert n == 2
    assert mgr.get("g1").status == StatusValue.QUEUED
    assert mgr.get("g2").status == StatusValue.QUEUED


# 5. get_by_status() returns only matching groups
def test_get_by_status_filters(tmp_path):
    state = _write_state(tmp_path, _untriaged_td(), _GROUPS)
    mgr = _mgr(state)
    mgr.update("g1", StatusValue.APPROVED)
    mgr.update("g2", StatusValue.APPROVED)
    mgr.update("g3", StatusValue.DEFERRED)
    approved = mgr.get_by_status(StatusValue.APPROVED)
    assert {r.group_id for r in approved} == {"g1", "g2"}


# 6. get_summary() counts are accurate
def test_get_summary_counts(tmp_path):
    state = _write_state(tmp_path, _triaged_td(), _GROUPS)
    mgr = _mgr(state)  # __init__ inherits triage -> APPROVED/DEFERRED/EXCLUDE/AWC
    summary = mgr.get_summary()
    assert summary["APPROVED"] == 1
    assert summary["DEFERRED"] == 1
    assert summary["EXCLUDE"] == 1
    assert summary["APPROVED_WITH_CONDITIONS"] == 1
    assert summary["total"] == 4
    assert sum(summary[k] for k in summary if k != "total") == summary["total"]


# 7. get_resume_queue() returns only APPROVED groups not yet in a batch
def test_get_resume_queue(tmp_path):
    state = _write_state(tmp_path, _untriaged_td(), _GROUPS)
    mgr = _mgr(state)
    mgr.update("g1", StatusValue.APPROVED)
    mgr.update("g3", StatusValue.APPROVED)
    mgr.update("g3", StatusValue.QUEUED)  # advanced into a batch — no longer resumable
    queue = mgr.get_resume_queue()
    assert {r.group_id for r in queue} == {"g1"}


# 8. sentinel status command displays the summary correctly
def test_status_command_displays_summary(tmp_path):
    state = _write_state(tmp_path, _triaged_td(), _GROUPS)
    r = CliRunner().invoke(main, ["status", "--state-dir", str(state)])
    assert r.exit_code == 0, r.output
    assert "=== SENTINEL STATUS ===" in r.output
    assert "APPROVED:" in r.output
    assert "Total:" in r.output
    assert "Ready to queue:" in r.output


# 9. get_resume_queue() includes APPROVED_WITH_CONDITIONS groups (FIX 1)
def test_get_resume_queue_includes_approved_with_conditions(tmp_path):
    state = _write_state(tmp_path, _untriaged_td(), _GROUPS)
    mgr = _mgr(state)
    mgr.update("g1", StatusValue.APPROVED)
    mgr.update("g4", StatusValue.APPROVED_WITH_CONDITIONS)
    mgr.update("g3", StatusValue.APPROVED)
    mgr.update("g3", StatusValue.QUEUED)  # advanced into a batch — no longer resumable
    queue = mgr.get_resume_queue()
    assert {r.group_id for r in queue} == {"g1", "g4"}


# 10. batch triage syncs finding_status.json for every decision (FIX 2)
def test_batch_triage_syncs_finding_status(tmp_path):
    state = _write_state(tmp_path, _untriaged_td(), _GROUPS)
    decisions = tmp_path / "d.json"
    decisions.write_text(
        json.dumps(
            [
                {"finding_id": "F-a", "decision": "APPROVE"},   # -> g1
                {"finding_id": "F-c", "decision": "DEFER"},     # -> g2
            ]
        ),
        encoding="utf-8",
    )
    r = CliRunner().invoke(main, ["triage", "--batch", str(decisions), "--state-dir", str(state)])
    assert r.exit_code == 0, r.output

    status_file = state / "finding_status.json"
    assert status_file.exists()
    by_gid = {rec["group_id"]: rec for rec in json.loads(status_file.read_text(encoding="utf-8"))}
    assert by_gid["g1"]["status"] == StatusValue.APPROVED
    assert by_gid["g2"]["status"] == StatusValue.DEFERRED
    # groups with no batch decision stay UNTRIAGED
    assert by_gid["g3"]["status"] == StatusValue.UNTRIAGED
    assert by_gid["g4"]["status"] == StatusValue.UNTRIAGED
