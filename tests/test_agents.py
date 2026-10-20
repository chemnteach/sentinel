"""
tests/test_agents.py

Tests for sentinel.agents.* — agent classes and their behaviour.

Uses MagicMock for ModelClient and SkillLoader to avoid live API calls.
Validates that agents route to the correct skills, handle response
errors, and enforce their contracts (e.g. Coder always injects Scope
Guardian; Architect validates required JSON keys).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sentinel.agents.architect import ArchitectAgent
from sentinel.agents.base import AgentError, AgentResponseError, BaseAgent
from sentinel.agents.coder import CoderAgent
from sentinel.agents.docs import DocsAgent
from sentinel.agents.qa import QAAgent
from sentinel.api.client import ModelClient, ModelResponse
from sentinel.config import Config
from sentinel.skills.loader import Skills, SkillLoader, SkillNotFoundError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def config() -> Config:
    with patch.dict(
        "os.environ",
        {
            "GITHUB_TOKEN": "ghp_test",
            "SENTINEL_REPO_PATH": "/tmp",
        },
    ):
        return Config()


@pytest.fixture()
def mock_client() -> MagicMock:
    client = MagicMock(spec=ModelClient)
    client.call_model.return_value = ModelResponse(
        content="mock response",
        model="gpt-5.1",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
    )
    client.call_model_json.return_value = {"mock": "json"}
    return client


@pytest.fixture()
def mock_loader() -> MagicMock:
    loader = MagicMock(spec=SkillLoader)
    loader.load.return_value = "# Mock skill body\nDo the thing."
    return loader


def make_agent(cls, config, mock_client, mock_loader):
    return cls(config=config, client=mock_client, loader=mock_loader)


# ---------------------------------------------------------------------------
# BaseAgent tests (via a minimal concrete subclass)
# ---------------------------------------------------------------------------

class ConcreteAgent(BaseAgent):
    name = "test-agent"


class TestBaseAgent:
    def test_run_calls_client_with_skill_body(
        self, config, mock_client, mock_loader
    ) -> None:
        agent = ConcreteAgent(config, mock_client, mock_loader)
        mock_loader.load.return_value = "# Skill body"

        agent.run("layer2a-quick-review", "some code")

        mock_loader.load.assert_called_once_with("layer2a-quick-review")
        call_args = mock_client.call_model.call_args
        assert "# Skill body" in call_args.kwargs["system_prompt"]

    def test_run_includes_agent_header(
        self, config, mock_client, mock_loader
    ) -> None:
        agent = ConcreteAgent(config, mock_client, mock_loader)

        agent.run("layer2a-quick-review", "code")

        system_prompt = mock_client.call_model.call_args.kwargs["system_prompt"]
        assert "Test-Agent Agent" in system_prompt or "test-agent" in system_prompt.lower()

    def test_run_json_returns_parsed_dict(
        self, config, mock_client, mock_loader
    ) -> None:
        agent = ConcreteAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"findings": []}

        result = agent.run_json("layer2a-quick-review", "code")

        assert result == {"findings": []}

    def test_run_wraps_api_error(
        self, config, mock_client, mock_loader
    ) -> None:
        agent = ConcreteAgent(config, mock_client, mock_loader)
        mock_client.call_model.side_effect = RuntimeError("network down")

        with pytest.raises(AgentError, match="test-agent"):
            agent.run("layer2a-quick-review", "code")

    def test_run_json_wraps_value_error(
        self, config, mock_client, mock_loader
    ) -> None:
        agent = ConcreteAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.side_effect = ValueError("bad json")

        with pytest.raises(AgentResponseError, match="non-JSON"):
            agent.run_json("layer2a-quick-review", "code")

    def test_skill_not_found_propagates(
        self, config, mock_client, mock_loader
    ) -> None:
        agent = ConcreteAgent(config, mock_client, mock_loader)
        mock_loader.load.side_effect = SkillNotFoundError("nope")

        with pytest.raises(SkillNotFoundError):
            agent.run("nonexistent-skill", "code")

    def test_repr(self, config, mock_client, mock_loader) -> None:
        agent = ConcreteAgent(config, mock_client, mock_loader)
        assert "ConcreteAgent" in repr(agent)
        assert "test-agent" in repr(agent)


# ---------------------------------------------------------------------------
# ArchitectAgent tests
# ---------------------------------------------------------------------------

class TestArchitectAgent:
    def test_map_codebase_uses_cartographer_skill(
        self, config, mock_client, mock_loader
    ) -> None:
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"repositories": [], "modules": []}

        result = architect.map_codebase("file tree content")

        mock_loader.load.assert_called_with(Skills.CARTOGRAPHER)
        assert "repositories" in result

    def test_quick_review_uses_correct_skill(
        self, config, mock_client, mock_loader
    ) -> None:
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "findings": [],
            "module": "test.py",
        }

        architect.quick_review("module code")

        mock_loader.load.assert_called_with(Skills.QUICK_REVIEW)

    def test_review_todo_validates_required_keys(
        self, config, mock_client, mock_loader
    ) -> None:
        architect = ArchitectAgent(config, mock_client, mock_loader)
        # Missing required keys
        mock_client.call_model_json.return_value = {"unexpected": "response"}

        with pytest.raises(AgentResponseError, match="missing required keys"):
            architect.review_todo("todo context")

    def test_review_todo_passes_when_keys_present(
        self, config, mock_client, mock_loader
    ) -> None:
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "implementation_plan_verdict": "APPROVE",
            "implementation_plan_feedback": "",
            "test_stubs_verdict": "APPROVE",
            "test_stubs_feedback": "",
        }

        result = architect.review_todo("context")
        assert result["implementation_plan_verdict"] == "APPROVE"

    def test_review_todo_malformed_response_does_not_approve(
        self, config, mock_client, mock_loader
    ) -> None:
        """Free-form text containing approval substrings (e.g. 'looks good
        except for a critical issue', 'not approved') must NOT be inferred
        as APPROVE. An unparseable structured response is a non-approval."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        # Force the JSON path to fail so the fallback branch runs.
        mock_client.call_model_json.side_effect = ValueError("not json")
        mock_client.call_model.return_value = ModelResponse(
            content="This looks good except for a critical issue; not approved.",
            model="gpt-5.1",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
        )

        result = architect.review_todo("context")

        assert result["implementation_plan_verdict"] == "REVISE"
        assert result["test_stubs_verdict"] == "REVISE"

    def test_review_todo_non_dict_response_defaults_to_revise(
        self, config, mock_client, mock_loader
    ) -> None:
        """A malformed structured payload (non-dict JSON) must fail closed
        to REVISE for both verdicts instead of propagating runtime errors."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = ["APPROVE", "REVISE"]

        result = architect.review_todo("context")

        assert result["implementation_plan_verdict"] == "REVISE"
        assert result["test_stubs_verdict"] == "REVISE"
        assert "malformed" in result["implementation_plan_feedback"].lower()
        assert "malformed" in result["test_stubs_feedback"].lower()

    def test_review_todo_conflicting_explicit_verdicts_remain_non_unblocking(
        self, config, mock_client, mock_loader
    ) -> None:
        """When verdict tokens conflict (one APPROVE, one REVISE),
        review_todo should return a deterministic non-unblocking outcome."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "implementation_plan_verdict": "APPROVE",
            "implementation_plan_feedback": "plan is acceptable",
            "test_stubs_verdict": "REVISE",
            "test_stubs_feedback": "test coverage is missing edge cases",
        }

        result = architect.review_todo("context")

        assert result["implementation_plan_verdict"] == "APPROVE"
        assert result["test_stubs_verdict"] == "REVISE"
        assert not (
            result["implementation_plan_verdict"] == "APPROVE"
            and result["test_stubs_verdict"] == "APPROVE"
        )

    def test_review_todo_explicit_approve_happy_path(
        self, config, mock_client, mock_loader
    ) -> None:
        """A well-formed explicit APPROVE response must preserve approvals."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "implementation_plan_verdict": "APPROVE",
            "implementation_plan_feedback": "looks good",
            "test_stubs_verdict": "APPROVE",
            "test_stubs_feedback": "stubs are complete",
        }

        result = architect.review_todo("context")

        assert result["implementation_plan_verdict"] == "APPROVE"
        assert result["test_stubs_verdict"] == "APPROVE"

    def test_review_todo_rejects_out_of_set_verdict(
        self, config, mock_client, mock_loader
    ) -> None:
        """Only the explicit allowed verdict token may approve; an
        out-of-set structured verdict collapses to REVISE so it cannot
        unblock a TODO."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "implementation_plan_verdict": "APPROVE WITH CHANGES",
            "implementation_plan_feedback": "minor nits",
            "test_stubs_verdict": "looks good",
            "test_stubs_feedback": "",
        }

        result = architect.review_todo("context")

        assert result["implementation_plan_verdict"] == "REVISE"
        assert result["test_stubs_verdict"] == "REVISE"

    def test_review_todo_contradictory_structured_verdict_not_approved(
        self, config, mock_client, mock_loader
    ) -> None:
        """A structured response that pairs an out-of-set/contradictory
        verdict string with negative feedback must not yield APPROVE."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "implementation_plan_verdict": "no P1, but revise",
            "implementation_plan_feedback": "needs rework",
            "test_stubs_verdict": "REVISE",
            "test_stubs_feedback": "missing edge cases",
        }

        result = architect.review_todo("context")

        assert result["implementation_plan_verdict"] == "REVISE"
        assert result["test_stubs_verdict"] == "REVISE"

    def test_review_todo_normalizes_lowercase_approve(
        self, config, mock_client, mock_loader
    ) -> None:
        """An explicit allowed verdict token differing only by case/whitespace
        is normalized to the canonical APPROVE."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "implementation_plan_verdict": " approve ",
            "implementation_plan_feedback": "",
            "test_stubs_verdict": "Approve",
            "test_stubs_feedback": "",
        }

        result = architect.review_todo("context")

        assert result["implementation_plan_verdict"] == "APPROVE"
        assert result["test_stubs_verdict"] == "APPROVE"

    def test_review_diff_derives_reject_when_verdict_absent(
        self, config, mock_client, mock_loader
    ) -> None:
        # Quick Review output with no `verdict` key and no recognized
        # highest_priority must fail-closed to REJECT, not silently APPROVE.
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"no_verdict_key": True}

        result = architect.review_diff(Skills.QUICK_REVIEW, "diff context")
        assert result["verdict"] == "REJECT"

    def test_review_diff_derives_reject_on_critical(
        self, config, mock_client, mock_loader
    ) -> None:
        # A Critical highest_priority in the Quick Review output derives REJECT.
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"highest_priority": "Critical"}

        result = architect.review_diff(Skills.QUICK_REVIEW, "diff context")
        assert result["verdict"] == "REJECT"

    def test_review_diff_missing_verdict_with_no_priority_does_not_approve(
        self, config, mock_client, mock_loader
    ) -> None:
        """When verdict is absent and no recognized highest_priority,
        review_diff must NOT silently APPROVE (fail-closed gate)."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"no_verdict_key": True}

        result = architect.review_diff(Skills.QUICK_REVIEW, "diff context")

        assert result["verdict"] != "APPROVE"

    def test_review_diff_malformed_priority_does_not_approve(
        self, config, mock_client, mock_loader
    ) -> None:
        """An unrecognized highest_priority value must not silently APPROVE."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"highest_priority": "malformed-garbage"}

        result = architect.review_diff(Skills.QUICK_REVIEW, "diff context")

        assert result["verdict"] != "APPROVE"

    def test_review_diff_unparseable_response_does_not_approve(
        self, config, mock_client, mock_loader
    ) -> None:
        """When run_json fails and free-form text is ambiguous,
        review_diff must NOT silently APPROVE (mirrors review_todo fail-closed).

        run_json converts ValueError → AgentResponseError, which review_diff
        catches to fall back to free-form text parsing.
        """
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.side_effect = ValueError("not json")
        mock_client.call_model.return_value = ModelResponse(
            content="I reviewed this and it seems acceptable overall.",
            model="gpt-5.1",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
        )

        result = architect.review_diff(Skills.QUICK_REVIEW, "diff context")

        assert result["verdict"] != "APPROVE"

    def test_review_diff_explicit_approve_verdict_passes_through(
        self, config, mock_client, mock_loader
    ) -> None:
        """An explicit verdict=APPROVE from the model must still be honoured."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "verdict": "APPROVE",
            "findings": [],
            "feedback": "No issues found.",
            "gate_checks_passed": True,
        }

        result = architect.review_diff(Skills.QUICK_REVIEW, "diff context")

        assert result["verdict"] == "APPROVE"

    def test_review_diff_normalizes_lowercase_verdict(
        self, config, mock_client, mock_loader
    ) -> None:
        """FIX-BLOCKING-088: a lowercase 'approve' token is normalized to
        APPROVE and gate_checks_passed is derived consistently."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "verdict": "approve",
            "findings": [],
            "feedback": "ok",
            "gate_checks_passed": False,
        }

        result = architect.review_diff(Skills.QUICK_REVIEW, "diff context")

        assert result["verdict"] == "APPROVE"
        assert result["gate_checks_passed"] is True

    def test_review_diff_rejects_invalid_verdict_token(
        self, config, mock_client, mock_loader
    ) -> None:
        """FIX-BLOCKING-088: an out-of-set verdict token fails closed to REJECT
        with a consistent gate_checks_passed flag, not passed through."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "verdict": "maybe",
            "findings": [],
            "feedback": "unclear",
            "gate_checks_passed": True,
        }

        result = architect.review_diff(Skills.QUICK_REVIEW, "diff context")

        assert result["verdict"] == "REJECT"
        assert result["gate_checks_passed"] is False

    def test_review_diff_gate_checks_passed_consistent_with_reject(
        self, config, mock_client, mock_loader
    ) -> None:
        """FIX-BLOCKING-088: a REJECT verdict paired with a truthy
        gate_checks_passed from the model is corrected to be consistent."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "verdict": "REJECT",
            "findings": [],
            "feedback": "problems",
            "gate_checks_passed": True,
        }

        result = architect.review_diff(Skills.QUICK_REVIEW, "diff context")

        assert result["verdict"] == "REJECT"
        assert result["gate_checks_passed"] is False

    @staticmethod
    def _remediation_pattern_payload(notes: Any) -> dict[str, Any]:
        return {
            "pattern_id": "P1",
            "group_id": "G1",
            "pattern_kind": "atomic-write",
            "title": "t",
            "rationale": "r",
            "proposed_changes": [],
            "acceptance_criteria": [],
            "scope_signals": [],
            "confidence": "high",
            "notes": notes,
        }

    def test_propose_remediation_pattern_coerces_null_notes(
        self, config, mock_client, mock_loader
    ) -> None:
        """FIX-BLOCKING-089: a null `notes` payload is coerced to a dict with
        the required subfields defaulted."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = self._remediation_pattern_payload(None)

        result = architect.propose_remediation_pattern("group context")

        assert isinstance(result["notes"], dict)
        assert result["notes"]["silent_peer_notes"] == ""
        assert result["notes"]["canonical_emitter_caveats"] == ""

    def test_propose_remediation_pattern_coerces_string_notes(
        self, config, mock_client, mock_loader
    ) -> None:
        """FIX-BLOCKING-089: a non-dict (string) `notes` payload is coerced to a
        dict with the required subfields present."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = self._remediation_pattern_payload(
            "freeform text"
        )

        result = architect.propose_remediation_pattern("group context")

        assert isinstance(result["notes"], dict)
        assert "silent_peer_notes" in result["notes"]
        assert "canonical_emitter_caveats" in result["notes"]

    def test_build_remediation_plan_uses_fix_planner(
        self, config, mock_client, mock_loader
    ) -> None:
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"blocking": [], "required": []}

        architect.build_remediation_plan("findings context")

        mock_loader.load.assert_called_with(Skills.FIX_PLANNER)


