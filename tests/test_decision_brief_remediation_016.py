"""Regression tests for run-016 remediation of decision_brief.py.

Covers self-contained MINIMUM-CHANGE fixes:
  - FIX-REQUIRED-311: radio group name is unique per row, independent of fid.
  - FIX-REQUIRED-313: LLM client init failure surfaces a degraded-mode warning.
  - FIX-REQUIRED-293: --github-ref help documents the optional auto-fill contract.
"""
from __future__ import annotations

import json
import logging

import pytest

from sentinel.reports import decision_brief as db


# ── FIX-REQUIRED-311 ──────────────────────────────────────────────────────
def test_radio_group_name_unique_even_with_missing_or_duplicate_fid():
    # Two findings with the SAME (here: empty) id must not share a radio group
    # name, or browser selection state leaks across unrelated rows.
    row0 = db._finding_row_html({"id": "", "severity": "P1"}, group_id_safe="G1", row_index=0)
    row1 = db._finding_row_html({"id": "", "severity": "P1"}, group_id_safe="G1", row_index=1)

    def _name(row: str) -> str:
        marker = 'type="radio" name="'
        start = row.index(marker) + len(marker)
        return row[start:row.index('"', start)]

    assert _name(row0) != _name(row1)


# ── FIX-REQUIRED-313 ──────────────────────────────────────────────────────
def _seed_minimal_state(tmp_path):
    (tmp_path / "findings.json").write_text(json.dumps(
        [{"id": "F1", "module": "demo:core", "finding_kind": "logic_bug",
          "severity": "Critical", "file": "a.py"}]), encoding="utf-8")
    (tmp_path / "semantic_groups.json").write_text(json.dumps(
        [{"group_id": "G1", "member_finding_ids": ["F1"]}]), encoding="utf-8")
    (tmp_path / "remediation_patterns.json").write_text(json.dumps(
        [{"group_id": "G1", "pattern_id": "P1", "title": "Fix", "pattern_kind": "k",
          "proposed_changes": ["do it"]}]), encoding="utf-8")
    (tmp_path / "tech_debt.json").write_text(json.dumps([]), encoding="utf-8")
    return tmp_path


def test_llm_init_failure_warns_and_degrades(tmp_path, monkeypatch, caplog):
    import sentinel.api.client as client_mod

    class _BoomClient:
        def __init__(self, *a, **k):
            raise RuntimeError("no gateway configured")

    monkeypatch.setattr(client_mod, "create_model_client", _BoomClient)
    state = _seed_minimal_state(tmp_path)

    with caplog.at_level(logging.WARNING, logger="sentinel.reports.decision_brief"):
        out_html, _md, _total, _rows = db.render(
            state, tmp_path / "brief.html", tmp_path / "brief.md", "Run",
            use_synthesis=True,
        )

    # Degradation is now visible, and render still completes (fallback to templates).
    assert "LLM client init failed" in caplog.text
    assert out_html.exists()


# ── FIX-REQUIRED-293 ──────────────────────────────────────────────────────
def test_github_ref_help_documents_optional_autofill(capsys):
    with pytest.raises(SystemExit):
        db._parse_args(["--state-dir", ".", "--help"])
    out = capsys.readouterr().out
    assert "--github-ref" in out
    # argparse may wrap "auto-filled" across lines, so assert on "Optional".
    assert "Optional" in out
    assert "Required with" not in out
