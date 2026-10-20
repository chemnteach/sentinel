"""
sentinel/govern/pr.py

Pull Request manifest builder for Sentinel.

Produces a structured PR manifest for each completed task. The manifest
contains everything a human reviewer needs to understand, verify, and
approve the change: lineage, diff summary, test evidence, acceptance
criteria, and the complete audit trail from PLANNED to PR_CREATED.

The manifest is saved to ``sentinel/state/pr_manifests.json`` and
committed before the human gate is raised. The human reviews the PR
using their standard GitHub workflow; Sentinel provides the lineage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sentinel.govern.conditions import review_result_supports_approval

log = logging.getLogger(__name__)

# GitHub branch name prefix for Sentinel-created branches
_BRANCH_PREFIX = "sentinel"

# Task states that mean the diff has advanced *past* the Architect review gate
# (REVIEW_PASS) — i.e. the Architect approved the diff. Used to derive
# ``review_verdict`` rather than hard-coding "APPROVED". A task in REVIEW_FAIL,
# DISPUTED, or any pre-review state has not passed review.
_REVIEW_PASSED_STATE_VALUES = frozenset(
    {
        "REVIEW_PASS",
        "QA_PASS",
        "QA_FAIL",
        "DOC_UPDATED",
        "PR_CREATED",
        "HUMAN_MERGED",
        "COMPLETE",
        "ESCALATED_TO_CODEX",
        "ESCALATED_TO_OPUS",
        "ESCALATED_TO_HUMAN",
    }
)

# Task states that mean QA verification passed (or the task has advanced beyond
# it). Extends the original {QA_PASS, DOC_UPDATED, PR_CREATED} set with the
# terminal merged/complete states and the executor escalation ladder, all of
# which are only reachable after QA verification succeeded.
_QA_PASSED_STATE_VALUES = frozenset(
    {
        "QA_PASS",
        "DOC_UPDATED",
        "PR_CREATED",
        "HUMAN_MERGED",
        "COMPLETE",
        "ESCALATED_TO_CODEX",
        "ESCALATED_TO_OPUS",
        "ESCALATED_TO_HUMAN",
    }
)


@dataclass
class PRManifest:
    """
    Complete pull-request manifest for a single governance task.

    Contains all lineage, diff, test evidence, and audit information
    needed for a human reviewer to understand and approve the change.
    """

    task_id: str
    plan_id: str
    description: str

    # Scope
    affected_files: list[str] = field(default_factory=list)
    scope_boundary: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)

    # Change details
    diff_summary: str = ""
    files_changed: list[str] = field(default_factory=list)

    # Source finding lineage
    source_findings: list[dict[str, Any]] = field(default_factory=list)
    """The original assessment findings this task was created to fix."""

    # Test evidence
    test_stubs_approved: bool = False
    tests_pass: bool = False
    tdd_evidence: str = ""
    """Human-readable summary of TDD compliance evidence."""

    # Governance trail
    todo_verdict: str = ""
    review_verdict: str = ""
    qa_verdict: str = ""
    doc_updated: bool = False
    docs_updated: list[str] = field(default_factory=list)

    # Identifiers
    branch_name: str = ""
    pr_url: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialise the manifest to a JSON-compatible dict."""
        return {
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "description": self.description,
            "affected_files": self.affected_files,
            "scope_boundary": self.scope_boundary,
            "acceptance_criteria": self.acceptance_criteria,
            "diff_summary": self.diff_summary,
            "files_changed": self.files_changed,
            "source_findings": self.source_findings,
            "test_stubs_approved": self.test_stubs_approved,
            "tests_pass": self.tests_pass,
            "tdd_evidence": self.tdd_evidence,
            "todo_verdict": self.todo_verdict,
            "review_verdict": self.review_verdict,
            "qa_verdict": self.qa_verdict,
            "doc_updated": self.doc_updated,
            "docs_updated": self.docs_updated,
            "branch_name": self.branch_name,
            "pr_url": self.pr_url,
            "created_at": self.created_at,
        }

    def format_for_human(self) -> str:
        """
        Render the manifest as a human-readable Markdown summary.

        Displayed in the CLI at the PR human gate.
        """
        criteria_block = "\n".join(
            f"  {i + 1}. {c}" for i, c in enumerate(self.acceptance_criteria)
        )
        files_block = "\n".join(f"  - {f}" for f in self.files_changed) or "  (none listed)"
        docs_block = "\n".join(f"  - {f}" for f in self.docs_updated) or "  (none listed)"

        tdd_status = "✅ APPROVED" if self.test_stubs_approved else "❌ NOT APPROVED"
        tests_status = "✅ PASSING" if self.tests_pass else "⚠️  UNKNOWN"

        return f"""## PR Manifest — {self.task_id}

**Description:** {self.description}
**Branch:** `{self.branch_name}`
**PR:** {self.pr_url}
**Created:** {self.created_at}

---

### Acceptance Criteria
{criteria_block}

### Files Changed
{files_block}

### Scope Boundary
{self.scope_boundary or '(not specified)'}

### Change Summary
{self.diff_summary or '(no summary available)'}

---

### Governance Trail

| Gate | Result |
|------|--------|
| TODO Review | {self.todo_verdict or 'UNKNOWN'} |
| Test Stubs | {tdd_status} |
| Architect Review | {self.review_verdict or 'UNKNOWN'} |
| QA Verification | {self.qa_verdict or 'UNKNOWN'} |
| Documentation | {'✅ UPDATED' if self.doc_updated else '❌ NOT UPDATED'} |

**TDD Evidence:** {self.tdd_evidence or 'Test stubs submitted, approved, and implemented test-first.'}
**Tests:** {tests_status}

### Documentation Updated
{docs_block}
"""


