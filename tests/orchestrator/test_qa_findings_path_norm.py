"""FIX 5 — QA original-findings path matching is normalized.

The QA original-findings assembly matched a finding's ``module`` against
``task.affected_files`` with exact string comparison, so a Windows-style or
mixed-case module path silently failed to match and QA verified with no
original-finding context. The match is now case-insensitive and
separator-independent (``Path(...).as_posix().lower()``).
"""

from __future__ import annotations

from sentinel.orchestrator.main import _module_in_affected


def test_windows_module_matches_posix_affected():
    assert _module_in_affected(
        r"sentinel\assess\reviewer.py",
        ["sentinel/assess/reviewer.py"],
    )


def test_mixed_case_module_matches():
    assert _module_in_affected(
        "Sentinel/Assess/Reviewer.PY",
        ["sentinel/assess/reviewer.py"],
    )


def test_different_path_does_not_match():
    assert not _module_in_affected(
        "sentinel/assess/other.py",
        ["sentinel/assess/reviewer.py"],
    )


def test_empty_module_does_not_match():
    assert not _module_in_affected("", ["sentinel/assess/reviewer.py"])
