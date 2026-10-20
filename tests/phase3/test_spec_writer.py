from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sentinel.phase3.spec_writer import HumanSpecGateRequired, SpecError, SpecWriter


class _DummyStore:
    def __init__(self, feasibility=None, specs=None):
        self._feasibility = feasibility or []
        self._specs = specs or []

    def read(self, key):
        if key == "feasibility":
            return self._feasibility
        if key == "specs":
            return self._specs
        return []

    def upsert_in_list(self, *args, **kwargs):
        pass

    def commit_state(self, *args, **kwargs):
        pass


def _valid_spec(fer_id: str) -> dict:
    return {
        "fer_id": fer_id,
        "feature_summary": "summary",
        "acceptance_criteria": {"functional": ["criterion"]},
        "scope_boundary": {"in_scope": ["sentinel/phase3/spec_writer.py"], "out_of_scope": []},
        "test_stubs": [{"name": "test_example"}],
        "implementation_order": ["do the thing"],
        "non_goals": [],
    }


def test_run_accepts_feasibility_approved_entry_state():
    """
    Acceptance criterion: SpecWriter.run accepts FEASIBILITY_APPROVED for a fresh run and accepts IN_SPEC only if resume behavior is explicitly supported by the current implementation.

    Scenario: Fresh run begins from the documented allowed entry state.
    Given: A FER in FEASIBILITY_APPROVED and a SpecWriter configured for a normal fresh run.
    When: SpecWriter.run is invoked.
    Then: The method does not raise SpecError for the entry-state gate and proceeds into downstream processing.
    """
    fer_id = "FER-123"
    fer = SimpleNamespace(fer_id=fer_id, status="FEASIBILITY_APPROVED")
    fer_in_spec = SimpleNamespace(fer_id=fer_id, status="IN_SPEC")
    fer_manager = Mock()
    fer_manager.get.return_value = fer
    fer_manager.start_spec.return_value = fer_in_spec
    fer_manager.build_spec_context.return_value = {"context": True}
    architect = Mock()
    architect.write_enhancement_spec.return_value = _valid_spec(fer_id)
    store = _DummyStore(feasibility=[{"fer_id": fer_id, "report": "ok"}])
    writer = SpecWriter(fer_manager, architect, Mock(), store)

    with pytest.raises(HumanSpecGateRequired):
        writer.run(fer_id)

    fer_manager.start_spec.assert_called_once_with(fer_id)
    fer_manager.build_spec_context.assert_called_once()
    architect.write_enhancement_spec.assert_called_once()


def test_run_from_in_spec_resumes_existing_spec_without_regenerating():
    """
    Acceptance criterion: IN_SPEC is an allowed entry state; when an existing spec is present, SpecWriter.run resumes it without re-running the generation path.

    Scenario: The documented contract permits entering SpecWriter.run from IN_SPEC and reusing the existing spec.
    Given: A FER in IN_SPEC state with an existing spec already stored.
    When: SpecWriter.run is invoked.
    Then: The resume path raises HumanSpecGateRequired without calling start_spec, build_spec_context, or write_enhancement_spec, and the stored specs are left unchanged.
    """
    fer_id = "FER-124"
    fer = SimpleNamespace(fer_id=fer_id, status="IN_SPEC")
    existing_spec = _valid_spec(fer_id)
    fer_manager = Mock()
    fer_manager.get.return_value = fer
    architect = Mock()
    store = _DummyStore(
        feasibility=[{"fer_id": fer_id, "report": "ok"}],
        specs=[existing_spec],
    )
    writer = SpecWriter(fer_manager, architect, Mock(), store)

    with pytest.raises(HumanSpecGateRequired):
        writer.run(fer_id)

    fer_manager.start_spec.assert_not_called()
    fer_manager.build_spec_context.assert_not_called()
    architect.write_enhancement_spec.assert_not_called()
    assert store.read("specs") == [existing_spec]


