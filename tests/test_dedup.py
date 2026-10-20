"""Tests for sentinel.assess.dedup — deduplication pipeline."""

from __future__ import annotations

import copy
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sentinel.assess.dedup import deduplicate
from sentinel.assess.scan_context import ScanContext
from sentinel.skills.loader import Skills


# ── Helpers ──────────────────────────────────────────────────────────


def _make_finding(**overrides) -> dict:
    """Create a finding dict with sensible defaults."""
    base = {
        "id": "F-abc123",
        "module": "backend",
        "skill": "layer2b-correctness-specialist",
        "severity": "P2",
        "category": "encoding",
        "file": "app.py",
        "function": "save_data",
        "line_range": "42-50",
        "location": "app.py:42-50 save_data()",
        "description": "Missing encoding='utf-8' on write_text() call",
        "suggested_fix": "Add encoding parameter",
        "status": "open",
    }
    base.update(overrides)
    return base


# ── Exact Dedup Tests ────────────────────────────────────────────────


class TestExactDedup:
    def test_exact_duplicate_removed(self):
        """Two findings with same file+line_range+description -> 1 finding."""
        f1 = _make_finding(skill="layer2b-correctness-specialist")
        f2 = _make_finding(skill="layer3a-security-specialist")
        result = deduplicate([f1, f2])
        assert len(result) == 1

    def test_exact_duplicate_merges_layers(self):
        """Merged finding has layers list with both skills."""
        f1 = _make_finding(skill="layer2b-correctness-specialist")
        f2 = _make_finding(skill="layer3a-security-specialist")
        result = deduplicate([f1, f2])
        assert len(result) == 1
        layers = result[0]["layers"]
        assert "layer2b-correctness-specialist" in layers
        assert "layer3a-security-specialist" in layers

    def test_exact_duplicate_keeps_higher_severity(self):
        """P2 + Critical -> Critical."""
        f1 = _make_finding(severity="P2", skill="layer2b-correctness-specialist")
        f2 = _make_finding(severity="Critical", skill="layer3a-security-specialist")
        result = deduplicate([f1, f2])
        assert len(result) == 1
        assert result[0]["severity"] == "Critical"

    def test_no_duplicates_unchanged(self):
        """Unique findings pass through unchanged."""
        f1 = _make_finding(file="app.py", description="Issue A")
        f2 = _make_finding(file="utils.py", description="Issue B")
        result = deduplicate([f1, f2])
        assert len(result) == 2

    def test_different_descriptions_not_deduped(self):
        """Same file but different descriptions and categories stay separate."""
        f1 = _make_finding(
            category="encoding",
            description="Missing encoding on write",
        )
        f2 = _make_finding(
            category="error_handling",
            description="Missing error handling on read",
        )
        result = deduplicate([f1, f2])
        assert len(result) == 2


# ── Semantic Dedup Tests ─────────────────────────────────────────────


