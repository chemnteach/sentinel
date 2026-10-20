from types import SimpleNamespace

import pytest

from sentinel.agents.base import BaseAgent, UnsupportedLocalOnlySkillError
from sentinel.api.client import ModelResponse
from sentinel.skills.loader import Skills, SkillNotFoundError


class DummyAgent(BaseAgent):
    name = "dummy"

    def _build_system_prompt(self, skill_name: str) -> str:
        return "dummy prompt"


@pytest.fixture
def local_only_agent():
    config = SimpleNamespace(local_only_mode=True)
    client = SimpleNamespace()
    loader = SimpleNamespace()
    return DummyAgent(config=config, client=client, loader=loader)


@pytest.fixture
def local_only_agent_with_registry():
    """Local-only agent whose loader knows only SCOPE_GUARDIAN exists."""
    config = SimpleNamespace(local_only_mode=True)
    client = SimpleNamespace()
    known = {Skills.SCOPE_GUARDIAN, Skills.VERIFICATION}
    loader = SimpleNamespace(
        exists=lambda name: name in known,
        list_skills=lambda: sorted(known),
    )
    return DummyAgent(config=config, client=client, loader=loader)


def test_local_only_run_raises_skill_not_found_for_unknown_skill(
    local_only_agent_with_registry,
):
    # FIX-BLOCKING-002/013: unknown skill validated before local-only dispatch.
    with pytest.raises(SkillNotFoundError):
        local_only_agent_with_registry.run("no-such-skill", "payload")


def test_local_only_run_json_raises_skill_not_found_for_unknown_skill(
    local_only_agent_with_registry,
):
    # FIX-BLOCKING-014: run_json unknown skill raises SkillNotFoundError, not a
    # fall-through into local-only handling.
    with pytest.raises(SkillNotFoundError):
        local_only_agent_with_registry.run_json("no-such-skill", "payload")


def test_local_only_known_but_unsupported_skill_raises_unsupported(
    local_only_agent_with_registry,
):
    # An existing skill unsupported in local-only raises the explicit
    # UnsupportedLocalOnlySkillError (not SkillNotFoundError).
    with pytest.raises(UnsupportedLocalOnlySkillError):
        local_only_agent_with_registry.run_json(Skills.VERIFICATION, "Review TASK-123")


def test_local_only_supported_skill_still_executes(local_only_agent_with_registry):
    result = local_only_agent_with_registry.run_json(
        Skills.SCOPE_GUARDIAN, "Implement TASK-123"
    )
    assert result["tests_pass"] is True


def test_local_only_unrecognized_review_skill_does_not_return_approval_payload(local_only_agent):
    """
    Acceptance criterion: When local-only mode receives an unrecognized review-compatible skill, the code does not return a payload with verdict "APPROVE" or equivalent approval metadata.

    Scenario: Local-only execution is invoked with a review-compatible skill that has no explicit local implementation.
    Given: A local-only agent/context and an unrecognized review-compatible skill identifier
    When: The local-only execution path is called for that skill
    Then: The result does not contain verdict='APPROVE' and does not include equivalent passing approval metadata
    """
    with pytest.raises(UnsupportedLocalOnlySkillError):
        local_only_agent.run_json(Skills.VERIFICATION, "Review TASK-123")


def test_local_only_unrecognized_review_skill_returns_explicit_unsupported_outcome(local_only_agent):
    """
    Acceptance criterion: The local-only path returns an explicit unsupported/error outcome or raises a dedicated exception for unrecognized review-compatible skills.

    Scenario: Unsupported review-compatible skill is routed through local-only fallback.
    Given: A local-only execution request for an unrecognized review-compatible skill
    When: The fallback path handles the request
    Then: The code raises a dedicated exception or returns an explicit unsupported/error result
    """
    with pytest.raises(UnsupportedLocalOnlySkillError, match="does not support review skill"):
        local_only_agent.run_json(Skills.VERIFICATION, "Review TASK-123")


def test_local_only_supported_review_skill_is_only_case_allowed_to_emit_approval_style_payload(local_only_agent):
    """
    Acceptance criterion: Any approval-style payload produced in local-only mode is limited to explicitly supported skills with a documented schema-specific implementation path.

    Scenario: Compare explicitly supported local-only review skill behavior against unsupported review-compatible fallback behavior.
    Given: One explicitly supported local-only review skill and one unrecognized review-compatible skill
    When: Both are executed in local-only mode
    Then: Only the explicitly supported skill may emit approval-style review fields; the unsupported skill may not
    """
    supported = local_only_agent.run_json(Skills.SCOPE_GUARDIAN, "Implement TASK-123")

    assert supported["tests_pass"] is True
    assert supported["self_review_triggered"] is True

    with pytest.raises(UnsupportedLocalOnlySkillError):
        local_only_agent.run_json(Skills.VERIFICATION, "Review TASK-123")


def test_regression_unhandled_review_skill_has_no_passing_downstream_approval_fields(local_only_agent):
    """
    Acceptance criterion: A regression test covers an unhandled review-compatible skill and verifies that downstream approval fields are absent or non-passing.

    Scenario: An unhandled review-compatible skill reaches local-only fallback and downstream-facing fields are inspected.
    Given: A local-only result for an unhandled review-compatible skill
    When: The downstream approval-related fields are examined
    Then: Approval fields are absent or explicitly non-passing so the result cannot be mistaken for a real approval
    """
    with pytest.raises(UnsupportedLocalOnlySkillError):
        local_only_agent.run_json(Skills.VERIFICATION, "Review TASK-123")
