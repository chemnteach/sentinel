"""
sentinel/assess/grouper_view.py

Group-aware triage view (Phase 1b).

Reads ``tech_debt.json`` + ``semantic_groups.json`` and produces ONE entry per
semantic group instead of one per finding, so a code owner triages distinct
root causes rather than near-duplicate findings. A triage decision applied to a
group propagates to every member finding in ``tech_debt.json`` — the
``tech_debt.json`` structure is unchanged (backward compatible, Plan Section 4.3
Option A: virtual grouping at display time).

Join key: a semantic group's ``member_finding_ids`` are finding ids
(``F2-...``), which map to tech_debt items via the item's ``finding_id`` field
(NOT the item's ``id`` / ``TD-...``).

This module does not wire into the CLI — that is a later Phase 1b step. It is a
pure read/apply pair over the two state files.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

# Canonical-emitter skill priority. The member whose skill ranks highest
# (lowest index) is the "canonical emitter"; it supplies the group entry's
# description, module/skill, and the inherited ``human_triage``.
# quick-review > correctness > security > error-handling > (everything else).
_SKILL_PRIORITY: tuple[str, ...] = (
    "quick-review",
    "correctness",
    "security",
    "error-handling",
)

# The four decision values the tech-debt register accepts (mirrors
# sentinel.govern.conditions.DECISION_TYPES).
_VALID_DECISIONS: frozenset[str] = frozenset(
    {"APPROVE", "APPROVE_WITH_CONDITIONS", "DEFER", "EXCLUDE"}
)

# Higher rank = more severe. Used when a merge picks the surviving group's
# severity (max of the two).
_SEVERITY_RANK: dict[str, int] = {"P3": 0, "P2": 1, "P1": 2, "Critical": 3}


def _skill_rank(skill: str | None) -> int:
    """Rank a skill by confidence priority (lower = higher confidence)."""
    s = (skill or "").lower()
    for i, key in enumerate(_SKILL_PRIORITY):
        if key in s:
            return i
    return len(_SKILL_PRIORITY)


def _load_json_list(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON array")
    return data


def _canonical_member(members: list[dict]) -> dict:
    """The canonical member of a group: highest-confidence skill, ties broken
    by ``finding_id`` for determinism."""
    return min(
        members,
        key=lambda m: (_skill_rank(m.get("skill")), str(m.get("finding_id") or "")),
    )


def generate_grouped_triage_view(
    tech_debt_path: str | Path,
    semantic_groups_path: str | Path,
) -> list[dict]:
    """Build a grouped triage view — one entry per semantic group, not one per
    finding.

    Each entry contains::

        {
            "group_id": "...",
            "canonical_finding_id": "...",
            "member_finding_ids": [...],
            "member_count": N,
            "severity": "P1",
            "description": "...",
            "module": "...",
            "skill": "...",
            "skills_corroborating": [...],
            "affected_files": [...],
            "human_triage": "DEFER"|"APPROVE"|"APPROVE_WITH_CONDITIONS"
                            |"EXCLUDE"|None,
            "conditions": "...",
            "rationale": "...",
            "effort_estimate": "...",
            "suggested_fix": "..."
        }

    Groups with a single member look identical to plain findings. Groups with
    multiple members show the canonical emitter's description plus a
    corroboration note.

    Canonical emitter = the member with the highest-confidence skill
    (quick-review > correctness > security > error-handling > others).

    ``human_triage`` is inherited from the canonical finding if already set,
    ``None`` if untriaged.

    Groups whose member findings are absent from ``tech_debt.json`` are skipped
    (there is nothing to triage for them).
    """
    tech_debt = _load_json_list(tech_debt_path)
    groups = _load_json_list(semantic_groups_path)

    by_finding_id: dict[str, dict] = {}
    for item in tech_debt:
        fid = item.get("finding_id")
        if fid:
            by_finding_id[str(fid)] = item

    view: list[dict] = []
    for group in groups:
        member_fids = [str(f) for f in (group.get("member_finding_ids") or [])]
        # Preserve group order; keep only members present in tech_debt.
        members = [by_finding_id[f] for f in member_fids if f in by_finding_id]
        if not members:
            continue

        canonical = _canonical_member(members)
        member_ids = [str(m.get("finding_id") or "") for m in members]
        member_count = len(members)

        skills_corroborating = sorted(
            {str(m.get("skill")) for m in members if m.get("skill")}
        )

        description = str(canonical.get("description") or "")
        if member_count > 1:
            description = (
                f"{description}\n\n"
                f"[Corroborated by {member_count} findings across "
                f"{len(skills_corroborating)} skill(s): "
                f"{', '.join(skills_corroborating)}]"
            )

        affected_files = sorted(
            {str(m.get("module")) for m in members if m.get("module")}
        )

        view.append(
            {
                "group_id": group.get("group_id"),
                "canonical_finding_id": canonical.get("finding_id"),
                "member_finding_ids": member_ids,
                "member_count": member_count,
                "severity": group.get("severity") or canonical.get("severity"),
                "description": description,
                "module": canonical.get("module"),
                "skill": canonical.get("skill"),
                "skills_corroborating": skills_corroborating,
                "affected_files": affected_files,
                # None (not "") when untriaged, so the UI can distinguish
                # "not yet decided" from a real decision.
                "human_triage": canonical.get("human_triage") or None,
                "conditions": canonical.get("conditions") or "",
                "rationale": canonical.get("rationale") or "",
                "effort_estimate": canonical.get("effort_estimate"),
                "suggested_fix": canonical.get("suggested_fix"),
            }
        )
    return view


def apply_group_triage_decision(
    group_id: str,
    decision: str,
    rationale: str,
    conditions: str,
    tech_debt_path: str | Path,
    semantic_groups_path: str | Path,
) -> int:
    """Apply a triage decision to ALL member findings in a group.

    ``decision``: ``APPROVE`` | ``APPROVE_WITH_CONDITIONS`` | ``DEFER`` |
    ``EXCLUDE``.

    Returns the count of findings updated.

    This is what makes group-level triage work — one decision propagates to all
    members so the Fix Planner sees them all approved/deferred correctly. For
    ``APPROVE_WITH_CONDITIONS`` the ``conditions`` string is written to every
    member both verbatim (``conditions``) and as a single-item
    ``conditions_structured`` ``APPROACH_DIRECTIVE`` (the shape the remediation
    pipeline reads). Any prior ``conditions_structured`` is cleared for the
    other three decisions.
    """
    if decision not in _VALID_DECISIONS:
        raise ValueError(
            f"invalid decision {decision!r}; expected one of "
            f"{sorted(_VALID_DECISIONS)}"
        )
    if decision == "APPROVE_WITH_CONDITIONS" and not (conditions and conditions.strip()):
        raise ValueError(
            "APPROVE_WITH_CONDITIONS requires a non-empty conditions string"
        )

    groups = _load_json_list(semantic_groups_path)
    group = next((g for g in groups if g.get("group_id") == group_id), None)
    if group is None:
        raise ValueError(f"group_id {group_id!r} not found in semantic groups")
    member_fids = {str(f) for f in (group.get("member_finding_ids") or [])}

    tech_debt_path = Path(tech_debt_path)
    conditions_structured: list[dict[str, Any]] | None = (
        [{"type": "APPROACH_DIRECTIVE", "target": None, "note": conditions}]
        if decision == "APPROVE_WITH_CONDITIONS"
        else None
    )

    # Serialize the full tech_debt.json read-modify-write under an exclusive
    # lock — acquired before loading and released only after the durable write —
    # so concurrent group-triage decisions cannot clobber each other
    # (FIX-BLOCKING-015).
    lock_path = tech_debt_path.with_suffix(f"{tech_debt_path.suffix}.triage.lock")
    lock_fd = _acquire_triage_lock(lock_path)
    try:
        tech_debt = _load_json_list(tech_debt_path)

        updated = 0
        for item in tech_debt:
            if str(item.get("finding_id")) in member_fids:
                item["human_triage"] = decision
                if rationale:
                    item["rationale"] = rationale
                if conditions:
                    item["conditions"] = conditions
                item["conditions_structured"] = conditions_structured
                updated += 1

        _atomic_write_json(tech_debt_path, tech_debt)
    finally:
        _release_triage_lock(lock_path, lock_fd)
    return updated


def merge_groups(
    group_id_a: str,
    group_id_b: str,
    semantic_groups_path: str | Path,
    tech_debt_path: str | Path,
) -> str:
    """Merge group B into group A. Returns the surviving group_id (A).

    All of B's member findings join A. Group B is removed from the groups file.
    tech_debt.json is updated so all of the surviving group's members (A's plus
    B's) carry ``group_id == A`` — the moved members now reference group A.

    Both files are rewritten atomically. Raises ``ValueError`` if either group
    id is missing or the two ids are the same.
    """
    if group_id_a == group_id_b:
        raise ValueError("cannot merge a group into itself")

    semantic_groups_path = Path(semantic_groups_path)
    tech_debt_path = Path(tech_debt_path)

    # Serialize the whole two-file read-modify-write under the shared triage
    # lock (the same lock apply_group_triage_decision uses), so a concurrent
    # merge/triage decision cannot read the same tech_debt image and clobber
    # the other's write. Snapshot both files first so that if the second write
    # fails the first is rolled back — the two-file update is all-or-nothing
    # rather than leaving semantic_groups merged but tech_debt not (or vice
    # versa). (FIX-CRITICAL run-019: unlocked RMW + missing transaction boundary)
    lock_path = tech_debt_path.with_suffix(f"{tech_debt_path.suffix}.triage.lock")
    lock_fd = _acquire_triage_lock(lock_path)
    try:
        groups = _load_json_list(semantic_groups_path)
        idx_a = next(
            (i for i, g in enumerate(groups) if g.get("group_id") == group_id_a), None
        )
        idx_b = next(
            (i for i, g in enumerate(groups) if g.get("group_id") == group_id_b), None
        )
        if idx_a is None:
            raise ValueError(f"group_id_a {group_id_a!r} not found in semantic groups")
        if idx_b is None:
            raise ValueError(f"group_id_b {group_id_b!r} not found in semantic groups")

        a = groups[idx_a]
        b = groups[idx_b]
        a_members = [str(x) for x in (a.get("member_finding_ids") or [])]
        b_members = [str(x) for x in (b.get("member_finding_ids") or [])]
        merged_members = list(dict.fromkeys(a_members + b_members))

        a["member_finding_ids"] = merged_members
        a["member_count"] = len(merged_members)
        a["severity"] = max(
            (a.get("severity"), b.get("severity")),
            key=lambda s: _SEVERITY_RANK.get(s, 0),
        )
        if a.get("contributing_emitters") is not None or b.get("contributing_emitters") is not None:
            a["contributing_emitters"] = list(
                dict.fromkeys(
                    (a.get("contributing_emitters") or []) + (b.get("contributing_emitters") or [])
                )
            )
        prior = a.get("merged_from_group_ids") or []
        a["merged_from_group_ids"] = list(dict.fromkeys([*prior, group_id_b]))

        groups.pop(idx_b)

        # Stamp group_id on the surviving group's members so the moved findings
        # reference A (additive field; tech_debt.json is otherwise unchanged).
        tech_debt = _load_json_list(tech_debt_path)
        member_set = set(merged_members)
        for item in tech_debt:
            if str(item.get("finding_id")) in member_set:
                item["group_id"] = group_id_a

        # Snapshot both files, then write both; roll back both on any failure.
        sg_snapshot = (
            semantic_groups_path.read_bytes() if semantic_groups_path.exists() else None
        )
        td_snapshot = (
            tech_debt_path.read_bytes() if tech_debt_path.exists() else None
        )
        try:
            _atomic_write_json(semantic_groups_path, groups)
            _atomic_write_json(tech_debt_path, tech_debt)
        except BaseException:
            _restore_state_bytes(semantic_groups_path, sg_snapshot)
            _restore_state_bytes(tech_debt_path, td_snapshot)
            raise
    finally:
        _release_triage_lock(lock_path, lock_fd)

    return group_id_a


def _atomic_write_json(path: str | Path, data: Any) -> None:
    """Write ``data`` as pretty JSON via a temp file + atomic replace, so a
    crash mid-write cannot corrupt the state file."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
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


