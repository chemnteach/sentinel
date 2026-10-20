"""
tests/test_phase3.py

Test suite for Sentinel Phase 3 — Feature Enhancement pipeline.

Covers:
  - FER validation and lifecycle (FERManager)
  - FER status transitions and guard rails
  - Feasibility orchestration (FeasibilityOrchestrator)
  - Spec generation and task creation (SpecWriter)
  - Backwards compatibility gate (CompatChecker)
  - Skills loader constants for Layer 7
  - StateStore Phase 3 stores
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from sentinel.orchestrator.state_store import StateStore
from sentinel.phase3.fer_manager import (
    FER,
    FERManager,
    FERTransitionError,
    FERValidationError,
    _next_fer_id,
)
from sentinel.phase3.feasibility import (
    FeasibilityOrchestrator,
    FeasibilityError,
    HumanFeasibilityGateRequired,
)
from sentinel.phase3.spec_writer import (
    HumanSpecGateRequired,
    SpecError,
    SpecWriter,
    _distribute_stubs,
)
from sentinel.phase3.compat_checker import (
    CompatChecker,
    CompatCheckerError,
    CompatibilityViolation,
)
from sentinel.skills.loader import Skills


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_state(tmp_path: Path) -> StateStore:
    """A fresh StateStore backed by a temporary directory."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return StateStore(state_dir=state_dir, repo_path=tmp_path)


@pytest.fixture
def fer_manager(tmp_state: StateStore) -> FERManager:
    return FERManager(store=tmp_state)


def _valid_fer_data(**overrides: Any) -> dict[str, Any]:
    return {
        "title": "Add CSV export to reporting module",
        "business_justification": "Finance team needs monthly exports for auditing.",
        "priority": "MUST_HAVE",
        "affected_systems": ["reporting", "api"],
        **overrides,
    }


def _mock_feasibility_report(fer_id: str = "FER-001") -> dict[str, Any]:
    return {
        "fer_id": fer_id,
        "capability_summary": "CSV export for reports",
        "clarifications_needed": [],
        "assumptions": [],
        "feasibility_verdict": "FEASIBLE",
        "feasibility_rationale": "Standard pattern, low risk.",
        "effort_decomposition": {
            "new_code": {"size": "M", "modules_affected": ["reporting/exporter.py"]},
            "tests": {"size": "S", "modules_affected": []},
            "documentation": {"size": "XS", "modules_affected": []},
            "modifications": {"size": "XS", "modules_affected": []},
            "integration": {"size": "XS", "modules_affected": []},
            "migration": {"size": "XS", "modules_affected": []},
            "cross_repo": {"size": "XS", "modules_affected": []},
        },
        "total_effort_estimate": "M",
        "risk_level": "LOW",
        "risk_factors": [],
        "architectural_alignment": "ALIGNED",
        "architectural_notes": "Fits existing export pattern.",
        "dependencies": [],
        "options": [
            {
                "approach_name": "Streaming exporter",
                "description": "Stream rows directly to response.",
                "effort": "M",
                "risks": [],
                "trade_offs": {"gains": "Low memory", "sacrifices": "Complexity"},
                "alignment": "ALIGNED",
            },
            {
                "approach_name": "Minimal adapter",
                "description": "Wrap existing report query with csv.writer.",
                "effort": "S",
                "risks": [],
                "trade_offs": {"gains": "Simple", "sacrifices": "No streaming"},
                "alignment": "ALIGNED",
            },
        ],
        "recommended_approach": "Minimal adapter",
        "not_in_scope": ["PDF export"],
    }


