"""Tests for sentinel.workflow.pr_feedback_handler."""

from __future__ import annotations

import pytest

from sentinel.workflow.config import (
	GitHubConfig,
	ProfileConfig,
	RemediationConfig,
	ThemesConfig,
	WorkflowConfig,
	WorkflowConfigError,
)
from sentinel.workflow.models import IssueClosurePlan
from sentinel.workflow.pr_feedback_handler import (
	FindingStateTransition,
	PerCommitRejection,
	PerCommitRejectionResult,
	PRClosedEvent,
	PRMergedEvent,
	PRMergeResult,
	ThemeStateTransition,
	handle_per_commit_rejection,
	handle_pr_closed_without_merge,
	handle_pr_merged,
)
from tests.conftest import advisory_config


# ---------------------------------------------------------------------------
# handle_pr_merged
# ---------------------------------------------------------------------------


def test_handle_pr_merged_emits_closure_plans_and_theme_transition() -> None:
	event = PRMergedEvent(
		pr_number=512,
		merged_sha="abc1234",
		theme_id="T-042",
		member_finding_ids=("F-1", "F-2", "F-3"),
		finding_id_to_issue_number={"F-1": 1001, "F-2": 1002, "F-3": 1003},
	)

	result = handle_pr_merged(event, config=WorkflowConfig.default())

	assert isinstance(result, PRMergeResult)
	assert result.closure_plans == (
		IssueClosurePlan(issue_number=1001, reason="pr-merged"),
		IssueClosurePlan(issue_number=1002, reason="pr-merged"),
		IssueClosurePlan(issue_number=1003, reason="pr-merged"),
	)
	assert result.theme_transition == ThemeStateTransition(
		theme_id="T-042",
		from_state="pr-open",
		to_state="closed",
		reason="PR #512 merged at abc1234",
	)


def test_handle_pr_merged_with_empty_member_findings_emits_only_theme_transition() -> None:
	event = PRMergedEvent(
		pr_number=99,
		merged_sha="def5678",
		theme_id="T-001",
		member_finding_ids=(),
		finding_id_to_issue_number={},
	)

	result = handle_pr_merged(event, config=WorkflowConfig.default())

	assert result.closure_plans == ()
	assert result.theme_transition.to_state == "closed"


def test_handle_pr_merged_raises_when_issue_number_missing() -> None:
	event = PRMergedEvent(
		pr_number=7,
		merged_sha="0badc0de",
		theme_id="T-007",
		member_finding_ids=("F-known", "F-missing"),
		finding_id_to_issue_number={"F-known": 42},
	)

	with pytest.raises(ValueError, match="missing issue numbers"):
		handle_pr_merged(event, config=WorkflowConfig.default())


# ---------------------------------------------------------------------------
# handle_pr_closed_without_merge
# ---------------------------------------------------------------------------


def test_handle_pr_closed_without_merge_returns_theme_to_owner_approved() -> None:
	event = PRClosedEvent(pr_number=313, theme_id="T-200")

	transition = handle_pr_closed_without_merge(event, config=WorkflowConfig.default())

	assert transition == ThemeStateTransition(
		theme_id="T-200",
		from_state="pr-open",
		to_state="owner-approved",
		reason="PR #313 closed without merge",
	)


def test_handle_pr_closed_without_merge_does_not_emit_finding_transitions() -> None:
	transition = handle_pr_closed_without_merge(
		PRClosedEvent(pr_number=1, theme_id="T-x"),
		config=WorkflowConfig.default(),
	)

	# Sanity: the result type is only a theme transition (not a tuple/result wrapper).
	assert isinstance(transition, ThemeStateTransition)


# ---------------------------------------------------------------------------
# handle_per_commit_rejection
# ---------------------------------------------------------------------------


