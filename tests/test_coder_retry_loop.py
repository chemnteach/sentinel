"""Deterministic integration tests for the Coder inner test-fix retry loop.

`CoderAgent.implement()` runs up to `_MAX_IMPL_ATTEMPTS` passes: on a failing
test run it feeds the failure output back into the model and re-implements,
rolling back to clean source between attempts. These tests drive the loop
directly with a mocked `run_json` (the model) and `run_tests` (the subprocess),
while letting the real `apply_edits` / `write_test_file` operate on a temp file.

`rollback_edits` is mocked to a no-op so the retry chain is deterministic
regardless of whether the temp dir is a git repo; each attempt's edit therefore
targets the prior attempt's on-disk result.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from sentinel.agents.base import AgentResponseError
from sentinel.agents.coder import CoderAgent


def _impl(old: str, new: str, summary: str = "s") -> dict:
    """A model implementation response that edits mod.py and writes a test."""
    return {
        "edits": [{"file": "mod.py", "old_text": old, "new_text": new}],
        "test_file": "tests/test_mod.py",
        "test_content": "def test_x():\n    assert True\n",
        "diff_summary": summary,
    }


def _make_agent(tmp_path, run_json_results: list[dict]) -> CoderAgent:
    """Construct a CoderAgent without running __init__ (no real client/loader)."""
    agent = CoderAgent.__new__(CoderAgent)
    agent._config = SimpleNamespace(
        repo_path=str(tmp_path),
        test_command=None,
        test_timeout=300,
        local_only_mode=False,
    )
    agent.run_json = MagicMock(side_effect=run_json_results)
    return agent


def test_retry_loop_recovers_on_second_attempt(tmp_path, monkeypatch):
    (tmp_path / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    # attempt 1 edit fails tests; attempt 2 edit (targets attempt-1 output,
    # since rollback is a no-op here) passes.
    agent = _make_agent(
        tmp_path,
        [_impl("VALUE = 1", "VALUE = 2", "a1"), _impl("VALUE = 2", "VALUE = 3", "a2")],
    )
    run_tests_mock = MagicMock(side_effect=[
        {"passed": False, "returncode": 1,
         "output": "AssertionError: expected 1 got 2", "summary": "1 failed"},
        {"passed": True, "returncode": 0,
         "output": "1 passed", "summary": "1 passed"},
    ])
    monkeypatch.setattr(
        "sentinel.workflow.remediation.test_runner.run_tests", run_tests_mock
    )
    monkeypatch.setattr(
        "sentinel.workflow.remediation.apply.rollback_edits", MagicMock()
    )

    result = agent.implement("CONTEXT")

    # 1. run_json called twice (attempt 1 + one retry)
    assert agent.run_json.call_count == 2
    # 2 & 3. the retry prompt carries the failure-feedback block + the output
    second_msg = agent.run_json.call_args_list[1].kwargs["user_message"]
    assert "Previous Implementation Failed" in second_msg
    assert "AssertionError: expected 1 got 2" in second_msg
    # 4 & 5. recovered: non-empty diff, tests pass
    assert result["files_changed"]
    assert result["tests_pass"] is True


def test_retry_loop_gives_up_after_three_attempts(tmp_path, monkeypatch):
    (tmp_path / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    # three chained edits that all apply but always fail tests
    agent = _make_agent(
        tmp_path,
        [
            _impl("VALUE = 1", "VALUE = 2"),
            _impl("VALUE = 2", "VALUE = 3"),
            _impl("VALUE = 3", "VALUE = 4"),
        ],
    )
    run_tests_mock = MagicMock(return_value={
        "passed": False, "returncode": 1,
        "output": "AssertionError: still wrong", "summary": "1 failed",
    })
    monkeypatch.setattr(
        "sentinel.workflow.remediation.test_runner.run_tests", run_tests_mock
    )
    monkeypatch.setattr(
        "sentinel.workflow.remediation.apply.rollback_edits", MagicMock()
    )

    result = agent.implement("CONTEXT")

    # 1. run_json called three times (attempt 1 + two retries)
    assert agent.run_json.call_count == 3
    # 2 & 3. exhausted: empty diff, tests did not pass
    assert result["files_changed"] == []
    assert result["tests_pass"] is False


def test_retry_loop_returns_structured_failure_when_retry_model_call_fails(
    tmp_path,
    monkeypatch,
):
    (tmp_path / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    agent = _make_agent(
        tmp_path,
        [
            _impl("VALUE = 1", "VALUE = 2", "a1"),
            AgentResponseError("retry model exploded"),
        ],
    )
    run_tests_mock = MagicMock(return_value={
        "passed": False,
        "returncode": 1,
        "output": "AssertionError: expected 1 got 2",
        "summary": "1 failed",
    })
    monkeypatch.setattr(
        "sentinel.workflow.remediation.test_runner.run_tests",
        run_tests_mock,
    )
    monkeypatch.setattr(
        "sentinel.workflow.remediation.apply.rollback_edits",
        MagicMock(),
    )

    result = agent.implement("CONTEXT")

    assert result["tests_pass"] is False
    assert "model error" in result["test_results"]
    assert isinstance(result["test_results"], str)
    assert isinstance(result["test_summary"], str)
    assert run_tests_mock.call_count == 1


def test_retry_loop_returns_structured_failure_when_retry_edit_apply_fails(
    tmp_path,
    monkeypatch,
):
    (tmp_path / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    # Attempt 2 intentionally uses the wrong old_text so apply_edits fails
    # before tests execute on the retry path.
    agent = _make_agent(
        tmp_path,
        [
            _impl("VALUE = 1", "VALUE = 2", "a1"),
            _impl("VALUE = 999", "VALUE = 3", "a2"),
        ],
    )
    run_tests_mock = MagicMock(return_value={
        "passed": False,
        "returncode": 1,
        "output": "AssertionError: expected 1 got 2",
        "summary": "1 failed",
    })
    monkeypatch.setattr(
        "sentinel.workflow.remediation.test_runner.run_tests",
        run_tests_mock,
    )
    monkeypatch.setattr(
        "sentinel.workflow.remediation.apply.rollback_edits",
        MagicMock(),
    )

    result = agent.implement("CONTEXT")

    assert result["tests_pass"] is False
    assert "edit failed" in result["test_results"]
    assert isinstance(result["test_results"], str)
    assert isinstance(result["test_summary"], str)
    assert run_tests_mock.call_count == 1
