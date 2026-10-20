"""Tests for S3-6 rollback (revert) tracking in the PR-sync loop.

Covers revert detection for tasks already COMPLETE / HUMAN_MERGED, the
``record_pr_reverted`` state transition + finding/bundle side effects, the
``sync_all_prs`` "reverted" summary count, and the CLI sync output line.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from click.testing import CliRunner

import sentinel.cli as cli_module
from sentinel.cli import main
from sentinel.config import Config
import sentinel.config as config_module
from sentinel.orchestrator.main import Orchestrator


@pytest.fixture(autouse=True)
def _reset_global_config_singleton():
    config_module._config = None
    yield
    config_module._config = None


GH = "https://api.github.com"
ORIGINAL_PR_TITLE = "sentinel: task-1 — fix the widget"
MERGE_SHA = "abc123def456"


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _seed_state(state_dir: Path, task_state: str = "COMPLETE") -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    _write(state_dir / "tasks.json", [
        {
            "task_id": "task-1",
            "plan_id": "FIX-REQUIRED-001",
            "state": task_state,
            "pattern_id": "P-1",
            "pr_number": 42,
            "pr_url": "https://github.com/org/repo/pull/42",
            "bundle_id": "BUNDLE-001",
            "revision": 0,
        }
    ])
    _write(state_dir / "remediation_patterns.json", [
        {"pattern_id": "P-1", "group_id": "G-1"}
    ])
    _write(state_dir / "finding_status.json", [
        {
            "group_id": "G-1",
            "canonical_finding_id": "F-1",
            "member_finding_ids": ["F-1"],
            "severity": "P1",
            "status": "REMEDIATED",
            "approval_type": "APPROVE",
            "conditions": "",
            "task_id": "task-1",
            "batch_id": None,
            "status_updated_at": "2026-01-01T00:00:00Z",
            "status_updated_by": "sentinel",
            "notes": "",
        }
    ])
    _write(state_dir / "bundles.json", [
        {"bundle_id": "BUNDLE-001", "root_cause_title": "Widget bug",
         "bundle_status": "APPROVED"}
    ])


def _make_orch(tmp_path: Path, monkeypatch, token: str = "tok",
               task_state: str = "COMPLETE"):
    repo = tmp_path / "repo"
    repo.mkdir()
    state_dir = tmp_path / "state"
    _seed_state(state_dir, task_state=task_state)
    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_STATE_DIR": str(state_dir),
        "GITHUB_API_TOKEN": token,
    }
    cfg = Config(env=env, load_dotenv_file=False)
    orch = Orchestrator(cfg)
    monkeypatch.setattr(orch, "_github_repo_slug", lambda: "org/repo")
    return orch, state_dir


def _load_task_state(state_dir: Path) -> str:
    tasks = json.loads((state_dir / "tasks.json").read_text(encoding="utf-8"))
    return tasks[0]["state"]


def _load_group_status(state_dir: Path) -> str:
    recs = json.loads((state_dir / "finding_status.json").read_text(encoding="utf-8"))
    return recs[0]["status"]


def _load_bundle_status(state_dir: Path) -> str:
    bundles = json.loads((state_dir / "bundles.json").read_text(encoding="utf-8"))
    return bundles[0]["bundle_status"]


def _mock_original_pr(merged: bool = True) -> None:
    """Mock GET /pulls/42 — the original, merged PR."""
    respx.get(f"{GH}/repos/org/repo/pulls/42").mock(return_value=httpx.Response(
        200, json={
            "number": 42,
            "merged": merged,
            "state": "closed" if merged else "open",
            "title": ORIGINAL_PR_TITLE,
            "merge_commit_sha": MERGE_SHA,
            "html_url": "https://github.com/org/repo/pull/42",
        },
    ))


def _mock_pr_list(pulls: list[dict]) -> None:
    """Mock GET /pulls?state=all — the candidate list for revert detection."""
    respx.get(f"{GH}/repos/org/repo/pulls").mock(return_value=httpx.Response(
        200, json=pulls,
    ))


# ---------------------------------------------------------------------------
# 1. COMPLETE task + revert PR found → task transitions to REVERTED
# ---------------------------------------------------------------------------

@respx.mock
def test_complete_task_with_revert_pr_transitions_to_reverted(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, monkeypatch)
    _mock_original_pr(merged=True)
    _mock_pr_list([
        {"number": 99, "merged": True, "state": "closed",
         "title": f'Revert "{ORIGINAL_PR_TITLE}"', "body": "",
         "html_url": "https://github.com/org/repo/pull/99"},
    ])

    with orch:
        summary = orch.sync_all_prs()

    assert summary["reverted"] == 1
    assert _load_task_state(state_dir) == "REVERTED"


# ---------------------------------------------------------------------------
# 2. COMPLETE task + no revert PR → stays COMPLETE
# ---------------------------------------------------------------------------

@respx.mock
def test_complete_task_no_revert_pr_stays_complete(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, monkeypatch)
    _mock_original_pr(merged=True)
    _mock_pr_list([
        {"number": 50, "merged": True, "state": "closed",
         "title": "Some unrelated PR", "body": "",
         "html_url": "https://github.com/org/repo/pull/50"},
    ])

    with orch:
        summary = orch.sync_all_prs()

    assert summary["reverted"] == 0
    assert _load_task_state(state_dir) == "COMPLETE"


# ---------------------------------------------------------------------------
# 3. Revert matched by merge_commit_sha in the revert PR body
# ---------------------------------------------------------------------------

@respx.mock
def test_revert_matched_by_merge_commit_sha(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, monkeypatch)
    _mock_original_pr(merged=True)
    _mock_pr_list([
        {"number": 77, "merged": True, "state": "closed",
         "title": "Undo the widget change",
         "body": f"This reverts commit {MERGE_SHA}.",
         "html_url": "https://github.com/org/repo/pull/77"},
    ])

    with orch:
        summary = orch.sync_all_prs()

    assert summary["reverted"] == 1
    assert _load_task_state(state_dir) == "REVERTED"


# ---------------------------------------------------------------------------
# 4. REVERTED task → finding status set back to APPROVED (reopened)
# ---------------------------------------------------------------------------

@respx.mock
def test_revert_reopens_finding_status_to_approved(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, monkeypatch)
    _mock_original_pr(merged=True)
    _mock_pr_list([
        {"number": 99, "merged": True, "state": "closed",
         "title": f'Revert "{ORIGINAL_PR_TITLE}"', "body": "",
         "html_url": "https://github.com/org/repo/pull/99"},
    ])

    with orch:
        orch.sync_all_prs()

    assert _load_group_status(state_dir) == "APPROVED"


# ---------------------------------------------------------------------------
# 5. REVERTED task → bundle bundle_status = REVERTED
# ---------------------------------------------------------------------------

@respx.mock
def test_revert_sets_bundle_status_reverted(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, monkeypatch)
    _mock_original_pr(merged=True)
    _mock_pr_list([
        {"number": 99, "merged": True, "state": "closed",
         "title": f'Revert "{ORIGINAL_PR_TITLE}"', "body": "",
         "html_url": "https://github.com/org/repo/pull/99"},
    ])

    with orch:
        orch.sync_all_prs()

    assert _load_bundle_status(state_dir) == "REVERTED"


# ---------------------------------------------------------------------------
# 6. record_pr_reverted creates a pr_reverted audit event
# ---------------------------------------------------------------------------

def test_record_pr_reverted_audit_event(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, monkeypatch)
    with orch:
        task = orch._task_manager.load_task("task-1")
        orch.record_pr_reverted(task, revert_pr_number=99)

    audit = json.loads((state_dir / "audit_log.json").read_text(encoding="utf-8"))
    actions = [e.get("action", "") for e in audit]
    assert any("pr_reverted" in a for a in actions), actions
    # revert_pr_number captured somewhere in the audit details
    reverted = [e for e in audit if "pr_reverted" in e.get("action", "")]
    assert any(
        json.dumps(e.get("details", {})).find("99") != -1 for e in reverted
    ), reverted


# ---------------------------------------------------------------------------
# 7. record_pr_reverted transitions COMPLETE task to REVERTED
# ---------------------------------------------------------------------------

def test_record_pr_reverted_transitions_state(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, monkeypatch)
    with orch:
        task = orch._task_manager.load_task("task-1")
        orch.record_pr_reverted(task, revert_pr_number=99)
    assert _load_task_state(state_dir) == "REVERTED"


# ---------------------------------------------------------------------------
# 8. A revert-detection GitHub failure does not crash the sync
# ---------------------------------------------------------------------------

@respx.mock
def test_revert_detection_network_failure_is_safe(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, monkeypatch)
    respx.get(f"{GH}/repos/org/repo/pulls/42").mock(
        side_effect=httpx.ConnectError("boom")
    )

    with orch:
        summary = orch.sync_all_prs()

    assert summary["reverted"] == 0
    assert _load_task_state(state_dir) == "COMPLETE"


# ---------------------------------------------------------------------------
# 9. sync_all_prs summary includes a "reverted" count (no revert case)
# ---------------------------------------------------------------------------

@respx.mock
def test_sync_all_prs_summary_has_reverted_key(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, monkeypatch)
    _mock_original_pr(merged=True)
    _mock_pr_list([])

    with orch:
        summary = orch.sync_all_prs()

    assert "reverted" in summary
    assert summary["reverted"] == 0


# ---------------------------------------------------------------------------
# 10. CLI prints "W reverted" in the sync output
# ---------------------------------------------------------------------------

def test_cli_sync_prints_reverted_count(tmp_path, monkeypatch):
    class _FakeOrch:
        def __init__(self, cfg):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def sync_all_prs(self):
            return {"nothing_to_sync": False, "merged": 2, "rejected": 1,
                    "still_open": 3, "reverted": 4, "errors": 0, "results": []}

    monkeypatch.setattr(cli_module, "Orchestrator", _FakeOrch)

    result = CliRunner().invoke(
        main, ["govern", "--sync-prs"],
        env={"SENTINEL_STATE_DIR": str(tmp_path / "state")},
    )
    assert result.exit_code == 0, result.output
    assert "4 reverted" in result.output
