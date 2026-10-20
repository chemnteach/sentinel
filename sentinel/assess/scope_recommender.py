"""sentinel/assess/scope_recommender.py

Rule-based scope recommendation for Screen 0.

Before ``sentinel assess`` runs, code owners want to see what will be assessed
and approve/exclude directories. ``recommend_scope`` scans a repository tree and
classifies its top-level and second-level directories (plus any diagnostic
evidence directory at any depth) into exclusion categories using deterministic
rules — no LLM calls.

This module intentionally does NOT import ``file_filter`` (that module owns the
hard exclusion set used during assessment); scope recommendation is an advisory,
human-in-the-loop layer with its own richer, confidence-scored categories.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ── Classification rules ──────────────────────────────────────────────

_DOCS_NAMES = frozenset({"docs", "doc", "documentation"})
_GENERATED_NAMES = frozenset(
    {"dist", "build", "__pycache__", ".venv", "node_modules", ".git"}
)
_VENDOR_NAMES = frozenset({"vendor", "third_party", "extern"})
_FIXTURE_NAMES = frozenset({"fixtures", "testdata", "test_data"})

_DIAGNOSTIC_DIR_EXACT = frozenset({"raw_evidence"})
_DIAGNOSTIC_DIR_GLOBS = ("substep_*",)
_DIAGNOSTIC_FILE_GLOBS = ("run_layer*.py", "*_probe.py", "*_sample_validation.py")

# Marker files that identify an assessment run-output directory. Self-describing:
# any repo-root dir holding one of these is engagement output, not source — works
# for any engagement, not just Contoso.
_ENGAGEMENT_MARKER_FILES = frozenset(
    {"findings.json", "audit_log.json", "remediation_patterns.json"}
)

# A directory with no .py files and more than this many files is a low-confidence
# "other" exclusion candidate (e.g. a big asset/data dump).
_OTHER_MIN_FILES = 500

# Assessable source-file extensions. Root-level files with one of these
# extensions are shippable source and belong in the recommended include set
# alongside the top-level source directories.
_SOURCE_FILE_EXTENSIONS = frozenset(
    {
        ".py", ".pyi",
        ".js", ".jsx", ".ts", ".tsx",
        ".go", ".rs", ".java", ".rb",
        ".cpp", ".hpp", ".cc", ".c", ".h", ".cs", ".php",
    }
)


@dataclass
class ExcludeRecommendation:
    """One recommended directory to exclude, with rationale and confidence."""

    path: str  # relative path from repo root (POSIX, trailing slash)
    reason: str  # human-readable reason
    file_count: int  # how many files this would exclude
    category: str  # generated | vendor | docs | test_fixtures | diagnostic | other
    confidence: str  # high | medium | low


@dataclass
class ScopeRecommendation:
    """The full scope recommendation for a repository."""

    repo_path: str
    total_files: int
    total_lines: int
    recommended_excludes: list[ExcludeRecommendation] = field(default_factory=list)
    recommended_includes: list[str] = field(default_factory=list)
    generated_at: str = ""


# ── Helpers ───────────────────────────────────────────────────────────


def _matches_any(name: str, globs: tuple[str, ...]) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, g.lower()) for g in globs)


def _is_diagnostic_dir_name(name: str) -> bool:
    return name.lower() in _DIAGNOSTIC_DIR_EXACT or _matches_any(
        name, _DIAGNOSTIC_DIR_GLOBS
    )


def _count_newlines(path: Path) -> int:
    """Count newlines in a file, binary-safe. Returns 0 on any read error."""
    try:
        with path.open("rb") as fh:
            return sum(chunk.count(b"\n") for chunk in iter(lambda: fh.read(65536), b""))
    except OSError:
        return 0


def _dir_stats(dir_abs: Path) -> tuple[int, bool]:
    """Return (recursive file count, has any .py file) for a directory subtree."""
    file_count = 0
    has_py = False
    for _dirpath, _dirnames, filenames in os.walk(dir_abs):
        file_count += len(filenames)
        if not has_py and any(fn.lower().endswith(".py") for fn in filenames):
            has_py = True
    return file_count, has_py


def _contains_test_module(dir_abs: Path) -> bool:
    """True if the subtree contains a ``test_*.py`` file."""
    for _dirpath, _dirnames, filenames in os.walk(dir_abs):
        if any(fnmatch.fnmatch(fn, "test_*.py") for fn in filenames):
            return True
    return False


def _is_diagnostic_by_content(dir_abs: Path) -> bool:
    """True if the subtree's Python files are all diagnostic runner/probe scripts.

    Requires at least one Python file and zero non-diagnostic Python files, so a
    normal source directory is never misclassified as diagnostic.
    """
    py_files = 0
    for _dirpath, _dirnames, filenames in os.walk(dir_abs):
        for fn in filenames:
            if not fn.lower().endswith(".py"):
                continue
            py_files += 1
            if not _matches_any(fn, _DIAGNOSTIC_FILE_GLOBS):
                return False
    return py_files > 0


def _is_engagement_output_dir(dir_abs: Path) -> bool:
    """True if a marker file (findings.json, etc.) sits directly in the dir."""
    return any((dir_abs / marker).is_file() for marker in _ENGAGEMENT_MARKER_FILES)


def _classify(name: str, dir_abs: Path) -> tuple[str, str, str] | None:
    """Classify a directory. Returns (category, confidence, reason) or None."""
    lowered = name.lower()

    if _is_engagement_output_dir(dir_abs):
        return (
            "engagement_output",
            "high",
            "Assessment run output directory — not source code.",
        )

    if lowered in _DOCS_NAMES:
        return ("docs", "high", "Documentation directory — not shippable source.")

    if _is_diagnostic_dir_name(name):
        return (
            "diagnostic",
            "high",
            "Diagnostic evidence directory (probe / sample-validation scripts).",
        )

    if lowered in _GENERATED_NAMES or lowered.endswith(".egg-info"):
        return (
            "generated",
            "high",
            "Generated build output, cache, or dependency directory.",
        )

    if lowered in _VENDOR_NAMES:
        return ("vendor", "medium", "Vendored third-party code.")

    if lowered in _FIXTURE_NAMES and not _contains_test_module(dir_abs):
        return (
            "test_fixtures",
            "medium",
            "Test fixture data with no test modules.",
        )

    if _is_diagnostic_by_content(dir_abs):
        return (
            "diagnostic",
            "high",
            "Directory of diagnostic runner/probe scripts.",
        )

    file_count, has_py = _dir_stats(dir_abs)
    if file_count > _OTHER_MIN_FILES and not has_py:
        return (
            "other",
            "low",
            f"Large non-code directory ({file_count} files, no Python).",
        )

    return None


def _candidate_dirs(root: Path) -> list[Path]:
    """Relative directory paths to classify: depth 1-2 plus diagnostic dirs anywhere."""
    candidates: dict[str, Path] = {}
    for dirpath, _dirnames, _filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        parts = rel.parts
        depth = len(parts)
        if depth == 0:
            continue
        if depth in (1, 2) or _is_diagnostic_dir_name(parts[-1]):
            candidates[rel.as_posix()] = rel
    return [candidates[key] for key in sorted(candidates)]


# ── Public API ────────────────────────────────────────────────────────


def recommend_scope(repo_path: str) -> ScopeRecommendation:
    """Scan *repo_path* and return a rule-based scope recommendation.

    Deterministic and offline. Directories are classified by name and content
    into exclusion categories with a confidence level; top-level directories that
    are not recommended for exclusion become the recommended include set.
    """
    root = Path(repo_path)
    generated_at = datetime.now().isoformat(timespec="seconds")

    total_files = 0
    total_lines = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            total_files += 1
            total_lines += _count_newlines(Path(dirpath) / fn)

    excludes: list[ExcludeRecommendation] = []
    excluded_top_level: set[str] = set()
    for rel in _candidate_dirs(root):
        classified = _classify(rel.name, root / rel)
        if classified is None:
            continue
        category, confidence, reason = classified
        file_count, _has_py = _dir_stats(root / rel)
        excludes.append(
            ExcludeRecommendation(
                path=rel.as_posix() + "/",
                reason=reason,
                file_count=file_count,
                category=category,
                confidence=confidence,
            )
        )
        if len(rel.parts) == 1:
            excluded_top_level.add(rel.name)

    excludes.sort(key=lambda e: e.path)

    includes: list[str] = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            name = child.name
            if name.startswith("."):
                continue
            if child.is_dir():
                if name in excluded_top_level:
                    continue
                includes.append(name + "/")
            elif child.is_file() and child.suffix.lower() in _SOURCE_FILE_EXTENSIONS:
                # Root-level source files (e.g. setup.py, a top-level script)
                # are assessable and must not be dropped just because they are
                # not inside a directory.
                includes.append(name)

    return ScopeRecommendation(
        repo_path=str(repo_path),
        total_files=total_files,
        total_lines=total_lines,
        recommended_excludes=excludes,
        recommended_includes=includes,
        generated_at=generated_at,
    )


__all__ = ["ExcludeRecommendation", "ScopeRecommendation", "recommend_scope"]
