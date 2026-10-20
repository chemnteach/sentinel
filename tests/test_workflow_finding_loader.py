"""Tests for sentinel.workflow.finding_loader.

Cycle 27 Item 8 sub-step 8-C: pins the contract of the
``findings.json`` -> ``FindingIssueRecord`` rehydration bridge --
silent-default behaviour, the raw ``"id"`` -> ``finding_id`` regime
translation, the pre-triage placeholder synthesis for the six
downstream-of-dedup fields, and the producer-field round-trip.
"""

from __future__ import annotations

import json
from pathlib import Path

from sentinel.workflow.finding_loader import (
    _PRE_TRIAGE_PLACEHOLDER_THEME,
    load_findings,
)
from sentinel.workflow.models import FindingIssueRecord, ThemeReference


def test_returns_empty_tuple_when_findings_file_absent(tmp_path: Path) -> None:
    """Absent ``findings.json`` yields ``()`` (silent-default convention)."""
    assert load_findings(tmp_path) == ()


def test_returns_empty_tuple_when_findings_file_corrupt_json(
    tmp_path: Path,
) -> None:
    """A ``findings.json`` whose contents do not parse as JSON yields ``()``."""
    (tmp_path / "findings.json").write_text("{not valid json", encoding="utf-8")
    assert load_findings(tmp_path) == ()


def test_returns_empty_tuple_when_top_level_not_list(tmp_path: Path) -> None:
    """A ``findings.json`` whose top level is not a list yields ``()``."""
    (tmp_path / "findings.json").write_text(
        json.dumps({"unexpected": "shape"}), encoding="utf-8"
    )
    assert load_findings(tmp_path) == ()


def test_loads_minimal_raw_finding_dict_with_defaults(tmp_path: Path) -> None:
    """A 1-element minimal payload rehydrates with placeholder-six populated
    and all defaulted fields at their dataclass defaults."""
    raw = [
        {
            "id": "F-00000000abcd",
            "module": "handlers/wafer_state.py",
            "skill": "layer2a-quick-review",
            "description": "Unchecked Optional dereference at line 147.",
            "location": "handlers/wafer_state.py:147",
            "severity": "P1",
        }
    ]
    (tmp_path / "findings.json").write_text(json.dumps(raw), encoding="utf-8")

    records = load_findings(tmp_path)

    assert isinstance(records, tuple)
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, FindingIssueRecord)
    # Regime boundary: raw "id" -> finding_id.
    assert record.finding_id == "F-00000000abcd"
    # Producer-supplied fields round-trip.
    assert record.module == "handlers/wafer_state.py"
    assert record.skill == "layer2a-quick-review"
    assert record.description == "Unchecked Optional dereference at line 147."
    assert record.location == "handlers/wafer_state.py:147"
    assert record.severity == "P1"
    # Producer-side fields absent from minimal payload take tolerant defaults.
    assert record.summary == ""
    assert record.category == "general"
    assert record.suggested_fix == ""
    assert record.effort_estimate == "low"
    assert record.risk_of_deferral == ""
    # Pre-triage placeholder-six.
    assert record.triage_item_id == ""
    assert record.status == "raw"
    assert record.human_triage is None
    assert record.primary_theme is _PRE_TRIAGE_PLACEHOLDER_THEME
    assert record.struct_theme_id == ""
    assert record.concept_theme_id == ""
    # FindingIssueRecord dataclass-defaulted fields.
    assert record.root_cause_id == ""
    assert record.file == ""
    assert record.function == ""
    assert record.line_range == ""
    assert record.layers == ()
    assert record.consolidated_from == 1
    assert record.lane_provenance == ""
    assert record.needs_human_review is False
    assert record.first_seen == ""


def test_loads_richer_raw_finding_dict_round_trips_fields(
    tmp_path: Path,
) -> None:
    """A 2-element payload with all producer-supplied fields populated
    rehydrates with every field mapped correctly, including the
    ``id`` -> ``finding_id`` translation on each entry."""
    raw = [
        {
            "id": "F-00000000aaaa",
            "summary": "Unchecked Optional return from get_wafer_state",
            "severity": "P1",
            "skill": "layer2a-quick-review",
            "module": "handlers/wafer_state.py",
            "category": "error_handling",
            "location": "handlers/wafer_state.py:147",
            "description": "Unchecked Optional return may raise at runtime.",
            "suggested_fix": "Check for None before dereferencing.",
            "effort_estimate": "medium",
            "risk_of_deferral": "high",
            "root_cause_id": "RC-OPTIONAL-001",
            "file": "handlers/wafer_state.py",
            "function": "get_wafer_state",
            "line_range": "147",
            "layers": ["layer2a"],
            "consolidated_from": 3,
            "lane_provenance": "deterministic",
            "needs_human_review": True,
            "first_seen": "scan-2026-05-14",
        },
        {
            "id": "F-00000000bbbb",
            "summary": "Missing error handling on I/O",
            "severity": "P2",
            "skill": "layer3d-error-handling",
            "module": "io/reader.py",
            "category": "error_handling",
            "location": "io/reader.py:42",
            "description": "I/O call lacks exception handling.",
            "suggested_fix": "Wrap in try/except OSError.",
            "effort_estimate": "low",
            "risk_of_deferral": "medium",
        },
    ]
    (tmp_path / "findings.json").write_text(json.dumps(raw), encoding="utf-8")

    records = load_findings(tmp_path)

    assert len(records) == 2
    first, second = records

    # First record: all producer fields populated.
    assert first.finding_id == "F-00000000aaaa"
    assert first.summary == "Unchecked Optional return from get_wafer_state"
    assert first.severity == "P1"
    assert first.skill == "layer2a-quick-review"
    assert first.module == "handlers/wafer_state.py"
    assert first.category == "error_handling"
    assert first.location == "handlers/wafer_state.py:147"
    assert first.description == "Unchecked Optional return may raise at runtime."
    assert first.suggested_fix == "Check for None before dereferencing."
    assert first.effort_estimate == "medium"
    assert first.risk_of_deferral == "high"
    assert first.root_cause_id == "RC-OPTIONAL-001"
    assert first.file == "handlers/wafer_state.py"
    assert first.function == "get_wafer_state"
    assert first.line_range == "147"
    assert first.layers == ("layer2a",)
    assert first.consolidated_from == 3
    assert first.lane_provenance == "deterministic"
    assert first.needs_human_review is True
    assert first.first_seen == "scan-2026-05-14"
    # Placeholder-six still populated regardless of producer richness.
    assert first.status == "raw"
    assert first.primary_theme is _PRE_TRIAGE_PLACEHOLDER_THEME
    assert isinstance(first.primary_theme, ThemeReference)
    assert first.primary_theme.lens == "concept"
    assert first.primary_theme.theme_id == ""
    assert first.primary_theme.title == ""
    assert first.primary_theme.member_count == 0
    assert first.primary_theme.issue_url is None

    # Second record: id -> finding_id translation verified on a distinct entry.
    assert second.finding_id == "F-00000000bbbb"
    assert second.summary == "Missing error handling on I/O"
    assert second.module == "io/reader.py"
    assert second.skill == "layer3d-error-handling"
    assert second.severity == "P2"
