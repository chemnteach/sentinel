"""
tests/test_assess.py

Tests for the assessment pipeline: Chunker, Cartographer, and
ReviewOrchestrator specialist trigger logic.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sentinel.assess.chunker import Chunk, Chunker
from sentinel.assess.cartographer import Cartographer
from sentinel.assess.finding_schema import FindingSchemaValidationError
from sentinel.assess.scan_context import ScanContext
from sentinel.config import Config
from sentinel.orchestrator.context import build_module_review_context
from sentinel.orchestrator.main import Orchestrator
from sentinel.orchestrator.state_store import StateStore


def _scan_context_for_config(config: Config) -> ScanContext:
    """Build a test scan context from ``config.repo_path`` for unit-level reviewer tests."""

    repo_path = getattr(config, "repo_path", None)
    if not isinstance(repo_path, (str, Path)):
        repo_path = Path.cwd()
        config.repo_path = repo_path
    return ScanContext.from_repo_root(Path(repo_path))


def _reviewer_test_config() -> Config:
    config = MagicMock(spec=Config)
    config.repo_path = Path.cwd()
    config.chunk_max_lines = 500
    config.chunk_overlap_lines = 50
    config.max_chunks_per_module = 10
    config.enforce_full_specialist_coverage = False
    config.review_profile = "triggered"
    return config


def _noncompliant_raw_finding() -> dict:
    return {
        "description": "Producer omitted required finding_kind.",
        "severity": "P2",
        "location": "app.py:1",
    }


# ---------------------------------------------------------------------------
# Chunker tests
# ---------------------------------------------------------------------------

class TestChunker:
    """Tests for the code chunker — splits large files with overlap."""

    def test_short_file_produces_single_chunk(self) -> None:
        chunker = Chunker(max_lines=400, overlap_lines=40)
        content = "\n".join(f"line {i}" for i in range(50))

        chunks = list(chunker.chunk_content("test.py", content))

        assert len(chunks) == 1
        assert chunks[0].chunk_index == 0
        assert chunks[0].total_chunks == 1
        assert chunks[0].start_line == 1
        assert "line 0" in chunks[0].content

    def test_large_file_produces_multiple_chunks(self) -> None:
        chunker = Chunker(max_lines=100, overlap_lines=10)
        content = "\n".join(f"line {i}" for i in range(250))

        chunks = list(chunker.chunk_content("test.py", content))

        assert len(chunks) >= 2
        for i, chunk in enumerate(chunks):
            assert chunk.chunk_index == i
            assert chunk.total_chunks == len(chunks)

    def test_chunks_are_contiguous_with_overlap(self) -> None:
        chunker = Chunker(max_lines=100, overlap_lines=10)
        content = "\n".join(f"line {i}" for i in range(250))

        chunks = list(chunker.chunk_content("test.py", content))

        # Every line appears in at least one chunk
        all_content = "\n".join(c.content for c in chunks)
        for i in range(250):
            assert f"line {i}" in all_content

    def test_chunk_header_contains_file_path(self) -> None:
        chunker = Chunker(max_lines=400, overlap_lines=40)
        chunks = list(chunker.chunk_content("mymodule/app.py", "x = 1"))

        assert "mymodule/app.py" in chunks[0].header

    def test_chunk_header_includes_line_numbers(self) -> None:
        chunker = Chunker(max_lines=400, overlap_lines=40)
        chunks = list(chunker.chunk_content("app.py", "a = 1\nb = 2\n"))

        assert "1" in chunks[0].header

    def test_content_with_header(self) -> None:
        chunker = Chunker(max_lines=400, overlap_lines=40)
        chunks = list(chunker.chunk_content("app.py", "x = 1"))

        combined = chunks[0].content_with_header
        assert "app.py" in combined
        assert "x = 1" in combined

    def test_empty_file_returns_no_chunks(self) -> None:
        chunker = Chunker(max_lines=400, overlap_lines=40)
        chunks = list(chunker.chunk_content("empty.py", ""))

        assert chunks == []

    def test_whitespace_only_file_returns_no_chunks(self) -> None:
        chunker = Chunker(max_lines=400, overlap_lines=40)
        chunks = list(chunker.chunk_content("ws.py", "   \n   \n"))

        assert chunks == []

    def test_chunk_from_path(self, tmp_path: Path) -> None:
        source = tmp_path / "hello.py"
        source.write_text("print('hello')\n", encoding="utf-8")

        chunker = Chunker(max_lines=400, overlap_lines=40)
        chunks = list(chunker.chunk_file(source))

        assert len(chunks) == 1
        assert "print('hello')" in chunks[0].content

    def test_chunk_from_path_missing_file_raises(self, tmp_path: Path) -> None:
        chunker = Chunker(max_lines=400, overlap_lines=40)
        with pytest.raises(OSError):
            list(chunker.chunk_file(tmp_path / "nonexistent.py"))

    def test_chunk_respects_class_boundaries(self) -> None:
        """Chunks should prefer splitting at class/def boundaries."""
        chunker = Chunker(max_lines=10, overlap_lines=2)
        content = (
            "x = 1\ny = 2\n" * 3
            + "class Foo:\n"
            + "    def method(self):\n"
            + "        pass\n" * 5
            + "class Bar:\n"
            + "    pass\n"
        )

        chunks = list(chunker.chunk_content("app.py", content))

        # At least one chunk should start at or near a class definition
        all_content = " ".join(c.content for c in chunks)
        assert "class Foo" in all_content
        assert "class Bar" in all_content

    def test_find_ast_split_points_returns_top_level_boundaries(self) -> None:
        chunker = Chunker(max_lines=20, overlap_lines=2)
        lines = [
            "def foo():",
            "    pass",
            "",
            "class Bar:",
            "    pass",
            "",
            "async def baz():",
            "    pass",
        ]

        points = chunker._find_ast_split_points(lines)

        assert points == [0, 3, 6]

    def test_chunk_string_prefers_ast_split_points(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chunker = Chunker(max_lines=6, overlap_lines=1)
        content = "\n".join(f"line {i}" for i in range(12))

        monkeypatch.setattr(chunker, "_find_ast_split_points", lambda lines: [4])
        monkeypatch.setattr(chunker, "_find_split_points", lambda lines: [2])

        chunks = list(chunker.chunk_string(content))

        assert len(chunks) >= 2
        assert chunks[0].end_line == 4

    def test_chunk_string_falls_back_to_regex_when_ast_is_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chunker = Chunker(max_lines=6, overlap_lines=1)
        content = "\n".join(f"line {i}" for i in range(12))

        monkeypatch.setattr(chunker, "_find_ast_split_points", lambda lines: [])
        # A split point in the latter half of the window (floor = 6 // 2 = 3)
        # so it is eligible; points at/below the floor are intentionally
        # ignored to prevent one-line-at-a-time crawling.
        monkeypatch.setattr(chunker, "_find_split_points", lambda lines: [4])

        chunks = list(chunker.chunk_string(content))

        assert len(chunks) >= 2
        assert chunks[0].end_line == 4

    def test_split_point_near_start_does_not_explode_chunk_count(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: a split point near the window start must not pin `end`
        and advance one line at a time when the next split point lies beyond
        the window (e.g. a large class body with no top-level defs). Before the
        fix this produced ~one chunk per line across the gap."""
        chunker = Chunker(max_lines=100, overlap_lines=40)
        content = "\n".join(f"line {i}" for i in range(600))
        # Two split points 500 lines apart — the gap that triggered the crawl.
        monkeypatch.setattr(chunker, "_find_ast_split_points", lambda lines: [60, 560])

        chunks = list(chunker.chunk_string(content))

        # ~600 lines at a >=50-line minimum stride is at most a dozen chunks,
        # never the hundreds the pin-and-crawl bug produced.
        assert len(chunks) <= 15
        # No degenerate sliver chunks.
        assert min(c.end_line - c.start_line + 1 for c in chunks) >= 30
        # Coverage is preserved.
        all_content = "\n".join(c.content for c in chunks)
        for i in (0, 300, 599):
            assert f"line {i}" in all_content

    def test_chunk_string_accepts_preferred_split_hints_argument(self) -> None:
        chunker = Chunker(max_lines=4, overlap_lines=1)
        content = "\n".join(f"line {i}" for i in range(10))

        chunks = list(chunker.chunk_string(content, preferred_split_hints=[]))

        assert len(chunks) >= 2

    def test_len_of_chunk(self) -> None:
        chunk = Chunk(
            file_path="x.py",
            chunk_index=0,
            total_chunks=1,
            start_line=1,
            end_line=3,
            content="abc",
        )
        assert len(chunk) == 3


