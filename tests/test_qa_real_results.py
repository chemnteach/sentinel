"""Cycle 57 (Commit 2) — QA verifies the real live test output."""
from __future__ import annotations

from sentinel.orchestrator.context import build_verification_context


def test_build_verification_context_includes_test_results():
    task = {"task_id": "T1", "description": "d", "acceptance_criteria": ["c1"]}
    context = build_verification_context(
        task=task,
        diff="some diff",
        original_findings=[],
        test_results="2 passed in 0.1s\n",
        tests_pass=True,
        test_summary="2 passed in 0.1s",
    )
    assert "REAL TEST RESULTS" in context
    assert "**Status:** PASS" in context
    assert "2 passed in 0.1s" in context


def test_build_verification_context_status_fail():
    task = {"task_id": "T1", "description": "d", "acceptance_criteria": ["c1"]}
    context = build_verification_context(
        task=task,
        diff="some diff",
        original_findings=[],
        test_results="1 failed\n",
        tests_pass=False,
        test_summary="1 failed in 0.1s",
    )
    assert "**Status:** FAIL" in context
