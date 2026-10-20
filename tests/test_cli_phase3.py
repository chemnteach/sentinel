import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import click
import pytest
from click.testing import CliRunner
from rich.console import Console

from sentinel import cli_phase3


@pytest.fixture
def spec_revise_harness(monkeypatch):
    runner = CliRunner()
    main = click.Group()
    console = Console(record=True)

    store = MagicMock()
    store.read.return_value = [
        {"fer_id": "FER-123", "feature_summary": "existing spec"},
        {"fer_id": "FER-999", "feature_summary": "other spec"},
    ]

    fer_mgr = MagicMock()
    task_mgr = MagicMock()

    cfg = SimpleNamespace(state_dir="/tmp/state", repo_path="/tmp/repo")

    monkeypatch.setattr(cli_phase3, "load_config", lambda: cfg)

    import sentinel.orchestrator.state_store as state_store_module
    import sentinel.orchestrator.task_manager as task_manager_module
    import sentinel.phase3.fer_manager as fer_manager_module

    monkeypatch.setattr(state_store_module, "StateStore", lambda *args, **kwargs: store)
    monkeypatch.setattr(task_manager_module, "TaskManager", lambda *args, **kwargs: task_mgr)
    monkeypatch.setattr(fer_manager_module, "FERManager", lambda *args, **kwargs: fer_mgr)

    cli_phase3.register_phase3_commands(
        main,
        console,
        is_safe_id=lambda value: True,
        max_submit_json_bytes=1024,
        max_feedback_chars=500,
    )

    return SimpleNamespace(
        runner=runner,
        main=main,
        console=console,
        store=store,
        fer_mgr=fer_mgr,
        task_mgr=task_mgr,
    )


def test_spec_revise_exits_non_zero_when_fer_triage_raises(spec_revise_harness):
    """
    Acceptance criterion: If the atomic `fer_mgr.revise(...)` operation raises during `spec --revise`, the command exits with a non-zero outcome instead of reporting success.

    Scenario: Revise flow encounters an exception from the atomic revise operation.
    Given: A spec in revisable state and a mocked `fer_mgr.revise(...)` that raises an exception during `spec --revise`.
    When: The CLI command is executed with `spec --revise`.
    Then: The command returns a non-zero exit/result and does not report success.
    """
    spec_revise_harness.fer_mgr.revise.side_effect = RuntimeError("revise boom")

    result = spec_revise_harness.runner.invoke(
        spec_revise_harness.main,
        ["spec", "--revise", "FER-123", "--feedback", "needs changes"],
    )

    assert result.exit_code != 0
    assert "Spec cleared for FER-123" not in result.output



def test_spec_revise_does_not_emit_success_message_when_fer_triage_raises(spec_revise_harness):
    """
    Acceptance criterion: The success message `Spec cleared ... Feedback recorded` is not emitted when the atomic revise operation fails.

    Scenario: Revise flow fails inside the atomic revise operation after entering the revise path.
    Given: A mocked `fer_mgr.revise(...)` that raises during `spec --revise`.
    When: The command output is captured while running `spec --revise`.
    Then: The success message `Spec cleared ... Feedback recorded` is absent from output/logs.
    """
    spec_revise_harness.fer_mgr.revise.side_effect = RuntimeError("revise boom")

    result = spec_revise_harness.runner.invoke(
        spec_revise_harness.main,
        ["spec", "--revise", "FER-123", "--feedback", "needs changes"],
    )

    assert "Spec cleared for FER-123" not in result.output
    assert "Feedback recorded" not in result.output



def test_spec_revise_does_not_persist_cleared_spec_when_fer_triage_raises(spec_revise_harness):
    """
    Acceptance criterion: When the atomic revise operation fails during `spec --revise`, the system does not persist a state where the spec is cleared but the corresponding FER triage/state update did not complete.

    Scenario: Revise flow delegates to the atomic `fer_mgr.revise(...)`, which fails. The CLI no longer performs the spec-store mutation as an independent top-level step.
    Given: A persisted spec state before revise and a mocked `fer_mgr.revise(...)` that raises.
    When: The revise command is executed and the CLI's direct store interaction is inspected.
    Then: The CLI does not perform any top-level spec-store write of its own — persistence is owned by the atomic revise operation.
    """
    spec_revise_harness.fer_mgr.revise.side_effect = RuntimeError("revise boom")

    result = spec_revise_harness.runner.invoke(
        spec_revise_harness.main,
        ["spec", "--revise", "FER-123", "--feedback", "needs changes"],
    )

    assert result.exit_code != 0
    spec_revise_harness.store.write.assert_not_called()



def test_spec_revise_logs_failure_message_when_fer_triage_raises(spec_revise_harness, caplog):
    """
    Acceptance criterion: The exception path logs a failure message that identifies the revise operation as failed rather than downgraded to a warning-only success path.

    Scenario: Revise flow catches an exception from the atomic revise operation in the failure path.
    Given: A mocked `fer_mgr.revise(...)` that raises and log capture enabled.
    When: The revise command is run.
    Then: Captured logs/output contain a failure message identifying the revise operation as failed, not merely a warning followed by success.
    """
    spec_revise_harness.fer_mgr.revise.side_effect = RuntimeError("revise boom")

    with caplog.at_level(logging.ERROR):
        result = spec_revise_harness.runner.invoke(
            spec_revise_harness.main,
            ["spec", "--revise", "FER-123", "--feedback", "needs changes"],
        )

    assert result.exit_code != 0
    assert "Spec revise failed for FER FER-123: revise boom" in caplog.text
    assert "warning" not in caplog.text.lower()



def test_spec_revise_success_path_still_reports_success_when_fer_triage_succeeds(spec_revise_harness):
    """
    Acceptance criterion: Automated tests cover the triage-exception path and verify that no silent partial success remains.

    Scenario: Negative/regression case proving only the exception path changes.
    Given: A mocked `fer_mgr.revise(...)` that succeeds during `spec --revise`.
    When: The revise command is executed normally.
    Then: The command succeeds and emits the normal success message, confirming the failure handling does not break the happy path.
    """
    result = spec_revise_harness.runner.invoke(
        spec_revise_harness.main,
        ["spec", "--revise", "FER-123", "--feedback", "needs changes"],
    )

    assert result.exit_code == 0
    assert "Spec cleared for FER-123" in result.output
    assert "Feedback recorded" in result.output
    # The CLI delegates the FER transition + spec mutation to a single atomic
    # manager method, rather than orchestrating triage and store.write itself.
    spec_revise_harness.fer_mgr.revise.assert_called_once_with(
        "FER-123",
        "needs changes",
    )
    spec_revise_harness.store.write.assert_not_called()
