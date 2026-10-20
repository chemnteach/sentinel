"""
sentinel/orchestrator/retry_context_builder.py

Phase 2 — retry context builder (Plan Section 5, Option A).

When a governance gate (Architect diff review, Phase-3 backwards-compat, or QA
verification) rejects a task's diff, the Coder is sent back to re-implement. On
its own that loses the *reason* for rejection — the Coder re-enters blind and
tends to repeat the failed approach. ``build_retry_context`` closes that gap: it
renders a structured markdown packet describing which gate failed, what the
feedback said, and an explicit instruction not to repeat the same approach, and
persists it to ``retry/{task_id}/attempt-{N}/context.md`` for the run record.

The returned string is threaded onto the task (``task.retry_context``) and
injected into the next implementation prompt by
``TaskManager.build_implementation_context``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_CONTEXT_FILENAME = "context.md"
_BATCH_SUMMARY_FILENAME = "batch_summary.json"

# Windows lock-race guard for the atomic replace, mirroring the bounded-retry
# convention used for the audit/FER file locks in state_store.py.
_REPLACE_RETRY_TIMEOUT_SECONDS = 5.0
_REPLACE_RETRY_POLL_SECONDS = 0.05

# Interprocess lock serializing the full batch_summary.json read-modify-write so
# concurrent task-status updates cannot lose each other's writes
# (FIX-BLOCKING-012). The atomic replace alone only makes the final write
# atomic — it does not prevent two writers reading the same base and clobbering.
_BATCH_SUMMARY_LOCK_TIMEOUT_SECONDS = 10.0
_BATCH_SUMMARY_LOCK_POLL_SECONDS = 0.01
# A lock file older than this is treated as abandoned (writer crashed) and is
# forcibly reclaimed once, so a stale lock cannot wedge progress indefinitely.
_BATCH_SUMMARY_LOCK_STALE_SECONDS = 60.0


def _format_test_failures(test_failures: list[Any] | None) -> str:
    """Render the test-failure list as markdown bullets, or a 'None' line."""
    if not test_failures:
        return "None reported."
    lines: list[str] = []
    for tf in test_failures:
        if isinstance(tf, dict):
            name = tf.get("name") or tf.get("test") or "unknown test"
            detail = tf.get("detail") or tf.get("message") or ""
            lines.append(f"- {name}: {detail}".rstrip(": ").rstrip())
        else:
            lines.append(f"- {tf}")
    return "\n".join(lines)


def build_retry_context(
    task_id: str,
    gate_name: str,
    gate_feedback: str,
    test_failures: list[Any] | None,
    attempt_number: int,
    base_dir: str | Path | None = None,
) -> str:
    """Build (and persist) the retry-context packet for one failed attempt.

    Args:
        task_id: The task being retried.
        gate_name: Which gate rejected the prior attempt (e.g. "Architect diff
            review", "QA", "Backwards-compatibility").
        gate_feedback: The gate's own feedback text explaining the rejection.
        test_failures: Structured failing-test records (dicts with ``name`` /
            ``detail``) or None when the gate produced no test failures.
        attempt_number: The number of the attempt about to begin (the prior
            attempt is ``attempt_number - 1``).
        base_dir: Root under which ``retry/{task_id}/attempt-{N}/context.md`` is
            written. Defaults to the current working directory; the orchestrator
            passes the run/state dir so packets land alongside run state.

    Returns:
        The markdown packet (also written to disk).
    """
    prior = attempt_number - 1
    feedback = gate_feedback.strip() if gate_feedback else "(no feedback provided)"

    markdown = (
        f"## Retry context — Attempt {attempt_number}\n\n"
        f"### Gate that rejected the previous attempt\n"
        f"{gate_name}\n\n"
        f"### Why it was rejected\n"
        f"{feedback}\n\n"
        f"### Test failures\n"
        f"{_format_test_failures(test_failures)}\n\n"
        f"### Instruction for this attempt\n"
        f"The previous patch (attempt {prior}) was rejected at the "
        f"{gate_name} gate.\n"
        f"Before writing any code, state in one sentence why it failed and what "
        f"must change.\n"
        f"Do NOT repeat the same approach as the previous attempt — a "
        f"fundamentally different fix is required.\n"
    )

    root = Path(base_dir) if base_dir is not None else Path.cwd()
    attempt_dir = root / "retry" / task_id / f"attempt-{attempt_number}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / _CONTEXT_FILENAME).write_text(markdown, encoding="utf-8")

    return markdown


def _now_iso() -> str:
    """UTC timestamp, ISO 8601 with trailing ``Z`` (no microseconds)."""
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write ``data`` as JSON to ``path`` atomically (tmp + replace).

    The replace is guarded with a bounded retry on ``PermissionError`` /
    ``FileExistsError``, the same lock-race pattern state_store.py uses for
    Windows, where a concurrent reader can briefly hold the destination open.
    """
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    deadline = time.monotonic() + _REPLACE_RETRY_TIMEOUT_SECONDS
    while True:
        try:
            tmp.replace(path)  # os.replace — atomic overwrite
            return
        except (FileExistsError, PermissionError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(_REPLACE_RETRY_POLL_SECONDS)


def _acquire_batch_summary_lock(lock_path: Path) -> int:
    """Acquire the interprocess lock guarding batch_summary.json updates."""
    deadline = time.monotonic() + _BATCH_SUMMARY_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except (FileExistsError, PermissionError):
            # Reclaim an abandoned lock: if the existing lock file is older than
            # the stale threshold the previous writer almost certainly crashed
            # without releasing it. Unlink it once and retry so a crash can't
            # wedge all future batch_summary updates.
            if _reclaim_stale_lock(lock_path):
                try:
                    return os.open(
                        str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR
                    )
                except (FileExistsError, PermissionError):
                    pass  # Another writer beat us to it — fall through to poll.
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Timed out waiting for batch_summary lock; another writer "
                    "may hold a stale lock."
                )
            time.sleep(_BATCH_SUMMARY_LOCK_POLL_SECONDS)


def _reclaim_stale_lock(lock_path: Path) -> bool:
    """Unlink ``lock_path`` if it is older than the stale threshold.

    Returns True if a stale lock was removed (caller should retry acquisition),
    False otherwise. Any error inspecting/removing the lock is treated as
    "not stale" so a live lock is never disturbed.
    """
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return False
    if age < _BATCH_SUMMARY_LOCK_STALE_SECONDS:
        return False
    try:
        os.unlink(str(lock_path))
    except OSError:
        return False
    log.warning(
        "Reclaimed stale batch_summary lock %s (age %.0fs); previous writer "
        "likely crashed without releasing it.",
        lock_path.name,
        age,
    )
    return True


def _release_batch_summary_lock(lock_path: Path, lock_fd: int) -> None:
    """Release the batch_summary.json lock acquired above."""
    try:
        os.close(lock_fd)
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            return


def write_batch_summary(
    state_dir: str | Path,
    task_id: str,
    status: str,
    attempt_count: int,
) -> Path:
    """Merge one task's remediation status into ``state_dir/batch_summary.json``.

    Read-modify-write: preserves any previously recorded tasks and updates (or
    inserts) ``task_id``. This is the file Screen 3 (``GET /api/progress``)
    polls. The summary schema::

        {
          "run_label": "<state_dir basename>",
          "last_updated": "<ISO timestamp>",
          "tasks": {
            "<task_id>": {"status": ..., "attempt_count": N, "last_updated": ...}
          }
        }

    Args:
        state_dir: The run/state directory (basename becomes ``run_label``).
        task_id: The task whose status is being recorded.
        status: One of ``COMPLETE`` / ``ESCALATED`` / ``IN_PROGRESS`` / ``FAILED``.
        attempt_count: How many attempts the task has taken so far.

    Returns:
        The path to ``batch_summary.json``.
    """
    state_dir = Path(state_dir)
    path = state_dir / _BATCH_SUMMARY_FILENAME

    # Serialize the FULL read-modify-write under an exclusive lock so two
    # concurrent updates for different tasks cannot both read the same base and
    # clobber each other — the atomic replace alone does not prevent lost
    # updates (FIX-BLOCKING-012).
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = _acquire_batch_summary_lock(lock_path)
    try:
        data: dict[str, Any] = {"run_label": state_dir.name, "last_updated": "", "tasks": {}}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    data = existing
                    if not isinstance(data.get("tasks"), dict):
                        data["tasks"] = {}
                    data["run_label"] = state_dir.name
            except (OSError, ValueError) as exc:
                # Corrupt/unreadable summary — start fresh rather than fail the
                # run, but surface the loss so the reset is not silent.
                log.warning("Corrupt batch_summary.json — starting fresh: %s", exc)
                data = {"run_label": state_dir.name, "last_updated": "", "tasks": {}}

        now = _now_iso()
        data["last_updated"] = now
        data["tasks"][str(task_id)] = {
            "status": status,
            "attempt_count": int(attempt_count),
            "last_updated": now,
        }
        _atomic_write_json(path, data)
    finally:
        _release_batch_summary_lock(lock_path, lock_fd)
    return path


__all__ = ["build_retry_context", "write_batch_summary"]