def _restore_state_bytes(path: str | Path, snapshot: bytes | None) -> None:
    """Restore ``path`` to a previously captured byte image, or remove it when
    ``snapshot is None`` (the file did not exist before). Best-effort: used to
    roll back a multi-file write when a later write in the group fails."""
    path = Path(path)
    try:
        if snapshot is None:
            if path.exists():
                path.unlink()
        else:
            path.write_bytes(snapshot)
    except OSError:
        pass


_TECH_DEBT_TRIAGE_LOCK_TIMEOUT_SECONDS = 10.0
_TECH_DEBT_TRIAGE_LOCK_POLL_SECONDS = 0.01
# A lock file older than this is treated as stale — left behind by a crashed
# writer that never released it — and is reclaimed once before giving up.
_TECH_DEBT_TRIAGE_LOCK_STALE_SECONDS = 60.0


def _acquire_triage_lock(lock_path: Path) -> int:
    """Acquire the interprocess lock guarding a tech_debt.json triage RMW.

    On timeout, attempt one stale-lock recovery: if the existing lock file is
    older than ``_TECH_DEBT_TRIAGE_LOCK_STALE_SECONDS`` it is assumed abandoned
    by a crashed writer, unlinked, and the acquire is retried once. This keeps a
    single crash from wedging every future triage/merge indefinitely.
    """
    deadline = time.monotonic() + _TECH_DEBT_TRIAGE_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except (FileExistsError, PermissionError):
            if time.monotonic() >= deadline:
                if _reclaim_stale_triage_lock(lock_path):
                    # One retry after reclaiming a stale lock. A live competitor
                    # that grabbed it first will fail again and surface below.
                    try:
                        return os.open(
                            str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR
                        )
                    except (FileExistsError, PermissionError):
                        pass
                raise TimeoutError(
                    "Timed out waiting for tech_debt triage lock; another "
                    "writer may hold a stale lock."
                )
            time.sleep(_TECH_DEBT_TRIAGE_LOCK_POLL_SECONDS)


def _reclaim_stale_triage_lock(lock_path: Path) -> bool:
    """Unlink the lock file if it is older than the stale threshold.

    Returns ``True`` when a stale lock was removed (so the caller may retry),
    ``False`` when the lock is fresh, already gone, or its age is unreadable.
    """
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return False
    if age < _TECH_DEBT_TRIAGE_LOCK_STALE_SECONDS:
        return False
    try:
        lock_path.unlink()
        return True
    except FileNotFoundError:
        # Someone else reclaimed/released it — treat as reclaimable and retry.
        return True
    except OSError:
        return False


def _release_triage_lock(lock_path: Path, lock_fd: int) -> None:
    """Release the tech_debt triage lock acquired above."""
    try:
        os.close(lock_fd)
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            return


__all__ = [
    "generate_grouped_triage_view",
    "apply_group_triage_decision",
    "merge_groups",
]
