"""Tests for the group-aware triage view (Phase 1b).

Fixture-driven — no reads from D:/Sentinel-Runs/. The fixtures are minimal dicts
that mirror the tech_debt.json / semantic_groups.json schemas: the join key is
``group.member_finding_ids`` (finding ids) -> ``tech_debt.finding_id``.
"""

from __future__ import annotations

import json

import pytest

from click.testing import CliRunner

from sentinel.assess.grouper_view import (
    apply_group_triage_decision,
    generate_grouped_triage_view,
    merge_groups,
)
from sentinel.cli import main


def _td(fid, skill, sev="P1", triage=None, module=None, **extra):
    """Minimal tech_debt item."""
    item = {
        "id": f"TD-{fid}",
        "finding_id": fid,
        "human_triage": triage,
        "conditions_structured": None,
        "conditions": "",
        "rationale": "",
        "description": f"desc {fid}",
        "effort_estimate": "medium",
        "module": module or f"mod:{fid}.py",
        "severity": sev,
        "skill": skill,
        "status": "pending_triage",
        "suggested_fix": f"fix {fid}",
    }
    item.update(extra)
    return item


def _grp(gid, fids, sev="P1"):
    """Minimal semantic group."""
    return {
        "group_id": gid,
        "member_finding_ids": list(fids),
        "member_count": len(fids),
        "severity": sev,
        "file": "x.py",
    }


@pytest.fixture
def state(tmp_path):
    tech_debt = [
        _td("F-a", "layer2b-correctness-specialist", sev="P2"),
        _td("F-b", "layer2a-quick-review", sev="P1", triage="APPROVE", module="mod:b.py"),
        _td("F-c", "layer2c-security-specialist", sev="P1", triage="APPROVE", module="mod:c.py"),
        _td("F-d", "layer2b-correctness-specialist", sev="P1", triage="APPROVE", module="mod:b.py"),
        _td("F-e", "layer3d-error-handling-specialist", sev="P1", triage=None),
        _td("F-f", "layer4a-code-smell-detector", sev="P1", triage=None),
    ]
    groups = [
        _grp("g1", ["F-a"], sev="P2"),                # single-member
        _grp("g2", ["F-b", "F-c", "F-d"], sev="P1"),  # multi; canonical = quick-review (F-b)
        _grp("g3", ["F-e", "F-f"], sev="P1"),         # multi; untriaged
    ]
    td_path = tmp_path / "tech_debt.json"
    sg_path = tmp_path / "semantic_groups.json"
    td_path.write_text(json.dumps(tech_debt), encoding="utf-8")
    sg_path.write_text(json.dumps(groups), encoding="utf-8")
    return td_path, sg_path


def _by_gid(view):
    return {e["group_id"]: e for e in view}


# 1. one entry per group (not per finding)
def test_one_entry_per_group(state):
    td_path, sg_path = state
    view = generate_grouped_triage_view(td_path, sg_path)
    assert len(view) == 3  # 3 groups, though they cover 6 findings
    assert {e["group_id"] for e in view} == {"g1", "g2", "g3"}


# 2. single-member group looks identical to a plain finding
def test_single_member_matches_plain_finding(state):
    td_path, sg_path = state
    e = _by_gid(generate_grouped_triage_view(td_path, sg_path))["g1"]
    assert e["member_count"] == 1
    assert e["member_finding_ids"] == ["F-a"]
    assert e["canonical_finding_id"] == "F-a"
    assert e["description"] == "desc F-a"  # no corroboration note appended
    assert e["skill"] == "layer2b-correctness-specialist"
    assert e["suggested_fix"] == "fix F-a"


# 3. multi-member group includes all member_finding_ids and correct member_count
def test_multi_member_ids_and_count(state):
    td_path, sg_path = state
    e = _by_gid(generate_grouped_triage_view(td_path, sg_path))["g2"]
    assert e["member_count"] == 3
    assert set(e["member_finding_ids"]) == {"F-b", "F-c", "F-d"}


# 4. skills_corroborating lists all unique skills across member findings
def test_skills_corroborating_unique(state):
    td_path, sg_path = state
    e = _by_gid(generate_grouped_triage_view(td_path, sg_path))["g2"]
    assert e["skills_corroborating"] == sorted(
        {
            "layer2a-quick-review",
            "layer2c-security-specialist",
            "layer2b-correctness-specialist",
        }
    )


# 5. canonical emitter picks quick-review over correctness when both present
def test_canonical_prefers_quick_review_over_correctness(state):
    td_path, sg_path = state
    e = _by_gid(generate_grouped_triage_view(td_path, sg_path))["g2"]
    assert e["canonical_finding_id"] == "F-b"
    assert e["skill"] == "layer2a-quick-review"


