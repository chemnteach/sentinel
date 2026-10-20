"""P1 wave-3 tests for agent response-contract hardening.

Covers:
  - DocsAgent.detect_doc_drift: validates required drift-report keys, raising
    AgentResponseError when any are missing instead of returning raw run_json.
  - QAAgent.verify_fix: unrecognized verdict shapes now raise AgentResponseError
    rather than being silently coerced to FAIL; the verdict key is matched
    case-insensitively.
  - ArchitectAgent.review_todo: maps the four documented TODO-review fields,
    deriving them from a Quick-Review-shaped response when absent.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sentinel.agents.architect import ArchitectAgent
from sentinel.agents.base import AgentResponseError
from sentinel.agents.docs import DocsAgent
from sentinel.agents.qa import QAAgent
from sentinel.api.client import ModelClient
from sentinel.config import Config
from sentinel.skills.loader import SkillLoader


@pytest.fixture()
def config() -> Config:
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
    loader.load.return_value = "# stub skill"
    return loader


# ---------------------------------------------------------------------------
# DocsAgent.detect_doc_drift
# ---------------------------------------------------------------------------


class TestDetectDocDrift:
    def _valid(self) -> dict:
        return {
            "drifted_docs": ["README.md"],
            "drift_severity": "high",
            "recommended_updates": ["Update the API section"],
        }

    def test_valid_report_passes_through(self, config, mock_client, mock_loader):
        docs = DocsAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = self._valid()

        result = docs.detect_doc_drift("ctx")

        assert result["drift_severity"] == "high"
        assert result["drifted_docs"] == ["README.md"]

    def test_missing_severity_raises(self, config, mock_client, mock_loader):
        docs = DocsAgent(config, mock_client, mock_loader)
        bad = self._valid()
        del bad["drift_severity"]
        mock_client.call_model_json.return_value = bad

        with pytest.raises(AgentResponseError, match="drift_severity"):
            docs.detect_doc_drift("ctx")

    def test_missing_all_keys_raises(self, config, mock_client, mock_loader):
        docs = DocsAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"unrelated": True}

        with pytest.raises(AgentResponseError, match="missing required keys"):
            docs.detect_doc_drift("ctx")


# ---------------------------------------------------------------------------
# QAAgent.verify_fix — unrecognized verdicts and case-insensitive key
# ---------------------------------------------------------------------------


class TestVerifyFixVerdictHardening:
    def test_unrecognized_string_verdict_raises_not_coerced_to_fail(
        self, config, mock_client, mock_loader
    ):
        """A verdict that is neither PASS nor FAIL (e.g. 'MAYBE') must raise
        rather than silently become a fabricated FAIL."""
        qa = QAAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"verdict": "MAYBE"}

        with pytest.raises(AgentResponseError, match="unrecognized verdict"):
            qa.verify_fix("ctx")

    def test_empty_dict_verdict_raises(self, config, mock_client, mock_loader):
        qa = QAAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"verdict": {}}

        with pytest.raises(AgentResponseError):
            qa.verify_fix("ctx")

    def test_case_variant_verdict_key_is_recognized(
        self, config, mock_client, mock_loader
    ):
        """'Verdict' (capitalized key) must be picked up, not treated as
        a missing verdict."""
        qa = QAAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"Verdict": "PASS"}

        result = qa.verify_fix("ctx")

        assert result["verdict"] == "PASS"

    def test_upper_case_verdict_key_normalizes(
        self, config, mock_client, mock_loader
    ):
        qa = QAAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"VERDICT": "FAIL"}

        result = qa.verify_fix("ctx")

        assert result["verdict"] == "FAIL"

    def test_recognized_pass_still_passes(self, config, mock_client, mock_loader):
        qa = QAAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"verdict": "PASS"}

        result = qa.verify_fix("ctx")

        assert result["verdict"] == "PASS"

    def test_conditional_pass_still_normalizes_to_pass(
        self, config, mock_client, mock_loader
    ):
        """Anti-regression: the existing CONDITIONAL PASS -> PASS contract
        must survive the new validation."""
        qa = QAAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {"verdict": "CONDITIONAL PASS"}

        result = qa.verify_fix("ctx")

        assert result["verdict"] == "PASS"


# ---------------------------------------------------------------------------
# ArchitectAgent.review_todo — map/derive the four documented fields
# ---------------------------------------------------------------------------


class TestReviewTodoFieldMapping:
    def test_quick_review_shape_derives_four_fields_low_severity(
        self, config, mock_client, mock_loader
    ):
        """When the model replies in Quick Review shape (highest_priority +
        findings), review_todo derives the four documented TODO-review
        fields. Low severity -> APPROVE."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "highest_priority": "P3",
            "findings": [{"description": "minor style nit"}],
        }

        result = architect.review_todo("ctx")

        assert result["implementation_plan_verdict"] == "APPROVE"
        assert result["test_stubs_verdict"] == "APPROVE"
        assert "implementation_plan_feedback" in result
        assert "test_stubs_feedback" in result
        assert "minor style nit" in result["implementation_plan_feedback"]

    def test_quick_review_shape_p1_derives_revise(
        self, config, mock_client, mock_loader
    ):
        """A Critical/P1 Quick Review result must fail closed to REVISE."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "highest_priority": "P1",
            "findings": [{"issue": "unhandled error path"}],
        }

        result = architect.review_todo("ctx")

        assert result["implementation_plan_verdict"] == "REVISE"
        assert result["test_stubs_verdict"] == "REVISE"
        assert "unhandled error path" in result["test_stubs_feedback"]

    def test_explicit_fields_take_precedence_over_derivation(
        self, config, mock_client, mock_loader
    ):
        """A response already carrying the TODO-review fields is untouched by
        the Quick-Review-derivation branch."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "implementation_plan_verdict": "APPROVE",
            "implementation_plan_feedback": "plan ok",
            "test_stubs_verdict": "APPROVE",
            "test_stubs_feedback": "stubs ok",
            "highest_priority": "P1",  # should NOT override explicit APPROVE
        }

        result = architect.review_todo("ctx")

        assert result["implementation_plan_verdict"] == "APPROVE"
        assert result["test_stubs_verdict"] == "APPROVE"
        assert result["implementation_plan_feedback"] == "plan ok"

    def test_feedback_fields_always_present(
        self, config, mock_client, mock_loader
    ):
        """Even a terse valid response gains the documented feedback keys."""
        architect = ArchitectAgent(config, mock_client, mock_loader)
        mock_client.call_model_json.return_value = {
            "implementation_plan_verdict": "APPROVE",
            "test_stubs_verdict": "APPROVE",
        }

        result = architect.review_todo("ctx")

        assert result["implementation_plan_feedback"] == ""
        assert result["test_stubs_feedback"] == ""
