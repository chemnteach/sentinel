"""Triage-disposition handlers for theme- and finding-level Code Owner actions.

This module implements the Phase 4 triage primitives described in
``docs/sentinel-bug-tracking-remediation-workflow-spec.md`` §11.2:

    "Code Owner flows for accept-theme / reject-finding / wont-fix / defer."

Theme-level acceptance is the §6.6 / §7.2 entry point into the
remediation lane: the Code Owner asserts a theme is real, in scope,
and ready for the owner's structural approval. Per §1.6.1's ``owner_gate``
default profile, accepted themes transition to ``owner-approved``;
under ``solo_local_remediating`` they transition to ``approved``
(canonical synthesis per cycle 11 Commit 2's Q2 resolution).

These are pure functions. They accept parsed event payloads
(dataclasses) and return :class:`ThemeStateTransition` planning
records that downstream code routes to the GitHub client and
state-store writer. This mirrors the
``handle_pr_merged`` / ``handle_pr_closed_without_merge`` /
``handle_per_commit_rejection`` pattern established in
:mod:`sentinel.workflow.pr_feedback_handler`.

Profile portability (item 16 part B precedent, cycle 11 Commit 4):
to-state labels are sourced from the active profile's
:class:`TransitionStateConfig` via :func:`_resolve_transitions`,
which honors the three-state no-config-fallback discipline locked
in Commit 1. The from-state is caller-supplied via the event so
this module stays pure-planning (no state-store reads).

Forward reference (cycle 11 Commit 6): ``handle_reject_finding``
will mirror this shape with a ``disposition`` parameter selecting
between ``rejected_states`` members (``wont-fix`` / ``deferred``
under owner_gate; ``rejected`` / ``deferred`` under
solo_local_remediating).
"""

from __future__ import annotations

from dataclasses import dataclass

from sentinel.workflow.config import WorkflowConfig, _resolve_transitions
# ``Disposition`` is re-exported below for backward compatibility with
# callers that imported it from this module before cycle 12 Commit 1
# relocated the canonical definition to :mod:`sentinel.workflow.models`.
from sentinel.workflow.models import (
	BulkSkippedMember,
	BulkThemeMemberActionEvent,
	BulkThemeMemberActionResult,
	BulkTriageEligibilityError,
	Disposition,
)
from sentinel.workflow.pr_feedback_handler import (
	FindingStateTransition,
	ThemeStateTransition,
)


# ---------------------------------------------------------------------------
# Event payload dataclasses (inbound from webhook / CLI layer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AcceptThemeEvent:
	"""Code Owner accepted a theme for owner structural approval.

	Per §6.6, theme-level acceptance is the entry point into the
	remediation lane. The Code Owner's assessment recommends a lens
	(structural / conceptual / hybrid); the owner approves the recommended
	grouping at the theme level.

	``current_state`` is caller-supplied so this module stays pure-
	planning (no state-store reads). Callers obtain it from the
	GitHub issue label or the workflow state file; if the caller does
	not know the current state (e.g., a fresh issue that has only ever
	been at ``triage_state``), passing ``transitions.triage_state``
	or the foundation default ``"triage"`` is the conventional choice.

	``actor`` and ``timestamp`` are embedded in the resulting
	transition's ``reason`` field per the
	:mod:`sentinel.workflow.pr_feedback_handler` precedent, which
	encodes provenance into ``reason`` rather than promoting it to
	first-class fields on :class:`ThemeStateTransition`.
	"""

	theme_id: str
	current_state: str
	actor: str
	timestamp: str
	reason: str = ""


# Disposition discriminator for finding-level rejection (Phase 4,
# cycle 11 Commit 6). The Q3 design pass found that reject-finding /
# wont-fix / defer is a single foundation transition
# (``accepted -> rejected``) parameterized by *which* member of
# ``rejected_states`` applies. The profile-portable abstraction maps
# to the concrete label via
# :class:`TransitionStateConfig.rejection_label` /
# :attr:`TransitionStateConfig.deferral_label` so handler code stays
# free of profile-specific literals (the same anti-pattern Commit 4
# eliminated from :mod:`sentinel.workflow.pr_feedback_handler`).
#
# Cycle 12 Commit 1 relocated the canonical ``Disposition`` definition
# to :mod:`sentinel.workflow.models` so the new bulk-action event
# (:class:`sentinel.workflow.models.BulkThemeMemberActionEvent`) can
# share it without a circular import. The import at the top of this
# module re-exports it for backward compatibility.


