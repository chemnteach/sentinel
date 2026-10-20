"""Workflow-track loader for raw findings persisted by the assess track.

Cycle 27 Item 8 sub-step 8-C: provides a ``findings.json`` ->
``FindingIssueRecord`` rehydration bridge so workflow-track consumers
(starting with the cycle-28 Stage 4c concept-theme builder) can hydrate
typed records from the deduplicated finding payload the assess track
already writes to ``<state_dir>/findings.json``.

Pre-triage subset caveat
------------------------
``FindingIssueRecord`` carries six required fields that are downstream of
the dedup/reconciliation stage and therefore NOT present in the raw
``findings.json`` payload:

    triage_item_id, status, human_triage,
    primary_theme, struct_theme_id, concept_theme_id

This loader synthesises sentinel placeholders for those six fields so the
dataclass constructor succeeds:

    triage_item_id  = ""
    status          = "raw"   (sentinel literal, see below)
    human_triage    = None
    primary_theme   = _PRE_TRIAGE_PLACEHOLDER_THEME
                      (ThemeReference(lens="concept", theme_id="",
                                      title="", member_count=0,
                                      issue_url=None))
    struct_theme_id = ""
    concept_theme_id = ""

The literal ``"raw"`` on ``status`` is a sentinel value reserved by this
loader to signal a pre-triage-state record; downstream code that needs
real triage state must not rely on records produced here. The records
emitted by this loader are therefore concept-theme-builder-ready (the
builder reads description/summary/severity/skill/file/module fields, all
of which are populated from the raw dict) but NOT triage-ready or
issue-emission-ready. Any consumer that needs the downstream-six should
hydrate them from triage state (``tech_debt.json``) rather than from this
loader.

Regime boundary
---------------
``findings.json`` uses the canonical raw-dict JSON key ``"id"`` for the
F2 deterministic identifier (per ``materialize_emitter_finding``); the
typed ``FindingIssueRecord`` exposes the same value through the
``finding_id`` attribute. This loader performs that one-way translation at
the boundary -- ``raw["id"] -> record.finding_id`` -- preserving the
cycle-23 two-regime separation between raw-dict JSON and typed dataclass.

Cross-track-assess decoupling
-----------------------------
This module reads ``<state_dir>/findings.json`` directly via ``Path``,
mirroring the workflow-track loader pattern established by
``struct_theme_loader.load_semantic_groups``. It does not import
``StateStore`` even though ``"findings"`` is registered there: keeping
the workflow-track loaders disk-coupled preserves the cycle-9
workflow/assess decoupling discipline (Q1 decision 10-B). The
silent-default-empty-tuple contract on absent/corrupt input matches the
sibling loader exactly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sentinel.workflow.models import FindingIssueRecord, ThemeReference

# Reads the JSON shape emitted by sentinel.assess.finding_schema.
# materialize_emitter_finding (file <state_dir>/findings.json); update if
# that producer shape changes.
_FINDINGS_FILENAME = "findings.json"

# Sentinel placeholder for the six downstream-of-dedup fields on
# FindingIssueRecord that the raw findings payload cannot populate.
# Constant exposed at module scope (rather than constructed inline) so
# the placeholder shape can be pinned by tests and inspected by callers.
_PRE_TRIAGE_PLACEHOLDER_THEME: ThemeReference = ThemeReference(
    lens="concept",
    theme_id="",
    title="",
    member_count=0,
    issue_url=None,
)

__all__ = ["load_findings"]


def _deserialize_finding(payload: dict[str, Any]) -> FindingIssueRecord:
    """Reconstruct a ``FindingIssueRecord`` from one raw findings entry.

    Performs the raw-dict-key -> typed-field translation at the regime
    boundary (raw ``"id"`` -> ``finding_id``) and synthesises the six
    downstream-of-dedup placeholder fields per the module docstring.
    Tolerant ``.get`` reads with conservative defaults cover any
    producer-side fields that may be absent on older payloads.
    """
    return FindingIssueRecord(
        # Pre-triage placeholders (downstream-of-dedup fields).
        triage_item_id="",
        status="raw",
        human_triage=None,
        primary_theme=_PRE_TRIAGE_PLACEHOLDER_THEME,
        struct_theme_id="",
        concept_theme_id="",
        # Regime boundary: raw "id" -> finding_id.
        finding_id=payload.get("id", ""),
        # Producer-supplied fields with tolerant defaults.
        summary=payload.get("summary", ""),
        severity=payload.get("severity", "P3"),
        skill=payload.get("skill", ""),
        module=payload.get("module", ""),
        category=payload.get("category", "general"),
        location=payload.get("location", ""),
        description=payload.get("description", ""),
        suggested_fix=payload.get("suggested_fix", ""),
        effort_estimate=payload.get("effort_estimate", "low"),
        risk_of_deferral=payload.get("risk_of_deferral", ""),
        # Optional defaulted fields on FindingIssueRecord: pass through
        # only when present, otherwise let the dataclass defaults apply.
        root_cause_id=payload.get("root_cause_id", ""),
        file=payload.get("file", ""),
        function=payload.get("function", ""),
        line_range=payload.get("line_range", ""),
        layers=tuple(payload.get("layers") or ()),
        consolidated_from=payload.get("consolidated_from", 1),
        lane_provenance=payload.get("lane_provenance", ""),
        needs_human_review=payload.get("needs_human_review", False),
        first_seen=payload.get("first_seen", ""),
    )


def load_findings(state_dir: str | Path) -> tuple[FindingIssueRecord, ...]:
    """Load raw findings as a tuple of ``FindingIssueRecord``.

    Reads ``<state_dir>/findings.json``. Returns an empty tuple when the
    file is absent, unreadable, contains invalid JSON, or does not hold a
    top-level list -- the same silent-default convention used by
    ``struct_theme_loader.load_semantic_groups`` so callers need not
    distinguish absent from corrupt.
    """
    path = Path(state_dir) / _FINDINGS_FILENAME
    if not path.exists():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ()
    if not isinstance(payload, list):
        return ()
    return tuple(_deserialize_finding(entry) for entry in payload)
