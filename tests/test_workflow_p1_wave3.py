"""P1 wave-3 regression tests: error-signal-loss and contract fixes across
workflow files.

Covers:
  * build_struct_themes: WARNING on unresolved member finding IDs; theme
    member_finding_ids emits only resolved ids (contract).
  * struct_theme_loader.load_semantic_groups: distinct log levels per failure
    mode (absent silent, OSError/JSON error ERROR, wrong-type WARNING).
  * sync_state: load_sync_state WARNING on corrupt (not on absent);
    WorkflowSyncState.from_dict type coercion + outcome validation.
  * apply.rollback_edits: returns failed paths and checks git returncode.
  * consequence_synth.save_cache: returns bool; synthesize_all reports only
    on success.
"""
from __future__ import annotations

import json
import logging

import pytest

from sentinel.workflow.build_struct_themes import build_struct_themes
from sentinel.workflow.struct_theme_loader import (
    _GROUPS_FILENAME,
    load_semantic_groups,
)
from sentinel.workflow.sync_state import (
    WorkflowSyncState,
    _SYNC_STATE_FILENAME,
    load_sync_state,
)
from sentinel.workflow.remediation.apply import rollback_edits
from sentinel.reports import consequence_synth

# Reuse the format-correct fixture builders from the existing suites.
from tests.test_workflow_build_struct_themes import _make_group, _make_finding


# --- build_struct_themes: unresolved member ids ------------------------------

def test_build_struct_themes_warns_on_unresolved_member_ids(caplog):
    group = _make_group(members=("F1", "F2", "MISSING"))
    findings = [_make_finding("F1", "summary one"), _make_finding("F2", "summary two")]
    with caplog.at_level(logging.WARNING, logger="sentinel.workflow.build_struct_themes"):
        themes = build_struct_themes([group], findings)
    assert any(
        "unresolved" in rec.getMessage() and "1" in rec.getMessage()
        for rec in caplog.records
    ), caplog.text
    # Contract: only the resolved ids are emitted, MISSING is dropped.
    assert themes[0].member_finding_ids == ["F1", "F2"]


def test_build_struct_themes_no_warning_when_all_resolved(caplog):
    group = _make_group(members=("F1", "F2"))
    findings = [_make_finding("F1", "s1"), _make_finding("F2", "s2")]
    with caplog.at_level(logging.WARNING, logger="sentinel.workflow.build_struct_themes"):
        themes = build_struct_themes([group], findings)
    assert not caplog.records
    assert themes[0].member_finding_ids == ["F1", "F2"]


def test_build_struct_themes_empty_members_keeps_declared_ids():
    # Degenerate path (no findings plumbed in): membership preserved from group.
    group = _make_group(members=("F1", "F2"))
    themes = build_struct_themes([group], [])
    assert themes[0].member_finding_ids == ["F1", "F2"]


# --- struct_theme_loader.load_semantic_groups: failure modes -----------------

def test_load_semantic_groups_absent_is_silent(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="sentinel.workflow.struct_theme_loader"):
        result = load_semantic_groups(tmp_path)
    assert result == ()
    assert not caplog.records


def test_load_semantic_groups_invalid_json_logs_error(tmp_path, caplog):
    (tmp_path / _GROUPS_FILENAME).write_text("{ not json", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="sentinel.workflow.struct_theme_loader"):
        result = load_semantic_groups(tmp_path)
    assert result == ()
    assert any(rec.levelno == logging.ERROR for rec in caplog.records)
    assert any("Corrupt" in rec.getMessage() for rec in caplog.records)


def test_load_semantic_groups_wrong_type_logs_warning(tmp_path, caplog):
    (tmp_path / _GROUPS_FILENAME).write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sentinel.workflow.struct_theme_loader"):
        result = load_semantic_groups(tmp_path)
    assert result == ()
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)
    assert not any(rec.levelno == logging.ERROR for rec in caplog.records)


def test_load_semantic_groups_oserror_logs_error(tmp_path, caplog, monkeypatch):
    path = tmp_path / _GROUPS_FILENAME
    path.write_text("[]", encoding="utf-8")

    from pathlib import Path as _Path

    orig_read_text = _Path.read_text

    def boom(self, *args, **kwargs):
        if self.name == _GROUPS_FILENAME:
            raise OSError("disk gone")
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "read_text", boom)
    with caplog.at_level(logging.ERROR, logger="sentinel.workflow.struct_theme_loader"):
        result = load_semantic_groups(tmp_path)
    assert result == ()
    assert any(
        rec.levelno == logging.ERROR and "Failed to read" in rec.getMessage()
        for rec in caplog.records
    )


# --- sync_state -------------------------------------------------------------

def test_load_sync_state_absent_is_silent(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="sentinel.workflow.sync_state"):
        state = load_sync_state(tmp_path)
    assert state == WorkflowSyncState()
    assert not caplog.records