def test_run_from_in_spec_follows_documented_resume_path_before_any_human_gate_when_in_spec_is_allowed():
    """
    Acceptance criterion: If `IN_SPEC` is an allowed entry state, the test asserts the documented resume path before any later human gate is raised.

    Scenario: The documented contract permits entering SpecWriter.run from IN_SPEC and resuming work.
    Given: A workflow/task prepared in IN_SPEC state with instrumentation to observe the resume path.
    When: SpecWriter.run is invoked.
    Then: The documented resume-path effect occurs before any later HumanSpecGateRequired is raised.
    """
    fer_id = "FER-127"
    fer = SimpleNamespace(fer_id=fer_id, status="IN_SPEC")
    existing_spec = _valid_spec(fer_id)
    fer_manager = Mock()
    fer_manager.get.return_value = fer
    architect = Mock()
    store = _DummyStore(
        feasibility=[{"fer_id": fer_id, "report": "ok"}],
        specs=[existing_spec],
    )
    writer = SpecWriter(fer_manager, architect, Mock(), store)

    with pytest.raises(HumanSpecGateRequired) as exc:
        writer.run(fer_id)

    fer_manager.start_spec.assert_not_called()
    fer_manager.build_spec_context.assert_not_called()
    architect.write_enhancement_spec.assert_not_called()
    assert store.read("specs") == [existing_spec]
    assert exc.value.fer_id == fer_id


def test_run_from_in_spec_verifies_contract_without_using_human_spec_gate_alone_as_acceptance_proof():
    """
    Acceptance criterion: The test covering `IN_SPEC` explicitly verifies whether `SpecWriter.run` accepts or rejects `IN_SPEC` according to the documented contract, without using `HumanSpecGateRequired` alone as proof of entry-state acceptance.

    Scenario: The IN_SPEC test distinguishes acceptance from rejection using a state-specific observable.
    Given: A workflow/task in IN_SPEC state and a way to observe either validation failure or resume-path behavior.
    When: SpecWriter.run is invoked.
    Then: The assertion proves the documented IN_SPEC contract directly, rather than inferring acceptance from a later human gate exception.
    """
    fer_id = "FER-128"
    fer = SimpleNamespace(fer_id=fer_id, status="IN_SPEC")
    existing_spec = _valid_spec(fer_id)
    fer_manager = Mock()
    fer_manager.get.return_value = fer
    architect = Mock()
    store = _DummyStore(
        feasibility=[{"fer_id": fer_id, "report": "ok"}],
        specs=[existing_spec],
    )
    writer = SpecWriter(fer_manager, architect, Mock(), store)

    with pytest.raises(HumanSpecGateRequired) as exc:
        writer.run(fer_id)

    fer_manager.start_spec.assert_not_called()
    fer_manager.build_spec_context.assert_not_called()
    architect.write_enhancement_spec.assert_not_called()
    assert store.read("specs") == [existing_spec]
    assert exc.value.fer_id == fer_id


def test_run_raises_spec_error_for_disallowed_entry_state():
    """
    Acceptance criterion: SpecWriter.run raises SpecError when invoked for a FER in any state not explicitly allowed for spec entry.

    Scenario: A FER starts in a state outside the allowed spec-entry states.
    Given: A FER in a disallowed state such as DRAFT or another non-entry state.
    When: SpecWriter.run is invoked.
    Then: SpecWriter.run raises SpecError.
    """
    fer_id = "FER-125"
    fer = SimpleNamespace(fer_id=fer_id, status="DRAFT")
    fer_manager = Mock()
    fer_manager.get.return_value = fer
    writer = SpecWriter(fer_manager, Mock(), Mock(), _DummyStore())

    with pytest.raises(SpecError, match="FEASIBILITY_APPROVED or IN_SPEC"):
        writer.run(fer_id)


def test_run_from_feasibility_approved_observes_post_validation_state_specific_effect_before_later_gate():
    """
    Acceptance criterion: The test covering `FEASIBILITY_APPROVED` verifies a state-specific effect or exception ordering that can only occur after successful entry-state validation.

    Scenario: A valid FEASIBILITY_APPROVED entry proceeds into the documented next step before any later gate interrupts execution.
    Given: A workflow/task prepared in FEASIBILITY_APPROVED state with instrumentation to observe the first post-validation action.
    When: SpecWriter.run is invoked.
    Then: A state-specific post-validation effect or exception ordering is observed that proves entry validation succeeded.
    """
    fer_id = "FER-126"
    fer = SimpleNamespace(fer_id=fer_id, status="FEASIBILITY_APPROVED")
    fer_in_spec = SimpleNamespace(fer_id=fer_id, status="IN_SPEC")
    fer_manager = Mock()
    fer_manager.get.return_value = fer
    fer_manager.start_spec.return_value = fer_in_spec
    fer_manager.build_spec_context.return_value = {"context": True}
    architect = Mock()
    architect.write_enhancement_spec.return_value = _valid_spec(fer_id)
    store = _DummyStore(feasibility=[{"fer_id": fer_id, "report": "ok"}])
    writer = SpecWriter(fer_manager, architect, Mock(), store)

    with pytest.raises(HumanSpecGateRequired):
        writer.run(fer_id)

    fer_manager.start_spec.assert_called_once_with(fer_id)
    fer_manager.build_spec_context.assert_called_once()
    architect.write_enhancement_spec.assert_called_once()


