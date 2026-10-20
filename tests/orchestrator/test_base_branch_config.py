"""FIX 3 — base branch is configurable via SENTINEL_BASE_BRANCH.

Line_tool may not use ``main`` as its default branch. The PR-creation path must
read the configured base branch rather than a hardcoded literal.

- Config reads SENTINEL_BASE_BRANCH from the environment.
- Config defaults to "main" when the var is absent.
- The GitHub PR path passes config.base_branch to open_pr.
- The compare-URL fallback (_create_pr) uses config.base_branch, not "main".
"""

from __future__ import annotations

from unittest.mock import patch

from sentinel.config import Config
from sentinel.orchestrator.main import Orchestrator
from sentinel.orchestrator.state_machine import TaskState

from tests.test_integration_e2e import _build_config, _make_task, _setup_repo


def test_config_reads_base_branch_from_env():
    cfg = Config(env={"SENTINEL_BASE_BRANCH": "develop"})
    assert cfg.base_branch == "develop"


def test_config_defaults_base_branch_to_main():
    cfg = Config(env={})
    assert cfg.base_branch == "main"


def test_pr_creation_passes_config_base_branch_to_open_pr(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    cfg.self_approve_mode = False
    cfg.base_branch = "develop"

    orch = Orchestrator(cfg)
    task = _make_task("task-bb-pr", is_phase3=False)
    task.state = TaskState.DOC_UPDATED
    task.files_changed = list(task.affected_files)
    orch._store.upsert_task(task.to_dict())

    with patch.object(Orchestrator, "_git_commit_scoped", return_value=None), \
         patch.object(Orchestrator, "_git_push_branch", return_value=None), \
         patch(
             "sentinel.workflow.remediation.pr_opener.open_pr",
             return_value="https://github.com/test/repo/pull/9",
         ) as open_pr_mock:
        orch.approve_local_and_push("task-bb-pr")

    assert open_pr_mock.call_args.kwargs["base_branch"] == "develop"


def test_create_pr_fallback_uses_config_base_branch(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    cfg.base_branch = "develop"

    orch = Orchestrator(cfg)
    task = _make_task("task-bb-fallback", is_phase3=False)

    with patch.object(Orchestrator, "_git_current_branch", return_value=None), \
         patch.object(Orchestrator, "_git_push_branch", return_value=True), \
         patch.object(Orchestrator, "_github_repo_slug", return_value="org/repo"):
        url = orch._create_pr(task)

    assert "compare/develop..." in url
