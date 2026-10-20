from pathlib import Path

import pytest

from sentinel.workflow.remediation.apply import PathValidationError, write_test_file


def test_write_test_file_rejects_path_resolving_outside_repo_before_filesystem_mutation(tmp_path, monkeypatch):
    mkdir_calls = []
    write_calls = []

    original_mkdir = Path.mkdir
    original_write_text = Path.write_text

    def tracking_mkdir(self, *args, **kwargs):
        mkdir_calls.append(self)
        return original_mkdir(self, *args, **kwargs)

    def tracking_write_text(self, *args, **kwargs):
        write_calls.append(self)
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", tracking_mkdir)
    monkeypatch.setattr(Path, "write_text", tracking_write_text)

    with pytest.raises(PathValidationError):
        write_test_file("tests/../../outside.txt", "# content", str(tmp_path))

    assert mkdir_calls == []
    assert write_calls == []
    assert not (tmp_path / "outside.txt").exists()


def test_write_test_file_rejects_paths_that_escape_tests_directory_via_parent_segments(tmp_path, monkeypatch):
    mkdir_calls = []
    write_calls = []

    original_mkdir = Path.mkdir
    original_write_text = Path.write_text

    def tracking_mkdir(self, *args, **kwargs):
        mkdir_calls.append(self)
        return original_mkdir(self, *args, **kwargs)

    def tracking_write_text(self, *args, **kwargs):
        write_calls.append(self)
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", tracking_mkdir)
    monkeypatch.setattr(Path, "write_text", tracking_write_text)

    with pytest.raises(PathValidationError):
        write_test_file("tests/unit/../..//apply.py", "# content", str(tmp_path))

    assert mkdir_calls == []
    assert write_calls == []
    assert not (tmp_path / "apply.py").exists()


def test_write_test_file_rejects_absolute_paths(tmp_path, monkeypatch):
    mkdir_calls = []
    write_calls = []

    original_mkdir = Path.mkdir
    original_write_text = Path.write_text

    def tracking_mkdir(self, *args, **kwargs):
        mkdir_calls.append(self)
        return original_mkdir(self, *args, **kwargs)

    def tracking_write_text(self, *args, **kwargs):
        write_calls.append(self)
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", tracking_mkdir)
    monkeypatch.setattr(Path, "write_text", tracking_write_text)

    absolute_target = (tmp_path / "tests" / "unit" / "test_example.py").resolve()
    with pytest.raises(PathValidationError):
        write_test_file(str(absolute_target), "# content", str(tmp_path))

    assert mkdir_calls == []
    assert write_calls == []
    assert not absolute_target.exists()


def test_write_test_file_creates_valid_relative_path_within_tests_subtree(tmp_path):
    result = write_test_file(
        "tests/unit/test_example.py",
        "print('ok')\n",
        str(tmp_path),
    )

    assert result == "tests/unit/test_example.py"
    assert (tmp_path / "tests" / "unit" / "test_example.py").read_text(encoding="utf-8") == "print('ok')\n"


def test_write_test_file_validates_containment_using_resolved_repo_root_and_candidate_path(tmp_path, monkeypatch):
    mkdir_calls = []
    write_calls = []

    original_mkdir = Path.mkdir
    original_write_text = Path.write_text

    def tracking_mkdir(self, *args, **kwargs):
        mkdir_calls.append(self)
        return original_mkdir(self, *args, **kwargs)

    def tracking_write_text(self, *args, **kwargs):
        write_calls.append(self)
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", tracking_mkdir)
    monkeypatch.setattr(Path, "write_text", tracking_write_text)

    with pytest.raises(PathValidationError):
        write_test_file("tests/dir/../../escaped.py", "# content", str(tmp_path))

    assert mkdir_calls == []
    assert write_calls == []
    assert not (tmp_path / "escaped.py").exists()


def test_write_test_file_allows_non_traversing_relative_tests_path(tmp_path):
    result = write_test_file(
        "tests/integration/test_safe.py",
        "def test_safe():\n    assert True\n",
        str(tmp_path),
    )

    assert result == "tests/integration/test_safe.py"
    assert (tmp_path / "tests" / "integration" / "test_safe.py").exists()
