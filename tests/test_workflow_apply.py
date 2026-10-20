"""Tests for the Coder execution layer: apply edits + test runner (cycle 56)."""
from __future__ import annotations

import subprocess
import sys

import pytest

from sentinel.workflow.remediation.apply import (
    EditApplicationError,
    apply_edits,
    rollback_edits,
    write_test_file,
)
from sentinel.workflow.remediation.test_runner import run_tests


# --- apply_edits -------------------------------------------------------------

def test_apply_edits_simple(tmp_path):
    f = tmp_path / "m.py"
    f.write_text('def hello():\n    return "world"\n', encoding="utf-8")
    modified = apply_edits(
        [{"file": "m.py", "old_text": 'return "world"', "new_text": 'return "hi"'}],
        str(tmp_path),
    )
    assert modified == ["m.py"]
    assert 'return "hi"' in f.read_text(encoding="utf-8")


def test_apply_edits_old_text_not_found(tmp_path):
    f = tmp_path / "m.py"
    orig = "x = 1\n"
    f.write_text(orig, encoding="utf-8")
    with pytest.raises(EditApplicationError):
        apply_edits([{"file": "m.py", "old_text": "DOES_NOT_EXIST", "new_text": "y"}], str(tmp_path))
    assert f.read_text(encoding="utf-8") == orig  # unchanged


def test_apply_edits_multiple_edits(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("a = 1\nb = 2\n", encoding="utf-8")
    apply_edits(
        [
            {"file": "m.py", "old_text": "a = 1", "new_text": "a = 10"},
            {"file": "m.py", "old_text": "b = 2", "new_text": "b = 20"},
        ],
        str(tmp_path),
    )
    content = f.read_text(encoding="utf-8")
    assert "a = 10" in content and "b = 20" in content


def test_apply_edits_missing_file(tmp_path):
    with pytest.raises(EditApplicationError):
        apply_edits([{"file": "nope.py", "old_text": "x", "new_text": "y"}], str(tmp_path))


# --- write_test_file ---------------------------------------------------------

def test_write_test_file_creates_file(tmp_path):
    path = write_test_file("tests/test_x.py", "def test_x():\n    assert True\n", str(tmp_path))
    assert path is not None
    assert (tmp_path / "tests" / "test_x.py").read_text(encoding="utf-8").startswith("def test_x")


def test_write_test_file_empty_content(tmp_path):
    assert write_test_file("tests/test_x.py", "", str(tmp_path)) is None
    assert not (tmp_path / "tests" / "test_x.py").exists()


# --- rollback_edits (temp git repo — never touches the real repo) ------------

def test_rollback_edits_restores_file(tmp_path):
    repo = tmp_path

    def _git(*args):
        # Fail fast with visible diagnostics if any setup command errors, so a
        # broken fixture cannot masquerade as a passing rollback assertion.
        proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
        assert proc.returncode == 0, (
            f"git {' '.join(args)} failed (exit {proc.returncode})\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
        return proc

    _git("init")
    _git("config", "user.email", "t@t.dev")
    _git("config", "user.name", "t")
    f = repo / "src.py"
    orig = "value = 1\n"
    f.write_text(orig, encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "init")

    f.write_text("value = 999\n", encoding="utf-8")  # uncommitted edit
    rollback_edits(["src.py"], str(repo))
    assert f.read_text(encoding="utf-8") == orig


# --- run_tests ---------------------------------------------------------------

def test_run_tests_passing():
    r = run_tests(repo_root=".", test_command=f'{sys.executable} -c "import sys; sys.exit(0)"', timeout=30)
    assert r["passed"] is True
    assert r["returncode"] == 0


def test_run_tests_failing():
    r = run_tests(repo_root=".", test_command=f'{sys.executable} -c "import sys; sys.exit(1)"', timeout=30)
    assert r["passed"] is False


def test_run_tests_timeout():
    r = run_tests(repo_root=".", test_command=f'{sys.executable} -c "import time; time.sleep(10)"', timeout=1)
    assert r["passed"] is False
    assert "TIMEOUT" in r["output"]


def test_run_tests_does_not_interpret_shell_chaining(tmp_path):
    """Shell metacharacters (``&&``) must NOT be interpreted by a shell.

    Under the buggy ``shell=True`` implementation, the ``&&`` chains a second
    command that creates ``marker`` — proving arbitrary command execution.
    Under the fix (``shell=False`` + tokenized argv), ``&&`` and everything
    after it are inert literal arguments to the first ``python -c`` call, so the
    marker is never created.
    """
    marker = tmp_path / "pwned.txt"
    payload = marker.as_posix()
    injected = (
        f'{sys.executable} -c "pass" '
        f'&& {sys.executable} -c "open(r\'{payload}\', \'w\').close()"'
    )
    r = run_tests(repo_root=str(tmp_path), test_command=injected, timeout=30)

    assert not marker.exists(), "shell chaining was executed — injection not blocked"
    # Result shape must remain stable for current callers.
    assert {"passed", "returncode", "output", "summary"} <= set(r)


def test_run_tests_rejects_disallowed_executable(tmp_path):
    """A non-allowlisted executable is rejected before process launch."""
    r = run_tests(repo_root=str(tmp_path), test_command="definitely_not_a_test_runner --go", timeout=30)
    assert r["passed"] is False
    assert {"passed", "returncode", "output", "summary"} <= set(r)


def test_run_tests_accepts_argument_list(tmp_path):
    """An explicit argv list is accepted and executed with shell=False."""
    r = run_tests(repo_root=str(tmp_path), test_command=[sys.executable, "-c", "import sys; sys.exit(0)"], timeout=30)
    assert r["passed"] is True
    assert r["returncode"] == 0
