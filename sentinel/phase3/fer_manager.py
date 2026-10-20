"""
sentinel/phase3/fer_manager.py

Feature Enhancement Request (FER) manager for Sentinel Phase 3.

Handles FER intake, schema validation, status lifecycle, and the human
triage gate. FERs are persisted in sentinel/state/fers.json and committed
to the repository after each status change.

FER Status Lifecycle
--------------------
PENDING         — submitted, awaiting human triage
APPROVED        — human approved, queued for feasibility assessment
DEFERRED        — human deferred, no immediate action
REJECTED        — human rejected, will not proceed
IN_FEASIBILITY  — Layer 7a feasibility assessment in progress
FEASIBILITY_APPROVED — feasibility complete, human selected approach
IN_SPEC         — Layer 7b spec generation in progress
SPEC_APPROVED   — spec human-approved, tasks generated, ready for Phase 2
IN_PROGRESS     — Phase 2 tasks are being executed
COMPLETE        — all tasks merged, FER delivered
"""

from __future__ import annotations

import copy
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable

from sentinel.orchestrator.state_store import StateStore

log = logging.getLogger(__name__)

# Valid MoSCoW priority values
_PRIORITY_VALUES = frozenset({"MUST_HAVE", "SHOULD_HAVE", "COULD_HAVE", "WONT_HAVE"})

# Valid human triage decisions
_TRIAGE_DECISIONS = frozenset({"APPROVED", "DEFERRED", "REJECTED"})

# Transitions permitted per status
_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING":               frozenset({"APPROVED", "DEFERRED", "REJECTED"}),
    "APPROVED":              frozenset({"IN_FEASIBILITY", "DEFERRED"}),
    "DEFERRED":              frozenset({"APPROVED", "REJECTED"}),
    "REJECTED":              frozenset(),
    "IN_FEASIBILITY":        frozenset({"FEASIBILITY_APPROVED", "APPROVED"}),
    "FEASIBILITY_APPROVED":  frozenset({"IN_SPEC", "DEFERRED"}),
    "IN_SPEC":               frozenset({"SPEC_APPROVED", "FEASIBILITY_APPROVED"}),
    # SPEC_APPROVED -> APPROVED is the dedicated spec-revision transition
    # (FIX-BLOCKING-011); it is not reachable via triage.
    "SPEC_APPROVED":         frozenset({"IN_PROGRESS", "IN_SPEC", "APPROVED"}),
    "IN_PROGRESS":           frozenset({"COMPLETE"}),
    "COMPLETE":              frozenset(),
}

_FER_TRANSITION_LOCK_FILE = "fers.transition.lock"
_FER_TRANSITION_LOCK_POLL_SECONDS = 0.005
_FER_TRANSITION_LOCK_TIMEOUT_SECONDS = 10.0
# A transition lock older than this is treated as abandoned (the holding
# process crashed) and is forcibly reclaimed once so a stale lock cannot wedge
# all future FER transitions.
_FER_TRANSITION_LOCK_STALE_SECONDS = 60.0


class FERValidationError(ValueError):
    """Raised when a FER submission fails schema validation."""


class FERTransitionError(ValueError):
    """Raised when an invalid status transition is attempted."""


class FERTransitionConflictError(FERTransitionError):
    """Raised when a concurrent lifecycle transition conflicts for a FER."""


@dataclass
class FER:
    """
    A Feature Enhancement Request.

    Represents a structured human-submitted request for new functionality.
    FERs describe business capabilities, not implementation approaches.
    """

    fer_id: str
    title: str
    business_justification: str
    priority: str
    affected_systems: list[str] = field(default_factory=list)
    submitted_by: str = "human"
    submitted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: str = "PENDING"
    # Set after feasibility stage
    selected_approach: str = ""
    feasibility_verdict: str = ""
    # Set after spec stage
    spec_version: str = ""
    # Populated when Phase 2 tasks are generated
    task_ids: list[str] = field(default_factory=list)
    # Audit trail entries for this FER
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FER":
        return cls(
            fer_id=data["fer_id"],
            title=data["title"],
            business_justification=data["business_justification"],
            priority=data["priority"],
            affected_systems=data.get("affected_systems", []),
            submitted_by=data.get("submitted_by", "human"),
            submitted_at=data.get("submitted_at", ""),
            status=data.get("status", "PENDING"),
            selected_approach=data.get("selected_approach", ""),
            feasibility_verdict=data.get("feasibility_verdict", ""),
            spec_version=data.get("spec_version", ""),
            task_ids=data.get("task_ids", []),
            history=data.get("history", []),
        )


