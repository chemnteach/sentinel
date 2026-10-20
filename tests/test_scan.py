"""Tests for sentinel.scan — Quick assessment module.

Covers:
  - Scanner initialization
  - Language detection from file extensions
  - Single file scanning
  - Directory scanning with extension filters
  - ScanResult properties (has_blockers, summary_text)
  - Specialist trigger detection
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.assess.finding_kinds import (
    ACTIVE_SUPPORTED_REPORT_FINDING_KINDS,
    SUPPORTED_REPORT_FINDING_KINDS,
)
from sentinel.assess.finding_schema import FindingSchemaValidationError, normalize_location
from sentinel.assess.scan_context import ScanContext
from sentinel.config import Config
from sentinel.scan import Scanner, ScanResult


class RootObservingScanner(Scanner):
    """Expose quick-scan root selection without exercising emitter compliance."""

    def __init__(self, config: Config | None = None) -> None:
        super().__init__(config=config)
        self.observed_roots: list[str] = []
        self.received_contexts: list[ScanContext | None] = []

    def scan_file(
        self,
        file_path: Path,
        scan_context: ScanContext | None = None,
    ) -> ScanResult:
        self.received_contexts.append(scan_context)
        return super().scan_file(file_path, scan_context=scan_context)

    def _quick_review_patterns(
        self,
        content: str,
        file_path: str,
        language: str,
        repo_root_canonical: str,
    ) -> list[dict]:
        self.observed_roots.append(repo_root_canonical)
        return [
            {
                "severity": "P3",
                "description": "observed quick-scan root",
                "location": normalize_location(f"{file_path}:1", repo_root_canonical),
            }
        ]


# ---------------------------------------------------------------------------
# Scanner initialization
# ---------------------------------------------------------------------------


class TestScannerInit:
    """Scanner can be created with default or custom config."""

    def test_scanner_init_default(self) -> None:
        scanner = Scanner()
        assert scanner is not None

    def test_scanner_init_with_config(self) -> None:
        from sentinel.config import Config

        cfg = Config(env={"SENTINEL_REPO_PATH": "."})
        scanner = Scanner(config=cfg)
        assert scanner is not None


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------


class TestDetectLanguage:
    """Scanner._detect_language maps extensions to language names."""

    def test_detect_language_python(self) -> None:
        scanner = Scanner()
        assert scanner._detect_language(Path("foo.py")) == "python"

    def test_detect_language_javascript(self) -> None:
        scanner = Scanner()
        assert scanner._detect_language(Path("app.js")) == "javascript"

    def test_detect_language_typescript(self) -> None:
        scanner = Scanner()
        assert scanner._detect_language(Path("component.ts")) == "typescript"

    def test_detect_language_tsx(self) -> None:
        scanner = Scanner()
        assert scanner._detect_language(Path("App.tsx")) == "typescript"

    def test_detect_language_go(self) -> None:
        scanner = Scanner()
        assert scanner._detect_language(Path("main.go")) == "go"

    def test_detect_language_rust(self) -> None:
        scanner = Scanner()
        assert scanner._detect_language(Path("lib.rs")) == "rust"

    def test_detect_language_unknown(self) -> None:
        scanner = Scanner()
        assert scanner._detect_language(Path("data.xyz")) == "unknown"


# ---------------------------------------------------------------------------
# scan_file
# ---------------------------------------------------------------------------


class TestScanFile:
    """Scanner.scan_file returns a ScanResult with expected fields."""

    def test_scan_file_returns_result(self, tmp_path: Path) -> None:
        source = tmp_path / "example.py"
        source.write_text("x = 1\n", encoding="utf-8")

        scanner = Scanner()
        result = scanner.scan_file(source)

        assert isinstance(result, ScanResult)
        assert result.target == str(source)
        assert isinstance(result.findings, list)
        assert isinstance(result.findings_by_severity, dict)
        assert isinstance(result.specialists_triggered, list)
        assert isinstance(result.scan_duration_ms, int)
        assert result.scan_duration_ms >= 0

    def test_scan_file_nonexistent(self) -> None:
        scanner = Scanner()
        with pytest.raises(FileNotFoundError):
            scanner.scan_file(Path("/nonexistent/file.py"))

    def test_scan_file_severity_counts_present(self, tmp_path: Path) -> None:
        source = tmp_path / "app.py"
        source.write_text("x = 1\n", encoding="utf-8")

        scanner = Scanner()
        result = scanner.scan_file(source)

        # Severity buckets should always contain these keys
        for key in ("Critical", "P1", "P2", "P3"):
            assert key in result.findings_by_severity

    def test_scan_file_materializes_schema_valid_findings(self, tmp_path: Path) -> None:
        source = tmp_path / "app.py"
        source.write_text("import os\nos.system('rm -rf /')\n", encoding="utf-8")

        scanner = Scanner()
        result = scanner.scan_file(source)

        assert result.findings
        first = result.findings[0]
        assert first["severity"] == "P1"
        assert first["category"] == "security"

        supported_security_kinds = {
            "injection_risk",
            "authorization_gap",
            "sensitive_data_exposure",
            "insecure_deserialization",
            "crypto_misuse",
            "security_misconfiguration",
        } & SUPPORTED_REPORT_FINDING_KINDS
        if supported_security_kinds:
            assert first["finding_kind"] in supported_security_kinds
            assert first["finding_kind"] != "logic_bug"
        else:
            # If the report contract exposes no security-specific kind,
            # fallback to logic_bug keeps scan findings schema-valid.
            assert first["finding_kind"] == "logic_bug"

        assert first["id"].startswith("F2-")

    def test_scan_file_emits_active_report_contract_finding_kind(self, tmp_path: Path) -> None:
        source = tmp_path / "app.py"
        source.write_text("import os\nos.system('rm -rf /')\n", encoding="utf-8")

        scanner = Scanner()
        result = scanner.scan_file(source)

        assert result.findings
        assert result.findings[0]["finding_kind"] in ACTIVE_SUPPORTED_REPORT_FINDING_KINDS

    def test_scan_file_with_explicit_scan_context_uses_context_root(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        src = repo_root / "src"
        src.mkdir(parents=True)
        source = src / "app.py"
        source.write_text("x = 1\n", encoding="utf-8")
        scan_context = ScanContext.from_repo_root(repo_root)

        scanner = RootObservingScanner()
        result = scanner.scan_file(source, scan_context=scan_context)

        assert scanner.observed_roots == [scan_context.repo_root_canonical]
        assert result.findings[0]["location"] == "src/app.py:1"

    def test_scan_file_without_context_uses_config_repo_path(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        src = repo_root / "src"
        src.mkdir(parents=True)
        source = src / "app.py"
        source.write_text("x = 1\n", encoding="utf-8")
        config = Config(
            env={"SENTINEL_REPO_PATH": str(repo_root), "SENTINEL_LOCAL_ONLY_MODE": "true"},
            load_dotenv_file=False,
        )

        scanner = RootObservingScanner(config=config)
        result = scanner.scan_file(source)

        expected_context = ScanContext.from_repo_root(repo_root)
        assert scanner.observed_roots == [expected_context.repo_root_canonical]
        assert result.findings[0]["location"] == "src/app.py:1"

    def test_scan_file_without_context_or_config_uses_file_parent(self, tmp_path: Path) -> None:
        source = tmp_path / "app.py"
        source.write_text("x = 1\n", encoding="utf-8")

        scanner = RootObservingScanner()
        result = scanner.scan_file(source)

        expected_context = ScanContext.from_repo_root(source.parent)
        assert scanner.observed_roots == [expected_context.repo_root_canonical]
        assert result.findings[0]["location"] == "app.py:1"


# ---------------------------------------------------------------------------
# scan_directory
# ---------------------------------------------------------------------------


class TestScanDirectory:
    """Scanner.scan_directory scans matching files in a directory."""

    def test_scan_directory_filters_extensions(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "style.css").write_text("body {}\n", encoding="utf-8")
        (tmp_path / "util.py").write_text("y = 2\n", encoding="utf-8")

        scanner = Scanner()
        results = scanner.scan_directory(tmp_path, extensions=[".py"])

        # Should only scan .py files
        targets = {r.target for r in results}
        assert all(t.endswith(".py") for t in targets)
        assert len(results) == 2

    def test_scan_directory_empty(self, tmp_path: Path) -> None:
        scanner = Scanner()
        results = scanner.scan_directory(tmp_path)
        assert results == []

    def test_scan_directory_excludes_excluded_dirs(self, tmp_path: Path) -> None:
        # Create file in excluded directory
        node_modules = tmp_path / "node_modules"
        node_modules.mkdir()
        (node_modules / "pkg.js").write_text("var x;", encoding="utf-8")

        # Create file in normal directory
        (tmp_path / "app.js").write_text("var y;", encoding="utf-8")

        scanner = Scanner()
        results = scanner.scan_directory(tmp_path)

        targets = {r.target for r in results}
        assert not any("node_modules" in t for t in targets)

    def test_scan_directory_no_extensions_scans_known(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "data.xyz").write_text("nope\n", encoding="utf-8")

        scanner = Scanner()
        results = scanner.scan_directory(tmp_path)

        # Should scan the .py file but skip unknown extensions
        assert len(results) >= 1
        assert all(not r.target.endswith(".xyz") for r in results)

    def test_scan_directory_reuses_one_scan_context_across_files(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / "a.py").write_text("x = 1\n", encoding="utf-8")
        (repo_root / "b.py").write_text("y = 2\n", encoding="utf-8")

        scanner = RootObservingScanner()
        results = scanner.scan_directory(repo_root)

        assert len(results) == 2
        assert scanner.received_contexts[0] is scanner.received_contexts[1]
        assert scanner.received_contexts[0] is not None
        assert set(scanner.observed_roots) == {
            ScanContext.from_repo_root(repo_root).repo_root_canonical
        }
        assert {finding["location"] for result in results for finding in result.findings} == {
            "a.py:1",
            "b.py:1",
        }

    def test_scan_directory_relative_repo_path_does_not_raise(self, tmp_path: Path) -> None:
        """scan_directory must not raise when SENTINEL_REPO_PATH is a relative path
        like '.' while dir_path is absolute (F2-032ec0436fc3)."""
        source = tmp_path / "app.py"
        source.write_text("x = 1\n", encoding="utf-8")
        config = Config(
            env={"SENTINEL_REPO_PATH": ".", "SENTINEL_LOCAL_ONLY_MODE": "true"},
            load_dotenv_file=False,
        )

        scanner = RootObservingScanner(config=config)
        # Must not raise ValueError from path.relative_to(relative_root).
        results = scanner.scan_directory(tmp_path)

        assert len(results) >= 1
        # repo_root_canonical was resolved to an absolute path.
        assert all(Path(root).is_absolute() for root in scanner.observed_roots)

    @pytest.mark.parametrize(
        ("use_config", "expected_location"),
        [
            (True, "src/app.py:1"),
            (False, "app.py:1"),
        ],
    )
    def test_scan_directory_root_selection(
        self,
        tmp_path: Path,
        use_config: bool,
        expected_location: str,
    ) -> None:
        repo_root = tmp_path / "repo"
        dir_path = repo_root / "src"
        dir_path.mkdir(parents=True)
        source = dir_path / "app.py"
        source.write_text("x = 1\n", encoding="utf-8")
        config = None
        expected_root = dir_path
        if use_config:
            config = Config(
                env={"SENTINEL_REPO_PATH": str(repo_root), "SENTINEL_LOCAL_ONLY_MODE": "true"},
                load_dotenv_file=False,
            )
            expected_root = repo_root

        scanner = RootObservingScanner(config=config)
        results = scanner.scan_directory(dir_path)

        assert scanner.observed_roots == [
            ScanContext.from_repo_root(expected_root).repo_root_canonical
        ]
        assert results[0].findings[0]["location"] == expected_location


# ---------------------------------------------------------------------------
# ScanResult properties
# ---------------------------------------------------------------------------


class TestScanResult:
    """ScanResult.has_blockers and summary_text work correctly."""

    def test_has_blockers_true_critical(self) -> None:
        result = ScanResult(
            target="test.py",
            findings=[{"severity": "Critical", "description": "bad"}],
            findings_by_severity={"Critical": 1, "P1": 0, "P2": 0, "P3": 0},
            specialists_triggered=[],
            scan_duration_ms=100,
        )
        assert result.has_blockers() is True

    def test_has_blockers_true_p1(self) -> None:
        result = ScanResult(
            target="test.py",
            findings=[{"severity": "P1", "description": "bad"}],
            findings_by_severity={"Critical": 0, "P1": 1, "P2": 0, "P3": 0},
            specialists_triggered=[],
            scan_duration_ms=100,
        )
        assert result.has_blockers() is True

    def test_has_blockers_false(self) -> None:
        result = ScanResult(
            target="test.py",
            findings=[{"severity": "P2", "description": "minor"}],
            findings_by_severity={"Critical": 0, "P1": 0, "P2": 1, "P3": 0},
            specialists_triggered=[],
            scan_duration_ms=50,
        )
        assert result.has_blockers() is False

    def test_has_blockers_false_empty(self) -> None:
        result = ScanResult(
            target="test.py",
            findings=[],
            findings_by_severity={"Critical": 0, "P1": 0, "P2": 0, "P3": 0},
            specialists_triggered=[],
            scan_duration_ms=10,
        )
        assert result.has_blockers() is False

    def test_summary_text_readable(self) -> None:
        result = ScanResult(
            target="src/auth.py",
            findings=[
                {"severity": "Critical", "description": "SQL injection"},
                {"severity": "P3", "description": "minor style"},
            ],
            findings_by_severity={"Critical": 1, "P1": 0, "P2": 0, "P3": 1},
            specialists_triggered=["security"],
            scan_duration_ms=200,
        )
        text = result.summary_text()
        assert "src/auth.py" in text
        assert "Critical" in text
        assert "2" in text  # total findings count


# ---------------------------------------------------------------------------
# Specialist trigger detection
# ---------------------------------------------------------------------------


class TestSpecialistTriggers:
    """Scanner._detect_specialist_triggers identifies relevant specialists."""

    def test_detect_security_triggers(self) -> None:
        scanner = Scanner()
        content = "def login(user_input):\n    query = f'SELECT * FROM users WHERE name={user_input}'\n"
        findings = [{"description": "SQL injection via unsanitized user input"}]

        triggers = scanner._detect_specialist_triggers(content, findings)
        assert "security" in [t.lower() for t in triggers]

    def test_detect_concurrency_triggers(self) -> None:
        scanner = Scanner()
        content = "import asyncio\nasync def fetch(): pass\n"
        findings = [{"description": "potential race condition in async handler"}]

        triggers = scanner._detect_specialist_triggers(content, findings)
        trigger_lower = [t.lower() for t in triggers]
        assert "concurrency" in trigger_lower

    def test_detect_no_triggers(self) -> None:
        scanner = Scanner()
        content = "x = 1\nprint(x)\n"
        findings: list[dict] = []

        triggers = scanner._detect_specialist_triggers(content, findings)
        assert triggers == []

    def test_detect_performance_triggers(self) -> None:
        scanner = Scanner()
        content = "for item in items:\n    db.query(item)\n"
        findings = [{"description": "N+1 query pattern detected in loop"}]

        triggers = scanner._detect_specialist_triggers(content, findings)
        trigger_lower = [t.lower() for t in triggers]
        assert "performance" in trigger_lower
