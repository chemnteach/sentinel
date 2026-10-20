"""Tests for the S2-3 completion gate (`sentinel govern --verify-complete`).

Covers CompletionChecker.check() severity-band computation, blocking-item
assembly, bundle counting, report writing, print_report output, and the CLI
exit code (0 READY / 1 NOT READY).

The severity view is built from findings.json (each finding carries
``severity`` / ``id`` / optionally ``verified``); approved-ness comes from
tech_debt.json (``human_triage == "APPROVE"``); tasks relate to findings via
plan.json (a plan item's ``id`` == task.plan_id, item ``source_findings`` list
the finding ids). See CompletionChecker docstring for the exact join.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import sentinel.config as config_module
from sentinel.config import Config
from sentinel.orchestrator.completion_checker import CompletionChecker
from sentinel.orchestrator.state_store import StateStore


@pytest.fixture(autouse=True)
def _reset_global_config_singleton():
    config_module._config = None
    yield
    config_module._config = None


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _seed(
    state_dir: Path,
    *,
    findings=None,
    tech_debt=None,
    plan=None,
    tasks=None,
    bundles=None,
    scan_metadata=None,
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    if findings is not None:
        _write(state_dir / "findings.json", findings)
    if tech_debt is not None:
        _write(state_dir / "tech_debt.json", tech_debt)
    if plan is not None:
        _write(state_dir / "plan.json", plan)
    if tasks is not None:
        _write(state_dir / "tasks.json", tasks)
    if bundles is not None:
        _write(state_dir / "bundles.json", bundles)
    if scan_metadata is not None:
        _write(state_dir / "scan_metadata.json", scan_metadata)


def _config(state_dir: Path, tmp_path: Path, operator: str = "unknown") -> Config:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_STATE_DIR": str(state_dir),
        "SENTINEL_OPERATOR": operator,
    }
    return Config(env=env, load_dotenv_file=False)


def _checker(state_dir: Path, tmp_path: Path, operator: str = "unknown") -> CompletionChecker:
    cfg = _config(state_dir, tmp_path, operator=operator)
    store = StateStore(state_dir=state_dir, repo_path=cfg.repo_path)
    return CompletionChecker(store, cfg)


def _finding(fid: str, severity: str, verified: bool | None = None) -> dict:
    f = {"id": fid, "severity": severity, "title": f"finding {fid}"}
    if verified is not None:
        f["verified"] = verified
    return f


def _approve(fid: str) -> dict:
    return {"finding_id": fid, "human_triage": "APPROVE"}


def _plan_item(plan_id: str, source_findings: list[str]) -> dict:
    return {"id": plan_id, "source_findings": source_findings}


def _task(task_id: str, plan_id: str, state: str) -> dict:
    return {"task_id": task_id, "plan_id": plan_id, "state": state, "revision": 0}


# ---------------------------------------------------------------------------
# 1. All Critical + P1 tasks COMPLETE -> READY
# ---------------------------------------------------------------------------

def test_all_critical_and_p1_complete_is_ready(tmp_path):
    state_dir = tmp_path / "state"
    _seed(
        state_dir,
        findings=[_finding("F-1", "Critical"), _finding("F-2", "P1")],
        tech_debt=[_approve("F-1"), _approve("F-2")],
        plan={"items": [_plan_item("P-1", ["F-1"]), _plan_item("P-2", ["F-2"])]},
        tasks=[_task("t1", "P-1", "COMPLETE"), _task("t2", "P-2", "HUMAN_MERGED")],
    )
    report = _checker(state_dir, tmp_path).check()

    assert report["status"] == "READY"
    assert report["certification"]["ready"] is True
    assert report["certification"]["critical_open"] == 0
    assert report["certification"]["p1_open"] == 0
    assert report["findings_by_severity"]["Critical"]["complete"] == 1
    assert report["findings_by_severity"]["Critical"]["open"] == 0
    assert report["findings_by_severity"]["P1"]["complete"] == 1
    assert report["certification"]["signed_at"] is not None


# ---------------------------------------------------------------------------
# 2. One open P1 task -> NOT READY + listed in blocking
# ---------------------------------------------------------------------------

def test_open_p1_task_is_not_ready_and_blocking(tmp_path):
    state_dir = tmp_path / "state"
    _seed(
        state_dir,
        findings=[_finding("F-1", "Critical"), _finding("F-2", "P1")],
        tech_debt=[_approve("F-1"), _approve("F-2")],
        plan={"items": [_plan_item("P-1", ["F-1"]), _plan_item("P-2", ["F-2"])]},
        tasks=[_task("t1", "P-1", "COMPLETE"), _task("t2", "P-2", "IN_PROGRESS")],
    )
    report = _checker(state_dir, tmp_path).check()

    assert report["status"] == "NOT_READY"
    assert report["certification"]["ready"] is False
    assert report["certification"]["p1_open"] == 1
    assert report["findings_by_severity"]["P1"]["open"] == 1
    assert any("P1" in item for item in report["blocking_items"])
    assert report["certification"]["signed_at"] is None


# ---------------------------------------------------------------------------
# 3. P2 open -> still READY (non-blocking)
# ---------------------------------------------------------------------------

def test_open_p2_task_is_still_ready(tmp_path):
    state_dir = tmp_path / "state"
    _seed(
        state_dir,
        findings=[_finding("F-1", "Critical"), _finding("F-2", "P2")],
        tech_debt=[_approve("F-1"), _approve("F-2")],
        plan={"items": [_plan_item("P-1", ["F-1"]), _plan_item("P-2", ["F-2"])]},
        tasks=[_task("t1", "P-1", "COMPLETE"), _task("t2", "P-2", "IN_PROGRESS")],
    )
    report = _checker(state_dir, tmp_path).check()

    assert report["status"] == "READY"
    assert report["findings_by_severity"]["P2"]["open"] == 1


# ---------------------------------------------------------------------------
# 4. ESCALATED Critical -> NOT READY + in blocking (stuck)
# ---------------------------------------------------------------------------

def test_escalated_critical_is_not_ready_and_blocking(tmp_path):
    state_dir = tmp_path / "state"
    _seed(
        state_dir,
        findings=[_finding("F-1", "Critical")],
        tech_debt=[_approve("F-1")],
        plan={"items": [_plan_item("P-1", ["F-1"])]},
        tasks=[_task("t1", "P-1", "ESCALATED")],
    )
    report = _checker(state_dir, tmp_path).check()

    assert report["status"] == "NOT_READY"
    assert report["findings_by_severity"]["Critical"]["stuck"] == 1
    assert report["certification"]["critical_open"] == 1
    assert any("Critical" in item for item in report["blocking_items"])


# ---------------------------------------------------------------------------
# 5. Approved Critical finding with NO task at all -> NOT READY + blocking
# ---------------------------------------------------------------------------

def test_approved_critical_with_no_task_is_not_ready(tmp_path):
    state_dir = tmp_path / "state"
    _seed(
        state_dir,
        findings=[_finding("F-1", "Critical")],
        tech_debt=[_approve("F-1")],
        plan={"items": []},
        tasks=[],
    )
    report = _checker(state_dir, tmp_path).check()

    assert report["status"] == "NOT_READY"
    assert report["findings_by_severity"]["Critical"]["open"] == 1
    assert any(
        "no task" in item.lower() for item in report["blocking_items"]
    )


# ---------------------------------------------------------------------------
# 6. Bundles counted correctly by BundleStatus
# ---------------------------------------------------------------------------

def test_bundles_counted_by_status(tmp_path):
    state_dir = tmp_path / "state"
    _seed(
        state_dir,
        findings=[_finding("F-1", "P2")],
        tech_debt=[_approve("F-1")],
        plan={"items": [_plan_item("P-1", ["F-1"])]},
        tasks=[_task("t1", "P-1", "COMPLETE")],
        bundles=[
            {"bundle_id": "B-1", "bundle_status": "COMPLETE", "member_findings": []},
            {"bundle_id": "B-2", "bundle_status": "IN_REVIEW", "member_findings": []},
            {"bundle_id": "B-3", "bundle_status": "IN_PROGRESS", "member_findings": []},
            {"bundle_id": "B-4", "bundle_status": "APPROVED", "member_findings": []},
            {"bundle_id": "B-5", "bundle_status": "UNDECIDED", "member_findings": []},
            {"bundle_id": "B-6", "bundle_status": "REVERTED", "member_findings": []},
        ],
    )
    report = _checker(state_dir, tmp_path).check()

    b = report["bundles"]
    assert b["total"] == 6
    assert b["complete"] == 1
    assert b["in_review"] == 1
    assert b["in_progress"] == 1
    assert b["approved"] == 1
    assert b["undecided"] == 1
    assert b["reverted"] == 1


# ---------------------------------------------------------------------------
# 7. report dict carries all required keys
# ---------------------------------------------------------------------------

def test_report_dict_keys_present(tmp_path):
    state_dir = tmp_path / "state"
    _seed(
        state_dir,
        findings=[_finding("F-1", "Critical")],
        tech_debt=[_approve("F-1")],
        plan={"items": [_plan_item("P-1", ["F-1"])]},
        tasks=[_task("t1", "P-1", "COMPLETE")],
        scan_metadata={"commit_sha": "abc123", "model": "gpt-5.1", "provider": "openai"},
    )
    report = _checker(state_dir, tmp_path).check()

    for key in (
        "run_id", "assessed_commit", "model", "provider", "operator",
        "generated_at", "verified_findings", "unverified_filtered",
        "findings_by_severity", "bundles", "blocking_items", "status",
        "certification",
    ):
        assert key in report, f"missing report key: {key}"
    for band in ("Critical", "P1", "P2", "P3"):
        assert band in report["findings_by_severity"]
        for sub in ("approved", "complete", "open", "stuck"):
            assert sub in report["findings_by_severity"][band]


# ---------------------------------------------------------------------------
# 8. print_report emits the STATUS line (READY / NOT READY)
# ---------------------------------------------------------------------------

def test_print_report_ready_status_line(tmp_path, capsys):
    state_dir = tmp_path / "state"
    _seed(
        state_dir,
        findings=[_finding("F-1", "Critical")],
        tech_debt=[_approve("F-1")],
        plan={"items": [_plan_item("P-1", ["F-1"])]},
        tasks=[_task("t1", "P-1", "COMPLETE")],
    )
    checker = _checker(state_dir, tmp_path)
    report = checker.check()
    checker.print_report(report)
    out = capsys.readouterr().out
    assert "READY FOR DELIVERY" in out


def test_print_report_not_ready_status_line(tmp_path, capsys):
    state_dir = tmp_path / "state"
    _seed(
        state_dir,
        findings=[_finding("F-1", "Critical")],
        tech_debt=[_approve("F-1")],
        plan={"items": [_plan_item("P-1", ["F-1"])]},
        tasks=[_task("t1", "P-1", "IN_PROGRESS")],
    )
    checker = _checker(state_dir, tmp_path)
    report = checker.check()
    checker.print_report(report)
    out = capsys.readouterr().out
    assert "NOT READY" in out


# ---------------------------------------------------------------------------
# 9. scan_metadata model/provider/commit captured in report
# ---------------------------------------------------------------------------

def test_scan_metadata_captured(tmp_path):
    state_dir = tmp_path / "state"
    _seed(
        state_dir,
        findings=[_finding("F-1", "P3")],
        tech_debt=[_approve("F-1")],
        plan={"items": [_plan_item("P-1", ["F-1"])]},
        tasks=[_task("t1", "P-1", "COMPLETE")],
        scan_metadata={"commit_sha": "deadbeef", "model": "gpt-x", "provider": "openai"},
    )
    report = _checker(state_dir, tmp_path).check()
    assert report["assessed_commit"] == "deadbeef"
    assert report["model"] == "gpt-x"
    assert report["provider"] == "openai"


# ---------------------------------------------------------------------------
# 10. SENTINEL_OPERATOR captured in report
# ---------------------------------------------------------------------------

def test_operator_captured(tmp_path):
    state_dir = tmp_path / "state"
    _seed(
        state_dir,
        findings=[_finding("F-1", "P3")],
        tech_debt=[_approve("F-1")],
        plan={"items": [_plan_item("P-1", ["F-1"])]},
        tasks=[_task("t1", "P-1", "COMPLETE")],
    )
    report = _checker(state_dir, tmp_path, operator="alice").check()
    assert report["operator"] == "alice"


# ---------------------------------------------------------------------------
# 11. verified_findings counts findings where verified == True
# ---------------------------------------------------------------------------

def test_verified_findings_counted(tmp_path):
    state_dir = tmp_path / "state"
    _seed(
        state_dir,
        findings=[
            _finding("F-1", "Critical", verified=True),
            _finding("F-2", "P1", verified=True),
            _finding("F-3", "P2", verified=False),
        ],
        tech_debt=[_approve("F-1"), _approve("F-2")],
        plan={"items": [_plan_item("P-1", ["F-1"]), _plan_item("P-2", ["F-2"])]},
        tasks=[_task("t1", "P-1", "COMPLETE"), _task("t2", "P-2", "COMPLETE")],
    )
    report = _checker(state_dir, tmp_path).check()
    assert report["verified_findings"] == 2


def test_verified_findings_counts_all_when_field_absent(tmp_path):
    state_dir = tmp_path / "state"
    _seed(
        state_dir,
        findings=[_finding("F-1", "Critical"), _finding("F-2", "P1")],
        tech_debt=[_approve("F-1"), _approve("F-2")],
        plan={"items": [_plan_item("P-1", ["F-1"]), _plan_item("P-2", ["F-2"])]},
        tasks=[_task("t1", "P-1", "COMPLETE"), _task("t2", "P-2", "COMPLETE")],
    )
    report = _checker(state_dir, tmp_path).check()
    assert report["verified_findings"] == 2


# ---------------------------------------------------------------------------
# 12. Graceful with missing plan/tasks/bundles -> approved findings all "open"
# ---------------------------------------------------------------------------

def test_missing_plan_and_tasks_treats_approved_as_open(tmp_path):
    state_dir = tmp_path / "state"
    _seed(
        state_dir,
        findings=[_finding("F-1", "P1")],
        tech_debt=[_approve("F-1")],
    )
    report = _checker(state_dir, tmp_path).check()
    assert report["status"] == "NOT_READY"
    assert report["findings_by_severity"]["P1"]["open"] == 1
    assert report["bundles"]["total"] == 0


# ---------------------------------------------------------------------------
# 13. CLI --verify-complete: exit codes + completion_report.json written
# ---------------------------------------------------------------------------

def _run_cli(state_dir: Path, tmp_path: Path, monkeypatch, operator: str = "unknown"):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    monkeypatch.setenv("SENTINEL_REPO_PATH", str(repo))
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SENTINEL_LOCAL_ONLY_MODE", "true")
    monkeypatch.setenv("SENTINEL_OPERATOR", operator)
    from sentinel.cli import main
    runner = CliRunner()
    return runner.invoke(main, ["govern", "--verify-complete"])


def test_cli_exit_zero_when_ready(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    _seed(
        state_dir,
        findings=[_finding("F-1", "Critical")],
        tech_debt=[_approve("F-1")],
        plan={"items": [_plan_item("P-1", ["F-1"])]},
        tasks=[_task("t1", "P-1", "COMPLETE")],
    )
    result = _run_cli(state_dir, tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    assert (state_dir / "completion_report.json").exists()
    report = json.loads((state_dir / "completion_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "READY"


def test_cli_exit_one_when_not_ready(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    _seed(
        state_dir,
        findings=[_finding("F-1", "Critical")],
        tech_debt=[_approve("F-1")],
        plan={"items": [_plan_item("P-1", ["F-1"])]},
        tasks=[_task("t1", "P-1", "IN_PROGRESS")],
    )
    result = _run_cli(state_dir, tmp_path, monkeypatch)
    assert result.exit_code == 1, result.output
    assert (state_dir / "completion_report.json").exists()