def test_handle_per_commit_rejection_partial_subset() -> None:
	rejection = PerCommitRejection(
		pr_number=812,
		commit_sha="fa11ed",
		theme_id="T-042",
		rejected_finding_ids=("F-2",),
		all_finding_ids=("F-1", "F-2", "F-3"),
		finding_id_to_issue_number={"F-1": 101, "F-2": 102, "F-3": 103},
	)

	result = handle_per_commit_rejection(rejection, config=WorkflowConfig.default())

	assert isinstance(result, PerCommitRejectionResult)
	assert result.rejected_transitions == (
		FindingStateTransition(
			finding_id="F-2",
			issue_number=102,
			from_state="pr-open",
			to_state="remediating",
			reason="PR #812 commit fa11ed rejected",
		),
	)
	assert result.continuing_finding_ids == ("F-1", "F-3")


def test_handle_per_commit_rejection_all_findings_rejected_leaves_no_continuing() -> None:
	rejection = PerCommitRejection(
		pr_number=900,
		commit_sha="deadbee",
		theme_id="T-100",
		rejected_finding_ids=("F-1", "F-2"),
		all_finding_ids=("F-1", "F-2"),
		finding_id_to_issue_number={"F-1": 11, "F-2": 12},
	)

	result = handle_per_commit_rejection(rejection, config=WorkflowConfig.default())

	assert len(result.rejected_transitions) == 2
	assert result.continuing_finding_ids == ()


def test_handle_per_commit_rejection_preserves_continuing_order_from_pr_body() -> None:
	rejection = PerCommitRejection(
		pr_number=1,
		commit_sha="abc",
		theme_id="T-1",
		rejected_finding_ids=("F-b",),
		all_finding_ids=("F-a", "F-b", "F-c", "F-d"),
		finding_id_to_issue_number={"F-a": 1, "F-b": 2, "F-c": 3, "F-d": 4},
	)

	result = handle_per_commit_rejection(rejection, config=WorkflowConfig.default())

	assert result.continuing_finding_ids == ("F-a", "F-c", "F-d")


def test_handle_per_commit_rejection_raises_when_rejected_is_empty() -> None:
	rejection = PerCommitRejection(
		pr_number=5,
		commit_sha="x",
		theme_id="T-1",
		rejected_finding_ids=(),
		all_finding_ids=("F-1",),
		finding_id_to_issue_number={"F-1": 1},
	)

	with pytest.raises(ValueError, match="at least one finding id"):
		handle_per_commit_rejection(rejection, config=WorkflowConfig.default())


def test_handle_per_commit_rejection_raises_when_rejected_not_in_member_set() -> None:
	rejection = PerCommitRejection(
		pr_number=6,
		commit_sha="y",
		theme_id="T-1",
		rejected_finding_ids=("F-not-in-pr",),
		all_finding_ids=("F-1", "F-2"),
		finding_id_to_issue_number={"F-1": 1, "F-2": 2, "F-not-in-pr": 99},
	)

	with pytest.raises(ValueError, match="not in PR member set"):
		handle_per_commit_rejection(rejection, config=WorkflowConfig.default())


def test_handle_per_commit_rejection_raises_when_issue_number_missing() -> None:
	rejection = PerCommitRejection(
		pr_number=7,
		commit_sha="z",
		theme_id="T-1",
		rejected_finding_ids=("F-1",),
		all_finding_ids=("F-1", "F-2"),
		finding_id_to_issue_number={"F-2": 22},
	)

	with pytest.raises(ValueError, match="missing issue numbers"):
		handle_per_commit_rejection(rejection, config=WorkflowConfig.default())


# ---------------------------------------------------------------------------
# Dataclass shape sanity
# ---------------------------------------------------------------------------


