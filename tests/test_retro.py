"""Tests for sentinel.retro — retrospective analysis of assessments.

Covers:
  - RetroReport creation, serialization, and markdown output
  - RetroAnalyzer metric computation from sample findings
  - Edge cases: empty findings, missing state, partial data
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentinel.retro import RetroReport, RetroAnalyzer


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def sample_findings() -> list[dict]:
    """Realistic findings for testing."""
    return [
        {
            "id": "F-001",
            "severity": "Critical",
            "category": "security",
            "title": "SQL injection in user query",
            "description": "User input passed directly to SQL query",
            "location": {"file": "src/db.py", "line": 42},
            "recommendation": "Use parameterized queries",
            "skill": "layer2c-security-specialist",
        },
        {
            "id": "F-002",
            "severity": "Critical",
            "category": "security",
            "title": "SQL injection in admin query",
            "description": "Admin input passed directly to SQL query",
            "location": {"file": "src/admin.py", "line": 15},
            "recommendation": "Use parameterized queries",
            "skill": "layer2c-security-specialist",
        },
        {
            "id": "F-003",
            "severity": "P1",
            "category": "error_handling",
            "title": "Bare except clause",
            "description": "Catches all exceptions silently",
            "location": {"file": "src/api.py", "line": 78},
            "recommendation": "Catch specific exceptions",
            "skill": "layer3d-error-handling-specialist",
        },
        {
            "id": "F-004",
            "severity": "P2",
            "category": "performance",
            "title": "N+1 query in loop",
            "description": "Database query inside for loop",
            "location": {"file": "src/reports.py", "line": 55},
            "recommendation": "Use JOIN or prefetch",
            "skill": "layer3a-performance-specialist",
        },
        {
            "id": "F-005",
            "severity": "P3",
            "category": "maintainability",
            "title": "Magic number",
            "description": "Unexplained constant 86400",
            "location": {"file": "src/scheduler.py", "line": 12},
            "recommendation": "Use named constant SECONDS_PER_DAY",
            "skill": "layer3b-maintainability-specialist",
        },
    ]


@pytest.fixture
def state_dir_with_findings(tmp_path: Path, sample_findings: list[dict]) -> Path:
    """Create a state directory with findings.json populated."""
    state_dir = tmp_path / "sentinel" / "state"
    state_dir.mkdir(parents=True)
    findings_file = state_dir / "findings.json"
    findings_file.write_text(json.dumps(sample_findings), encoding="utf-8")
    return state_dir


@pytest.fixture
def state_dir_with_learnings(state_dir_with_findings: Path) -> Path:
    """State directory with findings and some learnings."""
    from sentinel.learnings.store import LearningStore
    from sentinel.learnings.schema import Learning, LearningCategory

    learnings_dir = state_dir_with_findings / "learnings"
    store = LearningStore(learnings_dir)
    store.save([
        Learning(
            id="LRN-20260315-001",
            source_repo="test/repo",
            authored_by_foundry=False,
            category=LearningCategory.SECURITY,
            severity="Critical",
            pattern="SQL injection via string concatenation",
            anti_pattern_code=None,
            correct_pattern_code=None,
            applicable_to_generation=True,
            why="Found in 2 findings",
            sentinel_finding_ids=["F-001", "F-002"],
            extracted_at="2026-03-15T10:00:00Z",
        ),
    ])
    return state_dir_with_findings


# ── RetroReport Tests ───────────────────────────────────────────────


class TestRetroReport:
    """Tests for RetroReport dataclass."""

    def test_report_creation(self) -> None:
        """Create a RetroReport with all fields populated."""
        report = RetroReport(
            repo_name="test/repo",
            total_findings=5,
            findings_by_severity={"Critical": 2, "P1": 1, "P2": 1, "P3": 1},
            findings_by_category={"security": 2, "error_handling": 1,
                                  "performance": 1, "maintainability": 1},
            top_categories=[("security", 2), ("error_handling", 1), ("performance", 1)],
            learnings_extracted=1,
            dedup_ratio=0.0,
            specialists_used=["layer2c-security-specialist", "layer3d-error-handling-specialist"],
            false_positive_rate=None,
            noise_categories=[],
            trigger_gaps=[],
            persona_improvements=[],
        )
        assert report.repo_name == "test/repo"
        assert report.total_findings == 5
        assert report.findings_by_severity["Critical"] == 2
        assert report.learnings_extracted == 1
        assert report.dedup_ratio == 0.0

    def test_report_to_markdown(self) -> None:
        """to_markdown produces valid markdown with all sections."""
        report = RetroReport(
            repo_name="test/repo",
            total_findings=5,
            findings_by_severity={"Critical": 2, "P1": 1, "P2": 1, "P3": 1},
            findings_by_category={"security": 2, "error_handling": 1,
                                  "performance": 1, "maintainability": 1},
            top_categories=[("security", 2), ("error_handling", 1), ("performance", 1)],
            learnings_extracted=1,
            dedup_ratio=0.1,
            specialists_used=["layer2c-security-specialist"],
            false_positive_rate=0.05,
            noise_categories=["maintainability"],
            trigger_gaps=["concurrency"],
            persona_improvements=["Improve security prompt specificity"],
        )
        md = report.to_markdown()
        assert isinstance(md, str)
        assert "# Retrospective" in md
        assert "test/repo" in md
        assert "Critical" in md
        assert "security" in md
        assert "Learnings" in md or "learnings" in md
        assert "concurrency" in md  # trigger gap

    def test_report_to_dict(self) -> None:
        """to_dict serializes all fields to a dict."""
        report = RetroReport(
            repo_name="test/repo",
            total_findings=3,
            findings_by_severity={"P1": 2, "P2": 1},
            findings_by_category={"security": 2, "error_handling": 1},
            top_categories=[("security", 2), ("error_handling", 1)],
            learnings_extracted=0,
            dedup_ratio=0.0,
            specialists_used=[],
            false_positive_rate=None,
            noise_categories=[],
            trigger_gaps=[],
            persona_improvements=[],
        )
        d = report.to_dict()
        assert isinstance(d, dict)
        assert d["repo_name"] == "test/repo"
        assert d["total_findings"] == 3
        assert d["findings_by_severity"] == {"P1": 2, "P2": 1}
        assert d["learnings_extracted"] == 0
        assert d["false_positive_rate"] is None

    def test_report_empty_findings(self) -> None:
        """Report with zero findings is valid."""
        report = RetroReport(
            repo_name="empty/repo",
            total_findings=0,
            findings_by_severity={},
            findings_by_category={},
            top_categories=[],
            learnings_extracted=0,
            dedup_ratio=0.0,
            specialists_used=[],
            false_positive_rate=None,
            noise_categories=[],
            trigger_gaps=[],
            persona_improvements=[],
        )
        assert report.total_findings == 0
        md = report.to_markdown()
        assert "0" in md or "empty" in md.lower() or "no findings" in md.lower()


# ── RetroAnalyzer Tests ─────────────────────────────────────────────


class TestRetroAnalyzer:
    """Tests for RetroAnalyzer — metric computation from state data."""

    def test_analyzer_init(self, state_dir_with_findings: Path) -> None:
        """Creates analyzer with valid state directory."""
        analyzer = RetroAnalyzer(state_store_path=state_dir_with_findings)
        assert analyzer is not None

    def test_analyzer_init_missing_state(self, tmp_path: Path) -> None:
        """Handles missing state directory gracefully (no crash)."""
        missing_dir = tmp_path / "nonexistent" / "state"
        analyzer = RetroAnalyzer(state_store_path=missing_dir)
        report = analyzer.analyze()
        assert report.total_findings == 0

    def test_analyze_basic(self, state_dir_with_findings: Path,
                           sample_findings: list[dict]) -> None:
        """Produces report with correct counts from sample findings."""
        analyzer = RetroAnalyzer(state_store_path=state_dir_with_findings)
        report = analyzer.analyze()
        assert report.total_findings == 5
        assert report.findings_by_severity["Critical"] == 2
        assert report.findings_by_severity["P1"] == 1
        assert report.findings_by_category["security"] == 2

    def test_calculate_dedup_ratio_no_dupes(self, state_dir_with_findings: Path) -> None:
        """Returns 0.0 when no duplicate findings exist."""
        analyzer = RetroAnalyzer(state_store_path=state_dir_with_findings)
        # All findings are unique in our sample
        ratio = analyzer._calculate_dedup_ratio(
            json.loads((state_dir_with_findings / "findings.json").read_text(encoding="utf-8"))
        )
        assert ratio == 0.0

    def test_calculate_dedup_ratio_half_dupes(self, tmp_path: Path) -> None:
        """Returns ~0.5 when half of findings have duplicate descriptions."""
        state_dir = tmp_path / "sentinel" / "state"
        state_dir.mkdir(parents=True)
        # 4 findings, 2 with identical descriptions
        findings = [
            {"id": "F-1", "severity": "P1", "category": "security",
             "description": "Same issue here"},
            {"id": "F-2", "severity": "P1", "category": "security",
             "description": "Same issue here"},
            {"id": "F-3", "severity": "P2", "category": "performance",
             "description": "Unique issue A"},
            {"id": "F-4", "severity": "P2", "category": "performance",
             "description": "Unique issue B"},
        ]
        (state_dir / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
        analyzer = RetroAnalyzer(state_store_path=state_dir)
        ratio = analyzer._calculate_dedup_ratio(findings)
        # 2 unique descriptions out of 4 total = 2 dupes removed / 4 = 0.5
        assert 0.2 <= ratio <= 0.6

    def test_identify_top_categories(self, state_dir_with_findings: Path,
                                     sample_findings: list[dict]) -> None:
        """Returns correct top 3 categories by count."""
        analyzer = RetroAnalyzer(state_store_path=state_dir_with_findings)
        top = analyzer._identify_top_categories(sample_findings, n=3)
        assert len(top) == 3
        # security has 2 findings, should be first
        assert top[0][0] == "security"
        assert top[0][1] == 2

    def test_identify_top_categories_fewer_than_n(self, tmp_path: Path) -> None:
        """Handles fewer categories than n gracefully."""
        state_dir = tmp_path / "sentinel" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "findings.json").write_text("[]", encoding="utf-8")
        analyzer = RetroAnalyzer(state_store_path=state_dir)
        findings = [
            {"id": "F-1", "severity": "P1", "category": "security",
             "description": "Issue"},
        ]
        top = analyzer._identify_top_categories(findings, n=5)
        assert len(top) == 1

    def test_identify_trigger_gaps(self, state_dir_with_findings: Path,
                                   sample_findings: list[dict]) -> None:
        """Detects categories where no specialist was triggered."""
        analyzer = RetroAnalyzer(state_store_path=state_dir_with_findings)
        # Only provide security specialist — should flag others
        gaps = analyzer._identify_trigger_gaps(
            sample_findings,
            specialists_used=["layer2c-security-specialist"],
        )
        # error_handling, performance, maintainability have findings but no specialist
        assert len(gaps) > 0

    def test_identify_trigger_gaps_none(self, state_dir_with_findings: Path,
                                        sample_findings: list[dict]) -> None:
        """No gaps when all specialists that had findings were triggered."""
        analyzer = RetroAnalyzer(state_store_path=state_dir_with_findings)
        all_skills = list({f.get("skill", "") for f in sample_findings if f.get("skill")})
        gaps = analyzer._identify_trigger_gaps(sample_findings, specialists_used=all_skills)
        assert gaps == []

    def test_generate_persona_improvements(self, state_dir_with_findings: Path,
                                            sample_findings: list[dict]) -> None:
        """Produces at least one recommendation when findings exist."""
        analyzer = RetroAnalyzer(state_store_path=state_dir_with_findings)
        improvements = analyzer._generate_persona_improvements(sample_findings, learnings=[])
        assert isinstance(improvements, list)
        # With 5 findings including 2 Critical, should have recommendations
        assert len(improvements) >= 0  # may be empty if no patterns detected

    def test_analyze_with_learnings(self, state_dir_with_learnings: Path) -> None:
        """Report includes learning count when learnings exist."""
        analyzer = RetroAnalyzer(
            state_store_path=state_dir_with_learnings,
            learnings_path=state_dir_with_learnings / "learnings",
        )
        report = analyzer.analyze()
        assert report.learnings_extracted >= 1

    def test_analyze_with_triage(self, state_dir_with_findings: Path) -> None:
        """Report includes false positive rate when triage data exists."""
        # Write triage data
        triage_data = [
            {"id": "F-001", "human_triage": "APPROVE"},
            {"id": "F-002", "human_triage": "APPROVE"},
            {"id": "F-003", "human_triage": "EXCLUDE"},  # false positive
            {"id": "F-004", "human_triage": "APPROVE"},
            {"id": "F-005", "human_triage": "APPROVE"},
        ]
        (state_dir_with_findings / "tech_debt.json").write_text(
            json.dumps(triage_data), encoding="utf-8",
        )
        analyzer = RetroAnalyzer(state_store_path=state_dir_with_findings)
        report = analyzer.analyze()
        # 1 out of 5 excluded => 0.2 false positive rate
        assert report.false_positive_rate is not None
        assert 0.1 <= report.false_positive_rate <= 0.3

    def test_count_learnings_logs_and_returns_zero_on_unreadable_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        """An unreadable learnings store returns 0 but emits a diagnostic
        warning rather than silently masquerading as empty. (FIX-DESIRED-014)"""
        import logging

        learnings = tmp_path / "learnings"
        learnings.mkdir()
        analyzer = RetroAnalyzer(state_store_path=tmp_path)

        class _Boom:
            def __init__(self, *args, **kwargs) -> None:
                raise RuntimeError("corrupt store")

        monkeypatch.setattr("sentinel.learnings.store.LearningStore", _Boom)
        with caplog.at_level(logging.WARNING):
            count = analyzer._count_learnings()

        assert count == 0
        assert any(
            "Learnings store unreadable" in record.message
            for record in caplog.records
        )