@dataclass(frozen=True, slots=True)
class RejectFindingEvent:
	"""Code Owner rejected a finding with a disposition (rejection or deferral).

	Per §11.2's "reject-finding / wont-fix / defer" flow, this is one
	foundation transition parameterized by ``disposition``. The
	``rejection`` disposition lands on the profile's
	``rejection_label`` (under ``owner_gate`` that is ``"wont-fix"``;
	under ``solo_local_remediating`` that is ``"rejected"``). The
	``deferral`` disposition lands on the profile's
	``deferral_label`` (``"deferred"`` under both current profiles
	per the documented cross-profile overlap in
	``test_solo_local_remediating_distinct_from_owner_gate`` dim 5).

	``issue_number`` is mandatory and matches
	:class:`FindingStateTransition`'s shape. Phase 2 (Issue Sync,
	§11.2) creates the GitHub issue for each finding before Phase 4
	(Triage UX) operates on it, so a finding being rejected at
	triage time always has an issue number; the caller obtains it
	from the workflow state store.

	``current_state`` is caller-supplied so this module stays pure-
	planning (no state-store reads), matching the
	:class:`AcceptThemeEvent` precedent.

	``actor`` and ``timestamp`` are embedded into the resulting
	transition's ``reason`` field per the
	:mod:`sentinel.workflow.pr_feedback_handler` convention (encode
	provenance into ``reason`` rather than promoting it to first-
	class fields on :class:`FindingStateTransition`).
	"""

	finding_id: str
	issue_number: int
	current_state: str
	disposition: Disposition
	actor: str
	timestamp: str
	reason: str = ""


# ---------------------------------------------------------------------------
# Phase 4 triage primitives (§11.2)
# ---------------------------------------------------------------------------


def handle_accept_theme(
	event: AcceptThemeEvent,
	*,
	config: WorkflowConfig | None = None,
) -> ThemeStateTransition:
	"""Process a Code Owner theme-acceptance action.

	Returns a :class:`ThemeStateTransition` from
	``event.current_state`` to the active profile's
	``approval_state`` (under ``owner_gate`` that is
	``"owner-approved"``; under ``solo_local_remediating`` that is
	``"approved"``).

	Profile portability (item 16 part B precedent): the target state
	label is sourced from :class:`TransitionStateConfig.approval_state`
	via :func:`_resolve_transitions`, which honors the three-state
	no-config-fallback discipline locked in Commit 1.

	The handler is pure: no I/O, no state-store access. The caller is
	responsible for asserting ``event.current_state`` matches the
	persisted state before applying the returned transition.
	"""
	transitions = _resolve_transitions(config)
	reason_parts = [f"theme accepted by {event.actor} at {event.timestamp}"]
	if event.reason:
		reason_parts.append(event.reason)
	return ThemeStateTransition(
		theme_id=event.theme_id,
		from_state=event.current_state,
		to_state=transitions.approval_state,
		reason=" -- ".join(reason_parts),
	)


def handle_reject_finding(
	event: RejectFindingEvent,
	*,
	config: WorkflowConfig | None = None,
) -> FindingStateTransition:
	"""Process a Code Owner finding-rejection action.

	Returns a :class:`FindingStateTransition` from
	``event.current_state`` to the active profile's
	``rejection_label`` (when ``disposition == "rejection"``) or
	``deferral_label`` (when ``disposition == "deferral"``). Under
	``owner_gate`` those are ``"wont-fix"`` and ``"deferred"``; under
	``solo_local_remediating`` they are ``"rejected"`` and
	``"deferred"``.

	Q3 design pass (cycle 11 Commit 6): reject-finding / wont-fix /
	defer are one foundation transition
	(``accepted -> rejected``) parameterized by disposition. The
	disposition-to-label mapping is sourced from
	:class:`TransitionStateConfig` (Q1 lock extension at Commit 6)
	so handler code stays free of profile-specific literals.

	The handler is pure: no I/O, no state-store access. The caller
	is responsible for asserting ``event.current_state`` matches
	the persisted state before applying the returned transition.

	Raises:
		ValueError: if ``event.disposition`` is neither
			``"rejection"`` nor ``"deferral"``.
		WorkflowConfigError: via :func:`_resolve_transitions` when
			``config`` names an advisory profile (no transitions).
	"""
	transitions = _resolve_transitions(config)
	if event.disposition == "rejection":
		to_state = transitions.rejection_label
		flavor = "rejected"
	elif event.disposition == "deferral":
		to_state = transitions.deferral_label
		flavor = "deferred"
	else:
		raise ValueError(
			f"unknown disposition {event.disposition!r}; "
			"expected 'rejection' or 'deferral'"
		)
	reason_parts = [
		f"finding {flavor} by {event.actor} at {event.timestamp}"
	]
	if event.reason:
		reason_parts.append(event.reason)
	return FindingStateTransition(
		finding_id=event.finding_id,
		issue_number=event.issue_number,
		from_state=event.current_state,
		to_state=to_state,
		reason=" -- ".join(reason_parts),
	)


