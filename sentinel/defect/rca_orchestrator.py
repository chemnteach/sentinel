"""
sentinel/defect/rca_orchestrator.py

Root Cause Analysis (RCA) and remediation spec orchestrator for Sentinel Defect subsystem.

Drives Layer 8a (Root Cause Analyzer) and Layer 8b (Bug Remediation Spec Writer)
via the Architect Agent, generates advisory reports, and raises the human gate.
"""

from __future__ import annotations

import logging
from typing import Any

from sentinel.agents.architect import ArchitectAgent
from sentinel.defect.context_slicer import ContextSlicer
from sentinel.defect.models import DefectReport, DefectStatus, RCAReport, DefectSpec
from sentinel.defect.defect_manager import DefectManager
from sentinel.orchestrator.state_store import StateStore

log = logging.getLogger(__name__)

_REQUIRED_RCA_KEYS = frozenset({
    "defect_id",
    "symptom_analysis",
    "proximal_mechanism",
    "systemic_factor",
    "root_cause",
    "remediation_options",
    "recommended_option",
})

_REQUIRED_SPEC_KEYS = frozenset({
    "defect_id",
    "selected_option",
    "reproduction_test_stubs",
    "proposed_diff",
    "acceptance_criteria",
})


class RCAError(RuntimeError):
    """Raised when the RCA pipeline encounters an unrecoverable failure."""


class HumanRCAGateRequired(Exception):
    """
    Raised when the RCA report and proposed remediation diff are ready for
    operator review / sign-off.
    """

    def __init__(self, defect_id: str, payload: dict[str, Any]) -> None:
        self.defect_id = defect_id
        self.payload = payload
        super().__init__(f"Human RCA review gate required for {defect_id}")


class RCAOrchestrator:
    """
    Orchestrates Layer 8a (RCA) and Layer 8b (Spec generation) for an ingested Defect.
    """

    def __init__(
        self,
        defect_manager: DefectManager,
        architect: ArchitectAgent,
        slicer: ContextSlicer,
        store: StateStore,
    ) -> None:
        self._defect_mgr = defect_manager
        self._architect = architect
        self._slicer = slicer
        self._store = store

    def run(self, defect_id: str) -> None:
        """
        Run the full Root Cause Analysis & remediation spec pipeline.
        """
        defect = self._defect_mgr.get(defect_id)
        log.info("RCAOrchestrator: starting analysis for %s (%s)", defect_id, defect.title)

        # 1. Advance to IN_RCA if in APPROVED state
        if defect.status == DefectStatus.APPROVED:
            self._defect_mgr.transition(defect_id, DefectStatus.IN_RCA, reason="Starting Layer 8a RCA")

        # 2. Build targeted context slice
        context_slice = self._slicer.build_slice(defect)

        # 3. Invoke Layer 8a Root Cause Analyzer
        log.info("RCAOrchestrator: invoking Layer 8a Root Cause Analyzer for %s", defect_id)
        rca_raw = self._architect.analyze_root_cause(context_slice.formatted_context)
        self._validate_rca(defect_id, rca_raw)

        rca_report = RCAReport.from_dict(rca_raw)
        self._store.upsert_in_list("rca_reports", rca_report.to_dict(), key_field="defect_id")

        self._defect_mgr.transition(defect_id, DefectStatus.RCA_COMPLETE, reason="Layer 8a RCA complete")

        # 4. Advance to IN_SPEC and invoke Layer 8b Spec Writer
        self._defect_mgr.transition(defect_id, DefectStatus.IN_SPEC, reason="Starting Layer 8b Spec generation")

        spec_context = self._build_spec_context(defect, rca_report, context_slice.formatted_context)
        log.info("RCAOrchestrator: invoking Layer 8b Bug Spec Writer for %s", defect_id)
        spec_raw = self._architect.write_bug_spec(spec_context)
        self._validate_spec(defect_id, spec_raw)

        defect_spec = DefectSpec.from_dict(spec_raw)
        self._store.upsert_in_list("defect_specs", defect_spec.to_dict(), key_field="defect_id")

        # 5. Format & save Markdown report + patch if formatter available
        try:
            from sentinel.reports.rca_formatter import RCAReportFormatter
            formatter = RCAReportFormatter(self._store.repo_path)
            report_path, patch_path = formatter.write_artifacts(defect, rca_report, defect_spec)
            self._defect_mgr.update_paths(defect_id, report_path=str(report_path), patch_path=str(patch_path))
        except Exception as exc:
            log.warning("RCAOrchestrator: could not write formatted markdown/patch artifacts: %s", exc)

        # 6. Advance to DIAGNOSED_REPORT_READY
        self._defect_mgr.transition(
            defect_id,
            DefectStatus.DIAGNOSED_REPORT_READY,
            reason="Layer 8b Spec & Advisory Report ready",
        )

        self._store.commit_state(f"sentinel: Defect {defect_id} — diagnosis and remediation spec complete")

        # 7. Raise Human Gate
        raise HumanRCAGateRequired(
            defect_id=defect_id,
            payload={
                "defect_id": defect_id,
                "title": defect.title,
                "severity": defect.severity,
                "symptom_analysis": rca_report.symptom_analysis,
                "proximal_mechanism": rca_report.proximal_mechanism,
                "systemic_factor": rca_report.systemic_factor,
                "root_cause": rca_report.root_cause,
                "blast_radius": rca_report.blast_radius,
                "recommended_option": rca_report.recommended_option,
                "proposed_diff": defect_spec.proposed_diff,
                "reproduction_test_stubs": defect_spec.reproduction_test_stubs,
                "acceptance_criteria": defect_spec.acceptance_criteria,
            },
        )

    def _validate_rca(self, defect_id: str, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise RCAError(f"Layer 8a response for {defect_id} is not a JSON object.")
        missing = _REQUIRED_RCA_KEYS - set(data.keys())
        if missing:
            raise RCAError(f"Layer 8a response for {defect_id} missing required keys: {', '.join(sorted(missing))}")

    def _validate_spec(self, defect_id: str, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise RCAError(f"Layer 8b response for {defect_id} is not a JSON object.")
        missing = _REQUIRED_SPEC_KEYS - set(data.keys())
        if missing:
            raise RCAError(f"Layer 8b response for {defect_id} missing required keys: {', '.join(sorted(missing))}")

    def _build_spec_context(self, defect: DefectReport, rca: RCAReport, source_context: str) -> str:
        parts: list[str] = [
            f"### Defect Remediation Spec Context for {defect.defect_id}: {defect.title}",
            f"\n**Observed Symptoms**: {rca.symptom_analysis}",
            f"\n**3-Tier Causal Decomposition**:",
            f"- Tier 1 (Proximal Mechanism): {rca.proximal_mechanism}",
            f"- Tier 2 (Systemic Factor): {rca.systemic_factor}",
            f"- Tier 3 (Root Cause): {rca.root_cause}",
            f"\n**Recommended Remediation Option**: {rca.recommended_option}",
            f"\n**Source Context & Code Slices**:\n{source_context}",
        ]
        return "\n".join(parts)
