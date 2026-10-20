"""Coder protected-boundary and error-signal tests (P1 error_signal_loss).

Covers:
  * ``implement`` rolls back applied code edits when the subsequent
    ``write_test_file`` fails, instead of leaving a partial working tree.
  * ``self_review`` flags a total review failure via ``quick_review_failed``
    rather than returning an indistinguishable empty findings list.
  * ``build_position_statement`` returns an empty-but-valid statement with an
    ``error`` field on a model failure instead of raising.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sentinel.agents.base import AgentResponseError
from sentinel.agents.coder import CoderAgent


def _make_agent(tmp_path, run_json_result: dict) -> CoderAgent:
    agent = CoderAgent.__new__(CoderAgent)
    agent._config = SimpleNamespace(
        repo_path=str(tmp_path),
        test_command=None,
        test_timeout=300,
        local_only_mode=False,
    )
    agent.run_json = MagicMock(return_value=run_json_result)
    return agent


def test_write_test_file_failure_rolls_back_edits(tmp_path, monkeypatch):
    (tmp_path / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    impl = {
        "edits": [{"file": "mod.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"}],
        "test_file": "tests/test_mod.py",
        "test_content": "def test_x():\n    assert True\n",
        "diff_summary": "s",
    }
    agent = _make_agent(tmp_path, impl)

    rollback_mock = MagicMock()
    monkeypatch.setattr(
        "sentinel.workflow.remediation.apply.rollback_edits", rollback_mock
    )

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(
        "sentinel.workflow.remediation.apply.write_test_file", _boom
    )
    # run_tests should never be reached — the test write fails first.
    monkeypatch.setattr(
        "sentinel.workflow.remediation.test_runner.run_tests",
        MagicMock(side_effect=AssertionError("run_tests must not run")),
    )

    with pytest.raises(RuntimeError, match="test file write failed"):
        agent.implement("CONTEXT")

    # The applied code edit was rolled back before the error surfaced.
    rollback_mock.assert_called_once()
    rolled_back_files = rollback_mock.call_args.args[0]
    assert "mod.py" in rolled_back_files


def _review_agent(json_side_effect, run_side_effect=None) -> CoderAgent:
    agent = CoderAgent.__new__(CoderAgent)
    agent._config = SimpleNamespace(local_only_mode=False)
    agent.run_json = MagicMock(side_effect=json_side_effect)
    if run_side_effect is not None:
        agent.run = MagicMock(side_effect=run_side_effect)
    return agent


def test_self_review_flags_total_failure():
    # Both the JSON review and the plain-text fallback fail.
    agent = _review_agent(
        json_side_effect=AgentResponseError("json boom"),
        run_side_effect=AgentResponseError("text boom"),
    )
    result = agent.self_review("## Diff\n\n...")
    assert result["findings"] == []
    assert result["quick_review_failed"] is True
    assert "text boom" in result["error"]


def test_self_review_text_fallback_is_not_flagged_failed():
    # JSON fails but the plain-text fallback succeeds — degraded, not a failure.
    agent = _review_agent(
        json_side_effect=AgentResponseError("json boom"),
        run_side_effect=[SimpleNamespace(content="looks fine")],
    )
    result = agent.self_review("## Diff\n\n...")
    assert result["findings"] == []
    assert result["summary"] == "looks fine"
    assert "quick_review_failed" not in result


def test_build_position_statement_returns_error_dict_on_failure():
    agent = CoderAgent.__new__(CoderAgent)
    agent._config = SimpleNamespace(local_only_mode=False)
    agent.run_json = MagicMock(side_effect=AgentResponseError("model down"))

    result = agent.build_position_statement("dispute ctx")
    assert "model down" in result["error"]
    # Empty-but-valid schema fields so callers can read them safely.
    for key in (
        "issue",
        "approach",
        "evidence",
        "impact_assessment",
        "risk_of_alternative",
        "proposed_compromise",
    ):
        assert result[key] == ""
