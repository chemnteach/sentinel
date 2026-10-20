"""
sentinel/assess/synthesizer.py

Stage 4 — Cross-Module Synthesis.

Correlates findings across all modules, identifies systemic patterns,
and prepares the aggregated context for the Documentation Agent's
report generation and the Fix Planner.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Any

from sentinel.orchestrator.state_store import StateStore

log = logging.getLogger(__name__)

_SEVERITY_RANK = {"Critical": 0, "P1": 1, "P2": 2, "P3": 3}


class Synthesizer:
    """
    Synthesises findings from all modules into a coherent summary.

    Identifies systemic patterns (e.g., "missing error handling appears
    in 8 of 12 modules"), computes severity breakdowns, and produces
    the tech debt register.
    """

    def __init__(self, store: StateStore) -> None:
        self._store = store

    def run(self) -> dict[str, Any]:
        """
        Perform cross-module synthesis.

        Reads from findings.json and modules.json, writes to tech_debt.json.

        Returns:
            Synthesis summary dict used as context for Fix Planner and
            Documentation Agent.
        """
        findings = self._store.read("findings")
        modules = self._store.read("modules")

        log.info(
            "Synthesis: %d finding(s) across %d module(s)",
            len(findings),
            len(modules),
        )

        severity_counts = Counter(
            f.get("severity", "P3") for f in findings
        )
        findings_by_module: dict[str, list] = defaultdict(list)
        for f in findings:
            findings_by_module[f.get("module", "unknown")].append(f)

        findings_by_skill: dict[str, list] = defaultdict(list)
        for f in findings:
            findings_by_skill[f.get("skill", "unknown")].append(f)

        # Identify systemic patterns — skills with many findings across modules
        systemic_patterns = []
        for skill, skill_findings in findings_by_skill.items():
            affected_modules = len({f.get("module") for f in skill_findings})
            if affected_modules >= 3:  # Appears in 3+ modules = systemic
                systemic_patterns.append({
                    "pattern": skill,
                    "affected_modules": affected_modules,
                    "finding_count": len(skill_findings),
                    "severity_breakdown": dict(
                        Counter(f.get("severity") for f in skill_findings)
                    ),
                })

        systemic_patterns.sort(key=lambda p: -p["affected_modules"])

        # Build tech debt register
        tech_debt = self._build_tech_debt_register(findings, modules)

        self._store.write(
            "tech_debt",
            tech_debt,
            commit=True,
            commit_message="sentinel: Stage 4 — tech debt register",
        )

        summary = {
            "total_findings": len(findings),
            "severity_counts": dict(severity_counts),
            "total_modules_reviewed": len(modules),
            "modules_with_findings": len(findings_by_module),
            "systemic_patterns": systemic_patterns,
            "highest_risk_modules": self._rank_modules(findings_by_module),
            "tech_debt_items": len(tech_debt),
        }

        log.info(
            "Synthesis complete: %s",
            ", ".join(f"{k}={v}" for k, v in severity_counts.items()),
        )
        return summary

    def build_fix_planner_context(self) -> dict[str, Any]:
        """
        Assemble the full context dict for the Fix Planner.

        Returns:
            Dict with findings, modules, dependencies, and synthesis summary.
        """
        return {
            "findings": self._store.read("findings"),
            "modules": self._store.read("modules"),
            "dependencies": self._store.read("dependencies"),
            "tech_debt": self._store.read("tech_debt"),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_tech_debt_register(
        self,
        findings: list[dict[str, Any]],
        modules: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Build the tech debt register from accumulated findings."""
        register = []
        for i, finding in enumerate(findings):
            register.append({
                "id": f"TD-{i+1:04d}",
                "module": finding.get("module", ""),
                "finding_id": finding.get("id", ""),
                "skill": finding.get("skill", ""),
                "severity": finding.get("severity", "P3"),
                "description": finding.get("description", ""),
                "suggested_fix": finding.get("suggested_fix", ""),
                "effort_estimate": _estimate_effort(finding),
                "risk_of_deferral": _estimate_deferral_risk(finding),
                "human_triage": None,  # Set during Stage 5 human triage
                "status": "pending_triage",
            })
        return register

    def _rank_modules(
        self,
        findings_by_module: dict[str, list],
    ) -> list[dict[str, Any]]:
        """Return the top 10 highest-risk modules by finding severity.

        Accepted severities: Critical, P1, P2, P3.
        Unrecognized severity strings are treated as highest-risk
        (equivalent to Critical) so that unknown risks surface at the
        top of the ranking rather than being silently downgraded.
        """
        scored = []
        for module, module_findings in findings_by_module.items():
            score = sum(
                4 - _SEVERITY_RANK.get(f.get("severity", "P3"), 0)
                for f in module_findings
            )
            scored.append({
                "module": module,
                "risk_score": score,
                "finding_count": len(module_findings),
                "critical_count": sum(
                    1 for f in module_findings if f.get("severity") == "Critical"
                ),
            })
        scored.sort(key=lambda m: -m["risk_score"])
        return scored[:10]


def _estimate_effort(finding: dict[str, Any]) -> str:
    """Rough effort estimate based on severity and skill."""
    severity = finding.get("severity", "P3")
    if severity == "Critical":
        return "high"
    if severity == "P1":
        return "medium-high"
    if severity == "P2":
        return "medium"
    return "low"


def _estimate_deferral_risk(finding: dict[str, Any]) -> str:
    """Risk of deferring this finding."""
    severity = finding.get("severity", "P3")
    if severity == "Critical":
        return "unacceptable — must fix before next release"
    if severity == "P1":
        return "high — likely to cause production issues"
    if severity == "P2":
        return "medium — technical debt accumulates"
    return "low — cosmetic or nice-to-have"