def test_result_dataclasses_are_frozen() -> None:
	import dataclasses

	transition = ThemeStateTransition(
		theme_id="T-1",
		from_state="pr-open",
		to_state="closed",
		reason="r",
	)
	with pytest.raises(dataclasses.FrozenInstanceError):
		transition.to_state = "owner-approved"  # type: ignore[misc]

	finding = FindingStateTransition(
		finding_id="F-1",
		issue_number=1,
		from_state="pr-open",
		to_state="remediating",
		reason="r",
	)
	with pytest.raises(dataclasses.FrozenInstanceError):
		finding.issue_number = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Cycle 11 Commit 4: item 16 part B -- rewire pr_feedback_handler
# transition sites to read TransitionStateConfig from
# config.profile.transitions via _resolve_transitions. Closes item 16.
#
# Cascade scope: ZERO. The three handlers have no production callers
# yet (webhook listener wiring is deferred per module docstring); only
# tests and module self-references invoke them. No upstream signature
# changes required.
#
# Cycle-10 audit naming gap: audit and Commit 4 task said
# 'handle_per_commit_rejections' (plural); actual function is
# 'handle_per_commit_rejection' (singular). Fourth Family-2
# implementation-time-completeness instance, reference-level
# correction shape (parallel to Commit 3's project_finding_state ->
# reconcile_finding_projection correction).
# ---------------------------------------------------------------------------


def _solo_config() -> WorkflowConfig:
	return WorkflowConfig.default(profile_name="solo_local_remediating")


def _merged_event() -> PRMergedEvent:
	return PRMergedEvent(
		pr_number=42,
		merged_sha="sha1234",
		theme_id="T-1",
		member_finding_ids=("F-1",),
		finding_id_to_issue_number={"F-1": 100},
	)


def _closed_event() -> PRClosedEvent:
	return PRClosedEvent(pr_number=43, theme_id="T-2")


def _rejection() -> PerCommitRejection:
	return PerCommitRejection(
		pr_number=44,
		commit_sha="abc",
		theme_id="T-3",
		rejected_finding_ids=("F-1",),
		all_finding_ids=("F-1", "F-2"),
		finding_id_to_issue_number={"F-1": 1, "F-2": 2},
	)


# --- Format-compatibility verification (Commit 1 contract pin) -------------


def test_handle_pr_merged_owner_gate_matches_pre_rewire_literals() -> None:
	"""Cycle 11 Commit 1's test_owner_gate_transitions_default pinned
	that _OWNER_GATE_TRANSITIONS.review_state == 'pr-open' and
	.closed_state == 'closed'. Verify the Commit 4 rewire preserves
	the pre-rewire string literals exactly under owner_gate."""
	result = handle_pr_merged(_merged_event(), config=WorkflowConfig.default())
	assert result.theme_transition.from_state == "pr-open"
	assert result.theme_transition.to_state == "closed"


def test_handle_pr_closed_without_merge_owner_gate_matches_pre_rewire_literals() -> None:
	result = handle_pr_closed_without_merge(
		_closed_event(), config=WorkflowConfig.default()
	)
	assert result.from_state == "pr-open"
	assert result.to_state == "owner-approved"


def test_handle_per_commit_rejection_owner_gate_matches_pre_rewire_literals() -> None:
	result = handle_per_commit_rejection(
		_rejection(), config=WorkflowConfig.default()
	)
	assert result.rejected_transitions[0].from_state == "pr-open"
	assert result.rejected_transitions[0].to_state == "remediating"


# --- solo_local_remediating profile vocabulary ------------------------------


def test_handle_pr_merged_solo_local_remediating_uses_profile_vocabulary() -> None:
	"""Under solo_local_remediating, review_state='review' and
	closed_state='closed' per Commit 2's _SOLO_LOCAL_REMEDIATING_TRANSITIONS."""
	result = handle_pr_merged(_merged_event(), config=_solo_config())
	assert result.theme_transition.from_state == "review"
	assert result.theme_transition.to_state == "closed"