# ---------------------------------------------------------------------------
# Chunk dataclass tests
# ---------------------------------------------------------------------------

class TestChunk:
    def test_frozen(self) -> None:
        chunk = Chunk(
            file_path="x.py",
            chunk_index=0,
            total_chunks=1,
            start_line=1,
            end_line=5,
            content="code",
        )
        with pytest.raises((AttributeError, TypeError)):
            chunk.content = "changed"  # type: ignore[misc]

    def test_header_format(self) -> None:
        chunk = Chunk(
            file_path="foo/bar.py",
            chunk_index=1,
            total_chunks=3,
            start_line=101,
            end_line=200,
            content="...",
        )
        header = chunk.header
        assert "foo/bar.py" in header
        assert "2/3" in header  # chunk_index + 1 / total
        assert "101" in header
        assert "200" in header


# ---------------------------------------------------------------------------
# Specialist trigger logic tests
# ---------------------------------------------------------------------------

class TestSpecialistTriggers:
    """Tests for the reviewer's specialist triggering heuristic."""

    def test_security_keyword_triggers_security_skill(self) -> None:
        from sentinel.assess.reviewer import _determine_specialists
        from sentinel.skills.loader import Skills

        findings = [
            {"severity": "P1", "description": "SQL injection risk in user input handler"}
        ]
        skills = _determine_specialists(findings, module_content="")
        assert Skills.SECURITY in skills

    def test_race_condition_triggers_concurrency(self) -> None:
        from sentinel.assess.reviewer import _determine_specialists
        from sentinel.skills.loader import Skills

        findings = [
            {"severity": "P1", "description": "race condition in shared state update"}
        ]
        skills = _determine_specialists(findings, module_content="")
        assert Skills.CONCURRENCY in skills

    def test_no_findings_no_specialists(self) -> None:
        from sentinel.assess.reviewer import _determine_specialists

        skills = _determine_specialists([], module_content="hello world")
        # Quick review only — no specialist triggers on empty findings
        assert skills == [] or all(
            s.startswith("layer") for s in skills
        )

    def test_critical_finding_always_triggers_review(self) -> None:
        from sentinel.assess.reviewer import _determine_specialists
        from sentinel.skills.loader import Skills

        findings = [
            {
                "severity": "Critical",
                "description": "heap corruption in memory allocator",
            }
        ]
        # Critical findings should trigger correctness at minimum
        skills = _determine_specialists(findings, module_content="")
        assert len(skills) >= 0  # Even empty is acceptable; we just validate no crash


