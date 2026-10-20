"""Cycle 60 — end-to-end Phase 2 pipeline integration test.

Walks one task through the complete lifecycle (PLANNED → PR_CREATED) with
mocked LLM agents and mocked git/gh, but REAL state-machine transitions, REAL
apply_edits on a temp git repo, and the real context builders / StateStore.
This is the narrative test for the execution layer built across cycles 56–59.
"""
from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

import pytest
from unittest.mock import patch

from sentinel.agents.architect import ArchitectAgent
from sentinel.agents.coder import CoderAgent
from sentinel.agents.docs import DocsAgent
from sentinel.agents.qa import QAAgent
from sentinel.config import Config
from sentinel.orchestrator.main import HumanGateRequired, Orchestrator
from sentinel.orchestrator.state_machine import TaskState
from sentinel.phase3.compat_checker import CompatChecker
from sentinel.orchestrator.task_manager import Task

_TARGET_REL = "sentinel/assess/reviewer.py"

# A TODO the real validator accepts: stub names start with test_, stub_code
# raises NotImplementedError, every field non-empty, and both acceptance
# criteria are substring-covered by a stub's acceptance_criterion.
_TODO = {
    "task_id": "task-e2e-001",
    "implementation_plan": {
        "summary": "Null-safe finding_kind extraction",
        "files_to_modify": [_TARGET_REL],
        "approach": "Use raw.get('finding_kind') with a structural default",
        "edge_cases": [],
        "risks": [],
        "dependencies": [],
    },
    "test_stubs": {
        "test_file": "tests/test_fix.py",
        "stubs": [
            {
                "name": "test_null_finding_kind_handled",
                "acceptance_criterion": "null finding_kind handled",
                "scenario": "raw lacks finding_kind",
                "given": "a raw finding dict with no finding_kind key",
                "when": "_extract_findings is called",
                "then": "it returns the default and does not raise",
                "stub_code": "def test_null_finding_kind_handled():\n    raise NotImplementedError\n",
            },
            {
                "name": "test_existing_tests_pass",
                "acceptance_criterion": "existing tests pass",
                "scenario": "regression",
                "given": "the existing suite",
                "when": "the suite runs",
                "then": "all tests pass",
                "stub_code": "def test_existing_tests_pass():\n    raise NotImplementedError\n",
            },
        ],
        "coverage_summary": "covers both acceptance criteria",
    },
    "out_of_scope_findings": [],
}

_EDIT = {
    "file": _TARGET_REL,
    "old_text": "    kind = raw['finding_kind']\n",
    "new_text": "    kind = raw.get('finding_kind') or 'structural_complexity'\n",
}

_IMPL_RESULT = {
    "task_id": "task-e2e-001",
    "files_changed": [_TARGET_REL],
    "diff_summary": "Added null-safe .get() for finding_kind",
    "tests_pass": True,
    "test_results": "1 passed",
    "test_summary": "1 passed in 0.01s",
    "edits": [_EDIT],
    "edit_errors": [],
}


def _make_task(task_id: str, *, is_phase3: bool) -> Task:
    return Task(
        task_id=task_id,
        plan_id="plan-e2e",
        state=TaskState.PLANNED,
        description="Fix null check in _extract_findings",
        affected_files=[_TARGET_REL],
        acceptance_criteria=["null finding_kind handled", "existing tests pass"],
        scope_boundary="Only modify _extract_findings in reviewer.py",
        required_skills=["layer2a-quick-review"],
        verification_required=["layer6a-verification-reviewer"],
        is_phase3=is_phase3,
    )


