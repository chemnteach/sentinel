"""
tests/test_govern.py

Tests for the governance pipeline's PR-manifest construction and
rendering helpers:
  - PRManifestBuilder output
  - PRManifest format_for_human rendering
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from sentinel.govern.pr import PRManifest, PRManifestBuilder
from sentinel.orchestrator.state_machine import TaskState


# ---------------------------------------------------------------------------
# Fixtures — minimal Task-like object
# ---------------------------------------------------------------------------

@dataclass
class FakeTask:
    """Minimal task stand-in for tests that don't need full task_manager."""

    task_id: str = "task-abc12345"
    plan_id: str = "plan-001"
    state: TaskState = TaskState.DOC_UPDATED
    description: str = "Fix null pointer in user login"
    acceptance_criteria: list[str] = field(default_factory=lambda: [
        "Login succeeds when credentials are valid",
        "Login fails gracefully when credentials are invalid",
    ])
    affected_files: list[str] = field(default_factory=lambda: ["auth/login.py"])
    scope_boundary: str = "auth/login.py only — do not modify auth/logout.py"
    test_stubs_approved: bool = True
    doc_updated: bool = True
    docs_updated: list[str] = field(default_factory=lambda: ["auth/README.md"])
    tests_pass: bool = False
    test_results: str = ""
    test_stubs: dict[str, Any] = field(default_factory=lambda: {
        "stubs": [
            {"name": "test_login_success", "stub_code": "raise NotImplementedError"},
            {"name": "test_login_invalid_creds", "stub_code": "raise NotImplementedError"},
        ]
    })

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "state": self.state.value,
            "description": self.description,
            "acceptance_criteria": self.acceptance_criteria,
            "affected_files": self.affected_files,
            "scope_boundary": self.scope_boundary,
            "test_stubs_approved": self.test_stubs_approved,
            "doc_updated": self.doc_updated,
        }


# ---------------------------------------------------------------------------
# PRManifestBuilder tests
# ---------------------------------------------------------------------------

class TestPRManifestBuilder:
    def test_build_populates_all_fields(self) -> None:
        builder = PRManifestBuilder()
        task = FakeTask()

        manifest = builder.build(
            task=task,
            plan_item={"id": "plan-001", "description": "Fix login"},
            diff_summary="Added null check in auth/login.py",
            files_changed=["auth/login.py"],
            source_findings=[{"severity": "P1", "description": "NPE in login"}],
        )

        assert manifest.task_id == "task-abc12345"
        assert manifest.plan_id == "plan-001"
        assert manifest.description == "Fix null pointer in user login"
        assert manifest.test_stubs_approved is True
        assert manifest.doc_updated is True
        assert "auth/login.py" in manifest.files_changed
        assert manifest.diff_summary == "Added null check in auth/login.py"
        assert len(manifest.source_findings) == 1

    def test_branch_name_uses_sentinel_prefix(self) -> None:
        builder = PRManifestBuilder()
        branch = PRManifestBuilder._branch_name("task-abc12345")
        assert branch.startswith("sentinel/")
        assert "task-abc12345" in branch

    def test_pr_url_contains_branch(self) -> None:
        builder = PRManifestBuilder()
        task = FakeTask()

        manifest = builder.build(
            task=task,
            plan_item={},
            diff_summary="",
            files_changed=[],
        )

        assert "task-abc12345" in manifest.pr_url

    def test_tdd_evidence_reflects_approved_stubs(self) -> None:
        builder = PRManifestBuilder()
        task = FakeTask(test_stubs_approved=True)

        manifest = builder.build(
            task=task,
            plan_item={},
            diff_summary="",
            files_changed=[],
        )

        assert "approved" in manifest.tdd_evidence.lower()
        assert "2" in manifest.tdd_evidence  # 2 stubs

    def test_tdd_evidence_flags_unapproved_stubs(self) -> None:
        builder = PRManifestBuilder()
        task = FakeTask(test_stubs_approved=False)

        manifest = builder.build(
            task=task,
            plan_item={},
            diff_summary="",
            files_changed=[],
        )

        assert "not explicitly approved" in manifest.tdd_evidence.lower()

    def test_to_dict_is_json_serialisable(self) -> None:
        import json
        builder = PRManifestBuilder()
        task = FakeTask()
        manifest = builder.build(
            task=task, plan_item={}, diff_summary="", files_changed=[]
        )
        # Should not raise
        serialised = json.dumps(manifest.to_dict())
        assert "task-abc12345" in serialised

    def test_tests_pass_true_for_verified_passing_run(self) -> None:
        builder = PRManifestBuilder()
        task = FakeTask(tests_pass=True, test_results="3 passed in 0.42s")

        manifest = builder.build(
            task=task, plan_item={}, diff_summary="", files_changed=[]
        )

        assert manifest.tests_pass is True

    def test_failed_run_cannot_be_reported_as_passing(self) -> None:
        builder = PRManifestBuilder()
        task = FakeTask(tests_pass=False, test_results="1 failed, 2 passed in 0.5s")

        manifest = builder.build(
            task=task, plan_item={}, diff_summary="", files_changed=[]
        )

        assert manifest.tests_pass is False

    def test_skipped_run_not_reported_as_passing(self) -> None:
        builder = PRManifestBuilder()
        task = FakeTask(tests_pass=False, test_results="2 skipped, 0 passed in 0.1s")

        manifest = builder.build(
            task=task, plan_item={}, diff_summary="", files_changed=[]
        )

        assert manifest.tests_pass is False

    def test_no_test_results_not_reported_as_passing(self) -> None:
        # Claims a pass but carries no runner output -> untrustworthy, unknown.
        builder = PRManifestBuilder()
        task = FakeTask(tests_pass=True, test_results="")

        manifest = builder.build(
            task=task, plan_item={}, diff_summary="", files_changed=[]
        )

        assert manifest.tests_pass is False

    def test_source_findings_defaults_to_empty(self) -> None:
        builder = PRManifestBuilder()
        task = FakeTask()
        manifest = builder.build(
            task=task, plan_item={}, diff_summary="", files_changed=[]
        )
        assert manifest.source_findings == []