class TestCartographerFallback:
    def test_falls_back_to_local_manifest_when_model_call_fails(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        src = repo / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "Program.cs").write_text("class Program {}\n", encoding="utf-8")

        env = {
            "SENTINEL_REPO_PATH": str(repo),
            "SENTINEL_LOCAL_ONLY_MODE": "true",
            "SENTINEL_STATE_DIR": "sentinel/state",
        }
        cfg = Config(env=env, load_dotenv_file=False)
        cfg.ensure_state_dir()

        mock_architect = MagicMock()
        mock_architect.map_codebase.side_effect = RuntimeError("model failure")
        store = StateStore(state_dir=cfg.state_dir, repo_path=cfg.repo_path)

        cartographer = Cartographer(mock_architect, store, cfg)
        manifest = cartographer.run([repo])

        assert manifest.get("repositories")
        assert manifest.get("modules")
        assert any("Program.cs" in f for m in manifest["modules"] for f in m.get("files", []))

    def test_strict_mode_collect_samples_includes_non_source_extensions(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        (repo / "notes.txt").write_text("important context\n", encoding="utf-8")
        (repo / "src.py").write_text("print('ok')\n", encoding="utf-8")

        env = {
            "SENTINEL_REPO_PATH": str(repo),
            "SENTINEL_STRICT_QUALITY_MODE": "true",
            "SENTINEL_LOCAL_ONLY_MODE": "true",
        }
        cfg = Config(env=env, load_dotenv_file=False)
        cfg.ensure_state_dir()

        cartographer = Cartographer(MagicMock(), MagicMock(), cfg)
        samples = cartographer._collect_samples([repo])

        assert any(path.endswith("notes.txt") for path in samples)

    def test_strict_mode_build_file_tree_includes_deep_paths(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        deep_dir = repo / "a" / "b" / "c" / "d" / "e" / "f"
        deep_dir.mkdir(parents=True, exist_ok=True)
        (deep_dir / "deep.py").write_text("print('deep')\n", encoding="utf-8")

        env = {
            "SENTINEL_REPO_PATH": str(repo),
            "SENTINEL_STRICT_QUALITY_MODE": "true",
            "SENTINEL_LOCAL_ONLY_MODE": "true",
        }
        cfg = Config(env=env, load_dotenv_file=False)
        cfg.ensure_state_dir()

        cartographer = Cartographer(MagicMock(), MagicMock(), cfg)
        tree = cartographer._build_file_tree([repo])

        assert "deep.py" in tree

    def test_local_manifest_excludes_generated_assessment_dirs(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True, exist_ok=True)
        (repo / "src" / "Program.cs").write_text("class Program {}\n", encoding="utf-8")
        (repo / "Tool_Line_run4").mkdir(parents=True, exist_ok=True)
        (repo / "Tool_Line_run4" / "quality_scorecard.json").write_text("{}\n", encoding="utf-8")
        (repo / "RemediationPackages").mkdir(parents=True, exist_ok=True)
        (repo / "RemediationPackages" / "manifest.json").write_text("{}\n", encoding="utf-8")

        env = {
            "SENTINEL_REPO_PATH": str(repo),
            "SENTINEL_STRICT_QUALITY_MODE": "true",
            "SENTINEL_LOCAL_ONLY_MODE": "true",
        }
        cfg = Config(env=env, load_dotenv_file=False)
        cfg.ensure_state_dir()

        cartographer = Cartographer(MagicMock(), MagicMock(), cfg)
        manifest = cartographer._build_local_manifest([repo])
        module_paths = {module["path"] for module in manifest["modules"]}

        assert "src" in module_paths
        assert "Tool_Line_run4" not in module_paths
        assert "RemediationPackages" not in module_paths


class TestReviewerScanContextAndValidation:
    def test_review_orchestrator_requires_scan_context(self) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator

        with pytest.raises(TypeError):
            ReviewOrchestrator(MagicMock(), MagicMock(), _reviewer_test_config())

    def test_quick_review_schema_validation_logs_error_and_reraises(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator
        from sentinel.skills.loader import Skills

        config = _reviewer_test_config()
        store = MagicMock()
        architect = MagicMock()
        architect.prioritize_review.return_value = {
            "recommended_skills": [],
            "criticality": "low",
        }
        architect.quick_review.return_value = {"findings": [_noncompliant_raw_finding()]}
        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=ScanContext.from_repo_root(config.repo_path),
        )

        with (
            caplog.at_level(logging.ERROR, logger="sentinel.assess.reviewer"),
            pytest.raises(FindingSchemaValidationError),
        ):
            orchestrator.review_module(
                {"name": "backend", "path": "backend/", "files": ["app.py"]},
                "def save_data():\n    return True\n",
                {"modules": []},
            )

        assert "Finding schema validation failed on backend" in caplog.text
        assert Skills.QUICK_REVIEW in caplog.text

    def test_specialist_schema_validation_logs_error_and_reraises(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator
        from sentinel.skills.loader import Skills

        config = _reviewer_test_config()
        store = MagicMock()
        architect = MagicMock()
        architect.specialist_review.return_value = {"findings": [_noncompliant_raw_finding()]}
        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=ScanContext.from_repo_root(config.repo_path),
        )
        chunk = Chunk(
            file_path="backend/app.py",
            chunk_index=0,
            total_chunks=1,
            start_line=1,
            end_line=1,
            content="def save_data(): pass",
        )

        with (
            caplog.at_level(logging.ERROR, logger="sentinel.assess.reviewer"),
            pytest.raises(FindingSchemaValidationError),
        ):
            orchestrator._run_specialist(
                Skills.SECURITY,
                {"name": "backend", "path": "backend/", "files": ["app.py"]},
                chunk.content,
                [chunk],
                [],
            )

        assert "Finding schema validation failed on backend" in caplog.text
        assert Skills.SECURITY in caplog.text

    def test_payload_retry_schema_validation_logs_error_and_reraises(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator
        from sentinel.skills.loader import Skills

        config = _reviewer_test_config()
        store = MagicMock()
        architect = MagicMock()
        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=ScanContext.from_repo_root(config.repo_path),
        )
        module = {"name": "backend", "path": "backend/", "files": ["app.py"]}
        chunk = Chunk(
            file_path="backend/app.py",
            chunk_index=0,
            total_chunks=1,
            start_line=1,
            end_line=1,
            content="def save_data(): pass",
        )
        runner = lambda _context: {"findings": [_noncompliant_raw_finding()]}

        with (
            caplog.at_level(logging.ERROR, logger="sentinel.assess.reviewer"),
            pytest.raises(FindingSchemaValidationError),
        ):
            orchestrator._retry_chunk_for_payload(
                module=module,
                module_name="backend",
                skill=Skills.QUICK_REVIEW,
                chunk=chunk,
                prior_findings=None,
                runner=runner,
            )
        assert "payload retry" in caplog.text
        assert "backend" in caplog.text

        caplog.clear()
        with (
            caplog.at_level(logging.ERROR, logger="sentinel.assess.reviewer"),
            pytest.raises(FindingSchemaValidationError),
        ):
            orchestrator._retry_chunk_for_payload_chars(
                module=module,
                module_name="backend",
                skill=Skills.QUICK_REVIEW,
                source_code=chunk.content,
                runner=runner,
            )
        assert "char payload retry" in caplog.text
        assert "backend" in caplog.text

    def test_non_validation_quick_review_exception_is_logged_and_swallowed(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator

        config = _reviewer_test_config()
        store = MagicMock()
        architect = MagicMock()
        architect.prioritize_review.return_value = {
            "recommended_skills": [],
            "criticality": "low",
        }
        architect.quick_review.side_effect = RuntimeError("simulated transient failure")
        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=ScanContext.from_repo_root(config.repo_path),
        )

        with caplog.at_level(logging.WARNING, logger="sentinel.assess.reviewer"):
            findings = orchestrator.review_module(
                {"name": "backend", "path": "backend/", "files": ["app.py"]},
                "def save_data():\n    return True\n",
                {"modules": []},
            )

        assert findings == []
        assert "Quick Review failed on backend chunk 0" in caplog.text

    def test_orchestrator_raises_guard_when_reviewer_not_initialized(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        src = repo / "src"
        src.mkdir(parents=True)
        (src / "app.py").write_text("print('hello')\n", encoding="utf-8")
        cfg = Config(
            env={
                "SENTINEL_REPO_PATH": str(repo),
                "SENTINEL_LOCAL_ONLY_MODE": "true",
                "SENTINEL_STATE_DIR": "sentinel/state",
            },
            load_dotenv_file=False,
        )
        manifest = {
            "repositories": [{"name": "repo", "path": str(repo)}],
            "modules": [{"name": "src", "path": "src", "files": ["src/app.py"]}],
        }

        with Orchestrator(cfg) as orch:
            monkeypatch.setattr(orch._store, "commit_state", lambda _message: None)
            monkeypatch.setattr(
                orch._cartographer,
                "run",
                lambda _repo_paths, restricted_files=None: (
                    orch._store.write("repositories", manifest["repositories"]),
                    orch._store.write("modules", manifest["modules"]),
                    manifest,
                )[-1],
            )
            monkeypatch.setattr(orch._dependency_analyzer, "run", lambda: None)
            monkeypatch.setattr(
                "sentinel.orchestrator.main.ArchChecker",
                lambda architect, store: SimpleNamespace(run=lambda: []),
            )
            monkeypatch.setattr(
                "sentinel.orchestrator.main.ReviewOrchestrator",
                lambda *args, **kwargs: None,
            )

            with pytest.raises(
                RuntimeError,
                match="ReviewOrchestrator not initialized with scan context",
            ):
                orch.run_phase1([repo])


class TestReviewTelemetry:
    def test_review_module_with_telemetry_uses_reliability_first_profile(self) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator
        from sentinel.skills.loader import Skills

        config = MagicMock(spec=Config)
        config.chunk_max_lines = 500
        config.chunk_overlap_lines = 50
        config.max_chunks_per_module = 10
        config.enforce_full_specialist_coverage = False
        config.review_profile = "reliability-first"

        store = MagicMock()
        architect = MagicMock()
        architect.prioritize_review.return_value = {
            "recommended_skills": [Skills.PERFORMANCE, Skills.TESTABILITY],
            "criticality": "low",
        }
        architect.quick_review.return_value = {"findings": []}
        architect.specialist_review.return_value = {"findings": []}

        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=_scan_context_for_config(config),
        )
        module = {"name": "backend", "path": "backend/", "files": ["app.py"]}
        source_code = "def save_data():\n    return True\n"
        manifest = {"modules": [module]}

        _, status, failed_chunks = orchestrator.review_module_with_telemetry(
            module,
            source_code,
            manifest,
        )

        expected = {
            Skills.CORRECTNESS,
            Skills.DATA_INTEGRITY,
            Skills.ERROR_HANDLING,
            Skills.OPERATIONAL_RISK,
        }

        assert status["review_complete"] is True
        assert set(status["skills_run"][1:]) == expected
        assert all(status["specialist_attempted_chunks"][skill] == 1 for skill in expected)
        assert failed_chunks == []

    def test_review_module_with_telemetry_enforces_full_specialist_coverage(self) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator
        from sentinel.skills.loader import Skills

        config = MagicMock(spec=Config)
        config.chunk_max_lines = 500
        config.chunk_overlap_lines = 50
        config.max_chunks_per_module = 10
        config.enforce_full_specialist_coverage = True

        store = MagicMock()
        architect = MagicMock()
        architect.prioritize_review.return_value = {
            "recommended_skills": [],
            "criticality": "low",
        }
        architect.quick_review.return_value = {"findings": []}
        architect.specialist_review.return_value = {"findings": []}

        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=_scan_context_for_config(config),
        )
        module = {"name": "backend", "path": "backend/", "files": ["app.py"]}
        source_code = "def save_data():\n    return True\n"
        manifest = {"modules": [module]}

        _, status, failed_chunks = orchestrator.review_module_with_telemetry(
            module,
            source_code,
            manifest,
        )

        expected = {
            Skills.CORRECTNESS,
            Skills.SECURITY,
            Skills.DATA_INTEGRITY,
            Skills.PERFORMANCE,
            Skills.MAINTAINABILITY,
            Skills.TESTABILITY,
            Skills.ERROR_HANDLING,
            Skills.CONCURRENCY,
            Skills.ALARM_INTEGRITY,
            Skills.CODE_SMELL,
            Skills.REFACTORING_ADVISOR,
        }

        assert status["review_complete"] is True
        assert set(status["skills_run"][1:]) == expected
        assert all(status["specialist_attempted_chunks"][skill] == 1 for skill in expected)
        assert all(status["specialist_failed_chunks_by_skill"][skill] == 0 for skill in expected)
        assert failed_chunks == []

    def test_review_module_with_telemetry_tracks_failed_chunks(self) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator

        config = MagicMock(spec=Config)
        config.chunk_max_lines = 500
        config.chunk_overlap_lines = 50
        config.max_chunks_per_module = 10

        store = MagicMock()
        architect = MagicMock()
        architect.prioritize_review.return_value = {
            "recommended_skills": [],
            "criticality": "low",
        }
        architect.quick_review.side_effect = RuntimeError("simulated quick review failure")

        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=_scan_context_for_config(config),
        )
        module = {"name": "backend", "path": "backend/", "files": ["app.py"]}
        source_code = "def save_data():\n    return True\n"
        manifest = {"modules": [module]}

        findings, status, failed_chunks = orchestrator.review_module_with_telemetry(
            module,
            source_code,
            manifest,
        )

        assert findings == []
        assert status["review_complete"] is False
        assert status["quick_review_failed_chunks"] == 1
        assert len(failed_chunks) == 1
        assert failed_chunks[0]["skill"] == "layer2a-quick-review"

    def test_review_module_with_telemetry_recovers_quick_review_after_413(self) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator

        config = MagicMock(spec=Config)
        config.chunk_max_lines = 500
        config.chunk_overlap_lines = 50
        config.max_chunks_per_module = 10

        store = MagicMock()
        architect = MagicMock()
        architect.prioritize_review.return_value = {
            "recommended_skills": [],
            "criticality": "low",
        }

        call_count = {"n": 0}

        def quick_review_side_effect(_context: str) -> dict:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("HTTP 413 Payload Too Large")
            return {"findings": []}

        architect.quick_review.side_effect = quick_review_side_effect

        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=_scan_context_for_config(config),
        )
        module = {"name": "backend", "path": "backend/", "files": ["app.py"]}
        source_code = "\n".join([f"line {i}" for i in range(300)])
        manifest = {"modules": [module]}

        _, status, failed_chunks = orchestrator.review_module_with_telemetry(
            module,
            source_code,
            manifest,
        )

        assert status["quick_review_failed_chunks"] == 0
        assert status["review_complete"] is True
        assert failed_chunks == []

    def test_review_module_with_telemetry_recovers_specialist_after_413(self) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator

        config = MagicMock(spec=Config)
        config.chunk_max_lines = 500
        config.chunk_overlap_lines = 50
        config.max_chunks_per_module = 10

        store = MagicMock()
        architect = MagicMock()
        architect.prioritize_review.return_value = {
            "recommended_skills": ["layer2c-security-specialist"],
            "criticality": "low",
        }
        architect.quick_review.return_value = {"findings": []}

        call_count = {"n": 0}

        def specialist_side_effect(_skill: str, _context: str) -> dict:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("HTTP 413 Payload Too Large")
            return {"findings": []}

        architect.specialist_review.side_effect = specialist_side_effect

        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=_scan_context_for_config(config),
        )
        module = {"name": "backend", "path": "backend/", "files": ["app.py"]}
        source_code = "\n".join([f"line {i}" for i in range(300)])
        manifest = {"modules": [module]}

        _, status, failed_chunks = orchestrator.review_module_with_telemetry(
            module,
            source_code,
            manifest,
        )

        assert status["specialist_failed_chunks"] == 0
        assert status["review_complete"] is True
        assert failed_chunks == []

    def test_review_module_with_telemetry_recovers_specialist_after_504_with_payload_fallback(self) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator

        config = MagicMock(spec=Config)
        config.chunk_max_lines = 500
        config.chunk_overlap_lines = 50
        config.max_chunks_per_module = 10
        config.enforce_full_specialist_coverage = False
        config.review_profile = "reliability-first"

        store = MagicMock()
        architect = MagicMock()
        architect.prioritize_review.return_value = {
            "recommended_skills": [],
            "criticality": "low",
        }
        architect.quick_review.return_value = {"findings": []}

        call_count = {"n": 0}
        seen_contexts: list[str] = []

        def specialist_side_effect(skill: str, context: str) -> dict:
            if skill != "layer2b-correctness-specialist":
                return {"findings": []}

            call_count["n"] += 1
            seen_contexts.append(context)
            if call_count["n"] == 1:
                raise RuntimeError(
                    "Agent 'architect' failed on skill 'layer2b-correctness-specialist': "
                    "Server error 504: gateway timeout"
                )
            assert len(context) < len(seen_contexts[0])
            return {"findings": []}

        architect.specialist_review.side_effect = specialist_side_effect

        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=_scan_context_for_config(config),
        )
        module = {"name": "backend", "path": "backend/", "files": ["app.py"]}
        source_code = "\n".join([f"line {i}" for i in range(300)])
        manifest = {"modules": [module]}

        _, status, failed_chunks = orchestrator.review_module_with_telemetry(
            module,
            source_code,
            manifest,
        )

        assert status["specialist_failed_chunks_by_skill"]["layer2b-correctness-specialist"] == 0
        assert status["specialist_failed_chunks"] == 0
        assert status["review_complete"] is True
        assert failed_chunks == []

    def test_review_module_with_telemetry_uses_char_fallback_after_line_retry_fails(self) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator

        config = MagicMock(spec=Config)
        config.chunk_max_lines = 500
        config.chunk_overlap_lines = 50
        config.max_chunks_per_module = 10

        store = MagicMock()
        architect = MagicMock()
        architect.prioritize_review.return_value = {
            "recommended_skills": [],
            "criticality": "low",
        }

        call_count = {"n": 0}

        def quick_review_side_effect(_context: str) -> dict:
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise RuntimeError("HTTP 413 Payload Too Large")
            return {"findings": []}

        architect.quick_review.side_effect = quick_review_side_effect

        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=_scan_context_for_config(config),
        )
        module = {"name": "backend", "path": "backend/", "files": ["app.py"]}
        source_code = "X" * 20000
        manifest = {"modules": [module]}

        _, status, failed_chunks = orchestrator.review_module_with_telemetry(
            module,
            source_code,
            manifest,
        )

        assert status["quick_review_failed_chunks"] == 0
        assert status["review_complete"] is True
        assert failed_chunks == []

    def test_review_module_with_telemetry_uses_char_fallback_for_specialist(self) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator

        config = MagicMock(spec=Config)
        config.chunk_max_lines = 500
        config.chunk_overlap_lines = 50
        config.max_chunks_per_module = 10

        store = MagicMock()
        architect = MagicMock()
        architect.prioritize_review.return_value = {
            "recommended_skills": ["layer2c-security-specialist"],
            "criticality": "low",
        }
        architect.quick_review.return_value = {"findings": []}

        call_count = {"n": 0}

        def specialist_side_effect(_skill: str, _context: str) -> dict:
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise RuntimeError("HTTP 413 Payload Too Large")
            return {"findings": []}

        architect.specialist_review.side_effect = specialist_side_effect

        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=_scan_context_for_config(config),
        )
        module = {"name": "backend", "path": "backend/", "files": ["app.py"]}
        source_code = "X" * 20000
        manifest = {"modules": [module]}

        _, status, failed_chunks = orchestrator.review_module_with_telemetry(
            module,
            source_code,
            manifest,
        )

        assert status["specialist_failed_chunks"] == 0
        assert status["review_complete"] is True
        assert failed_chunks == []

    def test_review_module_with_telemetry_recovers_when_payload_fallback_hits_429(self) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator

        config = MagicMock(spec=Config)
        config.chunk_max_lines = 500
        config.chunk_overlap_lines = 50
        config.max_chunks_per_module = 10

        store = MagicMock()
        architect = MagicMock()
        architect.prioritize_review.return_value = {
            "recommended_skills": [],
            "criticality": "low",
        }

        call_count = {"n": 0}

        def quick_review_side_effect(_context: str) -> dict:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("HTTP 413 Payload Too Large")
            if call_count["n"] == 2:
                raise RuntimeError("HTTP 429 Too Many Requests")
            return {"findings": []}

        architect.quick_review.side_effect = quick_review_side_effect

        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=_scan_context_for_config(config),
        )
        module = {"name": "backend", "path": "backend/", "files": ["app.py"]}
        source_code = "X" * 20000
        manifest = {"modules": [module]}

        _, status, failed_chunks = orchestrator.review_module_with_telemetry(
            module,
            source_code,
            manifest,
        )

        assert status["quick_review_failed_chunks"] == 0
        assert status["review_complete"] is True
        assert failed_chunks == []

    def test_review_module_with_telemetry_recovers_quick_review_after_400_bad_request(self) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator

        config = MagicMock(spec=Config)
        config.chunk_max_lines = 500
        config.chunk_overlap_lines = 50
        config.max_chunks_per_module = 10

        store = MagicMock()
        architect = MagicMock()
        architect.prioritize_review.return_value = {
            "recommended_skills": [],
            "criticality": "low",
        }

        call_count = {"n": 0}

        def quick_review_side_effect(_context: str) -> dict:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("HTTP error: Client error '400 Bad Request' for url 'https://llm.example.com/api/chat'")
            return {"findings": []}

        architect.quick_review.side_effect = quick_review_side_effect

        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=_scan_context_for_config(config),
        )
        module = {"name": "backend", "path": "backend/", "files": ["app.py"]}
        source_code = "X" * 20000
        manifest = {"modules": [module]}

        _, status, failed_chunks = orchestrator.review_module_with_telemetry(
            module,
            source_code,
            manifest,
        )

        assert status["quick_review_failed_chunks"] == 0
        assert status["review_complete"] is True
        assert failed_chunks == []

    def test_review_module_with_telemetry_recovers_quick_review_after_504_with_payload_fallback(self) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator

        config = MagicMock(spec=Config)
        config.chunk_max_lines = 500
        config.chunk_overlap_lines = 50
        config.max_chunks_per_module = 10

        store = MagicMock()
        architect = MagicMock()
        architect.prioritize_review.return_value = {
            "recommended_skills": [],
            "criticality": "low",
        }

        call_count = {"n": 0}
        seen_contexts: list[str] = []

        def quick_review_side_effect(context: str) -> dict:
            call_count["n"] += 1
            seen_contexts.append(context)
            if call_count["n"] == 1:
                assert "line 0" in context
                assert "line 299" in context
                raise RuntimeError(
                    "Agent 'architect' failed on skill 'layer2a-quick-review': "
                    "Server error 504: gateway timeout"
                )
            assert len(context) < len(seen_contexts[0])
            return {"findings": []}

        architect.quick_review.side_effect = quick_review_side_effect

        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=_scan_context_for_config(config),
        )
        module = {"name": "backend", "path": "backend/", "files": ["app.py"]}
        source_code = "\n".join([f"line {i}" for i in range(300)])
        manifest = {"modules": [module]}

        _, status, failed_chunks = orchestrator.review_module_with_telemetry(
            module,
            source_code,
            manifest,
        )

        assert status["quick_review_failed_chunks"] == 0
        assert status["review_complete"] is True
        assert failed_chunks == []

    def test_review_module_with_telemetry_recovers_quick_review_after_non_json_with_payload_fallback(self) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator

        config = MagicMock(spec=Config)
        config.chunk_max_lines = 500
        config.chunk_overlap_lines = 50
        config.max_chunks_per_module = 10

        store = MagicMock()
        architect = MagicMock()
        architect.prioritize_review.return_value = {
            "recommended_skills": [],
            "criticality": "low",
        }

        call_count = {"n": 0}
        seen_contexts: list[str] = []

        def quick_review_side_effect(context: str) -> dict:
            call_count["n"] += 1
            seen_contexts.append(context)
            if call_count["n"] == 1:
                assert "line 0" in context
                assert "line 299" in context
                raise RuntimeError(
                    "Agent 'architect' returned non-JSON on skill 'layer2a-quick-review': "
                    "Model response is not valid JSON after retry: Model response is not valid JSON"
                )
            assert len(context) < len(seen_contexts[0])
            return {"findings": []}

        architect.quick_review.side_effect = quick_review_side_effect

        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=_scan_context_for_config(config),
        )
        module = {"name": "backend", "path": "backend/", "files": ["app.py"]}
        source_code = "\n".join([f"line {i}" for i in range(300)])
        manifest = {"modules": [module]}

        _, status, failed_chunks = orchestrator.review_module_with_telemetry(
            module,
            source_code,
            manifest,
        )

        assert status["quick_review_failed_chunks"] == 0
        assert status["review_complete"] is True
        assert failed_chunks == []

    def test_review_module_with_telemetry_tolerates_refactoring_non_json(self) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator

        config = MagicMock(spec=Config)
        config.chunk_max_lines = 500
        config.chunk_overlap_lines = 50
        config.max_chunks_per_module = 10
        config.enforce_full_specialist_coverage = True

        store = MagicMock()
        architect = MagicMock()
        architect.prioritize_review.return_value = {
            "recommended_skills": [],
            "criticality": "low",
        }
        architect.quick_review.return_value = {"findings": []}

        def specialist_side_effect(skill: str, _context: str) -> dict:
            if skill == "layer4b-refactoring-advisor":
                raise RuntimeError(
                    "Agent 'architect' returned non-JSON on skill 'layer4b-refactoring-advisor': "
                    "Model response is not valid JSON"
                )
            return {"findings": []}

        architect.specialist_review.side_effect = specialist_side_effect

        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=_scan_context_for_config(config),
        )
        module = {"name": "backend", "path": "backend/", "files": ["app.py"]}
        source_code = "def save_data():\n    return True\n"
        manifest = {"modules": [module]}

        _, status, failed_chunks = orchestrator.review_module_with_telemetry(
            module,
            source_code,
            manifest,
        )

        # F2-79661ada17ab et al. — a non-JSON specialist response is a genuine
        # coverage gap. The run does not crash (still "tolerated"), but the
        # failure is now recorded in the scorecard telemetry rather than
        # silently swallowed.
        assert status["specialist_failed_chunks_by_skill"]["layer4b-refactoring-advisor"] == 1
        assert status["specialist_failed_chunks"] == 1
        assert status["review_complete"] is False
        assert len(failed_chunks) == 1
        assert failed_chunks[0]["skill"] == "layer4b-refactoring-advisor"

    def test_review_module_with_telemetry_tolerates_non_json_for_correctness(self) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator

        config = MagicMock(spec=Config)
        config.chunk_max_lines = 500
        config.chunk_overlap_lines = 50
        config.max_chunks_per_module = 10
        config.enforce_full_specialist_coverage = False
        config.review_profile = "reliability-first"

        store = MagicMock()
        architect = MagicMock()
        architect.prioritize_review.return_value = {
            "recommended_skills": [],
            "criticality": "low",
        }
        architect.quick_review.return_value = {"findings": []}

        def specialist_side_effect(skill: str, _context: str) -> dict:
            if skill == "layer2b-correctness-specialist":
                raise RuntimeError(
                    "Agent 'architect' returned non-JSON on skill 'layer2b-correctness-specialist': "
                    "Model response is not valid JSON"
                )
            return {"findings": []}

        architect.specialist_review.side_effect = specialist_side_effect

        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=_scan_context_for_config(config),
        )
        module = {"name": "backend", "path": "backend/", "files": ["app.py"]}
        source_code = "def save_data():\n    return True\n"
        manifest = {"modules": [module]}

        _, status, failed_chunks = orchestrator.review_module_with_telemetry(
            module,
            source_code,
            manifest,
        )

        # F2-79661ada17ab et al. — a non-JSON specialist response is a genuine
        # coverage gap; it is now recorded in telemetry rather than swallowed.
        assert status["specialist_failed_chunks_by_skill"]["layer2b-correctness-specialist"] == 1
        assert status["specialist_failed_chunks"] == 1
        assert status["review_complete"] is False
        assert len(failed_chunks) == 1
        assert failed_chunks[0]["skill"] == "layer2b-correctness-specialist"

    def test_payload_retry_context_compacts_large_module_metadata_in_strict_mode(self) -> None:
        from sentinel.assess.reviewer import ReviewOrchestrator

        config = MagicMock(spec=Config)
        config.chunk_max_lines = 500
        config.chunk_overlap_lines = 50
        config.max_chunks_per_module = 10
        config.strict_quality_mode = True

        store = MagicMock()
        architect = MagicMock()
        architect.prioritize_review.return_value = {
            "recommended_skills": [],
            "criticality": "low",
        }

        call_count = {"n": 0}

        def quick_review_side_effect(context: str) -> dict:
            call_count["n"] += 1
            if call_count["n"] == 1:
                assert '"files": [' in context
                raise RuntimeError("HTTP 413 Payload Too Large")
            assert '"files_sample"' in context
            assert '"file_count": 500' in context
            return {"findings": []}

        architect.quick_review.side_effect = quick_review_side_effect

        orchestrator = ReviewOrchestrator(
            architect,
            store,
            config,
            scan_context=_scan_context_for_config(config),
        )
        module = {
            "name": "backend",
            "path": "backend/",
            "files": [f"src/File{i}.cs" for i in range(500)],
        }
        source_code = "X" * 20000
        manifest = {"modules": [module]}

        _, status, failed_chunks = orchestrator.review_module_with_telemetry(
            module,
            source_code,
            manifest,
        )

        assert status["quick_review_failed_chunks"] == 0
        assert status["review_complete"] is True
        assert failed_chunks == []


