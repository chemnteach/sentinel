"""Regression tests for run-016 remediation of reviewer.py.

Covers self-contained MINIMUM-CHANGE fixes:
  - FIX-REQUIRED-012: review_module resets the per-module invalid-kind counter.
  - FIX-REQUIRED-023: unsupported review_profile is validated (warn + fallback).
  - FIX-REQUIRED-101: reliability-first excluded recommendations are audited.
  - FIX-REQUIRED-327: _PayloadRetryResult.status is a closed Literal contract.
"""
from __future__ import annotations

import logging
import typing

from sentinel.assess.reviewer import (
    ReviewOrchestrator,
    _PayloadRetryResult,
    _determine_specialists,
)
from sentinel.skills.loader import Skills


class _ArchitectStub:
    def specialist_review(self, skill, context):  # pragma: no cover - unused here
        return {"findings": []}


class _StoreStub:
    def append_finding(self, finding):
        pass


class _ConfigStub:
    strict_quality_mode = False
    chunk_max_lines = 200
    chunk_overlap_lines = 20
    max_chunks_per_module = 10


class _ScanContextStub:
    repo_root_canonical = ""


def _make_orchestrator() -> ReviewOrchestrator:
    return ReviewOrchestrator(
        architect=_ArchitectStub(),
        store=_StoreStub(),
        config=_ConfigStub(),
        scan_context=_ScanContextStub(),
    )


# ── FIX-REQUIRED-012 ──────────────────────────────────────────────────────
def test_review_module_resets_invalid_kind_counter():
    """review_module must zero the per-module counter before doing any work,
    so a count from a prior module cannot leak into the next review."""
    orch = _make_orchestrator()
    orch._invalid_kind_counter = [7]

    class _Boom(Exception):
        pass

    # Raise right after the reset (chunking is the first step after it) so we
    # isolate the reset from the rest of the cascade.
    def _boom(*args, **kwargs):
        raise _Boom()

    orch._chunker.chunk_string = _boom
    try:
        orch.review_module({"name": "m", "path": "m.py"}, "print('x')\n", {})
    except _Boom:
        pass

    assert orch._invalid_kind_counter == [0]


# ── FIX-REQUIRED-023 ──────────────────────────────────────────────────────
def test_unsupported_review_profile_warns_and_falls_back(caplog):
    with caplog.at_level(logging.WARNING, logger="sentinel.assess.reviewer"):
        result = _determine_specialists(
            [],
            module_content="",
            recommended_skills=None,
            module=None,
            review_profile="reliability_first",  # typo: underscore, not hyphen
        )
    assert "Unsupported review_profile" in caplog.text
    # Falls back to 'triggered'; empty findings + empty content ⇒ no specialists.
    assert result == []


def test_supported_review_profiles_do_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="sentinel.assess.reviewer"):
        _determine_specialists([], module_content="", review_profile="triggered")
        _determine_specialists([], module_content="", review_profile="reliability-first")
    assert "Unsupported review_profile" not in caplog.text


# ── FIX-REQUIRED-101 ──────────────────────────────────────────────────────
def test_reliability_first_audits_excluded_recommended_skills(caplog):
    # PERFORMANCE is a valid Layer-1 recommendation but not on the
    # reliability-first allowlist — its exclusion must be logged.
    with caplog.at_level(logging.INFO, logger="sentinel.assess.reviewer"):
        result = _determine_specialists(
            [],
            module_content="",
            recommended_skills=[Skills.PERFORMANCE],
            module=None,
            review_profile="reliability-first",
        )
    assert Skills.PERFORMANCE not in result
    assert "excluded Layer-1 recommended" in caplog.text
    assert Skills.PERFORMANCE in caplog.text


# ── FIX-REQUIRED-327 ──────────────────────────────────────────────────────
def test_payload_retry_result_status_is_closed_literal():
    hints = typing.get_type_hints(_PayloadRetryResult)
    assert typing.get_args(hints["status"]) == ("complete", "partial", "failed")
