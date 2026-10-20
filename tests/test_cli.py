from __future__ import annotations

import json

from click.testing import CliRunner
import pytest

from sentinel.cli import main
import sentinel.config as config_module


@pytest.fixture(autouse=True)
def _reset_global_config_singleton() -> None:
    config_module._config = None
    yield
    config_module._config = None


def _seed_tech_debt(repo_root: str) -> str:
    state_dir = f"{repo_root}/sentinel/state"
    import os

    os.makedirs(state_dir, exist_ok=True)
    with open(f"{state_dir}/tech_debt.json", "w", encoding="utf-8") as f:
        json.dump(
            [
                {
                    "id": "TD-0001",
                    "severity": "P1",
                    "description": "demo item",
                    "human_triage": None,
                }
            ],
            f,
            indent=2,
        )
    return state_dir


def test_triage_rejects_invalid_tech_debt_id(tmp_path) -> None:
    runner = CliRunner()
    repo_root = str(tmp_path)
    state_dir = _seed_tech_debt(repo_root)

    result = runner.invoke(
        main,
        ["triage", "--approve", "../../etc/passwd"],
        env={"SENTINEL_REPO_PATH": repo_root, "SENTINEL_STATE_DIR": state_dir},
    )

    assert result.exit_code == 1
    assert "Invalid tech debt ID format" in result.output

    tech_debt_file = tmp_path / "sentinel" / "state" / "tech_debt.json"
    data = json.loads(tech_debt_file.read_text(encoding="utf-8"))
    assert data[0]["human_triage"] is None


def test_triage_accepts_valid_tech_debt_id(tmp_path) -> None:
    runner = CliRunner()
    repo_root = str(tmp_path)
    state_dir = _seed_tech_debt(repo_root)

    result = runner.invoke(
        main,
        ["triage", "--approve", "TD-0001"],
        env={"SENTINEL_REPO_PATH": repo_root, "SENTINEL_STATE_DIR": state_dir},
    )

    assert result.exit_code == 0
    assert "Item TD-0001: APPROVE" in result.output

    tech_debt_file = tmp_path / "sentinel" / "state" / "tech_debt.json"
    data = json.loads(tech_debt_file.read_text(encoding="utf-8"))
    assert data[0]["human_triage"] == "APPROVE"


def test_task_rejects_invalid_task_id() -> None:
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["task", "--approve", "../../etc/passwd"],
    )

    assert result.exit_code == 1
    assert "Invalid TASK_ID format" in result.output