# ---------------------------------------------------------------------------
# CoderAgent tests
# ---------------------------------------------------------------------------

class TestCoderAgent:
    def test_scope_guardian_injected_into_system_prompt(
        self, config, mock_client, mock_loader
    ) -> None:
        coder = CoderAgent(config, mock_client, mock_loader)
        mock_loader.load.side_effect = lambda name: f"# {name} body"
        mock_client.call_model_json.return_value = {
            "task_id": "task-abc",
            "implementation_plan": {"summary": "fix x", "files_to_modify": [], "approach": "..."},
            "test_stubs": {"test_file": "test_x.py", "stubs": [], "coverage_summary": "..."},
        }

        coder.submit_todo("task context")

        calls = mock_loader.load.call_args_list
        loaded_skills = [c.args[0] for c in calls]
        assert Skills.SCOPE_GUARDIAN in loaded_skills

    def test_submit_todo_validates_required_keys(
        self, config, mock_client, mock_loader
    ) -> None:
        coder = CoderAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"only_task_id": "123"}

        with pytest.raises(AgentResponseError, match="missing required keys"):
            coder.submit_todo("context")

    def test_submit_todo_passes_when_schema_valid(
        self, config, mock_client, mock_loader
    ) -> None:
        coder = CoderAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "task_id": "task-abc",
            "implementation_plan": {"summary": "fix x", "files_to_modify": [], "approach": "..."},
            "test_stubs": {"test_file": "test_x.py", "stubs": [], "coverage_summary": "..."},
        }

        result = coder.submit_todo("context")
        assert result["task_id"] == "task-abc"

    def test_self_review_uses_quick_review(
        self, config, mock_client, mock_loader
    ) -> None:
        coder = CoderAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"findings": []}
        mock_loader.load.side_effect = lambda name: f"# {name}"

        coder.self_review("diff content")

        calls = [c.args[0] for c in mock_loader.load.call_args_list]
        assert Skills.QUICK_REVIEW in calls


