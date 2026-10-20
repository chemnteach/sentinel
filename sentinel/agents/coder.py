"""
sentinel/agents/coder.py

Coding Agent for Sentinel.

The Coding Agent implements changes only. Layer 0c (Scope Guardian) is
permanently active — it enforces both surgical-changes-only discipline
and test-first implementation order.

Responsibilities:
  - Submit TODOs containing an implementation plan AND test stubs
  - Implement test stubs first (red), then implementation (green)
  - Self-review using Layer 2a before submitting the diff
  - Submit position statements in the disagreement protocol

The Coding Agent never reviews other agents' work and never approves
its own submissions. It does not see Architect review findings before
they are relayed by the Orchestrator.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sentinel.agents.base import BaseAgent, AgentResponseError
from sentinel.api.client import ModelClient
from sentinel.config import Config
from sentinel.skills.loader import SkillLoader, Skills

log = logging.getLogger(__name__)

# Max Coder implementation attempts: the first single-shot pass plus up to two
# retries that feed the failing test output back into the model (cycle — inner
# test-fix loop). The working tree is rolled back to clean source between
# attempts so each retry re-applies against the original code.
_MAX_IMPL_ATTEMPTS = 3

# Retry temperature ladder: attempt 1 stays deterministic (0.15) for a precise
# first shot; retries raise temperature so the model explores a different
# approach instead of repeating the same failing edit.
_RETRY_TEMPERATURES = {2: 0.4, 3: 0.7}

# --- T-1: enriched retry-failure feedback ------------------------------------
# On a failed implementation attempt the model is re-prompted with a structured
# bundle (re-presented post-rollback source + parsed expected/actual values +
# prior-attempt summaries + a forced diagnosis sentence) instead of a raw pytest
# tail. These are pure helpers so they can be unit-tested in isolation. No new
# API calls: the bundle only restructures the string fed to the existing retry.

_ASSERT_EQ_RE = re.compile(r"assert\s+(.+?)\s*==\s*(.+)")
_ASSERT_RE = re.compile(r"assert\s+(.+)")
_DEF_RE = re.compile(r"\bdef\s+(\w+)\s*\(")


def _parse_expected_actual(test_output: str) -> tuple[str | None, str | None]:
    """Extract ``(actual, expected)`` from a pytest assertion in ``test_output``.

    Recognises ``assert X == Y`` (actual=X, expected=Y) and bare ``assert X``
    (actual=X, expected=None), tolerating pytest's ``E``/``>`` line prefixes and
    an ``AssertionError:`` lead-in. Returns ``(None, None)`` when no assertion
    line is present.
    """
    def _clean(line: str) -> str:
        s = line.strip()
        # Skip pytest's '>' source-context line — it shows the assertion
        # *expression* (e.g. ``compute() == 4``), not the *evaluated* values
        # (``3 == 4``) that live on the following ``E`` line.
        if s.startswith(">"):
            return ""
        s = re.sub(r"^E\s+", "", s)
        return s.replace("AssertionError:", "").strip()

    for line in test_output.splitlines():
        m = _ASSERT_EQ_RE.search(_clean(line))
        if m:
            return m.group(1).strip(), m.group(2).strip()
    for line in test_output.splitlines():
        m = _ASSERT_RE.match(_clean(line))
        if m:
            return m.group(1).strip(), None
    return None, None


def _build_what_went_wrong(test_output: str) -> str:
    """Build the ``## What went wrong`` section from pytest output.

    Prefers parsed actual/expected values; falls back to the first ``FAILED``
    line when no assertion is present.
    """
    actual, expected = _parse_expected_actual(test_output)
    if actual is not None:
        exp = expected if expected is not None else "(assertion expected a truthy value)"
        return f"## What went wrong\n\nactual:   {actual}\nexpected: {exp}"
    for line in test_output.splitlines():
        if "FAILED" in line:
            return f"## What went wrong\n\n{line.strip()}"
    return "## What went wrong\n\n(no assertion or FAILED line found in test output)"


def _summarize_attempt(diff_summary: str, had_changes: bool) -> str:
    """Summarise a prior implementation attempt in <=2 lines.

    Returns ``"no changes applied"`` when the attempt produced no file changes;
    otherwise the first two non-empty lines of its ``diff_summary``.
    """
    if not had_changes:
        return "no changes applied"
    lines = [l.strip() for l in (diff_summary or "").splitlines() if l.strip()]
    if not lines:
        return "applied changes (no diff summary provided)"
    return "\n".join(lines[:2])


def _function_from_edit(edit: dict) -> str:
    """Best-effort function name from an edit's text, for function-aware source
    retrieval. Empty string => retrieval falls back to a capped whole-file view."""
    text = f"{edit.get('old_text', '') or ''}\n{edit.get('new_text', '') or ''}"
    m = _DEF_RE.search(text)
    return m.group(1) if m else ""


def _present_current_source(edits: list[dict], repo_root: str) -> str:
    """Re-read the current (post-rollback) source of each edited file.

    Uses the function-aware retrieval in ``source_retrieval`` (degrades to a
    capped whole-file view when no function can be identified). Returns an empty
    string when there is nothing to show, so callers degrade gracefully.
    """
    try:
        from sentinel.orchestrator.source_retrieval import get_source_for_finding

        seen: list[str] = []
        parts: list[str] = []
        for edit in (edits or []):
            file_field = (edit or {}).get("file", "")
            if not file_field or file_field in seen:
                continue
            seen.append(file_field)
            parts.append(
                get_source_for_finding(
                    {"file": file_field, "function": _function_from_edit(edit)},
                    repo_root,
                )
            )
        return "\n\n".join(parts)
    except Exception as exc:  # never let source re-presentation break a retry
        log.warning("retry source re-presentation failed: %s", exc)
        return ""


def _build_retry_feedback(
    source_section: str,
    what_went_wrong: str,
    attempt_summaries: list[str],
    raw_tail: str,
) -> str:
    """Assemble the enriched retry-failure bundle.

    Sections: re-presented current source, parsed expected/actual, prior-attempt
    summaries, a forced diagnosis sentence, and the raw pytest tail (kept as a
    fallback after the structured bundle). Sections with no content are omitted.
    """
    blocks = ["## Previous Implementation Failed"]
    if source_section:
        blocks.append(f"## Current source (post-rollback)\n\n{source_section}")
    blocks.append(what_went_wrong)
    if attempt_summaries:
        blocks.append(
            "## What was tried\n\n"
            + "\n".join(
                f"Attempt {i}: {s}"
                for i, s in enumerate(attempt_summaries, start=1)
            )
        )
    blocks.append(
        "Before writing any code, complete this sentence in one line:\n"
        "'The previous patch failed because [root cause], so the correct fix "
        "must [what needs to change].'"
    )
    if raw_tail:
        blocks.append(f"## Raw test output (tail)\n\n```\n{raw_tail[:8000]}\n```")
    return "\n\n".join(blocks)


class CoderAgent(BaseAgent):
    """
    Coding Agent — implements approved changes test-first.

    Scope Guardian (Layer 0c) is injected into every system prompt.
    The Orchestrator validates TODO submissions before they reach the
    Architect.
    """

    name = "coder"

    def __init__(self, config: Config, client: ModelClient, loader: SkillLoader) -> None:
        super().__init__(config, client, loader)

    def _build_system_prompt(self, skill_name: str) -> str:
        """
        Prepend Scope Guardian to every Coder system prompt.

        Layer 0c is always active for the Coding Agent. It cannot be
        disabled or overridden. Other skills are appended after it.
        """
        try:
            scope_guardian = self._loader.load(Skills.SCOPE_GUARDIAN)
        except Exception as exc:
            log.warning("Scope Guardian skill not found, using fallback: %s", exc)
            scope_guardian = "SCOPE GUARDIAN: Modify only files explicitly listed in your task scope."

        if skill_name == Skills.SCOPE_GUARDIAN:
            # Avoid double-injection
            return super()._build_system_prompt(skill_name)

        try:
            skill_body = self._loader.load(skill_name)
        except Exception:
            raise

        header = self._agent_header()
        return f"{header}\n\n## Active Layer: Scope Guardian (always on)\n\n{scope_guardian}\n\n---\n\n{skill_body}"

    def submit_todo(self, task_context: str) -> dict[str, Any]:
        """
        Generate a TODO submission for a task assignment.

        The Orchestrator will validate the returned JSON against the
        TodoSubmission schema before it reaches the Architect.

        Returns:
            Dict matching the TodoSubmission schema:
            task_id, implementation_plan, test_stubs, out_of_scope_findings.
        """
        result = self.run_json(
            skill_name=Skills.SCOPE_GUARDIAN,
            user_message=task_context,
            temperature=0.15,
            max_tokens=16384,
        )

        required = {"task_id", "implementation_plan", "test_stubs"}
        missing = required - set(result.keys())
        if missing:
            raise AgentResponseError(
                f"Coder TODO submission missing required keys: {missing}. "
                f"Orchestrator will reject this. Got: {list(result.keys())}"
            )

        return result

    def implement(self, implementation_context: str) -> dict[str, Any]:
        """
        Implement the approved task (cycle 56 — execution layer).

        1. Call the Coder LLM for structured edit operations + a test file.
        2. Apply the edits to disk (``apply_edits``).
        3. Write the test file to disk (``write_test_file``).
        4. Run the real test suite (``run_tests`` — subprocess).
        5. On failure, roll back the edits so the tree is clean for revision.

        Returns ``{files_changed, diff_summary, tests_pass, test_results,
        test_summary, edits, edit_errors}`` — ``tests_pass`` reflects the real
        runner, not a model claim.

        In ``local_only_mode`` no disk writes or test runs occur: the model's
        JSON is returned as-is (preserves offline / dry-run behavior).
        """
        try:
            result = self.run_json(
                skill_name=Skills.SCOPE_GUARDIAN,
                user_message=implementation_context,
                temperature=0.15,
                max_tokens=16384,
            )
        except AgentResponseError as exc:
            return {
                "files_changed": [],
                "diff_summary": str(exc),
                "tests_pass": False,
                "test_results": "",
                "edits": [],
                "edit_errors": [str(exc)],
            }

        # Offline / dry-run: never touch disk or spawn a test process.
        if getattr(self._config, "local_only_mode", False):
            result.setdefault("files_changed", [])
            result.setdefault("tests_pass", True)
            return result

        from sentinel.workflow.remediation.apply import (
            EditApplicationError,
            apply_edits,
            rollback_edits,
            write_test_file,
        )
        from sentinel.workflow.remediation.test_runner import run_tests

        edits = result.get("edits", []) or []
        test_file = result.get("test_file", "")
        test_content = result.get("test_content", "")
        diff_summary = result.get("diff_summary", "")
        repo_root = str(self._config.repo_path)
        modified_files: list[str] = []
        test_results: dict[str, Any] = {
            "passed": False,
            "output": "Implementation failed before tests were executed.",
            "summary": "failed before tests",
        }

        # Apply edits to disk. If old_text doesn't match (hallucinated code),
        # stop before tests and report the failure for revision.
        if edits:
            try:
                modified_files = apply_edits(edits, repo_root)
            except EditApplicationError as exc:
                log.warning("Edit application failed: %s", exc)
                return {
                    "files_changed": [],
                    "diff_summary": diff_summary,
                    "tests_pass": False,
                    "test_results": f"Edit failed: {exc}",
                    "edits": edits,
                    "edit_errors": [str(exc)],
                }

        # Write the test file. Track the path write_test_file actually wrote
        # to (it may coerce a non-tests/ path), not the original — otherwise
        # the scoped commit targets a path that does not exist and drops the test.
        # apply_edits above is protected by rollback; the test write must be too,
        # or a failure here leaves the code edits applied but the tree in a
        # partial state. On failure, roll back the applied edits and surface it.
        if test_file and test_content:
            try:
                written_path = write_test_file(test_file, test_content, repo_root)
            except Exception as e:
                rollback_edits(modified_files, repo_root)
                raise RuntimeError(f"test file write failed: {e}") from e
            actual_path = written_path or test_file
            if actual_path not in modified_files:
                modified_files.append(actual_path)

        # Run tests; on failure, feed the failing output back to the Coder and
        # retry (up to _MAX_IMPL_ATTEMPTS), rolling back to clean source between
        # attempts so each retry re-applies against the original code. Attempt 1
        # uses the edits/test file already applied above; attempts 2+ re-prompt.
        failure_feedback = ""
        attempt_summaries: list[str] = []
        for attempt in range(1, _MAX_IMPL_ATTEMPTS + 1):
            if attempt > 1:
                # T-1: enriched retry bundle — re-presented post-rollback source
                # (of the files the prior attempt edited), parsed expected/actual,
                # prior-attempt summaries, and a forced diagnosis sentence. The
                # raw pytest tail is kept as a fallback inside the bundle.
                retry_context = (
                    implementation_context
                    + "\n\n"
                    + _build_retry_feedback(
                        source_section=_present_current_source(edits, repo_root),
                        what_went_wrong=_build_what_went_wrong(failure_feedback),
                        attempt_summaries=attempt_summaries,
                        raw_tail=failure_feedback,
                    )
                )
                try:
                    result = self.run_json(
                        skill_name=Skills.SCOPE_GUARDIAN,
                        user_message=retry_context,
                        temperature=_RETRY_TEMPERATURES.get(attempt, 0.7),
                        max_tokens=16384,
                    )
                except AgentResponseError as exc:
                    log.warning(
                        "Impl retry %d/%d: model error: %s",
                        attempt, _MAX_IMPL_ATTEMPTS, exc,
                    )
                    test_results = {
                        "passed": False,
                        "output": (
                            f"Impl retry {attempt}/{_MAX_IMPL_ATTEMPTS}: "
                            f"model error: {exc}"
                        ),
                        "summary": "retry setup failed before tests",
                    }
                    break

                edits = result.get("edits", []) or []
                test_file = result.get("test_file", "")
                test_content = result.get("test_content", "")
                diff_summary = result.get("diff_summary", "")

                if edits:
                    try:
                        modified_files = apply_edits(edits, repo_root)
                    except EditApplicationError as exc:
                        log.warning(
                            "Impl retry %d/%d: edit failed: %s",
                            attempt, _MAX_IMPL_ATTEMPTS, exc,
                        )
                        test_results = {
                            "passed": False,
                            "output": (
                                f"Impl retry {attempt}/{_MAX_IMPL_ATTEMPTS}: "
                                f"edit failed: {exc}"
                            ),
                            "summary": "retry setup failed before tests",
                        }
                        modified_files = []
                        break

                if test_file and test_content:
                    try:
                        written_path = write_test_file(
                            test_file, test_content, repo_root
                        )
                    except Exception as e:
                        rollback_edits(modified_files, repo_root)
                        raise RuntimeError(
                            f"test file write failed: {e}"
                        ) from e
                    actual_path = written_path or test_file
                    if actual_path not in modified_files:
                        modified_files.append(actual_path)

            test_results = run_tests(
                repo_root=repo_root,
                test_command=self._config.test_command or None,
                timeout=self._config.test_timeout,
            )

            if test_results["passed"]:
                log.info(
                    "Tests passed on impl attempt %d/%d",
                    attempt, _MAX_IMPL_ATTEMPTS,
                )
                break  # success

            # Tests failed — capture output, roll back to clean source, retry.
            failure_feedback = test_results["output"]
            had_changes = bool(modified_files)
            log.warning(
                "Tests failed on impl attempt %d/%d — rolling back %d file(s)",
                attempt, _MAX_IMPL_ATTEMPTS, len(modified_files),
            )
            rollback_failed = rollback_edits(modified_files, repo_root)
            if rollback_failed:
                log.warning(
                    "Rollback incomplete on impl attempt %d/%d — %d file(s) not "
                    "restored: %s",
                    attempt, _MAX_IMPL_ATTEMPTS, len(rollback_failed),
                    ", ".join(rollback_failed),
                )
            modified_files = []
            # T-1: record this attempt's approach for the next retry's bundle.
            attempt_summaries.append(_summarize_attempt(diff_summary, had_changes))

            if attempt == _MAX_IMPL_ATTEMPTS:
                log.warning(
                    "All %d impl attempts failed — giving up",
                    _MAX_IMPL_ATTEMPTS,
                )

        return {
            "files_changed": modified_files,
            "diff_summary": diff_summary,
            "tests_pass": test_results["passed"],
            "test_results": test_results["output"],
            "test_summary": test_results["summary"],
            "edits": edits,
            "edit_errors": [],
        }

    def self_review(self, diff_context: str) -> dict[str, Any]:
        """
        Self-review the diff using Layer 2a (Quick Review) before submitting.

        The Coding Agent must complete a Quick Review before submission.
        Any Critical or P1 findings from self-review must be resolved before
        the Orchestrator accepts the submission.

        Returns:
            Quick Review findings dict.
        """
        try:
            return self.run_json(
                skill_name=Skills.QUICK_REVIEW,
                user_message=diff_context,
                temperature=0.1,
                max_tokens=16384,
            )
        except AgentResponseError as exc:
            try:
                response = self.run(
                    skill_name=Skills.QUICK_REVIEW,
                    user_message=diff_context,
                    temperature=0.1,
                )
            except AgentResponseError as exc2:
                # Both the JSON review and the plain-text fallback failed.
                # Distinguish a failed review from a genuine "no findings"
                # result so the orchestrator does not treat this empty findings
                # list as a clean self-review.
                return {
                    "findings": [],
                    "quick_review_failed": True,
                    "error": str(exc2),
                }
            # The plain-text fallback produced a review — degraded (no structured
            # findings) but not a failure. Match the Quick Review contract so
            # callers don't KeyError on standard fields (F2-be3d768f0aa4).
            return {
                "module": "",
                "skill": Skills.QUICK_REVIEW,
                "findings": [],
                "quick_review_summary": response.content,
                "highest_priority": "",
                # Retained for backward compatibility with existing callers that
                # read `summary` off the fallback.
                "summary": response.content,
            }

    def build_position_statement(self, dispute_context: str) -> dict[str, Any]:
        """
        Build a position statement for the disagreement protocol.

        Returns dict with: issue, approach, evidence, impact_assessment,
        risk_of_alternative, proposed_compromise.

        On a model/parse failure returns an empty-but-valid statement carrying
        an ``error`` field so the caller can distinguish a failed statement from
        a genuine empty response rather than crashing the dispute flow.
        """
        try:
            return self.run_json(
                skill_name=Skills.SCOPE_GUARDIAN,
                user_message=dispute_context,
                temperature=0.2,
                max_tokens=16384,
            )
        except AgentResponseError as e:
            return {
                "error": str(e),
                "issue": "",
                "approach": "",
                "evidence": "",
                "impact_assessment": "",
                "risk_of_alternative": "",
                "proposed_compromise": "",
            }
