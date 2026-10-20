"""Interactive triage: present findings for user decision.

Each finding is presented with severity, location, description, and recommendation.
User chooses: Accept (fix), Defer, Won't Fix, or Dispute (false positive).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class TriageDecision(str, Enum):
    """Possible triage decisions for a finding."""

    ACCEPT = "accept"
    DEFER = "defer"
    WONT_FIX = "wont_fix"
    DISPUTE = "dispute"


@dataclass
class TriageResult:
    """Result of triaging a single finding."""

    finding_id: str
    decision: TriageDecision
    rationale: str | None


@dataclass
class TriageSession:
    """Complete triage session results."""

    total: int
    results: list[TriageResult]

    @property
    def accepted(self) -> int:
        """Count of accepted findings."""
        return sum(1 for r in self.results if r.decision == TriageDecision.ACCEPT)

    @property
    def deferred(self) -> int:
        """Count of deferred findings."""
        return sum(1 for r in self.results if r.decision == TriageDecision.DEFER)

    @property
    def wont_fix(self) -> int:
        """Count of won't-fix findings."""
        return sum(1 for r in self.results if r.decision == TriageDecision.WONT_FIX)

    @property
    def disputed(self) -> int:
        """Count of disputed findings."""
        return sum(1 for r in self.results if r.decision == TriageDecision.DISPUTE)

    def summary(self) -> dict[str, int]:
        """Return triage summary counts."""
        return {
            "total": self.total,
            "accepted": self.accepted,
            "deferred": self.deferred,
            "wont_fix": self.wont_fix,
            "disputed": self.disputed,
        }

    def has_open_blockers(self) -> bool:
        """True if any Critical/P1 findings were not accepted.

        A blocker is a Critical or P1 finding whose triage decision is
        anything other than ACCEPT (i.e. deferred, won't-fix, or disputed).
        """
        blocker_severities = {"Critical", "P1"}
        # Build a lookup of decisions by finding_id
        decisions_by_id = {r.finding_id: r.decision for r in self.results}
        # Check if any finding_id in the results maps to a blocker severity
        # that was NOT accepted. We need the original findings for severity info.
        # Store findings reference at session creation via _findings attribute.
        findings_map = getattr(self, "_findings_map", {})
        for result in self.results:
            severity = findings_map.get(result.finding_id, "")
            if severity in blocker_severities and result.decision != TriageDecision.ACCEPT:
                return True
        return False


class TriageManager:
    """Manages interactive triage of findings."""

    def __init__(self, findings: list[dict]) -> None:
        """Initialize with findings sorted by severity (Critical first)."""
        self._findings = sorted(findings, key=self._severity_key, reverse=True)
        self._results: list[TriageResult] = []
        self._triaged_ids: set[str] = set()
        # Map finding_id -> severity for blocker detection
        self._severity_map: dict[str, str] = {
            f.get("id", ""): f.get("severity", "P3") for f in findings
        }

    def get_next(self) -> dict | None:
        """Get next finding to triage. Returns None when all triaged."""
        for f in self._findings:
            fid = f.get("id", "")
            if fid not in self._triaged_ids:
                return f
        return None

    def record_decision(
        self,
        finding_id: str,
        decision: TriageDecision,
        rationale: str | None = None,
    ) -> TriageResult:
        """Record user's decision for a finding."""
        # Verify finding exists
        if not any(f.get("id") == finding_id for f in self._findings):
            # Also check loaded results (findings list may be empty after load)
            if finding_id not in self._severity_map:
                raise ValueError(
                    f"Finding '{finding_id}' not found in the current triage set."
                )

        result = TriageResult(
            finding_id=finding_id,
            decision=decision,
            rationale=rationale,
        )
        self._results.append(result)
        self._triaged_ids.add(finding_id)
        return result

    def get_session(self) -> TriageSession:
        """Get complete triage session results.

        ``total`` reflects the total number of findings in the triage set — not
        merely how many have been triaged so far — so a partially-completed
        session does not under-report its scope. The authoritative set of known
        findings is ``_severity_map`` (original findings plus any loaded from a
        prior results file).
        """
        session = TriageSession(
            total=self._total_findings(),
            results=list(self._results),
        )
        # Attach severity map for blocker detection
        session._findings_map = dict(self._severity_map)  # type: ignore[attr-defined]
        return session

    def _total_findings(self) -> int:
        """Total distinct findings known to this session.

        Uses the severity map, which holds every original finding ID plus any
        IDs restored from a previously saved results file — the complete triage
        scope regardless of how many have been decided so far.
        """
        return len(self._severity_map)

    def format_finding_for_display(self, finding: dict) -> str:
        """Format a finding for interactive display.

        Returns a formatted string with severity, location, description,
        and lettered decision options.
        """
        fid = finding.get("id", "?")
        severity = finding.get("severity", "?")
        location = finding.get("location", "?")
        category = finding.get("category", "")
        description = finding.get("description", "")
        suggested_fix = finding.get("suggested_fix", "")

        lines = [
            f"[{fid}] {severity} | {location} | {category}",
            description,
            "",
        ]
        if suggested_fix:
            lines.append(f"Recommendation: {suggested_fix}")
            lines.append("")

        lines.extend([
            "A) Accept -- fix this finding",
            "B) Defer -- fix later",
            "C) Won't fix -- accept the risk",
            "D) Dispute -- this is a false positive",
        ])
        return "\n".join(lines)

    def save_results(self, output_path: Path) -> None:
        """Save triage results to JSON file."""
        data = [
            {
                "finding_id": r.finding_id,
                "decision": r.decision.value,
                "rationale": r.rationale,
            }
            for r in self._results
        ]
        output_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def load_results(self, input_path: Path) -> None:
        """Load previous triage results."""
        data = json.loads(input_path.read_text(encoding="utf-8"))
        self._results.clear()
        self._triaged_ids.clear()
        for item in data:
            result = TriageResult(
                finding_id=item["finding_id"],
                decision=TriageDecision(item["decision"]),
                rationale=item.get("rationale"),
            )
            self._results.append(result)
            self._triaged_ids.add(item["finding_id"])
            # Ensure severity map has entries for loaded results. When a loaded
            # result references a finding whose severity is unknown to this
            # session, default CONSERVATIVELY to "Critical" rather than "P3":
            # assuming the lowest severity could silently hide a Critical/P1
            # blocker from has_open_blockers(). Log so the assumption is visible.
            if item["finding_id"] not in self._severity_map:
                log.warning(
                    "Loaded triage result for '%s' has unknown severity; "
                    "defaulting to 'Critical' (conservative).",
                    item["finding_id"],
                )
                self._severity_map[item["finding_id"]] = "Critical"

    @staticmethod
    def _severity_key(finding: dict) -> int:
        """Sort key: Critical=4, P1=3, P2=2, P3=1."""
        severity_order = {
            "Critical": 4,
            "P1": 3,
            "P2": 2,
            "P3": 1,
        }
        return severity_order.get(finding.get("severity", ""), 0)