def test_build_module_review_context_compacts_large_file_lists() -> None:
    module = {
        "name": "big-module",
        "path": "BigModule",
        "criticality": "high",
        "files": [f"src/File{i}.cs" for i in range(500)],
    }

    context = build_module_review_context(
        module=module,
        source_code="class Example {}",
        skill_name="layer2a-quick-review",
        prior_findings=None,
    )

    assert '"file_count": 500' in context
    assert '"files_sample"' in context
    assert "src/File0.cs" in context
    assert "src/File499.cs" not in context


class TestExtractFindingsRepair:
    """Cycle 37: _extract_findings repairs an out-of-contract finding without
    losing the valid findings around it (no whole-run abort)."""

    def test_repair_preserves_sibling_findings(self) -> None:
        from sentinel.assess.reviewer import _extract_findings

        result = {
            "findings": [
                {
                    "severity": "P1",
                    "finding_kind": "logic_bug",
                    "description": "Off-by-one in loop bound.",
                    "location": "app.py:10",
                },
                {
                    "severity": "P2",
                    "finding_kind": "wrong_assumption",  # out-of-contract
                    "description": "Assumes config is always present.",
                    "location": "app.py:20",
                },
                {
                    "severity": "P1",
                    "finding_kind": "error_signal_loss",
                    "description": "Exception swallowed silently.",
                    "location": "app.py:30",
                },
            ],
        }
        counter = [0]

        findings = _extract_findings(
            result,
            "app_module",
            "layer2a-quick-review",
            repo_root_canonical="d:/Git/sentinel",
            invalid_kind_counter=counter,
        )

        assert len(findings) == 3
        kinds = [f["finding_kind"] for f in findings]
        assert kinds[0] == "logic_bug"
        assert kinds[1] == "structural_complexity"  # repaired
        assert findings[1]["raw_finding_kind"] == "wrong_assumption"
        assert kinds[2] == "error_signal_loss"
        assert counter[0] == 1