def _next_fer_id(existing: list[dict[str, Any]]) -> str:
    """Generate the next FER-NNN identifier."""
    if not existing:
        return "FER-001"
    nums = []
    for item in existing:
        m = re.match(r"FER-(\d+)", item.get("fer_id", ""))
        if m:
            nums.append(int(m.group(1)))
    return f"FER-{(max(nums) + 1):03d}" if nums else "FER-001"


class FERManager:
    """
    Manages the complete FER lifecycle from intake through to task generation.

    All state changes are persisted to sentinel/state/fers.json via the
    StateStore. The StateStore commits to git after significant steps,
    providing a full audit trail.
    """

    def __init__(self, store: StateStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Intake
    # ------------------------------------------------------------------

    def submit(self, data: dict[str, Any]) -> FER:
        """
        Submit a new FER.

        Validates the submission against the FER schema, assigns a FER ID,
        persists to state, and returns the created FER.

        Args:
            data: Dict with keys: title, business_justification, priority,
                  affected_systems (optional), submitted_by (optional).

        Returns:
            The created FER in PENDING status.

        Raises:
            FERValidationError: If required fields are missing or invalid.
        """
        self._validate_submission(data)

        fer = FER(
            fer_id="FER-000",
            title=data["title"].strip(),
            business_justification=data["business_justification"].strip(),
            priority=data["priority"].upper(),
            affected_systems=data.get("affected_systems", []),
            submitted_by=data.get("submitted_by", "human"),
        )
        fer.history.append(self._event("submitted", details={"priority": fer.priority}))

        persisted = self._store.create_fer(fer.to_dict())
        fer = FER.from_dict(persisted)
        fer_id = fer.fer_id
        self._store.append_audit({
            "action": "fer_submitted",
            "fer_id": fer_id,
            "title": fer.title,
            "priority": fer.priority,
        })
        self._store.commit_state(f"sentinel: FER submitted — {fer_id}")

        log.info("FER submitted: %s — %s", fer_id, fer.title)
        return fer

    # ------------------------------------------------------------------
    # Human triage gate
    # ------------------------------------------------------------------

    def triage(
        self,
        fer_id: str,
        decision: str,
        reason: str = "",
        actor: str = "human",
    ) -> FER:
        """
        Apply a human triage decision to a PENDING FER.

        Args:
            fer_id: The FER to triage.
            decision: APPROVED | DEFERRED | REJECTED
            reason: Human-provided rationale (optional but encouraged).
            actor: Who made the decision (default: "human").

        Returns:
            The updated FER.

        Raises:
            FERValidationError: If the FER is not found.
            FERTransitionError: If the decision is invalid from current status.
        """
        decision = decision.upper()
        if decision not in _TRIAGE_DECISIONS:
            raise FERValidationError(
                f"Invalid triage decision '{decision}'. "
                f"Must be one of: {', '.join(sorted(_TRIAGE_DECISIONS))}"
            )

        fer = self._atomic_transition(
            fer_id,
            decision,
            f"sentinel: FER {fer_id} triaged — {decision}",
            "triaged",
            {"decision": decision, "reason": reason, "actor": actor},
        )
        self._store.append_audit({
            "action": "fer_triaged",
            "fer_id": fer_id,
            "decision": decision,
            "actor": actor,
            "reason": reason,
        })

        log.info("FER %s triaged: %s by %s", fer_id, decision, actor)
        return fer

    # ------------------------------------------------------------------
    # Feasibility stage
    # ------------------------------------------------------------------

    def start_feasibility(self, fer_id: str) -> FER:
        """Advance FER from APPROVED to IN_FEASIBILITY."""
        return self._atomic_transition(
            fer_id,
            "IN_FEASIBILITY",
            f"sentinel: FER {fer_id} — feasibility assessment started",
            "feasibility_started",
        )

    def record_feasibility_approved(
        self,
        fer_id: str,
        selected_approach: str,
        feasibility_verdict: str,
        actor: str = "human",
    ) -> FER:
        """
        Record human approval of the feasibility assessment with a selected
        implementation approach. Advances FER to FEASIBILITY_APPROVED.

        Args:
            fer_id: The FER being approved.
            selected_approach: The approach_name selected from the Layer 7a options.
            feasibility_verdict: The verdict from Layer 7a (FEASIBLE, etc.).
            actor: Who approved (default: "human").
        """
        def _mutate(fer: FER) -> None:
            fer.selected_approach = selected_approach
            fer.feasibility_verdict = feasibility_verdict

        fer = self._atomic_transition(
            fer_id,
            "FEASIBILITY_APPROVED",
            f"sentinel: FER {fer_id} — feasibility approved, approach: {selected_approach}",
            "feasibility_approved",
            {
                "selected_approach": selected_approach,
                "feasibility_verdict": feasibility_verdict,
                "actor": actor,
            },
            mutate=_mutate,
        )
        log.info(
            "FER %s feasibility approved — approach: %s",
            fer_id,
            selected_approach,
        )
        return fer

    # ------------------------------------------------------------------
    # Spec stage
    # ------------------------------------------------------------------

    def start_spec(self, fer_id: str) -> FER:
        """Advance FER from FEASIBILITY_APPROVED to IN_SPEC."""
        return self._atomic_transition(
            fer_id,
            "IN_SPEC",
            f"sentinel: FER {fer_id} — spec generation started",
            "spec_started",
        )

    def record_spec_approved(
        self,
        fer_id: str,
        spec_version: str,
        task_ids: list[str],
        actor: str = "human",
    ) -> FER:
        """
        Record human approval of the enhancement spec and the resulting
        task IDs generated from the spec acceptance criteria contract.

        The active contract is that each generated task (steps 1..N) carries
        the original approved spec acceptance criteria payload rather than
        synthetic placeholder criteria for later steps.

        Advances FER to SPEC_APPROVED.
        """
        def _mutate(fer: FER) -> None:
            fer.spec_version = spec_version
            fer.task_ids = task_ids

        fer = self._atomic_transition(
            fer_id,
            "SPEC_APPROVED",
            f"sentinel: FER {fer_id} — spec approved, {len(task_ids)} task(s) generated",
            "spec_approved",
            {"spec_version": spec_version, "task_ids": task_ids, "actor": actor},
            mutate=_mutate,
        )
        log.info(
            "FER %s spec approved — %d task(s) generated",
            fer_id,
            len(task_ids),
        )
        return fer

    def revise(self, fer_id: str, feedback: str, actor: str = "human") -> FER:
        """
        Request a spec revision for a ``SPEC_APPROVED`` FER.

        Performs two durable mutations as one atomic unit of work under the FER
        transition lock (FIX-BLOCKING-009/011):
          1. Transition the FER ``SPEC_APPROVED -> APPROVED`` via a dedicated
             revision transition (NOT triage), recording the feedback in history.
          2. Remove the now-stale spec for this FER from the specs store.

        Both writes happen inside the transition lock and are committed only
        after both succeed. If a write fails, only the two touched files
        (``fers`` and ``specs``) are restored from a byte snapshot — no
        whole-collection ``store.write`` snapshot is used — so no partial
        durable state survives. If rollback itself also fails, the original
        failure is preserved as the primary cause (FIX-REQUIRED-004).

        Raises:
            FERValidationError: If the FER is not found.
            FERTransitionError: If the FER is not in a revisable source state.
            StateStoreError: If a store write fails (state rolled back).
        """
        fers_path = self._store._path("fers")
        specs_path = self._store._path("specs")
        lock_path = fers_path.with_suffix(f"{fers_path.suffix}.transition.lock")
        lock_fd = self._acquire_transition_lock(lock_path)

        fers_snapshot = fers_path.read_bytes() if fers_path.exists() else None
        specs_snapshot = specs_path.read_bytes() if specs_path.exists() else None

        try:
            records = self._store.read("fers")
            if not isinstance(records, list):
                raise FERValidationError(
                    "FER state store is invalid (expected a JSON list)."
                )
            match_index = next(
                (
                    idx
                    for idx, record in enumerate(records)
                    if record.get("fer_id") == fer_id
                ),
                None,
            )
            if match_index is None:
                raise FERValidationError(f"FER '{fer_id}' not found.")

            fer = FER.from_dict(records[match_index])
            # Dedicated revise transition — raises FERTransitionError for an
            # unsupported source state (FIX-BLOCKING-011 AC4).
            self._transition(fer, "APPROVED")
            fer.history.append(
                self._event(
                    "spec_revision_requested",
                    details={"feedback": feedback, "actor": actor},
                )
            )
            records[match_index] = fer.to_dict()

            specs = self._store.read("specs")
            revised_specs = [
                s for s in (specs if isinstance(specs, list) else [])
                if s.get("fer_id") != fer_id
            ]

            try:
                self._store.write("fers", records, commit=False)
                self._store.write("specs", revised_specs, commit=False)
                self._store.commit_state(
                    f"sentinel: FER {fer_id} — spec revision requested"
                )
            except Exception as write_exc:
                # Restore only the two touched files; preserve the original
                # write failure even if the compensating rollback also fails.
                try:
                    self._store._restore_file(fers_path, fers_snapshot)
                    self._store._restore_file(specs_path, specs_snapshot)
                except Exception as rollback_exc:
                    log.error(
                        "FER %s revise failed AND rollback failed "
                        "(original=%s; rollback=%s)",
                        fer_id, write_exc, rollback_exc,
                    )
                    raise write_exc from rollback_exc
                log.error(
                    "FER %s revise failed; rolled back to pre-call state: %s",
                    fer_id, write_exc,
                )
                raise
        finally:
            self._release_transition_lock(lock_path, lock_fd)

        log.info("FER %s spec revision requested by %s", fer_id, actor)
        return fer

    # ------------------------------------------------------------------
    # Implementation tracking
    # ------------------------------------------------------------------

    def mark_in_progress(self, fer_id: str) -> FER:
        """Advance FER to IN_PROGRESS once Phase 2 tasks begin executing."""
        return self._atomic_transition(
            fer_id,
            "IN_PROGRESS",
            f"sentinel: FER {fer_id} — implementation started",
            "implementation_started",
        )

    def mark_complete(self, fer_id: str) -> FER:
        """Mark FER COMPLETE once all associated tasks are HUMAN_MERGED."""
        fer = self._atomic_transition(
            fer_id,
            "COMPLETE",
            f"sentinel: FER {fer_id} — complete",
            "completed",
        )
        log.info("FER %s complete", fer_id)
        return fer

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, fer_id: str) -> FER:
        """Return a FER by ID. Raises FERValidationError if not found."""
        return self._load(fer_id)

    def list_all(self) -> list[FER]:
        """Return all FERs."""
        return [FER.from_dict(d) for d in self._store.read("fers")]

    def list_by_status(self, status: str) -> list[FER]:
        """Return all FERs in the given status."""
        return [f for f in self.list_all() if f.status == status.upper()]

    def build_feasibility_context(self, fer: FER) -> str:
        """
        Build the context string for a Layer 7a feasibility assessment call.

        Includes the FER details, codebase manifest (from Phase 1 state),
        and module inventory.
        """
        manifest = self._store.read("repositories")
        modules = self._store.read("modules")
        dependencies = self._store.read("dependencies")

        return (
            f"# Feature Enhancement Request — {fer.fer_id}\n\n"
            f"**Title:** {fer.title}\n\n"
            f"**Priority:** {fer.priority}\n\n"
            f"**Business Justification:**\n{fer.business_justification}\n\n"
            f"**Affected Systems (as stated by requestor):**\n"
            + "\n".join(f"  - {s}" for s in fer.affected_systems)
            + f"\n\n---\n\n"
            f"## Codebase Context (from Phase 1 Assessment)\n\n"
            f"### Repositories\n```json\n{_json_summary(manifest)}\n```\n\n"
            f"### Module Inventory ({len(modules)} modules)\n```json\n{_json_summary(modules[:40])}\n```\n\n"
            f"### Key Dependencies\n```json\n{_json_summary(dependencies[:30])}\n```\n\n"
            f"---\n\n"
            f"Perform a complete feasibility assessment for this FER per the Layer 7a skill instructions."
        )

    def build_spec_context(self, fer: FER, feasibility: dict[str, Any]) -> str:
        """
        Build the context string for a Layer 7b spec generation call.

        Includes the FER, feasibility report, and the selected approach.
        """
        return (
            f"# Enhancement Specification — {fer.fer_id}\n\n"
            f"## Feature Enhancement Request\n\n"
            f"**Title:** {fer.title}\n\n"
            f"**Priority:** {fer.priority}\n\n"
            f"**Business Justification:**\n{fer.business_justification}\n\n"
            f"**Affected Systems:** {', '.join(fer.affected_systems)}\n\n"
            f"---\n\n"
            f"## Feasibility Assessment\n\n"
            f"**Verdict:** {feasibility.get('feasibility_verdict', 'FEASIBLE')}\n\n"
            f"**Rationale:** {feasibility.get('feasibility_rationale', '')}\n\n"
            f"**Selected Approach:** {fer.selected_approach}\n\n"
            f"**Effort Estimate:** {feasibility.get('total_effort_estimate', 'M')}\n\n"
            f"**Risk Level:** {feasibility.get('risk_level', 'MEDIUM')}\n\n"
            f"**Options Analysed:**\n"
            + "\n".join(
                f"  - {o.get('approach_name', '')}: {o.get('description', '')}"
                for o in feasibility.get("options", [])
            )
            + f"\n\n**Not in Scope:**\n"
            + "\n".join(f"  - {item}" for item in feasibility.get("not_in_scope", []))
            + "\n\n---\n\n"
            f"Write the complete implementation specification for this FER, "
            f"using the '{fer.selected_approach}' implementation approach."
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self, fer_id: str) -> FER:
        """Load a FER by ID or raise FERValidationError."""
        records = self._store.read("fers")
        for record in records:
            if record.get("fer_id") == fer_id:
                return FER.from_dict(record)
        raise FERValidationError(f"FER '{fer_id}' not found.")

    def _transition(self, fer: FER, new_status: str) -> None:
        """Apply a status transition or raise FERTransitionError."""
        allowed = _VALID_TRANSITIONS.get(fer.status, frozenset())
        if new_status not in allowed:
            raise FERTransitionError(
                f"Cannot transition FER {fer.fer_id} from {fer.status!r} to {new_status!r}. "
                f"Valid transitions from {fer.status!r}: "
                f"{', '.join(sorted(allowed)) or '(none)'}"
            )
        fer.status = new_status

    def _atomic_transition(
        self,
        fer_id: str,
        new_status: str,
        commit_message: str,
        history_event: str,
        history_details: dict[str, Any] | None = None,
        *,
        mutate: Callable[[FER], None] | None = None,
    ) -> FER:
        """Apply one FER status transition under a shared serialized lock."""

        fers_path = self._store._path("fers")
        lock_path = fers_path.with_suffix(f"{fers_path.suffix}.transition.lock")
        lock_fd = self._acquire_transition_lock(lock_path)

        try:
            records = self._store.read("fers")
            if not isinstance(records, list):
                raise FERValidationError(
                    "FER state store is invalid (expected a JSON list)."
                )

            match_index = next(
                (
                    idx
                    for idx, record in enumerate(records)
                    if record.get("fer_id") == fer_id
                ),
                None,
            )
            if match_index is None:
                raise FERValidationError(f"FER '{fer_id}' not found.")

            fer = FER.from_dict(records[match_index])
            self._transition(fer, new_status)
            if mutate is not None:
                mutate(fer)
            fer.history.append(self._event(history_event, details=history_details))
            records[match_index] = fer.to_dict()
            self._store.write("fers", records, commit=False)
            # Commit the durable state INSIDE the lock so no concurrent
            # transition can interleave between the write and the commit, and a
            # commit failure surfaces as a failed transition rather than a
            # falsely-reported success (FIX-BLOCKING-010 / FIX-REQUIRED-003).
            self._store.commit_state(commit_message)
        finally:
            self._release_transition_lock(lock_path, lock_fd)

        return fer

    def _acquire_transition_lock(self, lock_path: Path) -> int:
        """Acquire an interprocess lock for FER lifecycle transitions."""

        deadline = time.monotonic() + _FER_TRANSITION_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except (FileExistsError, PermissionError):
                # Reclaim an abandoned lock: if the existing lock file is older
                # than the stale threshold, the previous holder almost certainly
                # crashed without releasing it. Unlink once and retry so a crash
                # can't wedge all future FER transitions.
                if self._reclaim_stale_transition_lock(lock_path):
                    try:
                        return os.open(
                            str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR
                        )
                    except (FileExistsError, PermissionError):
                        pass  # Lost the race — fall through to poll.
                if time.monotonic() >= deadline:
                    raise FERTransitionConflictError(
                        "Timed out waiting for FER transition lock. "
                        "Retry the transition request."
                    )
                time.sleep(_FER_TRANSITION_LOCK_POLL_SECONDS)
            except OSError as exc:
                raise FERTransitionConflictError(
                    f"Failed to acquire FER transition lock: {exc}"
                ) from exc

    @staticmethod
    def _reclaim_stale_transition_lock(lock_path: Path) -> bool:
        """Unlink ``lock_path`` if it is older than the stale threshold.

        Returns True if a stale lock was removed (caller should retry), False
        otherwise. Any error inspecting/removing the lock is treated as
        "not stale" so a live lock is never disturbed.
        """
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            return False
        if age < _FER_TRANSITION_LOCK_STALE_SECONDS:
            return False
        try:
            os.unlink(str(lock_path))
        except OSError:
            return False
        log.warning(
            "Reclaimed stale FER transition lock %s (age %.0fs); previous "
            "holder likely crashed without releasing it.",
            lock_path.name,
            age,
        )
        return True

    def _release_transition_lock(self, lock_path: Path, lock_fd: int) -> None:
        """Release the FER transition lock acquired by _acquire_transition_lock."""

        try:
            os.close(lock_fd)
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                return
            except OSError as exc:
                # A leaked lock blocks ALL future FER transitions until it is
                # manually cleared — this is a critical inconsistency, not a
                # cosmetic warning. Surface it loudly and raise so the caller
                # knows the system is wedged rather than silently continuing.
                log.error(
                    "Failed to remove FER transition lock file %s: %s. "
                    "The lock is leaked and will block future FER transitions.",
                    lock_path,
                    exc,
                )
                raise RuntimeError(
                    "FER transition lock could not be released"
                ) from exc

    @staticmethod
    def _event(event_type: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        """Build a history event dict."""
        entry: dict[str, Any] = {
            "event": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if details:
            entry["details"] = details
        return entry

    @staticmethod
    def _validate_submission(data: dict[str, Any]) -> None:
        """Validate a FER submission dict. Raises FERValidationError on failure."""
        errors: list[str] = []

        # Required string fields must actually be strings. A JSON null or a
        # non-string (e.g. an int) would otherwise crash .strip()/.upper()
        # with an AttributeError instead of producing a clean validation error.
        title = data.get("title", "")
        if not isinstance(title, str):
            errors.append("'title' must be a string.")
            title = ""
        if not title.strip():
            errors.append("'title' is required and must not be blank.")
        elif len(title.strip()) > 200:
            errors.append("'title' must not exceed 200 characters.")

        justification = data.get("business_justification", "")
        if not isinstance(justification, str):
            errors.append("'business_justification' must be a string.")
            justification = ""
        if not justification.strip():
            errors.append("'business_justification' is required and must not be blank.")
        elif len(justification.strip()) > 2000:
            errors.append("'business_justification' must not exceed 2000 characters.")

        raw_priority = data.get("priority", "")
        priority = raw_priority.upper() if isinstance(raw_priority, str) else ""
        if priority not in _PRIORITY_VALUES:
            errors.append(
                f"'priority' must be one of: {', '.join(sorted(_PRIORITY_VALUES))}. "
                f"Got: {data.get('priority', '(missing)')!r}"
            )

        if errors:
            raise FERValidationError(
                f"FER validation failed ({len(errors)} error(s)):\n"
                + "\n".join(f"  - {e}" for e in errors)
            )


def _json_summary(data: Any, max_chars: int = 2000) -> str:
    """Return a truncated JSON string for context assembly."""
    import json
    text = json.dumps(data, indent=2)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n  ... (truncated)"
    return text
