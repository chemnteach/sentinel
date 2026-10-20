"""Code-owner triage semantics in the decision brief.

A Sentinel-triage APPROVE means "in scope for the CODE OWNER to decide" — so it
must render UNDECIDED (active radios, not hidden by 'Hide decided'). Only
DEFER/EXCLUDE are already-decided from the code owner's perspective.
"""
import json
import re

import sentinel.reports.decision_brief as db
from sentinel.reports.decision_brief import render


def _row(triage: str) -> str:
    return db._finding_row_html(
        {"id": "F1", "severity": "P1", "_human_triage": triage},
        group_id_safe="G1", row_index=0)


def test_approve_renders_undecided_for_code_owner():
    row = _row("APPROVE")
    assert "finding-row decided" not in row      # not dimmed / not hideable
    assert 'data-triaged=""' in row              # JS restore won't mark decided
    assert 'value="APPROVE" checked' not in row  # code owner decides fresh


def test_approve_with_conditions_also_undecided():
    row = _row("APPROVE_WITH_CONDITIONS")
    assert "finding-row decided" not in row
    assert 'data-triaged=""' in row


def test_defer_renders_decided():
    row = _row("DEFER")
    assert "finding-row decided" in row
    assert 'data-triaged="DEFER"' in row
    assert 'value="DEFER" checked' in row


def test_exclude_renders_decided():
    row = _row("EXCLUDE")
    assert "finding-row decided" in row
    assert 'data-triaged="EXCLUDE"' in row


def test_untriaged_renders_undecided():
    row = _row("")
    assert "finding-row decided" not in row
    assert 'data-triaged=""' in row


def _seed(tmp_path, triage_map):
    findings = [{"id": fid, "module": "demo:core", "finding_kind": "logic_bug",
                 "severity": "P1", "file": "a.py"} for fid in triage_map]
    groups = [{"group_id": "G1", "member_finding_ids": list(triage_map)}]
    patterns = [{"group_id": "G1", "pattern_id": "P1", "title": "Fix",
                 "pattern_kind": "logic-fix", "proposed_changes": ["x"],
                 "acceptance_criteria": ["y"]}]
    tech_debt = [{"id": f"TD-{i}", "finding_id": fid, "module": "demo:core",
                  "severity": "P1", "human_triage": triage_map[fid]}
                 for i, fid in enumerate(triage_map)]
    (tmp_path / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
    (tmp_path / "semantic_groups.json").write_text(json.dumps(groups), encoding="utf-8")
    (tmp_path / "remediation_patterns.json").write_text(json.dumps(patterns), encoding="utf-8")
    (tmp_path / "tech_debt.json").write_text(json.dumps(tech_debt), encoding="utf-8")
    return tmp_path


def test_fidtriaged_map_excludes_approve(tmp_path):
    """The restore-seed map must NOT contain APPROVE, or _restoreState re-marks
    approved findings decided on load and 'Hide decided' hides them."""
    state = _seed(tmp_path, {"FA": "APPROVE", "FD": "DEFER", "FE": "EXCLUDE"})
    out = tmp_path / "brief.html"
    render(state, out, tmp_path / "brief.md", "T")
    html = out.read_text(encoding="utf-8")
    m = re.search(r'window\._fidTriaged=(\{.*?\});', html)
    assert m, "emitted _fidTriaged map not found"
    fmap = json.loads(m.group(1))
    assert "FA" not in fmap            # APPROVE stays undecided (code owner decides)
    assert fmap.get("FD") == "DEFER"
    assert fmap.get("FE") == "EXCLUDE"
