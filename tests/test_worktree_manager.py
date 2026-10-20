"""Tests for the Phase-1 worktree manager (hermetic — real git in tmp_path)."""
from __future__ import annotations

import subprocess

import pytest

from sentinel.orchestrator.worktree_manager import (
    ScopeViolation,
    WorktreeError,
    _parse_rename_tokens,
    create_worktree,
    discard_worktree,
    enforce_scope,
    get_worktree_changed_files,
    merge_worktree_to_main,
)


@pytest.fixture
def git_repo(tmp_path):
    """A real, throwaway git repo with HEAD committed (not D:/Git/Sentinel)."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    run("init")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Sentinel Test")
    run("config", "commit.gpgsign", "false")  # hermetic: don't depend on global signing
    (repo / "sentinel").mkdir()
    (repo / "tests").mkdir()
    (repo / "sentinel" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "tests" / "keep.py").write_text("# keep tests/ tracked\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "initial")
    return repo


def test_create_worktree_creates_dir_with_repo_files(git_repo):
    wt = create_worktree("t1", git_repo)
    assert wt.exists() and wt.is_dir()
    assert (wt / "sentinel" / "mod.py").exists()  # checked out repo content


def test_create_worktree_twice_raises(git_repo):
    create_worktree("t2", git_repo)
    with pytest.raises(WorktreeError):
        create_worktree("t2", git_repo)


def test_changed_files_returns_modified_tracked(git_repo):
    wt = create_worktree("t3", git_repo)
    (wt / "sentinel" / "mod.py").write_text("x = 2\n", encoding="utf-8")
    assert "sentinel/mod.py" in get_worktree_changed_files(wt)


def test_changed_files_returns_untracked_new(git_repo):
    wt = create_worktree("t4", git_repo)
    (wt / "tests" / "test_new.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    assert "tests/test_new.py" in get_worktree_changed_files(wt)


def test_merge_copies_changes_and_commits(git_repo):
    wt = create_worktree("t5", git_repo)
    (wt / "sentinel" / "mod.py").write_text("x = 42\n", encoding="utf-8")
    sha = merge_worktree_to_main(wt, "t5", "fix: bump x", git_repo, ["sentinel/mod.py"])
    assert isinstance(sha, str) and len(sha) >= 7
    assert (git_repo / "sentinel" / "mod.py").read_text(encoding="utf-8") == "x = 42\n"
    subj = subprocess.run(
        ["git", "-C", str(git_repo), "log", "-1", "--pretty=%s"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert subj == "fix: bump x"


def test_discard_removes_directory(git_repo):
    wt = create_worktree("t6", git_repo)
    assert wt.exists()
    discard_worktree(wt, git_repo)
    assert not wt.exists()


def test_discard_is_idempotent(git_repo):
    wt = create_worktree("t7", git_repo)
    discard_worktree(wt, git_repo)
    discard_worktree(wt, git_repo)  # no error on the second call
    assert not wt.exists()


def test_enforce_scope_passes_for_affected_and_test_files(git_repo):
    wt = create_worktree("t8", git_repo)
    (wt / "sentinel" / "mod.py").write_text("x = 3\n", encoding="utf-8")
    (wt / "tests" / "test_new.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    enforce_scope(wt, ["sentinel/mod.py"])  # must not raise


def test_enforce_scope_raises_on_out_of_scope_source(git_repo):
    wt = create_worktree("t9", git_repo)
    (wt / "sentinel" / "other.py").write_text("y = 1\n", encoding="utf-8")
    with pytest.raises(ScopeViolation) as ei:
        enforce_scope(wt, ["sentinel/mod.py"])
    assert "sentinel/other.py" in ei.value.violations


def test_enforce_scope_always_allows_test_files(git_repo):
    wt = create_worktree("t10", git_repo)
    (wt / "tests" / "test_anything.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    enforce_scope(wt, [])  # empty affected_files, but tests/ is always allowed


def test_enforce_scope_accepts_changed_files_list_directly():
    # Fallback path: no worktree — scope-check an explicit changed-file list.
    enforce_scope(
        worktree_path=None,
        affected_files=["sentinel/config.py"],
        changed_files=["sentinel/config.py", "tests/test_config.py"],
    )  # in scope (+ a test file) — must not raise


# --- FIX-BLOCKING-019/022: status-aware merge + scope over the same set ---


def test_merge_removes_deleted_file(git_repo):
    wt = create_worktree("t-del", git_repo)
    (wt / "sentinel" / "mod.py").unlink()  # delete a tracked file in the worktree
    sha = merge_worktree_to_main(
        wt, "t-del", "chore: drop mod", git_repo, ["sentinel/mod.py"]
    )
    assert isinstance(sha, str) and len(sha) >= 7
    # The deletion is applied to the target tree (no filesystem copy error).
    assert not (git_repo / "sentinel" / "mod.py").exists()


def test_merge_applies_renamed_file_to_destination(git_repo):
    wt = create_worktree("t-ren", git_repo)
    subprocess.run(
        ["git", "-C", str(wt), "mv", "sentinel/mod.py", "sentinel/renamed.py"],
        check=True, capture_output=True,
    )
    sha = merge_worktree_to_main(
        wt, "t-ren", "refactor: rename mod",
        git_repo, ["sentinel/mod.py", "sentinel/renamed.py"],
    )
    assert isinstance(sha, str)
    # Destination path applied; original removed — not treated as "old -> new".
    assert (git_repo / "sentinel" / "renamed.py").exists()
    assert not (git_repo / "sentinel" / "mod.py").exists()


def test_scope_gate_blocks_out_of_scope_deletion(git_repo):
    wt = create_worktree("t-del-oos", git_repo)
    (wt / "sentinel" / "mod.py").unlink()  # deletion outside affected_files
    with pytest.raises(ScopeViolation) as ei:
        # affected_files does NOT include sentinel/mod.py.
        merge_worktree_to_main(
            wt, "t-del-oos", "chore: drop", git_repo, ["sentinel/other.py"]
        )
    assert "sentinel/mod.py" in ei.value.violations



def test_enforce_scope_changed_files_list_out_of_scope_raises():
    with pytest.raises(ScopeViolation) as ei:
        enforce_scope(
            worktree_path=None,
            affected_files=["sentinel/config.py"],
            changed_files=["sentinel/config.py", "sentinel/other.py"],
        )
    assert "sentinel/other.py" in ei.value.violations


# --- FIX-CRITICAL run-019: path traversal containment + directory deletion ---


def test_merge_rejects_path_that_escapes_repo_root(tmp_path, monkeypatch):
    """A change path that resolves outside repo_root (via tests/.. traversal —
    which slips past the tests/ scope allowance) must be rejected before any
    filesystem sink, not delete/write a file outside the repo. (Issue 6)"""
    import sentinel.orchestrator.worktree_manager as wm

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    outside = tmp_path / "evil.py"
    outside.write_text("secret", encoding="utf-8")

    # A tracked deletion whose repo-relative path traverses out of the repo.
    monkeypatch.setattr(
        wm, "_enumerate_worktree_changes",
        lambda p: [("tests/../../evil.py", True)],
    )

    with pytest.raises(wm.WorktreeError):
        wm.merge_worktree_to_main(worktree, "t1", "msg", repo_root, ["tests/x.py"])

    assert outside.exists()  # containment blocked the delete; file outside repo intact


def test_merge_removes_directory_deletion_without_isadirectory_error(tmp_path, monkeypatch):
    """A deletion whose target resolves to a directory must be removed via
    rmtree rather than unlink (which raises on a directory). (Issue 7)"""
    import sentinel.orchestrator.worktree_manager as wm

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    target_dir = repo_root / "sentinel" / "pkg"
    target_dir.mkdir(parents=True)
    (target_dir / "f.py").write_text("x", encoding="utf-8")

    monkeypatch.setattr(
        wm, "_enumerate_worktree_changes",
        lambda p: [("sentinel/pkg", True)],
    )
    monkeypatch.setattr(wm, "_git", lambda *a, **k: "abc1234\n")

    sha = wm.merge_worktree_to_main(
        worktree, "t1", "msg", repo_root, ["sentinel/pkg"]
    )

    assert sha == "abc1234"
    assert not target_dir.exists()  # directory removed cleanly (no IsADirectoryError)


# ---------------------------------------------------------------------------
# _parse_rename_tokens (F2-007971827f5b)
# ---------------------------------------------------------------------------

class TestParseRenameTokens:
    def test_unquoted(self):
        orig, new = _parse_rename_tokens("old.py -> new.py")
        assert orig == "old.py"
        assert new == "new.py"

    def test_quoted_simple(self):
        orig, new = _parse_rename_tokens('"old path.py" -> "new path.py"')
        assert orig == '"old path.py"'
        assert new == '"new path.py"'

    def test_quoted_filename_with_arrow(self):
        """A filename that itself contains ' -> ' must not be split at the wrong point."""
        orig, new = _parse_rename_tokens('"old -> file.py" -> "new.py"')
        assert orig == '"old -> file.py"'
        assert new == '"new.py"'

    def test_quoted_with_escaped_quote(self):
        orig, new = _parse_rename_tokens(r'"old \"x\" file.py" -> "new.py"')
        assert orig == r'"old \"x\" file.py"'
        assert new == '"new.py"'


# ---------------------------------------------------------------------------
# merge_worktree_to_main rollback (F2-ecee62ebbf40)
# ---------------------------------------------------------------------------

def test_merge_rolls_back_partial_apply_on_mid_copy_failure(tmp_path, monkeypatch):
    """If a file copy fails mid-apply, already-applied changes must be rolled back
    so the repo root is not left in a half-merged state (FIX-CRITICAL run-020)."""
    import sentinel.orchestrator.worktree_manager as wm

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "sentinel").mkdir()
    (repo_root / "sentinel" / "a.py").write_text("original-a\n", encoding="utf-8")
    (repo_root / "sentinel" / "b.py").write_text("original-b\n", encoding="utf-8")

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "sentinel").mkdir()
    (worktree / "sentinel" / "a.py").write_text("modified-a\n", encoding="utf-8")
    (worktree / "sentinel" / "b.py").write_text("modified-b\n", encoding="utf-8")

    # After applying a.py, the copy of b.py raises — triggering rollback.
    call_count = [0]
    real_copyfile = __import__("shutil").copyfile

    def patched_copyfile(src, dst):
        call_count[0] += 1
        if call_count[0] == 2:
            raise OSError("simulated disk error")
        real_copyfile(src, dst)

    monkeypatch.setattr(
        wm, "_enumerate_worktree_changes",
        lambda p: [("sentinel/a.py", False), ("sentinel/b.py", False)],
    )
    monkeypatch.setattr("shutil.copyfile", patched_copyfile)
    # Make git calls no-ops so rollback via checkout doesn't need a real repo.
    monkeypatch.setattr(wm, "_git", lambda *a, **k: "")

    with pytest.raises(WorktreeError, match="mid-apply"):
        wm.merge_worktree_to_main(
            worktree, "t1", "msg", repo_root, ["sentinel/a.py", "sentinel/b.py"]
        )

    # b.py was never copied; a.py was applied but rollback attempted.
    # The key assertion: no half-merged b.py with modified content.
    assert (repo_root / "sentinel" / "b.py").read_text(encoding="utf-8") == "original-b\n"


# ---------------------------------------------------------------------------
# resolve_start_line containment (F2-e5323908fcbe / F2-0ea8d2d877ac)
# ---------------------------------------------------------------------------

def test_resolve_start_line_rejects_traversal(tmp_path):
    from sentinel.reports.source_reader import resolve_start_line

    outside = tmp_path / "secret.py"
    outside.write_text("def secret(): pass\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()

    finding = {"file": "../secret.py", "location": "../secret.py:secret", "line_range": ""}
    assert resolve_start_line(finding, str(repo)) is None


def test_resolve_start_line_rejects_absolute_outside_repo(tmp_path):
    from sentinel.reports.source_reader import resolve_start_line

    outside = tmp_path / "secret.py"
    outside.write_text("def secret(): pass\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()

    finding = {"file": str(outside), "location": f"{outside}:secret", "line_range": ""}
    assert resolve_start_line(finding, str(repo)) is None