# 6. human_triage inherited from canonical finding when already set
def test_human_triage_inherited_from_canonical(state):
    td_path, sg_path = state
    e = _by_gid(generate_grouped_triage_view(td_path, sg_path))["g2"]
    assert e["human_triage"] == "APPROVE"  # canonical F-b is APPROVE


# 7. human_triage is None when no member has been triaged
def test_human_triage_none_when_untriaged(state):
    td_path, sg_path = state
    e = _by_gid(generate_grouped_triage_view(td_path, sg_path))["g3"]
    assert e["human_triage"] is None


# 8. apply_group_triage_decision updates ALL member findings in tech_debt.json
def test_apply_updates_all_members(state):
    td_path, sg_path = state
    n = apply_group_triage_decision("g2", "DEFER", "later", "", td_path, sg_path)
    assert n == 3
    by = {i["finding_id"]: i for i in json.loads(td_path.read_text(encoding="utf-8"))}
    for fid in ("F-b", "F-c", "F-d"):
        assert by[fid]["human_triage"] == "DEFER"
        assert by[fid]["rationale"] == "later"
    # a finding outside the group is untouched
    assert by["F-a"]["human_triage"] is None


# 9. APPROVE_WITH_CONDITIONS sets conditions_structured on all members
def test_apply_awc_sets_conditions_structured_on_all(state):
    td_path, sg_path = state
    n = apply_group_triage_decision(
        "g2", "APPROVE_WITH_CONDITIONS", "ok", "Keep scope to file X", td_path, sg_path
    )
    assert n == 3
    by = {i["finding_id"]: i for i in json.loads(td_path.read_text(encoding="utf-8"))}
    for fid in ("F-b", "F-c", "F-d"):
        assert by[fid]["human_triage"] == "APPROVE_WITH_CONDITIONS"
        assert by[fid]["conditions"] == "Keep scope to file X"
        cs = by[fid]["conditions_structured"]
        assert isinstance(cs, list) and len(cs) == 1
        assert cs[0]["type"] == "APPROACH_DIRECTIVE"
        assert cs[0]["note"] == "Keep scope to file X"


# 10. apply_group_triage_decision returns the correct count of findings updated
def test_apply_returns_count(state):
    td_path, sg_path = state
    assert apply_group_triage_decision("g1", "APPROVE", "", "", td_path, sg_path) == 1
    assert apply_group_triage_decision("g3", "EXCLUDE", "", "", td_path, sg_path) == 2


# ---------------------------------------------------------------------------
# Phase 1c — near-miss surfacing + merge_groups
# ---------------------------------------------------------------------------


@pytest.fixture
def two_groups(tmp_path):
    """Two single-member groups A and B, ready to be merged."""
    td_path = tmp_path / "tech_debt.json"
    sg_path = tmp_path / "semantic_groups.json"
    td_path.write_text(
        json.dumps(
            [
                _td("F-a", "layer2a-quick-review", sev="P1"),
                _td("F-b", "layer2b-correctness-specialist", sev="P2"),
            ]
        ),
        encoding="utf-8",
    )
    sg_path.write_text(
        json.dumps(
            [
                {"group_id": "A", "member_finding_ids": ["F-a"], "member_count": 1, "severity": "P1"},
                {"group_id": "B", "member_finding_ids": ["F-b"], "member_count": 1, "severity": "P2"},
            ]
        ),
        encoding="utf-8",
    )
    return td_path, sg_path


def _seed_cli_near_miss(tmp_path):
    """State dir with 3 single-member groups and one near-miss pair (F-a, F-b)."""
    state = tmp_path / "state"
    state.mkdir()
    tech_debt = [
        _td("F-a", "layer2a-quick-review", sev="P1"),
        _td("F-b", "layer2b-correctness-specialist", sev="P1"),
        _td("F-c", "layer2c-security-specialist", sev="P2"),
    ]
    groups = [
        {"group_id": "g-a", "member_finding_ids": ["F-a"], "member_count": 1, "severity": "P1", "file": "a.py"},
        {"group_id": "g-b", "member_finding_ids": ["F-b"], "member_count": 1, "severity": "P1", "file": "a.py"},
        {"group_id": "g-c", "member_finding_ids": ["F-c"], "member_count": 1, "severity": "P2", "file": "c.py"},
    ]
    near = [
        {"finding_id_a": "F-a", "finding_id_b": "F-b", "file": "a.py",
         "similarity": 0.7421, "description_a": "alpha desc", "description_b": "alpha-ish desc"},
    ]
    (state / "tech_debt.json").write_text(json.dumps(tech_debt), encoding="utf-8")
    (state / "semantic_groups.json").write_text(json.dumps(groups), encoding="utf-8")
    (state / "semantic_groups_near_misses.json").write_text(json.dumps(near), encoding="utf-8")
    return str(state)