def test_load_sync_state_corrupt_logs_warning(tmp_path, caplog):
    (tmp_path / _SYNC_STATE_FILENAME).write_text("{ not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sentinel.workflow.sync_state"):
        state = load_sync_state(tmp_path)
    assert state == WorkflowSyncState()
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_load_sync_state_wrong_shape_logs_warning(tmp_path, caplog):
    (tmp_path / _SYNC_STATE_FILENAME).write_text(json.dumps(["a", "b"]), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sentinel.workflow.sync_state"):
        state = load_sync_state(tmp_path)
    assert state == WorkflowSyncState()
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_from_dict_coerces_non_string_values():
    state = WorkflowSyncState.from_dict(
        {
            "last_sync_timestamp": 12345,
            "last_sync_outcome": "success",
            "last_sync_error_summary": None,
        }
    )
    assert state.last_sync_timestamp == "12345"
    assert state.last_sync_outcome == "success"
    assert state.last_sync_error_summary == ""


def test_from_dict_rejects_unknown_outcome(caplog):
    with caplog.at_level(logging.WARNING, logger="sentinel.workflow.sync_state"):
        state = WorkflowSyncState.from_dict({"last_sync_outcome": "bogus"})
    assert state.last_sync_outcome == "never"
    assert any("bogus" in rec.getMessage() for rec in caplog.records)


def test_from_dict_none_outcome_defaults_to_never():
    state = WorkflowSyncState.from_dict({"last_sync_outcome": None})
    assert state.last_sync_outcome == "never"


@pytest.mark.parametrize("outcome", ["never", "success", "failure"])
def test_from_dict_accepts_valid_outcomes(outcome):
    state = WorkflowSyncState.from_dict({"last_sync_outcome": outcome})
    assert state.last_sync_outcome == outcome


# --- rollback_edits ---------------------------------------------------------

def _init_git_repo(repo, run):
    run("init")
    run("config", "user.email", "t@t.dev")
    run("config", "user.name", "t")


def test_rollback_edits_returns_empty_on_success(tmp_path):
    import subprocess

    repo = tmp_path

    def run(*args):
        proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return proc

    _init_git_repo(repo, run)
    f = repo / "src.py"
    f.write_text("value = 1\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "init")
    f.write_text("value = 999\n", encoding="utf-8")

    failed = rollback_edits(["src.py"], str(repo))
    assert failed == []
    assert f.read_text(encoding="utf-8") == "value = 1\n"


def test_rollback_edits_reports_untracked_path_as_failed(tmp_path, caplog):
    import subprocess

    repo = tmp_path

    def run(*args):
        proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return proc

    _init_git_repo(repo, run)
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "init")

    # "ghost.py" is not tracked -> git checkout -- ghost.py exits non-zero.
    with caplog.at_level(logging.WARNING, logger="sentinel.workflow.remediation.apply"):
        failed = rollback_edits(["ghost.py"], str(repo))
    assert failed == ["ghost.py"]
    assert any("Rollback failed" in rec.getMessage() for rec in caplog.records)


def test_rollback_edits_subprocess_exception_reports_failed(tmp_path, monkeypatch):
    import subprocess

    def boom(*args, **kwargs):
        raise FileNotFoundError("git not installed")

    monkeypatch.setattr(subprocess, "run", boom)
    failed = rollback_edits(["a.py", "b.py"], str(tmp_path))
    assert failed == ["a.py", "b.py"]


# --- consequence_synth.save_cache -------------------------------------------

def test_save_cache_returns_true_on_success(tmp_path):
    ok = consequence_synth.save_cache(str(tmp_path), "factory", {"F1": {"best_case": "x"}})
    assert ok is True
    data = json.loads((tmp_path / consequence_synth._CACHE_FILENAME).read_text(encoding="utf-8"))
    assert data["domain"] == "factory"


def test_save_cache_returns_false_on_write_error(tmp_path, monkeypatch):
    from pathlib import Path as _Path

    def boom(self, *args, **kwargs):
        raise OSError("read-only fs")

    monkeypatch.setattr(_Path, "write_text", boom)
    ok = consequence_synth.save_cache(str(tmp_path), "factory", {"F1": {}})
    assert ok is False


def test_synthesize_all_reports_failure_when_save_fails(tmp_path, monkeypatch, capsys):
    findings = [{"id": "F1", "finding_kind": "k"}]
    consequences = {"k": {"factory": {"best_case": "b", "likely": "l", "worst_case": "w"}}}

    class _Client:
        def call_model(self, system_prompt="", user_message="", **kw):
            return type("R", (), {"content": '{"best_case":"b","likely":"l","worst_case":"w"}'})()

    monkeypatch.setattr(consequence_synth, "save_cache", lambda *a, **k: False)
    consequence_synth.synthesize_all(
        findings, consequences, "factory", {"F1": None}, str(tmp_path), _Client()
    )
    err = capsys.readouterr().err
    assert "Cache save FAILED" in err
    assert "Cache saved:" not in err


def test_synthesize_all_reports_success_when_save_succeeds(tmp_path, monkeypatch, capsys):
    findings = [{"id": "F1", "finding_kind": "k"}]
    consequences = {"k": {"factory": {"best_case": "b", "likely": "l", "worst_case": "w"}}}

    class _Client:
        def call_model(self, system_prompt="", user_message="", **kw):
            return type("R", (), {"content": '{"best_case":"b","likely":"l","worst_case":"w"}'})()

    monkeypatch.setattr(consequence_synth, "save_cache", lambda *a, **k: True)
    consequence_synth.synthesize_all(
        findings, consequences, "factory", {"F1": None}, str(tmp_path), _Client()
    )
    err = capsys.readouterr().err
    assert "Cache saved:" in err
    assert "Cache save FAILED" not in err
