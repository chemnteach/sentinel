"""Tests for sentinel.learnings — learning extraction, schema, and store.

Covers:
  - Learning dataclass creation, serialization, and validation
  - LearningExtractor pattern grouping and filtering
  - LearningStore persistence and querying
  - CLI commands for ingest-findings and export-learnings
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentinel.learnings.schema import Learning, LearningCategory
from sentinel.learnings.extractor import LearningExtractor
from sentinel.learnings.store import LearningStore


# ── Helpers ──────────────────────────────────────────────────────────


def _make_finding(**overrides) -> dict:
    """Create a finding dict with sensible defaults matching finding_schema V2."""
    base = {
        "id": "F-abc123def0",
        "module": "backend",
        "skill": "layer2b-correctness-specialist",
        "severity": "P2",
        "category": "error_handling",
        "file": "app.py",
        "function": "save_data",
        "line_range": "42-50",
        "location": "app.py:42-50 save_data()",
        "description": "Bare except swallows all errors silently",
        "suggested_fix": "Use specific exception types",
        "status": "open",
    }
    base.update(overrides)
    return base


def _make_learning(**overrides) -> Learning:
    """Create a Learning with sensible defaults."""
    defaults = dict(
        id="LRN-20260315-001",
        source_repo="acme/backend",
        authored_by_foundry=False,
        category=LearningCategory.ERROR_HANDLING,
        severity="P2",
        pattern="Bare except blocks swallow errors silently",
        anti_pattern_code="try:\n    save()\nexcept:\n    pass",
        correct_pattern_code="try:\n    save()\nexcept IOError as e:\n    log(e)",
        applicable_to_generation=True,
        why="Found in 3 modules; silent failures hide bugs in production",
        sentinel_finding_ids=["F-abc123def0", "F-def456789a"],
        extracted_at="2026-03-15T10:00:00Z",
    )
    defaults.update(overrides)
    return Learning(**defaults)


# ── Schema Tests ─────────────────────────────────────────────────────


class TestLearningSchema:
    """Tests for Learning dataclass and validation."""

    def test_learning_creation(self) -> None:
        """Create a Learning with all fields populated."""
        learning = _make_learning()
        assert learning.id == "LRN-20260315-001"
        assert learning.source_repo == "acme/backend"
        assert learning.category == LearningCategory.ERROR_HANDLING
        assert learning.severity == "P2"
        assert learning.pattern == "Bare except blocks swallow errors silently"
        assert learning.applicable_to_generation is True
        assert len(learning.sentinel_finding_ids) == 2

    def test_learning_to_dict(self) -> None:
        """to_dict produces a plain dict with all fields."""
        learning = _make_learning()
        d = learning.to_dict()
        assert isinstance(d, dict)
        assert d["id"] == "LRN-20260315-001"
        assert d["category"] == "error_handling"
        assert d["sentinel_finding_ids"] == ["F-abc123def0", "F-def456789a"]
        assert d["authored_by_foundry"] is False

    def test_learning_from_dict(self) -> None:
        """from_dict reconstructs a Learning from a dict."""
        d = {
            "id": "LRN-20260315-002",
            "source_repo": "acme/frontend",
            "authored_by_foundry": True,
            "category": "security",
            "severity": "P1",
            "pattern": "SQL injection via string concatenation",
            "anti_pattern_code": None,
            "correct_pattern_code": None,
            "applicable_to_generation": True,
            "why": "Found in 2 query builders",
            "sentinel_finding_ids": ["F-111", "F-222"],
            "extracted_at": "2026-03-15T11:00:00Z",
        }
        learning = Learning.from_dict(d)
        assert learning.id == "LRN-20260315-002"
        assert learning.category == LearningCategory.SECURITY
        assert learning.authored_by_foundry is True

    def test_learning_roundtrip(self) -> None:
        """to_dict then from_dict produces an equivalent Learning."""
        original = _make_learning()
        d = original.to_dict()
        restored = Learning.from_dict(d)
        assert restored.id == original.id
        assert restored.category == original.category
        assert restored.severity == original.severity
        assert restored.pattern == original.pattern
        assert restored.anti_pattern_code == original.anti_pattern_code
        assert restored.correct_pattern_code == original.correct_pattern_code
        assert restored.applicable_to_generation == original.applicable_to_generation
        assert restored.why == original.why
        assert restored.sentinel_finding_ids == original.sentinel_finding_ids
        assert restored.extracted_at == original.extracted_at

    def test_learning_validate_valid(self) -> None:
        """No validation errors on a well-formed Learning."""
        learning = _make_learning()
        errors = learning.validate()
        assert errors == []

    def test_learning_validate_missing_id(self) -> None:
        """Empty id triggers a validation error."""
        learning = _make_learning(id="")
        errors = learning.validate()
        assert any("id" in e.lower() for e in errors)

    def test_learning_validate_missing_pattern(self) -> None:
        """Empty pattern triggers a validation error."""
        learning = _make_learning(pattern="")
        errors = learning.validate()
        assert any("pattern" in e.lower() for e in errors)

    def test_learning_category_enum(self) -> None:
        """All expected categories exist in LearningCategory."""
        expected = {
            "security", "concurrency", "error_handling", "performance",
            "data_integrity", "correctness", "maintainability",
            "testability", "code_smell",
        }
        actual = {c.value for c in LearningCategory}
        assert expected == actual


# ── Extractor Tests ──────────────────────────────────────────────────


class TestLearningExtractor:
    """Tests for LearningExtractor — pattern extraction from findings."""

    def test_extractor_empty_findings(self) -> None:
        """Empty findings list produces no learnings."""
        extractor = LearningExtractor([], source_repo="acme/app")
        result = extractor.extract()
        assert result == []

    def test_extractor_single_finding(self) -> None:
        """A single finding is not a pattern — returns empty list."""
        findings = [_make_finding()]
        extractor = LearningExtractor(findings, source_repo="acme/app")
        result = extractor.extract()
        assert result == []

    def test_extractor_two_similar_findings(self) -> None:
        """Two findings with same category + similar description = one learning."""
        f1 = _make_finding(
            id="F-aaa",
            description="Bare except swallows all errors in save_data",
            category="error_handling",
            file="app.py",
        )
        f2 = _make_finding(
            id="F-bbb",
            description="Bare except swallows all errors in load_data",
            category="error_handling",
            file="utils.py",
        )
        extractor = LearningExtractor([f1, f2], source_repo="acme/app")
        result = extractor.extract()
        assert len(result) == 1
        assert result[0].category == LearningCategory.ERROR_HANDLING
        assert "F-aaa" in result[0].sentinel_finding_ids
        assert "F-bbb" in result[0].sentinel_finding_ids

    def test_extractor_groups_by_category(self) -> None:
        """Findings in different categories produce separate learnings."""
        findings = [
            _make_finding(id="F-1", category="error_handling",
                          description="Bare except swallows errors in module A"),
            _make_finding(id="F-2", category="error_handling",
                          description="Bare except swallows errors in module B"),
            _make_finding(id="F-3", category="concurrency",
                          description="Race condition in shared state handler X"),
            _make_finding(id="F-4", category="concurrency",
                          description="Race condition in shared state handler Y"),
        ]
        extractor = LearningExtractor(findings, source_repo="acme/app")
        result = extractor.extract()
        categories = {l.category for l in result}
        assert LearningCategory.ERROR_HANDLING in categories
        assert LearningCategory.CONCURRENCY in categories

    def test_extractor_applicable_security(self) -> None:
        """Security findings are marked applicable_to_generation."""
        findings = [
            _make_finding(id="F-s1", category="security",
                          severity="P1",
                          description="SQL injection via string concatenation in query A"),
            _make_finding(id="F-s2", category="security",
                          severity="P1",
                          description="SQL injection via string concatenation in query B"),
        ]
        extractor = LearningExtractor(findings, source_repo="acme/app")
        result = extractor.extract()
        assert len(result) >= 1
        for learning in result:
            if learning.category == LearningCategory.SECURITY:
                assert learning.applicable_to_generation is True

    def test_extractor_not_applicable_p3(self) -> None:
        """P3-only findings are not applicable to generation."""
        findings = [
            _make_finding(id="F-p3a", category="code_smell", severity="P3",
                          description="Naming convention violation in function alpha"),
            _make_finding(id="F-p3b", category="code_smell", severity="P3",
                          description="Naming convention violation in function beta"),
        ]
        extractor = LearningExtractor(findings, source_repo="acme/app")
        result = extractor.extract()
        assert len(result) >= 1
        for learning in result:
            assert learning.applicable_to_generation is False

    def test_extractor_authored_by_foundry_flag(self) -> None:
        """authored_by_foundry flag propagates to all extracted learnings."""
        findings = [
            _make_finding(id="F-f1", category="error_handling",
                          description="Bare except in generated handler A"),
            _make_finding(id="F-f2", category="error_handling",
                          description="Bare except in generated handler B"),
        ]
        extractor = LearningExtractor(
            findings, source_repo="acme/app", authored_by_foundry=True,
        )
        result = extractor.extract()
        assert len(result) >= 1
        for learning in result:
            assert learning.authored_by_foundry is True

    def test_extractor_generates_unique_ids(self) -> None:
        """Each extracted learning gets a unique ID."""
        findings = [
            _make_finding(id="F-1", category="error_handling",
                          description="Bare except swallows errors in A"),
            _make_finding(id="F-2", category="error_handling",
                          description="Bare except swallows errors in B"),
            _make_finding(id="F-3", category="concurrency",
                          description="Race condition on shared state X"),
            _make_finding(id="F-4", category="concurrency",
                          description="Race condition on shared state Y"),
        ]
        extractor = LearningExtractor(findings, source_repo="acme/app")
        result = extractor.extract()
        ids = [l.id for l in result]
        assert len(ids) == len(set(ids)), f"Duplicate IDs found: {ids}"

    def test_extractor_severity_is_highest(self) -> None:
        """Learning severity equals the highest severity in the group."""
        findings = [
            _make_finding(id="F-h1", category="error_handling", severity="P3",
                          description="Bare except in save handler A"),
            _make_finding(id="F-h2", category="error_handling", severity="Critical",
                          description="Bare except in save handler B"),
        ]
        extractor = LearningExtractor(findings, source_repo="acme/app")
        result = extractor.extract()
        assert len(result) == 1
        assert result[0].severity == "Critical"


# ── Store Tests ──────────────────────────────────────────────────────


class TestLearningStore:
    """Tests for LearningStore — persistence and querying."""

    def test_store_empty(self, tmp_path: Path) -> None:
        """New store returns empty list."""
        store = LearningStore(tmp_path / "learnings")
        assert store.load_all() == []

    def test_store_save_creates_files(self, tmp_path: Path) -> None:
        """Saving learnings creates individual JSON files."""
        store = LearningStore(tmp_path / "learnings")
        learnings = [_make_learning(id="LRN-20260315-001")]
        paths = store.save(learnings)
        assert len(paths) == 1
        assert paths[0].exists()
        assert paths[0].suffix == ".json"

    def test_store_save_creates_index(self, tmp_path: Path) -> None:
        """Saving learnings creates/updates index.json."""
        store = LearningStore(tmp_path / "learnings")
        learnings = [_make_learning(id="LRN-20260315-001")]
        store.save(learnings)
        index = store.load_index()
        assert "learnings" in index
        assert len(index["learnings"]) == 1
        assert index["learnings"][0]["id"] == "LRN-20260315-001"

    def test_store_load_all(self, tmp_path: Path) -> None:
        """Round-trip: save then load_all returns equivalent learnings."""
        store = LearningStore(tmp_path / "learnings")
        original = _make_learning(id="LRN-20260315-001")
        store.save([original])
        loaded = store.load_all()
        assert len(loaded) == 1
        assert loaded[0].id == original.id
        assert loaded[0].pattern == original.pattern

    def test_store_load_index(self, tmp_path: Path) -> None:
        """Index has correct count and IDs."""
        store = LearningStore(tmp_path / "learnings")
        l1 = _make_learning(id="LRN-20260315-001")
        l2 = _make_learning(id="LRN-20260315-002", pattern="Different pattern")
        store.save([l1, l2])
        index = store.load_index()
        ids = [entry["id"] for entry in index["learnings"]]
        assert "LRN-20260315-001" in ids
        assert "LRN-20260315-002" in ids

    def test_store_query_by_category(self, tmp_path: Path) -> None:
        """Query filters by category."""
        store = LearningStore(tmp_path / "learnings")
        l1 = _make_learning(id="LRN-20260315-001",
                            category=LearningCategory.ERROR_HANDLING)
        l2 = _make_learning(id="LRN-20260315-002",
                            category=LearningCategory.SECURITY,
                            pattern="SQL injection pattern")
        store.save([l1, l2])
        result = store.query(category="security")
        assert len(result) == 1
        assert result[0].category == LearningCategory.SECURITY

    def test_store_query_generation_only(self, tmp_path: Path) -> None:
        """Query filters by applicable_to_generation."""
        store = LearningStore(tmp_path / "learnings")
        l1 = _make_learning(id="LRN-20260315-001", applicable_to_generation=True)
        l2 = _make_learning(id="LRN-20260315-002",
                            applicable_to_generation=False,
                            pattern="Cosmetic issue")
        store.save([l1, l2])
        result = store.query(applicable_to_generation=True)
        assert len(result) == 1
        assert result[0].applicable_to_generation is True

    def test_store_append(self, tmp_path: Path) -> None:
        """Saving more learnings doesn't overwrite existing ones."""
        store = LearningStore(tmp_path / "learnings")
        l1 = _make_learning(id="LRN-20260315-001")
        store.save([l1])
        assert len(store.load_all()) == 1

        l2 = _make_learning(id="LRN-20260315-002", pattern="New pattern")
        store.save([l2])
        all_learnings = store.load_all()
        assert len(all_learnings) == 2
        ids = {l.id for l in all_learnings}
        assert "LRN-20260315-001" in ids
        assert "LRN-20260315-002" in ids

    def test_store_concurrent_save_no_lost_index_update(self, tmp_path: Path) -> None:
        """Concurrent saves must not lose index.json entries — the index
        read-modify-write is serialized under a lock."""
        import threading

        store = LearningStore(tmp_path / "learnings")
        n = 16
        barrier = threading.Barrier(n)
        errors: list = []

        def worker(i: int) -> None:
            try:
                barrier.wait()  # maximize overlap on the index RMW
                store.save([_make_learning(id=f"LRN-20260315-{i:03d}")])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        index = store.load_index()
        assert len(index["learnings"]) == n  # every entry survives — no lost update