def handle_bulk_theme_member_action(
	event: BulkThemeMemberActionEvent,
	*,
	config: WorkflowConfig | None = None,
) -> BulkThemeMemberActionResult:
	"""Process a Code Owner bulk-action over a subset of a theme's members.

	Implements §11.2's "bulk operations on theme members" deliverable
	(cycle 12 Commit 2). Each eligible member yields one
	:class:`FindingStateTransition` from the caller-supplied
	``current_state`` to the active profile's ``rejection_label`` or
	``deferral_label`` per the event's ``disposition``. The handler
	resolves :func:`_resolve_transitions` exactly once and reuses the
	resulting :class:`TransitionStateConfig` for every member, so
	profile portability is preserved (no profile-specific literals
	appear in the handler body) and the resolved vocabulary is stable
	across the whole batch.

	Partial-failure policy: **strict-eligibility-gate** (refuse-all).
	The handler iterates every member, collecting every eligibility
	failure (missing from the lookup tables OR ``current_state`` not
	in :attr:`TransitionStateConfig.accepted_states`) into a
	:class:`BulkSkippedMember` tuple. If the tuple is non-empty after
	the full pass, :class:`BulkTriageEligibilityError` is raised
	carrying the complete set so the caller can correct every problem
	in a single round-trip. This mirrors the established refuse-all
	pattern in
	:func:`sentinel.workflow.pr_feedback_handler.handle_per_commit_rejection`
	and matches the cycle 12 design proposal modification 1.

	The handler is pure: no I/O, no state-store access. The caller
	supplies all state (``finding_id_to_current_state``,
	``finding_id_to_issue_number``) and is responsible for asserting
	those tables match the persisted state before applying the
	returned transitions.

	Args:
		event: The bulk-action envelope; see
			:class:`BulkThemeMemberActionEvent` for field semantics.
		config: Active workflow configuration; forwarded to
			:func:`_resolve_transitions` for the three-state
			no-config-fallback discipline.

	Returns:
		:class:`BulkThemeMemberActionResult` carrying one
		:class:`FindingStateTransition` per member in
		``event.member_finding_ids`` order.

	Raises:
		ValueError: if ``event.disposition`` is neither
			``"rejection"`` nor ``"deferral"``.
		BulkTriageEligibilityError: if any member fails the
			eligibility gate; the exception's ``skipped`` tuple
			carries the complete set of failures from this call.
		WorkflowConfigError: via :func:`_resolve_transitions` when
			``config`` names an advisory profile (no transitions).
	"""
	# Resolve once -- profile portability via TransitionStateConfig;
	# no literal state-name strings appear below.
	transitions = _resolve_transitions(config)

	# Disposition-to-label mapping reuses handle_reject_finding's
	# shape; failure here is a programmer error, not an eligibility
	# failure, so it raises before the per-member loop.
	if event.disposition == "rejection":
		to_state = transitions.rejection_label
		flavor = "rejected"
	elif event.disposition == "deferral":
		to_state = transitions.deferral_label
		flavor = "deferred"
	else:
		raise ValueError(
			f"unknown disposition {event.disposition!r}; "
			"expected 'rejection' or 'deferral'"
		)

	# Strict-eligibility-gate (collect-all-failures): iterate every
	# member, collect every failure, raise once at the end. Do not
	# fail-fast on the first ineligible member.
	skipped: list[BulkSkippedMember] = []
	for finding_id in event.member_finding_ids:
		if finding_id not in event.finding_id_to_current_state:
			skipped.append(
				BulkSkippedMember(
					finding_id=finding_id,
					reason=(
						f"finding {finding_id} not present in "
						"finding_id_to_current_state"
					),
				)
			)
			continue
		if finding_id not in event.finding_id_to_issue_number:
			skipped.append(
				BulkSkippedMember(
					finding_id=finding_id,
					reason=(
						f"finding {finding_id} not present in "
						"finding_id_to_issue_number"
					),
				)
			)
			continue
		current_state = event.finding_id_to_current_state[finding_id]
		if current_state not in transitions.accepted_states:
			skipped.append(
				BulkSkippedMember(
					finding_id=finding_id,
					reason=(
						f"finding {finding_id} current_state "
						f"{current_state!r} is not in accepted_states"
					),
				)
			)
			continue

	if skipped:
		raise BulkTriageEligibilityError(skipped=tuple(skipped))

	# Happy path: all members eligible. Emit one transition per
	# member in event.member_finding_ids order. Provenance (actor,
	# timestamp, optional reason) embedded into the reason field
	# per the established pr_feedback_handler precedent.
	reason_prefix = (
		f"bulk theme-member {flavor} by {event.actor} at {event.timestamp} "
		f"(theme {event.theme_id})"
	)
	reason_parts_base = [reason_prefix]
	if event.reason:
		reason_parts_base.append(event.reason)
	reason = " -- ".join(reason_parts_base)

	emitted: list[FindingStateTransition] = []
	for finding_id in event.member_finding_ids:
		emitted.append(
			FindingStateTransition(
				finding_id=finding_id,
				issue_number=event.finding_id_to_issue_number[finding_id],
				from_state=event.finding_id_to_current_state[finding_id],
				to_state=to_state,
				reason=reason,
			)
		)

	return BulkThemeMemberActionResult(transitions=tuple(emitted))


__all__ = [
	"AcceptThemeEvent",
	"Disposition",
	"RejectFindingEvent",
	"handle_accept_theme",
	"handle_bulk_theme_member_action",
	"handle_reject_finding",
]
