"""
sentinel/orchestrator/worktree_manager.py

Worktree manager — Phase 1 of the agentic-executor architecture.

An agentic executor (Codex / Claude Code subagent) fixes one task inside an
isolated git worktree so a failed or out-of-scope fix can never contaminate
main. This module owns the worktree lifecycle:

  - ``create_worktree``            — add an isolated worktree at HEAD
  - ``get_worktree_changed_files`` — inspect the worktree diff
  - ``enforce_scope``              — HARD pre-merge scope gate (block, not detect)
  - ``merge_worktree_to_main``     — copy + commit a gate-approved diff
  - ``discard_worktree``           — remove the worktree and reset the repo root

All git operations run through ``subprocess.run(check=True,
capture_output=True)``. No dependencies beyond the standard library and pathlib.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_WORKTREE_SUBDIR = ".claude/worktrees"


class WorktreeError(Exception):
    pass


class ScopeViolation(WorktreeError):
    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__(f"Scope violation: {violations}")


def _git(args: list[str], cwd: str | Path | None = None) -> str:
    """Run a git command and return stdout (utf-8). Raise WorktreeError on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise WorktreeError(f"git executable not found: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise WorktreeError(
            f"git {' '.join(args)} failed (exit {exc.returncode}): {stderr}"
        ) from exc
    return (result.stdout or b"").decode("utf-8", errors="replace")


def _norm(path: str) -> str:
    """Forward-slash a path string."""
    return path.replace("\\", "/")


def _resolve_within(base: Path, rel: str) -> Path:
    """Resolve ``base / rel`` and verify it stays inside ``base``.

    Rejects absolute paths and ``..`` traversal that would escape the tree —
    repo-controlled porcelain paths (or the ``tests/`` scope allowance) must not
    be able to read, overwrite, or delete files outside the worktree/repo root
    (CWE-22). Returns the resolved path or raises ``WorktreeError``.
    """
    base_resolved = base.resolve()
    candidate = (base / rel).resolve()
    if candidate != base_resolved and not candidate.is_relative_to(base_resolved):
        raise WorktreeError(
            f"path {rel!r} escapes {base_resolved} after normalization; "
            "refusing to touch it"
        )
    return candidate


def create_worktree(task_id: str, repo_root: str | Path) -> Path:
    """Create an isolated worktree at ``.claude/worktrees/task-<task_id>`` on HEAD.

    Returns the worktree path. Raises ``WorktreeError`` if it already exists or
    the git command fails.
    """
    repo_root = Path(repo_root)
    worktree = repo_root / _WORKTREE_SUBDIR / f"task-{task_id}"
    if worktree.exists():
        raise WorktreeError(f"Worktree already exists: {_norm(str(worktree))}")
    log.info("Creating worktree for task %s at %s", task_id, _norm(str(worktree)))
    _git(["worktree", "add", str(worktree), "HEAD"], cwd=repo_root)
    return worktree


def get_worktree_changed_files(worktree_path: str | Path) -> list[str]:
    """Return repo-root-relative paths changed in the worktree.

    Modified tracked files (``git diff --name-only HEAD``) plus new untracked
    files under ``sentinel/`` and ``tests/`` (``git ls-files --others
    --exclude-standard``). Forward-slashed, de-duplicated, original order.
    """
    worktree = Path(worktree_path)
    tracked = _git(["-C", str(worktree), "diff", "--name-only", "HEAD"])
    untracked = _git(["-C", str(worktree), "ls-files", "--others", "--exclude-standard"])

    files: list[str] = []
    for line in tracked.splitlines():
        f = _norm(line.strip())
        if f:
            files.append(f)
    for line in untracked.splitlines():
        f = _norm(line.strip())
        if f and (f.startswith("sentinel/") or f.startswith("tests/")):
            files.append(f)

    seen: set[str] = set()
    out: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _unquote_porcelain_path(token: str) -> str:
    """Strip surrounding quotes git adds to porcelain paths with special chars."""
    token = token.strip()
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        token = token[1:-1]
    return token


