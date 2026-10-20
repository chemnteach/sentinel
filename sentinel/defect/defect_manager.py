"""
sentinel/defect/defect_manager.py

Defect / Bug Report manager for Sentinel.

Handles defect intake, schema validation, lifecycle state transitions,
locking, and human triage. Persists to sentinel/state/defects.json.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from sentinel.defect.models import (
    DefectReport,
    DefectSeverity,
    DefectStatus,
)
from sentinel.orchestrator.state_store import StateStore

log = logging.getLogger(__name__)

_TRIAGE_DECISIONS = frozenset({"APPROVED", "DEFERRED", "REJECTED"})

_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    DefectStatus.PENDING: frozenset({
        DefectStatus.APPROVED,
        DefectStatus.DEFERRED,
        DefectStatus.REJECTED,
    }),
    DefectStatus.APPROVED: frozenset({
        DefectStatus.IN_RCA,
        DefectStatus.DEFERRED,
    }),
    DefectStatus.DEFERRED: frozenset({
        DefectStatus.APPROVED,
        DefectStatus.REJECTED,
    }),
    DefectStatus.REJECTED: frozenset(),
    DefectStatus.IN_RCA: frozenset({
        DefectStatus.RCA_COMPLETE,
        DefectStatus.APPROVED,
    }),
    DefectStatus.RCA_COMPLETE: frozenset({
        DefectStatus.DIAGNOSED_REPORT_READY,
        DefectStatus.IN_SPEC,
        DefectStatus.IN_RCA,
        DefectStatus.DEFERRED,
    }),
    DefectStatus.DIAGNOSED_REPORT_READY: frozenset({
        DefectStatus.IN_SPEC,
        DefectStatus.IN_RCA,
        DefectStatus.COMPLETE,
        DefectStatus.DEFERRED,
    }),
    DefectStatus.IN_SPEC: frozenset({
        DefectStatus.SPEC_APPROVED,
        DefectStatus.RCA_COMPLETE,
        DefectStatus.DIAGNOSED_REPORT_READY,
    }),
    DefectStatus.SPEC_APPROVED: frozenset({
        DefectStatus.IN_PROGRESS,
        DefectStatus.IN_SPEC,
        DefectStatus.APPROVED,
    }),
    DefectStatus.IN_PROGRESS: frozenset({
        DefectStatus.COMPLETE,
    }),
    DefectStatus.COMPLETE: frozenset(),
}

_DEFECT_TRANSITION_LOCK_FILE = "defects.transition.lock"
_DEFECT_TRANSITION_LOCK_POLL_SECONDS = 0.005
_DEFECT_TRANSITION_LOCK_TIMEOUT_SECONDS = 10.0
_DEFECT_TRANSITION_LOCK_STALE_SECONDS = 60.0


class DefectValidationError(ValueError):
    """Raised when a defect submission or triage decision fails validation."""


class DefectTransitionError(ValueError):
    """Raised when an invalid status transition is attempted."""


class DefectTransitionConflictError(DefectTransitionError):
    """Raised when a concurrent transition conflict occurs."""


class DefectManager:
    """
    Manages the complete Defect lifecycle from submission to RCA/Spec and remediation.
    """

    def __init__(self, store: StateStore) -> None:
        self._store = store

    def submit(self, data: dict[str, Any]) -> DefectReport:
        """
        Submit a new defect report.
        """
        self._validate_submission(data)

        defect = DefectReport(
            defect_id="DEF-000",
            title=data["title"].strip(),
            description=data["description"].strip(),
            symptoms=data["symptoms"].strip(),
            stack_trace=data.get("stack_trace", "").strip(),
            reproduction_steps=data.get("reproduction_steps", "").strip(),
            suspected_files=data.get("suspected_files", []),
            severity=data.get("severity", DefectSeverity.HIGH).upper(),
            submitted_by=data.get("submitted_by", "human"),
        )
        defect.history.append(self._event("submitted", details={"severity": defect.severity}))

        persisted = self._store.create_defect(defect.to_dict())
        defect = DefectReport.from_dict(persisted)
        defect_id = defect.defect_id
        self._store.append_audit({
            "action": "defect_submitted",
            "defect_id": defect_id,
            "title": defect.title,
            "severity": defect.severity,
        })
        self._store.commit_state(f"sentinel: Defect submitted — {defect_id}")
        return defect

    def triage(self, defect_id: str, decision: str, reason: str = "") -> DefectReport:
        """
        Record a human triage decision (APPROVED, DEFERRED, REJECTED).
        """
        dec = decision.upper()
        if dec not in _TRIAGE_DECISIONS:
            raise DefectValidationError(
                f"Invalid triage decision '{decision}'. Must be one of: {', '.join(sorted(_TRIAGE_DECISIONS))}"
            )

        return self.transition(
            defect_id=defect_id,
            to_status=dec,
            reason=reason or f"Triage decision: {dec}",
            event_type=f"triaged_{dec.lower()}",
            commit_message=f"sentinel: Defect {defect_id} triaged as {dec}",
        )

    def transition(
        self,
        defect_id: str,
        to_status: str,
        reason: str = "",
        event_type: str = "transition",
        details: dict[str, Any] | None = None,
        commit_message: str | None = None,
    ) -> DefectReport:
        """
        Atomically transition a defect to a new status.
        """
        lock_path = self._store._dir / _DEFECT_TRANSITION_LOCK_FILE
        lock_fd = self._acquire_transition_lock(lock_path)
        try:
            defects_raw = self._store.read("defects")
            if not isinstance(defects_raw, list):
                defects_raw = []

            found_idx = -1
            target_data: dict[str, Any] | None = None
            for i, d in enumerate(defects_raw):
                if d.get("defect_id") == defect_id:
                    found_idx = i
                    target_data = d
                    break

            if target_data is None or found_idx == -1:
                raise DefectValidationError(f"Defect '{defect_id}' not found in state.")

            current_status = target_data.get("status", DefectStatus.PENDING)
            allowed = _VALID_TRANSITIONS.get(current_status, frozenset())
            if to_status not in allowed:
                raise DefectTransitionError(
                    f"Cannot transition Defect '{defect_id}' from '{current_status}' to '{to_status}'. "
                    f"Allowed: {', '.join(sorted(allowed)) if allowed else 'none'}"
                )

            defect = DefectReport.from_dict(target_data)
            defect.status = to_status
            ev_details = {"from": current_status, "to": to_status}
            if details:
                ev_details.update(details)
            defect.history.append(self._event(event_type, reason=reason, details=ev_details))

            defects_raw[found_idx] = defect.to_dict()
            self._store.write("defects", defects_raw, commit=False)

            self._store.append_audit({
                "action": "defect_transition",
                "defect_id": defect_id,
                "from_status": current_status,
                "to_status": to_status,
                "reason": reason,
            })
            if commit_message:
                self._store.commit_state(commit_message)

            return defect
        finally:
            self._release_transition_lock(lock_path, lock_fd)

    def get(self, defect_id: str) -> DefectReport:
        """
        Get a defect by ID.
        """
        defects = self.list_defects()
        for d in defects:
            if d.defect_id == defect_id:
                return d
        raise DefectValidationError(f"Defect '{defect_id}' not found.")

    def list_defects(self, status: str | None = None) -> list[DefectReport]:
        """
        List defects, optionally filtered by status.
        """
        raw = self._store.read("defects")
        if not isinstance(raw, list):
            return []
        defects = [DefectReport.from_dict(d) for d in raw]
        if status is not None:
            st = status.upper()
            defects = [d for d in defects if d.status == st]
        return defects

    def update_paths(self, defect_id: str, report_path: str = "", patch_path: str = "") -> DefectReport:
        """
        Update report_path and patch_path for a defect.
        """
        defects_raw = self._store.read("defects")
        if not isinstance(defects_raw, list):
            defects_raw = []

        found_idx = -1
        target: dict[str, Any] | None = None
        for i, d in enumerate(defects_raw):
            if d.get("defect_id") == defect_id:
                found_idx = i
                target = d
                break

        if target is None:
            raise DefectValidationError(f"Defect '{defect_id}' not found.")

        if report_path:
            target["report_path"] = report_path
        if patch_path:
            target["patch_path"] = patch_path

        defects_raw[found_idx] = target
        self._store.write("defects", defects_raw, commit=False)
        return DefectReport.from_dict(target)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_submission(self, data: dict[str, Any]) -> None:
        for f in ("title", "description", "symptoms"):
            val = data.get(f)
            if not isinstance(val, str) or not val.strip():
                raise DefectValidationError(f"Field '{f}' is required and must be a non-empty string.")

        sev = data.get("severity", DefectSeverity.HIGH)
        if not isinstance(sev, str) or sev.upper() not in DefectSeverity.ALL:
            raise DefectValidationError(
                f"Invalid severity '{sev}'. Must be one of: {', '.join(sorted(DefectSeverity.ALL))}"
            )

    def _event(self, event_type: str, reason: str = "", details: dict[str, Any] | None = None) -> dict[str, Any]:
        ev = {
            "event": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if reason:
            ev["reason"] = reason
        if details:
            ev["details"] = details
        return ev

    def _acquire_transition_lock(self, lock_path: Any) -> int:
        deadline = time.monotonic() + _DEFECT_TRANSITION_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except (FileExistsError, PermissionError):
                try:
                    mtime = os.path.getmtime(str(lock_path))
                    if time.time() - mtime > _DEFECT_TRANSITION_LOCK_STALE_SECONDS:
                        try:
                            os.unlink(str(lock_path))
                        except OSError:
                            pass
                except OSError:
                    pass

                if time.monotonic() >= deadline:
                    raise DefectTransitionConflictError("Timed out waiting for defect transition lock.")
                time.sleep(_DEFECT_TRANSITION_LOCK_POLL_SECONDS)
            except OSError as exc:
                raise DefectTransitionError(f"Failed to acquire defect transition lock: {exc}") from exc

    def _release_transition_lock(self, lock_path: Any, lock_fd: int) -> None:
        try:
            os.close(lock_fd)
        finally:
            try:
                lock_path.unlink()
            except (FileNotFoundError, OSError):
                pass
