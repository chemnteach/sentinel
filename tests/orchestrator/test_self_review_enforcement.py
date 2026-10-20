"""FIX 1 — Coder self-review Critical/P1 findings block submission.

Before this change, a Critical/P1 finding from the Coder's own self-review only
logged a warning and the diff advanced to the Architect review anyway (the
coder.py docstring promised these block submission). These tests pin the gate:

- A Critical self-review finding sends the task back to IN_PROGRESS for
  revision — it never reaches the Architect diff review.
- A P2-only self-review finding does NOT block: the diff proceeds to review.
- Exhaustion (>3 self-review rejections) escalates cleanly (ESCALATED +
  HumanGateRequired), never a crash.

Reuses the real _run_task harness from tests/test_integration_e2e (temp git
repo, real state machine, mocked LLM agents).
"""

from __future__ import annotations

import contextlib

import pytest
from unittest.mock import MagicMock, patch

from sentinel.agents.architect import ArchitectAgent
from sentinel.agents.coder import CoderAgent
from sentinel.agents.docs import DocsAgent
from sentinel.agents.qa import QAAgent
from sentinel.orchestrator.main import HumanGateRequired, Orchestrator
from sentinel.orchestrator.state_machine import TaskState
from sentinel.phase3.compat_checker import CompatChecker

from tests.test_integration_e2e import (
    _EDIT,
    _IMPL_RESULT,
    _TODO,
    _build_config,
    _make_task,
    _setup_repo,
)

_CRITICAL = {"findings": [{"severity": "Critical", "description": "unsafe eval() introduced"}]}
_P2_ONLY = {"findings": [{"severity": "P2", "description": "minor naming nit"}]}
_CLEAN = {"findings": []}


@contextlib.contextmanager
def _agents(repo_root, *, self_review):
    """Mocked-agent stack with a configurable ``self_review`` and spyable
    ``review_diff`` / ``implement`` mocks (yielded to the test)."""
    from sentinel.workflow.remediation.apply import apply_edits

    def _fake_implement(self, _context):
        # Idempotent: on a send-back the task re-enters implement(), but the
        # edit from the prior attempt is still on disk (the real Coder rolls
        # back internally; this fake does not). Skip re-applying when the
        # replacement text is already present.
        target = repo_root / _EDIT["file"]
        if _EDIT["new_text"].strip() not in target.read_text(encoding="utf-8"):
            apply_edits([_EDIT], str(repo_root))
        return dict(_IMPL_RESULT)

    if isinstance(self_review, list):
        self_review_kwargs = {"side_effect": list(self_review)}
    else:
        self_review_kwargs = {"return_value": self_review}

    with contextlib.ExitStack() as stack:
        e = stack.enter_context
        e(patch.object(CoderAgent, "submit_todo", return_value=dict(_TODO)))
        implement_mock = e(patch.object(
            CoderAgent, "implement", autospec=True, side_effect=_fake_implement))
        e(patch.object(CoderAgent, "self_review", **self_review_kwargs))
        e(patch.object(
            ArchitectAgent, "review_todo",
            return_value={
                "implementation_plan_verdict": "APPROVE",
                "test_stubs_verdict": "APPROVE",
            },
        ))
        review_diff_mock = e(patch.object(
            ArchitectAgent, "review_diff",
            return_value={"verdict": "APPROVE", "findings": [], "feedback": ""}))
        e(patch.object(QAAgent, "verify_fix", return_value={"verdict": "PASS", "tdd_assessment": {}}))
        e(patch.object(
            DocsAgent, "generate_transition_manifest",
            return_value={"docs_updated": [], "doc_updated": True},
        ))
        e(patch.object(CompatChecker, "check", MagicMock(return_value={"verdict": "PASS", "violations": []})))
        yield {"implement": implement_mock, "review_diff": review_diff_mock}


def test_critical_self_review_sends_back_to_in_progress(tmp_path):
    """A Critical self-review finding is sent back for revision, not advanced
    to the Architect diff review. After a clean re-review it proceeds normally."""
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")

    # Critical on the first self-review, clean thereafter → one send-back, then
    # the revised diff proceeds all the way to the DOC_UPDATED local-review gate.
    with _agents(repo_root, self_review=[_CRITICAL, _CLEAN, _CLEAN]) as mocks:
        orch = Orchestrator(cfg)
        task = _make_task("task-sr-back", is_phase3=False)
        orch._store.upsert_task(task.to_dict())

        with pytest.raises(HumanGateRequired) as exc:
            orch._run_task(task)
        assert exc.value.gate_type == "local_review"

        # The critical pass never reached the Architect: review_diff runs only
        # once, on the clean recursion.
        assert mocks["review_diff"].call_count == 1
        # Re-implementation happened (initial + one send-back).
        assert mocks["implement"].call_count == 2

        final = orch._task_manager.load_task("task-sr-back")
        assert final.review_attempts == 1
        assert "self_review" in (final.retry_context or "")


def test_p2_only_self_review_proceeds_to_submitted(tmp_path):
    """A P2-only self-review finding does not block: the diff reaches review."""
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")

    with _agents(repo_root, self_review=_P2_ONLY) as mocks:
        orch = Orchestrator(cfg)
        task = _make_task("task-sr-p2", is_phase3=False)
        orch._store.upsert_task(task.to_dict())

        with pytest.raises(HumanGateRequired) as exc:
            orch._run_task(task)
        assert exc.value.gate_type == "local_review"

        # Proceeded straight to review — no send-back.
        assert mocks["review_diff"].call_count == 1
        assert mocks["implement"].call_count == 1

        final = orch._task_manager.load_task("task-sr-p2")
        assert final.review_attempts == 0


def test_self_review_exhaustion_escalates_cleanly(tmp_path):
    """>3 self-review rejections escalate (ESCALATED + HumanGateRequired), and
    the diff never reaches the Architect review."""
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")

    with _agents(repo_root, self_review=_CRITICAL) as mocks:
        orch = Orchestrator(cfg)
        task = _make_task("task-sr-exh", is_phase3=False)
        # Pre-load the shared counter so the first Critical crosses the >3 cap.
        task.review_attempts = 3
        orch._store.upsert_task(task.to_dict())

        with pytest.raises(HumanGateRequired) as exc:
            orch._run_task(task)
        assert exc.value.gate_type == "self_review_failure"

        # Escalation is straight to a human — the Architect never reviewed it.
        mocks["review_diff"].assert_not_called()

        final = orch._task_manager.load_task("task-sr-exh")
        assert final.state == TaskState.ESCALATED
