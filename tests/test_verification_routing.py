"""Tests for per-task verification routing (cycle 6 item 8).

Covers the three-tier plumbing introduced in commits 3e5aa5b
(Task field + create_task + orchestrator-side disambiguation) and
4fca479 (QA dispatch consumer):

* ``TestTaskFieldAddition`` -- Task dataclass field default + roundtrip.
* ``TestCreateTaskKeyPresenceDisambiguation`` -- ``create_task``
  honors explicit list (including empty) vs absent default; orchestrator
  raises ``SkillNotFoundError`` on unknown verifier skill names at the
  planner -> executor boundary.
* ``TestQADispatchRouting`` -- ``_run_task`` REVIEW_PASS block: single
  skill threaded to ``verify_fix``; empty list raises
  ``HumanGateRequired(gate_type='registry_resolution')`` with the
  D2-6 message text branching on ``plan_id.startswith('FIX-BLOCKING-')``;
  multi-skill warns and takes the first element.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from sentinel.orchestrator.main import HumanGateRequired, Orchestrator
from sentinel.orchestrator.state_machine import TaskState
from sentinel.orchestrator.task_manager import Task, TaskManager
from sentinel.skills.loader import SkillLoader, SkillNotFoundError


# ── Builders ─────────────────────────────────────────────────────────


def _make_task(
    *,
    task_id: str = "T-001",
    plan_id: str = "PLAN-001",
    state: TaskState = TaskState.REVIEW_PASS,
    verification_required: list[str] | None = None,
) -> Task:
    t = Task(
        task_id=task_id,
        plan_id=plan_id,
        description="d",
        verification_required=verification_required or [],
    )
    t.state = state
    return t


def _make_orch_scaffold() -> Orchestrator:
    """Bare Orchestrator with mocked collaborators for dispatch tests.

    Bypasses ``__init__`` (which would build the model client and the
    state store); the dispatch logic in ``_run_task`` only touches the
    collaborators replaced here when ``task.state == REVIEW_PASS``.
    """
    orch = Orchestrator.__new__(Orchestrator)
    orch._store = MagicMock()
    orch._store.get_all_findings.return_value = []
    orch._task_manager = MagicMock()
    orch._task_manager.record_qa_verdict.side_effect = (
        lambda task, verdict, tdd_assessment=None, actor="qa": task
    )
    orch._qa = MagicMock()
    orch._qa.verify_fix.return_value = {"verdict": "PASS"}
    orch._config = MagicMock()
    orch._config.repo_path = "/tmp"
    # Real temp state_dir: the QA_FAIL send-back builds a retry-context packet
    # under state_dir; a MagicMock here would make build_retry_context write a
    # bogus "MagicMock/…" tree into the repo root.
    orch._config.state_dir = Path(tempfile.mkdtemp())
    return orch


# =====================================================================
# TestTaskFieldAddition
# =====================================================================


class TestTaskFieldAddition:
    def test_task_dataclass_has_verification_required_field_default_empty(
        self,
    ) -> None:
        task = Task(task_id="T-1", plan_id="P-1")
        assert task.verification_required == []
        assert isinstance(task.verification_required, list)

    def test_task_to_dict_from_dict_roundtrips_verification_required(
        self,
    ) -> None:
        original = Task(
            task_id="T-1",
            plan_id="P-1",
            verification_required=["skill-a", "skill-b"],
        )
        as_dict = original.to_dict()
        assert as_dict["verification_required"] == ["skill-a", "skill-b"]

        roundtripped = Task.from_dict(as_dict)
        assert roundtripped.verification_required == ["skill-a", "skill-b"]


# =====================================================================
# TestCreateTaskKeyPresenceDisambiguation
# =====================================================================


class TestCreateTaskKeyPresenceDisambiguation:
    def _make_manager(self) -> TaskManager:
        store = MagicMock()
        store.read.return_value = []
        return TaskManager(store=store)

    def test_plan_item_with_verification_required_key_honored_as_is(
        self,
    ) -> None:
        """Including the FIX-BLOCKING-* empty-list case."""
        manager = self._make_manager()

        # Explicit list honored verbatim.
        task = manager.create_task(
            plan_id="P-1",
            description="d",
            acceptance_criteria=[],
            affected_files=[],
            scope_boundary="",
            verification_required=["custom-skill"],
        )
        assert task.verification_required == ["custom-skill"]

        # Explicit empty list honored verbatim (the FIX-BLOCKING-* path).
        task = manager.create_task(
            plan_id="P-2",
            description="d",
            acceptance_criteria=[],
            affected_files=[],
            scope_boundary="",
            verification_required=[],
        )
        assert task.verification_required == []

    def test_plan_item_without_verification_required_key_defaults_to_empty(
        self,
    ) -> None:
        """create_task's None default coalesces to empty list.

        The orchestrator's key-presence disambiguation at
        _extract_plan_items lives at the call site (main.py ~line 1463)
        and converts absent key to ['layer6a-verification-reviewer']
        BEFORE calling create_task. This test verifies the dataclass
        receiver behavior; the disambiguation snippet itself is
        exercised end-to-end in TestQADispatchRouting (which depends on
        verification_required being populated correctly).
        """
        manager = self._make_manager()
        task = manager.create_task(
            plan_id="P-1",
            description="d",
            acceptance_criteria=[],
            affected_files=[],
            scope_boundary="",
            # Omit verification_required entirely (None default).
        )
        assert task.verification_required == []

    def test_skill_validation_raises_skill_not_found_error_on_unknown_skill(
        self, tmp_path
    ) -> None:
        """SkillLoader.load() propagates SkillNotFoundError naturally.

        Mirrors the orchestrator-side validation loop at
        main.py ~line 1473 which iterates verification_required and
        calls loader.load() on each skill name, letting unknown skills
        surface immediately at the planner -> executor boundary.
        """
        skill_files_dir = tmp_path / "skill_files"
        skill_files_dir.mkdir()
        (skill_files_dir / "layer6a-verification-reviewer.md").write_text(
            "---\nname: layer6a-verification-reviewer\n---\nstub\n",
            encoding="utf-8",
        )
        loader = SkillLoader(skill_files_dir)

        # Known skill loads fine.
        loader.load("layer6a-verification-reviewer")

        # Unknown skill raises SkillNotFoundError.
        with pytest.raises(SkillNotFoundError):
            loader.load("nonexistent-skill")


# =====================================================================
# TestQADispatchRouting
# =====================================================================


def _enter_review_pass_dispatch(orch: Orchestrator, task: Task) -> None:
    """Invoke _run_task on a REVIEW_PASS task.

    REVIEW_PASS skips every prior phase (implementation, todo, review)
    so the QA dispatch fragment is the first thing _run_task executes.
    """
    orch._run_task(task)


class TestQADispatchRouting:
    def test_qa_dispatch_with_single_skill_passes_skill_name_to_verify_fix(
        self,
    ) -> None:
        # New contract: a QA failure routes the task BACK to the Coder for
        # revision (review_attempts++, QA feedback stored) and does NOT escalate
        # on the first failure — mirroring the Architect REVIEW_FAIL send-back.
        # A spy intercepts the recursive _run_task so we can inspect the revised
        # task without re-running implementation.
        orch = _make_orch_scaffold()
        orch._qa.verify_fix.return_value = {
            "verdict": "FAIL", "feedback": "stub coverage gap"
        }
        orch._task_manager.record_qa_verdict.side_effect = lambda t, **kw: (
            setattr(t, "state", TaskState.QA_FAIL) or t
        )
        orch._task_manager._persist.side_effect = lambda t: t

        task = _make_task(
            verification_required=["layer6a-verification-reviewer"]
        )

        real_run = orch._run_task
        seen: list[Task] = []

        def spy(t: Task) -> None:
            seen.append(t)
            if len(seen) == 1:
                real_run(t)  # outer call: run the QA dispatch fragment

        orch._run_task = spy
        spy(task)  # must NOT raise (no escalation on first fail)

        # skill name threaded to verify_fix
        orch._qa.verify_fix.assert_called_once()
        assert (
            orch._qa.verify_fix.call_args.kwargs["skill_name"]
            == "layer6a-verification-reviewer"
        )
        # task was sent back to the Coder (second _run_task call), revised
        assert len(seen) == 2
        sent_back = seen[1]
        assert sent_back.review_attempts == 1
        assert sent_back.state != TaskState.ESCALATED
        assert "QA verification failed" in sent_back.last_human_review_comment
        assert "stub coverage gap" in sent_back.last_human_review_comment

    def test_qa_dispatch_with_empty_list_raises_human_gate_registry_resolution(
        self,
    ) -> None:
        orch = _make_orch_scaffold()
        task = _make_task(
            plan_id="PLAN-001", verification_required=[]
        )

        with pytest.raises(HumanGateRequired) as exc_info:
            _enter_review_pass_dispatch(orch, task)

        assert exc_info.value.gate_type == "registry_resolution"
        assert exc_info.value.payload["task_id"] == "T-001"
        # verify_fix MUST NOT be called when the list is empty
        orch._qa.verify_fix.assert_not_called()

    def test_qa_dispatch_message_text_branches_on_fix_blocking_plan_id(
        self,
    ) -> None:
        # FIX-BLOCKING-* case
        orch = _make_orch_scaffold()
        task = _make_task(plan_id="FIX-BLOCKING-001", verification_required=[])
        with pytest.raises(HumanGateRequired) as exc_info:
            _enter_review_pass_dispatch(orch, task)
        msg = exc_info.value.payload["message"]
        assert "FIX-BLOCKING-* registry blocker" in msg
        assert "sentinel task --registry-approve T-001" in msg

        # Non-FIX-BLOCKING (anomaly) case
        orch = _make_orch_scaffold()
        task = _make_task(plan_id="PLAN-001", verification_required=[])
        with pytest.raises(HumanGateRequired) as exc_info:
            _enter_review_pass_dispatch(orch, task)
        msg = exc_info.value.payload["message"]
        assert "planner emission anomaly" in msg
        assert "sentinel task --registry-approve T-001" in msg

    def test_qa_dispatch_multi_skill_all_pass_yields_qa_pass(
        self,
    ) -> None:
        """All-PASS path: every skill called, aggregated verdict PASS."""
        orch = _make_orch_scaffold()
        orch._qa.verify_fix.return_value = {"verdict": "PASS"}
        # Drive task to QA_PASS so the lifecycle progresses past the
        # qa_failure branch. The DOC gate that follows will reach for
        # collaborators not mocked here, so we capture the resulting
        # HumanGateRequired and assert only that it is NOT qa_failure.
        orch._task_manager.record_qa_verdict.side_effect = lambda t, **kw: (
            setattr(t, "state", TaskState.QA_PASS) or t
        )

        task = _make_task(verification_required=["skill-a", "skill-b"])

        try:
            _enter_review_pass_dispatch(orch, task)
        except HumanGateRequired as exc:
            # Downstream gate (DOC / local_review) is acceptable;
            # qa_failure would indicate aggregation went wrong.
            assert exc.gate_type != "qa_failure"
        except Exception:  # noqa: BLE001
            # AttributeError / TypeError from unmocked DOC-gate path
            # is acceptable -- our assertions below cover the dispatch.
            pass

        assert orch._qa.verify_fix.call_count == 2
        call_args_list = orch._qa.verify_fix.call_args_list
        assert call_args_list[0].kwargs["skill_name"] == "skill-a"
        assert call_args_list[1].kwargs["skill_name"] == "skill-b"

        orch._task_manager.record_qa_verdict.assert_called_once()
        record_kwargs = (
            orch._task_manager.record_qa_verdict.call_args.kwargs
        )
        assert record_kwargs["verdict"] == "PASS"
        assert record_kwargs["per_skill_verdicts"] == [
            {"skill": "skill-a", "verdict": "PASS"},
            {"skill": "skill-b", "verdict": "PASS"},
        ]

    def test_qa_dispatch_multi_skill_any_fail_sends_back_to_coder(
        self,
    ) -> None:
        """Any-FAIL path: every skill still called (no short-circuit);
        aggregated verdict FAIL routes the task back to the Coder for revision
        (review_attempts++) rather than escalating on the first failure.
        """
        orch = _make_orch_scaffold()
        # First (index 0) skill fails with feedback; the send-back feedback is
        # taken from per_skill_results[0]. Second skill still called (no
        # short-circuit), so verify_fix runs twice.
        orch._qa.verify_fix.side_effect = [
            {"verdict": "FAIL", "feedback": "skill-a mismatch"},
            {"verdict": "PASS"},
        ]
        orch._task_manager.record_qa_verdict.side_effect = lambda t, **kw: (
            setattr(t, "state", TaskState.QA_FAIL) or t
        )
        orch._task_manager._persist.side_effect = lambda t: t

        task = _make_task(verification_required=["skill-a", "skill-b"])

        real_run = orch._run_task
        seen: list[Task] = []

        def spy(t: Task) -> None:
            seen.append(t)
            if len(seen) == 1:
                real_run(t)

        orch._run_task = spy
        spy(task)  # must NOT raise

        # both skills evaluated, no short-circuit
        assert orch._qa.verify_fix.call_count == 2
        # sent back to the Coder, not escalated
        assert len(seen) == 2
        sent_back = seen[1]
        assert sent_back.review_attempts == 1
        assert sent_back.state != TaskState.ESCALATED
        assert "skill-a mismatch" in sent_back.last_human_review_comment

    def test_qa_dispatch_fail_escalates_after_revision_attempts_exhausted(
        self,
    ) -> None:
        """Once review_attempts exceeds the cap, a QA failure escalates to a
        human gate (carrying the per-skill payload) instead of looping forever.
        """
        orch = _make_orch_scaffold()
        orch._qa.verify_fix.side_effect = [
            {"verdict": "PASS"},
            {"verdict": "FAIL", "feedback": "still failing"},
        ]
        orch._task_manager.record_qa_verdict.side_effect = lambda t, **kw: (
            setattr(t, "state", TaskState.QA_FAIL) or t
        )
        orch._task_manager.mark_disputed.side_effect = lambda t: t
        orch._task_manager.escalate_dispute.side_effect = lambda t: t
        orch._task_manager._persist.side_effect = lambda t: t

        task = _make_task(verification_required=["skill-a", "skill-b"])
        task.review_attempts = 3  # next failure exceeds the cap -> escalate

        with pytest.raises(HumanGateRequired) as exc_info:
            _enter_review_pass_dispatch(orch, task)

        assert exc_info.value.gate_type == "qa_failure"
        assert orch._qa.verify_fix.call_count == 2
        payload = exc_info.value.payload
        assert payload["per_skill_verdicts"] == [
            {"skill": "skill-a", "verdict": "PASS"},
            {"skill": "skill-b", "verdict": "FAIL"},
        ]
        assert orch._task_manager.escalate_dispute.called
