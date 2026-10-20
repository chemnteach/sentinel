"""Cycle 58 (Commit 2) — real PR creation via gh, with graceful fallback."""
from __future__ import annotations

from types import SimpleNamespace

from sentinel.workflow.remediation import pr_opener
from sentinel.workflow.remediation.pr_opener import build_pr_body, open_pr


def _fake_run(returncode, stdout="", stderr=""):
    def run(*args, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    return run


# --- open_pr -----------------------------------------------------------------

def test_open_pr_gh_not_found(monkeypatch):
    monkeypatch.setattr(pr_opener, "_find_gh", lambda: None)
    assert open_pr("/repo", "sentinel/T1", "title", "body") is None


def test_open_pr_gh_fails(monkeypatch):
    monkeypatch.setattr(pr_opener, "_find_gh", lambda: "gh")
    monkeypatch.setattr(pr_opener.subprocess, "run", _fake_run(1, stderr="boom"))
    assert open_pr("/repo", "sentinel/T1", "title", "body") is None


def test_open_pr_gh_succeeds(monkeypatch):
    url = "https://github.com/org/repo/pull/42"
    monkeypatch.setattr(pr_opener, "_find_gh", lambda: "gh")
    monkeypatch.setattr(pr_opener.subprocess, "run", _fake_run(0, stdout=url + "\n"))
    assert open_pr("/repo", "sentinel/T1", "title", "body") == url


# --- build_pr_body -----------------------------------------------------------

def _task(**kw):
    base = dict(
        task_id="T1",
        description="Fix the null deref in parser",
        files_changed=["sentinel/parse.py"],
        acceptance_criteria=["No null deref", "Tests pass"],
        conditions_structured=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_pr_body_basic():
    body = build_pr_body(_task())
    assert "Sentinel Task: T1" in body
    assert "Fix the null deref in parser" in body
    assert "`sentinel/parse.py`" in body
    assert "No null deref" in body
    assert "Reviewer Conditions" not in body


def test_build_pr_body_with_conditions():
    task = _task(
        conditions_structured=[{"type": "REQUIRE", "note": "add a regression test"}]
    )
    body = build_pr_body(task, qa_verdict="PASS")
    assert "Reviewer Conditions" in body
    assert "**REQUIRE**: add a regression test" in body
    assert "### QA Verdict" in body
