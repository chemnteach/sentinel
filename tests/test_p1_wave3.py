"""Tests for P1 wave-3 remediations across smaller modules.

Covers, one test class per fixed module:
  - LearningStore.load_all corrupt-file tolerance
  - LearningStore.query min_severity filtering
  - retry_context_builder corrupt batch_summary logging + stale-lock recovery
  - FeasibilityOrchestrator resume-path validation
  - FERManager transition-lock release error escalation + stale-lock recovery
  - RetroAnalyzer._load_json_list warning on corrupt/unexpected input
  - ChatSession config isolation (no mutation of caller's Config)
  - Config chunk overlap < max invariant
  - TriageManager conservative severity default + total-findings count
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import pytest

from sentinel.learnings.schema import Learning, LearningCategory
from sentinel.learnings.store import LearningStore


# ── Shared helpers ────────────────────────────────────────────────────


def _make_learning(**overrides: Any) -> Learning:
    defaults = dict(
        id="LRN-20260315-001",
        source_repo="acme/backend",
        authored_by_foundry=False,
        category=LearningCategory.ERROR_HANDLING,
        severity="P2",
        pattern="Bare except swallows errors",
        anti_pattern_code=None,
        correct_pattern_code=None,
        applicable_to_generation=True,
        why="Found in 3 modules",
        sentinel_finding_ids=["F-abc123def0"],
        extracted_at="2026-03-15T10:00:00Z",
    )
    defaults.update(overrides)
    return Learning(**defaults)  # type: ignore[arg-type]


# ── LearningStore.load_all corrupt-file tolerance ─────────────────────


class TestLearningStoreLoadAllCorrupt:
    def test_corrupt_file_is_skipped_not_fatal(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = LearningStore(tmp_path / "learnings")
        store.save([_make_learning(id="LRN-20260315-001")])
        store.save([_make_learning(id="LRN-20260315-002")])

        # Corrupt one of the two learning files.
        bad = tmp_path / "learnings" / "LRN-20260315-001.json"
        bad.write_text("{ this is not valid json", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            loaded = store.load_all()

        # The good file still loads; the corrupt one is skipped.
        ids = {learning.id for learning in loaded}
        assert ids == {"LRN-20260315-002"}
        assert any("Skipping corrupt learning file" in r.message for r in caplog.records)

    def test_missing_key_file_is_skipped(self, tmp_path: Path) -> None:
        store = LearningStore(tmp_path / "learnings")
        store.save([_make_learning(id="LRN-20260315-002")])
        # A structurally-valid JSON object missing required keys triggers KeyError
        # in from_dict — must be skipped, not fatal.
        bad = tmp_path / "learnings" / "LRN-20260315-001.json"
        bad.write_text(json.dumps({"id": "LRN-20260315-001"}), encoding="utf-8")

        loaded = store.load_all()
        assert {learning.id for learning in loaded} == {"LRN-20260315-002"}


# ── LearningStore.query min_severity ──────────────────────────────────


class TestLearningStoreMinSeverity:
    def _seed(self, tmp_path: Path) -> LearningStore:
        store = LearningStore(tmp_path / "learnings")
        store.save([
            _make_learning(id="LRN-20260315-001", severity="Critical"),
            _make_learning(id="LRN-20260315-002", severity="P1"),
            _make_learning(id="LRN-20260315-003", severity="P2"),
            _make_learning(id="LRN-20260315-004", severity="P3"),
        ])
        return store

    def test_min_severity_none_returns_all(self, tmp_path: Path) -> None:
        store = self._seed(tmp_path)
        assert len(store.query()) == 4
        assert len(store.query(min_severity=None)) == 4

    def test_min_severity_p1_excludes_lower(self, tmp_path: Path) -> None:
        store = self._seed(tmp_path)
        result = {learning.severity for learning in store.query(min_severity="P1")}
        assert result == {"Critical", "P1"}

    def test_min_severity_critical_returns_only_critical(self, tmp_path: Path) -> None:
        store = self._seed(tmp_path)
        result = [learning.severity for learning in store.query(min_severity="Critical")]
        assert result == ["Critical"]

    def test_unknown_min_severity_applies_no_filter(self, tmp_path: Path) -> None:
        store = self._seed(tmp_path)
        assert len(store.query(min_severity="bogus")) == 4


# ── retry_context_builder ─────────────────────────────────────────────


class TestRetryContextBuilder:
    def test_corrupt_batch_summary_logs_and_starts_fresh(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from sentinel.orchestrator import retry_context_builder as rcb

        summary = tmp_path / "batch_summary.json"
        summary.write_text("{ not valid json", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            path = rcb.write_batch_summary(tmp_path, "T-001", "COMPLETE", 1)

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["tasks"]["T-001"]["status"] == "COMPLETE"
        assert any("Corrupt batch_summary.json" in r.message for r in caplog.records)

    def test_stale_lock_is_reclaimed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from sentinel.orchestrator import retry_context_builder as rcb

        # Simulate an abandoned lock: create the lock file and back-date its mtime
        # well beyond the stale threshold.
        summary = tmp_path / "batch_summary.json"
        lock = summary.with_suffix(f"{summary.suffix}.lock")
        lock.write_text("", encoding="utf-8")
        old = time.time() - (rcb._BATCH_SUMMARY_LOCK_STALE_SECONDS + 10)
        os.utime(lock, (old, old))

        with caplog.at_level(logging.WARNING):
            # Should reclaim the stale lock rather than time out.
            path = rcb.write_batch_summary(tmp_path, "T-002", "IN_PROGRESS", 2)

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["tasks"]["T-002"]["status"] == "IN_PROGRESS"
        assert any("Reclaimed stale batch_summary lock" in r.message for r in caplog.records)
        # Lock is released after the write.
        assert not lock.exists()

    def test_fresh_lock_is_not_reclaimed(self, tmp_path: Path) -> None:
        from sentinel.orchestrator import retry_context_builder as rcb

        summary = tmp_path / "batch_summary.json"
        lock = summary.with_suffix(f"{summary.suffix}.lock")
        lock.write_text("", encoding="utf-8")  # fresh mtime → not stale

        # A live (fresh) lock must NOT be reclaimed.
        assert rcb._reclaim_stale_lock(lock) is False
        assert lock.exists()


# ── Config chunk invariant ────────────────────────────────────────────


class TestConfigChunkInvariant:
    def test_overlap_ge_max_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sentinel.config import Config

        monkeypatch.setenv("SENTINEL_CHUNK_MAX_LINES", "100")
        monkeypatch.setenv("SENTINEL_CHUNK_OVERLAP_LINES", "100")
        with pytest.raises(ValueError, match="strictly less than"):
            Config()

    def test_overlap_greater_than_max_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sentinel.config import Config

        monkeypatch.setenv("SENTINEL_CHUNK_MAX_LINES", "50")
        monkeypatch.setenv("SENTINEL_CHUNK_OVERLAP_LINES", "200")
        with pytest.raises(ValueError):
            Config()

    def test_valid_overlap_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sentinel.config import Config

        monkeypatch.setenv("SENTINEL_CHUNK_MAX_LINES", "300")
        monkeypatch.setenv("SENTINEL_CHUNK_OVERLAP_LINES", "80")
        cfg = Config()
        assert cfg.chunk_overlap_lines < cfg.chunk_max_lines


# ── RetroAnalyzer._load_json_list ─────────────────────────────────────


class TestRetroLoadJsonList:
    def test_corrupt_file_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from sentinel.retro import RetroAnalyzer

        (tmp_path / "findings.json").write_text("{ nope", encoding="utf-8")
        analyzer = RetroAnalyzer(tmp_path)
        with caplog.at_level(logging.WARNING):
            result = analyzer._load_json_list("findings.json")
        assert result == []
        assert any("findings.json" in r.message for r in caplog.records)

    def test_non_list_json_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from sentinel.retro import RetroAnalyzer

        (tmp_path / "findings.json").write_text('{"a": 1}', encoding="utf-8")
        analyzer = RetroAnalyzer(tmp_path)
        with caplog.at_level(logging.WARNING):
            result = analyzer._load_json_list("findings.json")
        assert result == []
        assert any("did not contain a JSON list" in r.message for r in caplog.records)

    def test_valid_list_returned(self, tmp_path: Path) -> None:
        from sentinel.retro import RetroAnalyzer

        (tmp_path / "findings.json").write_text('[{"id": "F-1"}]', encoding="utf-8")
        analyzer = RetroAnalyzer(tmp_path)
        assert analyzer._load_json_list("findings.json") == [{"id": "F-1"}]


# ── TriageManager severity default + total ────────────────────────────


class TestTriageManager:
    def test_load_results_unknown_severity_defaults_critical(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from sentinel.triage import TriageDecision, TriageManager

        # Session starts with NO findings (e.g. reload-only path), then loads a
        # results file referencing an unknown finding.
        mgr = TriageManager(findings=[])
        results_file = tmp_path / "triage.json"
        results_file.write_text(
            json.dumps([
                {"finding_id": "F-unknown", "decision": "defer", "rationale": None}
            ]),
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING):
            mgr.load_results(results_file)

        # Unknown severity defaults to Critical (conservative), so a deferred
        # blocker is NOT hidden.
        session = mgr.get_session()
        assert session.has_open_blockers() is True
        assert any("defaulting to 'Critical'" in r.message for r in caplog.records)

    def test_total_reflects_all_findings_not_just_triaged(self) -> None:
        from sentinel.triage import TriageDecision, TriageManager

        findings = [
            {"id": "F-1", "severity": "P1"},
            {"id": "F-2", "severity": "P2"},
            {"id": "F-3", "severity": "P3"},
        ]
        mgr = TriageManager(findings=findings)
        # Only triage one of three.
        mgr.record_decision("F-1", TriageDecision.ACCEPT)

        session = mgr.get_session()
        assert session.total == 3
        assert len(session.results) == 1


# ── ChatSession config isolation ──────────────────────────────────────


class TestChatSessionConfigIsolation:
    def test_constructor_does_not_mutate_callers_config(self) -> None:
        from dataclasses import dataclass

        from sentinel.chat.session import ChatSession

        @dataclass
        class Cfg:
            self_approve_mode: bool = False
            chat_native_mode: bool = False
            local_only_mode: bool = False

        original = Cfg()
        session = ChatSession(config=original)

        # Caller's object is untouched...
        assert original.chat_native_mode is False
        assert original.local_only_mode is False
        # ...while the session's private copy carries the chat overrides.
        assert session._config.chat_native_mode is True
        assert session._config.local_only_mode is True
        assert session._config is not original

    def test_run_single_task_does_not_mutate_callers_config(self) -> None:
        from dataclasses import dataclass

        from sentinel.chat.session import ChatSession
        from sentinel.orchestrator.main import HumanGateRequired

        @dataclass
        class Cfg:
            self_approve_mode: bool = True
            chat_native_mode: bool = False
            local_only_mode: bool = False

        class _Orch:
            def __init__(self, cfg: Cfg) -> None:
                self.cfg = cfg

            def __enter__(self) -> "_Orch":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def run_phase2(self, task_id: str | None = None, limit: int | None = None) -> None:
                raise HumanGateRequired("local_review", {"task_id": task_id})

        import sentinel.chat.session as chat_mod

        original = Cfg()
        session = ChatSession(config=original)
        # Swap in the fake orchestrator on the module.
        saved = chat_mod.Orchestrator
        chat_mod.Orchestrator = _Orch  # type: ignore[assignment]
        try:
            session.run_single_task("task-1", self_approve=True)
        finally:
            chat_mod.Orchestrator = saved  # type: ignore[assignment]

        # run_single_task wrote self_approve_mode onto the PRIVATE copy, never
        # onto the caller's object.
        assert original.self_approve_mode is True  # unchanged baseline


# ── FeasibilityOrchestrator resume validation ─────────────────────────


def _valid_report(fer_id: str = "FER-001") -> dict[str, Any]:
    return {
        "fer_id": fer_id,
        "feasibility_verdict": "FEASIBLE",
        "effort_decomposition": {"new_code": {"size": "M"}},
        "risk_level": "LOW",
        "options": [{"approach_name": "A", "description": "d", "effort": "M"}],
        "recommended_approach": "A",
    }


class TestFeasibilityResumeValidation:
    def _setup(self, tmp_path: Path):
        from unittest.mock import MagicMock

        from sentinel.orchestrator.state_store import StateStore
        from sentinel.phase3.fer_manager import FERManager
        from sentinel.phase3.feasibility import FeasibilityOrchestrator

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = StateStore(state_dir=state_dir, repo_path=tmp_path)
        fer_mgr = FERManager(store=store)
        fer = fer_mgr.submit({
            "title": "Add CSV export",
            "business_justification": "Users need reports as CSV for spreadsheets.",
            "priority": "SHOULD_HAVE",
            "affected_systems": ["reporting"],
        })
        fer_mgr.triage(fer.fer_id, "APPROVED")
        fer_mgr.start_feasibility(fer.fer_id)
        architect = MagicMock()
        architect.assess_feasibility.return_value = _valid_report(fer.fer_id)
        orch = FeasibilityOrchestrator(fer_mgr, architect, store)
        return orch, architect, store, fer.fer_id

    def test_valid_cached_report_is_reused(self, tmp_path: Path) -> None:
        from sentinel.phase3.feasibility import HumanFeasibilityGateRequired

        orch, architect, store, fer_id = self._setup(tmp_path)
        store.upsert_in_list(
            "feasibility", _valid_report(fer_id), key_field="fer_id"
        )
        with pytest.raises(HumanFeasibilityGateRequired):
            orch.run(fer_id)
        # Valid cache → no model call.
        architect.assess_feasibility.assert_not_called()

    def test_invalid_cached_report_is_cleared_and_rerun(self, tmp_path: Path) -> None:
        from sentinel.phase3.feasibility import HumanFeasibilityGateRequired

        orch, architect, store, fer_id = self._setup(tmp_path)
        # Seed a corrupt/partial cached report missing required keys.
        store.upsert_in_list(
            "feasibility",
            {"fer_id": fer_id, "feasibility_verdict": "FEASIBLE"},
            key_field="fer_id",
        )
        with pytest.raises(HumanFeasibilityGateRequired):
            orch.run(fer_id)
        # Invalid cache → model IS re-run, and the fresh valid report is stored.
        architect.assess_feasibility.assert_called_once()
        reports = [r for r in store.read("feasibility") if r["fer_id"] == fer_id]
        assert len(reports) == 1
        assert reports[0]["options"]  # fresh valid report replaced the stub


# ── FERManager transition lock ────────────────────────────────────────


class TestFERTransitionLock:
    def _mgr(self, tmp_path: Path):
        from sentinel.orchestrator.state_store import StateStore
        from sentinel.phase3.fer_manager import FERManager

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = StateStore(state_dir=state_dir, repo_path=tmp_path)
        return FERManager(store=store), store

    def test_release_lock_raises_on_unlink_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        mgr, _ = self._mgr(tmp_path)
        lock_path = tmp_path / "state" / "fers.json.transition.lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)

        # Force unlink to fail (simulate a locked/held file).
        def _boom(self: Path) -> None:
            raise OSError("cannot remove")

        monkeypatch.setattr(Path, "unlink", _boom)
        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError, match="could not be released"):
                mgr._release_transition_lock(lock_path, fd)
        assert any("leaked" in r.message for r in caplog.records)

    def test_stale_transition_lock_is_reclaimed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from sentinel.phase3.fer_manager import _FER_TRANSITION_LOCK_STALE_SECONDS

        mgr, _ = self._mgr(tmp_path)
        lock_path = tmp_path / "state" / "fers.json.transition.lock"
        lock_path.write_text("", encoding="utf-8")
        old = time.time() - (_FER_TRANSITION_LOCK_STALE_SECONDS + 10)
        os.utime(lock_path, (old, old))

        with caplog.at_level(logging.WARNING):
            fd = mgr._acquire_transition_lock(lock_path)
        try:
            assert any("Reclaimed stale FER transition lock" in r.message for r in caplog.records)
        finally:
            os.close(fd)
            if lock_path.exists():
                lock_path.unlink()

    def test_fresh_transition_lock_not_reclaimed(self, tmp_path: Path) -> None:
        from sentinel.phase3.fer_manager import FERManager

        mgr, _ = self._mgr(tmp_path)
        lock_path = tmp_path / "state" / "fers.json.transition.lock"
        lock_path.write_text("", encoding="utf-8")  # fresh
        assert FERManager._reclaim_stale_transition_lock(lock_path) is False
        assert lock_path.exists()
