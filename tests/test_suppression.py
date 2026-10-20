"""
tests/test_suppression.py

Tests for S3-5 — Suppression rules (learning pass).

Covers the SuppressionManager rule engine (match/apply semantics, rule
persistence) and the Stage 4d wiring that writes suppressed findings to a
sidecar and active findings back to findings.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentinel.orchestrator.suppression import SuppressionManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _findings() -> list[dict]:
    return [
        {
            "id": "F-1",
            "file": "src/auth.py",
            "finding_kind": "hardcoded_secret",
            "location": "src/auth.py:42 in login",
            "description": "hardcoded key",
        },
        {
            "id": "F-2",
            "file": "src/auth.py",
            "finding_kind": "hardcoded_secret",
            "location": "src/auth.py:99 in logout",
            "description": "another key",
        },
        {
            "id": "F-3",
            "file": "src/db.py",
            "finding_kind": "sql_injection",
            "location": "src/db.py:10 in query",
            "description": "unsanitized input",
        },
    ]


# ---------------------------------------------------------------------------
# apply() — matching semantics
# ---------------------------------------------------------------------------

def test_exact_file_and_kind_suppressed(tmp_path: Path) -> None:
    sm = SuppressionManager(tmp_path)
    sm.add_rule(
        {
            "file": "src/db.py",
            "finding_kind": "sql_injection",
            "rationale": "known false positive",
        }
    )
    active, suppressed = sm.apply(_findings())

    assert {f["id"] for f in suppressed} == {"F-3"}
    assert {f["id"] for f in active} == {"F-1", "F-2"}


def test_location_pattern_only_matching_suppressed(tmp_path: Path) -> None:
    sm = SuppressionManager(tmp_path)
    sm.add_rule(
        {
            "file": "src/auth.py",
            "finding_kind": "hardcoded_secret",
            "location_pattern": "login",
            "rationale": "test fixture",
        }
    )
    active, suppressed = sm.apply(_findings())

    # Only the finding whose location contains "login" is suppressed.
    assert {f["id"] for f in suppressed} == {"F-1"}
    assert {f["id"] for f in active} == {"F-2", "F-3"}


def test_no_location_pattern_suppresses_all_in_file_and_kind(tmp_path: Path) -> None:
    sm = SuppressionManager(tmp_path)
    sm.add_rule(
        {
            "file": "src/auth.py",
            "finding_kind": "hardcoded_secret",
            "rationale": "all auth secrets are env-injected",
        }
    )
    active, suppressed = sm.apply(_findings())

    assert {f["id"] for f in suppressed} == {"F-1", "F-2"}
    assert {f["id"] for f in active} == {"F-3"}


def test_glob_file_match(tmp_path: Path) -> None:
    sm = SuppressionManager(tmp_path)
    sm.add_rule(
        {
            "file": "src/*.py",
            "finding_kind": "hardcoded_secret",
            "rationale": "glob match",
        }
    )
    active, suppressed = sm.apply(_findings())

    assert {f["id"] for f in suppressed} == {"F-1", "F-2"}
    assert {f["id"] for f in active} == {"F-3"}


def test_no_rules_pass_through_unchanged(tmp_path: Path) -> None:
    sm = SuppressionManager(tmp_path)
    findings = _findings()
    active, suppressed = sm.apply(findings)

    assert suppressed == []
    assert active == findings


def test_any_rule_match_suppresses(tmp_path: Path) -> None:
    sm = SuppressionManager(tmp_path)
    sm.add_rule({"file": "src/db.py", "finding_kind": "sql_injection", "rationale": "r1"})
    sm.add_rule(
        {
            "file": "src/auth.py",
            "finding_kind": "hardcoded_secret",
            "location_pattern": "logout",
            "rationale": "r2",
        }
    )
    active, suppressed = sm.apply(_findings())

    assert {f["id"] for f in suppressed} == {"F-2", "F-3"}
    assert {f["id"] for f in active} == {"F-1"}


def test_suppressed_finding_stamped_with_rule_id(tmp_path: Path) -> None:
    sm = SuppressionManager(tmp_path)
    sm.add_rule({"file": "src/db.py", "finding_kind": "sql_injection", "rationale": "r"})
    rule_id = sm.list_rules()[0]["rule_id"]

    _active, suppressed = sm.apply(_findings())

    assert suppressed[0]["suppressed_by"] == rule_id


# ---------------------------------------------------------------------------
# Rule persistence
# ---------------------------------------------------------------------------

def test_add_rule_persists_and_assigns_id(tmp_path: Path) -> None:
    sm = SuppressionManager(tmp_path)
    sm.add_rule({"file": "src/db.py", "finding_kind": "sql_injection", "rationale": "r"})

    rules_path = tmp_path / "suppression_rules.json"
    assert rules_path.exists()
    stored = json.loads(rules_path.read_text(encoding="utf-8"))
    assert len(stored) == 1
    assert stored[0]["rule_id"]
    assert stored[0]["created"]
    assert stored[0]["created_by"]


def test_add_rule_preserves_supplied_rule_id(tmp_path: Path) -> None:
    sm = SuppressionManager(tmp_path)
    sm.add_rule(
        {
            "rule_id": "fixed-id",
            "file": "src/db.py",
            "finding_kind": "sql_injection",
            "rationale": "r",
        }
    )
    assert sm.list_rules()[0]["rule_id"] == "fixed-id"


def test_remove_rule_deletes_by_id(tmp_path: Path) -> None:
    sm = SuppressionManager(tmp_path)
    sm.add_rule({"file": "a.py", "finding_kind": "k1", "rationale": "r1"})
    sm.add_rule({"file": "b.py", "finding_kind": "k2", "rationale": "r2"})
    target = sm.list_rules()[0]["rule_id"]

    sm.remove_rule(target)

    remaining = sm.list_rules()
    assert len(remaining) == 1
    assert remaining[0]["rule_id"] != target


def test_load_rules_missing_file_returns_empty(tmp_path: Path) -> None:
    sm = SuppressionManager(tmp_path)
    assert sm.load_rules() == []


def test_load_rules_unreadable_returns_empty(tmp_path: Path) -> None:
    sm = SuppressionManager(tmp_path)
    (tmp_path / "suppression_rules.json").write_text("{ not json", encoding="utf-8")
    assert sm.load_rules() == []


# ---------------------------------------------------------------------------
# Stage 4d wiring (run_phase1)
# ---------------------------------------------------------------------------

def test_run_phase1_writes_sidecar_and_active_findings(tmp_path: Path) -> None:
    """Stage 4d wiring: findings.json rewritten with active only; suppressed
    findings written to suppressed_findings.json sidecar."""
    from sentinel.orchestrator.state_store import StateStore

    store = StateStore(tmp_path, repo_path=tmp_path)
    store.write("findings", _findings())

    sm = SuppressionManager(tmp_path)
    sm.add_rule({"file": "src/db.py", "finding_kind": "sql_injection", "rationale": "r"})

    # Mirror the Stage 4d wiring block inline (the injection point in
    # run_phase1) to validate the read/apply/write contract end to end.
    rules = sm.load_rules()
    assert rules
    findings = store.read("findings") or []
    active, suppressed = sm.apply(findings)
    store.write("findings", active)
    if suppressed:
        (tmp_path / "suppressed_findings.json").write_text(
            json.dumps(suppressed, indent=2), encoding="utf-8"
        )

    written_active = store.read("findings")
    assert {f["id"] for f in written_active} == {"F-1", "F-2"}

    sidecar = tmp_path / "suppressed_findings.json"
    assert sidecar.exists()
    stored_suppressed = json.loads(sidecar.read_text(encoding="utf-8"))
    assert {f["id"] for f in stored_suppressed} == {"F-3"}


# ---------------------------------------------------------------------------
# CLI — sentinel suppress
# ---------------------------------------------------------------------------

def test_cli_add_list_remove(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from sentinel.cli import main

    runner = CliRunner()
    sd = str(tmp_path)

    add = runner.invoke(
        main,
        [
            "suppress", "--state-dir", sd, "--add",
            "--file", "src/db.py", "--kind", "sql_injection",
            "--rationale", "false positive",
        ],
    )
    assert add.exit_code == 0, add.output
    assert "Added suppression rule" in add.output

    listed = runner.invoke(main, ["suppress", "--state-dir", sd, "--list"])
    assert listed.exit_code == 0, listed.output
    assert "sql_injection" in listed.output

    rules = SuppressionManager(tmp_path).list_rules()
    assert len(rules) == 1
    rule_id = rules[0]["rule_id"]

    removed = runner.invoke(
        main, ["suppress", "--state-dir", sd, "--remove", rule_id]
    )
    assert removed.exit_code == 0, removed.output
    assert SuppressionManager(tmp_path).list_rules() == []


def test_cli_add_requires_file_and_kind(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from sentinel.cli import main

    runner = CliRunner()
    result = runner.invoke(
        main, ["suppress", "--state-dir", str(tmp_path), "--add", "--file", "x.py"]
    )
    assert result.exit_code == 1
    assert "requires both --file and --kind" in result.output


# ---------------------------------------------------------------------------
# STEP 4 — Decision Brief DEFER -> suppression_rule export
# ---------------------------------------------------------------------------

def test_brief_exposes_finding_kind_and_suppression_export(tmp_path: Path) -> None:
    """The rendered brief carries data-finding-kind per row and the export JS
    emits a suppression_rule for a DEFER decision with a rationale."""
    from sentinel.reports.decision_brief import render

    findings = [
        {
            "id": "F1",
            "module": "demo:core",
            "finding_kind": "logic_bug",
            "severity": "Critical",
            "file": "core/a.py",
            "location": "core/a.py:10 in run",
        },
    ]
    groups = [{"group_id": "G1", "member_finding_ids": ["F1"]}]
    patterns = [
        {
            "group_id": "G1",
            "pattern_id": "P1",
            "title": "Fix",
            "pattern_kind": "logic-fix",
            "proposed_changes": ["c"],
            "acceptance_criteria": ["a"],
        }
    ]
    tech_debt = [
        {
            "id": "TD-0001",
            "finding_id": "F1",
            "module": "demo:core",
            "severity": "Critical",
            "human_triage": None,
        }
    ]
    (tmp_path / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
    (tmp_path / "semantic_groups.json").write_text(json.dumps(groups), encoding="utf-8")
    (tmp_path / "remediation_patterns.json").write_text(
        json.dumps(patterns), encoding="utf-8"
    )
    (tmp_path / "tech_debt.json").write_text(json.dumps(tech_debt), encoding="utf-8")

    out_html = tmp_path / "brief.html"
    out_md = tmp_path / "brief.md"
    render(tmp_path, out_html, out_md, "Test Run")

    html = out_html.read_text(encoding="utf-8")
    # Row carries the finding_kind for suppression-rule seeding.
    assert 'data-finding-kind="logic_bug"' in html
    # Export path emits a suppression_rule for DEFER + rationale.
    assert "entry.suppression_rule=sr" in html
    assert "en.dec.decision==='DEFER'" in html