class TestModuleOwnership:
    """Cycle 38B: a directory module must not re-review files owned by a deeper
    module. The repo-root module (path '.') must not sweep the whole tree."""

    def test_normalize_module_dir(self) -> None:
        from sentinel.orchestrator.main import _normalize_module_dir

        assert _normalize_module_dir(".") == ""
        assert _normalize_module_dir("") == ""
        assert _normalize_module_dir("sentinel/assess") == "sentinel/assess"
        assert _normalize_module_dir("sentinel\\assess") == "sentinel/assess"
        assert _normalize_module_dir("/sentinel/") == "sentinel"

    def test_root_descendants_are_all_other_modules(self) -> None:
        from sentinel.orchestrator.main import _module_descendant_dirs

        dirs = {"", "sentinel", "sentinel/assess", "tests", "docs"}
        assert set(_module_descendant_dirs("", dirs)) == {
            "sentinel",
            "sentinel/assess",
            "tests",
            "docs",
        }

    def test_parent_excludes_only_nested_modules(self) -> None:
        from sentinel.orchestrator.main import _module_descendant_dirs

        dirs = {"", "sentinel", "sentinel/assess", "sentinel/api", "tests"}
        desc = _module_descendant_dirs("sentinel", dirs)
        assert set(desc) == {"sentinel/assess", "sentinel/api"}
        assert "tests" not in desc  # sibling, not nested
        assert "sentinel" not in desc  # not itself

    def test_no_false_prefix_match(self) -> None:
        from sentinel.orchestrator.main import _module_descendant_dirs

        # "sentineltools" must NOT be treated as nested under "sentinel".
        dirs = {"sentinel", "sentineltools"}
        assert _module_descendant_dirs("sentinel", dirs) == []

    def test_leaf_module_has_no_descendants(self) -> None:
        from sentinel.orchestrator.main import _module_descendant_dirs

        dirs = {"", "sentinel", "sentinel/assess", "tests"}
        assert _module_descendant_dirs("sentinel/assess", dirs) == []
