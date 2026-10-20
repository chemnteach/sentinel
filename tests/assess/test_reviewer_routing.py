"""Routing-corpus coverage for FIX-REQUIRED-014.

`_determine_specialists` must build its trigger-matching corpus from all
supported normalized finding fields (summary, description, finding_kind,
category, subject_key) — not `description` alone — so a routing signal that
lives outside `description` still selects the right specialist.

Uses the real ``"performance"`` trigger from ``_SPECIALIST_TRIGGERS``
(→ ``Skills.PERFORMANCE``); an empty ``module_content`` and no recommended
skills isolate the corpus behavior from the security/refactoring side paths.
"""
from __future__ import annotations

from sentinel.assess.reviewer import (
    _ROUTING_CORPUS_FIELDS,
    _determine_specialists,
)
from sentinel.skills.loader import Skills

TRIGGER = "performance"  # _SPECIALIST_TRIGGERS["performance"] -> [Skills.PERFORMANCE]
NEUTRAL = "a plain observation with no routing keyword"


def _route(finding: dict) -> list[str]:
    return _determine_specialists(
        [finding],
        module_content="",
        recommended_skills=None,
        module=None,
        review_profile="triggered",
    )


def _finding_with_signal_in(field: str) -> dict:
    """A finding whose only trigger signal lives in ``field``."""
    finding = {f: NEUTRAL for f in _ROUTING_CORPUS_FIELDS}
    finding[field] = f"{TRIGGER} concern noted"
    return finding


# criterion [0] positive
def test_empty_description_routes_when_signal_in_summary():
    finding = {"description": "", "summary": f"{TRIGGER} regression observed"}
    assert Skills.PERFORMANCE in _route(finding)


# criterion [0] negative
def test_no_specialist_when_no_field_carries_trigger():
    finding = {f: NEUTRAL for f in _ROUTING_CORPUS_FIELDS}
    result = _route(finding)
    assert Skills.PERFORMANCE not in result
    assert result == []


# criterion [1] — every supported field contributes to the corpus
def test_corpus_built_from_each_supported_field():
    for field in _ROUTING_CORPUS_FIELDS:
        result = _route(_finding_with_signal_in(field))
        assert Skills.PERFORMANCE in result, (
            f"signal carried by {field!r} did not reach the routing corpus"
        )


# criterion [2] — same specialist set regardless of which field carries the signal
def test_equivalent_findings_route_identically_across_fields():
    base = _route(_finding_with_signal_in("description"))
    for field in _ROUTING_CORPUS_FIELDS:
        assert _route(_finding_with_signal_in(field)) == base


# criterion [3] — regression guard: a description-only corpus would miss this
def test_summary_only_signal_routes_regression_guard():
    finding = {"description": NEUTRAL, "summary": f"{TRIGGER} hotspot"}
    # Under a description-only implementation NEUTRAL has no trigger, so the
    # specialist would be absent and this assertion would fail.
    assert Skills.PERFORMANCE in _route(finding)


# criterion [4] — existing description-based routing is unchanged
def test_description_signal_still_routes():
    finding = {"description": f"{TRIGGER} bottleneck in hot loop"}
    assert Skills.PERFORMANCE in _route(finding)