class TestSemanticDedup:
    def test_semantic_dedup_same_file_function_category(self):
        """Same file+function+category from different layers -> consolidated."""
        f1 = _make_finding(
            skill="layer2b-correctness-specialist",
            description="Missing encoding on write_text()",
            line_range="42-50",
        )
        f2 = _make_finding(
            skill="layer3b-maintainability-specialist",
            description="Encoding not specified in write call",
            line_range="42-55",
        )
        result = deduplicate([f1, f2])
        assert len(result) == 1

    def test_semantic_dedup_different_categories_kept(self):
        """Same file+function but different categories stay separate."""
        f1 = _make_finding(category="encoding", description="Encoding issue")
        f2 = _make_finding(category="error_handling", description="Error handling issue")
        result = deduplicate([f1, f2])
        assert len(result) == 2

    def test_semantic_dedup_consolidated_from_count(self):
        """Consolidated finding has consolidated_from field."""
        f1 = _make_finding(
            skill="layer2b-correctness-specialist",
            description="Missing encoding on write",
            line_range="42-50",
        )
        f2 = _make_finding(
            skill="layer3b-maintainability-specialist",
            description="Encoding not specified in write call",
            line_range="42-55",
        )
        result = deduplicate([f1, f2])
        assert len(result) == 1
        assert result[0]["consolidated_from"] == 2

    def test_semantic_dedup_layers_merged(self):
        """All contributing layers listed."""
        f1 = _make_finding(
            skill="layer2b-correctness-specialist",
            description="Missing encoding on write",
            line_range="42-50",
        )
        f2 = _make_finding(
            skill="layer3b-maintainability-specialist",
            description="Encoding not specified in write call",
            line_range="42-55",
        )
        result = deduplicate([f1, f2])
        layers = result[0]["layers"]
        assert "layer2b-correctness-specialist" in layers
        assert "layer3b-maintainability-specialist" in layers

    def test_semantic_dedup_highest_severity_wins(self):
        """Group severity = best severity."""
        f1 = _make_finding(
            severity="P3",
            skill="layer2b-correctness-specialist",
            description="Minor encoding note",
            line_range="42-50",
        )
        f2 = _make_finding(
            severity="P1",
            skill="layer3b-maintainability-specialist",
            description="Critical encoding deficiency in write path",
            line_range="42-55",
        )
        result = deduplicate([f1, f2])
        assert result[0]["severity"] == "P1"

    def test_semantic_dedup_longest_description_kept(self):
        """Primary finding has the most detailed description."""
        short_desc = "Encoding issue"
        long_desc = "Missing encoding='utf-8' parameter on write_text() call which can corrupt non-ASCII data"
        f1 = _make_finding(
            skill="layer2b-correctness-specialist",
            description=short_desc,
            line_range="42-50",
        )
        f2 = _make_finding(
            skill="layer3b-maintainability-specialist",
            description=long_desc,
            line_range="42-55",
        )
        result = deduplicate([f1, f2])
        assert result[0]["description"] == long_desc

    def test_findings_without_location_not_consolidated(self):
        """Findings with empty file+function get unique keys."""
        f1 = _make_finding(
            file="",
            function="",
            description="General architecture note A",
            line_range="",
            location="",
        )
        f2 = _make_finding(
            file="",
            function="",
            description="General architecture note B",
            line_range="",
            location="",
        )
        result = deduplicate([f1, f2])
        # They have different descriptions so exact dedup won't merge them.
        # Semantic dedup should NOT group them either since no location.
        assert len(result) == 2


# ── Integration Tests ────────────────────────────────────────────────


class TestPipelineIntegration:
    def test_full_pipeline_exact_then_semantic(self):
        """Exact dedup runs first, then semantic."""
        # Three findings: f1 and f2 are exact duplicates.
        # After exact dedup, the merged f1/f2 and f3 share file+function+category
        # so semantic dedup consolidates them.
        f1 = _make_finding(
            skill="layer2b-correctness-specialist",
            description="Missing encoding on write_text()",
            line_range="42-50",
        )
        f2 = _make_finding(
            skill="layer3a-security-specialist",
            description="Missing encoding on write_text()",
            line_range="42-50",
        )
        f3 = _make_finding(
            skill="layer4a-code-smell-detector",
            description="Encoding parameter omitted in file write operation",
            line_range="42-55",
        )
        result = deduplicate([f1, f2, f3])
        assert len(result) == 1
        layers = result[0]["layers"]
        assert "layer2b-correctness-specialist" in layers
        assert "layer3a-security-specialist" in layers
        assert "layer4a-code-smell-detector" in layers

    def test_empty_list_returns_empty(self):
        """Edge case: empty input."""
        assert deduplicate([]) == []

    def test_single_finding_unchanged(self):
        """Edge case: single finding passes through."""
        f = _make_finding()
        result = deduplicate([f])
        assert len(result) == 1
        assert result[0]["description"] == f["description"]
        assert result[0]["layers"] == [f["skill"]]


