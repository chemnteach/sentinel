"""Tests for chunk count cap per module (Fix 3: 0c)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from sentinel.assess.chunker import Chunk, Chunker
from sentinel.assess.reviewer import ReviewOrchestrator
from sentinel.assess.scan_context import ScanContext
from sentinel.config import Config


def _make_chunks(n: int) -> list[Chunk]:
    """Create *n* dummy Chunk objects."""
    return [
        Chunk(
            file_path="mod.py",
            chunk_index=i,
            total_chunks=n,
            start_line=i * 10 + 1,
            end_line=(i + 1) * 10,
            content=f"# chunk {i}\npass\n",
        )
        for i in range(n)
    ]


def _build_orchestrator(config: Config) -> ReviewOrchestrator:
    """Build a ReviewOrchestrator with mocked agent and store."""
    architect = MagicMock()
    # quick_review returns empty findings so specialist loop is skipped
    architect.quick_review.return_value = {"findings": []}
    architect.prioritize_review.return_value = {
        "recommended_skills": [],
        "criticality": "low",
    }
    store = MagicMock()
    return ReviewOrchestrator(
        architect=architect,
        store=store,
        config=config,
        scan_context=ScanContext.from_repo_root(config.repo_path),
    )


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


def test_chunks_capped_at_default() -> None:
    """More than 200 chunks should be truncated to the default cap of 200."""
    config = Config(env={"SENTINEL_LOCAL_ONLY_MODE": "true"}, load_dotenv_file=False)
    orch = _build_orchestrator(config)

    fake_chunks = _make_chunks(500)
    with patch.object(orch._chunker, "chunk_string", return_value=fake_chunks):
        orch.review_module(
            module={"name": "big_module"},
            source_code="x = 1",
            manifest={},
        )

    # quick_review should have been called exactly 200 times (default cap)
    assert orch._architect.quick_review.call_count == 200


def test_chunks_under_cap_unchanged() -> None:
    """Fewer than 200 chunks should pass through without truncation."""
    config = Config(env={"SENTINEL_LOCAL_ONLY_MODE": "true"}, load_dotenv_file=False)
    orch = _build_orchestrator(config)

    fake_chunks = _make_chunks(50)
    with patch.object(orch._chunker, "chunk_string", return_value=fake_chunks):
        orch.review_module(
            module={"name": "small_module"},
            source_code="x = 1",
            manifest={},
        )

    assert orch._architect.quick_review.call_count == 50


def test_cap_configurable_via_env() -> None:
    """SENTINEL_MAX_CHUNKS env var should override the default cap."""
    config = Config(
        env={"SENTINEL_LOCAL_ONLY_MODE": "true", "SENTINEL_MAX_CHUNKS": "25"},
        load_dotenv_file=False,
    )
    orch = _build_orchestrator(config)

    fake_chunks = _make_chunks(100)
    with patch.object(orch._chunker, "chunk_string", return_value=fake_chunks):
        orch.review_module(
            module={"name": "capped_module"},
            source_code="x = 1",
            manifest={},
        )

    assert orch._architect.quick_review.call_count == 25


def test_cap_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Truncation should emit a WARNING log message."""
    config = Config(
        env={"SENTINEL_LOCAL_ONLY_MODE": "true", "SENTINEL_MAX_CHUNKS": "10"},
        load_dotenv_file=False,
    )
    orch = _build_orchestrator(config)

    fake_chunks = _make_chunks(50)
    with (
        patch.object(orch._chunker, "chunk_string", return_value=fake_chunks),
        caplog.at_level(logging.WARNING, logger="sentinel.assess.reviewer"),
    ):
        orch.review_module(
            module={"name": "warn_module"},
            source_code="x = 1",
            manifest={},
        )

    assert any(
        "50 chunks" in msg and "capping at 10" in msg
        for msg in caplog.messages
    ), f"Expected capping warning in logs, got: {caplog.messages}"


def test_strict_quality_mode_disables_chunk_cap() -> None:
    """Strict mode should review every chunk instead of truncating at max cap."""
    config = Config(
        env={
            "SENTINEL_LOCAL_ONLY_MODE": "true",
            "SENTINEL_STRICT_QUALITY_MODE": "true",
            "SENTINEL_MAX_CHUNKS": "10",
        },
        load_dotenv_file=False,
    )
    orch = _build_orchestrator(config)

    fake_chunks = _make_chunks(50)
    with patch.object(orch._chunker, "chunk_string", return_value=fake_chunks):
        orch.review_module(
            module={"name": "strict_module"},
            source_code="x = 1",
            manifest={},
        )

    assert orch._architect.quick_review.call_count == 50
