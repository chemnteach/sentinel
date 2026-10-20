"""Tests for T-1 enriched Coder retry-failure feedback (coder.py pure helpers).

T-1 restructures the string fed back to the Coder on a failed implementation
attempt — no new API calls. These tests exercise the pure helpers that build
that bundle.
"""
from __future__ import annotations

from sentinel.agents.coder import (
    _build_retry_feedback,
    _build_what_went_wrong,
    _parse_expected_actual,
    _present_current_source,
    _summarize_attempt,
)


def test_assertion_parsing_extracts_actual_expected():
    out = (
        "    def test_a():\n"
        ">       assert compute() == 4\n"
        "E       AssertionError: assert 3 == 4\n"
        "FAILED tests/test_x.py::test_a"
    )
    actual, expected = _parse_expected_actual(out)
    assert actual == "3"
    assert expected == "4"


def test_assertion_parsing_returns_fallback_when_no_assertion():
    out = (
        "ImportError: cannot import name 'foo' from 'bar'\n"
        "FAILED tests/test_x.py::test_a - ImportError"
    )
    actual, expected = _parse_expected_actual(out)
    assert actual is None and expected is None
    # the 'what went wrong' section falls back to the first FAILED line
    wrong = _build_what_went_wrong(out)
    assert "## What went wrong" in wrong
    assert "FAILED tests/test_x.py::test_a" in wrong


def test_attempt_summary_truncates_to_two_lines():
    diff = "changed line one\nchanged line two\nchanged line three\nfour"
    summary = _summarize_attempt(diff, had_changes=True)
    assert summary.count("\n") <= 1  # at most two lines
    assert "changed line one" in summary
    assert "changed line two" in summary
    assert "changed line three" not in summary
    # empty file-change set => explicit marker, not a diff summary
    assert _summarize_attempt("anything", had_changes=False) == "no changes applied"


def test_enriched_bundle_contains_all_four_sections():
    bundle = _build_retry_feedback(
        source_section="  42  def compute():\n  43      return 3",
        what_went_wrong=_build_what_went_wrong("E   assert 3 == 4"),
        attempt_summaries=["no changes applied", "edited compute()"],
        raw_tail="E   assert 3 == 4",
    )
    assert "## Current source (post-rollback)" in bundle          # 1. source
    assert "## What went wrong" in bundle                         # 2. expected/actual
    assert "actual:   3" in bundle and "expected: 4" in bundle
    assert "## What was tried" in bundle                          # 3. prior attempts
    assert "Attempt 1:" in bundle and "Attempt 2:" in bundle
    assert "complete this sentence in one line" in bundle         # 4. forced diagnosis


def test_enriched_bundle_degrades_gracefully_when_source_empty():
    # Source retrieval returned nothing: bundle omits the source section, does
    # not crash, and still carries the raw pytest tail as a fallback.
    bundle = _build_retry_feedback(
        source_section="",
        what_went_wrong=_build_what_went_wrong("FAILED tests/test_x.py::test_a"),
        attempt_summaries=[],
        raw_tail="the raw pytest tail text",
    )
    assert "## Current source (post-rollback)" not in bundle
    assert "the raw pytest tail text" in bundle
    assert "complete this sentence in one line" in bundle
    # _present_current_source degrades to "" (no crash) with no/edit-less input
    assert _present_current_source([], repo_root=".") == ""
    assert _present_current_source([{"old_text": "x"}], repo_root=".") == ""
