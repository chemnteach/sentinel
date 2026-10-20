"""P1 wave-3 tests for sentinel.govern.conditions.

Covers:
  - get_changed_files_from_git: failures now log (WARNING on non-zero exit,
    ERROR on exception) instead of returning [] silently.
  - merge_conditions: an empty SCOPE_LIMIT intersection across disjoint file
    sets now emits an explicit most-restrictive ("none") SCOPE_LIMIT rather
    than omitting it (which would mean "no scope restriction").
"""

from __future__ import annotations

import logging
import subprocess
from unittest.mock import patch

from sentinel.govern.conditions import (
    get_changed_files_from_git,
    merge_conditions,
)


# ---------------------------------------------------------------------------
# get_changed_files_from_git — failures must be visible
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_nonzero_returncode_logs_warning_and_returns_empty(caplog):
    fake = _FakeCompleted(returncode=128, stdout="", stderr="fatal: bad revision 'HEAD~1'")
    with patch.object(subprocess, "run", return_value=fake):
        with caplog.at_level(logging.WARNING, logger="sentinel.govern.conditions"):
            result = get_changed_files_from_git("/some/repo")

    assert result == []
    assert "git diff for changed files failed" in caplog.text
    assert "bad revision" in caplog.text


def test_exception_logs_error_and_returns_empty(caplog):
    with patch.object(subprocess, "run", side_effect=OSError("git not found")):
        with caplog.at_level(logging.ERROR, logger="sentinel.govern.conditions"):
            result = get_changed_files_from_git("/some/repo")

    assert result == []
    assert "Failed to read changed files from git" in caplog.text


def test_success_returns_stripped_file_list_no_warning(caplog):
    fake = _FakeCompleted(returncode=0, stdout="a.py\n b.py \n\n")
    with patch.object(subprocess, "run", return_value=fake):
        with caplog.at_level(logging.WARNING, logger="sentinel.govern.conditions"):
            result = get_changed_files_from_git("/some/repo")

    assert result == ["a.py", "b.py"]
    assert caplog.text == ""


# ---------------------------------------------------------------------------
# merge_conditions — empty SCOPE_LIMIT intersection
# ---------------------------------------------------------------------------


def _scope_limits(merged):
    return [c for c in (merged or []) if c.get("type") == "SCOPE_LIMIT"]


def test_disjoint_scope_limits_emit_none_not_absent():
    """Two disjoint SCOPE_LIMITs intersect to nothing; the merge must emit an
    explicit empty ('none') scope rather than dropping SCOPE_LIMIT entirely."""
    a = [{"type": "SCOPE_LIMIT", "target": "sentinel/govern/pr.py", "note": None}]
    b = [{"type": "SCOPE_LIMIT", "target": "sentinel/agents/qa.py", "note": None}]

    merged = merge_conditions([a, b])

    limits = _scope_limits(merged)
    assert len(limits) == 1, "a SCOPE_LIMIT must still be present after disjoint merge"
    assert limits[0]["target"] == "none"


def test_overlapping_scope_limits_intersect_normally():
    """A non-empty intersection is unaffected by the new empty-intersection branch."""
    shared = "sentinel/govern/pr.py"
    a = [{"type": "SCOPE_LIMIT", "target": f"{shared},sentinel/agents/qa.py", "note": None}]
    b = [{"type": "SCOPE_LIMIT", "target": f"{shared},sentinel/agents/docs.py", "note": None}]

    merged = merge_conditions([a, b])

    limits = _scope_limits(merged)
    assert len(limits) == 1
    assert limits[0]["target"] == "govern/pr.py"


def test_single_input_scope_limit_preserved():
    """merge_conditions short-circuits for a single input; SCOPE_LIMIT survives."""
    a = [{"type": "SCOPE_LIMIT", "target": "sentinel/govern/pr.py", "note": None}]

    merged = merge_conditions([a])

    limits = _scope_limits(merged)
    assert len(limits) == 1
    assert limits[0]["target"] == "sentinel/govern/pr.py"
