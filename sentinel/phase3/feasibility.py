"""
sentinel/phase3/feasibility.py

Feasibility assessment orchestrator for Sentinel Phase 3.

Drives the Layer 7a (Feature Feasibility Analyzer) pipeline for a given
FER. Handles context assembly, model invocation via the Architect Agent,
result validation, state persistence, and the human approval gate.

The feasibility pipeline is resumable: if a model call fails after the
assessment is written to state but before the human gate, the state file
contains the partial result and the human gate can be re-raised without
re-running the model call.
"""

from __future__ import annotations

import logging
from typing import Any

from sentinel.agents.architect import ArchitectAgent
from sentinel.orchestrator.state_store import StateStore
from sentinel.phase3.fer_manager import FER, FERManager

log = logging.getLogger(__name__)

# Required top-level keys in a valid Layer 7a response
_REQUIRED_KEYS = frozenset({
    "fer_id",
    "feasibility_verdict",
    "effort_decomposition",
    "risk_level",
    "options",
    "recommended_approach",
})

_VALID_VERDICTS = frozenset({"FEASIBLE", "FEASIBLE_WITH_CONSTRAINTS", "NOT_FEASIBLE"})


class FeasibilityError(RuntimeError):
    """Raised when the feasibility pipeline encounters an unrecoverable state."""


class HumanFeasibilityGateRequired(Exception):
    """
    Raised when the feasibility assessment is complete and requires human
    review before proceeding to spec generation.

    The CLI displays payload to the operator and waits for
    ``sentinel feasibility --approve`` or ``sentinel feasibility --defer``.
    """

    def __init__(self, fer_id: str, payload: dict[str, Any]) -> None:
        self.fer_id = fer_id
        self.payload = payload
        super().__init__(f"Human feasibility gate required for {fer_id}")


