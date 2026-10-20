"""Tests for S2-2 — bundle-aware Fix Planner + PR titles + Bundle status model.

Covers:
  - BundlePlanner.build_bundles bundling rules (HIGH / MEDIUM / LOW / SINGLETON)
  - root-cause selection (highest severity, causal tie-break, first-in-file)
  - one acceptance criterion per member
  - predicted_resolved rules
  - BundleStatus enum values
  - plan-item -> bundle_id stamping
  - Task dataclass bundle fields
  - bundle-aware PR title + body
  - decision_brief bundle rendering (present) vs flat fallback (absent)
  - excluding a member moves that finding to DEFERRED
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentinel.orchestrator.bundle_planner import BundlePlanner
from sentinel.assess.finding_status import BundleStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _finding(
    fid,
    *,
    file="a.py",
    severity="P2",
    kind="logic_bug",
    title=None,
    line_range=None,
    verified_location=None,
    location=None,
):
    f = {
        "id": fid,
        "file": file,
        "severity": severity,
        "finding_kind": kind,
        "title": title or f"{kind} in {file}",
    }
    if line_range is not None:
        f["line_range"] = line_range
    if verified_location is not None:
        f["verified_location"] = verified_location
    if location is not None:
        f["location"] = location
    return f


def _by_member(bundles, fid):
    """Return the bundle containing member finding ``fid``."""
    for b in bundles:
        for m in b["member_findings"]:
            if m["finding_id"] == fid:
                return b
    return None


# ---------------------------------------------------------------------------
# BundleStatus enum
# ---------------------------------------------------------------------------

def test_bundle_status_has_all_six_values():
    assert BundleStatus.UNDECIDED == "UNDECIDED"
    assert BundleStatus.APPROVED == "APPROVED"
    assert BundleStatus.IN_PROGRESS == "IN_PROGRESS"
    assert BundleStatus.IN_REVIEW == "IN_REVIEW"
    assert BundleStatus.COMPLETE == "COMPLETE"
    assert BundleStatus.REVERTED == "REVERTED"
    values = {
        BundleStatus.UNDECIDED,
        BundleStatus.APPROVED,
        BundleStatus.IN_PROGRESS,
        BundleStatus.IN_REVIEW,
        BundleStatus.COMPLETE,
        BundleStatus.REVERTED,
    }
    assert len(values) == 6


# ---------------------------------------------------------------------------
# HIGH confidence — same file + same method / overlapping lines
# ---------------------------------------------------------------------------

def test_same_file_same_method_is_high():
    findings = [
        _finding("F1", file="svc.py", kind="logic_bug",
                 location="svc.py:process", line_range="10-15"),
        _finding("F2", file="svc.py", kind="error_signal_loss",
                 location="svc.py:process", line_range="200-205"),
    ]
    bundles = BundlePlanner().build_bundles(findings)
    b = _by_member(bundles, "F1")
    assert b is not None
    assert b["confidence"] == "HIGH"
    assert {m["finding_id"] for m in b["member_findings"]} == {"F1", "F2"}


def test_same_file_overlapping_lines_is_high():
    findings = [
        _finding("F1", file="svc.py", kind="logic_bug", line_range="10-20"),
        _finding("F2", file="svc.py", kind="observability_gap", line_range="25-30"),
    ]
    bundles = BundlePlanner().build_bundles(findings)
    b = _by_member(bundles, "F1")
    assert b["confidence"] == "HIGH"
    assert {m["finding_id"] for m in b["member_findings"]} == {"F1", "F2"}


def test_same_file_uses_verified_location_lines():
    findings = [
        _finding("F1", file="svc.py", kind="logic_bug",
                 verified_location={"start": 100, "end": 110}),
        _finding("F2", file="svc.py", kind="observability_gap",
                 verified_location={"start": 115, "end": 120}),
    ]
    bundles = BundlePlanner().build_bundles(findings)
    b = _by_member(bundles, "F1")
    assert b["confidence"] == "HIGH"


# ---------------------------------------------------------------------------
# MEDIUM confidence — same file + causal kind pair
# ---------------------------------------------------------------------------

def test_same_file_causal_pair_is_medium():
    # timeout_handling_gap -> error_signal_loss is a CAUSAL_PAIR; far-apart lines
    # so it is not HIGH by proximity.
    findings = [
        _finding("F1", file="net.py", kind="timeout_handling_gap", line_range="5-8"),
        _finding("F2", file="net.py", kind="error_signal_loss", line_range="400-405"),
    ]
    bundles = BundlePlanner().build_bundles(findings)
    b = _by_member(bundles, "F1")
    assert b is not None
    assert b["confidence"] == "MEDIUM"
    assert {m["finding_id"] for m in b["member_findings"]} == {"F1", "F2"}


def test_same_file_causal_pair_reverse_direction_is_medium():
    findings = [
        _finding("F1", file="net.py", kind="error_signal_loss", line_range="400-405"),
        _finding("F2", file="net.py", kind="timeout_handling_gap", line_range="5-8"),
    ]
    bundles = BundlePlanner().build_bundles(findings)
    b = _by_member(bundles, "F1")
    assert b["confidence"] == "MEDIUM"


# ---------------------------------------------------------------------------
# LOW confidence — cross-file causal (RELATED, separate)
# ---------------------------------------------------------------------------

def test_cross_file_causal_is_low_related():
    findings = [
        _finding("F1", file="net.py", kind="timeout_handling_gap", line_range="5-8"),
        _finding("F2", file="handler.py", kind="error_signal_loss", line_range="5-8"),
    ]
    bundles = BundlePlanner().build_bundles(findings)
    b = _by_member(bundles, "F1")
    assert b is not None
    assert b["confidence"] == "LOW"
    # shared bundle_id but both remain members (RELATED, not merged into one fix)
    assert {m["finding_id"] for m in b["member_findings"]} == {"F1", "F2"}
    # cross-file cascade members are predicted only, not co-fixed
    f2 = next(m for m in b["member_findings"] if m["finding_id"] == "F2")
    assert f2["predicted_resolved"] is False


# ---------------------------------------------------------------------------
# SINGLETON
# ---------------------------------------------------------------------------

def test_unrelated_finding_is_singleton():
    findings = [
        _finding("F1", file="a.py", kind="logic_bug", line_range="10-15"),
        _finding("F2", file="b.py", kind="performance_gap", line_range="500-505"),
    ]
    bundles = BundlePlanner().build_bundles(findings)
    b = _by_member(bundles, "F2")
    assert b["confidence"] == "SINGLETON"
    assert len(b["member_findings"]) == 1


def test_singleton_root_cause_predicted_resolved_true():
    findings = [_finding("F1", file="a.py", kind="logic_bug", line_range="10-15")]
    bundles = BundlePlanner().build_bundles(findings)
    assert len(bundles) == 1
    b = bundles[0]
    assert b["confidence"] == "SINGLETON"
    assert b["member_findings"][0]["predicted_resolved"] is True
    assert b["root_cause_finding_id"] == "F1"


# ---------------------------------------------------------------------------
# Root cause selection
# ---------------------------------------------------------------------------

def test_root_cause_is_highest_severity():
    findings = [
        _finding("F1", file="svc.py", kind="logic_bug",
                 severity="P3", line_range="10-15"),
        _finding("F2", file="svc.py", kind="error_signal_loss",
                 severity="Critical", line_range="12-18"),
    ]
    bundles = BundlePlanner().build_bundles(findings)
    b = _by_member(bundles, "F1")
    assert b["root_cause_finding_id"] == "F2"
    assert b["root_cause_severity"] == "Critical"


def test_root_cause_tie_break_first_in_file():
    # Same severity, no causal dependency ordering -> lowest start line wins.
    findings = [
        _finding("F1", file="svc.py", kind="logic_bug",
                 severity="P2", line_range="40-45"),
        _finding("F2", file="svc.py", kind="logic_bug",
                 severity="P2", line_range="10-15"),
    ]
    bundles = BundlePlanner().build_bundles(findings)
    b = _by_member(bundles, "F1")
    assert b["root_cause_finding_id"] == "F2"


# ---------------------------------------------------------------------------
# Acceptance criteria — one per member
# ---------------------------------------------------------------------------

def test_one_acceptance_criterion_per_member():
    findings = [
        _finding("F1", file="svc.py", kind="logic_bug", line_range="10-15"),
        _finding("F2", file="svc.py", kind="error_signal_loss", line_range="12-18"),
        _finding("F3", file="svc.py", kind="contract_violation", line_range="14-20"),
    ]
    bundles = BundlePlanner().build_bundles(findings)
    b = _by_member(bundles, "F1")
    assert len(b["member_findings"]) == 3
    assert len(b["acceptance_criteria"]) == 3


# ---------------------------------------------------------------------------
# Bundle ID numbering / deterministic
# ---------------------------------------------------------------------------

def test_bundle_ids_are_sequential_and_deterministic():
    findings = [
        _finding("F1", file="a.py", kind="logic_bug", line_range="10-15"),
        _finding("F2", file="b.py", kind="logic_bug", line_range="10-15"),
        _finding("F3", file="c.py", kind="logic_bug", line_range="10-15"),
    ]
    bundles = BundlePlanner().build_bundles(findings)
    ids = [b["bundle_id"] for b in bundles]
    assert ids == ["BUNDLE-001", "BUNDLE-002", "BUNDLE-003"]
    # deterministic across runs
    again = [b["bundle_id"] for b in BundlePlanner().build_bundles(findings)]
    assert again == ids


def test_every_bundle_starts_undecided():
    findings = [_finding("F1", file="a.py", kind="logic_bug", line_range="10-15")]
    bundles = BundlePlanner().build_bundles(findings)
    assert bundles[0]["bundle_status"] == BundleStatus.UNDECIDED


def test_handles_finding_with_no_line_info():
    findings = [
        _finding("F1", file="a.py", kind="logic_bug"),  # no line info at all
        _finding("F2", file="a.py", kind="error_signal_loss"),
    ]
    # Must not raise.
    bundles = BundlePlanner().build_bundles(findings)
    assert bundles  # at least one bundle produced


def test_empty_findings_returns_empty():
    assert BundlePlanner().build_bundles([]) == []


# ---------------------------------------------------------------------------
# Plan-item bundle stamping (main.py wiring helper)
# ---------------------------------------------------------------------------

def test_plan_items_get_bundle_id_stamped():
    from sentinel.orchestrator.bundle_planner import stamp_plan_items_with_bundles

    bundles = [
        {
            "bundle_id": "BUNDLE-001",
            "member_findings": [{"finding_id": "F1"}, {"finding_id": "F2"}],
            "root_cause_finding_id": "F1",
        },
        {
            "bundle_id": "BUNDLE-002",
            "member_findings": [{"finding_id": "F3"}],
            "root_cause_finding_id": "F3",
        },
    ]
    plan = {
        "blocking": [{"id": "FIX-BLOCKING-001", "source_findings": ["F2"]}],
        "required": [{"id": "FIX-REQUIRED-001", "source_findings": ["F3"]}],
        "desired": [],
        "nice_to_have": [],
    }
    stamp_plan_items_with_bundles(plan, bundles)
    assert plan["blocking"][0]["bundle_id"] == "BUNDLE-001"
    assert plan["blocking"][0]["bundle_status"] == BundleStatus.UNDECIDED
    assert plan["required"][0]["bundle_id"] == "BUNDLE-002"


def test_plan_item_spanning_bundles_uses_root_cause_bundle():
    from sentinel.orchestrator.bundle_planner import stamp_plan_items_with_bundles

    bundles = [
        {
            "bundle_id": "BUNDLE-001",
            "member_findings": [{"finding_id": "F1"}],
            "root_cause_finding_id": "F1",
        },
        {
            "bundle_id": "BUNDLE-002",
            "member_findings": [{"finding_id": "F2"}],
            "root_cause_finding_id": "F2",
        },
    ]
    # Item references findings from two bundles; F1 is a root cause -> its bundle.
    plan = {
        "blocking": [{"id": "X", "source_findings": ["F2", "F1"]}],
        "required": [], "desired": [], "nice_to_have": [],
    }
    stamp_plan_items_with_bundles(plan, bundles)
    assert plan["blocking"][0]["bundle_id"] == "BUNDLE-001"


# ---------------------------------------------------------------------------
# Task dataclass bundle fields
# ---------------------------------------------------------------------------

def test_task_has_bundle_fields():
    from sentinel.orchestrator.task_manager import Task
    from sentinel.orchestrator.state_machine import TaskState

    t = Task(task_id="t1", plan_id="p1")
    assert t.bundle_id is None
    assert t.bundle_status == BundleStatus.UNDECIDED


def test_create_task_stores_bundle_fields(tmp_path):
    from sentinel.orchestrator.state_store import StateStore
    from sentinel.orchestrator.task_manager import TaskManager

    store = StateStore(tmp_path)
    tm = TaskManager(store)
    task = tm.create_task(
        plan_id="P-1",
        description="fix",
        acceptance_criteria=["c"],
        affected_files=["a.py"],
        scope_boundary="only a.py",
        bundle_id="BUNDLE-001",
        bundle_status=BundleStatus.APPROVED,
    )
    assert task.bundle_id == "BUNDLE-001"
    assert task.bundle_status == BundleStatus.APPROVED
    reloaded = tm.load_task(task.task_id)
    assert reloaded.bundle_id == "BUNDLE-001"


def test_create_task_bundle_fields_default_backward_compatible(tmp_path):
    from sentinel.orchestrator.state_store import StateStore
    from sentinel.orchestrator.task_manager import TaskManager

    store = StateStore(tmp_path)
    tm = TaskManager(store)
    task = tm.create_task(
        plan_id="P-1",
        description="fix",
        acceptance_criteria=["c"],
        affected_files=["a.py"],
        scope_boundary="only a.py",
    )
    assert task.bundle_id is None
    assert task.bundle_status == BundleStatus.UNDECIDED


# ---------------------------------------------------------------------------
# PR title + body (pr_opener)
# ---------------------------------------------------------------------------

def _bundle_fixture():
    return {
        "bundle_id": "BUNDLE-002",
        "confidence": "HIGH",
        "root_cause_finding_id": "F1",
        "root_cause_severity": "Critical",
        "root_cause_title": "Missing timeout on network call",
        "affected_files": ["net.py"],
        "member_findings": [
            {"finding_id": "F1", "severity": "Critical",
             "title": "Missing timeout on network call",
             "predicted_resolved": True, "finding_kind": "timeout_handling_gap"},
            {"finding_id": "F2", "severity": "P2",
             "title": "Error swallowed downstream",
             "predicted_resolved": True, "finding_kind": "error_signal_loss"},
        ],
        "bundle_status": "APPROVED",
        "acceptance_criteria": ["Add timeout", "Propagate error"],
    }


def test_pr_title_includes_bundle_number_and_total():
    from sentinel.workflow.remediation.pr_opener import build_pr_title
    from sentinel.orchestrator.task_manager import Task

    task = Task(task_id="t1", plan_id="p1", bundle_id="BUNDLE-002",
                description="fix timeout")
    title = build_pr_title(task, _bundle_fixture(), total_bundles=5)
    assert "Missing timeout on network call" in title
    assert "Bundle 2 of 5" in title
    assert title.startswith("[Sentinel] Fix:")


def test_pr_title_without_bundle_is_unchanged():
    from sentinel.workflow.remediation.pr_opener import build_pr_title
    from sentinel.orchestrator.task_manager import Task

    task = Task(task_id="t1", plan_id="p1", description="fix stuff")
    title = build_pr_title(task, None, total_bundles=0)
    # legacy shape, no bundle markers
    assert "Bundle" not in title
    assert "t1" in title


def test_pr_body_includes_cascade_section():
    from sentinel.workflow.remediation.pr_opener import build_pr_body
    from sentinel.orchestrator.task_manager import Task

    task = Task(task_id="t1", plan_id="p1", bundle_id="BUNDLE-002",
                description="fix", pre_fix_sha="abc123")
    body = build_pr_body(task, bundle=_bundle_fixture())
    assert "Root cause" in body
    assert "Missing timeout on network call" in body
    assert "Cascade" in body
    assert "--changed-since abc123" in body


def test_pr_body_without_bundle_is_legacy_format():
    from sentinel.workflow.remediation.pr_opener import build_pr_body
    from sentinel.orchestrator.task_manager import Task

    task = Task(task_id="t1", plan_id="p1", description="legacy task")
    body = build_pr_body(task)
    assert "Sentinel Task: t1" in body
    assert "Cascade" not in body


# ---------------------------------------------------------------------------
# Decision brief bundle rendering
# ---------------------------------------------------------------------------

def _seed_brief_state(state_dir: Path, with_bundles: bool):
    findings = [
        {"id": "F1", "file": "svc.py", "severity": "Critical",
         "finding_kind": "timeout_handling_gap", "title": "Missing timeout",
         "line_range": "10-15", "module": "svc"},
        {"id": "F2", "file": "svc.py", "severity": "P2",
         "finding_kind": "error_signal_loss", "title": "Error swallowed",
         "line_range": "12-18", "module": "svc"},
    ]
    groups = [{"group_id": "G1", "member_finding_ids": ["F1", "F2"]}]
    patterns = [{"pattern_id": "PAT-1", "group_id": "G1",
                 "proposed_changes": ["Add timeout"], "acceptance_criteria": ["ok"]}]
    (state_dir / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
    (state_dir / "semantic_groups.json").write_text(json.dumps(groups), encoding="utf-8")
    (state_dir / "remediation_patterns.json").write_text(json.dumps(patterns), encoding="utf-8")
    if with_bundles:
        bundles = [{
            "bundle_id": "BUNDLE-001",
            "confidence": "HIGH",
            "root_cause_finding_id": "F1",
            "root_cause_severity": "Critical",
            "root_cause_title": "Missing timeout",
            "affected_files": ["svc.py"],
            "member_findings": [
                {"finding_id": "F1", "severity": "Critical", "title": "Missing timeout",
                 "predicted_resolved": True, "finding_kind": "timeout_handling_gap"},
                {"finding_id": "F2", "severity": "P2", "title": "Error swallowed",
                 "predicted_resolved": True, "finding_kind": "error_signal_loss"},
            ],
            "bundle_status": "UNDECIDED",
            "acceptance_criteria": ["Add timeout", "Propagate error"],
        }]
        (state_dir / "bundles.json").write_text(json.dumps(bundles), encoding="utf-8")


def test_brief_renders_bundle_header_when_bundles_json_exists(tmp_path):
    from sentinel.reports.decision_brief import render

    _seed_brief_state(tmp_path, with_bundles=True)
    out_html, _md, _total, _rows = render(tmp_path, run_label="test")
    html_text = Path(out_html).read_text(encoding="utf-8")
    assert "BUNDLE-001" in html_text or "Bundle 1" in html_text
    assert "APPROVE BUNDLE" in html_text
    assert "Missing timeout" in html_text


def test_brief_falls_back_to_flat_when_no_bundles_json(tmp_path):
    from sentinel.reports.decision_brief import render

    _seed_brief_state(tmp_path, with_bundles=False)
    out_html, _md, _total, _rows = render(tmp_path, run_label="test")
    html_text = Path(out_html).read_text(encoding="utf-8")
    # No bundle scaffolding when bundles.json absent (backward compatible)
    assert "APPROVE BUNDLE" not in html_text
    # But the flat cards still render.
    assert "timeout_handling_gap" in html_text


# ---------------------------------------------------------------------------
# Excluding a member moves that finding to DEFERRED
# ---------------------------------------------------------------------------

def test_exclude_member_defers_finding():
    from sentinel.orchestrator.bundle_planner import exclude_member

    bundle = _bundle_fixture()
    updated, deferred_fid = exclude_member(bundle, "F2")
    assert deferred_fid == "F2"
    # F2 no longer a member of the bundle
    assert "F2" not in {m["finding_id"] for m in updated["member_findings"]}
    # F1 (root cause) remains
    assert "F1" in {m["finding_id"] for m in updated["member_findings"]}


def test_exclude_root_cause_is_rejected():
    from sentinel.orchestrator.bundle_planner import exclude_member

    bundle = _bundle_fixture()
    with pytest.raises(ValueError):
        exclude_member(bundle, "F1")  # F1 is the root cause