# ── Registry-Driven Dedup Tests ─────────────────────────────────────


class TestRegistryDrivenDedup:
    @staticmethod
    def _registry_finding(**overrides) -> dict:
        base = _make_finding(
            file="registry.py",
            function="emit",
            line_range="10-20",
            location="registry.py:10-20 emit()",
            category="registry",
        )
        base.update(
            {
                "subject_key": "external_call:dep:site",
                "finding_kind": "timeout_handling_gap",
            }
        )
        base.update(overrides)
        return base

    def test_table_2_peer_consolidation(self):
        f1 = self._registry_finding(
            skill="layer3d-error-handling-specialist",
            finding_kind="error_signal_loss",
            subject_key="external_call:dep:site",
            description="Layer3d surface description",
        )
        f2 = self._registry_finding(
            skill="layer2d-data-integrity-specialist",
            finding_kind="silent_data_loss",
            subject_key="external_call:dep:site",
            description="Layer2d durable-write description",
        )

        result = deduplicate([f1, f2])

        assert len(result) == 1
        assert result[0]["consolidated_from"] == 2
        assert set(result[0]["layers"]) == {
            "layer3d-error-handling-specialist",
            "layer2d-data-integrity-specialist",
        }

    def test_quick_review_vs_specialist_consolidation(self):
        f1 = self._registry_finding(
            skill="layer2a-quick-review",
            finding_kind="missing_transaction_boundary",
            subject_key="transaction_boundary:save_data",
            description="Quick review finding",
            file="quick.py",
        )
        f2 = self._registry_finding(
            skill="layer2d-data-integrity-specialist",
            finding_kind="missing_transaction_boundary",
            subject_key="transaction_boundary:save_data",
            description="Specialist finding",
            file="specialist.py",
        )

        result = deduplicate([f1, f2])

        assert len(result) == 1
        assert result[0]["consolidated_from"] == 2
        assert set(result[0]["layers"]) == {
            "layer2a-quick-review",
            "layer2d-data-integrity-specialist",
        }

    def test_intra_emitter_dedup_with_disambiguation(self):
        f1 = self._registry_finding(
            skill="layer2d-data-integrity-specialist",
            finding_kind="missing_transaction_boundary",
            subject_key="transaction_boundary:save_data",
            description="First data-integrity finding",
            file="one.py",
        )
        f2 = self._registry_finding(
            skill="layer2d-data-integrity-specialist",
            finding_kind="missing_transaction_boundary",
            subject_key="transaction_boundary:save_data",
            description="Second data-integrity finding",
            file="two.py",
        )

        result = deduplicate([f1, f2])

        assert len(result) == 1
        assert result[0]["consolidated_from"] == 2
        assert result[0]["layers"] == ["layer2d-data-integrity-specialist"]

    def test_migration_token_collision_stays_separate(self):
        f1 = _make_finding(
            skill="layer2d-data-integrity-specialist",
            finding_kind="migration_hazard",
            subject_key="migration:alpha:step-1",
            file="migrate_a.py",
            function="apply",
            category="migration",
            description="Layer2d migration hazard",
        )
        f2 = _make_finding(
            skill="layer2i-change-impact-testability",
            finding_kind="change_verification_gap",
            subject_key="migration:alpha:step-1",
            file="migrate_b.py",
            function="apply",
            category="migration",
            description="Layer2i change verification gap",
        )

        result = deduplicate([f1, f2])

        assert len(result) == 2
        assert {finding["skill"] for finding in result} == {
            "layer2d-data-integrity-specialist",
            "layer2i-change-impact-testability",
        }

    def test_canonical_emitter_primary_selection(self):
        short = self._registry_finding(
            skill="layer2d-data-integrity-specialist",
            finding_kind="missing_transaction_boundary",
            subject_key="transaction_boundary:save_data",
            description="Short specialist description",
            file="short.py",
        )
        long = self._registry_finding(
            skill="layer2d-data-integrity-specialist",
            finding_kind="missing_transaction_boundary",
            subject_key="transaction_boundary:save_data",
            description=(
                "Longer specialist description that should win because the "
                "canonical emitter is present and it has the most detailed text"
            ),
            file="long.py",
        )
        quick = self._registry_finding(
            skill="layer2a-quick-review",
            finding_kind="missing_transaction_boundary",
            subject_key="transaction_boundary:save_data",
            description="Quick review description",
            file="quick.py",
        )

        result = deduplicate([short, long, quick])

        assert len(result) == 1
        assert result[0]["skill"] == "layer2d-data-integrity-specialist"
        assert result[0]["description"] == long["description"]
        assert result[0]["consolidated_from"] == 3

    def test_preserve_both_with_attribution_keeps_two_records(self):
        f1 = self._registry_finding(
            skill="layer3d-error-handling-specialist",
            finding_kind="timeout_handling_gap",
            subject_key="external_call:dep:site",
            description="Layer3d preserve-both record",
            file="left.py",
        )
        f2 = self._registry_finding(
            skill="layer2j-operational-risk-failure-modes",
            finding_kind="timeout_handling_gap",
            subject_key="external_call:dep:site",
            description="Layer2j preserve-both record",
            file="right.py",
        )

        result = deduplicate([f1, f2])

        assert len(result) == 2
        assert {finding["skill"] for finding in result} == {
            "layer3d-error-handling-specialist",
            "layer2j-operational-risk-failure-modes",
        }
        assert all(finding["consolidated_from"] == 1 for finding in result)
        assert len({finding["root_cause_id"] for finding in result}) == 2

    def test_legacy_fallback_without_subject_key_preserves_current_behavior(self):
        f1 = _make_finding(
            skill="layer2b-correctness-specialist",
            description="Missing encoding on write",
        )
        f2 = _make_finding(
            skill="layer3b-maintainability-specialist",
            description="Encoding not specified in write call",
            file="app.py",
            function="save_data",
            category="encoding",
        )

        result = deduplicate([f1, f2])

        assert len(result) == 1
        assert result[0]["consolidated_from"] == 2
        assert set(result[0]["layers"]) == {
            "layer2b-correctness-specialist",
            "layer3b-maintainability-specialist",
        }

    def test_mixed_registry_and_legacy_batch_consolidates_both_paths(self):
        registry_a = self._registry_finding(
            skill="layer3d-error-handling-specialist",
            finding_kind="error_signal_loss",
            subject_key="external_call:dep:site",
            description="Registry path A",
            file="registry_a.py",
        )
        registry_b = self._registry_finding(
            skill="layer2d-data-integrity-specialist",
            finding_kind="silent_data_loss",
            subject_key="external_call:dep:site",
            description="Registry path B",
            file="registry_b.py",
        )
        legacy_a = _make_finding(
            skill="layer2b-correctness-specialist",
            description="Legacy path A",
            file="legacy.py",
            function="save_data",
            category="encoding",
        )
        legacy_b = _make_finding(
            skill="layer3b-maintainability-specialist",
            description="Legacy path B",
            file="legacy.py",
            function="save_data",
            category="encoding",
        )

        result = deduplicate([registry_a, registry_b, legacy_a, legacy_b])

        assert len(result) == 2
        assert sorted(finding["consolidated_from"] for finding in result) == [2, 2]


