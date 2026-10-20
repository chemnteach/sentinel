from pathlib import PurePath

from sentinel.assess.file_filter import should_exclude_path


def test_should_exclude_direct_single_component_excluded_directory():
    """
    Acceptance criterion: A direct directory path such as `node_modules` or `build` is excluded when those names are configured as excluded directories.

    Scenario: A single-component path exactly matches a configured excluded directory name.
    Given: Excluded directories include 'node_modules' and the input path is 'node_modules'.
    When: should_exclude_path is called with that path.
    Then: The function returns True.
    """
    assert should_exclude_path("node_modules") is True
    assert should_exclude_path(PurePath("node_modules")) is True



def test_should_exclude_file_path_nested_under_excluded_directory():
    """
    Acceptance criterion: File paths nested under excluded directories are still excluded.

    Scenario: A file path contains an excluded directory before the final filename component.
    Given: Excluded directories include 'build' and the input path is 'src/build/output.txt'.
    When: should_exclude_path is called with that path.
    Then: The function returns True.
    """
    assert should_exclude_path("src/build/output.txt") is True



def test_should_exclude_multi_component_directory_with_excluded_interior_component():
    """
    Acceptance criterion: Tests cover both single-component and multi-component directory inputs for `should_exclude_path`.

    Scenario: A multi-component directory path contains an excluded directory as an interior component rather than only as the final component.
    Given: Excluded directories include 'node_modules' and the input path is 'packages/node_modules/cache'.
    When: should_exclude_path is called with that path.
    Then: The function returns True.
    """
    assert should_exclude_path("packages/node_modules/cache") is True



def test_should_not_exclude_single_component_non_excluded_path():
    """
    Acceptance criterion: The exclusion result no longer depends on an implicit assumption that the final path component is a filename.

    Scenario: A single-component path does not match any configured excluded directory name.
    Given: Excluded directories include 'node_modules' and 'build' and the input path is 'src'.
    When: should_exclude_path is called with that path.
    Then: The function returns False.
    """
    assert should_exclude_path("src") is False