def _setup_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Temp git repo with the real target file + an out-of-repo state dir."""
    repo_root = tmp_path / "repo"
    target = repo_root / _TARGET_REL
    target.parent.mkdir(parents=True)
    target.write_text(
        "def _extract_findings(raw):\n"
        "    kind = raw['finding_kind']\n"
        "    return kind\n",
        encoding="utf-8",
    )
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", "initial"],
    ):
        proc = subprocess.run(args, cwd=repo_root, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                "git setup command failed in _setup_repo: "
                f"{' '.join(args)} (exit {proc.returncode})\n"
                f"stdout:\n{proc.stdout}\n"
                f"stderr:\n{proc.stderr}"
            )
    return repo_root, target


def _build_config(repo_root: Path, state_dir: Path) -> Config:
    cfg = Config()
    cfg.repo_path = repo_root          # MUST stay a Path (code does repo_path / x)
    cfg.state_dir = state_dir
    cfg.self_approve_mode = False      # non-self path → real PR creation (mocked gh)
    cfg.strict_quality_mode = False    # allow NoOp model client fallback
    return cfg


@contextlib.contextmanager
def _mocked_agents(repo_root: Path, compat_check):
    """Patch every LLM agent method + git/gh so the walk is offline.

    implement() is given a side_effect that applies the real edit to disk via
    apply_edits, so the diff the Architect reviews and the scoped commit are
    backed by a genuine file change.
    """
    from sentinel.workflow.remediation.apply import EditApplicationError, apply_edits

    def _fake_implement(self, _context):
        # Derive the reported implementation result from the *actual* edit
        # outcome rather than returning an unconditional success payload.
        result = dict(_IMPL_RESULT)
        try:
            modified = apply_edits([_EDIT], str(repo_root))
        except EditApplicationError as exc:
            result["files_changed"] = []
            result["edit_errors"] = [str(exc)]
            result["tests_pass"] = False
            return result

        # Confirm the edit actually landed: the target file must now contain
        # the expected replacement text and be reported as modified.
        target_path = repo_root / _EDIT["file"]
        edit_applied = (
            _EDIT["file"] in modified
            and target_path.exists()
            and _EDIT["new_text"].strip() in target_path.read_text(encoding="utf-8")
        )
        result["files_changed"] = list(modified)
        result["edit_errors"] = (
            [] if edit_applied else ["expected replacement text not found after edit"]
        )
        result["tests_pass"] = edit_applied
        return result

    with contextlib.ExitStack() as stack:
        e = stack.enter_context
        e(patch.object(CoderAgent, "submit_todo", return_value=dict(_TODO)))
        e(patch.object(CoderAgent, "implement", autospec=True, side_effect=_fake_implement))
        e(patch.object(CoderAgent, "self_review", return_value={"findings": []}))
        e(patch.object(
            ArchitectAgent, "review_todo",
            return_value={
                "implementation_plan_verdict": "APPROVE",
                "test_stubs_verdict": "APPROVE",
            },
        ))
        e(patch.object(
            ArchitectAgent, "review_diff",
            return_value={"verdict": "APPROVE", "findings": [], "feedback": ""},
        ))
        e(patch.object(
            QAAgent, "verify_fix",
            return_value={"verdict": "PASS", "tdd_assessment": {}},
        ))
        e(patch.object(
            DocsAgent, "generate_transition_manifest",
            return_value={"docs_updated": [], "doc_updated": True},
        ))
        e(patch.object(CompatChecker, "check", compat_check))
        e(patch(
            "sentinel.workflow.remediation.pr_opener.open_pr",
            return_value="https://github.com/test/repo/pull/1",
        ))
        e(patch.object(Orchestrator, "_git_push_branch", return_value=None))
        e(patch.object(Orchestrator, "_git_commit_scoped", return_value=None))
        yield


def test_e2e_phase2_pipeline(tmp_path):
    """One Phase 2 task walks PLANNED → PR_CREATED through every gate."""
    from unittest.mock import MagicMock

    repo_root, target = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    compat_spy = MagicMock(return_value={"verdict": "PASS", "violations": []})

    with _mocked_agents(repo_root, compat_spy):
        orch = Orchestrator(cfg)
        task = _make_task("task-e2e-001", is_phase3=False)
        orch._store.upsert_task(task.to_dict())

        # Walks to the DOC_UPDATED local-review human gate.
        with pytest.raises(HumanGateRequired) as exc:
            orch._run_task(task)
        assert exc.value.gate_type == "local_review"
        assert orch._task_manager.load_task("task-e2e-001").state == TaskState.DOC_UPDATED

        # Phase 2 task must NOT hit the Phase 3 compat gate.
        compat_spy.assert_not_called()

        # The Coder's edit was really applied to disk (cycle 56 apply_edits).
        assert "raw.get('finding_kind')" in target.read_text(encoding="utf-8")

        # Approve → scoped commit (mocked) + real PR (gh mocked) → PR_CREATED.
        orch.approve_local_and_push("task-e2e-001")
        final = orch._task_manager.load_task("task-e2e-001")
        assert final.state == TaskState.PR_CREATED
        assert final.pr_url == "https://github.com/test/repo/pull/1"


def test_e2e_phase3_hits_compat_gate(tmp_path):
    """A Phase 3 task runs the backwards-compat gate (Layer 7c) on the live path."""
    from unittest.mock import MagicMock

    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    # Compat PASSES (returns a dict, no raise) so the walk proceeds.
    compat_spy = MagicMock(return_value={"verdict": "PASS", "violations": []})

    with _mocked_agents(repo_root, compat_spy):
        orch = Orchestrator(cfg)
        task = _make_task("task-e2e-001", is_phase3=True)
        orch._store.upsert_task(task.to_dict())

        with pytest.raises(HumanGateRequired) as exc:
            orch._run_task(task)
        assert exc.value.gate_type == "local_review"

        # The gate fired for the Phase 3 task (proves cycle 59 wiring is live).
        assert compat_spy.called
        called_task = compat_spy.call_args.args[0]
        assert called_task.task_id == "task-e2e-001"

        orch.approve_local_and_push("task-e2e-001")
        assert orch._task_manager.load_task("task-e2e-001").state == TaskState.PR_CREATED
