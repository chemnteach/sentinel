"""Tests for S3-4 pattern-group deduplication in the Decision Brief.

A "pattern group" collapses rows that share the SAME remediation pattern_id and
span 3+ distinct modules into a single collapsible row, so the reviewer can
approve/defer the whole remediation pattern at once instead of per-row.
"""
from __future__ import annotations

import json

from sentinel.reports.decision_brief import _build_rollups, emit_html


def _write_state(tmp_path, findings, groups, patterns):
    (tmp_path / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
    (tmp_path / "semantic_groups.json").write_text(json.dumps(groups), encoding="utf-8")
    (tmp_path / "remediation_patterns.json").write_text(json.dumps(patterns), encoding="utf-8")
    return tmp_path


def _pattern_group_rows(ordered):
    return [r for _k, r in ordered if isinstance(r, dict) and r.get("is_pattern_group")]


def test_same_pattern_across_three_modules_forms_one_group(tmp_path):
    """5 findings sharing one pattern_id across 3 modules → 1 pattern-group row."""
    findings = [
        {"id": "F1", "module": "mod:a", "finding_kind": "logic_bug", "severity": "Critical", "file": "a/x.py"},
        {"id": "F2", "module": "mod:a", "finding_kind": "logic_bug", "severity": "P1", "file": "a/y.py"},
        {"id": "F3", "module": "mod:b", "finding_kind": "logic_bug", "severity": "P1", "file": "b/x.py"},
        {"id": "F4", "module": "mod:c", "finding_kind": "logic_bug", "severity": "P2", "file": "c/x.py"},
        {"id": "F5", "module": "mod:c", "finding_kind": "logic_bug", "severity": "P2", "file": "c/y.py"},
    ]
    # Each module's findings sit in their own semantic group, but ALL three
    # groups resolve to the SAME remediation pattern_id.
    groups = [
        {"group_id": "G_a", "member_finding_ids": ["F1", "F2"]},
        {"group_id": "G_b", "member_finding_ids": ["F3"]},
        {"group_id": "G_c", "member_finding_ids": ["F4", "F5"]},
    ]
    patterns = [
        {"group_id": "G_a", "pattern_id": "PAT1", "title": "Guard the boundary", "pattern_kind": "logic-fix"},
        {"group_id": "G_b", "pattern_id": "PAT1", "title": "Guard the boundary", "pattern_kind": "logic-fix"},
        {"group_id": "G_c", "pattern_id": "PAT1", "title": "Guard the boundary", "pattern_kind": "logic-fix"},
    ]
    _write_state(tmp_path, findings, groups, patterns)

    ordered, total, sev_tot = _build_rollups(tmp_path)

    pgs = _pattern_group_rows(ordered)
    assert len(pgs) == 1
    pg = pgs[0]
    assert pg["pattern_id"] == "PAT1"
    assert pg["pattern_title"] == "Guard the boundary"
    assert pg["instance_count"] == 5
    assert sorted(pg["affected_modules"]) == ["mod:a", "mod:b", "mod:c"]
    # Highest severity across members wins.
    assert pg["severity"] == "Critical"
    # The pattern group REPLACES its member rows: no bare (module, logic_bug)
    # rollup tuples remain at the top level for these modules.
    remaining = [k for k, r in ordered if not (isinstance(r, dict) and r.get("is_pattern_group"))]
    assert ("mod:a", "logic_bug") not in remaining
    assert ("mod:b", "logic_bug") not in remaining
    assert ("mod:c", "logic_bug") not in remaining
    # total finding count is unaffected by grouping.
    assert total == 5


def test_two_modules_below_threshold_not_grouped(tmp_path):
    """Same pattern_id but only 2 distinct modules → NOT grouped."""
    findings = [
        {"id": "F1", "module": "mod:a", "finding_kind": "logic_bug", "severity": "P1", "file": "a/x.py"},
        {"id": "F2", "module": "mod:b", "finding_kind": "logic_bug", "severity": "P1", "file": "b/x.py"},
    ]
    groups = [
        {"group_id": "G_a", "member_finding_ids": ["F1"]},
        {"group_id": "G_b", "member_finding_ids": ["F2"]},
    ]
    patterns = [
        {"group_id": "G_a", "pattern_id": "PAT1", "title": "Guard", "pattern_kind": "logic-fix"},
        {"group_id": "G_b", "pattern_id": "PAT1", "title": "Guard", "pattern_kind": "logic-fix"},
    ]
    _write_state(tmp_path, findings, groups, patterns)

    ordered, _total, _sev = _build_rollups(tmp_path)

    assert _pattern_group_rows(ordered) == []
    # Both original rows survive as individual (module, kind) tuples.
    keys = [k for k, _r in ordered]
    assert ("mod:a", "logic_bug") in keys
    assert ("mod:b", "logic_bug") in keys


def test_rows_with_unique_pattern_stay_individual(tmp_path):
    """Rows whose findings resolve to distinct patterns are never grouped."""
    findings = [
        {"id": "F1", "module": "mod:a", "finding_kind": "logic_bug", "severity": "P1", "file": "a/x.py"},
        {"id": "F2", "module": "mod:b", "finding_kind": "error_signal_loss", "severity": "P1", "file": "b/x.py"},
        {"id": "F3", "module": "mod:c", "finding_kind": "perf", "severity": "P2", "file": "c/x.py"},
    ]
    groups = [
        {"group_id": "G_a", "member_finding_ids": ["F1"]},
        {"group_id": "G_b", "member_finding_ids": ["F2"]},
        {"group_id": "G_c", "member_finding_ids": ["F3"]},
    ]
    patterns = [
        {"group_id": "G_a", "pattern_id": "PAT_A", "title": "A", "pattern_kind": "logic-fix"},
        {"group_id": "G_b", "pattern_id": "PAT_B", "title": "B", "pattern_kind": "error-handling"},
        {"group_id": "G_c", "pattern_id": "PAT_C", "title": "C", "pattern_kind": "perf"},
    ]
    _write_state(tmp_path, findings, groups, patterns)

    ordered, _total, _sev = _build_rollups(tmp_path)

    assert _pattern_group_rows(ordered) == []
    keys = [k for k, _r in ordered]
    assert ("mod:a", "logic_bug") in keys
    assert ("mod:b", "error_signal_loss") in keys
    assert ("mod:c", "perf") in keys


def test_pattern_group_exposes_all_member_finding_ids(tmp_path):
    """A pattern group carries its member rows AND every member finding id."""
    findings = [
        {"id": "F1", "module": "mod:a", "finding_kind": "logic_bug", "severity": "P1", "file": "a/x.py"},
        {"id": "F2", "module": "mod:b", "finding_kind": "logic_bug", "severity": "P1", "file": "b/x.py"},
        {"id": "F3", "module": "mod:c", "finding_kind": "logic_bug", "severity": "P2", "file": "c/x.py"},
    ]
    groups = [
        {"group_id": "G_a", "member_finding_ids": ["F1"]},
        {"group_id": "G_b", "member_finding_ids": ["F2"]},
        {"group_id": "G_c", "member_finding_ids": ["F3"]},
    ]
    patterns = [
        {"group_id": g, "pattern_id": "PAT1", "title": "Guard", "pattern_kind": "logic-fix"}
        for g in ("G_a", "G_b", "G_c")
    ]
    _write_state(tmp_path, findings, groups, patterns)

    ordered, _total, _sev = _build_rollups(tmp_path)

    pgs = _pattern_group_rows(ordered)
    assert len(pgs) == 1
    pg = pgs[0]
    # member_rows are the original rollup rows.
    assert len(pg["member_rows"]) == 3
    # Every member finding id is discoverable through the group.
    member_fids = {
        f.get("id")
        for mr in pg["member_rows"]
        for f in mr["findings"]
    }
    assert member_fids == {"F1", "F2", "F3"}


def test_emit_html_renders_approve_pattern_button(tmp_path):
    """emit_html renders a collapsible pattern section with an APPROVE PATTERN
    control wired with the pattern id and its member finding ids."""
    findings = [
        {"id": "F1", "module": "mod:a", "finding_kind": "logic_bug", "severity": "Critical", "file": "a/x.py"},
        {"id": "F2", "module": "mod:b", "finding_kind": "logic_bug", "severity": "P1", "file": "b/x.py"},
        {"id": "F3", "module": "mod:c", "finding_kind": "logic_bug", "severity": "P2", "file": "c/x.py"},
    ]
    groups = [
        {"group_id": "G_a", "member_finding_ids": ["F1"]},
        {"group_id": "G_b", "member_finding_ids": ["F2"]},
        {"group_id": "G_c", "member_finding_ids": ["F3"]},
    ]
    patterns = [
        {"group_id": g, "pattern_id": "PAT1", "title": "Guard the boundary", "pattern_kind": "logic-fix"}
        for g in ("G_a", "G_b", "G_c")
    ]
    _write_state(tmp_path, findings, groups, patterns)

    ordered, total, sev_tot = _build_rollups(tmp_path)
    out_html = tmp_path / "brief.html"
    emit_html(ordered, total, sev_tot, out_html, "Test Run")

    html_text = out_html.read_text(encoding="utf-8")
    assert "APPROVE PATTERN" in html_text
    assert "Guard the boundary" in html_text
    assert "3 instances across 3 modules" in html_text
    # Pattern-level intent is wired for the decisions export.
    assert 'data-pattern-id="PAT1"' in html_text
    assert "pattern-approve" in html_text  # data-act hook for the button
