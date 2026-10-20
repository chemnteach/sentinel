"""Deterministic alarm-relevance classification for findings.

A finding's alarm relevance is one of:

- ``"direct"``   — the finding is in alarm-raising / handling / clearing /
  configuration code.
- ``"adjacent"`` — the finding is in code that feeds alarm decisions
  (thresholds, setpoints, interlocks, state an alarm reads) but is not the
  alarm itself.
- ``"none"``     — unrelated to alarms.

The classifier here is generic and keyword-based. It is used both to *seed*
the Architect LLM classification pass (Stage 5c-alarm) and as the *sole*
classifier in local-only / offline mode.
"""

from __future__ import annotations

from typing import Mapping

DIRECT = "direct"
ADJACENT = "adjacent"
NONE = "none"

# Generic alarm vocabulary — substring matched against a lowercased blob of the
# finding's textual fields. Kept target-agnostic (no repo-specific file names).
DIRECT_TERMS: tuple[str, ...] = (
    "alarm",
    "ecsalarm",
    "alarmreport",
    "alarmreported",
    "alarmid",
    "alarmdata",
    "alarmaction",
    "alarmstate",
    "alarmlist",
    "alarmenable",
    "setalarm",
    "clearalarm",
    "raisealarm",
)

ADJACENT_TERMS: tuple[str, ...] = (
    "threshold",
    "setpoint",
    "interlock",
    "nuisance",
    "trip point",
    "fault threshold",
    "safety interlock",
)

# Finding dict fields scanned for alarm vocabulary, in priority order.
_TEXT_FIELDS: tuple[str, ...] = (
    "description",
    "summary",
    "file",
    "function",
    "location",
    "finding_kind",
    "subject_key",
    "category",
    "suggested_fix",
)


def _finding_text(finding: Mapping[str, object]) -> str:
    """Join a finding's textual fields into one lowercased search blob."""
    parts: list[str] = []
    for field in _TEXT_FIELDS:
        value = finding.get(field)
        if value:
            parts.append(str(value))
    return " ".join(parts).lower()


def classify_finding(
    finding: Mapping[str, object],
    *,
    direct_terms: tuple[str, ...] = DIRECT_TERMS,
    adjacent_terms: tuple[str, ...] = ADJACENT_TERMS,
) -> tuple[str, str]:
    """Return ``(relevance, rationale)`` for one finding.

    ``direct`` wins over ``adjacent`` when both vocabularies match. ``none``
    carries an empty rationale.
    """
    blob = _finding_text(finding)

    direct_hits = sorted({t for t in direct_terms if t in blob})
    if direct_hits:
        return DIRECT, "matched alarm term(s): " + ", ".join(direct_hits)

    adjacent_hits = sorted({t for t in adjacent_terms if t in blob})
    if adjacent_hits:
        return ADJACENT, "matched alarm-adjacent term(s): " + ", ".join(adjacent_hits)

    return NONE, ""


def seed_alarm_relevance(findings: list) -> list[dict]:
    """Deterministically label a list of findings.

    Returns one entry per input element (order preserved) of the form
    ``{"index": i, "alarm_relevance": ..., "alarm_rationale": ...}``. Non-dict
    entries are labeled ``none`` so the result aligns 1:1 with the input.
    """
    seeds: list[dict] = []
    for i, finding in enumerate(findings):
        if isinstance(finding, Mapping):
            relevance, rationale = classify_finding(finding)
        else:
            relevance, rationale = NONE, ""
        seeds.append(
            {"index": i, "alarm_relevance": relevance, "alarm_rationale": rationale}
        )
    return seeds
