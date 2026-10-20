"""
sentinel/assess/file_filter.py

Shared file-path exclusion logic for vendored, build, and generated directories.

Used by the Cartographer (Stage 1), the Orchestrator (Stage 3 file
enumeration), and any future code that walks a target repository's tree.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import PurePath
from typing import Union

# Directories that should never be reviewed. These are vendored dependencies,
# build outputs, caches, and version-control internals.
EXCLUDE_DIRS: frozenset[str] = frozenset({
    # Version control
    ".git",
    # Python
    "__pycache__", ".venv", "venv", ".tox", ".nox", ".eggs",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".pip-tmp",
    # JavaScript / Node
    "node_modules", ".next", ".nuxt",
    # Build outputs
    "build", "dist",
    # C# / .NET build output (MSBuild bin/obj, NuGet packages, VS cache,
    # test output) — added before the Line_tool run. ".vs" is skipped as a
    # dotdir by the cartographer's tree walk, but should_exclude_path() has
    # no dotdir logic, so it must be listed here to be excluded everywhere.
    "bin", "obj", "packages", "TestResults", ".vs",
    # Vendored / third-party
    "vendor", "third_party",
    # Coverage / test output
    "coverage", ".nyc_output",
    # Non-shippable documentation / scratch / diagnostic evidence. These hold
    # narrative docs and throwaway probe scripts, not reviewable source, and
    # otherwise swamp the assessment (run-015: 92.5% of findings came from
    # docs/**/raw_evidence sample-validation scripts).
    "docs", "thoughts", "raw_evidence",
})

# Filename glob patterns for non-shippable diagnostic runner scripts. Matched
# against a path's final component regardless of directory, so a stray probe
# script anywhere in the tree is excluded.
EXCLUDE_FILE_PATTERNS: frozenset[str] = frozenset({
    "*_sample_validation.py",
    "*_llm_probe.py",
    "run_layer*.py",
})

_NORMALIZED_EXCLUDE_DIRS: frozenset[str] = frozenset(
    dirname.lower() for dirname in EXCLUDE_DIRS
)
_SPECIAL_EXCLUDE_DIRS: frozenset[str] = frozenset({
    "remediationpackages",
})
_GENERATED_RUN_DIR_RE = re.compile(r".+_run\d+$", re.IGNORECASE)
_PYTEST_TMP_RE = re.compile(r"pytest-of-.*", re.IGNORECASE)
# Assessment evidence subdirectories: substep_4a, substep_4c, substep_4d, ...
_SUBSTEP_DIR_RE = re.compile(r"substep_4.*", re.IGNORECASE)


def _is_excluded_dir_name(dirname: str) -> bool:
    lowered = dirname.lower()
    return (
        lowered in _NORMALIZED_EXCLUDE_DIRS
        or lowered in _SPECIAL_EXCLUDE_DIRS
        or _GENERATED_RUN_DIR_RE.fullmatch(dirname) is not None
        or _PYTEST_TMP_RE.fullmatch(dirname) is not None
        or _SUBSTEP_DIR_RE.fullmatch(dirname) is not None
    )


def _is_excluded_file_name(filename: str) -> bool:
    lowered = filename.lower()
    return any(
        fnmatch.fnmatch(lowered, pattern.lower())
        for pattern in EXCLUDE_FILE_PATTERNS
    )


def should_exclude_path(path: Union[str, PurePath]) -> bool:
    """
    Return True if *path* passes through any excluded directory.

    Checks each component of the path against ``EXCLUDE_DIRS`` plus generated
    assessment-output directory patterns. Only exact matches count for the
    static exclusion set — ``node_modules_backup`` will NOT be excluded. The
    final path component is additionally matched against
    ``EXCLUDE_FILE_PATTERNS``, so a diagnostic runner script is excluded
    regardless of the directory it lives in.

    Works with ``Path``, ``PurePosixPath``, or plain strings.
    """
    if isinstance(path, str):
        path = PurePath(path)

    if any(_is_excluded_dir_name(part) for part in path.parts):
        return True

    return bool(path.parts) and _is_excluded_file_name(path.parts[-1])


# ---------------------------------------------------------------------------
# Operational-code classification (language-agnostic scope)
# ---------------------------------------------------------------------------
# "Operational code" is production/runtime source in a recognized programming
# language — the code that does the work — as distinct from config/data,
# markup/docs, build/project files, helper scripts, generated output, and
# tests. Driven by an extension registry plus name/dir heuristics so the notion
# is generic ("all operational code") rather than tied to one language
# ("all .cs").

OPERATIONAL_CODE_EXTENSIONS: frozenset[str] = frozenset({
    # Python
    ".py", ".pyi", ".pyx",
    # .NET
    ".cs", ".vb", ".fs", ".fsx",
    # JVM
    ".java", ".kt", ".kts", ".scala", ".groovy",
    # JS / TS
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    # Go / Rust
    ".go", ".rs",
    # C / C++ / Objective-C
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".m", ".mm",
    # Other mainstream languages
    ".swift", ".rb", ".php", ".pl", ".pm", ".dart", ".lua",
    ".ex", ".exs", ".erl", ".clj", ".cljs", ".scala",
})

# Directory components that mark a test tree.
_TEST_DIR_NAMES: frozenset[str] = frozenset({
    "test", "tests", "__tests__", "spec", "specs", "testing",
})

# Filename glob patterns that mark a test file, matched case-insensitively
# against the final path component.
_TEST_FILE_PATTERNS: tuple[str, ...] = (
    "test_*.py", "*_test.py",
    "*_test.go",
    "*test.cs", "*tests.cs",          # WidgetTest.cs / WidgetTests.cs / *TddTests.cs
    "*.test.*", "*.spec.*",            # widget.test.js / widget.spec.ts
    "*spec.rb", "*_spec.rb",
    "test*.java", "*test.java", "*tests.java",
)


def is_test_path(path: Union[str, PurePath]) -> bool:
    """Return True when *path* is a test file or lives under a test tree."""
    if isinstance(path, str):
        path = PurePath(path)
    if any(part.lower() in _TEST_DIR_NAMES for part in path.parts):
        return True
    if not path.parts:
        return False
    name = path.parts[-1].lower()
    return any(fnmatch.fnmatch(name, pat) for pat in _TEST_FILE_PATTERNS)


def is_operational_code(path: Union[str, PurePath]) -> bool:
    """Return True when *path* is production/runtime source code.

    False for excluded dirs (build/vendor/docs), non-code extensions
    (config/data/markup/project files), helper scripts, and tests.
    """
    if isinstance(path, str):
        path = PurePath(path)
    if should_exclude_path(path):
        return False
    if path.suffix.lower() not in OPERATIONAL_CODE_EXTENSIONS:
        return False
    if is_test_path(path):
        return False
    return True

