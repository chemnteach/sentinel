"""
sentinel/assess/arch_checker.py

Codebase-level architectural checklist.

Runs once per assessment (not per-module) after Stage 1 discovery.
Uses the manifest, module inventory, and dependency graph to detect
missing capabilities that per-module review layers cannot find.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sentinel.agents.architect import ArchitectAgent
from sentinel.orchestrator.state_store import StateStore
from sentinel.skills.loader import Skills

log = logging.getLogger(__name__)


class ArchChecker:
    """Run the architectural checklist against the full codebase."""

    def __init__(
        self,
        architect: ArchitectAgent,
        store: StateStore,
    ) -> None:
        self._architect = architect
        self._store = store

    def run(self) -> list[dict[str, Any]]:
        """
        Execute the architectural checklist.

        Reads manifest, modules, and dependencies from state.
        Returns findings and appends them to the state store.
        """
        manifest = self._store.read("repositories")
        modules = self._store.read("modules")
        dependencies = self._store.read("dependencies")

        context = self._build_context(manifest, modules, dependencies)

        try:
            result = self._architect.specialist_review(
                Skills.ARCH_CHECKLIST, context
            )
        except Exception as exc:
            log.warning("Architectural checklist failed: %s", exc)
            return []

        findings = _extract_arch_findings(result)

        for finding in findings:
            self._store.append_finding(finding)

        log.info("Architectural checklist: %d finding(s)", len(findings))
        return findings

    def _build_context(
        self,
        manifest: Any,
        modules: list[dict],
        dependencies: Any,
    ) -> str:
        """Assemble context for the architectural checklist."""
        module_summary = []
        for m in modules:
            name = m.get("name", m.get("path", "unknown"))
            files = m.get("files", [])
            module_summary.append(f"  - {name} ({len(files)} files)")

        return (
            "## Codebase Architectural Review\n\n"
            f"### Manifest\n```json\n{json.dumps(manifest, indent=2)[:8000]}\n```\n\n"
            f"### Modules ({len(modules)} total)\n"
            + "\n".join(module_summary)
            + f"\n\n### Dependencies\n```json\n{json.dumps(dependencies, indent=2)[:4000]}\n```\n"
        )


def _extract_arch_findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract and normalize findings from the architectural checklist."""
    raw = result.get("findings", [])
    if not isinstance(raw, list):
        return []

    findings = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        findings.append({
            "module": ", ".join(f.get("affected_modules", ["codebase"])),
            "skill": Skills.ARCH_CHECKLIST,
            "severity": f.get("severity", "P2"),
            "category": f.get("category", "architectural"),
            "location": "codebase-level",
            "description": f.get("description", str(f)),
            "suggested_fix": f.get("suggested_fix", ""),
            "status": "open",
        })
    return findings
