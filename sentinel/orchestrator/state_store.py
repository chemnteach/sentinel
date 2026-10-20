"""
sentinel/orchestrator/state_store.py

JSON state file management for Sentinel.

All agent state is persisted as JSON files in ``sentinel/state/`` and
committed to the repository after each significant step. These files are
the single source of truth. Agents are stateless between model calls —
the Orchestrator reconstructs full context from state files before every
agent invocation.

Git history provides the complete audit trail. State can be inspected,
rolled back, or replayed using standard git tooling.

State Files
-----------
repositories.json   — Repo manifest from Layer 0a
modules.json        — Module inventory from Layer 0a
scan_metadata.json  — Scan-scoped repository identity metadata
dependencies.json   — Dependency graph from Layer 0b
findings.json       — Accumulated review findings (all skills)
plan.json           — Approved remediation plan
tasks.json          — Decomposed atomic tasks
reviews.json        — Agent review records
disputes.json       — Disagreement records
tech_debt.json      — Tech debt register with human triage
audit_log.json      — Immutable append-only action trail
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Canonical state file names
_FILES = {
    "repositories": "repositories.json",
    "modules": "modules.json",
    "scan_metadata": "scan_metadata.json",
    "dependencies": "dependencies.json",
    "findings": "findings.json",
    # Stage 5c — findings the automated verifier could not confirm against
    # source. Operator-visible only; never surfaced to the code owner.
    "unverified_findings": "unverified_findings.json",
    "plan": "plan.json",
    "advisory_findings": "advisory_findings.json",
    "tasks": "tasks.json",
    "reviews": "reviews.json",
    "disputes": "disputes.json",
    "tech_debt": "tech_debt.json",
    "audit_log": "audit_log.json",
    # Acceptance-criteria completeness gate (layer5b, SHADOW MODE) — one
    # append-style entry per completed task; never overwritten.
    "acceptance_gate_log": "acceptance_gate_log.json",
    "coverage_report": "coverage_report.json",
    "scope_manifest": "scope_manifest.json",
    "line_coverage_map": "line_coverage_map.json",
    "coverage_certificate": "coverage_certificate.json",
    "failed_chunk_registry": "failed_chunk_registry.json",
    "module_review_status": "module_review_status.json",
    # transition_manifests: per-task before/after narrative artefacts
    # produced at the QA_PASS -> DOC_UPDATED gate. Distinct from
    # pr_manifests (Phase 3 enhancement-PR lineage records, registered
    # in the Phase 3 block below).
    "transition_manifests": "transition_manifests.json",
    # Phase 0 — Quality Foundations
    "deterministic_findings": "deterministic_findings.json",
    "retrieval_manifest": "retrieval_manifest.json",
    "reasoning_findings": "reasoning_findings.json",
    "reconciliation_report": "reconciliation_report.json",
    "quality_scorecard": "quality_scorecard.json",
    "finding_conflicts": "finding_conflicts.json",
    "shadow_compare_report": "shadow_compare_report.json",
    "phase1_assessment": "phase1_assessment.json",
    "analysis_pack": "analysis_pack.json",
    "remediation_patterns": "remediation_patterns.json",
    # S2-2 — Stage 5d root-cause remediation bundles.
    "bundles": "bundles.json",
    "semantic_groups": "semantic_groups.json",
    "semantic_groups_near_misses": "semantic_groups_near_misses.json",
    "concept_themes": "concept_themes.json",
    # Phase 3 — Feature Enhancement
    "fers": "fers.json",
    "feasibility": "feasibility.json",
    "specs": "specs.json",
    "pr_manifests": "pr_manifests.json",
    "pr_reviews": "pr_reviews.json",
    # Defect RCA & Governed Bug Remediation
    "defects": "defects.json",
    "rca_reports": "rca_reports.json",
    "defect_specs": "defect_specs.json",
    # Per-call LLM telemetry (public provider clients)
    "llm_telemetry": "llm_telemetry.json",
}

_FER_SUBMIT_LOCK_FILE = "fers.submit.lock"
_FER_SUBMIT_LOCK_POLL_SECONDS = 0.005
_FER_SUBMIT_LOCK_TIMEOUT_SECONDS = 10.0

_DEFECT_SUBMIT_LOCK_FILE = "defects.submit.lock"
_DEFECT_SUBMIT_LOCK_POLL_SECONDS = 0.005
_DEFECT_SUBMIT_LOCK_TIMEOUT_SECONDS = 10.0

# Serializes read-modify-write mutations against list-backed state files so
# concurrent appends/upserts (and the atomic task+audit transition) cannot lose
# updates by overwriting a newer on-disk image with stale in-memory state.
_LIST_MUTATION_LOCK_FILE = "state.list_mutation.lock"
_LIST_MUTATION_LOCK_POLL_SECONDS = 0.005
_LIST_MUTATION_LOCK_TIMEOUT_SECONDS = 10.0


class StateStoreError(RuntimeError):
    """Raised when a state file operation fails."""


class TaskConcurrencyConflictError(StateStoreError):
    """Raised when a task's stored record changed since the caller's snapshot.

    Signals an optimistic-concurrency conflict: the transition was computed
    against a stale task version and must not overwrite the newer stored state.
    """


class StateStore:
    """
    Atomic read-modify-write operations for all JSON state files.

    The state directory is ``sentinel/state/`` inside the target repository.
    Each public method performs an atomic read-modify-write to prevent
    partial state corruption.

    All state changes can optionally be committed to git after writing,
    providing a complete audit trail via git history.
    """

    def __init__(self, state_dir: Path, repo_path: Path | None = None) -> None:
        self._dir = state_dir
        self._repo = repo_path or state_dir.parent.parent
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Generic read/write
    # ------------------------------------------------------------------

    def read(self, store_name: str) -> Any:
        """
        Read a state file and return its parsed contents.

        Returns an empty list for list-based stores (findings, modules, etc.)
        or an empty dict for object-based stores if the file does not yet
        exist.
        """
        path = self._path(store_name)
        if not path.exists():
            log.debug("State file '%s' not found — returning default", store_name)
            return _default_value(store_name)
        try:
            text = path.read_text(encoding="utf-8")
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise StateStoreError(
                f"State file '{store_name}' is corrupt: {exc}"
            ) from exc

    def write(
        self,
        store_name: str,
        data: Any,
        commit: bool = False,
        commit_message: str | None = None,
        *,
        sort_keys: bool = False,
    ) -> None:
        """
        Write data to a state file atomically (write to temp, then replace).

        Args:
            store_name: Key from the ``_FILES`` map.
            data: JSON-serialisable data.
            commit: If True, commit the change to git after writing.
            commit_message: Git commit message (uses default if omitted).
            sort_keys: If True, JSON output is emitted with keys sorted
                alphabetically at every nesting level. Keyword-only.
                Default ``False`` preserves Python dict-insertion order
                (byte-stable across runs given deterministic input).
                Opted into by ``semantic_groups`` writes (cycle 20
                sub-step 2) for cross-version git-diff stability.
        """
        path = self._path(store_name)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(
                    data,
                    indent=2,
                    ensure_ascii=False,
                    sort_keys=sort_keys,
                ),
                encoding="utf-8",
            )
            # pathlib.Path.rename() fails on Windows if the destination exists.
            # Path.replace() uses os.replace() which atomically overwrites.
            self._atomic_replace(tmp, path)
        except OSError as exc:
            raise StateStoreError(
                f"Failed to write state file '{store_name}': {exc}"
            ) from exc

        log.debug("State file '%s' written (%d bytes)", store_name, path.stat().st_size)

        if commit:
            self._git_commit(
                path,
                commit_message or f"sentinel: update {store_name}",
            )

    # Windows: a virus scanner / file watcher can transiently lock the
    # destination during os.replace, raising PermissionError (WinError 5). One
    # such failure must not abort a multi-hour run, so retry with backoff.
    _ATOMIC_REPLACE_ATTEMPTS = 5
    _ATOMIC_REPLACE_BACKOFF_SECONDS = 0.2

    def _atomic_replace(self, tmp: Path, path: Path) -> None:
        """Atomically replace ``path`` with ``tmp``, retrying transient locks."""
        last_exc: OSError | None = None
        for attempt in range(1, self._ATOMIC_REPLACE_ATTEMPTS + 1):
            try:
                tmp.replace(path)
                return
            except PermissionError as exc:
                last_exc = exc
                if attempt < self._ATOMIC_REPLACE_ATTEMPTS:
                    log.warning(
                        "Atomic replace of %s locked (attempt %d/%d): %s — retrying",
                        path.name, attempt, self._ATOMIC_REPLACE_ATTEMPTS, exc,
                    )
                    time.sleep(self._ATOMIC_REPLACE_BACKOFF_SECONDS * attempt)
        # Exhausted retries — surface to the caller's OSError handler.
        raise last_exc if last_exc is not None else OSError("atomic replace failed")

    def append_to_list(
        self,
        store_name: str,
        item: dict[str, Any],
        commit: bool = False,
        commit_message: str | None = None,
    ) -> None:
        """
        Append a single item to a list-based state file.

        Performs a lock-guarded atomic read-modify-write so concurrent
        appends/upserts against the same store cannot lose updates.
        """
        def _mutate(data: list[Any]) -> None:
            data.append(item)

        self._guarded_list_mutation(
            store_name, _mutate, commit=commit, commit_message=commit_message
        )

    def upsert_in_list(
        self,
        store_name: str,
        item: dict[str, Any],
        key_field: str,
        commit: bool = False,
        commit_message: str | None = None,
    ) -> None:
        """
        Insert or update an item in a list-based state file, keyed by
        ``key_field``.

        If an existing item with the same key value is found, it is
        replaced. Otherwise the item is appended.

        Uses the same lock-guarded read-modify-write path as
        ``append_to_list`` so concurrent list mutations are serialized.
        """
        key_value = item.get(key_field)

        def _mutate(data: list[Any]) -> None:
            for i, existing in enumerate(data):
                if existing.get(key_field) == key_value:
                    data[i] = item
                    return
            data.append(item)

        self._guarded_list_mutation(
            store_name, _mutate, commit=commit, commit_message=commit_message
        )

    def _guarded_list_mutation(
        self,
        store_name: str,
        mutate: "Any",
        *,
        commit: bool = False,
        commit_message: str | None = None,
    ) -> None:
        """Serialize a read-modify-write against a list-backed state file.

        Acquires the shared list-mutation lock for the whole read → in-memory
        mutation → durable write sequence, so two concurrent callers cannot
        each read the same image and have the later write clobber the earlier
        committed one. Single-threaded behavior is unchanged.
        """
        lock_path = self._dir / _LIST_MUTATION_LOCK_FILE
        lock_fd = self._acquire_list_mutation_lock(lock_path)
        try:
            data = self.read(store_name)
            if not isinstance(data, list):
                raise StateStoreError(
                    f"State file '{store_name}' is not a list — cannot mutate. "
                    f"Got type: {type(data).__name__}"
                )
            mutate(data)
            self.write(store_name, data, commit=commit, commit_message=commit_message)
        finally:
            self._release_list_mutation_lock(lock_path, lock_fd)

    def create_fer(self, fer_record: dict[str, Any]) -> dict[str, Any]:
        """
        Atomically allocate the next FER identifier and append a new FER record.

        This operation serializes allocation and persistence under an exclusive
        lock so concurrent callers cannot reserve the same FER ID.

        Raises:
            StateStoreError: If FER state is invalid or a duplicate ID is found.
        """

        lock_path = self._dir / _FER_SUBMIT_LOCK_FILE
        lock_fd = self._acquire_fer_submit_lock(lock_path)
        try:
            data = self.read("fers")
            if not isinstance(data, list):
                raise StateStoreError(
                    "State file 'fers' is not a list — cannot create FER."
                )

            max_seen = 0
            seen_ids: set[str] = set()
            for existing in data:
                existing_id = existing.get("fer_id")
                if not isinstance(existing_id, str):
                    continue

                if existing_id in seen_ids:
                    raise StateStoreError(
                        f"State file 'fers' contains duplicate fer_id '{existing_id}'."
                    )
                seen_ids.add(existing_id)

                match = re.match(r"^FER-(\d+)$", existing_id)
                if match:
                    max_seen = max(max_seen, int(match.group(1)))

            fer_id = f"FER-{(max_seen + 1):03d}"
            if fer_id in seen_ids:
                raise StateStoreError(
                    f"Duplicate fer_id '{fer_id}' encountered during FER creation."
                )

            record_to_persist = dict(fer_record)
            record_to_persist["fer_id"] = fer_id
            data.append(record_to_persist)
            self.write("fers", data, commit=False)
            return record_to_persist
        finally:
            self._release_fer_submit_lock(lock_path, lock_fd)

    def create_defect(self, defect_record: dict[str, Any]) -> dict[str, Any]:
        """
        Atomically allocate the next DEF-NNN identifier and append a new Defect record.

        This operation serializes allocation and persistence under an exclusive
        lock so concurrent callers cannot reserve the same DEF ID.

        Raises:
            StateStoreError: If defect state is invalid or a duplicate ID is found.
        """
        lock_path = self._dir / _DEFECT_SUBMIT_LOCK_FILE
        lock_fd = self._acquire_defect_submit_lock(lock_path)
        try:
            data = self.read("defects")
            if not isinstance(data, list):
                raise StateStoreError(
                    "State file 'defects' is not a list — cannot create defect."
                )

            max_seen = 0
            seen_ids: set[str] = set()
            for existing in data:
                existing_id = existing.get("defect_id")
                if not isinstance(existing_id, str):
                    continue

                if existing_id in seen_ids:
                    raise StateStoreError(
                        f"State file 'defects' contains duplicate defect_id '{existing_id}'."
                    )
                seen_ids.add(existing_id)

                match = re.match(r"^DEF-(\d+)$", existing_id)
                if match:
                    max_seen = max(max_seen, int(match.group(1)))

            defect_id = f"DEF-{(max_seen + 1):03d}"
            if defect_id in seen_ids:
                raise StateStoreError(
                    f"Duplicate defect_id '{defect_id}' encountered during defect creation."
                )

            record_to_persist = dict(defect_record)
            record_to_persist["defect_id"] = defect_id
            data.append(record_to_persist)
            self.write("defects", data, commit=False)
            return record_to_persist
        finally:
            self._release_defect_submit_lock(lock_path, lock_fd)

    # ------------------------------------------------------------------
    # Typed convenience methods
    # ------------------------------------------------------------------

    def append_finding(
        self,
        finding: dict[str, Any],
        commit: bool = False,
    ) -> None:
        """Append a review finding to findings.json."""
        self.append_to_list(
            "findings",
            finding,
            commit=commit,
            commit_message="sentinel: add finding",
        )

    def append_audit(self, event: dict[str, Any]) -> None:
        """
        Append an event to the immutable audit log.

        The audit log is never overwritten — only appended. This is
        called for every significant action. Routed through the shared
        list-mutation lock so audit appends serialize with task+audit
        atomic transitions and cannot lose concurrent updates.
        """
        def _mutate(data: list[Any]) -> None:
            event_to_persist = dict(event)
            event_to_persist.setdefault(
                "timestamp",
                datetime.now(timezone.utc).isoformat(),
            )
            data.append(event_to_persist)

        self._guarded_list_mutation("audit_log", _mutate, commit=False)

    def upsert_task(self, task: dict[str, Any], commit: bool = False) -> None:
        """Upsert a task record in tasks.json, keyed by task_id."""
        self.upsert_in_list(
            "tasks",
            task,
            key_field="task_id",
            commit=commit,
            commit_message=f"sentinel: update task {task.get('task_id', '?')}",
        )

    def persist_task_with_audit(
        self,
        task: dict[str, Any],
        audit_events: list[dict[str, Any]],
        commit: bool = False,
        commit_message: str | None = None,
        expected_revision: int | None = None,
        extra_operations: list[tuple[str, Any]] | None = None,
    ) -> None:
        """Atomically upsert a task and append its audit events in one call.

        The task upsert (tasks.json) and the audit appends (audit_log.json)
        are applied under the shared list-mutation lock. Both files are
        snapshotted before writing; if any write raises, both files are
        restored to their pre-operation image so no partial task-state change
        and no orphan audit entry remain externally visible. Returns only
        after both writes are durably recorded.

        When ``expected_revision`` is provided, an optimistic-concurrency check
        runs inside the lock: if an existing task record for the same id has a
        different ``revision``, a ``TaskConcurrencyConflictError`` is raised and
        nothing is written (the caller's snapshot was stale). ``revision`` is a
        monotonic counter, robust to coarse wall-clock resolution.

        ``extra_operations`` — optional ``(key, value_or_transform)`` pairs for
        *other* state stores (e.g. ``pr_reviews``, ``disputes``) that must commit
        together with the task+audit change. Each value may be a concrete list/dict
        **or** a single-argument callable ``fn(current) -> new`` that is applied
        *inside the lock* so the read and write are atomic — eliminating the
        lost-update window that a read-before-lock would leave open for concurrent
        callers (FIX-CRITICAL run-020). They are snapshotted and rolled back
        alongside tasks/audit so the whole multi-store change is all-or-nothing
        under a single lock and one git commit.
        """
        extra_operations = list(extra_operations or [])
        lock_path = self._dir / _LIST_MUTATION_LOCK_FILE
        lock_fd = self._acquire_list_mutation_lock(lock_path)

        tasks_path = self._path("tasks")
        audit_path = self._path("audit_log")
        tasks_snapshot = tasks_path.read_bytes() if tasks_path.exists() else None
        audit_snapshot = audit_path.read_bytes() if audit_path.exists() else None
        # Snapshot every extra store touched so rollback restores them too.
        extra_snapshots: list[tuple[Path, bytes | None]] = []
        for key, _value in extra_operations:
            extra_path = self._path(key)
            extra_snapshots.append(
                (extra_path, extra_path.read_bytes() if extra_path.exists() else None)
            )

        try:
            tasks = self.read("tasks")
            if not isinstance(tasks, list):
                raise StateStoreError(
                    "State file 'tasks' is not a list — cannot persist task."
                )
            audit = self.read("audit_log")
            if not isinstance(audit, list):
                raise StateStoreError(
                    "State file 'audit_log' is not a list — cannot append audit."
                )

            task_id = task.get("task_id")
            for i, existing in enumerate(tasks):
                if existing.get("task_id") == task_id:
                    # Optimistic-concurrency guard: reject a write computed
                    # against a stale snapshot rather than clobbering newer state.
                    if (
                        expected_revision is not None
                        and int(existing.get("revision", 0)) != expected_revision
                    ):
                        raise TaskConcurrencyConflictError(
                            f"Task {task_id} was modified concurrently "
                            f"(expected revision={expected_revision}, "
                            f"stored={existing.get('revision', 0)}); "
                            "refusing to overwrite newer state."
                        )
                    tasks[i] = task
                    break
            else:
                tasks.append(task)

            for event in audit_events or []:
                event_to_persist = dict(event)
                event_to_persist.setdefault(
                    "timestamp", datetime.now(timezone.utc).isoformat()
                )
                audit.append(event_to_persist)

            try:
                self.write("tasks", tasks, commit=False)
                self.write("audit_log", audit, commit=False)
                for key, value_or_fn in extra_operations:
                    value = value_or_fn(self.read(key)) if callable(value_or_fn) else value_or_fn
                    self.write(key, value, commit=False)
            except Exception:
                # Roll every touched file back to its pre-operation image so no
                # partial task change, orphan audit entry, or half-written extra
                # store survives. Direct byte restore (not self.write) so
                # rollback still works when the failure came from a patched or
                # failing write path.
                self._restore_file(tasks_path, tasks_snapshot)
                self._restore_file(audit_path, audit_snapshot)
                for extra_path, extra_snapshot in extra_snapshots:
                    self._restore_file(extra_path, extra_snapshot)
                raise
        finally:
            self._release_list_mutation_lock(lock_path, lock_fd)

        # Intentional design decision: the mutation lock is released BEFORE the
        # git commit runs. git commit is comparatively slow, and holding the
        # list-mutation lock across it would serialize all writers behind every
        # commit. The trade-off is that another writer may mutate state between
        # the lock release above and the commit below, so a single commit can
        # capture more than this call's change. That is acceptable — the
        # durable JSON write already happened under the lock (all-or-nothing),
        # and git history is an audit trail, not the source of truth.
        if commit:
            if not self.commit_state(
                commit_message
                or f"sentinel: update task {task.get('task_id', '?')}"
            ):
                log.warning(
                    "State for task %s written to disk but git commit was "
                    "skipped/failed — audit-trail commit missing.",
                    task.get("task_id", "?"),
                )

    def atomic_write(
        self,
        operations: list[tuple[str, Any]],
        *,
        commit: bool = True,
        commit_message: str = "sentinel: atomic state update",
        expected_revisions: dict[str, int] | None = None,
    ) -> None:
        """Write multiple state keys atomically under one lock.

        All writes in ``operations`` (``(key, value)`` pairs) succeed together
        or every touched file is rolled back to its pre-operation byte image —
        the shared multi-store generalization of ``persist_task_with_audit``.

        Args:
            operations: ``(key, value)`` pairs to write.
            commit: Git-commit once after all writes succeed.
            commit_message: Git commit message.
            expected_revisions: Optional optimistic-concurrency check mapping
                ``key -> expected revision``. The current value's top-level
                ``revision`` (0 when absent or non-dict) is compared before any
                write; a mismatch raises ``StateStoreError`` and nothing is
                written.

        Raises:
            StateStoreError: On a revision mismatch or any write failure (after
                rolling back all touched files).
        """
        if not operations:
            return

        lock_path = self._dir / _LIST_MUTATION_LOCK_FILE
        lock_fd = self._acquire_list_mutation_lock(lock_path)
        snapshots: list[tuple[Path, bytes | None]] = []
        try:
            # Validate keys and snapshot current bytes before any mutation.
            for key, _value in operations:
                path = self._path(key)
                snapshots.append(
                    (path, path.read_bytes() if path.exists() else None)
                )

            # Optimistic-concurrency guard — checked before any write.
            for key, expected in (expected_revisions or {}).items():
                current = self.read(key)
                current_rev = (
                    int(current.get("revision", 0))
                    if isinstance(current, dict)
                    else 0
                )
                if current_rev != int(expected):
                    raise StateStoreError(
                        f"Revision mismatch for '{key}' "
                        f"(expected {expected}, stored {current_rev}); "
                        "refusing atomic write."
                    )

            for key, value in operations:
                self.write(key, value, commit=False)
        except Exception as exc:
            for path, snapshot in snapshots:
                self._restore_file(path, snapshot)
            if isinstance(exc, StateStoreError):
                raise
            raise StateStoreError(f"atomic_write failed: {exc}") from exc
        finally:
            self._release_list_mutation_lock(lock_path, lock_fd)

        if commit:
            if not self.commit_state(commit_message):
                log.warning(
                    "atomic_write state written to disk but git commit "
                    "skipped/failed: %s",
                    commit_message,
                )

    def atomic_append(
        self,
        key: str,
        item: Any,
        *,
        commit: bool = True,
        commit_message: str = "sentinel: atomic append",
    ) -> None:
        """Atomically append ``item`` to a list-backed key.

        The read, append, and write all run under one acquisition of the shared
        list-mutation lock (via :meth:`atomic_read_modify_write`), so two
        concurrent appenders cannot each read the same pre-append image and have
        the later write clobber the earlier one (lost update). Single-threaded
        behavior is unchanged.
        """
        def _append(current: Any) -> list[Any]:
            if not current:
                current = []
            if not isinstance(current, list):
                raise StateStoreError(f"{key} is not a list")
            return current + [item]

        self.atomic_read_modify_write(
            [(key, _append)],
            commit=commit,
            commit_message=commit_message,
        )

    def atomic_read_modify_write(
        self,
        operations: list[tuple[str, Any]],
        *,
        commit: bool = True,
        commit_message: str = "sentinel: atomic RMW",
    ) -> None:
        """Read-modify-write multiple keys atomically under one lock.

        For each ``(key, transform)`` pair the current stored value is read
        **inside** the shared list-mutation lock, ``transform(current)`` produces
        the new value, and the result is written — so the entire read → modify →
        write cycle for every key happens under a single lock acquisition. No
        concurrent writer can interleave between the read and the write, which
        closes the lost-update window a read-*before*-lock leaves open. All
        writes succeed together or every touched file is rolled back to its
        pre-operation byte image.

        Args:
            operations: ``(key, transform)`` pairs. ``transform`` is called with
                the current stored value (``None`` / empty when the key is
                absent) and returns the new value to persist.
            commit: Git-commit once after all writes succeed.
            commit_message: Git commit message.

        Raises:
            StateStoreError: On any read/transform/write failure (after rolling
                back all touched files).
        """
        if not operations:
            return

        lock_path = self._dir / _LIST_MUTATION_LOCK_FILE
        lock_fd = self._acquire_list_mutation_lock(lock_path)
        snapshots: list[tuple[Path, bytes | None]] = []
        try:
            # Snapshot current bytes before any mutation so a mid-write failure
            # rolls every touched file back to its pre-operation image.
            for key, _transform in operations:
                path = self._path(key)
                snapshots.append(
                    (path, path.read_bytes() if path.exists() else None)
                )

            # Read → modify → write each key INSIDE the lock.
            for key, transform in operations:
                current = self.read(key)
                new_value = transform(current)
                self.write(key, new_value, commit=False)
        except Exception as exc:
            for path, snapshot in snapshots:
                self._restore_file(path, snapshot)
            if isinstance(exc, StateStoreError):
                raise
            raise StateStoreError(
                f"atomic_read_modify_write failed: {exc}"
            ) from exc
        finally:
            self._release_list_mutation_lock(lock_path, lock_fd)

        if commit:
            if not self.commit_state(commit_message):
                log.warning(
                    "atomic_read_modify_write state written to disk but git "
                    "commit skipped/failed: %s",
                    commit_message,
                )

    @staticmethod
    def _restore_file(path: Path, snapshot: bytes | None) -> None:
        """Restore ``path`` to a previously captured byte snapshot.

        ``snapshot is None`` means the file did not exist before the operation,
        so it is removed. Best-effort: rollback failures are logged, not raised,
        so the original error is not masked.
        """
        try:
            if snapshot is None:
                if path.exists():
                    path.unlink()
            else:
                path.write_bytes(snapshot)
        except OSError as exc:
            log.error("Failed to roll back state file %s: %s", path.name, exc)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Retrieve a single task by task_id, or None if not found."""
        tasks = self.read("tasks")
        return next((t for t in tasks if t.get("task_id") == task_id), None)

    def get_all_findings(self) -> list[dict[str, Any]]:
        """Return all accumulated findings."""
        return self.read("findings")

    def get_findings_by_severity(self, severity: str) -> list[dict[str, Any]]:
        """Return findings filtered by severity (Critical, P1, P2, P3)."""
        return [
            f for f in self.get_all_findings()
            if f.get("severity", "").upper() == severity.upper()
        ]

    def commit_state(self, message: str) -> bool:
        """
        Stage all state files and commit to git.

        Called after significant workflow steps to provide an audit trail.

        Returns ``True`` when the commit was created, ``False`` when the git
        commit was skipped or failed (e.g. not a repo, nothing staged, git
        error). A git failure is deliberately non-fatal — the state is already
        durably written to disk — but the boolean lets callers detect and log
        that the audit-trail commit did not happen (they must not assume it
        did).
        """
        try:
            subprocess.run(
                ["git", "add", str(self._dir)],
                cwd=self._repo,
                check=True,
                capture_output=True,
                timeout=60,
            )
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=self._repo,
                check=True,
                capture_output=True,
                timeout=60,
            )
            log.info("Git commit: %s", message)
            return True
        except subprocess.CalledProcessError as exc:
            # Non-fatal — state is still written to disk; commit just fails
            log.warning(
                "Git commit failed (state still on disk): %s",
                exc.stderr.decode(errors="replace") if exc.stderr else str(exc),
            )
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path(self, store_name: str) -> Path:
        filename = _FILES.get(store_name)
        if filename is None:
            raise StateStoreError(
                f"Unknown state store: '{store_name}'. "
                f"Valid stores: {', '.join(_FILES)}"
            )
        return self._dir / filename

    def _acquire_fer_submit_lock(self, lock_path: Path) -> int:
        """Acquire an interprocess-safe exclusive file lock for FER submits."""

        deadline = time.monotonic() + _FER_SUBMIT_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except (FileExistsError, PermissionError):
                if time.monotonic() >= deadline:
                    raise StateStoreError(
                        "Timed out waiting for FER submit lock. "
                        "Another process may be holding a stale lock."
                    )
                time.sleep(_FER_SUBMIT_LOCK_POLL_SECONDS)
            except OSError as exc:
                raise StateStoreError(f"Failed to acquire FER submit lock: {exc}") from exc

    def _release_fer_submit_lock(self, lock_path: Path, lock_fd: int) -> None:
        """Release the FER submit lock acquired by _acquire_fer_submit_lock."""

        try:
            os.close(lock_fd)
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                return
            except OSError as exc:
                log.warning("Failed to remove FER submit lock file %s: %s", lock_path, exc)

    def _acquire_defect_submit_lock(self, lock_path: Path) -> int:
        """Acquire an interprocess-safe exclusive file lock for Defect submits."""
        deadline = time.monotonic() + _DEFECT_SUBMIT_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except (FileExistsError, PermissionError):
                if time.monotonic() >= deadline:
                    raise StateStoreError(
                        "Timed out waiting for Defect submit lock. "
                        "Another process may be holding a stale lock."
                    )
                time.sleep(_DEFECT_SUBMIT_LOCK_POLL_SECONDS)
            except OSError as exc:
                raise StateStoreError(f"Failed to acquire Defect submit lock: {exc}") from exc

    def _release_defect_submit_lock(self, lock_path: Path, lock_fd: int) -> None:
        """Release the Defect submit lock acquired by _acquire_defect_submit_lock."""
        try:
            os.close(lock_fd)
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                return
            except OSError as exc:
                log.warning("Failed to remove Defect submit lock file %s: %s", lock_path, exc)

    def _acquire_list_mutation_lock(self, lock_path: Path) -> int:
        """Acquire the interprocess-safe lock guarding list-backed mutations."""
        deadline = time.monotonic() + _LIST_MUTATION_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except (FileExistsError, PermissionError):
                if time.monotonic() >= deadline:
                    raise StateStoreError(
                        "Timed out waiting for list-mutation lock. "
                        "Another process may be holding a stale lock."
                    )
                time.sleep(_LIST_MUTATION_LOCK_POLL_SECONDS)
            except OSError as exc:
                raise StateStoreError(
                    f"Failed to acquire list-mutation lock: {exc}"
                ) from exc

    def _release_list_mutation_lock(self, lock_path: Path, lock_fd: int) -> None:
        """Release the list-mutation lock acquired by _acquire_list_mutation_lock."""
        try:
            os.close(lock_fd)
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                return
            except OSError as exc:
                log.warning(
                    "Failed to remove list-mutation lock file %s: %s", lock_path, exc
                )

    def _git_commit(self, path: Path, message: str) -> None:
        try:
            subprocess.run(
                ["git", "add", str(path)],
                cwd=self._repo,
                check=True,
                capture_output=True,
                timeout=60,
            )
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=self._repo,
                check=True,
                capture_output=True,
                timeout=60,
            )
        except subprocess.CalledProcessError as exc:
            log.warning(
                "Git commit failed for %s: %s",
                path.name,
                exc.stderr.decode(errors="replace") if exc.stderr else str(exc),
            )


def _default_value(store_name: str) -> Any:
    """Return the appropriate empty value for each store type."""
    # List-based stores
    list_stores = {
        "repositories", "modules", "dependencies", "findings",
        "unverified_findings",  # Stage 5c quarantine
        "reviews", "disputes", "tech_debt", "audit_log", "tasks",
        "acceptance_gate_log",  # layer5b shadow-gate append log
        "failed_chunk_registry", "module_review_status",
        "transition_manifests",
        "deterministic_findings", "retrieval_manifest", "reasoning_findings",
        "finding_conflicts",
        "analysis_pack",
        "remediation_patterns",
        "bundles",
        "semantic_groups",
        "semantic_groups_near_misses",
        "concept_themes",
        # Phase 3
        "fers", "feasibility", "specs", "pr_manifests", "pr_reviews",
        # Defect RCA & Governed Bug Remediation
        "defects", "rca_reports", "defect_specs",
        "llm_telemetry",
    }
    if store_name in list_stores:
        return []
    # Object-based stores
    return {}
