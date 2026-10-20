"""Tests for sentinel.assess.scope_recommender — rule-based scope recommendation.

TDD tests written BEFORE implementation. No LLM calls are involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.assess.scope_recommender import (
    ExcludeRecommendation,
    ScopeRecommendation,
    recommend_scope,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small repo tree with shippable source and non-shippable dirs."""
    root = tmp_path / "repo"

    # Shippable source + tests (must NOT be excluded).
    (root / "sentinel" / "assess").mkdir(parents=True)
    (root / "sentinel" / "assess" / "grouper.py").write_text(
        "def group():\n    return 1\n", encoding="utf-8"
    )
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_grouper.py").write_text(
        "def test_group():\n    assert True\n", encoding="utf-8"
    )

    # docs/ (high) with a nested diagnostic evidence dir (high).
    (root / "docs" / "substep_4a" / "raw_evidence").mkdir(parents=True)
    (root / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (root / "docs" / "substep_4a" / "raw_evidence" / "run_layer2c_llm_probe.py").write_text(
        "print('probe')\n", encoding="utf-8"
    )

    # vendor/ (medium).
    (root / "vendor" / "lib").mkdir(parents=True)
    (root / "vendor" / "lib" / "third.py").write_text("x = 1\n", encoding="utf-8")

    return root


def _find(rec: ScopeRecommendation, name: str) -> ExcludeRecommendation | None:
    for e in rec.recommended_excludes:
        if e.path.rstrip("/").split("/")[-1] == name:
            return e
    return None


def _excluded_names(rec: ScopeRecommendation) -> set[str]:
    return {e.path.rstrip("/").split("/")[-1] for e in rec.recommended_excludes}


def test_returns_scope_recommendation(repo: Path) -> None:
    rec = recommend_scope(str(repo))
    assert isinstance(rec, ScopeRecommendation)
    assert rec.repo_path == str(repo)
    assert rec.total_files >= 1
    assert rec.total_lines >= 1
    assert rec.generated_at  # non-empty ISO timestamp


def test_recommends_excluding_docs(repo: Path) -> None:
    rec = recommend_scope(str(repo))
    docs = _find(rec, "docs")
    assert docs is not None
    assert isinstance(docs, ExcludeRecommendation)
    assert docs.category == "docs"
    assert docs.confidence == "high"
    assert docs.file_count >= 1


def test_raw_evidence_high_confidence_diagnostic(repo: Path) -> None:
    rec = recommend_scope(str(repo))
    raw = _find(rec, "raw_evidence")
    assert raw is not None
    assert raw.category == "diagnostic"
    assert raw.confidence == "high"


def test_does_not_exclude_sentinel(repo: Path) -> None:
    rec = recommend_scope(str(repo))
    assert "sentinel" not in _excluded_names(rec)


def test_does_not_exclude_tests(repo: Path) -> None:
    rec = recommend_scope(str(repo))
    assert "tests" not in _excluded_names(rec)


def test_vendor_medium_confidence(repo: Path) -> None:
    rec = recommend_scope(str(repo))
    vendor = _find(rec, "vendor")
    assert vendor is not None
    assert vendor.category == "vendor"
    assert vendor.confidence == "medium"


def test_engagement_output_dir_classified_high(tmp_path: Path) -> None:
    """A depth-1 dir containing findings.json is engagement_output / high."""
    root = tmp_path / "repo"
    (root / "Tool_Line_run3").mkdir(parents=True)
    (root / "Tool_Line_run3" / "findings.json").write_text("[]", encoding="utf-8")
    # A normal source dir for contrast.
    (root / "sentinel").mkdir(parents=True)
    (root / "sentinel" / "app.py").write_text("x = 1\n", encoding="utf-8")

    rec = recommend_scope(str(root))
    run_dir = _find(rec, "Tool_Line_run3")
    assert run_dir is not None
    assert run_dir.category == "engagement_output"
    assert run_dir.confidence == "high"
    assert "sentinel" not in _excluded_names(rec)


def test_recommended_includes_top_level_source(repo: Path) -> None:
    rec = recommend_scope(str(repo))
    includes = {i.rstrip("/") for i in rec.recommended_includes}
    assert "sentinel" in includes
    assert "tests" in includes
    assert "docs" not in includes
    assert "vendor" not in includes
