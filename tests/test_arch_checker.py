"""
tests/test_arch_checker.py

Tests for sentinel.assess.arch_checker — the codebase-level architectural
checklist that detects missing capabilities (absence detection).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, call

import pytest

from sentinel.agents.architect import ArchitectAgent
from sentinel.agents.base import AgentError
from sentinel.assess.arch_checker import ArchChecker, _extract_arch_findings
from sentinel.orchestrator.state_store import StateStore
from sentinel.skills.loader import Skills


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_store() -> MagicMock:
    store = MagicMock(spec=StateStore)
    store.read.side_effect = lambda name: {
        "repositories": [{"path": "/tmp/repo", "name": "test-repo"}],
        "modules": [
            {"name": "app.py", "path": "app.py", "files": ["app.py"]},
            {"name": "utils.py", "path": "utils.py", "files": ["utils.py"]},
        ],
        "dependencies": {"internal": [], "external": ["flask"]},
    }[name]
    return store


@pytest.fixture()
def mock_architect() -> MagicMock:
    return MagicMock(spec=ArchitectAgent)


# ---------------------------------------------------------------------------
# ArchChecker.run() tests
# ---------------------------------------------------------------------------

class TestArchCheckerRun:
    def test_calls_architect_with_correct_skill(
        self, mock_architect: MagicMock, mock_store: MagicMock
    ) -> None:
        mock_architect.specialist_review.return_value = {"findings": []}
        checker = ArchChecker(mock_architect, mock_store)

        checker.run()

        mock_architect.specialist_review.assert_called_once()
        skill_arg = mock_architect.specialist_review.call_args.args[0]
        assert skill_arg == Skills.ARCH_CHECKLIST

    def test_returns_normalized_findings(
        self, mock_architect: MagicMock, mock_store: MagicMock
    ) -> None:
        mock_architect.specialist_review.return_value = {
            "findings": [
                {
                    "category": "test_coverage",
                    "severity": "P1",
                    "description": "No tests for app.py",
                    "affected_modules": ["app.py"],
                    "suggested_fix": "Add test_app.py",
                },
                {
                    "category": "api_controls",
                    "severity": "P2",
                    "description": "No rate limiting on API",
                    "affected_modules": ["app.py", "routes.py"],
                    "suggested_fix": "Add rate limiting middleware",
                },
            ]
        }
        checker = ArchChecker(mock_architect, mock_store)

        findings = checker.run()

        assert len(findings) == 2
        # First finding
        assert findings[0]["module"] == "app.py"
        assert findings[0]["skill"] == Skills.ARCH_CHECKLIST
        assert findings[0]["severity"] == "P1"
        assert findings[0]["category"] == "test_coverage"
        assert findings[0]["location"] == "codebase-level"
        assert findings[0]["description"] == "No tests for app.py"
        assert findings[0]["suggested_fix"] == "Add test_app.py"
        assert findings[0]["status"] == "open"
        # Second finding — multiple affected modules joined
        assert findings[1]["module"] == "app.py, routes.py"
        assert findings[1]["severity"] == "P2"

    def test_findings_appended_to_state_store(
        self, mock_architect: MagicMock, mock_store: MagicMock
    ) -> None:
        mock_architect.specialist_review.return_value = {
            "findings": [
                {
                    "category": "observability",
                    "severity": "P3",
                    "description": "No structured logging",
                    "affected_modules": ["codebase"],
                    "suggested_fix": "Use structured logger",
                },
            ]
        }
        checker = ArchChecker(mock_architect, mock_store)

        findings = checker.run()

        assert mock_store.append_finding.call_count == 1
        appended = mock_store.append_finding.call_args.args[0]
        assert appended["category"] == "observability"
        assert appended["skill"] == Skills.ARCH_CHECKLIST

    def test_graceful_failure_on_exception(
        self, mock_architect: MagicMock, mock_store: MagicMock
    ) -> None:
        mock_architect.specialist_review.side_effect = AgentError(
            "architect", "network timeout"
        )
        checker = ArchChecker(mock_architect, mock_store)

        findings = checker.run()

        assert findings == []
        mock_store.append_finding.assert_not_called()

    def test_empty_findings_from_architect(
        self, mock_architect: MagicMock, mock_store: MagicMock
    ) -> None:
        mock_architect.specialist_review.return_value = {"findings": []}
        checker = ArchChecker(mock_architect, mock_store)

        findings = checker.run()

        assert findings == []
        mock_store.append_finding.assert_not_called()

    def test_reads_all_required_state(
        self, mock_architect: MagicMock, mock_store: MagicMock
    ) -> None:
        mock_architect.specialist_review.return_value = {"findings": []}
        checker = ArchChecker(mock_architect, mock_store)

        checker.run()

        read_calls = [c.args[0] for c in mock_store.read.call_args_list]
        assert "repositories" in read_calls
        assert "modules" in read_calls
        assert "dependencies" in read_calls


# ---------------------------------------------------------------------------
# _extract_arch_findings() tests
# ---------------------------------------------------------------------------

class TestExtractArchFindings:
    def test_normalizes_fields(self) -> None:
        result = {
            "findings": [
                {
                    "category": "cost_exposure",
                    "severity": "Critical",
                    "description": "Unbounded LLM calls",
                    "affected_modules": ["llm_client.py"],
                    "suggested_fix": "Add budget cap",
                }
            ]
        }

        findings = _extract_arch_findings(result)

        assert len(findings) == 1
        f = findings[0]
        assert f["module"] == "llm_client.py"
        assert f["skill"] == Skills.ARCH_CHECKLIST
        assert f["severity"] == "Critical"
        assert f["category"] == "cost_exposure"
        assert f["location"] == "codebase-level"

    def test_defaults_when_fields_missing(self) -> None:
        result = {"findings": [{"description": "something missing"}]}

        findings = _extract_arch_findings(result)

        assert len(findings) == 1
        f = findings[0]
        assert f["module"] == "codebase"
        assert f["severity"] == "P2"
        assert f["category"] == "architectural"
        assert f["suggested_fix"] == ""

    def test_non_list_findings_returns_empty(self) -> None:
        result = {"findings": "not a list"}

        findings = _extract_arch_findings(result)

        assert findings == []

    def test_missing_findings_key_returns_empty(self) -> None:
        result = {"other_key": "value"}

        findings = _extract_arch_findings(result)

        assert findings == []

    def test_non_dict_items_skipped(self) -> None:
        result = {"findings": ["not a dict", {"description": "valid"}]}

        findings = _extract_arch_findings(result)

        assert len(findings) == 1
        assert findings[0]["description"] == "valid"

    def test_multiple_affected_modules_joined(self) -> None:
        result = {
            "findings": [
                {
                    "description": "Missing tests",
                    "affected_modules": ["a.py", "b.py", "c.py"],
                }
            ]
        }

        findings = _extract_arch_findings(result)

        assert findings[0]["module"] == "a.py, b.py, c.py"
