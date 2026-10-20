"""CLI tests for the group-aware triage command (Phase 1b step 2).

These fixtures seed BOTH tech_debt.json and semantic_groups.json, which is what
activates the grouped triage paths in ``sentinel triage`` (display + decision
propagation). Without semantic_groups.json the command keeps its per-finding
behavior (covered by tests/test_cli.py).
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from sentinel.cli import main


def _td(fid, tdid, skill, sev="P1", module="mod:x.py"):
    return {
        "id": tdid,
        "finding_id": fid,
        "severity": sev,
        "module": module,
        "skill": skill,
        "description": f"root cause {fid}",
        "human_triage": None,
        "conditions": "",
        "conditions_structured": None,
        "rationale": "",
        "effort_estimate": "medium",
        "suggested_fix": f"fix {fid}",
        "status": "pending_triage",
    }


def _seed(tmp_path) -> str:
    """Seed a state dir with 4 findings collapsed into 2 semantic groups."""
    state = tmp_path / "state"
    state.mkdir()
    tech_debt = [
        _td("F-a", "TD-0001", "layer2a-quick-review", sev="P1", module="mod:a.py"),
        _td("F-b", "TD-0002", "layer2b-correctness-specialist", sev="P1", module="mod:a.py"),
        _td("F-c", "TD-0003", "layer2c-security-specialist", sev="P2", module="mod:c.py"),
        _td("F-d", "TD-0004", "layer3d-error-handling-specialist", sev="P2", module="mod:c.py"),
    ]
    groups = [
        # canonical = F-a (quick-review); F-b is a member
        {"group_id": "grp-alpha", "member_finding_ids": ["F-a", "F-b"],
         "member_count": 2, "severity": "P1", "file": "a.py"},
        # canonical = F-c (security); F-d is a member
        {"group_id": "grp-beta", "member_finding_ids": ["F-c", "F-d"],
         "member_count": 2, "severity": "P2", "file": "c.py"},
    ]
    (state / "tech_debt.json").write_text(json.dumps(tech_debt), encoding="utf-8")
    (state / "semantic_groups.json").write_text(json.dumps(groups), encoding="utf-8")
    return str(state)


def _read_by_fid(tmp_path):
    data = json.loads((tmp_path / "state" / "tech_debt.json").read_text(encoding="utf-8"))
    return {i["finding_id"]: i for i in data}


# 1. grouped view shows N groups, not N findings, for a fixture with duplicates
def test_list_shows_groups_not_findings(tmp_path):
    state = _seed(tmp_path)
    r = CliRunner().invoke(main, ["triage", "--list", "--state-dir", state])
    assert r.exit_code == 0, r.output
    assert "2 group(s) covering 4 finding(s)" in r.output
    assert "grp-alpha" in r.output and "grp-beta" in r.output


# 2. APPROVE_WITH_CONDITIONS prompts for conditions text and stores it
def test_awc_prompts_and_stores_conditions(tmp_path):
    state = _seed(tmp_path)
    r = CliRunner().invoke(
        main,
        ["triage", "--approve-with-conditions", "grp-alpha", "--state-dir", state],
        input="Keep changes to a.py only\n",
    )
    assert r.exit_code == 0, r.output
    by = _read_by_fid(tmp_path)
    for fid in ("F-a", "F-b"):
        assert by[fid]["human_triage"] == "APPROVE_WITH_CONDITIONS"
        cs = by[fid]["conditions_structured"]
        assert isinstance(cs, list) and cs[0]["note"] == "Keep changes to a.py only"
    # the other group is untouched
    assert by["F-c"]["human_triage"] is None


# 3. batch triage applies to the whole group when the file references a canonical id
def test_batch_applies_to_group_via_canonical_id(tmp_path):
    state = _seed(tmp_path)
    decisions = tmp_path / "d.json"
    decisions.write_text(json.dumps([{"finding_id": "F-a", "decision": "APPROVE"}]), encoding="utf-8")
    r = CliRunner().invoke(main, ["triage", "--batch", str(decisions), "--state-dir", state])
    assert r.exit_code == 0, r.output
    by = _read_by_fid(tmp_path)
    assert by["F-a"]["human_triage"] == "APPROVE"
    assert by["F-b"]["human_triage"] == "APPROVE"  # propagated to the member
    assert by["F-c"]["human_triage"] is None


# 4. batch triage applies to the whole group when the file references a MEMBER id
#    (the non-canonical finding) — the look-up path
def test_batch_applies_to_group_via_member_id(tmp_path):
    state = _seed(tmp_path)
    decisions = tmp_path / "d.json"
    # F-d is the non-canonical member of grp-beta
    decisions.write_text(
        json.dumps([{"finding_id": "F-d", "decision": "DEFER", "rationale": "later"}]),
        encoding="utf-8",
    )
    r = CliRunner().invoke(main, ["triage", "--batch", str(decisions), "--state-dir", state])
    assert r.exit_code == 0, r.output
    by = _read_by_fid(tmp_path)
    assert by["F-c"]["human_triage"] == "DEFER"  # propagated to the canonical
    assert by["F-d"]["human_triage"] == "DEFER"
    assert by["F-c"]["rationale"] == "later"
    # the other group is untouched
    assert by["F-a"]["human_triage"] is None


# 4b. per-finding conditions in a batch land on the OWN finding only — the
#     decision still propagates to the group, but two members with DIFFERENT
#     conditions do not overwrite each other (Phase 2 reconciles them per task).
def test_batch_per_finding_conditions_no_cross_contamination(tmp_path):
    state = _seed(tmp_path)
    decisions = tmp_path / "d.json"
    decisions.write_text(
        json.dumps([
            {"finding_id": "F-a", "decision": "APPROVE_WITH_CONDITIONS",
             "conditions_structured": [
                 {"type": "REQUIRE_CHANGE", "target": "change_0", "note": "guard at a.py:10"}]},
            {"finding_id": "F-b", "decision": "APPROVE_WITH_CONDITIONS",
             "conditions_structured": [
                 {"type": "EXCLUDE_CHANGE", "target": "change_0", "note": "leave b as-is"}]},
        ]),
        encoding="utf-8",
    )
    r = CliRunner().invoke(main, ["triage", "--batch", str(decisions), "--state-dir", state])
    assert r.exit_code == 0, r.output
    by = _read_by_fid(tmp_path)
    # Decision propagated to both group members...
    assert by["F-a"]["human_triage"] == "APPROVE_WITH_CONDITIONS"
    assert by["F-b"]["human_triage"] == "APPROVE_WITH_CONDITIONS"
    # ...but each finding keeps its OWN conditions (no last-write-wins bleed).
    assert by["F-a"]["conditions_structured"][0]["type"] == "REQUIRE_CHANGE"
    assert by["F-b"]["conditions_structured"][0]["type"] == "EXCLUDE_CHANGE"


# 5. corroboration note appears for multi-member groups in the display
def test_corroboration_note_appears_for_multi_member_group(tmp_path):
    state = _seed(tmp_path)
    r = CliRunner().invoke(main, ["triage", "--list", "--state-dir", state])
    assert r.exit_code == 0, r.output
    assert "Corroborated by" in r.output
