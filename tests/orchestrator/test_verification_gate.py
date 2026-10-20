"""Tests for the semantic verification gate in _run_task (DOC_UPDATED)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentinel.config import Config
import sentinel.config as config_module
from sentinel.orchestrator.main import HumanGateRequired, Orchestrator
from sentinel.orchestrator.task_manager import Task
from sentinel.orchestrator.state_machine import TaskState


@pytest.fixture(autouse=True)
def _reset_global_config_singleton():
    config_module._config = None
    yield
    config_module._config = None


def _seed_task(state_dir: Path, **overrides) -> dict:
    task = {
        "task_id": "task-1",
        "plan_id": "FIX-REQUIRED-001",
        "state": "DOC_UPDATED",
        "pattern_id": "P-1",
        "affected_files": ["sentinel/foo.py"],
        "test_stubs_approved": True,
        "todo_approved": True,
        "review_attempts": 0,
        "revision": 0,
    }
    task.update(overrides)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tasks.json").write_text(json.dumps([task], indent=2), encoding="utf-8")
    return task


def _make_orch(tmp_path: Path, gate: bool = True):
    repo = tmp_path / "repo"
    repo.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        env={
            "SENTINEL_REPO_PATH": str(repo),
            "SENTINEL_LOCAL_ONLY_MODE": "true",
            "SENTINEL_STATE_DIR": str(state_dir),
            "SENTINEL_VERIFICATION_GATE": "true" if gate else "false",
        },
        load_dotenv_file=False,
    )
    orch = Orchestrator(cfg)
    return orch, state_dir


# ---------------------------------------------------------------------------
# 1. Gate disabled → verification skipped
# ---------------------------------------------------------------------------

def test_gate_disabled_skips_verification(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, gate=False)
    called = {"verify": False}

    def _verify(task, sha):
        called["verify"] = True
        return (False, "should not run")

    monkeypatch.setattr(orch, "_run_semantic_verification", _verify)
    monkeypatch.setattr(orch, "_resolve_task_severity", lambda t: "Critical")
    task = Task.from_dict(_seed_task(state_dir))

    with orch:
        result = orch._run_verification_gate(task, diff_summary="")

    assert result is None
    assert called["verify"] is False


# ---------------------------------------------------------------------------
# 2. P2 severity → verification skipped
# ---------------------------------------------------------------------------

def test_p2_severity_skips_verification(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, gate=True)
    called = {"verify": False}

    def _verify(task, sha):
        called["verify"] = True
        return (False, "should not run")

    monkeypatch.setattr(orch, "_run_semantic_verification", _verify)
    monkeypatch.setattr(orch, "_resolve_task_severity", lambda t: "P2")
    task = Task.from_dict(_seed_task(state_dir))

    with orch:
        result = orch._run_verification_gate(task, diff_summary="")

    assert result is None
    assert called["verify"] is False


# ---------------------------------------------------------------------------
# 3. Critical + verified=True → proceed (returns None)
# ---------------------------------------------------------------------------

def test_critical_verified_true_proceeds(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, gate=True)
    monkeypatch.setattr(orch, "_run_semantic_verification", lambda t, s: (True, "resolved"))
    monkeypatch.setattr(orch, "_resolve_task_severity", lambda t: "Critical")
    task = Task.from_dict(_seed_task(state_dir))

    with orch:
        result = orch._run_verification_gate(task, diff_summary="")

    assert result is None  # proceed to local review
    assert task.state == TaskState.DOC_UPDATED  # unchanged


# ---------------------------------------------------------------------------
# 4. Critical + verified=False → reopened to IN_PROGRESS with retry context
# ---------------------------------------------------------------------------

def test_critical_verified_false_reopens(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, gate=True)
    monkeypatch.setattr(
        orch, "_run_semantic_verification",
        lambda t, s: (False, "P1 finding persists in foo"),
    )
    monkeypatch.setattr(orch, "_resolve_task_severity", lambda t: "P1")
    task = Task.from_dict(_seed_task(state_dir))

    with orch:
        result = orch._run_verification_gate(task, diff_summary="")

    assert result is not None
    assert result.state == TaskState.IN_PROGRESS
    assert "persists" in result.retry_context
    assert result.review_attempts == 1


# ---------------------------------------------------------------------------
# 5. Critical + verified=False past retry cap → escalates
# ---------------------------------------------------------------------------

def test_verified_false_escalates_after_cap(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, gate=True)
    monkeypatch.setattr(
        orch, "_run_semantic_verification",
        lambda t, s: (False, "still broken"),
    )
    monkeypatch.setattr(orch, "_resolve_task_severity", lambda t: "Critical")
    task = Task.from_dict(_seed_task(state_dir, review_attempts=3))

    with orch:
        with pytest.raises(HumanGateRequired) as exc:
            orch._run_verification_gate(task, diff_summary="")

    assert exc.value.gate_type == "semantic_verification_failure"


# ---------------------------------------------------------------------------
# 6. pre_fix_sha captured before Coder edits
# ---------------------------------------------------------------------------

def test_pre_fix_sha_captured(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, gate=True)
    monkeypatch.setattr(orch, "_git_head_sha", lambda repo_path: "abc123def")
    monkeypatch.setattr(orch._task_manager, "_persist", lambda task: None)
    task = Task.from_dict(_seed_task(state_dir, state="IN_PROGRESS", pre_fix_sha=""))

    with orch:
        result = orch._capture_pre_fix_sha(task)

    assert result.pre_fix_sha == "abc123def"


def test_pre_fix_sha_not_overwritten(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, gate=True)
    monkeypatch.setattr(orch, "_git_head_sha", lambda repo_path: "NEW")
    monkeypatch.setattr(orch._task_manager, "_persist", lambda task: None)
    task = Task.from_dict(_seed_task(state_dir, state="IN_PROGRESS", pre_fix_sha="ORIGINAL"))

    with orch:
        result = orch._capture_pre_fix_sha(task)

    assert result.pre_fix_sha == "ORIGINAL"


# ---------------------------------------------------------------------------
# 7. Verification exceeding the timeout fails open (verified=True) + WARNING
# ---------------------------------------------------------------------------

def test_verification_timeout_fails_closed(tmp_path, monkeypatch, caplog):
    # Security control: a hung verification worker must NOT silently pass the
    # gate. The timeout wrapper fails CLOSED (verified=False) and lets the
    # caller decide whether to escalate.
    import time

    orch, state_dir = _make_orch(tmp_path, gate=True)
    orch._config.verification_timeout_seconds = 0.1

    def _slow(task):
        time.sleep(1.0)
        return (True, "should not reach")

    monkeypatch.setattr(orch, "_do_semantic_verification", _slow)
    task = Task.from_dict(_seed_task(state_dir, state="DOC_UPDATED"))

    with orch:
        with caplog.at_level("WARNING"):
            verified, reason = orch._run_semantic_verification(task, "presha")

    assert verified is False
    assert "timed out" in reason.lower()
    assert "gate not cleared" in reason.lower()
    assert any("timed out" in rec.getMessage().lower() for rec in caplog.records)

