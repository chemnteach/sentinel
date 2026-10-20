"""The brief's executive-summary preamble names the assessed repo, not Sentinel."""
from __future__ import annotations

import json
from collections import Counter

from sentinel.reports import decision_brief as db


def test_preamble_names_the_assessed_repo():
    text = db._preamble("Tool_Line")
    assert "Tool_Line" in text
    assert "Sentinel codebase" not in text


def test_preamble_without_repo_name_is_neutral():
    text = db._preamble("")
    assert "the assessed codebase" in text
    assert "Sentinel codebase" not in text


def test_assessed_repo_name_reads_scan_metadata(tmp_path):
    (tmp_path / "scan_metadata.json").write_text(
        json.dumps({"repo_root_path": "D:/Git/Tool_Line", "commit_sha": "abc"}),
        encoding="utf-8",
    )
    assert db.assessed_repo_name(tmp_path) == "Tool_Line"


def test_assessed_repo_name_missing_metadata_is_empty(tmp_path):
    assert db.assessed_repo_name(tmp_path) == ""


def test_assessed_repo_name_corrupt_metadata_is_empty(tmp_path):
    (tmp_path / "scan_metadata.json").write_text("{not json", encoding="utf-8")
    assert db.assessed_repo_name(tmp_path) == ""


def test_emit_md_writes_repo_specific_preamble(tmp_path):
    out = tmp_path / "brief.md"
    db.emit_md([], 0, Counter(), out, "run-x", repo_name="Tool_Line")
    md = out.read_text(encoding="utf-8")
    assert "automated code review of Tool_Line" in md
    assert "Sentinel codebase" not in md
