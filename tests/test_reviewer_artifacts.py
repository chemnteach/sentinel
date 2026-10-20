"""Cycle 57 (Commit 1) — Architect reviews the real git diff."""
from __future__ import annotations

import subprocess
from pathlib import Path

import sentinel
from sentinel.orchestrator.task_manager import (
    Task,
    TaskManager,
    _get_git_diff,
)

_REPO_ROOT = str(Path(sentinel.__file__).resolve().parent.parent)


# --- _get_git_diff -----------------------------------------------------------

def test_get_git_diff_real_repo():
    # May be empty if the working tree is clean — that is correct. No crash.
    out = _get_git_diff(_REPO_ROOT, [])
    assert isinstance(out, str)


def test_get_git_diff_bad_repo():
    assert _get_git_diff("/nonexistent/path/repo") == ""


# --- build_diff_review_context (Architect sees the real diff) ----------------

def test_build_diff_review_context_includes_diff(tmp_path):
    repo = tmp_path

    def _git(*args):
        # Fail fast with visible diagnostics if any setup command errors, so the
        # diff-content assertions below only run against a verified repo state.
        proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
        assert proc.returncode == 0, (
            f"git {' '.join(args)} failed (exit {proc.returncode})\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
        return proc

    _git("init")
    _git("config", "user.email", "t@t.dev")
    _git("config", "user.name", "t")
    f = repo / "src.py"
    f.write_text("value = 1\n", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "init")

    f.write_text("value = 999\n", encoding="utf-8")  # uncommitted edit

    task = Task(
        task_id="T1",
        plan_id="P1",
        description="bump value",
        affected_files=["src.py"],
        acceptance_criteria=["value is 999"],
    )
    context = TaskManager(store=None).build_diff_review_context(
        task, diff_summary="prose summary", repo_root=str(repo)
    )
    assert "ACTUAL GIT DIFF" in context
    assert "value = 999" in context


def test_build_diff_review_context_falls_back_to_prose(tmp_path):
    # No git changes (not even a repo) -> prose fallback, never context-less.
    task = Task(
        task_id="T1",
        plan_id="P1",
        description="bump value",
        affected_files=["src.py"],
        acceptance_criteria=["value is 999"],
    )
    context = TaskManager(store=None).build_diff_review_context(
        task, diff_summary="prose summary here", repo_root=str(tmp_path)
    )
    assert "NO ACTUAL DIFF" in context
    assert "prose summary here" in context