# 1. merge_groups combines member lists correctly
def test_merge_groups_combines_member_lists(two_groups):
    td_path, sg_path = two_groups
    survivor = merge_groups("A", "B", sg_path, td_path)
    assert survivor == "A"
    groups = json.loads(sg_path.read_text(encoding="utf-8"))
    a = next(g for g in groups if g["group_id"] == "A")
    assert set(a["member_finding_ids"]) == {"F-a", "F-b"}
    assert a["member_count"] == 2


# 2. merge_groups removes the merged group
def test_merge_groups_removes_merged_group(two_groups):
    td_path, sg_path = two_groups
    merge_groups("A", "B", sg_path, td_path)
    groups = json.loads(sg_path.read_text(encoding="utf-8"))
    assert all(g["group_id"] != "B" for g in groups)
    assert len(groups) == 1


# 3. merge_groups updates tech_debt.json for all moved members
def test_merge_groups_updates_tech_debt_for_moved_members(two_groups):
    td_path, sg_path = two_groups
    merge_groups("A", "B", sg_path, td_path)
    by = {i["finding_id"]: i for i in json.loads(td_path.read_text(encoding="utf-8"))}
    assert by["F-b"]["group_id"] == "A"  # moved member now references A


# 3b. merge_groups rolls back the first (semantic_groups) write if the second
# (tech_debt) write fails — the two-file update is all-or-nothing.
def test_merge_groups_rolls_back_semantic_groups_when_tech_debt_write_fails(
    two_groups, monkeypatch
):
    import sentinel.assess.grouper_view as gv

    td_path, sg_path = two_groups
    orig = gv._atomic_write_json
    calls = {"n": 0}

    def failing(path, data):
        calls["n"] += 1
        if calls["n"] == 2:  # the tech_debt write (second write in the merge)
            raise OSError("simulated disk full on tech_debt write")
        return orig(path, data)

    monkeypatch.setattr(gv, "_atomic_write_json", failing)
    with pytest.raises(OSError):
        gv.merge_groups("A", "B", sg_path, td_path)

    # semantic_groups must be restored — group B is still present, not merged away.
    groups = json.loads(sg_path.read_text(encoding="utf-8"))
    assert len(groups) == 2
    assert any(g["group_id"] == "B" for g in groups)


# 3c. A concurrent merge and triage decision must not lose each other's writes
# to the shared tech_debt.json (both run under the same triage lock).
def test_merge_and_triage_decision_no_lost_update(two_groups):
    import threading

    td_path, sg_path = two_groups
    barrier = threading.Barrier(2)
    errors: list = []

    def do_merge():
        try:
            barrier.wait()
            merge_groups("A", "B", sg_path, td_path)
        except Exception as exc:  # noqa: BLE001
            errors.append(("merge", exc))

    def do_triage():
        try:
            barrier.wait()
            apply_group_triage_decision("A", "APPROVE", "", "", td_path, sg_path)
        except Exception as exc:  # noqa: BLE001
            errors.append(("triage", exc))

    t1 = threading.Thread(target=do_merge)
    t2 = threading.Thread(target=do_triage)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert not errors, errors
    td = {i["finding_id"]: i for i in json.loads(td_path.read_text(encoding="utf-8"))}
    assert td["F-b"]["group_id"] == "A"  # the merge's group_id stamp survived
    assert td["F-a"]["human_triage"] == "APPROVE"  # the triage decision survived


# 4. --show-near-misses displays pairs with similarity scores
def test_show_near_misses_displays_similarity(tmp_path):
    state = _seed_cli_near_miss(tmp_path)
    r = CliRunner().invoke(
        main, ["triage", "--show-near-misses", "--state-dir", state], input="K\n"
    )
    assert r.exit_code == 0, r.output
    assert "Possible duplicates" in r.output
    assert "0.7421" in r.output


# 5. Merging a near-miss pair via the CLI results in a single group
def test_cli_merge_near_miss_results_in_single_group(tmp_path):
    state = _seed_cli_near_miss(tmp_path)
    r = CliRunner().invoke(
        main, ["triage", "--show-near-misses", "--state-dir", state], input="M\n"
    )
    assert r.exit_code == 0, r.output
    groups = json.loads((tmp_path / "state" / "semantic_groups.json").read_text(encoding="utf-8"))
    gmap = {}
    for g in groups:
        for fid in g["member_finding_ids"]:
            gmap[fid] = g["group_id"]
    assert gmap["F-a"] == gmap["F-b"]  # now in one group
    assert len(groups) == 2  # was 3