# ── CLI Tests ────────────────────────────────────────────────────────


class TestLearningsCLI:
    """Tests for CLI commands: ingest-findings and export-learnings."""

    def test_export_learnings_text_format(self, tmp_path: Path) -> None:
        """export-learnings with --format text produces readable output."""
        from click.testing import CliRunner
        from sentinel.cli import main
        import sentinel.config as config_module

        config_module._config = None

        # Seed a learning in the store
        store_path = tmp_path / "sentinel" / "state" / "learnings"
        store = LearningStore(store_path)
        store.save([_make_learning(id="LRN-20260315-001")])

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["export-learnings", "--format", "text"],
            env={
                "SENTINEL_REPO_PATH": str(tmp_path),
                "SENTINEL_STATE_DIR": str(tmp_path / "sentinel" / "state"),
            },
        )
        config_module._config = None
        assert result.exit_code == 0
        assert "LRN-20260315-001" in result.output

    def test_export_learnings_json_format(self, tmp_path: Path) -> None:
        """export-learnings with --format json produces valid JSON."""
        from click.testing import CliRunner
        from sentinel.cli import main
        import sentinel.config as config_module

        config_module._config = None

        store_path = tmp_path / "sentinel" / "state" / "learnings"
        store = LearningStore(store_path)
        store.save([_make_learning(id="LRN-20260315-001")])

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["export-learnings", "--format", "json"],
            env={
                "SENTINEL_REPO_PATH": str(tmp_path),
                "SENTINEL_STATE_DIR": str(tmp_path / "sentinel" / "state"),
            },
        )
        config_module._config = None
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1

    def test_ingest_findings_cli(self, tmp_path: Path) -> None:
        """ingest-findings reads a JSON file and extracts learnings."""
        from click.testing import CliRunner
        from sentinel.cli import main
        import sentinel.config as config_module

        config_module._config = None

        # Create a findings JSON file with 2+ similar findings
        findings = [
            _make_finding(id="F-ext1", category="error_handling",
                          description="Bare except swallows errors in handler A"),
            _make_finding(id="F-ext2", category="error_handling",
                          description="Bare except swallows errors in handler B"),
        ]
        findings_file = tmp_path / "findings.json"
        findings_file.write_text(json.dumps(findings), encoding="utf-8")

        state_dir = tmp_path / "sentinel" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "ingest-findings",
                str(findings_file),
                "--source-repo", "external/repo",
            ],
            env={
                "SENTINEL_REPO_PATH": str(tmp_path),
                "SENTINEL_STATE_DIR": str(state_dir),
            },
        )
        config_module._config = None
        assert result.exit_code == 0
        assert "learning" in result.output.lower() or "ingested" in result.output.lower()
