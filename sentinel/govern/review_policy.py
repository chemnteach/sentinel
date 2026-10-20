"""Review policy helpers for local-first change review workflows."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from sentinel.govern.conditions import review_result_supports_approval


REVIEW_REQUIRED_PATTERNS: tuple[str, ...] = (
    "sentinel/**/*.py",
    "tests/**/*.py",
)

AUTO_ACCEPT_PATTERNS: tuple[str, ...] = (
    "sentinel/state/*.json",
    "README.md",
    "docs/*.md",
    "docs/**/*.md",
    ".env.example",
)


@dataclass
class ReviewBuckets:
    review_required: list[str]
    auto_accept: list[str]


class ReviewPolicyError(RuntimeError):
    """Raised when review policy processing cannot complete."""


def parse_status_porcelain(stdout: str) -> list[str]:
    """Parse `git status --porcelain` output into repository-relative paths."""
    paths: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue

        # Format: XY <path> or XY <old> -> <new> (rename)
        payload = line[3:] if len(line) >= 4 else ""
        if " -> " in payload:
            payload = payload.split(" -> ", 1)[1]
        path = payload.replace("\\", "/").strip()
        if path:
            paths.append(path)
    return paths


def _matches(path: str, patterns: Iterable[str]) -> bool:
    p = Path(path)
    return any(p.match(pattern) for pattern in patterns)


def classify_paths(paths: Iterable[str]) -> ReviewBuckets:
    """
    Classify files by review policy.

    Safety-first rule: anything not explicitly auto-accepted is review-required.
    """
    review_required: list[str] = []
    auto_accept: list[str] = []

    for raw in paths:
        path = raw.replace("\\", "/")
        if _matches(path, AUTO_ACCEPT_PATTERNS):
            auto_accept.append(path)
        else:
            review_required.append(path)

    return ReviewBuckets(
        review_required=sorted(set(review_required)),
        auto_accept=sorted(set(auto_accept)),
    )


def classify_changed_files(repo_path: Path) -> ReviewBuckets:
    """Classify current git working tree changes according to review policy."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
    except subprocess.CalledProcessError as exc:
        raise ReviewPolicyError(exc.stderr or str(exc)) from exc

    paths = parse_status_porcelain(result.stdout)
    return classify_paths(paths)



def merge_or_release_gate_satisfied(review_result: dict[str, Any] | None) -> bool:
    """Return whether a review result can satisfy approval-consuming gates."""
    return review_result_supports_approval(review_result)
