"""Tests for sentinel.triage — Interactive finding triage module.

Covers:
  - TriageDecision enum values
  - TriageResult creation
  - TriageSession computed properties and summary
  - TriageManager sorting, iteration, decision recording
  - Display formatting
  - Save/load round-trip persistence
  - Severity key ordering
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentinel.triage import (
    TriageDecision,
    TriageManager,
    TriageResult,
    TriageSession,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_finding(
    finding_id: str,
    severity: str = "P2",
    location: str = "src/app.py:10",
    category: str = "general",
    description: str = "Test finding",
) -> dict:
    """Create a minimal finding dict matching the standard schema."""
    return {
        "id": finding_id,
        "severity": severity,
        "location": location,
        "category": category,
        "description": description,
        "suggested_fix": "Fix it.",
        "status": "open",
    }


# ---------------------------------------------------------------------------
# TriageDecision enum
# ---------------------------------------------------------------------------


class TestTriageDecision:
    """TriageDecision enum has all expected values."""

    def test_triage_decision_enum_values(self) -> None:
        assert TriageDecision.ACCEPT == "accept"
        assert TriageDecision.DEFER == "defer"
        assert TriageDecision.WONT_FIX == "wont_fix"
        assert TriageDecision.DISPUTE == "dispute"

    def test_triage_decision_is_string(self) -> None:
        assert isinstance(TriageDecision.ACCEPT, str)


# ---------------------------------------------------------------------------
# TriageResult
# ---------------------------------------------------------------------------


class TestTriageResult:
    """TriageResult creation with all fields."""

    def test_triage_result_creation(self) -> None:
        result = TriageResult(
            finding_id="F-abc123",
            decision=TriageDecision.ACCEPT,
            rationale=None,
        )
        assert result.finding_id == "F-abc123"
        assert result.decision == TriageDecision.ACCEPT
        assert result.rationale is None

    def test_triage_result_with_rationale(self) -> None:
        result = TriageResult(
            finding_id="F-def456",
            decision=TriageDecision.DISPUTE,
            rationale="This is a false positive because the input is already sanitized.",
        )
        assert result.rationale is not None
        assert "false positive" in result.rationale


# ---------------------------------------------------------------------------
# TriageSession
# ---------------------------------------------------------------------------


class TestTriageSession:
    """TriageSession computed properties and summary."""

    def _make_session(self) -> TriageSession:
        return TriageSession(
            total=5,
            results=[
                TriageResult("F-001", TriageDecision.ACCEPT, None),
                TriageResult("F-002", TriageDecision.ACCEPT, None),
                TriageResult("F-003", TriageDecision.DEFER, None),
                TriageResult("F-004", TriageDecision.WONT_FIX, "accepted risk"),
                TriageResult("F-005", TriageDecision.DISPUTE, "false positive"),
            ],
        )

    def test_session_accepted_count(self) -> None:
        session = self._make_session()
        assert session.accepted == 2

    def test_session_deferred_count(self) -> None:
        session = self._make_session()
        assert session.deferred == 1

    def test_session_wont_fix_count(self) -> None:
        session = self._make_session()
        assert session.wont_fix == 1

    def test_session_disputed_count(self) -> None:
        session = self._make_session()
        assert session.disputed == 1

    def test_session_summary(self) -> None:
        session = self._make_session()
        summary = session.summary()
        assert isinstance(summary, dict)
        assert summary["total"] == 5
        assert summary["accepted"] == 2
        assert summary["deferred"] == 1
        assert summary["wont_fix"] == 1
        assert summary["disputed"] == 1

    def test_session_has_open_blockers_true(self) -> None:
        """True when a Critical finding was deferred (not accepted)."""
        findings = [
            _make_finding("F-001", severity="Critical"),
            _make_finding("F-002", severity="P2"),
        ]
        manager = TriageManager(findings)
        # Defer the Critical finding
        manager.record_decision("F-001", TriageDecision.DEFER)
        manager.record_decision("F-002", TriageDecision.ACCEPT)
        session = manager.get_session()
        assert session.has_open_blockers() is True

    def test_session_no_blockers(self) -> None:
        """False when all Critical/P1 findings are accepted."""
        findings = [
            _make_finding("F-001", severity="Critical"),
            _make_finding("F-002", severity="P1"),
            _make_finding("F-003", severity="P3"),
        ]
        manager = TriageManager(findings)
        manager.record_decision("F-001", TriageDecision.ACCEPT)
        manager.record_decision("F-002", TriageDecision.ACCEPT)
        manager.record_decision("F-003", TriageDecision.DEFER)
        session = manager.get_session()
        assert session.has_open_blockers() is False


# ---------------------------------------------------------------------------
# TriageManager
# ---------------------------------------------------------------------------


class TestTriageManager:
    """TriageManager sorting, iteration, and decision recording."""

    def test_manager_init_sorts_by_severity(self) -> None:
        findings = [
            _make_finding("F-p3", severity="P3"),
            _make_finding("F-crit", severity="Critical"),
            _make_finding("F-p1", severity="P1"),
        ]
        manager = TriageManager(findings)
        first = manager.get_next()
        assert first is not None
        assert first["severity"] == "Critical"

    def test_get_next_returns_highest_severity(self) -> None:
        findings = [
            _make_finding("F-p2", severity="P2"),
            _make_finding("F-p1", severity="P1"),
        ]
        manager = TriageManager(findings)
        first = manager.get_next()
        assert first is not None
        assert first["severity"] == "P1"

    def test_get_next_none_when_done(self) -> None:
        findings = [_make_finding("F-001")]
        manager = TriageManager(findings)
        manager.record_decision("F-001", TriageDecision.ACCEPT)
        assert manager.get_next() is None

    def test_record_decision_accept(self) -> None:
        findings = [_make_finding("F-001")]
        manager = TriageManager(findings)
        result = manager.record_decision("F-001", TriageDecision.ACCEPT)
        assert result.decision == TriageDecision.ACCEPT
        assert result.finding_id == "F-001"

    def test_record_decision_dispute_with_rationale(self) -> None:
        findings = [_make_finding("F-001")]
        manager = TriageManager(findings)
        result = manager.record_decision(
            "F-001",
            TriageDecision.DISPUTE,
            rationale="Already fixed upstream.",
        )
        assert result.rationale == "Already fixed upstream."

    def test_record_decision_unknown_finding(self) -> None:
        findings = [_make_finding("F-001")]
        manager = TriageManager(findings)
        with pytest.raises(ValueError, match="not found"):
            manager.record_decision("F-NONEXISTENT", TriageDecision.ACCEPT)


# ---------------------------------------------------------------------------
# Display formatting
# ---------------------------------------------------------------------------


class TestFormatFinding:
    """TriageManager.format_finding_for_display includes required elements."""

    def test_format_finding_for_display(self) -> None:
        finding = _make_finding(
            "F-042",
            severity="Critical",
            location="src/auth.py:42",
            category="security",
            description="SQL injection via unsanitized user input in login query.",
        )
        manager = TriageManager([finding])
        text = manager.format_finding_for_display(finding)

        assert "F-042" in text
        assert "Critical" in text
        assert "src/auth.py:42" in text
        assert "SQL injection" in text

    def test_format_includes_lettered_options(self) -> None:
        finding = _make_finding("F-001")
        manager = TriageManager([finding])
        text = manager.format_finding_for_display(finding)

        assert "A)" in text or "A." in text
        assert "B)" in text or "B." in text
        assert "C)" in text or "C." in text
        assert "D)" in text or "D." in text


# ---------------------------------------------------------------------------
# Save / Load persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    """TriageManager save and load round-trip."""

    def test_save_results(self, tmp_path: Path) -> None:
        findings = [_make_finding("F-001"), _make_finding("F-002")]
        manager = TriageManager(findings)
        manager.record_decision("F-001", TriageDecision.ACCEPT)
        manager.record_decision("F-002", TriageDecision.DEFER)

        output = tmp_path / "triage_results.json"
        manager.save_results(output)

        assert output.exists()
        data = json.loads(output.read_text(encoding="utf-8"))
        assert isinstance(data, list)
        assert len(data) == 2

    def test_load_results(self, tmp_path: Path) -> None:
        # Prepare a results file
        results_data = [
            {"finding_id": "F-001", "decision": "accept", "rationale": None},
            {"finding_id": "F-002", "decision": "defer", "rationale": "later"},
        ]
        output = tmp_path / "triage_results.json"
        output.write_text(json.dumps(results_data), encoding="utf-8")

        manager = TriageManager([])
        manager.load_results(output)

        session = manager.get_session()
        assert session.total == 2
        assert session.accepted == 1
        assert session.deferred == 1

    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        findings = [
            _make_finding("F-001", severity="Critical"),
            _make_finding("F-002", severity="P2"),
            _make_finding("F-003", severity="P3"),
        ]
        manager = TriageManager(findings)
        manager.record_decision("F-001", TriageDecision.ACCEPT)
        manager.record_decision("F-002", TriageDecision.WONT_FIX, rationale="accepted risk")
        manager.record_decision("F-003", TriageDecision.DISPUTE, rationale="false positive")

        output = tmp_path / "triage.json"
        manager.save_results(output)

        manager2 = TriageManager([])
        manager2.load_results(output)

        session = manager2.get_session()
        assert session.accepted == 1
        assert session.wont_fix == 1
        assert session.disputed == 1


# ---------------------------------------------------------------------------
# Severity key ordering
# ---------------------------------------------------------------------------


class TestSeverityKey:
    """TriageManager._severity_key orders severities correctly."""

    def test_severity_key_ordering(self) -> None:
        assert TriageManager._severity_key({"severity": "Critical"}) == 4
        assert TriageManager._severity_key({"severity": "P1"}) == 3
        assert TriageManager._severity_key({"severity": "P2"}) == 2
        assert TriageManager._severity_key({"severity": "P3"}) == 1
        assert TriageManager._severity_key({"severity": "unknown"}) == 0


# ---------------------------------------------------------------------------
# Full triage flow
# ---------------------------------------------------------------------------


class TestFullTriageSession:
    """End-to-end triage of multiple findings."""

    def test_full_triage_session(self) -> None:
        findings = [
            _make_finding("F-001", severity="Critical"),
            _make_finding("F-002", severity="P1"),
            _make_finding("F-003", severity="P3"),
        ]
        manager = TriageManager(findings)

        # Triage all findings in order
        f1 = manager.get_next()
        assert f1 is not None
        assert f1["severity"] == "Critical"
        manager.record_decision(f1["id"], TriageDecision.ACCEPT)

        f2 = manager.get_next()
        assert f2 is not None
        assert f2["severity"] == "P1"
        manager.record_decision(f2["id"], TriageDecision.DEFER)

        f3 = manager.get_next()
        assert f3 is not None
        assert f3["severity"] == "P3"
        manager.record_decision(f3["id"], TriageDecision.WONT_FIX, rationale="low risk")

        assert manager.get_next() is None

        session = manager.get_session()
        assert session.total == 3
        assert session.accepted == 1
        assert session.deferred == 1
        assert session.wont_fix == 1