def _parse_rename_tokens(rest: str) -> tuple[str, str]:
    """Split a porcelain rename payload into (orig, new) raw tokens.

    Handles unquoted ('old -> new') and C-quoted ('"old path" -> "new path"')
    forms, including filenames that contain a literal ' -> ' sequence — which
    git wraps in double-quotes, so the naive split(' -> ', 1) would cleave
    inside the first token (FIX-CRITICAL run-020).
    """
    if not rest.startswith('"'):
        # Fast path: no quoting — split on the first ' -> '
        parts = rest.split(" -> ", 1)
        return (parts[0], parts[1]) if len(parts) == 2 else (rest, "")
    # Scan for the end of the first C-quoted token, honouring \" escapes.
    i = 1
    while i < len(rest):
        if rest[i] == "\\" and i + 1 < len(rest):
            i += 2
            continue
        if rest[i] == '"':
            break
        i += 1
    sep = i + 1
    if rest[sep : sep + 4] == " -> ":
        return rest[:sep], rest[sep + 4:]
    # Malformed: fall back to the naive split
    parts = rest.split(" -> ", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (rest, "")


def _rollback_partial_merge(
    repo_root: Path, applied: list[str], delete_set: set[str]
) -> None:
    """Best-effort undo of partially-applied merge_worktree_to_main changes.

    For each path already applied: deleted paths are restored from git HEAD;
    added/modified paths are reset with ``git checkout`` (works for modified
    tracked files) or removed if they were newly untracked additions.
    Failures are logged but do not re-raise so the original exception propagates.
    """
    for path in applied:
        try:
            if path in delete_set:
                _git(["-C", str(repo_root), "checkout", "--", path])
            else:
                try:
                    _git(["-C", str(repo_root), "checkout", "--", path])
                except WorktreeError:
                    # Newly added (untracked) — just remove it.
                    dst = repo_root / path
                    try:
                        dst.unlink(missing_ok=True)
                    except Exception:
                        pass
        except Exception as exc:
            log.warning("merge rollback of %s failed: %s", path, exc)


def _enumerate_worktree_changes(worktree_path: str | Path) -> list[tuple[str, bool]]:
    """Single source of truth for worktree change enumeration, used by BOTH the
    scope gate and the merge routine so the validated set matches the modified
    set exactly (FIX-BLOCKING-019).

    Parses ``git status --porcelain`` status-aware rather than blindly slicing
    ``line[3:]`` (FIX-BLOCKING-022). Returns ``(repo-relative dest path,
    is_delete)`` records:

      * modified / added / untracked -> ``(path, False)``
      * deleted                      -> ``(path, True)``
      * renamed ``old -> new``       -> ``(old, True)`` and ``(new, False)``
    """
    porcelain = _git(["-C", str(worktree_path), "status", "--porcelain"])
    out: list[tuple[str, bool]] = []
    seen: set[tuple[str, bool]] = set()
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        xy = line[:2]
        rest = line[3:]
        if " -> " in rest:
            orig, new = _parse_rename_tokens(rest)
            records = [
                (_norm(_unquote_porcelain_path(orig)), True),
                (_norm(_unquote_porcelain_path(new)), False),
            ]
        else:
            path = _norm(_unquote_porcelain_path(rest))
            records = [(path, "D" in xy)]
        for rec in records:
            if rec[0] and rec not in seen:
                seen.add(rec)
                out.append(rec)
    return out


def enforce_scope(
    worktree_path: str | Path | None,
    affected_files: list[str],
    changed_files: list[str] | None = None,
) -> None:
    """Hard scope gate: raise ``ScopeViolation`` if any changed *source* file is
    not in ``affected_files``. Test files (``tests/...``) are always allowed.

    The changed-file set comes from the worktree (``worktree_path``) via the
    same status-aware enumeration the merge routine uses — so a tracked
    deletion or rename destination outside scope is caught here — or, as a
    fallback when no worktree is active yet, from an explicit ``changed_files``
    list. Raises ``WorktreeError`` if neither is supplied. Called by
    ``merge_worktree_to_main`` before any file is copied — a hard block, not a
    detect-and-send-back.
    """
    if worktree_path is not None:
        # Validate exactly the set the merge path can modify or remove
        # (FIX-BLOCKING-019): dest paths for adds/mods/renames AND deleted paths.
        changed = [path for path, _is_delete in _enumerate_worktree_changes(worktree_path)]
    elif changed_files is not None:
        changed = [_norm(f) for f in changed_files]
    else:
        raise WorktreeError(
            "enforce_scope requires either worktree_path or changed_files"
        )
    allowed = {_norm(a) for a in (affected_files or [])}
    violations = [
        f for f in changed
        if not f.startswith("tests/") and f not in allowed
    ]
    if violations:
        log.warning("Scope violation: %s", violations)
        raise ScopeViolation(violations)
    log.info("Scope OK (%d changed file(s) in scope)", len(changed))


def merge_worktree_to_main(
    worktree_path: str | Path,
    task_id: str,
    commit_message: str,
    repo_root: str | Path,
    affected_files: list[str],
) -> str:
    """Enforce scope, then apply the worktree's changed + untracked files to the
    repo root (status-aware: copy modified/added/renamed, remove deleted),
    stage them, and commit. Returns the commit SHA.

    Raises ``ScopeViolation`` (before any change is applied) on out-of-scope
    edits, and ``WorktreeError`` if there is nothing to merge or the commit
    fails.
    """
    worktree = Path(worktree_path)
    repo_root = Path(repo_root)

    # Hard scope gate FIRST — before any file touches the repo root — over the
    # same enumeration used to apply changes below.
    enforce_scope(worktree, affected_files)

    changes = _enumerate_worktree_changes(worktree)
    if not changes:
        raise WorktreeError(f"No changes to merge for task {task_id}")

    log.info(
        "Merging %d file change(s) from worktree to repo root for task %s",
        len(changes), task_id,
    )
    applied: list[str] = []
    delete_set: set[str] = {p for p, d in changes if d}
    try:
        for path, is_delete in changes:
            # Contain every sink path within the repo/worktree root before touching
            # the filesystem, so a traversal path cannot escape (FIX-CRITICAL run-019).
            dst = _resolve_within(repo_root, path)
            if is_delete:
                # Deleted in the worktree — remove from the target tree instead of
                # copying a non-existent source (FIX-BLOCKING-022). Handle a path
                # that resolves to a directory (or symlink) rather than assuming a
                # plain file, since unlink() raises on a directory (FIX-CRITICAL
                # run-019).
                if dst.is_symlink() or dst.is_file():
                    dst.unlink()
                elif dst.is_dir():
                    shutil.rmtree(dst)
                # else: already gone — nothing to do
            else:
                src = _resolve_within(worktree, path)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
            applied.append(path)
    except Exception as exc:
        # Mid-apply failure: undo every already-applied path so the repo root
        # is not left in a half-merged state (FIX-CRITICAL run-020).
        _rollback_partial_merge(repo_root, applied, delete_set)
        raise WorktreeError(
            f"merge_worktree_to_main failed mid-apply for task {task_id}: {exc}"
        ) from exc

    # ``git add -A`` stages both content changes and deletions for the paths.
    _git(["-C", str(repo_root), "add", "-A", "--", *applied])
    _git(["-C", str(repo_root), "commit", "-m", commit_message])
    sha = _git(["-C", str(repo_root), "rev-parse", "HEAD"]).strip()
    log.info("Merged task %s as commit %s", task_id, sha[:8])
    return sha


def discard_worktree(worktree_path: str | Path, repo_root: str | Path) -> None:
    """Remove the worktree (idempotent) and reset any dirty tracked files left
    in the repo root under ``sentinel/`` and ``tests/``."""
    worktree = Path(worktree_path)
    repo_root = Path(repo_root)
    log.info("Discarding worktree %s", _norm(str(worktree)))
    try:
        _git(["-C", str(repo_root), "worktree", "remove", "--force", str(worktree)])
    except WorktreeError as exc:
        # Idempotent: already removed / not a registered worktree.
        log.info("worktree remove (ignored, idempotent): %s", exc)
    try:
        _git(["-C", str(repo_root), "checkout", "--", "sentinel/", "tests/"])
    except WorktreeError as exc:
        # No such pathspec / nothing to reset — fine.
        log.info("repo-root reset (ignored): %s", exc)
