from pathlib import Path, PurePath

from sentinel.assess.cartographer import Cartographer


def test_module_key_for_groups_top_level_package_init_under_package_key(tmp_path: Path):
    """
    Acceptance criterion: For a file directly under a top-level package, `_module_key_for` returns the top-level package key rather than `.`.

    Scenario: A package initializer file exists at `pkg/__init__.py`.
    Given: A repository-relative path for `pkg/__init__.py` in a top-level package context.
    When: `_module_key_for` is called for that file.
    Then: It returns `pkg` rather than `.`.
    """
    repo = tmp_path
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")

    module_key = Cartographer._module_key_for(None, repo, PurePath("pkg/__init__.py"))

    assert module_key == "pkg"


def test_module_key_for_groups_top_level_package_module_under_package_key(tmp_path: Path):
    """
    Acceptance criterion: A regression test covers `pkg/__init__.py` and `pkg/module.py` and asserts both are grouped under the `pkg` module key.

    Scenario: A regular module file exists at `pkg/module.py` alongside `pkg/__init__.py`.
    Given: Repository-relative paths for `pkg/__init__.py` and `pkg/module.py`.
    When: `_module_key_for` is called for both files.
    Then: Both return the same module key `pkg`.
    """
    repo = tmp_path
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "module.py").write_text("value = 1\n", encoding="utf-8")

    init_key = Cartographer._module_key_for(None, repo, PurePath("pkg/__init__.py"))
    module_key = Cartographer._module_key_for(None, repo, PurePath("pkg/module.py"))

    assert init_key == "pkg"
    assert module_key == "pkg"


def test_module_key_for_preserves_root_key_for_true_repository_root_file(tmp_path: Path):
    """
    Acceptance criterion: For a true repository-root file that is not inside a package context, `_module_key_for` still returns the root module key behavior expected by current callers.

    Scenario: A Python file exists directly at repository root and is not inside any package directory.
    Given: A repository-relative path for a root file such as `tool.py` with no package parent.
    When: `_module_key_for` is called for that file.
    Then: It returns the existing root module key behavior rather than a package key.
    """
    repo = tmp_path
    (repo / "tool.py").write_text("print('hi')\n", encoding="utf-8")

    module_key = Cartographer._module_key_for(None, repo, PurePath("tool.py"))

    assert module_key == "."


def test_module_key_for_does_not_reassign_repository_root_file_to_package_key(tmp_path: Path):
    """
    Acceptance criterion: A regression test covers a repository-root file and asserts it is not incorrectly reassigned to a package module.

    Scenario: A repository-root file is evaluated while package files also exist elsewhere in the repository.
    Given: A repository-root file path and a separate top-level package path.
    When: `_module_key_for` is called for the repository-root file.
    Then: The root file is not assigned the package module key such as `pkg`.
    """
    repo = tmp_path
    (repo / "tool.py").write_text("print('hi')\n", encoding="utf-8")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")

    module_key = Cartographer._module_key_for(None, repo, PurePath("tool.py"))

    assert module_key == "."
    assert module_key != "pkg"
