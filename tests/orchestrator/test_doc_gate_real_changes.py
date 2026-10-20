"""FIX 4 — the documentation gate requires real on-disk doc changes.

Before this change, ``doc_updated=True`` from the model satisfied the gate even
when zero documentation files were actually written. These tests pin the gate to
on-disk reality:

- docs_updated paths that exist on disk → gate passes.
- docs_updated=[] with NO .md in affected_files (code-only) → gate passes
  (the code-only exception avoids false failures).
- docs_updated=[] with a .md in scope → gate fails, task sent back to retry.
- docs_updated paths that do NOT exist on disk → gate fails, task sent back.

Reuses the real _run_task harness from tests/test_integration_e2e.
"""

from __future__ import annotations

import contextlib

import pytest
from unittest.mock import patch

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
    _TARGET_REL,
    _TODO,
    _build_config,
    _make_task,
    _setup_repo,
)

_MD_REL = "docs/guide.md"


def _doc_result(docs_updated):
    return {
        "task_id": "task-doc",
        "before_summary": "before",
        "after_summary": "after",
        "docs_updated": list(docs_updated),
        "doc_updated": True,
    }


@contextlib.contextmanager
def _agents(repo_root, *, generate):
    from sentinel.workflow.remediation.apply import apply_edits

    def _fake_implement(self, _context):
        target = repo_root / _EDIT["file"]
        if _EDIT["new_text"].strip() not in target.read_text(encoding="utf-8"):
            apply_edits([_EDIT], str(repo_root))
        return dict(_IMPL_RESULT)

    gen_kwargs = {"side_effect": list(generate)} if isinstance(generate, list) else {"return_value": generate}

    with contextlib.ExitStack() as stack:
        e = stack.enter_context
        e(patch.object(CoderAgent, "submit_todo", return_value=dict(_TODO)))
        e(patch.object(CoderAgent, "implement", autospec=True, side_effect=_fake_implement))
        e(patch.object(CoderAgent, "self_review", return_value={"findings": []}))
        e(patch.object(
            ArchitectAgent, "review_todo",
            return_value={"implementation_plan_verdict": "APPROVE", "test_stubs_verdict": "APPROVE"},
        ))
        e(patch.object(
            ArchitectAgent, "review_diff",
            return_value={"verdict": "APPROVE", "findings": [], "feedback": ""},
        ))
        e(patch.object(QAAgent, "verify_fix", return_value={"verdict": "PASS", "tdd_assessment": {}}))
        gen_mock = e(patch.object(DocsAgent, "generate_transition_manifest", **gen_kwargs))
        e(patch.object(CompatChecker, "check", return_value={"verdict": "PASS", "violations": []}))
        yield gen_mock


def _doc_task(orch, task_id, affected_files):
    task = _make_task(task_id, is_phase3=False)
    task.affected_files = list(affected_files)
    orch._store.upsert_task(task.to_dict())
    return task


def test_docs_updated_paths_exist_passes_gate(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    (repo_root / _MD_REL).parent.mkdir(parents=True, exist_ok=True)
    (repo_root / _MD_REL).write_text("# guide\n", encoding="utf-8")
    cfg = _build_config(repo_root, tmp_path / "state")

    with _agents(repo_root, generate=_doc_result([_MD_REL])) as gen:
        orch = Orchestrator(cfg)
        task = _doc_task(orch, "task-doc-ok", [_TARGET_REL, _MD_REL])

        with pytest.raises(HumanGateRequired) as exc:
            orch._run_task(task)
        assert exc.value.gate_type == "local_review"
        assert gen.call_count == 1
        assert orch._task_manager.load_task("task-doc-ok").state == TaskState.DOC_UPDATED


def test_code_only_empty_docs_passes_gate(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")

    # No .md in affected_files → docs_updated=[] is acceptable (code-only).
    with _agents(repo_root, generate=_doc_result([])) as gen:
        orch = Orchestrator(cfg)
        task = _doc_task(orch, "task-doc-codeonly", [_TARGET_REL])

        with pytest.raises(HumanGateRequired) as exc:
            orch._run_task(task)
        assert exc.value.gate_type == "local_review"
        assert gen.call_count == 1
        assert orch._task_manager.load_task("task-doc-codeonly").state == TaskState.DOC_UPDATED


def test_empty_docs_with_md_in_scope_sends_back(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    (repo_root / _MD_REL).parent.mkdir(parents=True, exist_ok=True)
    (repo_root / _MD_REL).write_text("# guide\n", encoding="utf-8")
    cfg = _build_config(repo_root, tmp_path / "state")

    # First doc attempt updates nothing (gate fails → send back); second
    # attempt actually lists the on-disk .md (gate passes).
    with _agents(repo_root, generate=[_doc_result([]), _doc_result([_MD_REL])]) as gen:
        orch = Orchestrator(cfg)
        task = _doc_task(orch, "task-doc-empty-md", [_TARGET_REL, _MD_REL])

        with pytest.raises(HumanGateRequired) as exc:
            orch._run_task(task)
        assert exc.value.gate_type == "local_review"
        # The gate blocked the first attempt and retried the doc step.
        assert gen.call_count == 2
        final = orch._task_manager.load_task("task-doc-empty-md")
        assert final.state == TaskState.DOC_UPDATED
        assert final.review_attempts == 1
        assert "documentation" in (final.retry_context or "").lower()


def test_nonexistent_docs_paths_sends_back(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    (repo_root / _MD_REL).parent.mkdir(parents=True, exist_ok=True)
    (repo_root / _MD_REL).write_text("# guide\n", encoding="utf-8")
    cfg = _build_config(repo_root, tmp_path / "state")

    # First attempt claims a doc path that does not exist → gate fails.
    with _agents(
        repo_root,
        generate=[_doc_result(["docs/ghost.md"]), _doc_result([_MD_REL])],
    ) as gen:
        orch = Orchestrator(cfg)
        task = _doc_task(orch, "task-doc-ghost", [_TARGET_REL, _MD_REL])

        with pytest.raises(HumanGateRequired) as exc:
            orch._run_task(task)
        assert exc.value.gate_type == "local_review"
        assert gen.call_count == 2
        assert orch._task_manager.load_task("task-doc-ghost").review_attempts == 1
