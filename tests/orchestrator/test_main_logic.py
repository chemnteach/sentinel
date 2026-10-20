"""P1 logic-bug regression tests for orchestrator/main.py.

Covers seven fixes:
1. Retry-gate off-by-one (>= 3 escalation) in _run_task / _run_verification_gate.
2. _git_commit_scoped isolates the git index before staging.
3. sync_pr_status compares submitted_at as parsed timestamps (_ts_ge helper).
4. _build_phase1_assessment_report skips non-dict findings.
5. _classify_findings_adaptive fail-open path keys on finding_id (fallback id).
6. continue_phase1_after_triage guards a None tech_debt read.
7. _handle_dispute bounds dispute-resolution re-entry cycles.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import respx

import sentinel.config as config_module
from sentinel.agents.architect import ArchitectAgent
from sentinel.agents.coder import CoderAgent
from sentinel.config import Config
from sentinel.orchestrator.main import (
    HumanGateRequired,
    Orchestrator,
    _MAX_DISPUTE_ROUNDS,
    _extract_and_store_learnings,
)
from sentinel.orchestrator.state_machine import TaskState

from tests.test_integration_e2e import _build_config, _make_task, _setup_repo


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


# ---------------------------------------------------------------------------
# Finding 1 — retry-gate off-by-one: escalate on the 3rd attempt (>= 3)
# ---------------------------------------------------------------------------


def test_self_review_gate_escalates_on_third_attempt(tmp_path):
    """review_attempts seeded at 2 -> 3 after increment must escalate (>= 3),
    not send back for a 4th attempt. Pins the off-by-one fix in _run_task."""
    import contextlib

    from unittest.mock import MagicMock
    from sentinel.agents.docs import DocsAgent
    from sentinel.agents.qa import QAAgent
    from sentinel.phase3.compat_checker import CompatChecker
    from tests.test_integration_e2e import _EDIT, _IMPL_RESULT, _TODO
    from sentinel.workflow.remediation.apply import apply_edits

    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")

    def _fake_implement(self, _context):
        target = repo_root / _EDIT["file"]
        if _EDIT["new_text"].strip() not in target.read_text(encoding="utf-8"):
            apply_edits([_EDIT], str(repo_root))
        return dict(_IMPL_RESULT)

    critical = {"findings": [{"severity": "Critical", "description": "unsafe"}]}

    with contextlib.ExitStack() as stack:
        e = stack.enter_context
        e(patch.object(CoderAgent, "submit_todo", return_value=dict(_TODO)))
        e(patch.object(CoderAgent, "implement", autospec=True, side_effect=_fake_implement))
        e(patch.object(CoderAgent, "self_review", return_value=critical))
        e(patch.object(
            ArchitectAgent, "review_todo",
            return_value={
                "implementation_plan_verdict": "APPROVE",
                "test_stubs_verdict": "APPROVE",
            },
        ))
        review_diff_mock = e(patch.object(
            ArchitectAgent, "review_diff",
            return_value={"verdict": "APPROVE", "findings": [], "feedback": ""},
        ))
        e(patch.object(QAAgent, "verify_fix", return_value={"verdict": "PASS", "tdd_assessment": {}}))
        e(patch.object(
            DocsAgent, "generate_transition_manifest",
            return_value={"docs_updated": [], "doc_updated": True},
        ))
        e(patch.object(CompatChecker, "check", MagicMock(return_value={"verdict": "PASS", "violations": []})))

        orch = Orchestrator(cfg)
        task = _make_task("task-sr-boundary", is_phase3=False)
        task.review_attempts = 2  # -> 3 after the gate increments; >= 3 escalates
        orch._store.upsert_task(task.to_dict())

        with pytest.raises(HumanGateRequired) as exc:
            orch._run_task(task)
        assert exc.value.gate_type == "self_review_failure"
        review_diff_mock.assert_not_called()

        final = orch._task_manager.load_task("task-sr-boundary")
        assert final.state == TaskState.ESCALATED
        assert final.review_attempts == 3


# ---------------------------------------------------------------------------
# Finding 2 — _git_commit_scoped isolates the index
# ---------------------------------------------------------------------------


def test_scoped_commit_ignores_pre_staged_changes(tmp_path):
    """A file already staged from an earlier op must NOT land in a scoped commit."""
    repo = tmp_path
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.dev")
    _git(repo, "config", "user.name", "t")

    a = repo / "a.py"
    b = repo / "b.py"
    a.write_text("a = 1\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-m", "init")

    # Pre-stage a.py's change (simulating leftover index state), and modify b.py.
    a.write_text("a = 2\n", encoding="utf-8")
    b.write_text("b = 1\n", encoding="utf-8")
    _git(repo, "add", "a.py")  # pre-staged, must be excluded from the scoped commit

    fake = SimpleNamespace(_config=SimpleNamespace(repo_path=str(repo)))
    Orchestrator._git_commit_scoped(fake, ["b.py"], "scoped: b only")

    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert committed == ["b.py"]
    # a.py's pre-staged modification is still pending (not committed).
    assert "a.py" in _git(repo, "diff", "--name-only").stdout


# ---------------------------------------------------------------------------
# Finding 3 — timestamp comparison parses ISO-8601
# ---------------------------------------------------------------------------


def test_ts_ge_parses_iso_timestamps():
    # 09:00 is after 08:59 even though raw-string sort would agree here...
    assert Orchestrator._ts_ge("2026-01-01T09:00:00+00:00", "2026-01-01T08:59:00+00:00")
    # ...but offset-normalisation matters: a Z suffix vs +00:00 for the same instant.
    assert Orchestrator._ts_ge("2026-01-01T09:00:00Z", "2026-01-01T09:00:00+00:00")
    assert Orchestrator._ts_ge("2026-01-01T09:00:00+00:00", "2026-01-01T09:00:00Z")
    # An earlier instant is NOT >= a later one.
    assert not Orchestrator._ts_ge(
        "2026-01-01T08:00:00Z", "2026-01-01T09:00:00Z"
    )


def test_ts_ge_falls_back_to_string_on_unparseable():
    # Non-ISO strings fall back to raw comparison (no crash).
    assert Orchestrator._ts_ge("zzz", "aaa")
    assert not Orchestrator._ts_ge("aaa", "zzz")


# ---------------------------------------------------------------------------
# Finding 4 — _build_phase1_assessment_report tolerates non-dict findings
# ---------------------------------------------------------------------------


def test_assessment_report_skips_non_dict_findings(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    orch = Orchestrator(cfg)

    # Malformed findings mixed with a valid one — must not raise AttributeError.
    orch._store.write(
        "findings",
        [
            "a bad string entry",
            None,
            42,
            {"id": "F-1", "severity": "P1", "module": "m", "description": "d"},
        ],
    )
    orch._store.write("dependencies", [])
    orch._store.write("repositories", [{"name": "repo"}])

    report = orch._build_phase1_assessment_report({})

    # Only the single valid dict finding survives.
    ids = [f["id"] for f in report["findings"]]
    assert ids == ["F-1"]


# ---------------------------------------------------------------------------
# Finding 5 — fail-open verifiability path keys on finding_id
# ---------------------------------------------------------------------------


def test_classify_fail_open_keys_on_finding_id(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    orch = Orchestrator(cfg)

    findings = [{"finding_id": "FID-1", "description": "x"}]

    # Force the classify call to raise a recoverable ValueError so the
    # min-chunk fail-open branch runs.
    with patch.object(
        ArchitectAgent, "classify_findings", side_effect=ValueError("truncated")
    ):
        result = orch._classify_findings_adaptive(findings, str(repo_root), min_chunk=5)

    # The fail-open verdict is keyed by finding_id, not the (absent) "id".
    assert result == {"FID-1": "VERIFIABLE"}


def test_classify_fail_open_falls_back_to_id(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    orch = Orchestrator(cfg)

    findings = [{"id": "ID-1", "description": "x"}]
    with patch.object(
        ArchitectAgent, "classify_findings", side_effect=ValueError("truncated")
    ):
        result = orch._classify_findings_adaptive(findings, str(repo_root), min_chunk=5)

    assert result == {"ID-1": "VERIFIABLE"}


# ---------------------------------------------------------------------------
# Finding 6 — continue_phase1_after_triage guards None tech_debt
# ---------------------------------------------------------------------------


def test_continue_after_triage_handles_missing_tech_debt(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    orch = Orchestrator(cfg)

    # tech_debt state file absent → store.read returns None. Must not raise
    # TypeError; with no approved items it logs and returns cleanly.
    orch.continue_phase1_after_triage()  # no exception == pass


# ---------------------------------------------------------------------------
# Finding 7 — dispute-resolution cycle guard
# ---------------------------------------------------------------------------


def _submitted_task(orch, task_id, *, dispute_rounds=0):
    task = _make_task(task_id, is_phase3=False)
    task.state = TaskState.SUBMITTED
    task.files_changed = list(task.affected_files)
    task.dispute_rounds = dispute_rounds
    orch._store.upsert_task(task.to_dict())
    return task


_TRIGGER = {"verdict": "DISPUTE", "findings": [], "feedback": "architect disagrees"}


def test_dispute_cycle_guard_escalates_after_cap(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    orch = Orchestrator(cfg)
    # Already at the cap — the next resolution pushes it over and must escalate
    # instead of re-running the task.
    task = _submitted_task(orch, "task-loop", dispute_rounds=_MAX_DISPUTE_ROUNDS)

    with patch.object(
        ArchitectAgent,
        "build_position_statement",
        side_effect=[{"issue": "x"}, {"resolved": True, "agreed_approach": "retry"}],
    ), patch.object(
        CoderAgent, "build_position_statement", return_value={"issue": "y"}
    ), patch.object(Orchestrator, "_run_task") as run_task_mock:
        with pytest.raises(HumanGateRequired) as exc:
            orch._handle_dispute(task, _TRIGGER)

    assert exc.value.gate_type == "dispute_escalation"
    run_task_mock.assert_not_called()

    final = orch._task_manager.load_task("task-loop")
    assert final.state == TaskState.ESCALATED


def test_dispute_under_cap_still_reruns_and_counts(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    orch = Orchestrator(cfg)
    task = _submitted_task(orch, "task-ok", dispute_rounds=0)

    with patch.object(
        ArchitectAgent,
        "build_position_statement",
        side_effect=[{"issue": "x"}, {"resolved": True, "agreed_approach": "retry"}],
    ), patch.object(
        CoderAgent, "build_position_statement", return_value={"issue": "y"}
    ), patch.object(Orchestrator, "_run_task") as run_task_mock:
        orch._handle_dispute(task, _TRIGGER)

    run_task_mock.assert_called_once()
    final = orch._task_manager.load_task("task-ok")
    assert final.state == TaskState.IN_PROGRESS
    # The round counter was incremented for this resolution.
    assert final.dispute_rounds == 1


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Wave 2: semantic verification, sync_pr_status, concept_themes, push signal
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_global_config_singleton():
    config_module._config = None
    yield
    config_module._config = None


def _make_orch(tmp_path: Path, monkeypatch=None, token: str = "tok"):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_STATE_DIR": str(state_dir),
    }
    if token:
        env["GITHUB_API_TOKEN"] = token
    cfg = Config(env=env, load_dotenv_file=False)
    return Orchestrator(cfg), state_dir


# ---------------------------------------------------------------------------
# _run_semantic_verification — fail CLOSED (F2-5cc75ee7ffd2 / F2-9b00fa8ae0b3)
# ---------------------------------------------------------------------------


class _FakeTask:
    task_id = "task-1"
    affected_files = ["sentinel/foo.py"]
    pre_fix_sha = "abc123"


def test_semantic_verification_fails_closed_on_timeout(tmp_path):
    orch, _ = _make_orch(tmp_path)
    # Force a tiny timeout and a worker that overruns it.
    orch._config.verification_timeout_seconds = 0.1

    def _slow(task):
        time.sleep(2.0)
        return (True, "should never be observed")

    orch._do_semantic_verification = _slow
    verified, reason = orch._run_semantic_verification(_FakeTask(), "abc123")

    assert verified is False
    assert "timed out" in reason
    assert "gate not cleared" in reason


def test_semantic_verification_fails_closed_on_exception(tmp_path):
    orch, _ = _make_orch(tmp_path)

    def _boom(task):
        raise ValueError("worker exploded")

    orch._do_semantic_verification = _boom
    verified, reason = orch._run_semantic_verification(_FakeTask(), "abc123")

    assert verified is False
    assert "verification error" in reason
    assert "worker exploded" in reason


def test_semantic_verification_passes_through_worker_result(tmp_path):
    orch, _ = _make_orch(tmp_path)
    orch._do_semantic_verification = lambda task: (True, "all clear")
    verified, reason = orch._run_semantic_verification(_FakeTask(), "abc123")
    assert verified is True
    assert reason == "all clear"


# ---------------------------------------------------------------------------
# _run_concept_themes — RuntimeError classification
#   (F2-ca6825c0ddce / F2-cf07d12f2ae5)
# ---------------------------------------------------------------------------


def _seed_findings(state_dir: Path, n: int = 10) -> None:
    findings = [
        {
            "id": f"F-{i}",
            "finding_id": f"F-{i}",
            "severity": "P1",
            "description": f"finding {i}",
            "summary": f"finding {i}",
        }
        for i in range(n)
    ]
    (state_dir / "findings.json").write_text(
        json.dumps(findings, indent=2), encoding="utf-8"
    )


def test_concept_themes_skips_on_missing_dependency(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path)
    _seed_findings(state_dir)

    import sentinel.orchestrator.main as main_mod

    def _raise_missing(*a, **k):
        raise RuntimeError("No module named 'hdbscan'")

    monkeypatch.setattr(
        main_mod, "build_concept_themes_from_descriptions", _raise_missing
    )
    # Should swallow the missing-dependency RuntimeError and write empty themes.
    orch._run_concept_themes()
    themes = json.loads(
        (state_dir / "concept_themes.json").read_text(encoding="utf-8")
    )
    assert themes == []


def test_concept_themes_reraises_non_dependency_runtime_error(
    tmp_path, monkeypatch
):
    orch, state_dir = _make_orch(tmp_path)
    _seed_findings(state_dir)

    import sentinel.orchestrator.main as main_mod

    def _raise_logic(*a, **k):
        raise RuntimeError("clustering produced inconsistent labels")

    monkeypatch.setattr(
        main_mod, "build_concept_themes_from_descriptions", _raise_logic
    )
    with pytest.raises(RuntimeError, match="inconsistent labels"):
        orch._run_concept_themes()


# ---------------------------------------------------------------------------
# sync_pr_status — non-list reviews payload guard (F2-878dfffb3071)
# ---------------------------------------------------------------------------


def _seed_pr_task(state_dir: Path) -> None:
    (state_dir / "tasks.json").write_text(
        json.dumps([
            {
                "task_id": "task-1",
                "plan_id": "FIX-REQUIRED-001",
                "state": "PR_CREATED",
                "pattern_id": "P-1",
                "pr_number": 42,
                "pr_url": "https://github.com/org/repo/pull/42",
                "revision": 0,
            }
        ], indent=2),
        encoding="utf-8",
    )


API = "https://api.github.com/repos/org/repo/pulls/42"


@respx.mock
def test_sync_pr_status_non_list_reviews_degrades_to_pending(
    tmp_path, monkeypatch
):
    orch, state_dir = _make_orch(tmp_path)
    _seed_pr_task(state_dir)
    monkeypatch.setattr(orch, "_github_repo_slug", lambda: "org/repo")
    respx.get(API).mock(return_value=httpx.Response(
        200,
        json={"number": 42, "merged": False, "state": "open",
              "html_url": "https://github.com/org/repo/pull/42"},
    ))
    # Malformed reviews body: a dict instead of the documented array.
    respx.get(f"{API}/reviews").mock(
        return_value=httpx.Response(200, json={"message": "Not Found"})
    )

    with orch:
        result = orch.sync_pr_status("task-1")

    assert result["outcome"] == "PENDING"
    assert "not a list" in result["message"]


@respx.mock
def test_sync_pr_status_skips_non_dict_review_entries(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path)
    _seed_pr_task(state_dir)
    monkeypatch.setattr(orch, "_github_repo_slug", lambda: "org/repo")
    respx.get(API).mock(return_value=httpx.Response(
        200,
        json={"number": 42, "merged": False, "state": "open",
              "html_url": "https://github.com/org/repo/pull/42"},
    ))
    # A list containing a junk entry (str) alongside a valid approval.
    respx.get(f"{API}/reviews").mock(return_value=httpx.Response(200, json=[
        "garbage",
        {"state": "APPROVED", "user": {"login": "alice"},
         "submitted_at": "2026-01-01T00:00:00Z"},
    ]))

    with orch:
        result = orch.sync_pr_status("task-1")

    assert result["outcome"] == "APPROVED"
    assert "alice" in result["message"]


# ---------------------------------------------------------------------------
# _create_pr / _git_push_branch — surface push failure (F2-75b92466a43f)
# ---------------------------------------------------------------------------


class _Task:
    def __init__(self, task_id="task-1"):
        self.task_id = task_id


def test_git_push_branch_returns_false_on_failure(tmp_path, monkeypatch):
    orch, _ = _make_orch(tmp_path)

    def _fail(*a, **k):
        raise subprocess.CalledProcessError(1, "git push", stderr="denied")

    monkeypatch.setattr(subprocess, "run", _fail)
    assert orch._git_push_branch("sentinel/task-1") is False


def test_git_push_branch_returns_true_on_success(tmp_path, monkeypatch):
    orch, _ = _make_orch(tmp_path)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""),
    )
    assert orch._git_push_branch("sentinel/task-1") is True


def test_create_pr_raises_when_push_fails(tmp_path, monkeypatch):
    orch, _ = _make_orch(tmp_path)
    monkeypatch.setattr(orch, "_git_current_branch", lambda: "main")
    monkeypatch.setattr(orch, "_git_push_branch", lambda branch: False)
    with pytest.raises(RuntimeError, match="git push failed"):
        orch._create_pr(_Task())


def test_create_pr_returns_url_when_push_succeeds(tmp_path, monkeypatch):
    orch, _ = _make_orch(tmp_path)
    monkeypatch.setattr(orch, "_git_current_branch", lambda: "main")
    monkeypatch.setattr(orch, "_git_push_branch", lambda branch: True)
    monkeypatch.setattr(orch, "_github_repo_slug", lambda: "org/repo")
    url = orch._create_pr(_Task())
    assert "org/repo" in url
    assert "sentinel/task-1" in url


# ---------------------------------------------------------------------------
# _git_commit_scoped — failed diff probe surfaces (F2-1f872dfa6b02 / e67e945)
# ---------------------------------------------------------------------------


def test_git_commit_scoped_raises_on_probe_failure(tmp_path, monkeypatch):
    orch, _ = _make_orch(tmp_path)

    def _fake_run(cmd, *a, **k):
        # Staging succeeds; the diff --cached probe fails.
        if "diff" in cmd:
            raise subprocess.CalledProcessError(
                128, cmd, stderr="fatal: not a git repository"
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    with pytest.raises(RuntimeError, match="probe staged files"):
        orch._git_commit_scoped(["sentinel/foo.py"], "msg")


# ---------------------------------------------------------------------------
# _extract_and_store_learnings — None on unreadable findings
#   (F2-9fa9c1e5e735 / F2-b0f1d33144ad)
# ---------------------------------------------------------------------------


def test_extract_learnings_returns_none_on_corrupt_findings(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "findings.json").write_text("{not valid json", encoding="utf-8")
    result = _extract_and_store_learnings(state_dir, [tmp_path / "repo"])
    assert result is None


def test_extract_learnings_returns_zero_when_no_findings(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "findings.json").write_text("[]", encoding="utf-8")
    result = _extract_and_store_learnings(state_dir, [tmp_path / "repo"])
    assert result == 0


def test_extract_learnings_returns_zero_when_findings_missing(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    result = _extract_and_store_learnings(state_dir, [tmp_path / "repo"])
    assert result == 0


# ---------------------------------------------------------------------------
# sync_all_prs — ERROR counted separately from still_open (F2-f25783979c8d)
# ---------------------------------------------------------------------------


def test_sync_all_prs_counts_errors_separately(tmp_path, monkeypatch):
    orch, state_dir = _make_orch(tmp_path)
    _seed_pr_task(state_dir)
    monkeypatch.setattr(
        orch, "sync_pr_status",
        lambda task_id: {"task_id": task_id, "outcome": "ERROR",
                         "message": "GitHub API request failed"},
    )
    with orch:
        summary = orch.sync_all_prs()

    assert summary["errors"] == 1
    assert summary["still_open"] == 0
    assert summary["merged"] == 0
    assert summary["rejected"] == 0
