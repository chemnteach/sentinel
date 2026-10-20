"""
sentinel/assess/finding_status.py

Phase 1d — persistent per-group status tracking (Plan Section 8).

``finding_status.json`` carries one record per semantic group, tracking that
group's remediation status across sessions (UNTRIAGED -> APPROVED -> QUEUED ->
... -> COMPLETE / ESCALATED). This is the resumable-workflow backbone: a user
can triage, run a batch, leave, and return to see exactly where every group
stands.

The store is self-contained (like ``grouper_view``): it reads/writes
``finding_status.json`` in the state dir and bootstraps itself from
``semantic_groups.json`` + ``tech_debt.json`` on first use, inheriting any
existing ``human_triage`` decisions.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sentinel.assess.grouper_view import generate_grouped_triage_view

log = logging.getLogger(__name__)

STATUS_FILENAME = "finding_status.json"


class StatusValue:
    """The lifecycle statuses a finding group can carry (Plan Section 8.1)."""

    UNTRIAGED = "UNTRIAGED"
    DEFERRED = "DEFERRED"
    EXCLUDE = "EXCLUDE"
    APPROVED = "APPROVED"
    APPROVED_WITH_CONDITIONS = "APPROVED_WITH_CONDITIONS"
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    RETRYING = "RETRYING"
    COMPLETE = "COMPLETE"
    REMEDIATED = "REMEDIATED"
    ESCALATED = "ESCALATED"
    HUMAN_FIXED = "HUMAN_FIXED"


class BundleStatus:
    """Lifecycle statuses a remediation bundle can carry (S2-2).

    Distinct from ``StatusValue`` (per-finding-group triage lifecycle): a
    bundle tracks the root-cause-driven remediation of a cluster of findings
    through review and merge, independent of individual group triage.
    """

    UNDECIDED = "UNDECIDED"
    APPROVED = "APPROVED"
    IN_PROGRESS = "IN_PROGRESS"
    IN_REVIEW = "IN_REVIEW"
    COMPLETE = "COMPLETE"
    REVERTED = "REVERTED"


# Stable display/iteration order for summaries.
_STATUS_ORDER: tuple[str, ...] = (
    StatusValue.UNTRIAGED,
    StatusValue.APPROVED,
    StatusValue.APPROVED_WITH_CONDITIONS,
    StatusValue.QUEUED,
    StatusValue.IN_PROGRESS,
    StatusValue.RETRYING,
    StatusValue.COMPLETE,
    StatusValue.REMEDIATED,
    StatusValue.ESCALATED,
    StatusValue.HUMAN_FIXED,
    StatusValue.DEFERRED,
    StatusValue.EXCLUDE,
)

# human_triage value -> (status, approval_type). approval_type is only set for
# the two APPROVE variants (Plan Section 8.3).
_TRIAGE_TO_STATUS: dict[str, tuple[str, str | None]] = {
    "APPROVE": (StatusValue.APPROVED, "APPROVE"),
    "APPROVE_WITH_CONDITIONS": (StatusValue.APPROVED_WITH_CONDITIONS, "APPROVE_WITH_CONDITIONS"),
    "DEFER": (StatusValue.DEFERRED, None),
    "EXCLUDE": (StatusValue.EXCLUDE, None),
}


@dataclass
class FindingStatus:
    """Persistent status record for one semantic group."""

    group_id: str
    canonical_finding_id: str
    member_finding_ids: list[str]
    severity: str
    status: str
    approval_type: str | None
    conditions: str
    task_id: str | None
    batch_id: str | None
    status_updated_at: str
    status_updated_by: str
    notes: str = ""


def _now_iso() -> str:
    """UTC timestamp, ISO 8601 with trailing ``Z`` (no microseconds)."""
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _status_from_triage(human_triage: str | None) -> tuple[str, str | None]:
    """Map a tech_debt ``human_triage`` value to a (status, approval_type)."""
    if not human_triage:
        return (StatusValue.UNTRIAGED, None)
    return _TRIAGE_TO_STATUS.get(str(human_triage), (StatusValue.UNTRIAGED, None))


_FIELD_NAMES = frozenset(f.name for f in fields(FindingStatus))


class StatusManager:
    """Load, query, and mutate per-group status; persists atomically."""

    def __init__(
        self,
        state_dir: str | Path,
        semantic_groups_path: str | Path,
        tech_debt_path: str | Path,
    ) -> None:
        """Load ``finding_status.json`` from ``state_dir``. If it does not
        exist, bootstrap it from ``semantic_groups.json`` + ``tech_debt.json``
        — one entry per group, inheriting existing ``human_triage`` values
        (UNTRIAGED when a group has no decision yet)."""
        self._path = Path(state_dir) / STATUS_FILENAME
        self._groups_path = Path(semantic_groups_path)
        self._tech_debt_path = Path(tech_debt_path)

        if self._path.exists():
            self._records = self._load()
        else:
            self._records = self._bootstrap_records()
            self._save()
        self._reindex()

    # -- construction helpers -----------------------------------------

    def _load(self) -> list[FindingStatus]:
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(f"{self._path}: expected a JSON array")
        records: list[FindingStatus] = []
        for r in raw:
            # A single malformed record (missing a required field / wrong type)
            # must not abort the whole load and lose every well-formed record.
            try:
                records.append(
                    FindingStatus(**{k: v for k, v in r.items() if k in _FIELD_NAMES})
                )
            except (TypeError, KeyError, AttributeError) as e:
                log.warning("Skipping malformed finding_status record: %s", e)
                continue
        return records

    def _bootstrap_records(self) -> list[FindingStatus]:
        """One record per semantic group, status derived from the group's
        inherited ``human_triage`` (via the grouped triage view)."""
        view = generate_grouped_triage_view(self._tech_debt_path, self._groups_path)
        now = _now_iso()
        records: list[FindingStatus] = []
        for e in view:
            status, approval_type = _status_from_triage(e.get("human_triage"))
            records.append(
                FindingStatus(
                    group_id=e.get("group_id"),
                    canonical_finding_id=e.get("canonical_finding_id"),
                    member_finding_ids=list(e.get("member_finding_ids") or []),
                    severity=e.get("severity"),
                    status=status,
                    approval_type=approval_type,
                    conditions=e.get("conditions") or "",
                    task_id=None,
                    batch_id=None,
                    status_updated_at=now,
                    status_updated_by="sentinel",
                    notes="",
                )
            )
        return records

    def _reindex(self) -> None:
        self._by_id = {r.group_id: r for r in self._records}

    def _save(self) -> None:
        _atomic_write_json(self._path, [asdict(r) for r in self._records])

    # -- queries ------------------------------------------------------

    def get(self, group_id: str) -> FindingStatus | None:
        """Get the status record for a group, or None if unknown."""
        return self._by_id.get(group_id)

    def get_by_status(self, status: str) -> list[FindingStatus]:
        """All groups currently in ``status``."""
        return [r for r in self._records if r.status == status]

    def get_summary(self) -> dict:
        """Count of groups per status value, plus ``total`` (all statuses are
        present as keys, including zeros)."""
        summary: dict[str, int] = {s: 0 for s in _STATUS_ORDER}
        for r in self._records:
            summary[r.status] = summary.get(r.status, 0) + 1
        summary["total"] = len(self._records)
        return summary

    def get_resume_queue(self) -> list[FindingStatus]:
        """Groups approved for remediation but not yet advanced into a batch.

        Both APPROVED and APPROVED_WITH_CONDITIONS qualify; any group already
        moved on (QUEUED / IN_PROGRESS / RETRYING / COMPLETE / ESCALATED /
        HUMAN_FIXED) is excluded. Since each group carries a single status,
        this is exactly the set still in one of the two approved states."""
        return [
            r
            for r in self._records
            if r.status in (StatusValue.APPROVED, StatusValue.APPROVED_WITH_CONDITIONS)
        ]

    # -- mutations ----------------------------------------------------

    def update(
        self,
        group_id: str,
        status: str,
        updated_by: str = "sentinel",
        **kwargs,
    ) -> None:
        """Update a group's status (and any other FindingStatus fields passed
        as kwargs). Persists atomically. Raises ValueError for an unknown
        group_id."""
        record = self._by_id.get(group_id)
        if record is None:
            raise ValueError(f"group_id {group_id!r} not found in finding_status")
        self._apply(record, status, updated_by, kwargs)
        self._save()

    def bulk_update(
        self,
        group_ids: list[str],
        status: str,
        updated_by: str = "sentinel",
        **kwargs,
    ) -> int:
        """Update multiple groups at once; persists once. Returns the count of
        groups actually updated (unknown ids are skipped).

        The in-memory records are mutated before the single ``_save()``. If that
        durable write fails, every mutated record is rolled back to its prior
        field state so in-memory and on-disk state do not silently diverge.
        """
        updated = 0
        # Snapshot the pre-mutation field state of each record we touch, so a
        # failed _save() can restore them (in-memory <-> disk consistency).
        snapshots: list[tuple[FindingStatus, dict[str, Any]]] = []
        for gid in group_ids:
            record = self._by_id.get(gid)
            if record is None:
                continue
            snapshots.append((record, asdict(record)))
            self._apply(record, status, updated_by, kwargs)
            updated += 1
        if updated:
            try:
                self._save()
            except Exception:
                for record, prior in snapshots:
                    for field_name, value in prior.items():
                        setattr(record, field_name, value)
                raise
        return updated

    def bootstrap_from_triage(self, tech_debt_path: str | Path) -> int:
        """Sync each group's status from the current ``human_triage`` values in
        ``tech_debt.json`` — used when finding_status.json predates a triage
        pass (e.g. run-012's already-triaged state). Maps APPROVE->APPROVED,
        APPROVE_WITH_CONDITIONS->APPROVED_WITH_CONDITIONS, DEFER->DEFERRED,
        EXCLUDE->EXCLUDE, None->UNTRIAGED. Returns the number of groups that
        picked up a real (non-None) decision."""
        tech_debt = json.loads(Path(tech_debt_path).read_text(encoding="utf-8"))
        if not isinstance(tech_debt, list):
            raise ValueError(
                f"{tech_debt_path}: expected a JSON array of tech-debt items, "
                f"got {type(tech_debt).__name__}"
            )
        triage_by_fid: dict[str, str | None] = {}
        conditions_by_fid: dict[str, str] = {}
        for item in tech_debt:
            fid = str(item.get("finding_id") or "")
            if fid:
                triage_by_fid[fid] = item.get("human_triage")
                conditions_by_fid[fid] = item.get("conditions") or ""

        now = _now_iso()
        bootstrapped = 0
        for record in self._records:
            triage = triage_by_fid.get(record.canonical_finding_id)
            fid_used = record.canonical_finding_id
            if not triage:
                # Fall back to any member that carries a decision.
                for mfid in record.member_finding_ids:
                    if triage_by_fid.get(mfid):
                        triage = triage_by_fid[mfid]
                        fid_used = mfid
                        break
            status, approval_type = _status_from_triage(triage)
            record.status = status
            record.approval_type = approval_type
            if approval_type == "APPROVE_WITH_CONDITIONS":
                record.conditions = conditions_by_fid.get(fid_used, record.conditions)
            record.status_updated_at = now
            record.status_updated_by = "sentinel"
            if triage:
                bootstrapped += 1
        self._save()
        return bootstrapped

    # -- internal -----------------------------------------------------

    @staticmethod
    def _apply(record: FindingStatus, status: str, updated_by: str, kwargs: dict) -> None:
        record.status = status
        record.status_updated_by = updated_by
        record.status_updated_at = _now_iso()
        for key, value in kwargs.items():
            if key in _FIELD_NAMES and key not in ("group_id", "status", "status_updated_by", "status_updated_at"):
                setattr(record, key, value)


def _atomic_write_json(path: str | Path, data: Any) -> None:
    """Write JSON via temp file + atomic replace."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


__all__ = [
    "FindingStatus",
    "StatusValue",
    "BundleStatus",
    "StatusManager",
    "STATUS_FILENAME",
]
