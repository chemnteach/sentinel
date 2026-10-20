"""Tests for orchestrator learning extraction integration.

Covers:
  - _extract_and_store_learnings method on Orchestrator
  - Config flags for auto-extraction and learnings path
  - Empty findings edge case
  - authored_by_foundry flag propagation
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sentinel.config import Config
from sentinel.learnings.store import LearningStore


# ── Helpers ──────────────────────────────────────────────────────────


def _make_config(tmp_path: Path, **overrides) -> Config:
    """Create a Config with test-friendly defaults."""
    env = {
        "SENTINEL_REPO_PATH": str(tmp_path),
        "SENTINEL_STATE_DIR": str(tmp_path / "sentinel" / "state"),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_AUTO_EXTRACT_LEARNINGS": "true",
        "SENTINEL_AUTHORED_BY_FOUNDRY": "false",
        "SENTINEL_LEARNINGS_PATH": "",
        "OPENAI_API_KEY": "test-key",
    }
    env.update(overrides)
    return Config(env=env, load_dotenv_file=False)


def _seed_findings(state_dir: Path, findings: list[dict] | None = None) -> list[dict]:
    """Write findings to the state directory."""
    state_dir.mkdir(parents=True, exist_ok=True)
    if findings is None:
        findings = [
            {
                "id": "F-001",
                "severity": "P1",
                "category": "error_handling",
                "description": "Bare except swallows errors in handler A",
                "skill": "layer3d-error-handling-specialist",
            },
            {
                "id": "F-002",
                "severity": "P1",
                "category": "error_handling",
                "description": "Bare except swallows errors in handler B",
                "skill": "layer3d-error-handling-specialist",
            },
        ]
    (state_dir / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
    return findings


# ── Tests ────────────────────────────────────────────────────────────


class TestOrchestratorLearningExtraction:
    """Tests for the Orchestrator._extract_and_store_learnings integration."""

    def test_extract_and_store_learnings(self, tmp_path: Path) -> None:
        """Extracts learnings from findings and stores them."""
        cfg = _make_config(tmp_path)
        state_dir = Path(cfg.state_dir)
        _seed_findings(state_dir)

        # Import the extraction function directly to avoid full Orchestrator init
        from sentinel.orchestrator.main import _extract_and_store_learnings

        count = _extract_and_store_learnings(
            state_dir=state_dir,
            repo_paths=[tmp_path],
            authored_by_foundry=False,
            learnings_path=None,
        )
        assert count >= 1

        # Verify files were created
        learnings_dir = state_dir / "learnings"
        store = LearningStore(learnings_dir)
        all_learnings = store.load_all()
        assert len(all_learnings) >= 1

    def test_extract_learnings_empty(self, tmp_path: Path) -> None:
        """No findings produces 0 learnings."""
        cfg = _make_config(tmp_path)
        state_dir = Path(cfg.state_dir)
        _seed_findings(state_dir, findings=[])

        from sentinel.orchestrator.main import _extract_and_store_learnings

        count = _extract_and_store_learnings(
            state_dir=state_dir,
            repo_paths=[tmp_path],
            authored_by_foundry=False,
            learnings_path=None,
        )
        assert count == 0

    def test_extract_learnings_authored_by_foundry(self, tmp_path: Path) -> None:
        """authored_by_foundry flag propagates to extracted learnings."""
        cfg = _make_config(tmp_path)
        state_dir = Path(cfg.state_dir)
        _seed_findings(state_dir)

        from sentinel.orchestrator.main import _extract_and_store_learnings

        count = _extract_and_store_learnings(
            state_dir=state_dir,
            repo_paths=[tmp_path],
            authored_by_foundry=True,
            learnings_path=None,
        )
        assert count >= 1

        learnings_dir = state_dir / "learnings"
        store = LearningStore(learnings_dir)
        for learning in store.load_all():
            assert learning.authored_by_foundry is True

    def test_auto_extract_config_enabled(self, tmp_path: Path) -> None:
        """When SENTINEL_AUTO_EXTRACT_LEARNINGS=true, config flag is True."""
        cfg = _make_config(tmp_path, SENTINEL_AUTO_EXTRACT_LEARNINGS="true")
        assert cfg.auto_extract_learnings is True

    def test_auto_extract_config_disabled(self, tmp_path: Path) -> None:
        """When SENTINEL_AUTO_EXTRACT_LEARNINGS=false, config flag is False."""
        cfg = _make_config(tmp_path, SENTINEL_AUTO_EXTRACT_LEARNINGS="false")
        assert cfg.auto_extract_learnings is False

    def test_learnings_path_from_config(self, tmp_path: Path) -> None:
        """Uses configured learnings path when set."""
        custom_path = str(tmp_path / "custom_learnings")
        cfg = _make_config(tmp_path, SENTINEL_LEARNINGS_PATH=custom_path)
        state_dir = Path(cfg.state_dir)
        _seed_findings(state_dir)

        from sentinel.orchestrator.main import _extract_and_store_learnings

        count = _extract_and_store_learnings(
            state_dir=state_dir,
            repo_paths=[tmp_path],
            authored_by_foundry=False,
            learnings_path=Path(custom_path),
        )
        assert count >= 1

        store = LearningStore(Path(custom_path))
        assert len(store.load_all()) >= 1

    def test_learnings_default_path(self, tmp_path: Path) -> None:
        """Uses state_dir/learnings when no custom path is configured."""
        cfg = _make_config(tmp_path, SENTINEL_LEARNINGS_PATH="")
        state_dir = Path(cfg.state_dir)
        _seed_findings(state_dir)

        from sentinel.orchestrator.main import _extract_and_store_learnings

        count = _extract_and_store_learnings(
            state_dir=state_dir,
            repo_paths=[tmp_path],
            authored_by_foundry=False,
            learnings_path=None,
        )
        assert count >= 1

        # Default path is state_dir / "learnings"
        default_learnings_dir = state_dir / "learnings"
        store = LearningStore(default_learnings_dir)
        assert len(store.load_all()) >= 1
