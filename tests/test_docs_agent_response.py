"""Tests for DocsAgent response-handling defenses (cycle 6 item 5).

Covers the hardened ``doc_updated`` coercion in
``DocsAgent.generate_transition_manifest`` (commit d20bd7c). Replaced
``bool(result.get("doc_updated", False))`` -- which silently passed any
truthy string through, including ``bool("false") -> True`` -- with an
``isinstance(doc_updated, bool)`` check that warns and coerces to
False for unknown gate states.

Anti-assertion discipline: no test pins ``bool("false") -> True`` or
any other buggy pre-hardening behavior. The string-True test
explicitly asserts coercion to False so the harness fails loudly if
the bug ever returns.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sentinel.agents.docs import DocsAgent
from sentinel.api.client import ModelClient
from sentinel.config import Config
from sentinel.skills.loader import SkillLoader


def _valid_manifest() -> dict[str, Any]:
    return {
        "task_id": "t-001",
        "before_summary": "Before.",
        "after_summary": "After.",
        "docs_updated": ["README.md"],
        "doc_updated": True,
    }


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
    loader.load.return_value = "# stub layer6c"
    return loader


class TestDocUpdatedCoercion:
    def test_doc_updated_string_false_coerces_to_blocking(
        self, agent_config, mock_client, mock_loader, caplog
    ) -> None:
        docs = DocsAgent(agent_config, mock_client, mock_loader)
        bad = _valid_manifest()
        bad["doc_updated"] = "false"
        mock_client.call_model_json.return_value = bad

        with caplog.at_level(logging.WARNING, logger="sentinel.agents.docs"):
            result = docs.generate_transition_manifest("ctx")

        assert result["doc_updated"] is False
        assert "doc_updated of type str" in caplog.text
        assert "blocks PR" in caplog.text

    def test_doc_updated_string_true_still_coerces_to_blocking(
        self, agent_config, mock_client, mock_loader, caplog
    ) -> None:
        """Load-bearing anti-bug test.

        Pre-hardening, ``bool("true")`` returned True and the gate
        opened on a non-bool. Post-hardening, any non-bool (including
        the misleading string "true") must coerce to False so the
        unknown gate state blocks PR creation.
        """
        docs = DocsAgent(agent_config, mock_client, mock_loader)
        bad = _valid_manifest()
        bad["doc_updated"] = "true"
        mock_client.call_model_json.return_value = bad

        with caplog.at_level(logging.WARNING, logger="sentinel.agents.docs"):
            result = docs.generate_transition_manifest("ctx")

        assert result["doc_updated"] is False, (
            "string 'true' must coerce to False; the hardened gate "
            "treats any non-bool as an unknown state that blocks PR"
        )
        assert "doc_updated of type str" in caplog.text

    @pytest.mark.parametrize("value", [True, False])
    def test_doc_updated_proper_bool_passes_through(
        self, agent_config, mock_client, mock_loader, value
    ) -> None:
        docs = DocsAgent(agent_config, mock_client, mock_loader)
        good = _valid_manifest()
        good["doc_updated"] = value
        mock_client.call_model_json.return_value = good

        result = docs.generate_transition_manifest("ctx")

        assert result["doc_updated"] is value
        assert isinstance(result["doc_updated"], bool)

    def test_doc_updated_none_coerces_to_blocking(
        self, agent_config, mock_client, mock_loader, caplog
    ) -> None:
        docs = DocsAgent(agent_config, mock_client, mock_loader)
        bad = _valid_manifest()
        bad["doc_updated"] = None
        mock_client.call_model_json.return_value = bad

        with caplog.at_level(logging.WARNING, logger="sentinel.agents.docs"):
            result = docs.generate_transition_manifest("ctx")

        assert result["doc_updated"] is False
        assert "doc_updated of type NoneType" in caplog.text
