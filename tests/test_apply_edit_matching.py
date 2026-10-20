"""Whitespace-normalized fallback in apply_edits.

The Coder is shown the full source but reproduces ``old_text`` with enough
indentation/trailing-whitespace drift that an exact substring match fails and
the whole task is rejected ("submission contains no file changes"). apply_edits
now falls back to a whitespace-normalized line-span match when the exact match
misses, while leaving the exact fast path untouched.
"""
from __future__ import annotations

import pytest

from sentinel.workflow.remediation.apply import (
    EditApplicationError,
    apply_edits,
)


# 1. exact match still works (fast path, unchanged)
def test_exact_match_fast_path(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    modified = apply_edits(
        [{"file": "m.py", "old_text": "    return 1", "new_text": "    return 2"}],
        str(tmp_path),
    )
    assert modified == ["m.py"]
    assert f.read_text(encoding="utf-8") == "def foo():\n    return 2\n"


# 2. extra leading spaces in old_text -> normalized match applies correctly
def test_extra_leading_spaces_normalized_match(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    # Coder over-indents (8 spaces vs 4 on disk) -> exact fails, fallback fires
    modified = apply_edits(
        [{"file": "m.py", "old_text": "        return 1", "new_text": "    return 2"}],
        str(tmp_path),
    )
    assert modified == ["m.py"]
    assert f.read_text(encoding="utf-8") == "def foo():\n    return 2\n"


# 3. trailing whitespace on lines -> normalized match
def test_trailing_whitespace_normalized_match(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("x = 1\ny = 2\n", encoding="utf-8")
    # Coder appends trailing spaces to each line -> exact fails on both lines
    modified = apply_edits(
        [{"file": "m.py", "old_text": "x = 1 \ny = 2 ", "new_text": "x = 10\ny = 20"}],
        str(tmp_path),
    )
    assert modified == ["m.py"]
    assert f.read_text(encoding="utf-8") == "x = 10\ny = 20\n"


# 4. old_text genuinely not in file -> still raises, file untouched
def test_genuinely_absent_still_raises(tmp_path):
    f = tmp_path / "m.py"
    original = "def foo():\n    return 1\n"
    f.write_text(original, encoding="utf-8")
    with pytest.raises(EditApplicationError):
        apply_edits(
            [{"file": "m.py", "old_text": "    return 999  # not here", "new_text": "x"}],
            str(tmp_path),
        )
    assert f.read_text(encoding="utf-8") == original


# 5. normalized match locates the correct unique span; surroundings untouched
def test_normalized_match_locates_correct_span(tmp_path):
    f = tmp_path / "m.py"
    src = (
        "def a():\n"
        "    pass\n"
        "\n"
        "def target():\n"
        "    value = compute()\n"
        "    return value\n"
        "\n"
        "def b():\n"
        "    pass\n"
    )
    f.write_text(src, encoding="utf-8")
    # over-indented quote of the target block only
    old = "        value = compute()\n        return value"
    new = "    value = compute() + 1\n    return value"
    modified = apply_edits(
        [{"file": "m.py", "old_text": old, "new_text": new}], str(tmp_path)
    )
    assert modified == ["m.py"]
    out = f.read_text(encoding="utf-8")
    assert "value = compute() + 1" in out
    # surrounding functions are untouched and structure preserved
    assert "def a():\n    pass" in out
    assert "def b():\n    pass" in out
    assert out.count("def target():") == 1
    assert out.endswith("\n")


# 6. subtree-relative path (from a sentinel/ subtree assessment) resolves under
#    sentinel/ and records the real repo-relative path (run-011 batch bug).
def test_subtree_relative_path_resolves_under_sentinel(tmp_path):
    target = tmp_path / "sentinel" / "govern" / "conditions.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def get_effective_criteria():\n    return 1\n", encoding="utf-8"
    )
    modified = apply_edits(
        [{"file": "govern/conditions.py",
          "old_text": "    return 1", "new_text": "    return 2"}],
        str(tmp_path),
    )
    # effective repo-root-relative path recorded so git rollback/commit work
    assert modified == ["sentinel/govern/conditions.py"]
    assert target.read_text(encoding="utf-8") == (
        "def get_effective_criteria():\n    return 2\n"
    )


# 7. subtree resolution also applies on the whitespace-normalized fallback path
def test_subtree_path_with_whitespace_fallback(tmp_path):
    target = tmp_path / "sentinel" / "assess" / "grouper.py"
    target.parent.mkdir(parents=True)
    target.write_text("def f():\n    return 1\n", encoding="utf-8")
    modified = apply_edits(  # over-indented old_text -> fallback fires
        [{"file": "assess/grouper.py",
          "old_text": "        return 1", "new_text": "    return 2"}],
        str(tmp_path),
    )
    assert modified == ["sentinel/assess/grouper.py"]
    assert target.read_text(encoding="utf-8") == "def f():\n    return 2\n"


# 8. a path that exists at the given location is used as-is (no sentinel/ prefix)
def test_direct_path_preferred_over_subtree(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("a = 1\n", encoding="utf-8")
    modified = apply_edits(
        [{"file": "m.py", "old_text": "a = 1", "new_text": "a = 2"}],
        str(tmp_path),
    )
    assert modified == ["m.py"]


# 9. missing in both root and sentinel/ -> raises, message keeps the asked path
def test_missing_in_both_locations_raises(tmp_path):
    with pytest.raises(EditApplicationError) as exc:
        apply_edits(
            [{"file": "govern/nope.py", "old_text": "x", "new_text": "y"}],
            str(tmp_path),
        )
    assert "govern/nope.py" in str(exc.value)


def test_write_test_file_coerces_sentinel_prefix(tmp_path):
    # sentinel/tests/assess/test_foo.py ->
    # tests/assess/test_foo.py
    from sentinel.workflow.remediation.apply import (
        write_test_file,
    )
    result = write_test_file(
        "sentinel/tests/assess/test_foo.py",
        "# content",
        str(tmp_path),
    )
    assert result is not None
    assert "tests/assess/test_foo.py" in result.replace("\\", "/")
    assert "sentinel/tests" not in result.replace("\\", "/")


def test_write_test_file_coerces_missing_tests_prefix(tmp_path):
    # assess/test_foo.py -> tests/assess/test_foo.py
    from sentinel.workflow.remediation.apply import (
        write_test_file,
    )
    result = write_test_file(
        "assess/test_foo.py",
        "# content",
        str(tmp_path),
    )
    assert result is not None
    assert result.replace("\\", "/").endswith("tests/assess/test_foo.py") \
        or "tests" in result


def test_write_test_file_returns_repo_relative_path(tmp_path):
    # Regression: the return value feeds files_changed for the scoped commit, so
    # it must be repo-RELATIVE. An absolute return silently dropped the source
    # fix from a completion's commit (the -020 batch bug).
    import os
    from sentinel.workflow.remediation.apply import write_test_file
    result = write_test_file("tests/assess/test_foo.py", "# content", str(tmp_path))
    assert result == "tests/assess/test_foo.py"
    assert not os.path.isabs(result)
    assert str(tmp_path).replace("\\", "/") not in result.replace("\\", "/")
