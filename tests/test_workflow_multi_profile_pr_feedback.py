"""Cycle 13 Commit 2: multi-profile parametrized tests for
:mod:`sentinel.workflow.pr_feedback_handler` (Grouping P).

This module is the codebase-portability proof for the three
profile-coupled PR-feedback handlers:

- Site 4: ``handle_pr_merged`` -> theme transition
  ``review_state`` -> ``closed_state``
- Site 5: ``handle_pr_closed_without_merge`` -> theme transition
  ``review_state`` -> ``approval_state``
- Site 6: ``handle_per_commit_rejection`` -> finding transitions
  ``review_state`` -> ``remediation_state``

All three handlers go through ``_resolve_transitions``, so both
Grouping-P standing assertions (advisory-profile
``WorkflowConfigError`` + no-config single ``DeprecationWarning``)
apply. The chosen handler for both standing assertions is
``handle_pr_merged``: it is the §8.2 path-1 (merge) primitive and
its argument-construction surface is the simplest of the three.

``_PROFILE_EXPECTATIONS`` values are read from the live
``WorkflowConfig`` object at test-setup time, not inlined as
literals; per-site assertions include foundation-concept anchors
asserting equality with ``config.profile.transitions.*`` fields.
``advisory_config()`` is now imported from ``tests.conftest``
(cycle 16, Item 21 consolidation).

Phase-boundary note: pr_feedback_handler is a Phase-5 (Remediation
Loop) surface per spec §11.2 that inherits the Phase-4 profile-
coupling infrastructure rewired in cycle 11. This file demonstrates
that the inherited coupling holds across both remediating profiles.
"""

from __future__ import annotations

import warnings

import pytest

from sentinel.workflow.config import (
	GitHubConfig,
	ProfileConfig,
	RemediationConfig,
	ThemesConfig,
	WorkflowConfig,
	WorkflowConfigError,
)
from sentinel.workflow.pr_feedback_handler import (
	PerCommitRejection,
	PRClosedEvent,
	PRMergedEvent,
	handle_per_commit_rejection,
	handle_pr_closed_without_merge,
	handle_pr_merged,
)
from tests.conftest import advisory_config


# ---------------------------------------------------------------------------
# Profile-sweep parametrization
# ---------------------------------------------------------------------------


_PROFILE_NAMES: tuple[str, ...] = ("owner_gate", "solo_local_remediating")


def _profile_expectations(profile_name: str) -> dict[str, str]:
	"""Build the per-profile expectation table from the live config.

	Values are read from ``config.profile.transitions.*`` so the table
	tracks the canonical profile definition rather than embedding
	literals that could drift from the profile contract.
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	transitions = config.profile.transitions
	assert transitions is not None  # both sweep profiles are remediating
	return {
		"review_state": transitions.review_state,
		"approval_state": transitions.approval_state,
		"remediation_state": transitions.remediation_state,
		"closed_state": transitions.closed_state,
	}


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _pr_merged_event(
	*,
	pr_number: int = 1,
	merged_sha: str = "abc1234",
	theme_id: str = "T-001",
	member_finding_ids: tuple[str, ...] = ("F-100", "F-101"),
	finding_id_to_issue_number: dict[str, int] | None = None,
) -> PRMergedEvent:
	if finding_id_to_issue_number is None:
		finding_id_to_issue_number = {
			fid: 1000 + index
			for index, fid in enumerate(member_finding_ids, start=1)
		}
	return PRMergedEvent(
		pr_number=pr_number,
		merged_sha=merged_sha,
		theme_id=theme_id,
		member_finding_ids=member_finding_ids,
		finding_id_to_issue_number=finding_id_to_issue_number,
	)


def _pr_closed_event(
	*,
	pr_number: int = 2,
	theme_id: str = "T-002",
) -> PRClosedEvent:
	return PRClosedEvent(pr_number=pr_number, theme_id=theme_id)


def _per_commit_rejection(
	*,
	pr_number: int = 3,
	commit_sha: str = "def5678",
	theme_id: str = "T-003",
	rejected_finding_ids: tuple[str, ...] = ("F-201",),
	all_finding_ids: tuple[str, ...] = ("F-200", "F-201", "F-202"),
	finding_id_to_issue_number: dict[str, int] | None = None,
) -> PerCommitRejection:
	if finding_id_to_issue_number is None:
		finding_id_to_issue_number = {
			fid: 2000 + index
			for index, fid in enumerate(rejected_finding_ids, start=1)
		}
	return PerCommitRejection(
		pr_number=pr_number,
		commit_sha=commit_sha,
		theme_id=theme_id,
		rejected_finding_ids=rejected_finding_ids,
		all_finding_ids=all_finding_ids,
		finding_id_to_issue_number=finding_id_to_issue_number,
	)


# ---------------------------------------------------------------------------
# Test 1 — pr-merged (Site 4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_pr_feedback_pr_merged(profile_name: str) -> None:
	"""Site 4: ``handle_pr_merged`` emits a theme transition
	``review_state`` -> ``closed_state`` and one ``IssueClosurePlan``
	per member finding with reason ``"pr-merged"``.
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	expected = _profile_expectations(profile_name)
	event = _pr_merged_event(
		member_finding_ids=("F-100", "F-101"),
		finding_id_to_issue_number={"F-100": 1001, "F-101": 1002},
	)

	result = handle_pr_merged(event, config=config)

	# Theme transition: profile-portable from/to.
	assert result.theme_transition.from_state == expected["review_state"]
	assert result.theme_transition.to_state == expected["closed_state"]
	# Foundation-concept anchors.
	assert result.theme_transition.from_state == config.profile.transitions.review_state
	assert result.theme_transition.to_state == config.profile.transitions.closed_state
	assert result.theme_transition.theme_id == "T-001"
	# Closure plans: one per member finding with the merge reason.
	assert len(result.closure_plans) == len(event.member_finding_ids)
	assert all(plan.reason == "pr-merged" for plan in result.closure_plans)
	assert {plan.issue_number for plan in result.closure_plans} == {1001, 1002}


