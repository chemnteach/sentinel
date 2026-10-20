"""Tests for the render-time source reader (sentinel/reports/source_reader.py)."""
from __future__ import annotations

from sentinel.reports.source_reader import (
    github_link,
    read_finding_source,
    resolve_start_line,
    vscode_link,
)

_GH = (
    "https://github.example.com/example-org/"
    "example-tool-repo"
)
_SHA = "abc1234def5678"


def test_read_finding_source_with_line_range(tmp_path):
    f = tmp_path / "known.py"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)), encoding="utf-8")
    finding = {"file": "known.py", "line_range": "2-5", "location": ""}

    src = read_finding_source(finding, str(tmp_path))

    assert src is not None
    # The requested 2-5 window (with ±5 context) must include those lines.
    for n in (2, 3, 4, 5):
        assert f"line{n}" in src
    assert src.splitlines()[0].startswith("# known.py lines")


def test_read_finding_source_function_search(tmp_path):
    f = tmp_path / "test_file.py"
    f.write_text("import os\n\ndef my_func():\n    return 42\n", encoding="utf-8")
    finding = {"file": "test_file.py", "line_range": "", "location": "test_file.py:my_func"}

    src = read_finding_source(finding, str(tmp_path))

    assert src is not None
    assert "def my_func" in src


def test_read_finding_source_malformed_line_range(tmp_path):
    """A malformed range like '42-' (start parses, end does not) must not crash
    -- previously left end_line None and raised TypeError on end_line + 5."""
    f = tmp_path / "m.py"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)), encoding="utf-8")
    for bad in ("42-", "42-x", "-", "abc"):
        finding = {"file": "m.py", "line_range": bad, "location": "m.py:nofunc"}
        out = read_finding_source(finding, str(tmp_path))  # must not raise
        assert out is None or isinstance(out, str)


def test_read_finding_source_out_of_range_line_falls_back_to_function(tmp_path):
    """A line_range past EOF (models emit inflated line numbers) must fall back
    to the function named in `location`, not return None / empty source."""
    f = tmp_path / "m.py"
    f.write_text("import os\n\n\ndef target_fn():\n    return 1\n", encoding="utf-8")
    # 5-line file, but the finding cites lines 592-651, with the real function
    # name (plus a trailing ", lines …" the way the model emits it).
    finding = {
        "file": "m.py",
        "line_range": "592-651",
        "location": "m.py:target_fn, lines 592-651",
    }
    out = read_finding_source(finding, str(tmp_path))
    assert out is not None
    assert "def target_fn" in out  # fell back to the function, not blank


def test_read_finding_source_missing_file(tmp_path):
    finding = {"file": "does_not_exist.py", "line_range": "1-5", "location": ""}

    assert read_finding_source(finding, str(tmp_path)) is None


# --- FIX-CRITICAL run-019: path-traversal containment (Issue 8) ---
def test_read_finding_source_rejects_absolute_path_outside_repo(tmp_path):
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("SECRET", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    # An absolute path escaping the repo must not be read.
    assert read_finding_source({"file": str(outside)}, str(repo)) is None


def test_read_finding_source_rejects_traversal_outside_repo(tmp_path):
    outside = tmp_path / "secret.py"
    outside.write_text("SECRET", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    # ../secret.py traverses out of the repo root.
    assert read_finding_source({"file": "../secret.py"}, str(repo)) is None


def test_vscode_link_rejects_traversal_outside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert vscode_link({"file": "../../etc/passwd", "line_range": ""}, str(repo)) is None


def test_vscode_link_rejects_absolute_outside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "evil.py"
    outside.write_text("x", encoding="utf-8")
    assert vscode_link({"file": str(outside), "line_range": ""}, str(repo)) is None


def test_vscode_link_with_line_range():
    link = vscode_link({"file": "foo.py", "line_range": "10-20"}, "/repo")

    assert link is not None
    assert ":10" in link


def test_vscode_link_no_line_range():
    link = vscode_link({"file": "foo.py", "line_range": ""}, "/repo")

    assert link is not None
    assert link.endswith("foo.py")


def test_vscode_link_no_file():
    assert vscode_link({"file": "", "location": ""}, "/repo") is None


def test_resolve_start_line_finds_function(tmp_path):
    f = tmp_path / "file.py"
    f.write_text("def my_func():\n    return 42\n", encoding="utf-8")
    finding = {"file": "file.py", "location": "file.py:my_func", "line_range": ""}

    assert resolve_start_line(finding, str(tmp_path)) == 1


def test_resolve_start_line_skips_when_line_range_present(tmp_path):
    finding = {"file": "file.py", "location": "file.py:my_func", "line_range": "5-10"}

    assert resolve_start_line(finding, str(tmp_path)) is None


def test_resolve_start_line_missing_file(tmp_path):
    finding = {"file": "nope.py", "location": "nope.py:my_func", "line_range": ""}

    assert resolve_start_line(finding, str(tmp_path)) is None


def test_vscode_link_uses_resolved_line(tmp_path):
    f = tmp_path / "file.py"
    f.write_text("def my_func():\n    return 42\n", encoding="utf-8")
    finding = {"file": "file.py", "location": "file.py:my_func", "line_range": ""}

    link = vscode_link(finding, str(tmp_path))

    assert link is not None
    assert link.endswith(":1")


# --- github_link (cycle 61) --------------------------------------------------

def test_github_link_with_line_range():
    link = github_link(
        {"file": "Services/ACMEMESService.cs", "line_range": "42-50"}, _GH, _SHA
    )
    assert link == f"{_GH}/blob/{_SHA}/Services/ACMEMESService.cs#L42"


def test_github_link_uses_resolved_line_fallback():
    # No line_range, but render() has pre-computed _resolved_line.
    link = github_link(
        {"file": "a/b.cs", "line_range": "", "_resolved_line": 17}, _GH, _SHA
    )
    assert link == f"{_GH}/blob/{_SHA}/a/b.cs#L17"


def test_github_link_no_line_omits_anchor():
    link = github_link({"file": "a/b.cs", "line_range": ""}, _GH, _SHA)
    assert link == f"{_GH}/blob/{_SHA}/a/b.cs"
    assert "#L" not in link


def test_github_link_pins_to_commit_sha():
    # The ref appears verbatim after /blob/ — that is the pinning contract.
    link = github_link({"file": "x.cs", "line_range": "1-1"}, _GH, _SHA)
    assert f"/blob/{_SHA}/" in link


def test_github_link_normalizes_path_and_trailing_slash():
    link = github_link(
        {"file": "\\a\\b.cs", "line_range": "3-3"}, _GH + "/", _SHA
    )
    # Backslashes -> forward slashes, leading slash stripped, no double slash.
    assert link == f"{_GH}/blob/{_SHA}/a/b.cs#L3"


def test_github_link_path_from_location():
    link = github_link(
        {"file": "", "location": "Activities/Start.cs:OnExecute", "line_range": ""},
        _GH, _SHA,
    )
    assert link == f"{_GH}/blob/{_SHA}/Activities/Start.cs"


def test_github_link_none_without_base_url():
    assert github_link({"file": "a.cs", "line_range": "1-1"}, "", _SHA) is None


def test_github_link_none_without_ref():
    assert github_link({"file": "a.cs", "line_range": "1-1"}, _GH, "") is None


def test_github_link_none_without_file():
    assert github_link({"file": "", "location": ""}, _GH, _SHA) is None
