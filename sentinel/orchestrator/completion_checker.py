"""
sentinel/orchestrator/completion_checker.py

S2-3 — Completion gate (``sentinel govern --verify-complete``).

Produces a delivery-readiness certificate for a governed remediation run. The
gate is READY iff every approved Critical and P1 finding has landed (its owning
task is COMPLETE / HUMAN_MERGED); open P2/P3 work is reported but does not
block delivery.

Task ↔ severity join
--------------------
Tasks do not carry a severity field. The severity view is reconstructed from
the finding record, then related back to tasks via the plan:

1. ``findings.json`` — each finding has ``id`` and ``severity`` (and, post-S2-1,
   an optional ``verified`` bool). This is the authoritative severity source.
2. ``tech_debt.json`` — a finding is *approved* when it has a tech-debt item
   whose ``human_triage == "APPROVE"``. Only approved findings are in scope for
   the gate.
3. ``plan.json`` — each plan item has an ``id`` (== ``task.plan_id``) and a
   ``source_findings`` list of finding ids. This maps a finding → its plan item
   → its task. When a plan item also carries a ``bundle_id``, the bundle's
   ``member_findings`` provide a secondary finding→task path (bundle_id →
   task.bundle_id).
4. ``tasks.json`` — each task carries ``plan_id`` / ``bundle_id`` / ``state``.

For each approved finding we resolve its owning task (plan_id first, bundle_id
as fallback) and classify it into one severity-band bucket:

- ``complete`` — owning task is COMPLETE or HUMAN_MERGED.
- ``stuck``    — owning task is ESCALATED* / BLOCKED / CANCELLED.
- ``open``     — owning task is in any other (in-flight) state, OR the finding
                 has no owning task at all (surfaced as a blocking item:
                 "N <band> findings have no tasks").
- ``approved`` — total approved findings in the band (the denominator). When
                 the ``verified`` field is present on any finding, only verified
                 findings are counted as approved (unverified are filtered out).

Overall status is READY iff there are zero open Critical *and* zero open P1
findings, where "open" for certification purposes means open+stuck (anything
not landed). P2/P3 open counts are reported but non-blocking.

Missing plan.json / tasks.json / bundles.json are tolerated: with no plan/task
lineage every approved finding resolves to "no task" and is therefore open.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sentinel.assess.finding_status import BundleStatus
from sentinel.config import Config
from sentinel.orchestrator.state_machine import TaskState
from sentinel.orchestrator.state_store import StateStore

log = logging.getLogger(__name__)

COMPLETION_REPORT_FILENAME = "completion_report.json"

# Severity bands, in report/display order.
_SEVERITY_BANDS: tuple[str, ...] = ("Critical", "P1", "P2", "P3")

# Task states that count as "landed" (finding resolved).
_COMPLETE_TASK_STATES: frozenset[str] = frozenset(
    {TaskState.COMPLETE.value, TaskState.HUMAN_MERGED.value}
)

# Task states that count as "stuck" (needs human intervention, not landing).
_STUCK_TASK_STATES: frozenset[str] = frozenset(
    {
        TaskState.ESCALATED.value,
        TaskState.ESCALATED_TO_CODEX.value,
        TaskState.ESCALATED_TO_OPUS.value,
        TaskState.ESCALATED_TO_HUMAN.value,
        TaskState.BLOCKED.value,
        TaskState.CANCELLED.value,
    }
)

# The two bands whose open work blocks delivery.
_BLOCKING_BANDS: tuple[str, ...] = ("Critical", "P1")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _severity_band(raw: Any) -> str | None:
    """Normalise a finding's severity to one of the four bands, or None."""
    s = str(raw or "").strip()
    if not s:
        return None
    lo = s.lower()
    if lo in ("critical", "crit"):
        return "Critical"
    if lo in ("p1", "high"):
        return "P1"
    if lo in ("p2", "medium", "med"):
        return "P2"
    if lo in ("p3", "low"):
        return "P3"
    # Already a canonical band?
    if s in _SEVERITY_BANDS:
        return s
    return None