# ---------------------------------------------------------------------------
# Test 2 — pr-closed-without-merge (Site 5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_pr_feedback_pr_closed_without_merge(profile_name: str) -> None:
	"""Site 5: ``handle_pr_closed_without_merge`` returns a theme
	transition ``review_state`` -> ``approval_state`` so Sentinel can
	retry with a different remediation strategy.
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	expected = _profile_expectations(profile_name)
	event = _pr_closed_event(pr_number=42, theme_id="T-042")

	transition = handle_pr_closed_without_merge(event, config=config)

	assert transition.from_state == expected["review_state"]
	assert transition.to_state == expected["approval_state"]
	# Foundation-concept anchors.
	assert transition.from_state == config.profile.transitions.review_state
	assert transition.to_state == config.profile.transitions.approval_state
	assert transition.theme_id == "T-042"


# ---------------------------------------------------------------------------
# Test 3 — per-commit-rejection (Site 6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_pr_feedback_per_commit_rejection(profile_name: str) -> None:
	"""Site 6: ``handle_per_commit_rejection`` emits one
	``FindingStateTransition`` per rejected finding with from-state
	``review_state`` and to-state ``remediation_state``.
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	expected = _profile_expectations(profile_name)
	rejection = _per_commit_rejection(
		rejected_finding_ids=("F-201",),
		all_finding_ids=("F-200", "F-201", "F-202"),
		finding_id_to_issue_number={"F-201": 2001},
	)

	result = handle_per_commit_rejection(rejection, config=config)

	# One transition per rejected finding.
	assert len(result.rejected_transitions) == 1
	transition = result.rejected_transitions[0]
	assert transition.from_state == expected["review_state"]
	assert transition.to_state == expected["remediation_state"]
	# Foundation-concept anchors.
	assert transition.from_state == config.profile.transitions.review_state
	assert transition.to_state == config.profile.transitions.remediation_state
	assert transition.finding_id == "F-201"
	assert transition.issue_number == 2001
	# Continuing-ids invariant is profile-portable (set arithmetic).
	assert result.continuing_finding_ids == ("F-200", "F-202")


# ---------------------------------------------------------------------------
# Test 4 — advisory-raises (Grouping P standing assertion; handle_pr_merged)
# ---------------------------------------------------------------------------


def test_multi_profile_pr_feedback_advisory_profile_raises() -> None:
	"""Standing assertion: an advisory profile (``transitions=None``)
	raises ``WorkflowConfigError`` when threaded into ``handle_pr_merged``.

	``handle_pr_merged`` is the chosen handler (simplest argument
	surface). All three Grouping-P handlers share the same
	``_resolve_transitions(config)`` invocation; exercising one is
	sufficient to demonstrate the advisory contract is honored for
	the grouping.
	"""
	with pytest.raises(WorkflowConfigError):
		handle_pr_merged(_pr_merged_event(), config=advisory_config())


# ---------------------------------------------------------------------------
# Test 5 — no-config-single-warning (Grouping P standing assertion; handle_pr_merged)
# ---------------------------------------------------------------------------


def test_multi_profile_pr_feedback_no_config_emits_single_deprecation_warning() -> None:
	"""Standing assertion: ``config=None`` on ``handle_pr_merged`` emits
	exactly one ``DeprecationWarning`` (the cycle-11 Commit 1
	three-state fallback to ``_OWNER_GATE_TRANSITIONS``).

	Same handler choice as the advisory test for grouping consistency.
	"""
	with warnings.catch_warnings(record=True) as captured:
		warnings.simplefilter("always")
		handle_pr_merged(_pr_merged_event(), config=None)

	deprecations = [w for w in captured if issubclass(w.category, DeprecationWarning)]
	assert len(deprecations) == 1
