"""Operational Risk (layer2j) coverage under the reliability-first profile.

The operational-surface lens (unbounded timeouts, partial-failure cascades,
weak failure-path observability, unsafe retry) is the primary driver of
production idle-time / nuisance-error cycle cost on runtime-control code, so it
must run on EVERY module under reliability-first — not only when a keyword
trigger happens to fire.
"""
from __future__ import annotations

from sentinel.assess.reviewer import (
    _RELIABILITY_FIRST_ALLOWED,
    _RELIABILITY_FIRST_ALWAYS_ON,
    _determine_specialists,
)
from sentinel.skills.loader import Skills


def _route_reliability_first(finding: dict, module_content: str = "") -> list[str]:
    return _determine_specialists(
        [finding],
        module_content=module_content,
        recommended_skills=None,
        module=None,
        review_profile="reliability-first",
    )


def test_operational_risk_is_always_on_under_reliability_first():
    """A module with NO operational-risk keyword still gets the layer2j skill."""
    neutral = {"description": "a plain observation with no routing keyword",
               "summary": "", "finding_kind": "logic_bug"}
    assert Skills.OPERATIONAL_RISK in _route_reliability_first(neutral)


def test_operational_risk_in_always_on_set():
    assert Skills.OPERATIONAL_RISK in _RELIABILITY_FIRST_ALWAYS_ON


def test_always_on_is_subset_of_allowed():
    """Always-on skills must survive the reliability-first allowlist filter."""
    assert _RELIABILITY_FIRST_ALWAYS_ON <= _RELIABILITY_FIRST_ALLOWED


def test_operational_risk_survives_allowlist_with_recommended_skills():
    """Even when Layer 1 recommends an unrelated (dropped) specialist, the
    always-on operational-risk skill is still selected."""
    finding = {"description": "unrelated", "summary": "", "finding_kind": "logic_bug"}
    result = _determine_specialists(
        [finding],
        module_content="",
        recommended_skills=[Skills.MAINTAINABILITY],  # not on the reliability allowlist
        module=None,
        review_profile="reliability-first",
    )
    assert Skills.OPERATIONAL_RISK in result