# ---------------------------------------------------------------------------
# QAAgent tests
# ---------------------------------------------------------------------------

class TestQAAgent:
    def test_verify_fix_uses_verification_skill(
        self, config, mock_client, mock_loader
    ) -> None:
        qa = QAAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "verdict": "PASS",
            "tdd_assessment": {},
        }

        qa.verify_fix("context with diff")

        mock_loader.load.assert_called_with(Skills.VERIFICATION)

    def test_verify_fix_validates_verdict(
        self, config, mock_client, mock_loader
    ) -> None:
        qa = QAAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"no_verdict": True}

        with pytest.raises(AgentResponseError, match="verdict"):
            qa.verify_fix("context")

    def test_verify_fix_defaults_to_layer6a_when_skill_name_omitted(
        self, config, mock_client, mock_loader
    ) -> None:
        """Backward-compat regression for the runner.py call path.

        The signature extension in commit 4fca479 added a default
        ``skill_name=Skills.VERIFICATION`` so existing callers that
        invoke ``verify_fix(context)`` without the kwarg continue to
        route to Layer 6a unchanged.
        """
        qa = QAAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "verdict": "PASS",
            "tdd_assessment": {},
        }

        qa.verify_fix("context with diff")

        mock_loader.load.assert_called_with(Skills.VERIFICATION)

    def test_verify_fix_routes_to_custom_skill_when_skill_name_passed(
        self, config, mock_client, mock_loader
    ) -> None:
        """Orchestrator threads task.verification_required[0] here."""
        qa = QAAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "verdict": "PASS",
            "tdd_assessment": {},
        }

        qa.verify_fix("context", skill_name="custom-verifier")

        mock_loader.load.assert_called_with("custom-verifier")

    def test_verify_fix_normalizes_dict_conditional_pass_to_pass(
        self, config, mock_client, mock_loader
    ) -> None:
        """The model sometimes returns the verdict as a structured dict;
        a CONDITIONAL PASS status must normalize to PASS."""
        qa = QAAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "verdict": {
                "STATUS": "⚠️ CONDITIONAL PASS",
                "FIX": "some condition",
                "REVIEWER": "QA",
            }
        }

        result = qa.verify_fix("context", skill_name=Skills.VERIFICATION)

        assert result["verdict"] == "PASS"

    def test_verify_fix_normalizes_string_conditional_pass_to_pass(
        self, config, mock_client, mock_loader
    ) -> None:
        """A plain 'CONDITIONAL PASS' string verdict normalizes to PASS."""
        qa = QAAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"verdict": "CONDITIONAL PASS"}

        result = qa.verify_fix("context", skill_name=Skills.VERIFICATION)

        assert result["verdict"] == "PASS"

    def test_verify_fix_keeps_fail_verdict(
        self, config, mock_client, mock_loader
    ) -> None:
        """A FAIL verdict stays FAIL after normalization."""
        qa = QAAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"verdict": "FAIL"}

        result = qa.verify_fix("context", skill_name=Skills.VERIFICATION)

        assert result["verdict"] == "FAIL"


# ---------------------------------------------------------------------------
# DocsAgent tests have moved to tests/test_transition_manifest.py as of the
# Stage 6c transition manifest cycle. The two prior tests
# (test_update_affected_docs_returns_doc_updated_flag,
# test_update_logs_warning_when_doc_updated_false) were recharacterized as
# migration-contract tests under TestBackwardCompatibility in that file.
# ---------------------------------------------------------------------------