# ---------------------------------------------------------------------------
# PRManifest tests
# ---------------------------------------------------------------------------

class TestPRManifest:
    def test_format_for_human_contains_key_sections(self) -> None:
        manifest = PRManifest(
            task_id="task-abc",
            plan_id="plan-001",
            description="Fix the thing",
            acceptance_criteria=["Criterion A", "Criterion B"],
            files_changed=["src/main.py"],
            scope_boundary="src/main.py only",
            diff_summary="Removed global state",
            test_stubs_approved=True,
            tests_pass=True,
            doc_updated=True,
            docs_updated=["docs/README.md"],
            branch_name="sentinel/task-abc",
            pr_url="https://github.com/org/repo/pull/new/sentinel/task-abc",
        )

        rendered = manifest.format_for_human()

        assert "task-abc" in rendered
        assert "Criterion A" in rendered
        assert "Criterion B" in rendered
        assert "src/main.py" in rendered
        assert "Removed global state" in rendered
        assert "docs/README.md" in rendered

    def test_format_for_human_shows_tdd_approved(self) -> None:
        manifest = PRManifest(
            task_id="t1",
            plan_id="p1",
            description="desc",
            test_stubs_approved=True,
        )
        rendered = manifest.format_for_human()
        assert "APPROVED" in rendered

    def test_format_for_human_shows_tdd_not_approved(self) -> None:
        manifest = PRManifest(
            task_id="t1",
            plan_id="p1",
            description="desc",
            test_stubs_approved=False,
        )
        rendered = manifest.format_for_human()
        assert "NOT APPROVED" in rendered

    def test_to_dict_round_trips_correctly(self) -> None:
        manifest = PRManifest(
            task_id="task-xyz",
            plan_id="plan-99",
            description="Some fix",
            affected_files=["a.py"],
            diff_summary="added check",
        )
        d = manifest.to_dict()
        assert d["task_id"] == "task-xyz"
        assert d["plan_id"] == "plan-99"
        assert d["diff_summary"] == "added check"
        assert d["affected_files"] == ["a.py"]
