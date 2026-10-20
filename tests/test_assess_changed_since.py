"""Tests for `sentinel assess --changed-since <sha>` targeted verification pass."""
from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

import sentinel.cli as cli_module
from sentinel.cli import main, _changed_source_files_since
import sentinel.config as config_module


@pytest.fixture(autouse=True)
def _reset_global_config_singleton():
    config_module._config = None
    yield
    config_module._config = None


# ---------------------------------------------------------------------------
# git helpers for building a real temp repo
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr}")
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# Fake Orchestrator to record run_phase1 calls
# ---------------------------------------------------------------------------

class _FakeOrch:
    last_instance: "_FakeOrch | None" = None

    def __init__(self, cfg):
        self.cfg = cfg
        self.calls: list[tuple] = []
        _FakeOrch.last_instance = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run_phase1(self, repo_paths, restricted_files=None, resume_review=False):
        self.calls.append((list(repo_paths), restricted_files))


# ---------------------------------------------------------------------------
# Test 1 — no changed files exits cleanly
# ---------------------------------------------------------------------------

def test_changed_since_no_files_exits_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "Orchestrator", _FakeOrch)
    monkeypatch.setattr(cli_module, "_changed_source_files_since", lambda repo, sha: [])

    result = CliRunner().invoke(
        main,
        ["assess", str(tmp_path), "--changed-since", "abc1234"],
        env={"SENTINEL_STATE_DIR": str(tmp_path / "state")},
    )

    assert result.exit_code == 0, result.output
    assert "No source files changed" in result.output
    # Assessment pipeline must NOT run when nothing changed.
    assert _FakeOrch.last_instance is not None
    assert _FakeOrch.last_instance.calls == []


# ---------------------------------------------------------------------------
# Test 2 — 2 changed files restrict the file list to those 2
# ---------------------------------------------------------------------------

def test_changed_since_two_files_passed_to_run_phase1(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "Orchestrator", _FakeOrch)
    monkeypatch.setattr(
        cli_module, "_changed_source_files_since",
        lambda repo, sha: ["sentinel/a.py", "sentinel/b.py"],
    )

    result = CliRunner().invoke(
        main,
        ["assess", str(tmp_path), "--changed-since", "abc1234"],
        env={"SENTINEL_STATE_DIR": str(tmp_path / "state")},
    )

    assert result.exit_code == 0, result.output
    inst = _FakeOrch.last_instance
    assert inst is not None
    assert len(inst.calls) == 1
    _paths, restricted = inst.calls[0]
    assert restricted == ["sentinel/a.py", "sentinel/b.py"]


def test_changed_source_files_since_filters_to_py_and_cs(tmp_path):
    repo = tmp_path
    _init_repo(repo)
    (repo / "keep.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "note.txt").write_text("hello\n", encoding="utf-8")
    base = _commit_all(repo, "base")

    (repo / "keep.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "svc.cs").write_text("class C {}\n", encoding="utf-8")
    (repo / "note.txt").write_text("changed\n", encoding="utf-8")
    _commit_all(repo, "changes")

    changed = _changed_source_files_since(repo, base)

    assert sorted(changed) == ["keep.py", "svc.cs"]  # note.txt filtered out


# ---------------------------------------------------------------------------
# Test 3 — invalid SHA raises a clear error
# ---------------------------------------------------------------------------

def test_changed_source_files_since_invalid_sha_raises(tmp_path):
    repo = tmp_path
    _init_repo(repo)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "base")

    with pytest.raises(click.ClickException) as exc:
        _changed_source_files_since(repo, "deadbeefdeadbeef")

    assert "deadbeefdeadbeef" in str(exc.value.message)
    assert "commit" in str(exc.value.message).lower()


def test_changed_since_invalid_sha_cli_reports_error(tmp_path, monkeypatch):
    repo = tmp_path
    _init_repo(repo)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "base")

    monkeypatch.setattr(cli_module, "Orchestrator", _FakeOrch)

    result = CliRunner().invoke(
        main,
        ["assess", str(repo), "--changed-since", "deadbeefdeadbeef"],
        env={"SENTINEL_STATE_DIR": str(tmp_path / "state")},
    )

    assert result.exit_code != 0
    assert "deadbeefdeadbeef" in result.output


# ---------------------------------------------------------------------------
# Test 4 — CLI command registered and --help works
# ---------------------------------------------------------------------------

def test_assess_help_lists_changed_since():
    result = CliRunner().invoke(main, ["assess", "--help"])
    assert result.exit_code == 0
    assert "--changed-since" in result.output
