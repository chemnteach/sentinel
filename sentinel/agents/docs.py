"""
sentinel/agents/docs.py

Documentation Agent for Sentinel.

The Documentation Agent owns all generated documentation as a gate, not
an afterthought. Documentation is updated after every merged PR and before
PR creation (hard gate — PRs cannot be created without it).

Responsibilities:
  - Generate Mermaid architecture diagrams
  - Generate API contracts and module READMEs
  - Produce the Phase 1 assessment report
  - Update all affected documentation after code changes
  - Detect and flag documentation drift
  - Set doc_updated = True to satisfy the QA_PASS → DOC_UPDATED gate
"""

from __future__ import annotations

import logging
from typing import Any

from sentinel.agents.base import BaseAgent, AgentResponseError
from sentinel.api.client import ModelClient
from sentinel.config import Config
from sentinel.skills.loader import SkillLoader, Skills

log = logging.getLogger(__name__)

# The Documentation Agent uses the Cartographer and Dependency Analyzer
# skills for understanding what to document, and Quick Review for drift
# detection. It is the sole author of all generated documentation.
_DOC_SYSTEM_PROMPT_SUPPLEMENT = """
You are the Documentation Agent in the Sentinel multi-agent governance system.

Your output must be:
1. Accurate — reflecting the code as it is, not as it was intended
2. Complete — every public interface, module boundary, and data contract documented
3. Structured — Mermaid for diagrams, Markdown for prose, JSON for structured data
4. Actionable — a developer new to this codebase can understand the system from your docs alone

Documentation is a hard gate. No PR is created until you confirm doc_updated = True.
This is not optional. This is not an afterthought.
"""


class DocsAgent(BaseAgent):
    """
    Documentation Agent — documentation generation and drift detection.

    Uses multiple skills depending on the documentation task. The
    Orchestrator selects the appropriate skill context.
    """

    name = "documentation"

    def __init__(self, config: Config, client: ModelClient, loader: SkillLoader) -> None:
        super().__init__(config, client, loader)

    def _build_system_prompt(self, skill_name: str) -> str:
        """Append documentation gate reminder to every system prompt."""
        base = super()._build_system_prompt(skill_name)
        return f"{base}\n\n---\n{_DOC_SYSTEM_PROMPT_SUPPLEMENT}"

    def generate_assessment_report(self, assessment_context: str) -> str:
        """
        Generate the Phase 1 assessment report.

        Produces a comprehensive human-readable report covering:
          - Executive summary
          - Codebase overview
          - Critical findings
          - Full findings by severity
          - Tech debt register
          - Recommendations

        Returns the report as a Markdown string.
        """
        response = self.run(
            skill_name=Skills.CARTOGRAPHER,
            user_message=assessment_context,
            temperature=0.2,
        )
        return response.content

    def generate_module_readme(self, module_context: str) -> str:
        """
        Generate a README for a single module.

        Returns Markdown content for the module README.
        """
        response = self.run(
            skill_name=Skills.CARTOGRAPHER,
            user_message=module_context,
            temperature=0.2,
        )
        return response.content

    def generate_architecture_diagram(self, dependency_context: str) -> str:
        """
        Generate a Mermaid architecture diagram from the dependency analysis.

        Returns a Mermaid diagram string.
        """
        response = self.run(
            skill_name=Skills.DEPENDENCY_ANALYZER,
            user_message=dependency_context,
            temperature=0.1,
        )
        return response.content

    def generate_transition_manifest(self, context: str) -> dict[str, Any]:
        """
        Generate a per-task transition manifest at the QA_PASS -> DOC_UPDATED gate.

        Consumes the ``layer6c-transition-manifest-generator`` skill,
        which produces a release-note-style before/after narrative plus
        the two gate fields the doc-gate dispatches on.

        Behavioural change vs. the prior ``update_affected_docs`` method:
        the new schema demands five required keys (not the looser
        ``.get(..., False)`` shape). Any response missing a required
        key now raises ``AgentResponseError`` rather than being
        silently coerced. Gate-field types are coerced defensively
        (``docs_updated`` -> list, ``doc_updated`` -> bool).

        Args:
            context: Built by ``build_transition_manifest_context``.
                Contains the task record, the merged diff, and the
                contents of any ``.md`` files declared in
                ``task.affected_files``.

        Returns:
            One ``TransitionManifest`` dict with keys:
                - task_id: echoed from input
                - before_summary: prior-behaviour prose
                - after_summary: post-change-behaviour prose
                - docs_updated: list of doc file paths updated
                - doc_updated: bool (True = gate satisfied)

        Raises:
            AgentResponseError: When the model response is not parseable
                as JSON or is missing any of the required keys.
        """
        result = self.run_json(
            skill_name=Skills.TRANSITION_MANIFEST_GENERATOR,
            user_message=context,
            temperature=0.15,
        )

        required = {
            "task_id",
            "before_summary",
            "after_summary",
            "docs_updated",
            "doc_updated",
        }
        missing = required - set(result.keys())
        if missing:
            raise AgentResponseError(
                "Documentation Agent transition-manifest response missing "
                f"required keys: {sorted(missing)}. Got: {sorted(result.keys())}"
            )

        # Defensive coercion on gate fields.
        docs_updated = result.get("docs_updated")
        if not isinstance(docs_updated, list):
            log.warning(
                "Documentation Agent returned docs_updated of type %s; "
                "coercing to empty list",
                type(docs_updated).__name__,
            )
            result["docs_updated"] = []

        doc_updated = result.get("doc_updated")
        if not isinstance(doc_updated, bool):
            log.warning(
                "Documentation Agent returned doc_updated of type %s "
                "(value=%r); coercing to False (unknown gate state blocks PR)",
                type(doc_updated).__name__,
                doc_updated,
            )
            result["doc_updated"] = False

        # Preserve the gate-warning log: doc_updated == False blocks PR.
        if not result["doc_updated"]:
            log.warning(
                "Documentation Agent returned doc_updated=False -- "
                "this blocks PR creation. Review context and retry."
            )

        return result

    def detect_doc_drift(self, drift_context: str) -> dict[str, Any]:
        """
        Compare existing documentation against current code and flag mismatches.

        Returns:
            Dict with keys:
                - drifted_docs: list of files with outdated documentation
                - drift_severity: "high" | "medium" | "low"
                - recommended_updates: list of suggested changes

        Raises:
            AgentResponseError: When the model response is not parseable as
                JSON or is missing any of the required drift-report keys. The
                caller relies on all three keys being present; returning the
                raw ``run_json`` result unchecked would let a malformed drift
                report (e.g. missing ``drift_severity``) flow silently
                downstream and mask real documentation drift.
        """
        result = self.run_json(
            skill_name=Skills.QUICK_REVIEW,
            user_message=drift_context,
            temperature=0.15,
        )

        required = {"drifted_docs", "drift_severity", "recommended_updates"}
        missing = required - set(result.keys())
        if missing:
            raise AgentResponseError(
                "Documentation Agent drift-detection response missing required "
                f"keys: {sorted(missing)}. Got: {sorted(result.keys())}"
            )

        return result