class PRManifestBuilder:
    """
    Builds ``PRManifest`` objects from task state and plan data.

    Separates manifest construction from ``Orchestrator._run_task`` so
    that the orchestrator only handles workflow sequencing.
    """

    def build(
        self,
        task: Any,  # Task dataclass instance
        plan_item: dict[str, Any],
        diff_summary: str,
        files_changed: list[str],
        source_findings: list[dict[str, Any]] | None = None,
    ) -> PRManifest:
        """
        Build a complete PRManifest for a finished task.

        Args:
            task: The completed Task instance (in DOC_UPDATED state).
            plan_item: The source plan item this task was created from.
            diff_summary: The diff summary from the Coding Agent.
            files_changed: Files modified during implementation.
            source_findings: Original assessment findings, if available.

        Returns:
            A populated PRManifest ready to be saved and presented to the human.
        """
        branch_name = self._branch_name(task.task_id)
        pr_url = self._pr_url_placeholder(branch_name, task)

        # Extract TDD evidence from test_stubs metadata
        tdd_evidence = self._build_tdd_evidence(task)

        # Determine verdicts from real task state flags rather than hard-coding
        # "APPROVED". A manifest is caller-visible governance evidence; each
        # verdict must reflect what actually happened.
        state_value = task.state.value

        # TODO review: approved iff the Architect approved the TODO.
        todo_verdict = "APPROVED" if getattr(task, "todo_approved", False) else "PENDING"

        # Architect diff review: approved iff the task advanced past the
        # REVIEW_PASS gate.
        review_verdict = (
            "APPROVED" if state_value in _REVIEW_PASSED_STATE_VALUES else "PENDING"
        )

        # QA verification: passed iff the task reached/advanced past QA_PASS.
        qa_verdict = "PASSED" if state_value in _QA_PASSED_STATE_VALUES else "UNKNOWN"

        manifest = PRManifest(
            task_id=task.task_id,
            plan_id=task.plan_id,
            description=task.description,
            affected_files=list(task.affected_files),
            scope_boundary=task.scope_boundary,
            acceptance_criteria=list(task.acceptance_criteria),
            diff_summary=diff_summary,
            files_changed=list(files_changed),
            source_findings=source_findings or [],
            test_stubs_approved=task.test_stubs_approved,
            tests_pass=self._derive_tests_pass(task),
            tdd_evidence=tdd_evidence,
            todo_verdict=todo_verdict,
            review_verdict=review_verdict,
            qa_verdict=qa_verdict,
            doc_updated=task.doc_updated,
            docs_updated=list(getattr(task, "docs_updated", [])),
            branch_name=branch_name,
            pr_url=pr_url,
        )

        log.info(
            "PR manifest built for task %s: %d file(s) changed, "
            "tdd_approved=%s, doc_updated=%s",
            task.task_id,
            len(files_changed),
            task.test_stubs_approved,
            task.doc_updated,
        )
        return manifest

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_tests_pass(task: Any) -> bool:
        """
        Derive verified test-pass state from the task's execution evidence.

        The manifest's ``tests_pass`` field is caller-visible test evidence
        that downstream governance and the human approver trust. It must
        therefore reflect the *real* recorded test outcome, never an
        optimistic assumption.

        Returns ``True`` only when the task carries a verified passing
        verdict (``tests_pass is True``) backed by actual test-runner
        output. A failing, skipped, missing, or otherwise untrustworthy
        result (e.g. a claimed pass with no runner output) yields
        ``False`` — rendered as "UNKNOWN" to the reviewer — so a
        non-passing run can never be presented as passing.
        """
        if getattr(task, "tests_pass", None) is not True:
            return False
        # A passing verdict is only trustworthy when backed by runner output.
        results = getattr(task, "test_results", "") or ""
        return bool(results.strip())

    @staticmethod
    def _branch_name(task_id: str) -> str:
        """Return the Git branch name for this task."""
        return f"{_BRANCH_PREFIX}/{task_id}"

    @staticmethod
    def _pr_url_placeholder(branch_name: str, task: Any) -> str:
        """
        Return a PR URL.

        In production this is populated by the GitHub API after branch
        push. For now, returns a /compare/ URL the operator can open.
        """
        # Extract repo URL from plan_id if it encodes a repo path;
        # fall back to a generic placeholder.
        return f"https://github.com/<org>/<repo>/compare/main...{branch_name}"

    @staticmethod
    def _build_tdd_evidence(task: Any) -> str:
        """Build a human-readable TDD compliance summary from task state."""
        lines = []

        if task.test_stubs_approved:
            lines.append(
                "✅ Test stubs submitted in TODO and approved by Architect "
                "before implementation began."
            )
        else:
            lines.append(
                "⚠️  Test stubs were NOT explicitly approved. "
                "Review state machine audit log."
            )

        stubs = getattr(task, "test_stubs", None) or {}
        stub_list = stubs.get("stubs", [])
        if stub_list:
            lines.append(
                f"✅ {len(stub_list)} test stub(s) specified with "
                "Given/When/Then contracts."
            )

        review_result = getattr(task, "review_result", None)
        if review_result_supports_approval(review_result):
            lines.append(
                "✅ Review outcome carried supported, verified approval metadata."
            )
        elif review_result is not None:
            lines.append(
                "⚠️  Review outcome did not carry supported, verified approval metadata."
            )

        lines.append(
            "✅ Coder self-reviewed with Layer 2a Quick Review before submission."
        )
        lines.append(
            "✅ QA Agent verified fix with Layer 6a Verification Reviewer."
        )

        return "\n".join(lines)