# ── Reviewer Integration Tests ──────────────────────────────────────


class TestReviewerDedupIntegration:
    """Tests that reviewer.py calls deduplicate before persisting findings."""

    def test_review_module_calls_dedup_before_persist(self):
        """review_module should run dedup on findings before appending to store."""
        from sentinel.assess.reviewer import ReviewOrchestrator
        from sentinel.config import Config

        # Build minimal config
        config = MagicMock(spec=Config)
        config.repo_path = Path.cwd()
        config.chunk_max_lines = 500
        config.chunk_overlap_lines = 50
        config.max_chunks_per_module = 10

        store = MagicMock()
        architect = MagicMock()

        # architect.prioritize_review returns minimal result
        architect.prioritize_review.return_value = {
            "recommended_skills": [],
            "criticality": "low",
        }
        # architect.quick_review returns duplicate findings
        architect.quick_review.return_value = {
            "findings": [
                {
                    "description": "Missing encoding on write_text()",
                    "severity": "P2",
                    "location": "app.py:42-50 save_data()",
                    "finding_kind": "logic_bug",
                },
                {
                    "description": "Missing encoding on write_text()",
                    "severity": "P2",
                    "location": "app.py:42-50 save_data()",
                    "finding_kind": "logic_bug",
                },
            ],
        }

        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=ScanContext.from_repo_root(config.repo_path),
        )
        module = {"name": "backend", "path": "backend/", "files": ["app.py"]}
        source_code = "def save_data(): pass"
        manifest = {"modules": [module]}

        with patch(
            "sentinel.assess.reviewer.deduplicate", wraps=deduplicate
        ) as mock_dedup:
            findings = orchestrator.review_module(module, source_code, manifest)
            mock_dedup.assert_called_once()

        # The two identical findings should have been deduped to one
        assert len(findings) == 1
        # Store should have received only the deduped finding(s)
        assert store.append_finding.call_count == 1

    def test_review_module_dedup_reduces_duplicates_from_specialists(self):
        """Duplicate findings across quick review and specialists are consolidated."""
        from sentinel.assess.reviewer import ReviewOrchestrator
        from sentinel.config import Config

        config = MagicMock(spec=Config)
        config.repo_path = Path.cwd()
        config.chunk_max_lines = 500
        config.chunk_overlap_lines = 50
        config.max_chunks_per_module = 10
        # Pin routing so specialist selection is driven only by
        # recommended_skills (not the full-coverage or reliability-first
        # branches, which a bare MagicMock(spec=Config) would otherwise make
        # truthy and silently run the entire specialist suite).
        config.enforce_full_specialist_coverage = False
        config.review_profile = "triggered"

        store = MagicMock()
        architect = MagicMock()

        # Non-empty recommended_skills is what routes the (single) specialist.
        # The finding text below intentionally contains no trigger keywords, so
        # the specialist runs *because* of recommended_skills and nothing else.
        architect.prioritize_review.return_value = {
            "recommended_skills": [Skills.CORRECTNESS],
            "criticality": "low",
        }
        # Quick review returns a finding with no specialist-trigger keywords.
        architect.quick_review.return_value = {
            "findings": [
                {
                    "description": "Duplicate finding reported by two review layers on the same builder line",
                    "severity": "P1",
                    "location": "db.py:10-20 build_query()",
                    "finding_kind": "logic_bug",
                },
            ],
        }
        # The correctness specialist returns the same finding at higher severity.
        architect.specialist_review.return_value = {
            "findings": [
                {
                    "description": "Duplicate finding reported by two review layers on the same builder line",
                    "severity": "Critical",
                    "location": "db.py:10-20 build_query()",
                    "finding_kind": "logic_bug",
                },
            ],
        }

        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=ScanContext.from_repo_root(config.repo_path),
        )
        module = {"name": "backend", "path": "backend/", "files": ["db.py"]}
        source_code = "def build_query(x):\n    return x\n"
        manifest = {"modules": [module]}

        findings = orchestrator.review_module(module, source_code, manifest)

        # Specialist routing must actually have fired exactly once (single
        # specialist over a single chunk). This fails if recommended_skills is
        # empty or review_module skips specialist routing.
        assert architect.specialist_review.call_count == 1
        # The duplicate across quick review and specialist should be deduped
        # (exact same description and location)
        assert len(findings) == 1
        # Severity should be escalated to Critical (higher severity wins),
        # which can only happen if the specialist finding was present.
        assert findings[0]["severity"] == "Critical"
        assert store.append_finding.call_count == 1