class CompletionChecker:
    """Assemble and print the S2-3 delivery-readiness completion report.

    See the module docstring for the exact task ↔ severity join logic.
    """

    def __init__(self, store: StateStore, config: Config) -> None:
        self._store = store
        self._config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self) -> dict[str, Any]:
        """Compute the full completion report dict."""
        findings = self._read_list("findings")
        tasks = self._read_list("tasks")
        bundles = self._read_list("bundles")
        plan_items = self._read_plan_items()
        approved_ids = self._approved_finding_ids()
        scan_metadata = self._read_dict("scan_metadata")

        # verified filter: only applies when the field is present somewhere.
        has_verified_field = any("verified" in f for f in findings)
        verified_findings = sum(
            1 for f in findings if f.get("verified") is True
        )
        if not has_verified_field:
            verified_findings = len(findings)
        unverified_filtered = self._unverified_count()

        # Build finding_id -> owning task (plan_id path, bundle_id fallback).
        task_by_plan = {
            str(t.get("plan_id")): t for t in tasks if t.get("plan_id") is not None
        }
        task_by_bundle = {
            str(t.get("bundle_id")): t
            for t in tasks
            if t.get("bundle_id") is not None
        }
        fid_to_plan_item = self._finding_to_plan_item(plan_items)
        fid_to_bundle = self._finding_to_bundle(bundles)

        finding_by_id = {str(f.get("id")): f for f in findings if f.get("id") is not None}

        # Per-band tallies.
        bands: dict[str, dict[str, int]] = {
            band: {"approved": 0, "complete": 0, "open": 0, "stuck": 0}
            for band in _SEVERITY_BANDS
        }
        no_task_by_band: dict[str, int] = {band: 0 for band in _SEVERITY_BANDS}

        for fid in approved_ids:
            finding = finding_by_id.get(fid)
            if finding is None:
                continue
            band = _severity_band(finding.get("severity"))
            if band is None:
                continue
            # Filter out unverified findings when verification ran.
            if has_verified_field and finding.get("verified") is False:
                continue

            bands[band]["approved"] += 1

            task = self._owning_task(
                fid, fid_to_plan_item, fid_to_bundle, task_by_plan, task_by_bundle
            )
            if task is None:
                bands[band]["open"] += 1
                no_task_by_band[band] += 1
                continue

            state = str(task.get("state") or "")
            if state in _COMPLETE_TASK_STATES:
                bands[band]["complete"] += 1
            elif state in _STUCK_TASK_STATES:
                bands[band]["stuck"] += 1
            else:
                bands[band]["open"] += 1

        blocking_items = self._assemble_blocking_items(bands, no_task_by_band)

        critical_open = bands["Critical"]["open"] + bands["Critical"]["stuck"]
        p1_open = bands["P1"]["open"] + bands["P1"]["stuck"]
        ready = critical_open == 0 and p1_open == 0
        status = "READY" if ready else "NOT_READY"

        report: dict[str, Any] = {
            "run_id": self._run_id(),
            "assessed_commit": scan_metadata.get("commit_sha", ""),
            "model": scan_metadata.get("model", ""),
            "provider": scan_metadata.get("provider", ""),
            "operator": getattr(self._config, "operator", "unknown"),
            "generated_at": _now_iso(),
            "verified_findings": verified_findings,
            "unverified_filtered": unverified_filtered,
            "findings_by_severity": bands,
            "bundles": self._count_bundles(bundles),
            "blocking_items": blocking_items,
            "status": status,
            "certification": {
                "ready": ready,
                "critical_open": critical_open,
                "p1_open": p1_open,
                "signed_at": _now_iso() if ready else None,
            },
        }
        return report

    def print_report(self, report: dict[str, Any]) -> None:
        """Print a human-readable completion report box."""
        line = "=" * 64
        out: list[str] = []
        out.append(line)
        out.append("SENTINEL COMPLETION REPORT")
        out.append(line)
        out.append(f"Run:       {report.get('run_id', '')}")
        out.append(f"Commit:    {report.get('assessed_commit', '') or '(unknown)'}")
        out.append(
            f"Model:     {report.get('model', '') or '(unknown)'} "
            f"[{report.get('provider', '') or '(unknown)'}]"
        )
        out.append(f"Operator:  {report.get('operator', '')}")
        out.append(f"Generated: {report.get('generated_at', '')}")
        out.append("")

        out.append("FINDINGS (approved / complete / open / stuck)")
        out.append(f"  {'Band':<10}{'Appr':>6}{'Done':>6}{'Open':>6}{'Stuck':>7}")
        bands = report.get("findings_by_severity", {})
        for band in _SEVERITY_BANDS:
            b = bands.get(band, {})
            out.append(
                f"  {band:<10}"
                f"{b.get('approved', 0):>6}"
                f"{b.get('complete', 0):>6}"
                f"{b.get('open', 0):>6}"
                f"{b.get('stuck', 0):>7}"
            )
        out.append("")

        bundles = report.get("bundles", {})
        if bundles.get("total", 0):
            out.append("BUNDLES")
            out.append(
                f"  total={bundles.get('total', 0)} "
                f"complete={bundles.get('complete', 0)} "
                f"in_review={bundles.get('in_review', 0)} "
                f"in_progress={bundles.get('in_progress', 0)} "
                f"approved={bundles.get('approved', 0)} "
                f"undecided={bundles.get('undecided', 0)} "
                f"reverted={bundles.get('reverted', 0)}"
            )
            out.append("")

        blocking = report.get("blocking_items", [])
        out.append("BLOCKING")
        if blocking:
            for item in blocking:
                out.append(f"  - {item}")
        else:
            out.append("  None — all clear")
        out.append("")

        if report.get("status") == "READY":
            out.append("STATUS: ✅ READY FOR DELIVERY")
        else:
            out.append(f"STATUS: ❌ NOT READY — {len(blocking)} items blocking")
        out.append(line)

        print("\n".join(out))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_list(self, name: str) -> list[dict[str, Any]]:
        try:
            data = self._store.read(name)
        except Exception as exc:  # noqa: BLE001 — a corrupt file must degrade gracefully
            log.warning("completion_checker: could not read %s: %s", name, exc)
            return []
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        return []

    def _read_dict(self, name: str) -> dict[str, Any]:
        try:
            data = self._store.read(name)
        except Exception as exc:  # noqa: BLE001
            log.warning("completion_checker: could not read %s: %s", name, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _read_plan_items(self) -> list[dict[str, Any]]:
        """Flatten plan items across the Fix Planner's priority tiers.

        Tolerates the object form ({"items": [...]}, or {"blocking": [...], ...})
        and a bare list. Missing plan.json yields an empty list.
        """
        try:
            plan = self._store.read("plan")
        except Exception as exc:  # noqa: BLE001
            log.warning("completion_checker: could not read plan: %s", exc)
            return []
        if isinstance(plan, list):
            return [p for p in plan if isinstance(p, dict)]
        if not isinstance(plan, dict):
            return []
        items: list[dict[str, Any]] = []
        for priority in ("blocking", "required", "desired", "nice_to_have"):
            batch = plan.get(priority, plan.get(priority.replace("_", "-"), []))
            if isinstance(batch, list):
                items.extend(p for p in batch if isinstance(p, dict))
        if not items:
            for key in ("items", "tasks"):
                batch = plan.get(key, [])
                if isinstance(batch, list):
                    items.extend(p for p in batch if isinstance(p, dict))
        return items

    def _approved_finding_ids(self) -> set[str]:
        """Finding ids approved via tech_debt (human_triage == APPROVE)."""
        approved: set[str] = set()
        for item in self._read_list("tech_debt"):
            if str(item.get("human_triage") or "").strip().upper() == "APPROVE":
                fid = item.get("finding_id")
                if fid is not None:
                    approved.add(str(fid))
        return approved

    def _unverified_count(self) -> int:
        """Count of findings quarantined as unverified (S2-1)."""
        return len(self._read_list("unverified_findings"))

    @staticmethod
    def _finding_to_plan_item(
        plan_items: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Map each finding id to the first plan item that lists it."""
        mapping: dict[str, dict[str, Any]] = {}
        for item in plan_items:
            for fid in item.get("source_findings", []) or []:
                key = str(fid)
                if key not in mapping:
                    mapping[key] = item
        return mapping

    @staticmethod
    def _finding_to_bundle(bundles: list[dict[str, Any]]) -> dict[str, str]:
        """Map each finding id to its bundle_id via bundle member_findings."""
        mapping: dict[str, str] = {}
        for bundle in bundles:
            bid = bundle.get("bundle_id")
            if bid is None:
                continue
            for member in bundle.get("member_findings", []) or []:
                fid = member.get("finding_id")
                if fid is not None:
                    mapping.setdefault(str(fid), str(bid))
        return mapping

    @staticmethod
    def _owning_task(
        fid: str,
        fid_to_plan_item: dict[str, dict[str, Any]],
        fid_to_bundle: dict[str, str],
        task_by_plan: dict[str, dict[str, Any]],
        task_by_bundle: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Resolve a finding's owning task (plan_id first, bundle_id fallback)."""
        plan_item = fid_to_plan_item.get(fid)
        if plan_item is not None:
            plan_id = plan_item.get("id")
            if plan_id is not None:
                task = task_by_plan.get(str(plan_id))
                if task is not None:
                    return task
        bundle_id = fid_to_bundle.get(fid)
        if bundle_id is not None:
            task = task_by_bundle.get(bundle_id)
            if task is not None:
                return task
        return None

    @staticmethod
    def _assemble_blocking_items(
        bands: dict[str, dict[str, int]],
        no_task_by_band: dict[str, int],
    ) -> list[str]:
        """Human-readable blocking strings for Critical/P1 open + stuck work."""
        items: list[str] = []
        for band in _BLOCKING_BANDS:
            b = bands[band]
            no_task = no_task_by_band[band]
            in_flight_open = b["open"] - no_task
            if no_task:
                noun = "finding" if no_task == 1 else "findings"
                items.append(f"{no_task} {band} {noun} have no tasks")
            if in_flight_open > 0:
                noun = "task" if in_flight_open == 1 else "tasks"
                items.append(f"{in_flight_open} {band} {noun} still open")
            if b["stuck"] > 0:
                noun = "task" if b["stuck"] == 1 else "tasks"
                items.append(f"{b['stuck']} {band} {noun} stuck (escalated/blocked)")
        return items

    @staticmethod
    def _count_bundles(bundles: list[dict[str, Any]]) -> dict[str, int]:
        counts = {
            "total": len(bundles),
            "complete": 0,
            "in_review": 0,
            "in_progress": 0,
            "approved": 0,
            "undecided": 0,
            "reverted": 0,
        }
        status_to_key = {
            BundleStatus.COMPLETE: "complete",
            BundleStatus.IN_REVIEW: "in_review",
            BundleStatus.IN_PROGRESS: "in_progress",
            BundleStatus.APPROVED: "approved",
            BundleStatus.UNDECIDED: "undecided",
            BundleStatus.REVERTED: "reverted",
        }
        for bundle in bundles:
            status = str(bundle.get("bundle_status") or bundle.get("status") or "")
            key = status_to_key.get(status)
            if key:
                counts[key] += 1
        return counts

    def _run_id(self) -> str:
        try:
            return self._config.state_dir.name
        except Exception:  # noqa: BLE001
            return ""


__all__ = ["CompletionChecker", "COMPLETION_REPORT_FILENAME"]