def test_handle_pr_closed_without_merge_solo_local_remediating_uses_profile_vocabulary() -> None:
	"""Under solo_local_remediating, approval_state='approved' (canonical
	synthesis per Commit 2 Q2 spec-divergence resolution)."""
	result = handle_pr_closed_without_merge(_closed_event(), config=_solo_config())
	assert result.from_state == "review"
	assert result.to_state == "approved"


def test_handle_per_commit_rejection_solo_local_remediating_uses_profile_vocabulary() -> None:
	result = handle_per_commit_rejection(_rejection(), config=_solo_config())
	assert result.rejected_transitions[0].from_state == "review"
	assert result.rejected_transitions[0].to_state == "remediating"


# --- Advisory profile: WorkflowConfigError raised ---------------------------


def test_handle_pr_merged_advisory_profile_raises() -> None:
	with pytest.raises(WorkflowConfigError, match=r"declares no workflow transitions"):
		handle_pr_merged(_merged_event(), config=advisory_config())


def test_handle_pr_closed_without_merge_advisory_profile_raises() -> None:
	with pytest.raises(WorkflowConfigError, match=r"declares no workflow transitions"):
		handle_pr_closed_without_merge(_closed_event(), config=advisory_config())


def test_handle_per_commit_rejection_advisory_profile_raises() -> None:
	with pytest.raises(WorkflowConfigError, match=r"declares no workflow transitions"):
		handle_per_commit_rejection(_rejection(), config=advisory_config())


# --- No-config DeprecationWarning fallback ----------------------------------


def test_handle_pr_merged_no_config_warns_and_falls_back_to_owner_gate() -> None:
	with pytest.warns(DeprecationWarning, match=r"WorkflowConfig not supplied"):
		result = handle_pr_merged(_merged_event())
	assert result.theme_transition.from_state == "pr-open"
	assert result.theme_transition.to_state == "closed"


def test_handle_pr_closed_without_merge_no_config_warns_and_falls_back_to_owner_gate() -> None:
	with pytest.warns(DeprecationWarning, match=r"WorkflowConfig not supplied"):
		result = handle_pr_closed_without_merge(_closed_event())
	assert result.from_state == "pr-open"
	assert result.to_state == "owner-approved"


def test_handle_per_commit_rejection_no_config_warns_and_falls_back_to_owner_gate() -> None:
	with pytest.warns(DeprecationWarning, match=r"WorkflowConfig not supplied"):
		result = handle_per_commit_rejection(_rejection())
	assert result.rejected_transitions[0].from_state == "pr-open"
	assert result.rejected_transitions[0].to_state == "remediating"


# --- Item 16 closure verification: end-to-end profile sweep -----------------


@pytest.mark.parametrize(
	"profile_name, expected_review, expected_closed, expected_approval, expected_remediation",
	[
		("owner_gate", "pr-open", "closed", "owner-approved", "remediating"),
		("solo_local_remediating", "review", "closed", "approved", "remediating"),
	],
)
def test_all_three_handlers_use_profile_vocabulary_end_to_end(
	profile_name: str,
	expected_review: str,
	expected_closed: str,
	expected_approval: str,
	expected_remediation: str,
) -> None:
	"""Item 16 closure verification (cycle 11 Commit 4): all three
	pr_feedback_handler entry points produce profile-vocabulary state
	labels for both supported profiles, with zero hardcoded owner_gate
	literals leaking through. Paired with the grep-based closure
	verification in the Commit 4 commit body."""
	config = WorkflowConfig.default(profile_name=profile_name)

	merge = handle_pr_merged(_merged_event(), config=config)
	assert merge.theme_transition.from_state == expected_review
	assert merge.theme_transition.to_state == expected_closed

	close = handle_pr_closed_without_merge(_closed_event(), config=config)
	assert close.from_state == expected_review
	assert close.to_state == expected_approval

	rej = handle_per_commit_rejection(_rejection(), config=config)
	assert rej.rejected_transitions[0].from_state == expected_review
	assert rej.rejected_transitions[0].to_state == expected_remediation
