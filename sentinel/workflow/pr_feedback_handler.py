"""PR event handling for merge, close, and per-commit rejection flows.

This module implements the three review primitives described in
``docs/sentinel-bug-tracking-remediation-workflow-spec.md`` §8.2:

1. Merge the PR    -> all member findings close.
2. Reject the PR   -> theme returns to ``owner-approved`` for retry with a
                      different ``suggested_fix`` strategy.
3. Reject specific commits -> those findings return to ``remediating``;
                              the remaining findings are surfaced for the
                              caller's merge-or-hold decision.

These are pure functions. They accept parsed event payloads (dataclasses)
and return planning records; the webhook listener, GitHub client, and
state-store writer are wired separately. This mirrors the
``reconcile_finding_projection`` / ``plan_finding_closures`` pattern
established in :mod:`sentinel.workflow.reconcile_issues`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sentinel.workflow.config import WorkflowConfig, _resolve_transitions
from sentinel.workflow.models import IssueClosurePlan


# ---------------------------------------------------------------------------
# Event payload dataclasses (inbound from webhook layer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PRMergedEvent:
	"""Theme PR merged by the Code Owner.

	``member_finding_ids`` enumerates the member findings covered by the
	PR's per-finding commits (one commit per finding per §8.2). The
	mapping from finding id to GitHub issue number is provided alongside
	the event by the caller because the webhook payload itself does not
	carry it.
	"""

	pr_number: int
	merged_sha: str
	theme_id: str
	member_finding_ids: tuple[str, ...]
	finding_id_to_issue_number: dict[str, int]


@dataclass(frozen=True, slots=True)
class PRClosedEvent:
	"""Theme PR closed without merge (full-PR rejection per §8.2 path 2)."""

	pr_number: int
	theme_id: str


@dataclass(frozen=True, slots=True)
class PerCommitRejection:
	"""Code Owner rejected one or more commits in an otherwise-open PR.

	``all_finding_ids`` carries the full member set as it appeared in the
	PR body so the handler can derive the continuing set without an extra
	round trip. ``rejected_finding_ids`` must be a subset of
	``all_finding_ids``.
	"""

	pr_number: int
	commit_sha: str
	theme_id: str
	rejected_finding_ids: tuple[str, ...]
	all_finding_ids: tuple[str, ...]
	finding_id_to_issue_number: dict[str, int]


# ---------------------------------------------------------------------------
# Result dataclasses (outbound to caller)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ThemeStateTransition:
	"""Declarative theme-state change emitted by a PR handler."""

	theme_id: str
	from_state: str
	to_state: str
	reason: str

	def to_dict(self) -> dict[str, Any]:
		return {
			"theme_id": self.theme_id,
			"from_state": self.from_state,
			"to_state": self.to_state,
			"reason": self.reason,
		}


@dataclass(frozen=True, slots=True)
class FindingStateTransition:
	"""Declarative finding-state change emitted by a PR handler."""

	finding_id: str
	issue_number: int
	from_state: str
	to_state: str
	reason: str

	def to_dict(self) -> dict[str, Any]:
		return {
			"finding_id": self.finding_id,
			"issue_number": self.issue_number,
			"from_state": self.from_state,
			"to_state": self.to_state,
			"reason": self.reason,
		}


@dataclass(frozen=True, slots=True)
class PRMergeResult:
	"""Result of :func:`handle_pr_merged`.

	``closure_plans`` reuses :class:`IssueClosurePlan` from Commit 1 with
	``reason='pr-merged'`` so the downstream GitHub client invokes
	``close_issue`` with the ``completed`` state_reason.
	"""

	closure_plans: tuple[IssueClosurePlan, ...]
	theme_transition: ThemeStateTransition


@dataclass(frozen=True, slots=True)
class PerCommitRejectionResult:
	"""Result of :func:`handle_per_commit_rejection`.

	``rejected_transitions`` returns each rejected finding to
	``remediating``. ``continuing_finding_ids`` is the set difference
	``all_finding_ids - rejected_finding_ids`` and is returned verbatim
	so the caller can decide whether to merge or hold per §8.2's
	"merges or holds the rest per Code Owner instruction" clause. The
	theme stays at ``pr-open``; no theme transition is emitted.
	"""

	rejected_transitions: tuple[FindingStateTransition, ...]
	continuing_finding_ids: tuple[str, ...]


# ---------------------------------------------------------------------------
# Review-primitive handlers (§8.2)
# ---------------------------------------------------------------------------


def handle_pr_merged(
	event: PRMergedEvent,
	*,
	config: WorkflowConfig | None = None,
) -> PRMergeResult:
	"""Process a theme PR merge.

	Emits one :class:`IssueClosurePlan` with ``reason='pr-merged'`` for
	each member finding plus a theme transition
	``review_state -> closed_state`` (per profile vocabulary; under
	owner_gate that is ``pr-open -> closed`` -- the pre-rewire literals).

	Profile portability (item 16 part B, cycle 11 Commit 4): from/to
	state labels are sourced from the active profile's
	:class:`TransitionStateConfig` via :func:`_resolve_transitions`,
	which honors the three-state no-config-fallback discipline locked
	in Commit 1.
	"""
	transitions = _resolve_transitions(config)
	missing = [
		fid for fid in event.member_finding_ids
		if fid not in event.finding_id_to_issue_number
	]
	if missing:
		raise ValueError(
			f"PR #{event.pr_number}: missing issue numbers for member findings: {missing}"
		)
	closure_plans = tuple(
		IssueClosurePlan(
			issue_number=event.finding_id_to_issue_number[finding_id],
			reason="pr-merged",
		)
		for finding_id in event.member_finding_ids
	)
	transition = ThemeStateTransition(
		theme_id=event.theme_id,
		from_state=transitions.review_state,
		to_state=transitions.closed_state,
		reason=f"PR #{event.pr_number} merged at {event.merged_sha}",
	)
	return PRMergeResult(closure_plans=closure_plans, theme_transition=transition)


def handle_pr_closed_without_merge(
	event: PRClosedEvent,
	*,
	config: WorkflowConfig | None = None,
) -> ThemeStateTransition:
	"""Process a full-PR rejection (closed without merge).

	Per §8.2 path 2, the theme returns to the active profile's
	``approval_state`` so Sentinel can retry with a different
	``suggested_fix`` strategy. Under owner_gate that is
	``owner-approved`` -- the pre-rewire literal. Member findings stay
	where they are; no finding transitions are emitted.

	Profile portability (item 16 part B, cycle 11 Commit 4): from/to
	state labels are sourced from
	:class:`TransitionStateConfig.review_state` /
	:class:`TransitionStateConfig.approval_state` via
	:func:`_resolve_transitions`.
	"""
	transitions = _resolve_transitions(config)
	return ThemeStateTransition(
		theme_id=event.theme_id,
		from_state=transitions.review_state,
		to_state=transitions.approval_state,
		reason=f"PR #{event.pr_number} closed without merge",
	)


def handle_per_commit_rejection(
	rejection: PerCommitRejection,
	*,
	config: WorkflowConfig | None = None,
) -> PerCommitRejectionResult:
	"""Process per-commit rejection (§8.2 path 3).

	Rejected findings transition to the active profile's
	``remediation_state``; the remaining findings are returned as
	``continuing_finding_ids`` so the caller can route them to merge
	or hold per Code Owner instruction. The theme stays at
	``review_state`` (under owner_gate: ``pr-open``).

	Profile portability (item 16 part B, cycle 11 Commit 4): from/to
	state labels are sourced from
	:class:`TransitionStateConfig.review_state` /
	:class:`TransitionStateConfig.remediation_state` via
	:func:`_resolve_transitions`.
	"""
	transitions = _resolve_transitions(config)
	if not rejection.rejected_finding_ids:
		raise ValueError(
			f"PR #{rejection.pr_number}: per-commit rejection requires at least one finding id"
		)
	all_set = set(rejection.all_finding_ids)
	rejected_set = set(rejection.rejected_finding_ids)
	unknown = rejected_set - all_set
	if unknown:
		raise ValueError(
			f"PR #{rejection.pr_number}: rejected findings not in PR member set: {sorted(unknown)}"
		)
	missing_numbers = [
		fid for fid in rejected_set
		if fid not in rejection.finding_id_to_issue_number
	]
	if missing_numbers:
		raise ValueError(
			f"PR #{rejection.pr_number}: missing issue numbers for rejected findings: {sorted(missing_numbers)}"
		)
	rejected_transitions = tuple(
		FindingStateTransition(
			finding_id=finding_id,
			issue_number=rejection.finding_id_to_issue_number[finding_id],
			from_state=transitions.review_state,
			to_state=transitions.remediation_state,
			reason=f"PR #{rejection.pr_number} commit {rejection.commit_sha} rejected",
		)
		for finding_id in rejection.rejected_finding_ids
	)
	continuing = tuple(
		fid for fid in rejection.all_finding_ids if fid not in rejected_set
	)
	return PerCommitRejectionResult(
		rejected_transitions=rejected_transitions,
		continuing_finding_ids=continuing,
	)


__all__ = [
	"FindingStateTransition",
	"PRClosedEvent",
	"PRMergeResult",
	"PRMergedEvent",
	"PerCommitRejection",
	"PerCommitRejectionResult",
	"ThemeStateTransition",
	"handle_per_commit_rejection",
	"handle_pr_closed_without_merge",
	"handle_pr_merged",
]
