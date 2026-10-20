"""Cycle 59 — Phase 3 BackwardsCompatChecker (Layer 7c) wired into _run_task."""
from __future__ import annotations

from types import SimpleNamespace

from sentinel.orchestrator.main import Orchestrator
from sentinel.orchestrator.state_machine import TaskState
from sentinel.orchestrator.task_manager import Task, TaskManager
from sentinel.phase3 import compat_checker as cc_mod
from sentinel.phase3.compat_checker import CompatibilityViolation


class _FakeStore:
    def __init__(self):
        self.tasks = {}
        self.audits = []

    def upsert_task(self, d):
        self.tasks[d["task_id"]] = d

    def append_audit(self, d):
        self.audits.append(d)


def _phase3_task(**kw):
    base = dict(task_id="T1", plan_id="FER-1", is_phase3=True, source_fer_id="FER-1")
    base.update(kw)
    return Task(**base)


# --- _run_compat_check normalization -----------------------------------------

def test_run_compat_check_pass(monkeypatch):
    class _OK:
        def __init__(self, qa, store): ...
        def check(self, task, diff): return {"verdict": "PASS", "violations": []}

    monkeypatch.setattr(cc_mod, "CompatChecker", _OK)
    fake = SimpleNamespace(_qa=None, _store=None)
    result = Orchestrator._run_compat_check(fake, _phase3_task(), "diff")
    assert result["compatible"] is True
    assert result["issues"] == []


def test_run_compat_check_violation_returns_incompatible(monkeypatch):
    violations = [
        {"severity": "BREAKING", "location": "api.py:10", "description": "removed param"}
    ]

    class _Fail:
        def __init__(self, qa, store): ...
        def check(self, task, diff):
            raise CompatibilityViolation("T1", violations)

    monkeypatch.setattr(cc_mod, "CompatChecker", _Fail)
    fake = SimpleNamespace(_qa=None, _store=None)
    result = Orchestrator._run_compat_check(fake, _phase3_task(), "diff")
    assert result["compatible"] is False
    assert "1 breaking change" in result["summary"]
    assert any("removed param" in i for i in result["issues"])


def test_run_compat_check_error_is_indeterminate_not_compatible(monkeypatch):
    class _Boom:
        def __init__(self, qa, store): ...
        def check(self, task, diff):
            raise RuntimeError("agent exploded")

    monkeypatch.setattr(cc_mod, "CompatChecker", _Boom)
    fake = SimpleNamespace(_qa=None, _store=None)
    result = Orchestrator._run_compat_check(fake, _phase3_task(), "diff")
    # FIX-BLOCKING-006: a checker execution failure is indeterminate, not
    # confirmed compatibility — it must not report compatible=True.
    assert result["compatible"] is False
    assert result["status"] == "checker_error"
    assert "could not complete" in result["summary"]


# --- record_compat_failure (REVIEW_PASS -> QA_FAIL send-back) -----------------

def test_record_compat_failure_routes_to_qa_fail():
    tm = TaskManager(store=_FakeStore())
    task = _phase3_task(state=TaskState.REVIEW_PASS)
    out = tm.record_compat_failure(
        task,
        {"compatible": False, "summary": "API changed", "issues": ["broken contract"]},
    )
    # REVIEW_PASS -> QA_FAIL (REVIEW_PASS -> IN_PROGRESS is illegal); the
    # _run_task resume then advances QA_FAIL -> IN_PROGRESS.
    assert out.state == TaskState.QA_FAIL
    assert "BACKWARDS COMPATIBILITY CHECK FAILED" in out.compat_feedback
    assert "broken contract" in out.compat_feedback


# --- compat_feedback injection into the Coder's next context -----------------

def test_compat_feedback_injected_into_implementation_context():
    tm = TaskManager(store=_FakeStore())
    task = _phase3_task(
        compat_feedback="BREAKING: removed public method foo()",
        test_stubs={"test_file": "tests/test_x.py", "stubs": []},
    )
    context = tm.build_implementation_context(task, repo_root="")
    assert "BACKWARDS COMPATIBILITY FEEDBACK FROM PREVIOUS ATTEMPT" in context
    assert "removed public method foo()" in context


# --- the is_phase3 guard discriminator ---------------------------------------

def test_is_phase3_guard_defaults_false():
    # Phase 2 tasks must not trip the gate; Phase 3 tasks must.
    assert Task(task_id="t", plan_id="p").is_phase3 is False
    assert _phase3_task().is_phase3 is True
