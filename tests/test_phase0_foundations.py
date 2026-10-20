from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sentinel.assess.scan_context import ScanContext
from sentinel.config import Config
from sentinel.orchestrator.state_store import StateStore
from sentinel.orchestrator.quality_artifacts import (
    empty_reconciliation_report,
    empty_quality_scorecard,
)


def test_config_reads_phase0_feature_flags() -> None:
    env = {
        "SENTINEL_REPO_PATH": "/tmp/repo",
        "SENTINEL_ENABLE_DETERMINISTIC_LANE": "true",
        "SENTINEL_ENABLE_RETRIEVAL_REASONING_LANE": "true",
        "SENTINEL_ENABLE_RECONCILIATION": "true",
        "SENTINEL_ENABLE_QUALITY_SCORECARD": "true",
        "SENTINEL_SHADOW_COMPARE_MODE": "true",
        "SENTINEL_ENFORCE_FULL_SPECIALIST_COVERAGE": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    assert cfg.enable_deterministic_lane is True
    assert cfg.enable_retrieval_reasoning_lane is True
    assert cfg.enable_reconciliation is True
    assert cfg.enable_quality_scorecard is True
    assert cfg.shadow_compare_mode is True
    assert cfg.enforce_full_specialist_coverage is True


def test_state_store_supports_phase0_foundation_stores(tmp_path: Path) -> None:
    store = StateStore(state_dir=tmp_path / "state", repo_path=tmp_path)

    assert store.read("deterministic_findings") == []
    assert store.read("retrieval_manifest") == []
    assert store.read("reasoning_findings") == []
    assert store.read("finding_conflicts") == []
    assert store.read("reconciliation_report") == {}
    assert store.read("quality_scorecard") == {}


def test_state_store_scan_metadata_defaults_to_empty_dict(tmp_path: Path) -> None:
    store = StateStore(state_dir=tmp_path / "sentinel", repo_path=tmp_path)

    assert store.read("scan_metadata") == {}


def test_state_store_scan_metadata_round_trip(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    store = StateStore(state_dir=tmp_path / "sentinel", repo_path=tmp_path)
    metadata = ScanContext.from_repo_root(repo_root).to_metadata()

    store.write("scan_metadata", metadata)

    assert store.read("scan_metadata") == metadata


def test_state_store_scan_metadata_file_lands_at_registered_path_after_commit_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / "sentinel"
    store = StateStore(state_dir=state_dir, repo_path=tmp_path)
    metadata = {
        "repo_root_path": (tmp_path / "repo").as_posix(),
        "repo_root_canonical": "tmp/repo",
    }
    git_commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        git_commands.append(list(command))
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("sentinel.orchestrator.state_store.subprocess.run", fake_run)

    store.write("scan_metadata", metadata)
    store.commit_state("sentinel: Stage 1 complete")

    scan_metadata_path = state_dir / "scan_metadata.json"
    assert scan_metadata_path.exists()
    assert store.read("scan_metadata") == metadata
    assert git_commands[0] == ["git", "add", str(state_dir)]


def test_quality_artifact_empty_builders_shape() -> None:
    reconciliation = empty_reconciliation_report()
    scorecard = empty_quality_scorecard()

    assert reconciliation["summary"]["resolved_count"] == 0
    assert reconciliation["summary"]["unresolved_count"] == 0

    assert scorecard["confidence_label"] == "incomplete"
    assert scorecard["metrics"]["module_completeness_pct"] == 0.0
    assert scorecard["metrics"]["evidence_completeness_pct"] == 0.0
