"""
Tests for sentinel.assess.file_filter — vendored/build directory exclusion.

TDD tests written BEFORE implementation (Step 0b).
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from sentinel.assess.file_filter import (
    EXCLUDE_DIRS,
    EXCLUDE_FILE_PATTERNS,
    should_exclude_path,
)


class TestExcludeDirs:
    """Verify the exclusion set contains expected directories."""

    @pytest.mark.parametrize(
        "dirname",
        [
            "node_modules",
            "build",
            "dist",
            ".next",
            "__pycache__",
            ".git",
            ".venv",
            "venv",
            "vendor",
            "third_party",
            "coverage",
            ".nyc_output",
            ".pytest_cache",
        ],
    )
    def test_vendored_and_build_dirs_in_exclusion_set(self, dirname: str) -> None:
        assert dirname in EXCLUDE_DIRS

    @pytest.mark.parametrize(
        "dirname",
        ["src", "lib", "app", "sentinel", "tests", "utils", "core", "api"],
    )
    def test_normal_dirs_not_in_exclusion_set(self, dirname: str) -> None:
        assert dirname not in EXCLUDE_DIRS


class TestShouldExcludePath:
    """Verify path-level exclusion logic."""

    def test_node_modules_excluded(self) -> None:
        path = Path("project/node_modules/express/index.js")
        assert should_exclude_path(path) is True

    def test_nested_node_modules_excluded(self) -> None:
        path = Path("project/packages/web/node_modules/react/index.js")
        assert should_exclude_path(path) is True

    def test_build_dir_excluded(self) -> None:
        path = Path("project/build/bundle.js")
        assert should_exclude_path(path) is True

    def test_dist_dir_excluded(self) -> None:
        path = Path("project/dist/main.js")
        assert should_exclude_path(path) is True

    def test_next_dir_excluded(self) -> None:
        path = Path("project/.next/static/chunks/app.js")
        assert should_exclude_path(path) is True

    def test_pycache_excluded(self) -> None:
        path = Path("project/sentinel/__pycache__/main.cpython-311.pyc")
        assert should_exclude_path(path) is True

    def test_git_dir_excluded(self) -> None:
        path = Path("project/.git/objects/abc123")
        assert should_exclude_path(path) is True

    def test_venv_excluded(self) -> None:
        path = Path("project/.venv/lib/python3.11/site-packages/click.py")
        assert should_exclude_path(path) is True

    def test_vendor_excluded(self) -> None:
        path = Path("project/vendor/github.com/pkg/errors/errors.go")
        assert should_exclude_path(path) is True

    def test_third_party_excluded(self) -> None:
        path = Path("project/third_party/protobuf/message.py")
        assert should_exclude_path(path) is True

    def test_coverage_excluded(self) -> None:
        path = Path("project/coverage/lcov.info")
        assert should_exclude_path(path) is True

    def test_nyc_output_excluded(self) -> None:
        path = Path("project/.nyc_output/data.json")
        assert should_exclude_path(path) is True

    def test_pytest_cache_excluded(self) -> None:
        path = Path("project/.pytest_cache/v/cache/nodeids")
        assert should_exclude_path(path) is True

    def test_generated_assessment_run_dir_excluded(self) -> None:
        path = Path("project/Tool_Line_run4/quality_scorecard.json")
        assert should_exclude_path(path) is True

    def test_remediation_packages_dir_excluded(self) -> None:
        path = Path("project/RemediationPackages/manifest.json")
        assert should_exclude_path(path) is True

    # --- Normal directories should NOT be excluded ---

    def test_src_dir_included(self) -> None:
        path = Path("project/src/main.py")
        assert should_exclude_path(path) is False

    def test_lib_dir_included(self) -> None:
        path = Path("project/lib/utils.py")
        assert should_exclude_path(path) is False

    def test_app_dir_included(self) -> None:
        path = Path("project/app/routes/index.ts")
        assert should_exclude_path(path) is False

    def test_tests_dir_included(self) -> None:
        path = Path("project/tests/test_main.py")
        assert should_exclude_path(path) is False

    def test_root_level_file_included(self) -> None:
        path = Path("project/setup.py")
        assert should_exclude_path(path) is False

    def test_deeply_nested_normal_path_included(self) -> None:
        path = Path("project/src/core/utils/helpers/format.py")
        assert should_exclude_path(path) is False

    # --- Edge cases ---

    def test_file_named_like_excluded_dir_not_excluded(self) -> None:
        """A file named 'node_modules.py' at root should NOT be excluded."""
        path = Path("project/node_modules.py")
        # The file itself is named node_modules.py but it's not a directory
        # in the path — only path *parts* matter
        assert should_exclude_path(path) is False

    def test_partial_match_not_excluded(self) -> None:
        """A dir like 'node_modules_backup' should NOT be excluded."""
        path = Path("project/node_modules_backup/index.js")
        assert should_exclude_path(path) is False

    def test_accepts_pure_posix_path(self) -> None:
        path = PurePosixPath("project/node_modules/express/index.js")
        assert should_exclude_path(path) is True

    def test_accepts_string_path(self) -> None:
        assert should_exclude_path("project/node_modules/express/index.js") is True

    def test_accepts_string_normal_path(self) -> None:
        assert should_exclude_path("project/src/main.py") is False


class TestNonShippableExclusion:
    """
    Verify docs/, thoughts/, raw_evidence/, substep_4*/ directories and
    diagnostic runner file patterns are excluded from assessment scope.
    """

    # --- Directory-level exclusions ---

    def test_docs_raw_evidence_probe_script_excluded(self) -> None:
        path = Path("docs/substep_4a/raw_evidence/run_layer2c_llm_probe.py")
        assert should_exclude_path(path) is True

    def test_docs_anything_excluded(self) -> None:
        path = Path("docs/architecture/overview.md")
        assert should_exclude_path(path) is True

    def test_thoughts_dir_excluded(self) -> None:
        path = Path("thoughts/scratch/notes.md")
        assert should_exclude_path(path) is True

    def test_raw_evidence_dir_excluded(self) -> None:
        path = Path("some/nested/raw_evidence/data.json")
        assert should_exclude_path(path) is True

    def test_substep_4_dir_excluded(self) -> None:
        path = Path("project/substep_4c/details.md")
        assert should_exclude_path(path) is True

    # --- Normal source/test paths NOT excluded ---

    def test_sentinel_source_not_excluded(self) -> None:
        path = Path("sentinel/assess/grouper.py")
        assert should_exclude_path(path) is False

    def test_test_file_not_excluded(self) -> None:
        path = Path("tests/assess/test_grouper.py")
        assert should_exclude_path(path) is False

    # --- File-pattern exclusions (regardless of directory) ---

    def test_llm_probe_file_in_sentinel_excluded(self) -> None:
        path = Path("sentinel/run_layer2c_llm_probe.py")
        assert should_exclude_path(path) is True

    def test_sample_validation_file_excluded(self) -> None:
        path = Path("sentinel/assess/run_layer3a_sample_validation.py")
        assert should_exclude_path(path) is True

    def test_run_layer_file_excluded(self) -> None:
        path = Path("sentinel/run_layer2c.py")
        assert should_exclude_path(path) is True

    def test_exclude_file_patterns_constant_present(self) -> None:
        assert "*_sample_validation.py" in EXCLUDE_FILE_PATTERNS
        assert "*_llm_probe.py" in EXCLUDE_FILE_PATTERNS
        assert "run_layer*.py" in EXCLUDE_FILE_PATTERNS
