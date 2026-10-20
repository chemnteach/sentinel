"""Tests for `sentinel govern --sync-prs` PR-merge loop closure."""
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


API = "https://api.github.com/repos/org/repo/pulls/42"


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _seed_state(state_dir: Path, task_state: str = "PR_CREATED") -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    _write(state_dir / "tasks.json", [
        {
            "task_id": "task-1",
            "plan_id": "FIX-REQUIRED-001",
            "state": task_state,
            "pattern_id": "P-1",
            "pr_number": 42,
            "pr_url": "https://github.com/org/repo/pull/42",
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
            "status": "IN_PROGRESS",
            "approval_type": "APPROVE",
            "conditions": "",
            "task_id": "task-1",
            "batch_id": None,
            "status_updated_at": "2026-01-01T00:00:00Z",
            "status_updated_by": "sentinel",
            "notes": "",
        }
    ])


def _make_orch(tmp_path: Path, monkeypatch, token: str = "tok", task_state: str = "PR_CREATED"):
    repo = tmp_path / "repo"
    repo.mkdir()
    state_dir = tmp_path / "state"
    _seed_state(state_dir, task_state=task_state)
    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_STATE_DIR": str(state_dir),
    }
    if token:
        env["GITHUB_API_TOKEN"] = token
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


# ---------------------------------------------------------------------------
# 1. PR merged → task COMPLETE, finding REMEDIATED
# ---------------------------------------------------------------------------

@respx.mock
def test_sync_prs_merged_marks_complete_and_remediated(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, monkeypatch)
    respx.get(API).mock(return_value=httpx.Response(
        200, json={"number": 42, "merged": True, "state": "closed",
                   "html_url": "https://github.com/org/repo/pull/42"},
    ))

    with orch:
        summary = orch.sync_all_prs()

    assert summary["merged"] == 1
    assert summary["rejected"] == 0
    assert summary["still_open"] == 0
    assert _load_task_state(state_dir) == "COMPLETE"
    assert _load_group_status(state_dir) == "REMEDIATED"


# ---------------------------------------------------------------------------
# 2. PR rejected (closed unmerged) → task reopened for revision
# ---------------------------------------------------------------------------

@respx.mock
def test_sync_prs_rejected_reopens_task(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, monkeypatch)
    respx.get(API).mock(return_value=httpx.Response(
        200, json={"number": 42, "merged": False, "state": "closed",
                   "html_url": "https://github.com/org/repo/pull/42"},
    ))

    with orch:
        summary = orch.sync_all_prs()

    assert summary["rejected"] == 1
    assert summary["merged"] == 0
    assert _load_task_state(state_dir) == "IN_PROGRESS"
    # finding status unchanged (not remediated)
    assert _load_group_status(state_dir) == "IN_PROGRESS"


# ---------------------------------------------------------------------------
# 3. PR still open → no change
# ---------------------------------------------------------------------------

@respx.mock
def test_sync_prs_open_no_change(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, monkeypatch)
    respx.get(API).mock(return_value=httpx.Response(
        200, json={"number": 42, "merged": False, "state": "open",
                   "html_url": "https://github.com/org/repo/pull/42"},
    ))
    respx.get(f"{API}/reviews").mock(return_value=httpx.Response(200, json=[]))

    with orch:
        summary = orch.sync_all_prs()

    assert summary["merged"] == 0
    assert summary["rejected"] == 0
    assert summary["still_open"] == 1
    assert _load_task_state(state_dir) == "PR_CREATED"
    assert _load_group_status(state_dir) == "IN_PROGRESS"


# ---------------------------------------------------------------------------
# 4. No tasks in PR_CREATED → nothing to sync
# ---------------------------------------------------------------------------

def test_sync_prs_nothing_to_sync(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, monkeypatch, task_state="IN_PROGRESS")
    with orch:
        summary = orch.sync_all_prs()
    assert summary["nothing_to_sync"] is True
    assert summary["merged"] == 0


# ---------------------------------------------------------------------------
# 5. Missing GITHUB_TOKEN → clear error
# ---------------------------------------------------------------------------

def test_sync_prs_missing_token_raises(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path, monkeypatch, token="")
    with orch:
        with pytest.raises(RuntimeError) as exc:
            orch.sync_all_prs()
    assert "GITHUB" in str(exc.value).upper()


# ---------------------------------------------------------------------------
# 6. CLI registration + messages
# ---------------------------------------------------------------------------

def test_govern_help_lists_sync_prs():
    result = CliRunner().invoke(main, ["govern", "--help"])
    assert result.exit_code == 0
    assert "--sync-prs" in result.output


def test_govern_sync_prs_nothing_to_sync_message(tmp_path, monkeypatch):
    class _FakeOrch:
        def __init__(self, cfg):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def sync_all_prs(self):
            return {"nothing_to_sync": True, "merged": 0, "rejected": 0,
                    "still_open": 0, "results": []}

    monkeypatch.setattr(cli_module, "Orchestrator", _FakeOrch)

    result = CliRunner().invoke(
        main, ["govern", "--sync-prs"],
        env={"SENTINEL_STATE_DIR": str(tmp_path / "state")},
    )
    assert result.exit_code == 0, result.output
    assert "nothing to sync" in result.output.lower()
