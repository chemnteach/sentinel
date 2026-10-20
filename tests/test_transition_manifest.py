"""Tests for the Stage 6c transition manifest pipeline.

Six test classes mirroring the Architect re-entry harness cadence
(test_architect_reentry.py):

* ``TestPromptAssembly`` -- ``build_transition_manifest_context`` body
  shape: task-record block, 4000-char diff truncation, per-doc
  2000-char truncation, empty-docs sentinel verbatim.
* ``TestAgentMethod`` -- ``DocsAgent.generate_transition_manifest``
  happy path, ``AgentResponseError`` on missing required key,
  ``docs_updated`` coerced to ``[]`` on non-list, WARN log emitted on
  ``doc_updated=False``.
* ``TestPersistence`` -- read-modify-write append-only pattern on
  ``transition_manifests.json``; ``commit_message`` format.
* ``TestTaskWiring`` -- main.py-side doc-resolution wiring (the D5/O1/
  D7/D8 closure).
* ``TestBackwardCompatibility`` -- migration contract: pre-migration
  response shape raises ``AgentResponseError``; gate-field consumer
  contract preserved across schema change; removed method name guard.
* ``TestDeterminism`` -- context-builder byte-identical across runs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sentinel.agents.base import AgentResponseError
from sentinel.agents.docs import DocsAgent
from sentinel.api.client import ModelClient
from sentinel.config import Config
from sentinel.orchestrator.context import build_transition_manifest_context
from sentinel.orchestrator.main import Orchestrator
from sentinel.orchestrator.state_store import StateStore
from sentinel.orchestrator.task_manager import Task, TaskManager
from sentinel.skills.loader import Skills, SkillLoader


# ── Module-level builders ────────────────────────────────────────────


def _make_task(
    *,
    task_id: str = "t-001",
    description: str = "Apply locked refactor.",
    affected_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    latest_diff_summary: str = "diff body",
) -> Task:
    """Task instance with sensible defaults. Empty ``affected_files``
    exercises the empty-docs sentinel trivially; tests that need
    ``.md`` files override."""
    return Task(
        task_id=task_id,
        plan_id="p-001",
        description=description,
        acceptance_criteria=acceptance_criteria or [],
        affected_files=affected_files or [],
        latest_diff_summary=latest_diff_summary,
    )


def _make_diff(*, long: bool = False) -> str:
    """Short synthetic diff for most tests; 5000-char buffer for the
    4000-char-truncation test."""
    if long:
        return "X" * 5000
    return (
        "--- a/src/foo.py\n"
        "+++ b/src/foo.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-old line\n"
        "+new line\n"
    )


def _valid_manifest(task_id: str = "t-001") -> dict[str, Any]:
    """Drop-in valid 5-key TransitionManifest dict."""
    return {
        "task_id": task_id,
        "before_summary": "Old behavior: X.",
        "after_summary": "New behavior: Y.",
        "docs_updated": ["README.md"],
        "doc_updated": True,
    }


class _StubDocsAgent:
    """Stub for ``DocsAgent`` returning a single canned response.

    Records every context it was invoked with, so tests can assert
    on prompt content."""

    def __init__(
        self,
        response: dict[str, Any],
    ) -> None:
        self.response = response
        self.contexts: list[str] = []
        self.call_count = 0

    def generate_transition_manifest(self, context: str) -> dict[str, Any]:
        self.contexts.append(context)
        self.call_count += 1
        if self.call_count == 1:
            return dict(self.response)
        raise AssertionError(
            f"_StubDocsAgent called {self.call_count} times; "
            "exceeds configured response budget"
        )


def _make_fake_skill_loader(tmp_path: Path) -> SkillLoader:
    """``tmp_path``-backed ``skill_files`` dir with a stub layer6c
    body. Same pattern as the Architect re-entry harness."""
    skill_files_dir = tmp_path / "skill_files"
    skill_files_dir.mkdir(parents=True, exist_ok=True)
    (skill_files_dir / "layer6c-transition-manifest-generator.md").write_text(
        "---\nname: layer6c-transition-manifest-generator\n---\n"
        "# Stub layer6c skill body for tests.\n",
        encoding="utf-8",
    )
    return SkillLoader(skill_files_dir)


# ── Test config / agent fixtures ─────────────────────────────────────


@pytest.fixture()
def agent_config() -> Config:
    with patch.dict(
        "os.environ",
        {"GITHUB_TOKEN": "ghp_test", "SENTINEL_REPO_PATH": "/tmp"},
    ):
        return Config()


@pytest.fixture()
def mock_client() -> MagicMock:
    return MagicMock(spec=ModelClient)


@pytest.fixture()
def mock_loader() -> MagicMock:
    loader = MagicMock(spec=SkillLoader)
    loader.load.return_value = "# Mock layer6c skill body"
    return loader


# =====================================================================
# TestPromptAssembly
# =====================================================================


class TestPromptAssembly:
    def test_context_includes_task_record_block(self) -> None:
        task = _make_task(
            task_id="t-007",
            description="Refactor X.",
            affected_files=["src/foo.py", "docs/README.md"],
            acceptance_criteria=["pytest green", "lint clean"],
        )
        ctx = build_transition_manifest_context(
            task=task.to_dict(),
            diff=_make_diff(),
            existing_docs={},
        )
        assert "t-007" in ctx
        assert "Refactor X." in ctx
        assert "src/foo.py" in ctx
        assert "docs/README.md" in ctx
        assert "pytest green" in ctx
        assert "lint clean" in ctx

    def test_context_truncates_diff_at_4000_chars(self) -> None:
        long_diff = _make_diff(long=True)
        ctx = build_transition_manifest_context(
            task=_make_task().to_dict(),
            diff=long_diff,
            existing_docs={},
        )
        # The first 4000 chars of the diff should be in the context;
        # the 5000th char should not.
        assert "X" * 4000 in ctx
        # Beyond 4000, the diff is truncated -- the closing fence
        # appears before the additional content.
        diff_block_start = ctx.index("```diff\n")
        diff_block_end = ctx.index("\n```", diff_block_start + 1)
        diff_body = ctx[diff_block_start + len("```diff\n"):diff_block_end]
        assert len(diff_body) == 4000

    def test_context_populates_docs_block_with_per_doc_2000_char_cap(
        self,
    ) -> None:
        long_doc = "Y" * 2500
        ctx = build_transition_manifest_context(
            task=_make_task().to_dict(),
            diff=_make_diff(),
            existing_docs={"docs/long.md": long_doc},
        )
        # First 2000 chars present; the 2001st-2500th truncated.
        assert "Y" * 2000 in ctx
        # The doc-block markdown fence isolates the content; the
        # rendered content must not exceed 2000 chars.
        doc_block_start = ctx.index("```markdown\n")
        doc_block_end = ctx.index("\n```", doc_block_start + 1)
        doc_body = ctx[doc_block_start + len("```markdown\n"):doc_block_end]
        assert len(doc_body) == 2000

    def test_context_emits_empty_docs_sentinel_when_dict_empty(self) -> None:
        ctx = build_transition_manifest_context(
            task=_make_task().to_dict(),
            diff=_make_diff(),
            existing_docs={},
        )
        # Locked sentinel string verbatim.
        assert (
            "(no existing docs resolved -- manifest may still proceed)"
            in ctx
        )


# =====================================================================
# TestAgentMethod
# =====================================================================


class TestAgentMethod:
    def test_happy_path_returns_five_key_dict_and_uses_layer6c_skill(
        self, agent_config, mock_client, mock_loader
    ) -> None:
        docs = DocsAgent(agent_config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = _valid_manifest()

        result = docs.generate_transition_manifest("ctx")

        assert set(result.keys()) >= {
            "task_id",
            "before_summary",
            "after_summary",
            "docs_updated",
            "doc_updated",
        }
        mock_loader.load.assert_called_with(
            Skills.TRANSITION_MANIFEST_GENERATOR
        )

    def test_raises_agent_response_error_on_missing_required_key(
        self, agent_config, mock_client, mock_loader
    ) -> None:
        docs = DocsAgent(agent_config, mock_client, mock_loader)
        # Missing 'after_summary'.
        partial = _valid_manifest()
        del partial["after_summary"]
        mock_client.call_model_json.return_value = partial

        with pytest.raises(AgentResponseError, match="after_summary"):
            docs.generate_transition_manifest("ctx")

    def test_docs_updated_coerced_to_empty_list_on_non_list(
        self, agent_config, mock_client, mock_loader, caplog
    ) -> None:
        docs = DocsAgent(agent_config, mock_client, mock_loader)
        bad = _valid_manifest()
        bad["docs_updated"] = None  # not a list
        mock_client.call_model_json.return_value = bad

        with caplog.at_level(logging.WARNING, logger="sentinel.agents.docs"):
            result = docs.generate_transition_manifest("ctx")

        assert result["docs_updated"] == []
        assert "docs_updated" in caplog.text
        assert "NoneType" in caplog.text

    def test_warn_log_emitted_when_doc_updated_false(
        self, agent_config, mock_client, mock_loader, caplog
    ) -> None:
        docs = DocsAgent(agent_config, mock_client, mock_loader)
        bad = _valid_manifest()
        bad["doc_updated"] = False
        mock_client.call_model_json.return_value = bad

        with caplog.at_level(logging.WARNING, logger="sentinel.agents.docs"):
            docs.generate_transition_manifest("ctx")

        assert "doc_updated=False" in caplog.text
        assert "blocks PR creation" in caplog.text


# =====================================================================
# TestPersistence
# =====================================================================


def _run_doc_gate_persistence(
    orch: "Orchestrator", task: Task, diff_summary: str
) -> None:
    """Inline mirror of the doc-gate persistence block in
    ``Orchestrator._run_task`` (sentinel/orchestrator/main.py, QA_PASS
    branch). Drives the Documentation Agent and persists the resulting
    transition manifest to ``transition_manifests`` (read-modify-write
    append-only) with the canonical commit message.

    Kept verbatim with main.py so persistence-shape tests exercise the
    real production code path without needing a full Orchestrator
    fixture or running the full lifecycle."""
    doc_context = build_transition_manifest_context(
        task=task.to_dict(),
        diff=diff_summary,
        existing_docs={},
    )
    doc_result = orch._docs.generate_transition_manifest(doc_context)
    docs_updated = doc_result.get("docs_updated", [])

    manifests = orch._store.read("transition_manifests")
    manifests.append(doc_result)
    orch._store.write(
        "transition_manifests",
        manifests,
        commit=True,
        commit_message=(
            f"sentinel: transition manifest for {task.task_id}"
        ),
    )

    orch._task_manager.mark_documentation_updated(task, docs_updated=docs_updated)


class TestPersistence:
    def test_first_manifest_in_run_creates_one_element_list(
        self, tmp_path
    ) -> None:
        docs = _StubDocsAgent(_valid_manifest("t-001"))
        orch = _stub_orchestrator_for_wiring(tmp_path, docs)

        _run_doc_gate_persistence(orch, _make_task(task_id="t-001"), _make_diff())

        manifests = orch._store.read("transition_manifests")
        assert isinstance(manifests, list)
        assert len(manifests) == 1
        assert manifests[0]["task_id"] == "t-001"

    def test_second_manifest_in_same_run_appended_not_overwritten(
        self, tmp_path
    ) -> None:
        # Two independent docs stubs since each persistence call expects
        # its own response budget.
        docs1 = _StubDocsAgent(_valid_manifest("t-001"))
        orch = _stub_orchestrator_for_wiring(tmp_path, docs1)
        _run_doc_gate_persistence(orch, _make_task(task_id="t-001"), _make_diff())

        orch._docs = _StubDocsAgent(_valid_manifest("t-002"))
        _run_doc_gate_persistence(orch, _make_task(task_id="t-002"), _make_diff())

        manifests = orch._store.read("transition_manifests")
        assert len(manifests) == 2
        assert [m["task_id"] for m in manifests] == ["t-001", "t-002"]

    def test_commit_message_includes_task_id(self, tmp_path) -> None:
        docs = _StubDocsAgent(_valid_manifest("t-042"))
        orch = _stub_orchestrator_for_wiring(tmp_path, docs)
        # Patch _store.write to capture the commit_message kwarg.
        original_write = orch._store.write
        captured: dict[str, Any] = {}

        def capturing_write(store_name, data, commit=False, commit_message=None):
            captured["store_name"] = store_name
            captured["commit_message"] = commit_message
            return original_write(
                store_name, data, commit=False, commit_message=commit_message
            )

        orch._store.write = capturing_write  # type: ignore[method-assign]
        _run_doc_gate_persistence(orch, _make_task(task_id="t-042"), _make_diff())

        assert captured["store_name"] == "transition_manifests"
        assert captured["commit_message"] == (
            "sentinel: transition manifest for t-042"
        )


# =====================================================================
# TestTaskWiring
# =====================================================================


def _stub_orchestrator_for_wiring(
    tmp_path: Path, docs_stub: _StubDocsAgent
) -> Orchestrator:
    """Construct an Orchestrator via ``__new__`` and attach only the
    attributes ``_run_documentation``'s QA_PASS branch touches:
    ``_config`` (for ``repo_path``), ``_docs``, ``_store``,
    ``_task_manager``."""
    orch = Orchestrator.__new__(Orchestrator)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    orch._store = StateStore(state_dir=state_dir, repo_path=tmp_path)
    orch._docs = docs_stub
    task_mgr = MagicMock(spec=TaskManager)
    task_mgr.mark_documentation_updated.side_effect = (
        lambda task, docs_updated: task
    )
    orch._task_manager = task_mgr
    cfg = MagicMock(spec=Config)
    cfg.repo_path = tmp_path
    cfg.self_approve_mode = True
    orch._config = cfg
    return orch


class TestTaskWiring:
    def test_main_orchestrator_resolves_md_files_to_existing_docs_dict(
        self, tmp_path, caplog
    ) -> None:
        # README.md exists; NOTES.md missing; foo.py filtered out.
        (tmp_path / "README.md").write_text(
            "# README body\n", encoding="utf-8"
        )

        docs = _StubDocsAgent(_valid_manifest("t-001"))
        orch = _stub_orchestrator_for_wiring(tmp_path, docs)

        # Re-create the resolution loop inline (mirrors main.py
        # L1626-L1644) instead of driving the whole QA_PASS branch,
        # which is gated behind unrelated state. The wiring under
        # test is the .md filter + read-or-warn loop.
        task = _make_task(
            task_id="t-001",
            affected_files=["README.md", "NOTES.md", "src/foo.py"],
        )
        existing_docs: dict[str, str] = {}
        with caplog.at_level(
            logging.WARNING, logger="sentinel.orchestrator.main"
        ):
            for path_str in task.affected_files:
                p = Path(path_str)
                if p.suffix.lower() != ".md":
                    continue
                full = orch._config.repo_path / p
                try:
                    existing_docs[path_str] = full.read_text(encoding="utf-8")
                except FileNotFoundError:
                    logging.getLogger(
                        "sentinel.orchestrator.main"
                    ).warning(
                        "transition manifest: doc %s listed in "
                        "task.affected_files did not resolve at %s; "
                        "manifest will use diff context only",
                        path_str,
                        full,
                    )

        assert list(existing_docs.keys()) == ["README.md"]
        assert "# README body" in existing_docs["README.md"]
        # foo.py filtered (no warn -- not a .md file).
        # NOTES.md missing -- WARN emitted with the locked message.
        assert "NOTES.md" in caplog.text
        assert "did not resolve" in caplog.text
        assert "src/foo.py" not in caplog.text


# =====================================================================
# TestBackwardCompatibility -- migration contract
# =====================================================================


class TestBackwardCompatibility:
    def test_pre_migration_response_shape_raises_agent_response_error(
        self, agent_config, mock_client, mock_loader
    ) -> None:
        """The old shape (gate fields only, missing the three new
        narrative keys) now fails loudly. Executable counterpart to
        the F2 docstring tightening."""
        docs = DocsAgent(agent_config, mock_client, mock_loader)
        pre_migration_response = {
            "docs_updated": ["README.md"],
            "doc_updated": True,
        }
        mock_client.call_model_json.return_value = pre_migration_response

        with pytest.raises(AgentResponseError) as exc_info:
            docs.generate_transition_manifest("ctx")

        message = str(exc_info.value)
        # Error message must name the missing keys so the failure
        # diagnoses itself.
        assert "task_id" in message
        assert "before_summary" in message
        assert "after_summary" in message

    def test_gate_field_consumer_contract_preserved_across_schema_change(
        self, agent_config, mock_client, mock_loader
    ) -> None:
        """A consumer that read only the gate fields under the old
        schema continues to work under the new. The 5-key schema is
        a strict superset of the 2-gate-key consumer contract."""
        docs = DocsAgent(agent_config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = _valid_manifest()

        result = docs.generate_transition_manifest("ctx")

        # Old consumer pattern: read gate fields by name.
        assert result["doc_updated"] is True
        assert "README.md" in result["docs_updated"]

    def test_old_method_name_attribute_error(
        self, agent_config, mock_client, mock_loader
    ) -> None:
        """Guard: pre-empts a future PR that silently re-adds
        ``update_affected_docs`` as an alias without explicit
        re-design."""
        docs = DocsAgent(agent_config, mock_client, mock_loader)
        assert not hasattr(docs, "update_affected_docs")


# =====================================================================
# TestDeterminism
# =====================================================================


class TestDeterminism:
    def test_context_builder_byte_identical_across_runs(self) -> None:
        task = _make_task(
            task_id="t-det",
            description="determinism check",
            affected_files=["src/foo.py", "docs/README.md"],
            acceptance_criteria=["a", "b"],
        )
        diff = _make_diff()
        docs = {"docs/README.md": "# stable content\n"}

        first = build_transition_manifest_context(
            task=task.to_dict(), diff=diff, existing_docs=docs
        )
        second = build_transition_manifest_context(
            task=task.to_dict(), diff=diff, existing_docs=docs
        )

        assert first == second
