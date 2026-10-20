"""Tests for S4-8: surface Stage-5c recalibrated severity in the Decision Brief
and the Fix Planner context.

Stage 5c may store ``finding["recalibrated_severity"]`` on a finding when its
verified severity differs from the originally-assigned ``finding["severity"]``.
Everywhere severity drives prioritization/bucketing the *effective* severity
(recalibrated-or-original) must win, and the finding row must show BOTH badges
when a recalibration occurred so the reviewer sees the correction.
"""
from __future__ import annotations

import json

from sentinel.orchestrator.context import build_fix_planner_context
from sentinel.reports.decision_brief import (
    _build_rollups,
    _effective_sev,
    _finding_row_html,
)


def _write_state(tmp_path, findings, groups=None, patterns=None):
    (tmp_path / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
    (tmp_path / "semantic_groups.json").write_text(
        json.dumps(groups or []), encoding="utf-8"
    )
    (tmp_path / "remediation_patterns.json").write_text(
        json.dumps(patterns or []), encoding="utf-8"
    )
    return tmp_path


# --------------------------------------------------------------- _effective_sev
def test_effective_sev_prefers_recalibrated():
    assert _effective_sev({"severity": "Critical", "recalibrated_severity": "P1"}) == "P1"


def test_effective_sev_falls_back_to_original():
    assert _effective_sev({"severity": "P2"}) == "P2"


# --------------------------------------------------------------- _build_rollups
def test_recalibrated_finding_counts_and_sorts_as_new_severity(tmp_path):
    """A Critical recalibrated to P1 must roll up with the P1 bucket, not Critical.

    Two findings share (module, finding_kind): a genuine P1 and a Critical that
    Stage 5c recalibrated down to P1. The row's severity Counter must show 2 P1
    and 0 Critical, and the page-level sev_tot must likewise show 2 P1 / 0
    Critical.
    """
    findings = [
        {"id": "F1", "module": "mod:a", "finding_kind": "logic_bug", "severity": "P1"},
        {
            "id": "F2",
            "module": "mod:a",
            "finding_kind": "logic_bug",
            "severity": "Critical",
            "recalibrated_severity": "P1",
        },
    ]
    _write_state(tmp_path, findings)

    ordered, total, sev_tot = _build_rollups(tmp_path)

    assert total == 2
    # Page-level rollup uses effective severity.
    assert sev_tot.get("P1") == 2
    assert sev_tot.get("Critical", 0) == 0

    # The single (mod:a, logic_bug) row buckets both as P1.
    (_key, row), = ordered
    assert row["sev"].get("P1") == 2
    assert row["sev"].get("Critical", 0) == 0


def test_sort_places_recalibrated_row_by_effective_severity(tmp_path):
    """A row whose only Critical was recalibrated down must NOT sort above a row
    with a genuine Critical."""
    findings = [
        # Row A: a real Critical.
        {"id": "A1", "module": "mod:real", "finding_kind": "k", "severity": "Critical"},
        # Row B: a Critical recalibrated to P2 (effective P2).
        {
            "id": "B1",
            "module": "mod:recal",
            "finding_kind": "k",
            "severity": "Critical",
            "recalibrated_severity": "P2",
        },
    ]
    _write_state(tmp_path, findings)

    ordered, _total, _sev_tot = _build_rollups(tmp_path)
    modules_in_order = [key[0] for key, _r in ordered]
    # Genuine Critical row sorts ahead of the recalibrated-to-P2 row.
    assert modules_in_order.index("mod:real") < modules_in_order.index("mod:recal")


# --------------------------------------------------------------- _finding_row_html
def test_finding_row_shows_both_badges_when_recalibrated():
    f = {
        "id": "F9",
        "severity": "Critical",
        "recalibrated_severity": "P1",
        "file": "a/x.py",
        "finding_kind": "logic_bug",
        "description": "boom",
    }
    html_out = _finding_row_html(f)
    # Both severities appear.
    assert "Critical" in html_out
    assert "P1" in html_out
    # The recalibration tooltip text is present.
    assert "Severity recalibrated from Critical to P1 by Stage 5c verification" in html_out
    # The original is struck/greyed out.
    assert "sev-orig" in html_out


def test_finding_row_single_badge_when_not_recalibrated():
    f = {
        "id": "F10",
        "severity": "P2",
        "file": "a/y.py",
        "finding_kind": "logic_bug",
        "description": "fine",
    }
    html_out = _finding_row_html(f)
    assert "P2" in html_out
    # No recalibration artefacts.
    assert "recalibrated" not in html_out
    assert "sev-orig" not in html_out


def test_finding_row_single_badge_when_recalibrated_equals_original():
    """Defensive: recalibrated_severity present but equal to severity → single badge."""
    f = {
        "id": "F11",
        "severity": "P2",
        "recalibrated_severity": "P2",
        "file": "a/z.py",
        "finding_kind": "logic_bug",
        "description": "same",
    }
    html_out = _finding_row_html(f)
    assert "recalibrated" not in html_out
    assert "sev-orig" not in html_out


# --------------------------------------------------------- fix planner context
def test_fix_planner_context_uses_effective_severity():
    """A Critical recalibrated to P1 must be summarised/serialised as P1 in the
    planner context, not Critical."""
    findings = [
        {"id": "F1", "severity": "P1", "description": "a"},
        {
            "id": "F2",
            "severity": "Critical",
            "recalibrated_severity": "P1",
            "description": "b",
        },
    ]
    ctx = build_fix_planner_context(findings, modules=[], dependencies=[])

    # Summary line counts 0 Critical, 2 P1.
    assert "0 Critical, 2 P1" in ctx
    # The serialised finding carries an effective_severity field the planner sees.
    assert '"effective_severity": "P1"' in ctx