def test_task_list_works_without_task_id_validation(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path))
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["task", "--list"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 0
    assert "No tasks yet." in result.output


def test_plan_show_multi_item_does_not_raise_and_prints_titles(monkeypatch, tmp_path) -> None:
    """`sentinel plan --show` on a large multi-item plan must not raise
    JSONDecodeError (the old [:8000] slice cut JSON mid-string) and must print
    each item's full title."""
    long_desc = "x" * 900  # push total JSON well past 8000 chars
    items = [
        {
            "id": f"FIX-REQUIRED-{i:03d}",
            "title": f"Remediation item {i} alpha-bravo-{i}",
            "affected_files": [f"sentinel/mod_{i}/file_{i}.py", f"tests/test_mod_{i}.py"],
            "description": long_desc,
            "acceptance_criteria": [f"criterion {i}"],
            "source_findings": [f"F2-{i:012d}", f"F2-{i + 100:012d}"],
        }
        for i in range(1, 11)
    ]
    plan = {
        "plan_version": "1",
        "summary": {"required": 10},
        "blocking": [],
        "required": items,
        "desired": [],
        "nice_to_have": [],
        "deferred": [],
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path))

    result = CliRunner().invoke(
        main, ["plan", "--show"], env={"SENTINEL_REPO_PATH": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "Plan: 10 items" in result.output
    assert "Remediation item 1 alpha-bravo-1" in result.output
    assert "Remediation item 10 alpha-bravo-10" in result.output


def test_dispute_rejects_invalid_task_id() -> None:
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["dispute", "--resolve", "../../etc/passwd", "--winner", "architect"],
    )

    assert result.exit_code == 1
    assert "Invalid TASK_ID format" in result.output


def test_dispute_valid_task_id_keeps_existing_flow(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["dispute", "--resolve", "task-123", "--winner", "architect"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 0
    assert "No disputes on record." in result.output


def test_fer_rejects_invalid_fer_id(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["fer", "--show", "../../bad-id"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 1
    assert "Invalid FER_ID format" in result.output


def test_fer_submit_requires_json_extension(tmp_path) -> None:
    runner = CliRunner()
    submit_file = tmp_path / "request.txt"
    submit_file.write_text("{}", encoding="utf-8")

    result = runner.invoke(
        main,
        ["fer", "--submit", str(submit_file)],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 1
    assert "requires a .json file" in result.output


def test_spec_revise_rejects_overlong_feedback(tmp_path) -> None:
    runner = CliRunner()
    long_feedback = "x" * 1001

    result = runner.invoke(
        main,
        ["spec", "--revise", "FER-123", "--feedback", long_feedback],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 1
    assert "--feedback too long" in result.output


def test_govern_rejects_invalid_task_id() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["govern", "--start", "--task", "../../bad"],
    )

    assert result.exit_code == 1
    assert "Invalid TASK_ID format" in result.output


def test_task_approve_denied_when_not_designated_approver(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["task", "--approve", "task-123"],
        env={
            "SENTINEL_REPO_PATH": str(tmp_path),
            "SENTINEL_ENFORCE_SINGLE_PR_APPROVER": "true",
            "SENTINEL_REQUIRED_PR_APPROVER": "owner-user",
            "SENTINEL_OPERATOR": "team-user",
        },
    )

    assert result.exit_code == 1
    assert "Not authorized to approve/merge PR" in result.output


def test_govern_self_approve_blocked_by_single_approver_policy(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["govern", "--start", "--self-approve"],
        env={
            "SENTINEL_REPO_PATH": str(tmp_path),
            "SENTINEL_ENFORCE_SINGLE_PR_APPROVER": "true",
            "SENTINEL_REQUIRED_PR_APPROVER": "owner-user",
        },
    )

    assert result.exit_code == 1
    assert "Self-approve mode is disabled under single-approver policy" in result.output


# ---------------------------------------------------------------------------
# Blocking-wave hardening: safe state loads, atomic writes, serve host guard,
# lock permission classification (FIX-BLOCKING-031/035/037/052/008/069)
# ---------------------------------------------------------------------------


def test_serve_rejects_non_loopback_host_without_override(tmp_path) -> None:
    result = CliRunner().invoke(
        main,
        ["serve", "--state-dir", str(tmp_path), "--host", "0.0.0.0"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )
    assert result.exit_code == 1
    assert "non-loopback" in result.output.lower()


def test_plan_approve_reports_corrupted_plan_json(monkeypatch, tmp_path) -> None:
    (tmp_path / "plan.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path))

    result = CliRunner().invoke(
        main, ["plan", "--approve"], env={"SENTINEL_REPO_PATH": str(tmp_path)}
    )

    assert result.exit_code == 1
    assert "corrupted" in result.output.lower()
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_plan_approve_writes_atomically(monkeypatch, tmp_path) -> None:
    plan = {"plan_version": "1", "blocking": [], "required": []}
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path))

    result = CliRunner().invoke(
        main, ["plan", "--approve"], env={"SENTINEL_REPO_PATH": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert data["human_approved"] is True
    # No leftover temp files from the atomic write.
    assert not list(tmp_path.glob(".plan.json.*.tmp"))


def test_task_list_reports_corrupted_tasks_json(monkeypatch, tmp_path) -> None:
    (tmp_path / "tasks.json").write_text("{bad json", encoding="utf-8")
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path))

    result = CliRunner().invoke(
        main, ["task", "--list"], env={"SENTINEL_REPO_PATH": str(tmp_path)}
    )

    assert result.exit_code == 1
    assert "corrupted" in result.output.lower()


def test_dispute_reports_corrupted_disputes_json(monkeypatch, tmp_path) -> None:
    (tmp_path / "disputes.json").write_text("[not json", encoding="utf-8")
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path))

    result = CliRunner().invoke(
        main,
        ["dispute", "--resolve", "task-123", "--winner", "architect"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 1
    assert "corrupted" in result.output.lower()


def test_dispute_resolve_persists_atomically(monkeypatch, tmp_path) -> None:
    disputes = [{"task_id": "task-123", "escalated_to_human": False}]
    (tmp_path / "disputes.json").write_text(json.dumps(disputes), encoding="utf-8")
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path))

    result = CliRunner().invoke(
        main,
        ["dispute", "--resolve", "task-123", "--winner", "architect"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "disputes.json").read_text(encoding="utf-8"))
    assert data[0]["human_resolution"] == "architect"
    assert data[0]["escalated_to_human"] is True
    assert not list(tmp_path.glob(".disputes.json.*.tmp"))


def test_dispute_resolve_missing_task_exits_without_partial_write(
    monkeypatch, tmp_path
) -> None:
    disputes = [{"task_id": "other", "escalated_to_human": False}]
    original = json.dumps(disputes)
    (tmp_path / "disputes.json").write_text(original, encoding="utf-8")
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path))

    result = CliRunner().invoke(
        main,
        ["dispute", "--resolve", "task-123", "--winner", "architect"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 1
    assert "not found" in result.output.lower()
    # disputes.json is untouched (no partial write) and still valid JSON.
    assert json.loads((tmp_path / "disputes.json").read_text(encoding="utf-8")) == disputes


def test_status_reports_corrupted_state_json(monkeypatch, tmp_path) -> None:
    (tmp_path / "findings.json").write_text("{bad", encoding="utf-8")
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path))

    result = CliRunner().invoke(
        main, ["status"], env={"SENTINEL_REPO_PATH": str(tmp_path)}
    )

    assert result.exit_code == 1
    assert "corrupted" in result.output.lower()


def test_acquire_tech_debt_lock_surfaces_permission_error(tmp_path, monkeypatch) -> None:
    """A PermissionError on lock creation is surfaced immediately as an access
    error, not retried until timeout behind a conflict message."""
    from sentinel import cli as cli_module

    def _boom(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(cli_module.os, "open", _boom)

    with pytest.raises(cli_module.TechDebtStateError):
        cli_module._acquire_tech_debt_lock(tmp_path / "x.lock")


# ---------------------------------------------------------------------------
# TestRegistryApproveCli (cycle 6 commit 4)
# ---------------------------------------------------------------------------


def _seed_registry_task(
    repo_root: str,
    *,
    task_id: str = "T-REG-001",
    plan_id: str = "FIX-BLOCKING-001",
    state: str = "REVIEW_PASS",
) -> str:
    """Seed a tasks.json with one task at the given state."""
    import os
    state_dir = f"{repo_root}/sentinel/state"
    os.makedirs(state_dir, exist_ok=True)
    task = {
        "task_id": task_id,
        "plan_id": plan_id,
        "state": state,
        "description": "Test registry task",
        "acceptance_criteria": [],
        "affected_files": [],
        "scope_boundary": "",
        "required_skills": [],
        "test_stubs": None,
        "test_stubs_approved": False,
        "todo_approved": True,
        "doc_updated": False,
        "assigned_agent": None,
        "pr_url": None,
        "pr_number": None,
        "last_human_review_comment": "",
        "latest_diff_summary": "",
        "is_phase3": False,
        "source_fer_id": "",
        "compat_check_passed": False,
        "pattern_id": "",
        "verification_required": [],
    }
    with open(f"{state_dir}/tasks.json", "w", encoding="utf-8") as f:
        json.dump([task], f, indent=2)
    with open(f"{state_dir}/audit_log.json", "w", encoding="utf-8") as f:
        json.dump([], f)
    return state_dir


class TestRegistryApproveCli:
    def test_registry_approve_advances_review_pass_to_qa_pass(
        self, tmp_path, monkeypatch
    ) -> None:
        from unittest.mock import patch, MagicMock

        repo_root = str(tmp_path)
        state_dir = _seed_registry_task(repo_root)

        monkeypatch.setenv("SENTINEL_REPO_PATH", repo_root)
        monkeypatch.setenv("SENTINEL_STATE_DIR", state_dir)

        runner = CliRunner()
        with patch(
            "sentinel.orchestrator.state_store.StateStore.commit_state"
        ), patch(
            "sentinel.api.client.create_model_client"
        ) as mock_client:
            mock_client.return_value = MagicMock()
            result = runner.invoke(
                main,
                [
                    "task",
                    "--registry-approve",
                    "T-REG-001",
                    "--comment",
                    "Verified",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "registry-approved" in result.output

        tasks = json.loads(
            (tmp_path / "sentinel" / "state" / "tasks.json").read_text(
                encoding="utf-8"
            )
        )
        assert tasks[0]["state"] == "QA_PASS"

        audit = json.loads(
            (tmp_path / "sentinel" / "state" / "audit_log.json").read_text(
                encoding="utf-8"
            )
        )
        verdict_entries = [
            e for e in audit if e.get("action") == "qa_verdict"
        ]
        assert any(
            e.get("agent") == "human_registry_reviewer"
            for e in verdict_entries
        )

    def test_registry_approve_nonexistent_task_exits_1_with_friendly_error(
        self, tmp_path, monkeypatch
    ) -> None:
        from unittest.mock import patch, MagicMock

        repo_root = str(tmp_path)
        state_dir = _seed_registry_task(repo_root)

        monkeypatch.setenv("SENTINEL_REPO_PATH", repo_root)
        monkeypatch.setenv("SENTINEL_STATE_DIR", state_dir)

        runner = CliRunner()
        with patch(
            "sentinel.orchestrator.state_store.StateStore.commit_state"
        ), patch(
            "sentinel.api.client.create_model_client"
        ) as mock_client:
            mock_client.return_value = MagicMock()
            result = runner.invoke(
                main, ["task", "--registry-approve", "T-DOES-NOT-EXIST"]
            )

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_registry_approve_wrong_state_exits_1_with_friendly_error(
        self, tmp_path, monkeypatch
    ) -> None:
        """Idempotency check: re-running on an already-QA_PASS task."""
        from unittest.mock import patch, MagicMock

        repo_root = str(tmp_path)
        state_dir = _seed_registry_task(repo_root, state="QA_PASS")

        monkeypatch.setenv("SENTINEL_REPO_PATH", repo_root)
        monkeypatch.setenv("SENTINEL_STATE_DIR", state_dir)

        runner = CliRunner()
        with patch(
            "sentinel.orchestrator.state_store.StateStore.commit_state"
        ), patch(
            "sentinel.api.client.create_model_client"
        ) as mock_client:
            mock_client.return_value = MagicMock()
            result = runner.invoke(
                main, ["task", "--registry-approve", "T-REG-001"]
            )

        assert result.exit_code == 1
        assert "expected REVIEW_PASS" in result.output


# ---------------------------------------------------------------------------
# triage --batch (Decision Brief export -> tech-debt register)
# ---------------------------------------------------------------------------

def _seed_batch_tech_debt(tmp_path) -> str:
    """Seed a multi-item tech-debt register; return the state dir path."""
    import os

    state_dir = f"{tmp_path}/state"
    os.makedirs(state_dir, exist_ok=True)
    with open(f"{state_dir}/tech_debt.json", "w", encoding="utf-8") as f:
        json.dump(
            [
                {"id": "TD-0001", "severity": "P1", "description": "a", "human_triage": None},
                {"id": "TD-0002", "severity": "P2", "description": "b", "human_triage": None},
                {"id": "TD-0003", "severity": "P2", "description": "c", "human_triage": None},
            ],
            f,
            indent=2,
        )
    return state_dir


def _write_decisions(tmp_path, decisions) -> str:
    path = f"{tmp_path}/decisions.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(decisions, f, indent=2)
    return path


def test_batch_triage_applies_approve(tmp_path) -> None:
    runner = CliRunner()
    state_dir = _seed_batch_tech_debt(tmp_path)
    decisions = _write_decisions(
        tmp_path,
        [{"td_id": "TD-0001", "decision": "APPROVE", "conditions": None, "rationale": None}],
    )

    result = runner.invoke(main, ["triage", "--batch", decisions, "--state-dir", state_dir])

    assert result.exit_code == 0
    assert "1 approved" in result.output
    data = json.loads((tmp_path / "state" / "tech_debt.json").read_text(encoding="utf-8"))
    assert data[0]["human_triage"] == "APPROVE"


def test_batch_triage_applies_defer_with_rationale(tmp_path) -> None:
    runner = CliRunner()
    state_dir = _seed_batch_tech_debt(tmp_path)
    decisions = _write_decisions(
        tmp_path,
        [{"td_id": "TD-0002", "decision": "DEFER", "conditions": None,
          "rationale": "Address in Q3 refactor"}],
    )

    result = runner.invoke(main, ["triage", "--batch", decisions, "--state-dir", state_dir])

    assert result.exit_code == 0
    assert "1 deferred" in result.output
    data = json.loads((tmp_path / "state" / "tech_debt.json").read_text(encoding="utf-8"))
    item = next(i for i in data if i["id"] == "TD-0002")
    assert item["human_triage"] == "DEFER"
    assert item["rationale"] == "Address in Q3 refactor"


def test_batch_triage_applies_approve_with_conditions(tmp_path) -> None:
    runner = CliRunner()
    state_dir = _seed_batch_tech_debt(tmp_path)
    # Brief exports "approve with conditions" as APPROVE + a non-null conditions field.
    decisions = _write_decisions(
        tmp_path,
        [{"td_id": "TD-0003", "decision": "APPROVE",
          "conditions": "Fix internal impl only, do not change public API", "rationale": None}],
    )

    result = runner.invoke(main, ["triage", "--batch", decisions, "--state-dir", state_dir])

    assert result.exit_code == 0
    assert "1 approved" in result.output
    data = json.loads((tmp_path / "state" / "tech_debt.json").read_text(encoding="utf-8"))
    item = next(i for i in data if i["id"] == "TD-0003")
    assert item["human_triage"] == "APPROVE"
    assert item["conditions"] == "Fix internal impl only, do not change public API"


def test_batch_triage_unknown_id_exits_nonzero(tmp_path) -> None:
    runner = CliRunner()
    state_dir = _seed_batch_tech_debt(tmp_path)
    decisions = _write_decisions(
        tmp_path,
        [
            {"td_id": "TD-0001", "decision": "APPROVE", "conditions": None, "rationale": None},
            {"td_id": "TD-9999", "decision": "DEFER", "conditions": None, "rationale": None},
        ],
    )

    result = runner.invoke(main, ["triage", "--batch", decisions, "--state-dir", state_dir])

    assert result.exit_code == 1
    assert "not found" in result.output
    # Atomic: nothing written, including the valid TD-0001 entry.
    data = json.loads((tmp_path / "state" / "tech_debt.json").read_text(encoding="utf-8"))
    assert all(i["human_triage"] is None for i in data)


def test_batch_triage_writes_updated_tech_debt(tmp_path) -> None:
    runner = CliRunner()
    state_dir = _seed_batch_tech_debt(tmp_path)
    decisions = _write_decisions(
        tmp_path,
        [
            {"td_id": "TD-0001", "decision": "APPROVE", "conditions": None, "rationale": None},
            {"td_id": "TD-0002", "decision": "DEFER", "conditions": None, "rationale": "later"},
            {"td_id": "TD-0003", "decision": "EXCLUDE", "conditions": None, "rationale": "false positive"},
        ],
    )

    result = runner.invoke(main, ["triage", "--batch", decisions, "--state-dir", state_dir])

    assert result.exit_code == 0
    assert "Applied 3 decisions" in result.output
    assert "1 approved, 1 deferred, 1 excluded" in result.output
    data = json.loads((tmp_path / "state" / "tech_debt.json").read_text(encoding="utf-8"))
    by_id = {i["id"]: i for i in data}
    assert by_id["TD-0001"]["human_triage"] == "APPROVE"
    assert by_id["TD-0002"]["human_triage"] == "DEFER"
    assert by_id["TD-0003"]["human_triage"] == "EXCLUDE"
    assert by_id["TD-0003"]["rationale"] == "false positive"


class TestSingleApproverGate:
    def test_none_required_approver_exits_cleanly_not_attribute_error(self) -> None:
        # A config whose required_pr_approver is None (enforcement on but not
        # configured) must produce a clean SystemExit(1), not an AttributeError
        # from calling .strip() on None.
        from types import SimpleNamespace
        from sentinel.cli import _enforce_single_approver_for_pr_actions

        cfg = SimpleNamespace(
            enforce_single_pr_approver=True,
            required_pr_approver=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            _enforce_single_approver_for_pr_actions(cfg, "merge PR")
        assert exc_info.value.code == 1

    def test_disabled_enforcement_is_noop(self) -> None:
        from types import SimpleNamespace
        from sentinel.cli import _enforce_single_approver_for_pr_actions

        cfg = SimpleNamespace(
            enforce_single_pr_approver=False,
            required_pr_approver=None,
        )
        # Must return without raising.
        _enforce_single_approver_for_pr_actions(cfg, "merge PR")



# ---------------------------------------------------------------------------
# P1 error_signal_loss / contract_violation hardening (cli.py)
# ---------------------------------------------------------------------------


def test_graphify_extract_failure_exits_nonzero(monkeypatch, tmp_path) -> None:
    """A non-zero graphify extract must abort with exit 1 and NOT print the
    success 'Graph written to' line."""
    from types import SimpleNamespace
    from sentinel import cli as cli_module

    def _fake_run(cmd, *args, **kwargs):
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(cli_module.subprocess, "run", _fake_run)

    result = CliRunner().invoke(
        main,
        ["graphify", "--repo-path", str(tmp_path), "--out-dir", str(tmp_path / "g")],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 1
    assert "extract failed" in result.output.lower()
    assert "Graph written to" not in result.output


def test_graphify_cluster_failure_exits_nonzero(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace
    from sentinel import cli as cli_module

    calls = {"n": 0}

    def _fake_run(cmd, *args, **kwargs):
        calls["n"] += 1
        # extract succeeds, cluster-only fails
        return SimpleNamespace(returncode=0 if calls["n"] == 1 else 1)

    monkeypatch.setattr(cli_module.subprocess, "run", _fake_run)

    result = CliRunner().invoke(
        main,
        ["graphify", "--repo-path", str(tmp_path), "--out-dir", str(tmp_path / "g")],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 1
    assert "cluster-only failed" in result.output.lower()
    assert "Graph written to" not in result.output


def test_graphify_success_reports_written(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace
    from sentinel import cli as cli_module

    out = tmp_path / "g"

    # graphify is mocked; emulate its output so the post-process step has a
    # graph.html to make offline. --no-name avoids any model client.
    def _fake_run(*a, **k):
        out.mkdir(parents=True, exist_ok=True)
        (out / "graph.html").write_text("<html></html>", encoding="utf-8")
        (out / "graph.json").write_text('{"nodes": [], "links": []}', encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli_module.subprocess, "run", _fake_run)

    result = CliRunner().invoke(
        main,
        ["graphify", "--repo-path", str(tmp_path), "--out-dir", str(out), "--no-name"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    assert "Graph written to" in result.output
    # Post-process vendored vis-network.min.js for offline use.
    assert (out / "vis-network.min.js").exists()


def test_graphify_default_excludes_yml_alongside_yaml(monkeypatch, tmp_path) -> None:
    """*.yml must be excluded by default (graphify sniffs YAML as a doc needing an
    LLM backend). The defaults exclude *.yaml but historically missed *.yml, which
    hard-failed code-only runs — regression guard for that gap."""
    from types import SimpleNamespace
    from sentinel import cli as cli_module

    captured: list[list[str]] = []
    out = tmp_path / "g"

    def _fake_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        out.mkdir(parents=True, exist_ok=True)
        (out / "graph.html").write_text("<html></html>", encoding="utf-8")
        (out / "graph.json").write_text('{"nodes": [], "links": []}', encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli_module.subprocess, "run", _fake_run)

    result = CliRunner().invoke(
        main,
        ["graphify", "--repo-path", str(tmp_path), "--out-dir", str(out), "--no-name"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    # The extract command is the first subprocess invocation.
    extract_cmd = captured[0]
    # Every default exclude pattern is passed as a "--exclude <pattern>" pair.
    def _excludes(cmd: list[str]) -> set[str]:
        return {cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--exclude"}

    ex = _excludes(extract_cmd)
    assert "*.yml" in ex, f"*.yml not excluded; excludes were {sorted(ex)}"
    assert "*.yaml" in ex  # still present alongside


def test_govern_reports_corrupted_plan_json(monkeypatch, tmp_path) -> None:
    """govern --start on a corrupt plan.json exits 1 with a clean error, not a
    raw JSONDecodeError traceback."""
    (tmp_path / "plan.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path))

    result = CliRunner().invoke(
        main, ["govern", "--start"], env={"SENTINEL_REPO_PATH": str(tmp_path)}
    )

    assert result.exit_code == 1
    assert "corrupted" in result.output.lower()
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_govern_missing_plan_reports_not_approved(monkeypatch, tmp_path) -> None:
    """No plan.json => the prior 'not approved' path still holds (empty dict)."""
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path))

    result = CliRunner().invoke(
        main, ["govern", "--start"], env={"SENTINEL_REPO_PATH": str(tmp_path)}
    )

    assert result.exit_code == 1
    assert "not approved" in result.output.lower()


def test_govern_limit_rejects_zero(tmp_path) -> None:
    result = CliRunner().invoke(
        main,
        ["govern", "--start", "--limit", "0"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )
    assert result.exit_code != 0
    assert "positive integer" in result.output.lower()


def test_govern_limit_rejects_negative(tmp_path) -> None:
    result = CliRunner().invoke(
        main,
        ["govern", "--start", "--limit", "-3"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )
    assert result.exit_code != 0
    assert "positive integer" in result.output.lower()


def _patch_orchestrator(monkeypatch, method_name, exc):
    """Replace sentinel.cli.Orchestrator with a context-manager MagicMock whose
    *method_name* raises *exc*."""
    from unittest.mock import MagicMock
    from sentinel import cli as cli_module

    orch = MagicMock()
    getattr(orch, method_name).side_effect = exc
    cm = MagicMock()
    cm.__enter__.return_value = orch
    cm.__exit__.return_value = False
    monkeypatch.setattr(cli_module, "Orchestrator", MagicMock(return_value=cm))
    return orch


def test_task_show_diff_orch_error_exits_cleanly(monkeypatch, tmp_path) -> None:
    _patch_orchestrator(monkeypatch, "get_task_diff", KeyError("task-x"))
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path))

    result = CliRunner().invoke(
        main,
        ["task", "--show-diff", "task-x"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 1
    assert "Error:" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_task_sync_orch_runtime_error_exits_cleanly(monkeypatch, tmp_path) -> None:
    _patch_orchestrator(monkeypatch, "sync_pr_status", RuntimeError("gh offline"))
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path))

    result = CliRunner().invoke(
        main,
        ["task", "--sync", "task-x"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 1
    assert "Error:" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_task_reject_orch_error_exits_cleanly(monkeypatch, tmp_path) -> None:
    _patch_orchestrator(monkeypatch, "record_pr_rejected", ValueError("bad state"))
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path))

    result = CliRunner().invoke(
        main,
        ["task", "--reject", "task-x", "--comment", "no"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 1
    assert "Error:" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_no_alarms_flag_disables_alarm_config(monkeypatch, tmp_path) -> None:
    """--no-alarms turns off both the alarm specialist and alarm classification."""
    from unittest.mock import MagicMock
    from sentinel import cli as cli_module

    monkeypatch.setenv("SENTINEL_ENABLE_ALARM_SPECIALIST", "true")
    monkeypatch.setenv("SENTINEL_ENABLE_ALARM_CLASSIFICATION", "true")
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path / "state"))
    captured: dict = {}
    orch = MagicMock()
    orch.run_phase1.return_value = None
    cm = MagicMock()
    cm.__enter__.return_value = orch
    cm.__exit__.return_value = False

    def _factory(cfg):
        captured["cfg"] = cfg
        return cm

    monkeypatch.setattr(cli_module, "Orchestrator", _factory)
    (tmp_path / "src.py").write_text("x = 1\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["assess", str(tmp_path), "--no-alarms"])

    assert result.exit_code == 0, result.output
    assert captured["cfg"].enable_alarm_specialist is False
    assert captured["cfg"].enable_alarm_classification is False


def test_report_passes_resolved_ref_to_render(monkeypatch, tmp_path) -> None:
    """When --github-url is set with no --github-ref, render() must receive the
    assessed-commit fallback, not the empty original github_ref."""
    import sentinel.reports.decision_brief as brief

    captured = {}

    def _fake_render(state_dir, output_html, output_md, run_label, **kwargs):
        captured["github_ref"] = kwargs.get("github_ref")
        return ("out.html", "out.md", 0, 0)

    monkeypatch.setattr(brief, "assessed_commit", lambda sd: "abc123def456")
    monkeypatch.setattr(brief, "render", _fake_render)

    result = CliRunner().invoke(
        main,
        ["report", "--state-dir", str(tmp_path), "--github-url", "https://h/o/r"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    assert captured["github_ref"] == "abc123def456"


def test_report_without_github_url_passes_ref_through(monkeypatch, tmp_path) -> None:
    """No --github-url: render still receives the (possibly empty) explicit ref
    unchanged, and assessed_commit is not consulted."""
    import sentinel.reports.decision_brief as brief

    captured = {}

    def _fake_render(state_dir, output_html, output_md, run_label, **kwargs):
        captured["github_ref"] = kwargs.get("github_ref")
        return ("out.html", "out.md", 0, 0)

    def _boom(_sd):
        raise AssertionError("assessed_commit should not be called without --github-url")

    monkeypatch.setattr(brief, "assessed_commit", _boom)
    monkeypatch.setattr(brief, "render", _fake_render)

    result = CliRunner().invoke(
        main,
        ["report", "--state-dir", str(tmp_path), "--github-ref", "deadbeef"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    assert captured["github_ref"] == "deadbeef"


def test_retro_report_write_failure_exits_cleanly(monkeypatch, tmp_path) -> None:
    """A write error when saving the retro report exits 1 with a clean message."""
    import sentinel.retro as retro_module
    from unittest.mock import MagicMock

    fake_report = MagicMock()
    fake_report.to_markdown.return_value = "# retro"
    analyzer = MagicMock()
    analyzer.analyze.return_value = fake_report
    monkeypatch.setattr(retro_module, "RetroAnalyzer", MagicMock(return_value=analyzer))

    # output path points into a nonexistent directory => OSError on write
    bad_out = str(tmp_path / "no_such_dir" / "report.md")
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path))

    result = CliRunner().invoke(
        main,
        ["retro", "--state-dir", str(tmp_path), "--output", bad_out],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 1
    assert "error writing report" in result.output.lower()
    assert result.exception is None or isinstance(result.exception, SystemExit)