def _mock_spec(fer_id: str = "FER-001") -> dict[str, Any]:
    return {
        "fer_id": fer_id,
        "spec_version": "1.0",
        "feature_summary": "CSV export endpoint for the reporting module.",
        "acceptance_criteria": {
            "happy_path": [
                {
                    "id": "AC-001",
                    "given": "an authenticated ADMIN user",
                    "when": "they call GET /api/reports/export?format=csv",
                    "then": "the response is 200 with Content-Type text/csv",
                    "test_stub": "test_export_returns_csv",
                }
            ],
            "error_cases": [
                {
                    "id": "AC-002",
                    "given": "an unauthenticated request",
                    "when": "they call GET /api/reports/export",
                    "then": "the response is 401",
                    "test_stub": "test_export_requires_auth",
                }
            ],
            "edge_cases": [],
            "security": [],
            "backwards_compatibility": [],
        },
        "api_contracts": {"http_endpoints": [], "function_signatures": [], "cli_commands": []},
        "data_model": {"new_fields": [], "modified_fields": [], "migration_steps": []},
        "integration_points": [],
        "scope_boundary": {
            "in_scope": ["reporting/exporter.py (CREATE NEW)", "api/routes/reports.py (MODIFY)"],
            "out_of_scope": ["billing/", "auth/"],
            "new_files_permitted_in": ["reporting/"],
        },
        "test_strategy": {"unit": [], "integration": [], "contract": [], "regression": []},
        "test_stubs": [
            {
                "class": "TestCSVExport",
                "method": "test_export_returns_csv",
                "tests": "Happy path — valid authenticated request returns CSV",
                "requires": "mock AuthMiddleware",
                "asserts": "status 200, content-type text/csv",
            },
            {
                "class": "TestCSVExport",
                "method": "test_export_requires_auth",
                "tests": "Error case — unauthenticated request returns 401",
                "requires": "no auth header",
                "asserts": "status 401",
            },
        ],
        "documentation_requirements": ["Update api/README.md with export endpoint"],
        "non_goals": ["PDF export", "Excel export"],
        "implementation_order": [
            "Create reporting/exporter.py with CSVExporter class",
            "Add GET /api/reports/export route in api/routes/reports.py",
        ],
    }


# ═══════════════════════════════════════════════════════════════════════
# FERManager — intake and validation
# ═══════════════════════════════════════════════════════════════════════

