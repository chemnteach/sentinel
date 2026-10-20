"""
sentinel/agents/qa.py

QA Agent for Sentinel.

The QA Agent independently verifies fixes and TDD compliance. It does not
see the Architect's review findings before issuing its own verdict — it
works from acceptance criteria, the diff, and the approved test stubs.

Responsibilities:
  - Run Layer 6a (Verification Reviewer) on all fixes, including the
    mandatory TDD verification section (stub fidelity, test-first evidence,
    contract coverage)
  - Run Layer 6b (Cross-Repo Verification) for changes touching shared
    interfaces or cross-repo dependencies
  - Independently trigger the dispute protocol on TDD violations in Critical
    and P1 findings
  - Build position statements in the disagreement protocol
"""

from __future__ import annotations

import logging
from typing import Any

from sentinel.agents.base import BaseAgent, AgentResponseError
from sentinel.api.client import ModelClient
from sentinel.config import Config
from sentinel.skills.loader import SkillLoader, Skills

log = logging.getLogger(__name__)


class QAAgent(BaseAgent):
    """
    QA Agent — independent verification and TDD compliance.

    The QA Agent is the last automated gate before documentation and PR
    creation. It has authority to trigger the dispute protocol independently.
    """

    name = "qa"

    def __init__(self, config: Config, client: ModelClient, loader: SkillLoader) -> None:
        super().__init__(config, client, loader)

    def verify_fix(
        self,
        verification_context: str,
        skill_name: str = Skills.VERIFICATION,
    ) -> dict[str, Any]:
        """
        Run a verification skill (Layer 6a by default) on a completed fix.

        The verification context must include:
          - Original findings being addressed
          - Approved test stubs
          - Acceptance criteria
          - The code diff
          - Test results

        Parameters:
            verification_context: assembled context payload for the skill.
            skill_name: verification skill to invoke. Defaults to
                Skills.VERIFICATION (Layer 6a) for backward compatibility.
                The orchestrator threads ``task.verification_required[0]``
                through this parameter to honor per-task verification
                routing declared on the plan item.

        Returns dict with keys:
            - verdict: "PASS" | "FAIL"
            - tdd_assessment: dict (stub_fidelity, test_first_evidence,
              contract_coverage, overall_tdd_compliance)
            - completeness_check: dict
            - regression_check: dict
            - findings: list of any new issues discovered
            - feedback: str
        """
        result = self.run_json(
            skill_name=skill_name,
            user_message=verification_context,
            temperature=0.1,
        )

        # Case-insensitive lookup of the verdict key. The model occasionally
        # emits "Verdict"/"VERDICT"/" verdict " rather than the literal lower
        # "verdict"; a strict ``result["verdict"]`` check would miss those and
        # fall through to the missing-key error, discarding a real verdict.
        verdict_key = next(
            (k for k in result if str(k).strip().lower() == "verdict"), None
        )
        if verdict_key is not None and verdict_key != "verdict":
            result["verdict"] = result[verdict_key]

        # Normalize a verdict the model returned directly — as a clean string
        # ("CONDITIONAL PASS") or a structured dict ({"STATUS": "...", ...}).
        # CONDITIONAL PASS is a passing verdict per the Layer 6a skill. The
        # verification_verdict fallback below handles a missing verdict key.
        if result.get("verdict") is not None:
            verdict_value = result["verdict"]
            if isinstance(verdict_value, dict):
                status = (
                    verdict_value.get("STATUS")
                    or verdict_value.get("status")
                    or next(
                        (
                            v
                            for v in verdict_value.values()
                            if isinstance(v, str)
                            and ("PASS" in v.upper() or "FAIL" in v.upper())
                        ),
                        "",
                    )
                )
                result["verdict"] = status
            val = str(result["verdict"]).upper()
            if "FAIL" in val:
                result["verdict"] = "FAIL"
            elif "PASS" in val:
                result["verdict"] = "PASS"
            else:
                # A verdict value that contains neither PASS nor FAIL is an
                # unrecognized shape (e.g. "MAYBE", "ERROR", an empty dict).
                # Silently coercing it to FAIL destroys the signal that the
                # response was malformed and would mask a genuine parsing
                # problem behind a plausible-looking failing verdict. Raise so
                # the caller sees the malformed response rather than acting on
                # a fabricated FAIL.
                raise AgentResponseError(
                    "QA verification returned an unrecognized verdict "
                    f"(neither PASS nor FAIL): {result['verdict']!r}"
                )

        # Normalize occasional variant key styles from model responses.
        if "verdict" not in result:
            normalized: dict[str, Any] = {}
            for k, v in result.items():
                key = str(k).strip().lower().replace(" ", "_")
                normalized[key] = v

            if "verification_verdict" in normalized:
                val = str(normalized["verification_verdict"]).upper()
                if "PASS" in val:
                    normalized["verdict"] = "PASS"
                elif "FAIL" in val:
                    normalized["verdict"] = "FAIL"

            # Preserve original fields while adding normalized aliases.
            merged = dict(result)
            merged.update(normalized)
            result = merged

        if "verdict" not in result:
            raise AgentResponseError(
                f"QA verification missing 'verdict' key. "
                f"Got: {list(result.keys())}"
            )

        # Final defensive check: after all normalization the verdict must be one
        # of the recognized tokens. Anything else is a contract violation.
        final_verdict = str(result.get("verdict", "")).strip().upper()
        if final_verdict not in {"PASS", "FAIL"}:
            raise AgentResponseError(
                "QA verification produced a verdict outside the recognized set "
                f"{{PASS, FAIL}}: {result.get('verdict')!r}"
            )

        return result

    def verify_cross_repo(self, cross_repo_context: str) -> dict[str, Any]:
        """
        Run Layer 6b (Cross-Repo Verification) for changes touching
        shared interfaces, APIs, database schemas, or cross-repo dependencies.

        Returns dict with keys:
            - verdict: "PASS" | "FAIL"
            - api_contract_check: dict
            - schema_compatibility: dict
            - deployment_order: list[str]
            - affected_repos: list[str]
            - feedback: str
        """
        return self.run_json(
            skill_name=Skills.CROSS_REPO_VERIFY,
            user_message=cross_repo_context,
            temperature=0.1,
        )

    def build_position_statement(self, dispute_context: str) -> dict[str, Any]:
        """
        Build a QA position statement for the disagreement protocol.

        The QA Agent may independently initiate disputes when it disagrees
        with an Architect-approved fix or discovers TDD violations on
        Critical or P1 findings.
        """
        return self.run_json(
            skill_name=Skills.VERIFICATION,
            user_message=dispute_context,
            temperature=0.2,
        )

    def check_backwards_compat(self, compat_context: str) -> dict[str, Any]:
        """
        Run Layer 7c (Backwards Compatibility Checker) for a Phase 3 task.

        This is a mandatory gate between Architect REVIEW_PASS and QA
        verification for all Phase 3 (FER-derived) tasks.

        Args:
            compat_context: Task diff + scope + cross-repo dependency data.

        Returns:
            Dict with keys: verdict (PASS|FAIL), checks_performed,
            violations (breaking changes), warnings, cross_repo_impact,
            deployment_notes.

        Raises:
            AgentResponseError: If the response is missing required keys.
        """
        result = self.run_json(
            skill_name=Skills.COMPAT_CHECKER,
            user_message=compat_context,
            temperature=0.1,
        )

        if "verdict" not in result:
            raise AgentResponseError(
                f"Layer 7c response missing 'verdict' key. "
                f"Got: {list(result.keys())}"
            )

        return result
