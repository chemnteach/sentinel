"""Phase 1 ScanContext wiring tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sentinel.assess.scan_context import ScanContext
from sentinel.config import Config
from sentinel.orchestrator.main import HumanGateRequired, Orchestrator


def _config_for_repo(repo_path: Path) -> Config:
    return Config(
        env={
            "SENTINEL_REPO_PATH": str(repo_path),
            "SENTINEL_LOCAL_ONLY_MODE": "true",
            "SENTINEL_STATE_DIR": "sentinel/state",
        },
        load_dotenv_file=False,
    )


def _run_phase1_with_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest: dict[str, Any],
) -> tuple[Config, Orchestrator, list[tuple[Any, ...]], list[list[str]]]:
    config_repo = tmp_path / "config-repo"
    config_repo.mkdir()
    cfg = _config_for_repo(config_repo)
    events: list[tuple[Any, ...]] = []
    git_commands: list[list[str]] = []

    def fake_git_run(command, **kwargs):
        git_commands.append(list(command))
        # Include a str stdout so library ``subprocess.check_output`` calls made
        # in text mode under this global patch do not crash. Notably torch's
        # import-time ``platform.win32_ver()`` (reached via Stage 4b semantic
        # grouping → embedder load) runs a regex over ``run(...).stdout`` and
        # fails on bytes; an earlier test normally imports torch first so this
        # only surfaces under certain collection orders.
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("sentinel.orchestrator.state_store.subprocess.run", fake_git_run)
    monkeypatch.setattr(
        "sentinel.orchestrator.main.ArchChecker",
        lambda architect, store: SimpleNamespace(run=lambda: []),
    )

    orch = Orchestrator(cfg)
    original_write = orch._store.write
    original_commit_state = orch._store.commit_state

    def write_spy(store_name: str, data: Any, *args, **kwargs) -> None:
        events.append(("write", store_name, kwargs.get("commit", False)))
        original_write(store_name, data, *args, **kwargs)

    def commit_state_spy(message: str) -> None:
        events.append(("commit_state", message))
        original_commit_state(message)

    def fake_cartographer_run(repo_paths: list[Path], restricted_files=None) -> dict[str, Any]:
        orch._store.write("repositories", manifest.get("repositories", []))
        orch._store.write("modules", manifest.get("modules", []))
        return manifest

    monkeypatch.setattr(orch._store, "write", write_spy)
    monkeypatch.setattr(orch._store, "commit_state", commit_state_spy)
    monkeypatch.setattr(orch._cartographer, "run", fake_cartographer_run)
    monkeypatch.setattr(orch._dependency_analyzer, "run", lambda: None)
    monkeypatch.setattr(orch, "_run_additional_phase1_assessments", lambda synthesis: [])

    try:
        orch.run_phase1([config_repo])
    except HumanGateRequired as gate:
        assert gate.gate_type == "tech_debt_triage"
    else:
        pytest.fail("Expected Phase 1 to stop at tech debt triage gate")

    orch.close()

    return cfg, orch, events, git_commands


def test_phase1_writes_scan_metadata_before_existing_stage1_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_repo = tmp_path / "manifest-repo"
    manifest_repo.mkdir()
    manifest = {
        "repositories": [{"name": "manifest-repo", "path": str(manifest_repo)}],
        "modules": [],
    }

    cfg, _orch, events, git_commands = _run_phase1_with_manifest(
        monkeypatch,
        tmp_path,
        manifest,
    )

    scan_metadata_path = cfg.state_dir / "scan_metadata.json"
    assert scan_metadata_path.exists()
    scan_metadata_write_index = events.index(("write", "scan_metadata", False))
    stage1_commit_index = events.index(("commit_state", "sentinel: Stage 1 complete"))
    assert ("write", "scan_metadata", True) not in events
    assert scan_metadata_write_index < stage1_commit_index
    assert ["git", "commit", "-m", "sentinel: Stage 1 complete"] in git_commands


def test_phase1_scan_metadata_records_model_and_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_repo = tmp_path / "manifest-repo"
    manifest_repo.mkdir()
    manifest = {
        "repositories": [{"name": "manifest-repo", "path": str(manifest_repo)}],
        "modules": [],
    }

    cfg, _orch, _events, _git = _run_phase1_with_manifest(
        monkeypatch,
        tmp_path,
        manifest,
    )

    scan_metadata = json.loads(
        (cfg.state_dir / "scan_metadata.json").read_text(encoding="utf-8")
    )
    # The review model + provider are recorded so a run's baseline is verifiable
    # after the fact (they were previously unrecorded).
    assert scan_metadata["model"] == cfg.model
    assert scan_metadata["provider"] == cfg.provider



@pytest.mark.parametrize(
    ("repositories", "expected_from_manifest"),
    [
        ([{"name": "manifest-repo", "path": "{manifest_repo}"}], True),
        ([], False),
    ],
)
def test_phase1_scan_metadata_matches_manifest_target_root_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repositories: list[dict[str, str]],
    expected_from_manifest: bool,
) -> None:
    manifest_repo = tmp_path / "manifest-repo"
    manifest_repo.mkdir()
    resolved_repositories = [
        {**repo, "path": repo["path"].format(manifest_repo=manifest_repo)}
        for repo in repositories
    ]
    manifest = {"repositories": resolved_repositories, "modules": []}

    cfg, _orch, _events, _git_commands = _run_phase1_with_manifest(
        monkeypatch,
        tmp_path,
        manifest,
    )

    expected_root = manifest_repo if expected_from_manifest else cfg.repo_path
    scan_metadata_path = cfg.state_dir / "scan_metadata.json"
    persisted_metadata = json.loads(scan_metadata_path.read_text(encoding="utf-8"))

    assert scan_metadata_path.exists()
    assert ScanContext.from_metadata(persisted_metadata) == ScanContext.from_repo_root(
        expected_root
    )


def test_phase1_review_orchestrator_receives_scan_context_from_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_repo = tmp_path / "manifest-repo"
    manifest_repo.mkdir()
    manifest = {
        "repositories": [{"name": "manifest-repo", "path": str(manifest_repo)}],
        "modules": [],
    }

    cfg, orch, _events, _git_commands = _run_phase1_with_manifest(
        monkeypatch,
        tmp_path,
        manifest,
    )

    expected_context = ScanContext.from_repo_root(manifest_repo)
    config_context = ScanContext.from_repo_root(cfg.repo_path)
    assert orch._reviewer is not None
    assert orch._reviewer._scan_context == expected_context
    assert orch._reviewer._scan_context != config_context


# ---------------------------------------------------------------------------
# Required-assessment completion gate: transient transport failures are
# tolerated (Phase 1 not blocked); genuine failures still hard-fail.
# ---------------------------------------------------------------------------

def _run_phase1_with_analysis_pack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    analysis_pack: list[dict[str, Any]],
):
    """Drive run_phase1 with a fixed additional-assessment analysis_pack.

    Returns the raised exception (or ``None`` if Phase 1 reached the triage
    gate, i.e. the assessment failure was tolerated).
    """
    config_repo = tmp_path / "config-repo"
    config_repo.mkdir()
    cfg = _config_for_repo(config_repo)

    def fake_git_run(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("sentinel.orchestrator.state_store.subprocess.run", fake_git_run)
    monkeypatch.setattr(
        "sentinel.orchestrator.main.ArchChecker",
        lambda architect, store: SimpleNamespace(run=lambda: []),
    )

    manifest = {
        "repositories": [{"name": "config-repo", "path": str(config_repo)}],
        "modules": [],
    }

    orch = Orchestrator(cfg)

    def fake_cartographer_run(repo_paths, restricted_files=None):
        orch._store.write("repositories", manifest["repositories"])
        orch._store.write("modules", manifest["modules"])
        return manifest

    monkeypatch.setattr(orch._cartographer, "run", fake_cartographer_run)
    monkeypatch.setattr(orch._dependency_analyzer, "run", lambda: None)
    monkeypatch.setattr(
        orch, "_run_additional_phase1_assessments", lambda synthesis: analysis_pack
    )

    raised: Exception | None = None
    try:
        orch.run_phase1([config_repo])
    except HumanGateRequired:
        raised = None  # reached the triage gate → tolerated
    except RuntimeError as exc:
        raised = exc
    finally:
        orch.close()
    return raised


def test_required_assessment_transient_timeout_is_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway-timeout on a required batch assessment must NOT discard an
    otherwise-complete Phase 1 — it reaches the triage gate."""
    pack = [
        {
            "assessment_id": "operational_risk_failure_modes",
            "title": "Operational Risk and Failure Modes",
            "skill": "layer2j-operational-risk-failure-modes",
            "required": True,
            "status": "error",
            "summary": "assessment failed during execution",
            "findings": [],
            "error": "Model response is not valid JSON:\nContent: Error: Request timed out after 4 attempts",
            "error_type": "APIError",
        },
    ]
    raised = _run_phase1_with_analysis_pack(monkeypatch, tmp_path, pack)
    assert raised is None  # tolerated — reached triage gate


def test_required_assessment_genuine_failure_still_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine (non-transport) required-assessment failure must still
    hard-fail Phase 1 completion."""
    pack = [
        {
            "assessment_id": "config_environment_drift",
            "title": "Config and Environment Drift",
            "skill": "layer2g-config-environment-drift",
            "required": True,
            "status": "error",
            "summary": "assessment failed during execution",
            "findings": [],
            "error": "Schema validation failed: missing required key 'findings'",
            "error_type": "ValueError",
        },
    ]
    raised = _run_phase1_with_analysis_pack(monkeypatch, tmp_path, pack)
    assert isinstance(raised, RuntimeError)
    assert "config_environment_drift" in str(raised)