def test_entry_state_tests_fail_if_validation_is_bypassed_even_when_human_spec_gate_is_still_raised():
    """
    Acceptance criterion: The updated tests fail when entry-state validation is bypassed or incorrectly implemented, even if `HumanSpecGateRequired` is still raised later in execution.

    Scenario: A regression causes SpecWriter.run to skip or weaken entry-state validation while still reaching the later human gate.
    Given: Test instrumentation that distinguishes entry validation from later gate raising.
    When: SpecWriter.run is exercised from the covered states.
    Then: The assertions depend on validation-specific behavior, so they would fail under a bypassed-validation regression even if HumanSpecGateRequired still occurs.
    """
    fer_id = "FER-129"
    fer = SimpleNamespace(fer_id=fer_id, status="DRAFT")
    fer_manager = Mock()
    fer_manager.get.return_value = fer
    architect = Mock()
    store = Mock()
    writer = SpecWriter(fer_manager, architect, Mock(), store)

    with pytest.raises(SpecError):
        writer.run(fer_id)

    store.read.assert_not_called()
    fer_manager.start_spec.assert_not_called()
    fer_manager.build_spec_context.assert_not_called()
    architect.write_enhancement_spec.assert_not_called()


def test_run_docstring_matches_enforced_allowed_entry_states():
    """
    Acceptance criterion: The SpecWriter.run docstring states the same allowed entry states that the implementation enforces.

    Scenario: Documentation and behavior remain aligned.
    Given: The SpecWriter.run method definition.
    When: Its docstring is inspected alongside the enforced entry-state behavior.
    Then: The docstring lists exactly the same allowed entry states enforced by the method.
    """
    doc = SpecWriter.run.__doc__ or ""

    assert "FEASIBILITY_APPROVED or IN_SPEC status" in doc
    assert "Accepts FERs entering spec generation from FEASIBILITY_APPROVED" in doc
    assert "IN_SPEC (explicit resume)" in doc


def test_approve_rejects_fer_not_in_spec_before_generating_tasks():
    """Authorization gate: approve() must verify the FER is in IN_SPEC BEFORE
    generating any tasks, so a FER in a non-approvable state cannot leave
    orphan task records behind."""
    fer_id = "FER-200"
    fer = SimpleNamespace(fer_id=fer_id, status="IN_FEASIBILITY")
    fer_manager = Mock()
    fer_manager.get.return_value = fer
    task_manager = Mock()
    store = _DummyStore(specs=[_valid_spec(fer_id)])
    writer = SpecWriter(fer_manager, Mock(), task_manager, store)

    with pytest.raises(SpecError, match="must be in IN_SPEC"):
        writer.approve(fer_id)

    # No tasks were generated and no approval transition was recorded.
    task_manager.create_task.assert_not_called()
    fer_manager.record_spec_approved.assert_not_called()


def test_approve_in_spec_generates_tasks_and_records_approval():
    """A FER in IN_SPEC is approvable: tasks are generated and the approval
    transition is recorded."""
    fer_id = "FER-201"
    fer = SimpleNamespace(fer_id=fer_id, status="IN_SPEC")
    approved = SimpleNamespace(fer_id=fer_id, status="SPEC_APPROVED")
    fer_manager = Mock()
    fer_manager.get.return_value = fer
    fer_manager.record_spec_approved.return_value = approved
    task_manager = Mock()
    task_manager.create_task.return_value = SimpleNamespace(task_id="t1")
    store = _DummyStore(specs=[_valid_spec(fer_id)])
    writer = SpecWriter(fer_manager, Mock(), task_manager, store)

    result_fer, tasks = writer.approve(fer_id)

    assert result_fer is approved
    assert len(tasks) == 1
    fer_manager.record_spec_approved.assert_called_once()


def test_flatten_criteria_skips_non_list_category_values():
    """A model may emit a category whose value is null/scalar; flattening must
    skip it rather than raise TypeError during task generation."""
    criteria = {
        "functional": ["do X"],
        "nonfunctional": None,          # null category
        "security": "not a list",       # scalar category
    }
    flat = SpecWriter._flatten_criteria(criteria)
    assert "do X" in flat
    assert all(isinstance(item, str) for item in flat)
