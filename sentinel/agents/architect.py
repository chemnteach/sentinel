"""
sentinel/agents/architect.py

Architect Agent for Sentinel.

The Architect is the primary reviewer and plan authority. It executes all
review skills (Layers 2–5) and maintains plan fidelity. The Orchestrator
injects the appropriate skill; the Architect never self-selects skills.

Responsibilities:
  - Execute Layer 2–5 review skills as directed by the Orchestrator
  - Review TODO submissions — issuing two independent verdicts
  - Review code diffs with verdict: Approve, Reject, or Dispute
  - Generate the master remediation plan using the Layer 5 Fix Planner
  - Submit position statements in the disagreement protocol
"""

from __future__ import annotations

import logging
from typing import Any

from sentinel.agents.base import BaseAgent, AgentResponseError
from sentinel.api.client import ModelClient
from sentinel.config import Config
from sentinel.skills.loader import SkillLoader, Skills

log = logging.getLogger(__name__)


class ArchitectAgent(BaseAgent):
    """
    Architect Agent — primary reviewer and plan authority.

    All review verdicts pass through this agent. The Architect never
    approves its own work and does not see QA findings before issuing
    its own review.
    """

    name = "architect"

    def __init__(self, config: Config, client: ModelClient, loader: SkillLoader) -> None:
        super().__init__(config, client, loader)

    # ------------------------------------------------------------------
    # Phase 1 — Assessment
    # ------------------------------------------------------------------

    def map_codebase(self, repo_context: str) -> dict[str, Any]:
        """
        Run Layer 0a (Codebase Cartographer) to produce the repo manifest.

        Args:
            repo_context: Directory listing, file tree, and sample content.

        Returns:
            Parsed manifest dict (JSON from the model).
        """
        return self.run_json(
            skill_name=Skills.CARTOGRAPHER,
            user_message=repo_context,
            temperature=0.1,
        )

    def analyze_dependencies(self, manifest_context: str) -> dict[str, Any]:
        """
        Run Layer 0b (Dependency Analyzer) on the Stage 1 manifest.

        Args:
            manifest_context: JSON manifest from Stage 1 + relevant source.

        Returns:
            Parsed dependency analysis dict.
        """
        return self.run_json(
            skill_name=Skills.DEPENDENCY_ANALYZER,
            user_message=manifest_context,
            temperature=0.1,
            max_tokens=16384,
        )

    def prioritize_review(self, module_context: str) -> dict[str, Any]:
        """
        Run Layer 1 (Review Prioritizer) to classify a module and determine
        which Layer 2–4 skills to apply.

        Returns:
            Dict with keys: criticality, type, scope, risk_factors,
            recommended_skills, estimated_minutes.
        """
        return self.run_json(
            skill_name=Skills.REVIEW_PRIORITIZER,
            user_message=module_context,
            temperature=0.1,
        )

    def quick_review(self, module_context: str) -> dict[str, Any]:
        """
        Run Layer 2a (Quick Review) on a code module.

        Quick Review runs on every module unconditionally. Returns structured
        findings with Critical/P1/P2/P3 severity classification.
        """
        return self.run_json(
            skill_name=Skills.QUICK_REVIEW,
            user_message=module_context,
            temperature=0.15,
        )

    def specialist_review(self, skill_name: str, module_context: str) -> dict[str, Any]:
        """
        Run a specialist review skill (2b–2d, 3a–3e, 4a–4b) on a module.

        Args:
            skill_name: The skill constant from Skills (e.g. Skills.SECURITY).
            module_context: Module source with Quick Review findings prepended.

        Returns:
            Parsed specialist findings dict.
        """
        return self.run_json(
            skill_name=skill_name,
            user_message=module_context,
            temperature=0.15,
        )

    def build_remediation_plan(self, synthesis_context: str) -> dict[str, Any]:
        """
        Run Layer 5 (Fix Planner) to produce the master remediation plan.

        Args:
            synthesis_context: All accumulated findings from Phase 1.

        Returns:
            Structured remediation plan:
            blocking → required → desired → nice-to-have, with batching,
            sequencing, effort estimates, and verification criteria.
        """
        return self.run_json(
            skill_name=Skills.FIX_PLANNER,
            user_message=synthesis_context,
            temperature=0.1,
            max_tokens=16384,
        )

    def classify_findings(self, context: str) -> dict:
        """Run the verifiability gate on a batch of findings. Returns a dict
        with a 'classifications' list, each entry having finding_id, verdict,
        rationale, precondition_holds, test_writable.
        """
        return self.run_json(
            skill_name=Skills.VERIFIABILITY_GATE,
            user_message=context,
            temperature=0.1,
            max_tokens=16384,
        )

    def check_acceptance_criteria(self, context: str) -> dict:
        """Run the acceptance-criteria completeness gate on a completed task's
        diff. Returns a dict with criteria_results, overall_verdict, and
        premise_mismatch_flag.
        """
        return self.run_json(
            skill_name=Skills.ACCEPTANCE_CRITERIA_GATE,
            user_message=context,
            temperature=0.1,
            max_tokens=8192,
        )

    # ------------------------------------------------------------------
    # Phase 1.5 — Pre-planning Remediation Patterns (Stage 5b)
    # ------------------------------------------------------------------

    def propose_remediation_pattern(
        self, group_context: str
    ) -> dict[str, Any]:
        """
        Stage 5b: propose a single RemediationPattern for one
        SemanticGroup of deduped findings, before tech-debt triage
        and before the Fix Planner runs.

        Args:
            group_context: Full user_message assembled by
                ``orchestrator.context.build_remediation_pattern_context``
                (group record, member findings, canonical persona
                excerpt, peer one-liners, silent-peer guidance).

        Returns:
            One ``RemediationPattern`` dict as defined by the
            ``layer5b-remediation-pattern-proposer`` skill. ``pattern_id``
            and ``group_id`` are echoed from input; ``pattern_kind`` is a
            free-form kebab-case tag chosen by the model.

        Raises:
            AgentResponseError: When the model response is not parseable
                as JSON or is missing any of the required keys.
        """
        result = self.run_json(
            skill_name=Skills.REMEDIATION_PATTERN_PROPOSER,
            user_message=group_context,
            temperature=0.1,
        )

        required = {
            "pattern_id",
            "group_id",
            "pattern_kind",
            "title",
            "rationale",
            "proposed_changes",
            "acceptance_criteria",
            "scope_signals",
            "confidence",
            "notes",
        }
        missing = required - set(result.keys())
        if missing:
            raise AgentResponseError(
                "Architect remediation-pattern response missing required "
                f"keys: {sorted(missing)}. Got: {sorted(result.keys())}"
            )

        # `notes` must always be a dict so downstream consumers (Fix Planner)
        # can read object subfields. The model may emit null, a string, or
        # another non-dict value; coerce those to an empty dict rather than
        # letting a malformed value flow downstream (FIX-BLOCKING-089).
        notes = result.get("notes")
        if not isinstance(notes, dict):
            notes = {}
            result["notes"] = notes
        notes.setdefault("silent_peer_notes", "")
        notes.setdefault("canonical_emitter_caveats", "")

        return result

    # ------------------------------------------------------------------
    # Phase 1.5 — Automated Finding Verification (Stage 5c)
    # ------------------------------------------------------------------

    def verify_finding(self, finding_context: str) -> dict[str, Any]:
        """
        Stage 5c: verify a batch of findings that cite one source file.

        The batch context carries the file path, its detected language, and
        one or more findings (each with title/kind/description/location plus
        the source at the cited location). The model adjudicates every finding
        against the provided source and returns a per-finding verdict.

        Args:
            finding_context: The batch user_message assembled by
                ``Orchestrator._run_finding_verification`` — one file's path +
                language + its findings, each with the cited source snippet.

        Returns:
            A dict with a ``verifications`` list, one element per finding in
            the batch. Each element carries at least ``verdict``,
            ``confidence``, ``evidence``, ``actual_line_start``,
            ``severity_assessment``, and ``recommended_severity``; ``index``
            echoes the ``Finding N`` number so results can be matched back.

        Raises:
            AgentResponseError: When the response is not parseable as JSON,
                ``verifications`` is missing/empty/not a list, or any element
                lacks a required key.
        """
        result = self.run_json(
            skill_name=Skills.FINDING_VERIFIER,
            user_message=finding_context,
            temperature=0.1,
        )

        verifications = result.get("verifications")
        if not isinstance(verifications, list) or not verifications:
            raise AgentResponseError(
                "Architect finding-verification response missing a non-empty "
                f"'verifications' list. Got: {sorted(result.keys())}"
            )

        required = {
            "verdict",
            "confidence",
            "evidence",
            "actual_line_start",
            "severity_assessment",
            "recommended_severity",
        }
        for i, element in enumerate(verifications):
            if not isinstance(element, dict):
                raise AgentResponseError(
                    "Architect finding-verification element "
                    f"{i} is not an object: {element!r}"
                )
            missing = required - set(element.keys())
            if missing:
                raise AgentResponseError(
                    "Architect finding-verification element "
                    f"{i} missing required keys: {sorted(missing)}. "
                    f"Got: {sorted(element.keys())}"
                )

        return result

    # ------------------------------------------------------------------
    # Phase 1.6 — Alarm Relevance Classification (post-verification)
    # ------------------------------------------------------------------

    def classify_alarm_relevance(self, classification_context: str) -> dict[str, Any]:
        """Classify a batch of findings by ALARM-subsystem relevance.

        The batch context carries a JSON array of findings (index, title,
        finding_kind, description, file, seed_relevance). The model labels each
        one ``direct`` | ``adjacent`` | ``none`` with a one-line rationale.

        Args:
            classification_context: The batch user_message assembled by
                ``Orchestrator._run_alarm_classification`` — one JSON array of
                findings plus their deterministic seed labels.

        Returns:
            A dict with a ``classifications`` list, one element per finding.
            Each element carries ``index``, ``alarm_relevance`` (one of
            ``direct``/``adjacent``/``none``), and ``alarm_rationale``.

        Raises:
            AgentResponseError: When the response is not parseable as JSON,
                ``classifications`` is missing/empty/not a list, any element
                lacks a required key, or a relevance value is out of range.
        """
        result = self.run_json(
            skill_name=Skills.ALARM_CLASSIFIER,
            user_message=classification_context,
            temperature=0.1,
        )

        classifications = result.get("classifications")
        if not isinstance(classifications, list) or not classifications:
            raise AgentResponseError(
                "Architect alarm-classification response missing a non-empty "
                f"'classifications' list. Got: {sorted(result.keys())}"
            )

        allowed = {"direct", "adjacent", "none"}
        required = {"index", "alarm_relevance", "alarm_rationale"}
        for i, element in enumerate(classifications):
            if not isinstance(element, dict):
                raise AgentResponseError(
                    f"Architect alarm-classification element {i} is not an "
                    f"object: {element!r}"
                )
            missing = required - set(element.keys())
            if missing:
                raise AgentResponseError(
                    f"Architect alarm-classification element {i} missing "
                    f"required keys: {sorted(missing)}. Got: {sorted(element.keys())}"
                )
            if str(element.get("alarm_relevance", "")).strip().lower() not in allowed:
                raise AgentResponseError(
                    f"Architect alarm-classification element {i} has an "
                    f"out-of-range alarm_relevance: {element.get('alarm_relevance')!r}"
                )

        return result

    # ------------------------------------------------------------------
    # Phase 2 — Governed Remediation
    # ------------------------------------------------------------------

    def review_todo(self, todo_context: str) -> dict[str, Any]:
        """
        Review a Coding Agent's TODO submission.

        Returns two independent verdicts:
            - implementation_plan_verdict: "APPROVE" | "REVISE"
            - implementation_plan_feedback: str
            - test_stubs_verdict: "APPROVE" | "REVISE"
            - test_stubs_feedback: str

        Both must be "APPROVE" before IN_PROGRESS is unblocked.

        Uses the Review Prioritizer skill since TODO review involves
        architectural and test design judgement.
        """
        # Closed set of accepted verdict tokens. Only these (after
        # whitespace/case normalization) may yield approval; anything else
        # collapses to the non-approval verdict so a malformed or
        # out-of-set value cannot unblock a TODO.
        allowed_verdicts = {"APPROVE", "REVISE"}

        # TODO reviews use the quick_review skill as a base but with
        # special context. The Orchestrator builds the full context block
        # including the formatted TODO via format_todo_for_architect().
        try:
            result = self.run_json(
                skill_name=Skills.QUICK_REVIEW,
                user_message=todo_context,
                temperature=0.1,
            )
        except AgentResponseError as exc:
            # A response we cannot parse as a structured verdict is a
            # non-approval. We deliberately do NOT infer approval from
            # substring matches on free-form text: negative or
            # contradictory responses such as "not approved",
            # "looks good except for a critical issue", or
            # "no P1, but revise" all contain an approval-like substring
            # and would otherwise be wrongly converted into APPROVE,
            # unblocking an explicitly rejected TODO.
            feedback = (
                "Architect TODO review could not be parsed as a structured "
                f"verdict; defaulting to REVISE (non-approval). Cause: {exc}"
            )
            return {
                "implementation_plan_verdict": "REVISE",
                "implementation_plan_feedback": feedback,
                "test_stubs_verdict": "REVISE",
                "test_stubs_feedback": feedback,
            }

        if not isinstance(result, dict):
            feedback = (
                "Architect TODO review returned a malformed structured payload; "
                "defaulting to REVISE (non-approval). "
                f"Got type: {type(result).__name__}."
            )
            return {
                "implementation_plan_verdict": "REVISE",
                "implementation_plan_feedback": feedback,
                "test_stubs_verdict": "REVISE",
                "test_stubs_feedback": feedback,
            }

        # Because this method runs the Quick Review skill, the model may reply
        # in Quick Review's own shape (``highest_priority`` + ``findings``)
        # rather than the four TODO-review fields this method documents. When
        # the dedicated verdict fields are absent, derive them from the Quick
        # Review signal so a valid Quick Review response fulfills the documented
        # contract instead of failing the required-keys check below. Critical/P1
        # findings mean REVISE (fail-closed); anything lower means APPROVE.
        if (
            "implementation_plan_verdict" not in result
            and "test_stubs_verdict" not in result
            and "highest_priority" in result
        ):
            highest = str(result.get("highest_priority", "")).strip().upper()
            derived_verdict = "REVISE" if highest in ("CRITICAL", "P1") else "APPROVE"
            derived_feedback = self._summarize_quick_review_findings(result)
            result.setdefault("implementation_plan_verdict", derived_verdict)
            result.setdefault("test_stubs_verdict", derived_verdict)
            result.setdefault("implementation_plan_feedback", derived_feedback)
            result.setdefault("test_stubs_feedback", derived_feedback)

        # Validate expected structure
        required = {
            "implementation_plan_verdict",
            "test_stubs_verdict",
        }
        missing = required - set(result.keys())
        if missing:
            raise AgentResponseError(
                f"Architect TODO review missing required keys: {missing}. "
                f"Got: {list(result.keys())}"
            )

        # Strict verdict normalization: accept only explicit allowed
        # tokens (case/whitespace-insensitive). Any value outside the
        # closed set — including contradictory free-form strings — becomes
        # REVISE rather than an optimistic approval.
        for key in ("implementation_plan_verdict", "test_stubs_verdict"):
            normalized = str(result.get(key, "")).strip().upper()
            result[key] = normalized if normalized in allowed_verdicts else "REVISE"

        # Guarantee the two documented feedback fields are always present so
        # downstream consumers never KeyError on a valid but terse response.
        result.setdefault("implementation_plan_feedback", "")
        result.setdefault("test_stubs_feedback", "")

        return result

    @staticmethod
    def _summarize_quick_review_findings(result: dict[str, Any]) -> str:
        """Build a short feedback string from a Quick Review result's findings.

        Used when ``review_todo`` receives a Quick-Review-shaped response and
        must synthesize the documented feedback fields. Returns an empty string
        when there are no usable findings.
        """
        findings = result.get("findings")
        if not isinstance(findings, list) or not findings:
            return ""
        parts: list[str] = []
        for f in findings:
            if isinstance(f, dict):
                text = f.get("description") or f.get("issue") or f.get("title") or ""
            else:
                text = str(f)
            text = str(text).strip()
            if text:
                parts.append(text)
        return "; ".join(parts[:5])

    def review_diff(
        self,
        skill_name: str,
        diff_context: str,
    ) -> dict[str, Any]:
        """
        Review a Coding Agent's code diff.

        Args:
            skill_name: Skill determined by Layer 1 for this diff.
            diff_context: The diff, task context, and acceptance criteria.

        Returns:
            Dict with keys: verdict ("APPROVE"|"REJECT"|"DISPUTE"),
            findings, feedback, gate_checks_passed.
        """
        try:
            result = self.run_json(
                skill_name=skill_name,
                user_message=diff_context,
                temperature=0.1,
            )
        except AgentResponseError:
            response = self.run(
                skill_name=skill_name,
                user_message=diff_context,
                temperature=0.1,
            )
            lowered = response.content.lower()
            if "dispute" in lowered:
                verdict = "DISPUTE"
            elif any(k in lowered for k in ("reject", "fail", "must fix")):
                verdict = "REJECT"
            else:
                # A response we cannot parse as an explicit approval is a
                # non-approval. Mirror review_todo's fail-closed contract:
                # ambiguous or unrecognised text must not unblock a diff gate.
                verdict = "REJECT"

            result = {
                "verdict": verdict,
                "findings": [],
                "feedback": response.content,
                "gate_checks_passed": verdict == "APPROVE",
            }

        if "verdict" not in result:
            highest = result.get("highest_priority", "").upper()
            if highest in ("CRITICAL", "P1"):
                result["verdict"] = "REJECT"
            else:
                # Missing or unrecognised verdict with no explicit Critical/P1
                # signal: fail closed. The model must supply an explicit
                # verdict=APPROVE to unblock this gate.
                result["verdict"] = "REJECT"

        # Normalize the verdict to the closed set {APPROVE, REJECT, DISPUTE}
        # (case/whitespace-insensitive). Any out-of-set token fails closed to
        # REJECT so a malformed value cannot unblock the gate, and
        # gate_checks_passed is always derived from the normalized verdict so
        # the two can never disagree (FIX-BLOCKING-088).
        normalized_verdict = str(result.get("verdict", "")).strip().upper()
        if normalized_verdict not in {"APPROVE", "REJECT", "DISPUTE"}:
            normalized_verdict = "REJECT"
        result["verdict"] = normalized_verdict
        result["gate_checks_passed"] = normalized_verdict == "APPROVE"

        if "feedback" not in result:
            result["feedback"] = result.get("quick_review_summary", "")

        return result

    def build_position_statement(self, dispute_context: str) -> dict[str, Any]:
        """
        Build a position statement for the disagreement protocol.

        Returns:
            Dict with keys: issue, approach, evidence, impact_assessment,
            risk_of_alternative, proposed_compromise.
        """
        return self.run_json(
            skill_name=Skills.QUICK_REVIEW,
            user_message=dispute_context,
            temperature=0.2,
        )

    # ------------------------------------------------------------------
    # Phase 3 — Feature Enhancement
    # ------------------------------------------------------------------

    def assess_feasibility(self, fer_context: str) -> dict[str, Any]:
        """
        Run Layer 7a (Feature Feasibility Analyzer) for a FER.

        Args:
            fer_context: FER details + codebase manifest from Phase 1.

        Returns:
            Structured feasibility report with verdict, effort decomposition,
            risk classification, options analysis, and recommended approach.
        """
        return self.run_json(
            skill_name=Skills.FEASIBILITY_ANALYZER,
            user_message=fer_context,
            temperature=0.1,
        )

    def write_enhancement_spec(self, spec_context: str) -> dict[str, Any]:
        """
        Run Layer 7b (Enhancement Specification Writer) for an approved FER.

        Args:
            spec_context: FER + feasibility report + selected approach.

        Returns:
            Detailed implementation spec with acceptance criteria in
            Given/When/Then format, API contracts, data model, scope
            boundary, test strategy, and test stubs.
        """
        return self.run_json(
            skill_name=Skills.SPEC_WRITER,
            user_message=spec_context,
            temperature=0.1,
        )

    # ------------------------------------------------------------------
    # Layer 8 — Defect RCA & Governed Bug Remediation
    # ------------------------------------------------------------------

    def analyze_root_cause(self, rca_context: str) -> dict[str, Any]:
        """
        Run Layer 8a (Root Cause Analyzer) for an ingested defect report.

        Args:
            rca_context: Sliced code, stack traces, and symptom details.

        Returns:
            Structured 3-tier causal decomposition report and remediation options.
        """
        return self.run_json(
            skill_name=Skills.ROOT_CAUSE_ANALYZER,
            user_message=rca_context,
            temperature=0.1,
        )

    def write_bug_spec(self, bug_spec_context: str) -> dict[str, Any]:
        """
        Run Layer 8b (Bug Remediation Spec Writer) for an RCA report.

        Args:
            bug_spec_context: RCA report + chosen remediation strategy + code slice.

        Returns:
            Remediation specification with unified diff, reproduction test stubs,
            and task definitions.
        """
        return self.run_json(
            skill_name=Skills.BUG_SPEC_WRITER,
            user_message=bug_spec_context,
            temperature=0.1,
        )
