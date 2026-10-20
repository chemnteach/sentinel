"""Apply structured edit operations from the Coder to disk (cycle 56)."""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)


class EditApplicationError(Exception):
    """Raised when an edit cannot be applied because ``old_text`` is not found
    in the target file — i.e. the Coder quoted code that does not exist."""


class PathValidationError(EditApplicationError):
    """Raised when a requested path resolves outside the repository root."""


def _normalize_ws(text: str) -> str:
    """Collapse whitespace for fuzzy matching."""
    return " ".join(text.split())


def _validate_repo_contained_path(root: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` under ``root`` and reject paths outside ``root``."""
    normalized = Path(rel_path)
    if normalized.is_absolute():
        raise PathValidationError(f"Path escapes repository root: {rel_path}")

    resolved_root = root.resolve()
    resolved_target = (resolved_root / normalized).resolve(strict=False)
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise PathValidationError(f"Path escapes repository root: {rel_path}") from exc
    return resolved_target


def _resolve_target(root: Path, rel_path: str) -> tuple[Path, str]:
    """Resolve a Coder-supplied file path to an on-disk target under ``root``.

    Findings from a subtree assessment carry subtree-relative paths (e.g.
    ``govern/conditions.py`` when the scanned subtree is ``sentinel/``). Try the
    path as given first, then under the ``sentinel/`` subtree — mirroring
    ``source_retrieval._normalize_path``. Returns the absolute path plus the
    repo-root-relative path that actually exists, so git rollback/commit operate
    on the real file. Falls back to the as-given path when neither exists, so the
    caller raises a clear File-not-found error.
    """
    direct = _validate_repo_contained_path(root, rel_path)
    if direct.exists():
        return direct, rel_path
    subtree_rel = str(PurePosixPath("sentinel") / rel_path.replace("\\", "/"))
    subtree = _validate_repo_contained_path(root, subtree_rel)
    if subtree.exists():
        return subtree, subtree_rel
    return direct, rel_path


def apply_edits(edits: list[dict], repo_root: str) -> list[str]:
    """Apply a list of structured edit operations to files on disk.

    Each edit is ``{file, old_text, new_text}``. ``old_text`` is replaced with
    ``new_text`` (first occurrence). Returns the list of files actually
    modified.

    Partial application on failure is by design: if an edit raises partway
    through the list, earlier edits remain on disk. Callers must call
    ``rollback_edits(modified_files, repo_root)`` on exception to restore the
    working tree.

    Raises:
        EditApplicationError: when a target file does not exist, or ``old_text``
            is not found in it (the Coder hallucinated code).
    """
    modified: list[str] = []
    root = Path(repo_root)

    for edit in edits:
        rel_path = edit.get("file", "")
        old_text = edit.get("old_text", "")
        new_text = edit.get("new_text", "")

        if not rel_path or not old_text:
            log.warning("Skipping malformed edit: missing file or old_text")
            continue

        path, eff_path = _resolve_target(root, rel_path)
        if not path.exists():
            raise EditApplicationError(f"File not found: {rel_path}")

        source = path.read_text(encoding="utf-8", errors="replace")
        if old_text not in source:
            # Fallback: try a whitespace-normalized match before failing, so a
            # Coder edit that drifts only in indentation/trailing whitespace
            # still applies. The exact path above is unchanged (no cost when it
            # matches); this only runs on miss.
            norm_old = _normalize_ws(old_text)
            norm_source = _normalize_ws(source)
            if norm_old and norm_old in norm_source:
                # Find the original-source line span whose normalized form
                # equals the normalized old_text, then splice in new_text.
                old_lines = old_text.splitlines()
                src_lines = source.splitlines()
                match_start = None
                for i in range(len(src_lines) - len(old_lines) + 1):
                    candidate = src_lines[i:i + len(old_lines)]
                    if _normalize_ws("\n".join(candidate)) == norm_old:
                        match_start = i
                        break
                if match_start is not None:
                    new_lines = (
                        src_lines[:match_start]
                        + new_text.splitlines()
                        + src_lines[match_start + len(old_lines):]
                    )
                    updated = "\n".join(new_lines)
                    if source.endswith("\n"):
                        updated += "\n"
                    path.write_text(updated, encoding="utf-8")
                    log.info(
                        "Applied edit to %s (whitespace-normalized match)",
                        eff_path,
                    )
                    if eff_path not in modified:
                        modified.append(eff_path)
                    continue
            raise EditApplicationError(
                f"old_text not found in {rel_path}. The Coder may have quoted "
                f"code that does not exist.\nold_text: {old_text[:200]}"
            )

        updated = source.replace(old_text, new_text, 1)
        path.write_text(updated, encoding="utf-8")
        log.info("Applied edit to %s", eff_path)
        if eff_path not in modified:
            modified.append(eff_path)

    return modified


def write_test_file(test_file: str, test_content: str, repo_root: str) -> str | None:
    """Write the Coder's test file to disk. Returns the path written, or None
    when ``test_content`` is empty."""
    if not test_file or not test_content:
        return None

    normalized = PurePosixPath(test_file.replace("\\", "/"))
    if normalized.is_absolute():
        raise PathValidationError(f"Path escapes repository root: {test_file}")

    original = test_file
    parts = normalized.parts
    if parts and parts[0] == "sentinel":
        normalized = PurePosixPath(*parts[1:])
        parts = normalized.parts

    if not parts or parts[0] != "tests":
        normalized = PurePosixPath("tests") / normalized

    test_file = str(normalized)
    if test_file != original:
        log.warning(
            "test_file coerced from %r to %r",
            original,
            test_file,
        )

    resolved_root = Path(repo_root).resolve()
    resolved_tests_root = (resolved_root / "tests").resolve(strict=False)
    path = _validate_repo_contained_path(resolved_root, test_file)
    try:
        path.relative_to(resolved_tests_root)
    except ValueError as exc:
        raise PathValidationError(f"Path escapes tests directory: {test_file}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(test_content, encoding="utf-8")
    log.info("Wrote test file: %s", test_file)
    return test_file


def rollback_edits(modified_files: list[str], repo_root: str) -> list[str]:
    """Restore applied edits with ``git checkout -- <file>``.

    Called when the test runner fails after edits were applied, so the working
    tree returns to the last committed state. Best-effort per file.

    Returns the list of paths that could NOT be rolled back (empty list on full
    success). A file counts as failed when git exits non-zero (e.g. the path is
    not tracked, or checkout errored) or when the subprocess itself raises
    (git missing, timeout). Callers should log/surface a non-empty result so a
    partially-restored working tree is not mistaken for a clean rollback."""
    import subprocess

    failed: list[str] = []
    for rel_path in modified_files:
        try:
            result = subprocess.run(
                ["git", "-C", repo_root, "checkout", "--", rel_path],
                capture_output=True,
                timeout=30,
            )
        except Exception as exc:
            log.warning("Rollback failed for %s: %s", rel_path, exc)
            failed.append(rel_path)
            continue
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            log.warning(
                "Rollback failed for %s (git exit %d): %s",
                rel_path,
                result.returncode,
                stderr,
            )
            failed.append(rel_path)
            continue
        log.info("Rolled back: %s", rel_path)
    return failed
