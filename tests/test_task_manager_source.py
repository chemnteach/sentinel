"""Tests for _read_source_files — feeding real source to the Coder (cycle 56)."""
from __future__ import annotations

from pathlib import Path

import sentinel
from sentinel.orchestrator.main import _normalize_affected_files
from sentinel.orchestrator.task_manager import _read_source_files

# Repo root = the directory containing the installed sentinel/ package (portable).
_REPO_ROOT = str(Path(sentinel.__file__).resolve().parent.parent)


def test_read_source_files_existing():
    out = _read_source_files(["sentinel/config.py"], _REPO_ROOT)
    assert out
    assert "SOURCE FILES TO MODIFY" in out
    assert "sentinel/config.py" in out


def test_read_source_files_missing():
    assert _read_source_files(["nonexistent/file.py"], _REPO_ROOT) == ""


def test_normalize_affected_files_adds_prefix(tmp_path):
    # Create the file under sentinel/
    target = tmp_path / "sentinel" / "govern" / "conditions.py"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")

    result = _normalize_affected_files(
        ["govern/conditions.py"],
        str(tmp_path),
    )
    assert result == ["sentinel/govern/conditions.py"]


def test_normalize_affected_files_keeps_prefix(tmp_path):
    result = _normalize_affected_files(
        ["sentinel/govern/conditions.py"],
        str(tmp_path),
    )
    assert result == ["sentinel/govern/conditions.py"]


def test_normalize_affected_files_keeps_missing(tmp_path):
    # File doesn't exist under sentinel/ either
    result = _normalize_affected_files(
        ["govern/phantom.py"],
        str(tmp_path),
    )
    assert result == ["govern/phantom.py"]


def test_read_source_files_resolves_subtree_relative(tmp_path):
    # run-011: a sentinel/ subtree assessment records affected_files like
    # govern/conditions.py (no sentinel/ prefix). _read_source_files must find it
    # under sentinel/ rather than silently skipping it — a skip starves the Coder
    # of the real source and it hallucinates the function body.
    target = tmp_path / "sentinel" / "govern" / "conditions.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def get_effective_criteria(task):\n    return []\n", encoding="utf-8"
    )
    out = _read_source_files(["govern/conditions.py"], str(tmp_path))
    assert out
    assert "def get_effective_criteria(task):" in out


def test_read_source_files_truncates(tmp_path):
    f = tmp_path / "big.py"
    f.write_text("\n".join(f"line{i}" for i in range(500)), encoding="utf-8")
    out = _read_source_files(
        ["big.py"], str(tmp_path),
        max_lines_per_file=10,
        full_file_threshold=10
    )
    assert "more lines truncated" in out


def test_read_source_files_includes_small_file_in_full(
    tmp_path,
) -> None:
    # A file under full_file_threshold must be
    # included without truncation even if it
    # exceeds max_lines_per_file.
    f = tmp_path / "small.py"
    f.write_text(
        "\n".join(f"line{i}" for i in range(500)),
        encoding="utf-8",
    )
    out = _read_source_files(
        ["small.py"], str(tmp_path),
        max_lines_per_file=10,
        full_file_threshold=1500,
    )
    assert "more lines truncated" not in out
    assert "line499" in out  # last line present