class TestFERSubmission:
    def test_submit_valid_fer_returns_pending(self, fer_manager: FERManager) -> None:
        fer = fer_manager.submit(_valid_fer_data())
        assert fer.status == "PENDING"
        assert fer.fer_id == "FER-001"
        assert fer.title == "Add CSV export to reporting module"
        assert fer.priority == "MUST_HAVE"

    def test_submit_assigns_sequential_ids(self, fer_manager: FERManager) -> None:
        fer1 = fer_manager.submit(_valid_fer_data(title="First"))
        fer2 = fer_manager.submit(_valid_fer_data(title="Second"))
        assert fer1.fer_id == "FER-001"
        assert fer2.fer_id == "FER-002"

    def test_submit_persists_to_state(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        fer_manager.submit(_valid_fer_data())
        fers = tmp_state.read("fers")
        assert len(fers) == 1
        assert fers[0]["fer_id"] == "FER-001"

    def test_submit_normalises_priority_case(self, fer_manager: FERManager) -> None:
        fer = fer_manager.submit(_valid_fer_data(priority="must_have"))
        assert fer.priority == "MUST_HAVE"

    def test_submit_records_history_event(self, fer_manager: FERManager) -> None:
        fer = fer_manager.submit(_valid_fer_data())
        assert any(e["event"] == "submitted" for e in fer.history)

    def test_submit_rejects_missing_title(self, fer_manager: FERManager) -> None:
        with pytest.raises(FERValidationError, match="title"):
            fer_manager.submit(_valid_fer_data(title=""))

    def test_submit_rejects_title_too_long(self, fer_manager: FERManager) -> None:
        with pytest.raises(FERValidationError, match="200"):
            fer_manager.submit(_valid_fer_data(title="x" * 201))

    def test_submit_rejects_missing_justification(self, fer_manager: FERManager) -> None:
        with pytest.raises(FERValidationError, match="business_justification"):
            fer_manager.submit(_valid_fer_data(business_justification=""))

    def test_submit_rejects_invalid_priority(self, fer_manager: FERManager) -> None:
        with pytest.raises(FERValidationError, match="priority"):
            fer_manager.submit(_valid_fer_data(priority="URGENT"))

    def test_submit_rejects_null_title(self, fer_manager: FERManager) -> None:
        # A JSON null title must produce a clean FERValidationError, not an
        # AttributeError from calling .strip() on None.
        with pytest.raises(FERValidationError, match="title"):
            fer_manager.submit(_valid_fer_data(title=None))

    def test_submit_rejects_non_string_priority(self, fer_manager: FERManager) -> None:
        # A non-string priority must not crash .upper(); it is a validation error.
        with pytest.raises(FERValidationError, match="priority"):
            fer_manager.submit(_valid_fer_data(priority=123))

    def test_submit_rejects_null_justification(self, fer_manager: FERManager) -> None:
        with pytest.raises(FERValidationError, match="business_justification"):
            fer_manager.submit(_valid_fer_data(business_justification=None))

    def test_submit_defaults_affected_systems_to_empty(self, fer_manager: FERManager) -> None:
        data = _valid_fer_data()
        del data["affected_systems"]
        fer = fer_manager.submit(data)
        assert fer.affected_systems == []


# ═══════════════════════════════════════════════════════════════════════
# FERManager — triage
# ═══════════════════════════════════════════════════════════════════════

class TestFERTriage:
    def test_approve_advances_to_approved(self, fer_manager: FERManager) -> None:
        fer = fer_manager.submit(_valid_fer_data())
        fer = fer_manager.triage(fer.fer_id, "APPROVED")
        assert fer.status == "APPROVED"

    def test_defer_advances_to_deferred(self, fer_manager: FERManager) -> None:
        fer = fer_manager.submit(_valid_fer_data())
        fer = fer_manager.triage(fer.fer_id, "DEFERRED")
        assert fer.status == "DEFERRED"

    def test_reject_advances_to_rejected(self, fer_manager: FERManager) -> None:
        fer = fer_manager.submit(_valid_fer_data())
        fer = fer_manager.triage(fer.fer_id, "REJECTED")
        assert fer.status == "REJECTED"

    def test_triage_records_history(self, fer_manager: FERManager) -> None:
        fer = fer_manager.submit(_valid_fer_data())
        fer = fer_manager.triage(fer.fer_id, "APPROVED", reason="Looks good")
        events = [e["event"] for e in fer.history]
        assert "triaged" in events

    def test_cannot_triage_rejected_fer(self, fer_manager: FERManager) -> None:
        fer = fer_manager.submit(_valid_fer_data())
        fer_manager.triage(fer.fer_id, "REJECTED")
        with pytest.raises(FERTransitionError):
            fer_manager.triage(fer.fer_id, "APPROVED")

    def test_invalid_triage_decision_raises(self, fer_manager: FERManager) -> None:
        fer = fer_manager.submit(_valid_fer_data())
        with pytest.raises(FERValidationError, match="MAYBE"):
            fer_manager.triage(fer.fer_id, "MAYBE")

    def test_triage_unknown_fer_raises(self, fer_manager: FERManager) -> None:
        with pytest.raises(FERValidationError, match="FER-999"):
            fer_manager.triage("FER-999", "APPROVED")


# ═══════════════════════════════════════════════════════════════════════
# FERManager — full lifecycle happy path
# ═══════════════════════════════════════════════════════════════════════

class TestFERLifecycle:
    def test_full_pre_implementation_pipeline(self, fer_manager: FERManager) -> None:
        fer = fer_manager.submit(_valid_fer_data())
        fer = fer_manager.triage(fer.fer_id, "APPROVED")
        fer = fer_manager.start_feasibility(fer.fer_id)
        assert fer.status == "IN_FEASIBILITY"

        fer = fer_manager.record_feasibility_approved(
            fer.fer_id,
            selected_approach="Minimal adapter",
            feasibility_verdict="FEASIBLE",
        )
        assert fer.status == "FEASIBILITY_APPROVED"
        assert fer.selected_approach == "Minimal adapter"

        fer = fer_manager.start_spec(fer.fer_id)
        assert fer.status == "IN_SPEC"

        fer = fer_manager.record_spec_approved(
            fer.fer_id,
            spec_version="1.0",
            task_ids=["fer-fer-001-task-001", "fer-fer-001-task-002"],
        )
        assert fer.status == "SPEC_APPROVED"
        assert len(fer.task_ids) == 2

        fer = fer_manager.mark_in_progress(fer.fer_id)
        assert fer.status == "IN_PROGRESS"

        fer = fer_manager.mark_complete(fer.fer_id)
        assert fer.status == "COMPLETE"

    def test_list_by_status(self, fer_manager: FERManager) -> None:
        fer_manager.submit(_valid_fer_data(title="First"))
        fer_manager.submit(_valid_fer_data(title="Second"))
        fer_manager.triage("FER-001", "APPROVED")

        approved = fer_manager.list_by_status("APPROVED")
        pending = fer_manager.list_by_status("PENDING")
        assert len(approved) == 1
        assert len(pending) == 1


# ═══════════════════════════════════════════════════════════════════════
# FERManager — context builders
# ═══════════════════════════════════════════════════════════════════════

class TestFERContextBuilders:
    def test_feasibility_context_includes_fer_fields(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        fer = fer_manager.submit(_valid_fer_data())
        context = fer_manager.build_feasibility_context(fer)
        assert "FER-001" in context
        assert "Add CSV export" in context
        assert "MUST_HAVE" in context

    def test_spec_context_includes_feasibility_data(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        fer = fer_manager.submit(_valid_fer_data())
        fer_manager.triage(fer.fer_id, "APPROVED")
        fer_manager.start_feasibility(fer.fer_id)
        fer = fer_manager.record_feasibility_approved(
            fer.fer_id, "Minimal adapter", "FEASIBLE"
        )
        feasibility = _mock_feasibility_report()
        context = fer_manager.build_spec_context(fer, feasibility)
        assert "Minimal adapter" in context
        assert "FEASIBLE" in context


# ═══════════════════════════════════════════════════════════════════════
# FER ID generation
# ═══════════════════════════════════════════════════════════════════════

class TestFERIdGeneration:
    def test_empty_list_returns_001(self) -> None:
        assert _next_fer_id([]) == "FER-001"

    def test_increments_from_existing(self) -> None:
        existing = [{"fer_id": "FER-005"}, {"fer_id": "FER-003"}]
        assert _next_fer_id(existing) == "FER-006"

    def test_pads_to_three_digits(self) -> None:
        existing = [{"fer_id": "FER-009"}]
        assert _next_fer_id(existing) == "FER-010"


# ═══════════════════════════════════════════════════════════════════════
# FeasibilityOrchestrator
# ═══════════════════════════════════════════════════════════════════════

class TestFeasibilityOrchestrator:
    def _make_orchestrator(
        self,
        fer_mgr: FERManager,
        store: StateStore,
        report: dict[str, Any] | None = None,
    ) -> tuple[FeasibilityOrchestrator, MagicMock]:
        architect = MagicMock()
        architect.assess_feasibility.return_value = report or _mock_feasibility_report()
        orch = FeasibilityOrchestrator(fer_mgr, architect, store)
        return orch, architect

    def _approved_fer(self, fer_mgr: FERManager) -> FER:
        fer = fer_mgr.submit(_valid_fer_data())
        return fer_mgr.triage(fer.fer_id, "APPROVED")

    def test_run_raises_human_gate_on_success(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        fer = self._approved_fer(fer_manager)
        orch, _ = self._make_orchestrator(fer_manager, tmp_state)
        with pytest.raises(HumanFeasibilityGateRequired) as exc_info:
            orch.run(fer.fer_id)
        gate = exc_info.value
        assert gate.fer_id == fer.fer_id
        assert "feasibility_verdict" in gate.payload

    def test_run_persists_report_to_state(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        fer = self._approved_fer(fer_manager)
        orch, _ = self._make_orchestrator(fer_manager, tmp_state)
        with pytest.raises(HumanFeasibilityGateRequired):
            orch.run(fer.fer_id)
        reports = tmp_state.read("feasibility")
        assert any(r["fer_id"] == fer.fer_id for r in reports)

    def test_run_advances_fer_to_in_feasibility(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        fer = self._approved_fer(fer_manager)
        orch, _ = self._make_orchestrator(fer_manager, tmp_state)
        with pytest.raises(HumanFeasibilityGateRequired):
            orch.run(fer.fer_id)
        updated = fer_manager.get(fer.fer_id)
        assert updated.status == "IN_FEASIBILITY"

    def test_run_skips_model_call_if_report_exists(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        """Resumability: if report already in state, model is not called again."""
        fer = self._approved_fer(fer_manager)
        fer_manager.start_feasibility(fer.fer_id)
        # Pre-populate state with a report
        tmp_state.upsert_in_list(
            "feasibility",
            {"fer_id": fer.fer_id, **_mock_feasibility_report(fer.fer_id)},
            key_field="fer_id",
        )
        orch, architect = self._make_orchestrator(fer_manager, tmp_state)
        with pytest.raises(HumanFeasibilityGateRequired):
            orch.run(fer.fer_id)
        architect.assess_feasibility.assert_not_called()

    def test_approve_records_feasibility_approved(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        fer = self._approved_fer(fer_manager)
        orch, _ = self._make_orchestrator(fer_manager, tmp_state)
        with pytest.raises(HumanFeasibilityGateRequired):
            orch.run(fer.fer_id)
        fer = orch.approve(fer.fer_id, selected_approach="Minimal adapter")
        assert fer.status == "FEASIBILITY_APPROVED"
        assert fer.selected_approach == "Minimal adapter"

    def test_approve_rejects_unknown_approach(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        fer = self._approved_fer(fer_manager)
        orch, _ = self._make_orchestrator(fer_manager, tmp_state)
        with pytest.raises(HumanFeasibilityGateRequired):
            orch.run(fer.fer_id)
        with pytest.raises(FeasibilityError, match="not found"):
            orch.approve(fer.fer_id, selected_approach="Nonexistent approach")

    def test_validates_missing_required_keys(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        bad_report = {"fer_id": "FER-001", "feasibility_verdict": "FEASIBLE"}
        fer = self._approved_fer(fer_manager)
        orch, architect = self._make_orchestrator(fer_manager, tmp_state, report=bad_report)
        with pytest.raises(FeasibilityError, match="missing required keys"):
            orch.run(fer.fer_id)

    def test_validates_bad_verdict_value(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        bad_report = {**_mock_feasibility_report(), "feasibility_verdict": "MAYBE"}
        fer = self._approved_fer(fer_manager)
        orch, architect = self._make_orchestrator(fer_manager, tmp_state, report=bad_report)
        with pytest.raises(FeasibilityError, match="invalid feasibility_verdict"):
            orch.run(fer.fer_id)

    def test_approve_fails_without_existing_report(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        fer = self._approved_fer(fer_manager)
        orch, _ = self._make_orchestrator(fer_manager, tmp_state)
        with pytest.raises(FeasibilityError, match="No feasibility report"):
            orch.approve(fer.fer_id, selected_approach="Minimal adapter")


# ═══════════════════════════════════════════════════════════════════════
# SpecWriter
# ═══════════════════════════════════════════════════════════════════════

class TestSpecWriter:
    def _setup(
        self,
        fer_mgr: FERManager,
        store: StateStore,
        spec: dict[str, Any] | None = None,
    ) -> tuple[SpecWriter, MagicMock, MagicMock]:
        architect = MagicMock()
        architect.write_enhancement_spec.return_value = spec or _mock_spec()
        task_mgr = MagicMock()

        from sentinel.orchestrator.task_manager import Task, TaskState
        def _fake_create_task(**kwargs):
            return Task(
                task_id=kwargs.get("task_id", f"task-{uuid.uuid4().hex[:8]}"),
                plan_id=kwargs.get("source_fer_id", "FER-001"),
                description=kwargs.get("description", ""),
                acceptance_criteria=kwargs.get("acceptance_criteria", []),
                affected_files=kwargs.get("affected_files", []),
                scope_boundary=kwargs.get("scope_boundary", ""),
                is_phase3=kwargs.get("is_phase3", True),
                source_fer_id=kwargs.get("source_fer_id", ""),
                test_stubs=kwargs.get("test_stubs"),
            )
        task_mgr.create_task.side_effect = _fake_create_task

        writer = SpecWriter(fer_mgr, architect, task_mgr, store)
        return writer, architect, task_mgr

    def _feasibility_approved_fer(
        self, fer_mgr: FERManager, store: StateStore
    ) -> FER:
        fer = fer_mgr.submit(_valid_fer_data())
        fer_mgr.triage(fer.fer_id, "APPROVED")
        fer_mgr.start_feasibility(fer.fer_id)
        store.upsert_in_list(
            "feasibility",
            {"fer_id": fer.fer_id, **_mock_feasibility_report(fer.fer_id)},
            key_field="fer_id",
        )
        return fer_mgr.record_feasibility_approved(
            fer.fer_id, "Minimal adapter", "FEASIBLE"
        )

    def test_run_raises_human_gate_on_success(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        fer = self._feasibility_approved_fer(fer_manager, tmp_state)
        writer, _, _ = self._setup(fer_manager, tmp_state)
        with pytest.raises(HumanSpecGateRequired) as exc_info:
            writer.run(fer.fer_id)
        assert exc_info.value.fer_id == fer.fer_id

    def test_run_persists_spec_to_state(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        fer = self._feasibility_approved_fer(fer_manager, tmp_state)
        writer, _, _ = self._setup(fer_manager, tmp_state)
        with pytest.raises(HumanSpecGateRequired):
            writer.run(fer.fer_id)
        specs = tmp_state.read("specs")
        assert any(s["fer_id"] == fer.fer_id for s in specs)

    def test_run_skips_model_if_spec_exists(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        fer = self._feasibility_approved_fer(fer_manager, tmp_state)
        fer_manager.start_spec(fer.fer_id)
        tmp_state.upsert_in_list(
            "specs",
            {"fer_id": fer.fer_id, **_mock_spec(fer.fer_id)},
            key_field="fer_id",
        )
        writer, architect, _ = self._setup(fer_manager, tmp_state)
        with pytest.raises(HumanSpecGateRequired):
            writer.run(fer.fer_id)
        architect.write_enhancement_spec.assert_not_called()

    def test_run_fails_without_feasibility_report(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        fer = fer_manager.submit(_valid_fer_data())
        fer_manager.triage(fer.fer_id, "APPROVED")
        fer_manager.start_feasibility(fer.fer_id)
        fer_manager.record_feasibility_approved(fer.fer_id, "Minimal adapter", "FEASIBLE")
        # No feasibility report in state
        writer, _, _ = self._setup(fer_manager, tmp_state)
        with pytest.raises(SpecError, match="No feasibility report"):
            writer.run(fer.fer_id)

    def test_approve_generates_tasks(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        fer = self._feasibility_approved_fer(fer_manager, tmp_state)
        writer, _, task_mgr = self._setup(fer_manager, tmp_state)
        with pytest.raises(HumanSpecGateRequired):
            writer.run(fer.fer_id)
        updated_fer, tasks = writer.approve(fer.fer_id)
        # Mock spec has 2 implementation steps → 2 tasks
        assert len(tasks) == 2
        assert updated_fer.status == "SPEC_APPROVED"
        assert len(updated_fer.task_ids) == 2

    def test_approve_preserves_spec_acceptance_criteria_for_all_generated_tasks(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        fer = self._feasibility_approved_fer(fer_manager, tmp_state)
        writer, _, _ = self._setup(fer_manager, tmp_state)

        with pytest.raises(HumanSpecGateRequired):
            writer.run(fer.fer_id)

        persisted_spec = writer.get_spec(fer.fer_id)
        assert persisted_spec is not None
        expected_criteria = writer._flatten_criteria(
            persisted_spec.get("acceptance_criteria", {})
        )

        _, tasks = writer.approve(fer.fer_id)
        assert len(tasks) > 1
        assert expected_criteria

        for task in tasks:
            assert task.acceptance_criteria == expected_criteria

    def test_approve_fails_without_spec(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        fer = self._feasibility_approved_fer(fer_manager, tmp_state)
        writer, _, _ = self._setup(fer_manager, tmp_state)
        with pytest.raises(SpecError, match="No spec found"):
            writer.approve(fer.fer_id)

    def test_validates_missing_test_stubs(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        bad_spec = {**_mock_spec(), "test_stubs": []}
        fer = self._feasibility_approved_fer(fer_manager, tmp_state)
        writer, architect, _ = self._setup(fer_manager, tmp_state, spec=bad_spec)
        with pytest.raises(SpecError, match="no test_stubs"):
            writer.run(fer.fer_id)

    def test_validates_missing_implementation_order(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        bad_spec = {**_mock_spec(), "implementation_order": []}
        fer = self._feasibility_approved_fer(fer_manager, tmp_state)
        writer, architect, _ = self._setup(fer_manager, tmp_state, spec=bad_spec)
        with pytest.raises(SpecError, match="no implementation_order"):
            writer.run(fer.fer_id)

    def test_tasks_marked_as_phase3(
        self, fer_manager: FERManager, tmp_state: StateStore
    ) -> None:
        fer = self._feasibility_approved_fer(fer_manager, tmp_state)
        writer, _, task_mgr = self._setup(fer_manager, tmp_state)
        with pytest.raises(HumanSpecGateRequired):
            writer.run(fer.fer_id)
        writer.approve(fer.fer_id)
        # All create_task calls should have is_phase3=True
        for call in task_mgr.create_task.call_args_list:
            assert call.kwargs.get("is_phase3") is True


# ═══════════════════════════════════════════════════════════════════════
# Stub distribution helper
# ═══════════════════════════════════════════════════════════════════════

class TestDistributeStubs:
    def test_empty_stubs_returns_empty(self) -> None:
        assert _distribute_stubs([], 0, 3) == []

    def test_zero_steps_returns_empty(self) -> None:
        stubs = [{"method": "test_a"}, {"method": "test_b"}]
        assert _distribute_stubs(stubs, 0, 0) == []

    def test_distributes_evenly(self) -> None:
        stubs = [{"method": f"test_{i}"} for i in range(6)]
        step0 = _distribute_stubs(stubs, 0, 3)
        step1 = _distribute_stubs(stubs, 1, 3)
        step2 = _distribute_stubs(stubs, 2, 3)
        assert len(step0) == 2
        assert len(step1) == 2
        assert len(step2) == 2

    def test_last_step_gets_remainder(self) -> None:
        stubs = [{"method": f"test_{i}"} for i in range(5)]
        last = _distribute_stubs(stubs, 1, 2)
        assert len(last) == 3  # 5 - 2 = 3 remainder


# ═══════════════════════════════════════════════════════════════════════
# CompatChecker
# ═══════════════════════════════════════════════════════════════════════

def _mock_task(is_phase3: bool = True) -> MagicMock:
    from sentinel.orchestrator.task_manager import Task, TaskState
    task = MagicMock(spec=Task)
    task.task_id = "task-abc123"
    task.is_phase3 = is_phase3
    task.source_fer_id = "FER-001"
    task.description = "Add CSV export"
    task.acceptance_criteria = ["Given auth When export Then 200"]
    task.affected_files = ["reporting/exporter.py"]
    return task


class TestCompatChecker:
    def _make_checker(
        self, verdict: str = "PASS", violations: list | None = None
    ) -> tuple[CompatChecker, MagicMock]:
        qa = MagicMock()
        qa.check_backwards_compat.return_value = {
            "verdict": verdict,
            "checks_performed": [
                {"check": "api_contract_preservation", "result": "PASS", "detail": "No changes"}
            ],
            "violations": violations or [],
            "warnings": [],
            "cross_repo_impact": {"affected_repositories": []},
            "deployment_notes": "",
        }
        store = MagicMock()
        store.read.return_value = []
        store.append_to_list = MagicMock()
        checker = CompatChecker(qa=qa, store=store)
        return checker, qa

    def test_pass_verdict_returns_result(self) -> None:
        checker, _ = self._make_checker("PASS")
        result = checker.check(_mock_task(), "diff content")
        assert result["verdict"] == "PASS"

    def test_fail_verdict_raises_compatibility_violation(self) -> None:
        violations = [
            {
                "severity": "BREAKING",
                "check": "api_contract_preservation",
                "location": "api/routes.py:45",
                "description": "Removed required field 'id' from response",
                "affected_consumers": ["frontend"],
                "remediation": "Keep 'id' field or version the endpoint",
            }
        ]
        checker, _ = self._make_checker("FAIL", violations)
        with pytest.raises(CompatibilityViolation) as exc_info:
            checker.check(_mock_task(), "diff")
        assert exc_info.value.task_id == "task-abc123"
        assert len(exc_info.value.violations) == 1

    def test_fail_without_breaking_violations_still_blocks(self) -> None:
        """FAIL is authoritative: a FAIL verdict with only non-BREAKING
        violations must still raise CompatibilityViolation (not return PASS)."""
        violations = [
            {
                "severity": "WARNING",
                "check": "database_schema_evolution",
                "location": "migrations/001.sql",
                "description": "Migration not reversible",
                "affected_consumers": [],
                "remediation": "Consider adding rollback",
            }
        ]
        checker, _ = self._make_checker("FAIL", violations)
        with pytest.raises(CompatibilityViolation) as exc_info:
            checker.check(_mock_task(), "diff")
        # The full violation list is surfaced, not a BREAKING-filtered subset.
        assert len(exc_info.value.violations) == 1

    def test_fail_with_empty_violations_raises_error(self) -> None:
        """A FAIL verdict with an empty violations list is a malformed payload
        and must block via CompatCheckerError rather than returning normally."""
        checker, _ = self._make_checker("FAIL", [])
        with pytest.raises(CompatCheckerError, match="empty or malformed"):
            checker.check(_mock_task(), "diff")

    def test_fail_with_malformed_violations_raises_error(self) -> None:
        """A FAIL verdict whose violations payload is not a list of dicts is
        malformed and must block via CompatCheckerError."""
        qa = MagicMock()
        qa.check_backwards_compat.return_value = {
            "verdict": "FAIL",
            "checks_performed": [],
            "violations": "boom",  # malformed: not a list
            "warnings": [],
        }
        store = MagicMock()
        store.read.return_value = []
        store.append_to_list = MagicMock()
        checker = CompatChecker(qa=qa, store=store)
        with pytest.raises(CompatCheckerError, match="empty or malformed"):
            checker.check(_mock_task(), "diff")

    def test_fail_never_returns_pass_result(self) -> None:
        """No FAIL path may return a result dict (which the caller would treat
        as a PASS outcome)."""
        violations = [{"severity": "INFO", "location": "x", "description": "y"}]
        checker, _ = self._make_checker("FAIL", violations)
        with pytest.raises((CompatibilityViolation, CompatCheckerError)):
            checker.check(_mock_task(), "diff")

    def test_persists_review_record(self) -> None:
        checker, _ = self._make_checker("PASS")
        checker.check(_mock_task(), "diff")
        checker._store.append_to_list.assert_called_once()
        call_args = checker._store.append_to_list.call_args
        assert call_args[0][0] == "reviews"
        record = call_args[0][1]
        assert record["skill_used"] == "layer7c-backwards-compatibility-checker"
        assert record["verdict"] == "PASS"

    def test_raises_on_missing_verdict_key(self) -> None:
        qa = MagicMock()
        qa.check_backwards_compat.return_value = {
            "checks_performed": [], "violations": []
        }
        store = MagicMock()
        store.read.return_value = []
        checker = CompatChecker(qa=qa, store=store)
        with pytest.raises(CompatCheckerError, match="missing required keys"):
            checker.check(_mock_task(), "diff")

    def test_raises_on_invalid_verdict_value(self) -> None:
        qa = MagicMock()
        qa.check_backwards_compat.return_value = {
            "verdict": "MAYBE", "checks_performed": [], "violations": []
        }
        store = MagicMock()
        store.read.return_value = []
        checker = CompatChecker(qa=qa, store=store)
        with pytest.raises(CompatCheckerError, match="invalid verdict"):
            checker.check(_mock_task(), "diff")

    def test_context_includes_task_fields(self) -> None:
        checker, qa = self._make_checker("PASS")
        task = _mock_task()
        checker.check(task, "my diff content")
        # Called positionally: check_backwards_compat(context)
        context_passed = qa.check_backwards_compat.call_args[0][0]
        assert "task-abc123" in context_passed
        assert "my diff content" in context_passed


# ═══════════════════════════════════════════════════════════════════════
# Skills loader — Layer 7 constants
# ═══════════════════════════════════════════════════════════════════════

class TestLayer7SkillConstants:
    def test_feasibility_analyzer_constant(self) -> None:
        assert Skills.FEASIBILITY_ANALYZER == "layer7a-feature-feasibility-analyzer"

    def test_spec_writer_constant(self) -> None:
        assert Skills.SPEC_WRITER == "layer7b-enhancement-spec-writer"

    def test_compat_checker_constant(self) -> None:
        assert Skills.COMPAT_CHECKER == "layer7c-backwards-compatibility-checker"

    def test_layer7_skills_are_distinct(self) -> None:
        layer7 = [Skills.FEASIBILITY_ANALYZER, Skills.SPEC_WRITER, Skills.COMPAT_CHECKER]
        assert len(set(layer7)) == 3


# ═══════════════════════════════════════════════════════════════════════
# StateStore — Phase 3 stores
# ═══════════════════════════════════════════════════════════════════════

class TestStateStorePhase3:
    def test_fers_defaults_to_empty_list(self, tmp_state: StateStore) -> None:
        assert tmp_state.read("fers") == []

    def test_feasibility_defaults_to_empty_list(self, tmp_state: StateStore) -> None:
        assert tmp_state.read("feasibility") == []

    def test_specs_defaults_to_empty_list(self, tmp_state: StateStore) -> None:
        assert tmp_state.read("specs") == []

    def test_upsert_fer_roundtrip(self, tmp_state: StateStore) -> None:
        record = {"fer_id": "FER-001", "title": "Test", "status": "PENDING"}
        tmp_state.upsert_in_list("fers", record, key_field="fer_id")
        fers = tmp_state.read("fers")
        assert len(fers) == 1
        assert fers[0]["fer_id"] == "FER-001"

    def test_upsert_updates_existing_record(self, tmp_state: StateStore) -> None:
        record = {"fer_id": "FER-001", "status": "PENDING"}
        tmp_state.upsert_in_list("fers", record, key_field="fer_id")
        updated = {"fer_id": "FER-001", "status": "APPROVED"}
        tmp_state.upsert_in_list("fers", updated, key_field="fer_id")
        fers = tmp_state.read("fers")
        assert len(fers) == 1
        assert fers[0]["status"] == "APPROVED"

    def test_multiple_phase3_stores_independent(self, tmp_state: StateStore) -> None:
        tmp_state.upsert_in_list("fers", {"fer_id": "FER-001"}, key_field="fer_id")
        tmp_state.upsert_in_list("feasibility", {"fer_id": "FER-001", "verdict": "FEASIBLE"}, key_field="fer_id")
        tmp_state.upsert_in_list("specs", {"fer_id": "FER-001", "steps": 2}, key_field="fer_id")
        assert len(tmp_state.read("fers")) == 1
        assert len(tmp_state.read("feasibility")) == 1
        assert len(tmp_state.read("specs")) == 1


# ═══════════════════════════════════════════════════════════════════════
# FER dataclass serialisation
# ═══════════════════════════════════════════════════════════════════════

class TestFERSerialisation:
    def test_to_dict_roundtrip(self) -> None:
        fer = FER(
            fer_id="FER-042",
            title="Test FER",
            business_justification="Because.",
            priority="SHOULD_HAVE",
            affected_systems=["api"],
            status="APPROVED",
        )
        data = fer.to_dict()
        restored = FER.from_dict(data)
        assert restored.fer_id == "FER-042"
        assert restored.title == "Test FER"
        assert restored.priority == "SHOULD_HAVE"
        assert restored.status == "APPROVED"

    def test_from_dict_handles_missing_optional_fields(self) -> None:
        minimal = {
            "fer_id": "FER-001",
            "title": "Minimal",
            "business_justification": "Needed.",
            "priority": "COULD_HAVE",
        }
        fer = FER.from_dict(minimal)
        assert fer.affected_systems == []
        assert fer.task_ids == []
        assert fer.history == []
        assert fer.selected_approach == ""