class FeasibilityOrchestrator:
    """
    Orchestrates Layer 7a feasibility assessment for a single FER.

    Workflow:
      1. Advance FER to IN_FEASIBILITY
      2. Build context from FER + Phase 1 codebase manifest
      3. Invoke Architect with Layer 7a skill
      4. Validate and persist the feasibility report
      5. Raise HumanFeasibilityGateRequired for operator review
    """

    def __init__(
        self,
        fer_manager: FERManager,
        architect: ArchitectAgent,
        store: StateStore,
    ) -> None:
        self._fer_mgr = fer_manager
        self._architect = architect
        self._store = store

    def run(self, fer_id: str) -> None:
        """
        Run the full feasibility pipeline for a FER.

        Advances the FER to IN_FEASIBILITY, runs the Layer 7a assessment,
        persists the result, then raises HumanFeasibilityGateRequired.

        Args:
            fer_id: The FER to assess.

        Raises:
            HumanFeasibilityGateRequired: Always, on successful completion.
            FeasibilityError: On unrecoverable pipeline failure.
        """
        fer = self._fer_mgr.get(fer_id)
        log.info("FeasibilityOrchestrator: starting assessment for %s", fer_id)

        # Advance to IN_FEASIBILITY (idempotent if already there)
        if fer.status == "APPROVED":
            fer = self._fer_mgr.start_feasibility(fer_id)

        # Check if feasibility report already exists (resumability). A cached
        # report is only trustworthy if it still passes validation — a partial
        # or corrupt cached state must not be silently reused and skip the gate
        # contract. If validation fails, discard the cached state and re-run the
        # model call from scratch.
        existing = self._load_existing(fer_id)
        if existing:
            try:
                self._validate_report(fer_id, existing)
                reuse = True
            except FeasibilityError as exc:
                log.warning(
                    "FeasibilityOrchestrator: cached report for %s failed "
                    "validation (%s) — clearing and re-running assessment",
                    fer_id,
                    exc,
                )
                self._clear_existing(fer_id)
                reuse = False
        else:
            reuse = False

        if reuse:
            log.info(
                "FeasibilityOrchestrator: existing report found for %s — skipping model call",
                fer_id,
            )
            report = existing
        else:
            # Build context and invoke Architect with Layer 7a
            context = self._fer_mgr.build_feasibility_context(fer)
            log.info("FeasibilityOrchestrator: invoking Layer 7a for %s", fer_id)
            report = self._architect.assess_feasibility(context)

            # Validate the response
            self._validate_report(fer_id, report)

            # Persist the report
            self._store.upsert_in_list(
                "feasibility",
                {"fer_id": fer_id, **report},
                key_field="fer_id",
            )
            self._store.commit_state(
                f"sentinel: FER {fer_id} — feasibility assessment complete"
            )
            log.info(
                "FeasibilityOrchestrator: %s — verdict: %s, risk: %s, recommended: %s",
                fer_id,
                report.get("feasibility_verdict"),
                report.get("risk_level"),
                report.get("recommended_approach"),
            )

        # Format options for human display
        options_summary = self._format_options(report)

        # Both branches above converge on a validated ``report``; there is no
        # early-return path out of run(), so a successful assessment ALWAYS
        # reaches this raise. The human gate is therefore non-bypassable for any
        # valid completion state.
        raise HumanFeasibilityGateRequired(
            fer_id=fer_id,
            payload={
                "fer_id": fer_id,
                "feasibility_verdict": report.get("feasibility_verdict"),
                "risk_level": report.get("risk_level"),
                "total_effort_estimate": report.get("total_effort_estimate", "?"),
                "recommended_approach": report.get("recommended_approach"),
                "options_summary": options_summary,
                "clarifications_needed": report.get("clarifications_needed", []),
                "message": (
                    f"\nFeasibility assessment complete for {fer_id}.\n\n"
                    f"Verdict:    {report.get('feasibility_verdict')}\n"
                    f"Risk:       {report.get('risk_level')}\n"
                    f"Effort:     {report.get('total_effort_estimate', '?')}\n"
                    f"Recommended: {report.get('recommended_approach')}\n\n"
                    f"Options:\n{options_summary}\n\n"
                    "Full report: sentinel/state/feasibility.json\n\n"
                    "To approve and select an approach:\n"
                    f"  sentinel feasibility --approve {fer_id} "
                    f"--approach \"<approach_name>\"\n\n"
                    "To defer:\n"
                    f"  sentinel feasibility --defer {fer_id}"
                ),
            },
        )

    def approve(
        self,
        fer_id: str,
        selected_approach: str,
        actor: str = "human",
    ) -> FER:
        """
        Record human approval of the feasibility assessment.

        Args:
            fer_id: The FER to approve.
            selected_approach: The approach_name chosen by the operator.
            actor: Who approved (default: "human").

        Returns:
            The updated FER in FEASIBILITY_APPROVED status.

        Raises:
            FeasibilityError: If no feasibility report exists for this FER.
        """
        report = self._load_existing(fer_id)
        if not report:
            raise FeasibilityError(
                f"No feasibility report found for {fer_id}. "
                "Run the feasibility assessment first."
            )

        # Validate the selected approach exists in the report
        available = {o.get("approach_name") for o in report.get("options", [])}
        if selected_approach not in available:
            raise FeasibilityError(
                f"Approach '{selected_approach}' not found in feasibility report for {fer_id}. "
                f"Available approaches: {', '.join(sorted(available))}"
            )

        return self._fer_mgr.record_feasibility_approved(
            fer_id=fer_id,
            selected_approach=selected_approach,
            feasibility_verdict=report.get("feasibility_verdict", "FEASIBLE"),
            actor=actor,
        )

    def get_report(self, fer_id: str) -> dict[str, Any] | None:
        """Return the feasibility report for a FER, or None if not yet assessed."""
        return self._load_existing(fer_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_existing(self, fer_id: str) -> dict[str, Any] | None:
        """Load an existing feasibility report from state, or return None."""
        reports = self._store.read("feasibility")
        for report in reports:
            if report.get("fer_id") == fer_id:
                return report
        return None

    def _clear_existing(self, fer_id: str) -> None:
        """Remove a cached (invalid) feasibility report for this FER from state."""
        reports = self._store.read("feasibility")
        remaining = [r for r in reports if r.get("fer_id") != fer_id]
        if len(remaining) != len(reports):
            self._store.write("feasibility", remaining, commit=False)

    def _validate_report(self, fer_id: str, report: dict[str, Any]) -> None:
        """Validate the Layer 7a response structure."""
        missing = _REQUIRED_KEYS - set(report.keys())
        if missing:
            raise FeasibilityError(
                f"Layer 7a response for {fer_id} missing required keys: "
                f"{sorted(missing)}. Got: {sorted(report.keys())}"
            )

        verdict = report.get("feasibility_verdict", "").upper()
        if verdict not in _VALID_VERDICTS:
            raise FeasibilityError(
                f"Layer 7a returned invalid feasibility_verdict '{verdict}' for {fer_id}. "
                f"Must be one of: {', '.join(sorted(_VALID_VERDICTS))}"
            )

        if not report.get("options"):
            raise FeasibilityError(
                f"Layer 7a response for {fer_id} contains no implementation options."
            )

        recommended = report.get("recommended_approach", "")
        available = {o.get("approach_name") for o in report.get("options", [])}
        if recommended and recommended not in available:
            log.warning(
                "Layer 7a recommended approach '%s' not in options list for %s. Options: %s",
                recommended,
                fer_id,
                available,
            )

    @staticmethod
    def _format_options(report: dict[str, Any]) -> str:
        """Format options list for human-readable CLI output."""
        lines = []
        recommended = report.get("recommended_approach", "")
        for opt in report.get("options", []):
            name = opt.get("approach_name", "")
            marker = " ← recommended" if name == recommended else ""
            lines.append(
                f"  [{opt.get('effort', '?')}] {name}{marker}\n"
                f"      {opt.get('description', '')}"
            )
        return "\n".join(lines)
