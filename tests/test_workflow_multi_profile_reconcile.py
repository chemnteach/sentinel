"""Cycle 13 Commit 1: multi-profile parametrized tests for
:mod:`sentinel.workflow.reconcile_issues` (Grouping R).

This module is the codebase-portability proof for the three
profile-coupled sites in ``reconcile_issues.py``:

- Site 1: ``reconcile_finding_projection`` (override_label skip)
- Site 2: ``_derive_human_triage`` reached via
  ``reconcile_finding_projection`` (accepted_states / rejected_states)
- Site 3: ``plan_finding_closures`` (override_label skip)

It mirrors the fixture / parametrize / ``_PROFILE_EXPECTATIONS``
conventions of
``tests/test_workflow_multi_profile_demonstration.py`` (cycle 11
Commit 8) and ``tests/test_workflow_bulk_operations.py`` (cycle 12).
Per the cycle-13 design pass: the standing-assertion pattern
(advisory-profile ``WorkflowConfigError`` + no-config single
``DeprecationWarning``) applies here because Sites 1+3 invoke
``_derive_human_triage`` indirectly when the issue carries a
``state:`` label without the override label, and Site 2 invokes
``_resolve_transitions`` directly.

``_PROFILE_EXPECTATIONS`` values are read from the live
``WorkflowConfig`` object at test-setup time, not inlined as literals;
each per-site assertion includes a foundation-concept anchor
asserting equality with the corresponding ``config.profile.*``
field. ``advisory_config()`` is now imported from
``tests.conftest`` (cycle 16, Item 21 consolidation).

Site-1+3 combine decision: kept separate because each site emits a
distinct ``DriftReport.drift_kind`` value (``"skipped-projection"``
vs ``"skipped-closure"``) and exercises a distinct return type
(``FindingProjectionResult`` vs ``FindingClosurePlanningResult``).
The shared invariant ("the gating label is
``config.profile.override_label``") is verified independently at
each site rather than via a shared parametrize.
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
from sentinel.workflow.models import (
	FindingIssueRecord,
	GitHubIssueSnapshot,
	ThemeReference,
)
from tests.conftest import advisory_config
from sentinel.workflow.reconcile_issues import (
	plan_finding_closures,
	reconcile_finding_projection,
)


# ---------------------------------------------------------------------------
# Profile-sweep parametrization
# ---------------------------------------------------------------------------


_PROFILE_NAMES: tuple[str, ...] = ("owner_gate", "solo_local_remediating")


def _profile_expectations(profile_name: str) -> dict[str, object]:
	"""Build the per-profile expectation table from the live config.

	Values are read from ``config.profile.transitions.*`` and
	``config.profile.override_label`` rather than hardcoded so the
	table tracks the canonical profile definition. ``approved_state``
	and ``rejected_state`` choose a representative member of
	``accepted_states`` / ``rejected_states`` for the per-site state-
	label fixture.
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	transitions = config.profile.transitions
	assert transitions is not None  # both sweep profiles are remediating
	return {
		"override_label": config.profile.override_label,
		"approved_state": transitions.approval_state,
		"rejected_state": transitions.rejection_label,
		"accepted_states": transitions.accepted_states,
		"rejected_states": transitions.rejected_states,
	}


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _finding(
	finding_id: str = "F-100",
	*,
	status: str = "triage",
	human_triage: str | None = None,
) -> FindingIssueRecord:
	return FindingIssueRecord(
		triage_item_id=f"TD-{finding_id}",
		finding_id=finding_id,
		summary=f"summary {finding_id}",
		severity="P2",
		skill="layer2a-quick-review",
		module="m.py",
		category="error_handling",
		location="m.py:1",
		description="desc",
		suggested_fix="fix",
		status=status,
		human_triage=human_triage,
		effort_estimate="low",
		risk_of_deferral="low",
		primary_theme=ThemeReference(
			lens="struct",
			theme_id="T-001",
			title="primary",
			member_count=1,
		),
		struct_theme_id="T-001",
		concept_theme_id="",
	)


def _issue_with_state(
	state_label: str,
	*,
	issue_number: int = 101,
	body: str = "irrelevant",
) -> GitHubIssueSnapshot:
	return GitHubIssueSnapshot(
		issue_number=issue_number,
		title="t",
		body=body,
		labels=(f"state:{state_label}",),
	)


def _issue_with_override(
	override_label: str,
	*,
	issue_number: int = 102,
	body: str = "irrelevant",
	include_state: str | None = None,
) -> GitHubIssueSnapshot:
	labels: list[str] = [override_label]
	if include_state is not None:
		labels.append(f"state:{include_state}")
	return GitHubIssueSnapshot(
		issue_number=issue_number,
		title="t",
		body=body,
		labels=tuple(labels),
	)


def _finding_body(finding_id: str) -> str:
	return f"## Description\nsome detail\n\n- **Finding ID:** {finding_id}\n"


# ---------------------------------------------------------------------------
# Test 1 — projection-override-skip (Site 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_reconcile_projection_override_skip(profile_name: str) -> None:
	"""Site 1: ``reconcile_finding_projection`` skips and surfaces a
	``skipped-projection`` ``DriftReport`` when the issue carries the
	active profile's override label.
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	expected = _profile_expectations(profile_name)
	finding = _finding("F-100", status="triage")
	# Include a state: label too, to prove the override gate wins over
	# the state-projection path.
	issue = _issue_with_override(
		expected["override_label"],  # type: ignore[arg-type]
		issue_number=101,
		include_state=expected["approved_state"],  # type: ignore[arg-type]
	)

	result = reconcile_finding_projection(finding, issue, config=config)

	# Skip semantics: finding returned unchanged.
	assert result.finding == finding
	# DriftReport surfaced with the profile-portable drift_kind.
	assert result.drift_report is not None
	assert result.drift_report.drift_kind == "skipped-projection"
	assert result.drift_report.override_label_present is True
	assert result.drift_report.finding_id == "F-100"
	assert result.drift_report.issue_number == 101
	# Foundation-concept anchor: the gating label that triggered the
	# skip is the same string the live profile config publishes.
	assert expected["override_label"] == config.profile.override_label


# ---------------------------------------------------------------------------
# Test 2 — derive-accepted-path (Site 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_reconcile_derive_accepted_path(profile_name: str) -> None:
	"""Site 2: ``_derive_human_triage`` returns ``"accepted"`` when the
	GitHub state label is a member of the active profile's
	``accepted_states``. Reached through ``reconcile_finding_projection``.
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	expected = _profile_expectations(profile_name)
	finding = _finding("F-200", status="triage", human_triage=None)
	approved_state = expected["approved_state"]
	issue = _issue_with_state(approved_state)  # type: ignore[arg-type]

	result = reconcile_finding_projection(finding, issue, config=config)

	# Projection ran (no skip); state-label projected onto the record.
	assert result.drift_report is None
	assert result.finding.status == approved_state
	# Per the foundation contract: accepted_states branch returns the
	# literal "accepted" default when current_value is None.
	assert result.finding.human_triage == "accepted"
	# Foundation-concept anchors.
	assert approved_state in config.profile.transitions.accepted_states
	assert expected["accepted_states"] == config.profile.transitions.accepted_states


# ---------------------------------------------------------------------------
# Test 3 — derive-rejected-path (Site 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_reconcile_derive_rejected_path(profile_name: str) -> None:
	"""Site 2: ``_derive_human_triage`` returns the status string itself
	when the GitHub state label is a member of the active profile's
	``rejected_states``. Reached through ``reconcile_finding_projection``.
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	expected = _profile_expectations(profile_name)
	finding = _finding("F-300", status="triage", human_triage=None)
	rejected_state = expected["rejected_state"]
	issue = _issue_with_state(rejected_state)  # type: ignore[arg-type]

	result = reconcile_finding_projection(finding, issue, config=config)

	assert result.drift_report is None
	assert result.finding.status == rejected_state
	# Rejected branch: human_triage takes the status value itself
	# (per _derive_human_triage line `return status` in the
	# rejected_states branch).
	assert result.finding.human_triage == rejected_state
	# Foundation-concept anchors.
	assert rejected_state in config.profile.transitions.rejected_states
	assert expected["rejected_states"] == config.profile.transitions.rejected_states


# ---------------------------------------------------------------------------
# Test 4 — plan-closures-override-skip (Site 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_reconcile_plan_closures_override_skip(profile_name: str) -> None:
	"""Site 3: ``plan_finding_closures`` emits a ``DriftReport`` with
	``drift_kind="skipped-closure"`` for any open issue carrying the
	active profile's override label, while still emitting a closure
	plan for non-overridden issues whose finding_id is absent.
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	expected = _profile_expectations(profile_name)
	overridden_issue = GitHubIssueSnapshot(
		issue_number=201,
		title="t",
		body=_finding_body("F-overridden"),
		labels=(expected["override_label"],),  # type: ignore[arg-type]
	)
	plain_issue = GitHubIssueSnapshot(
		issue_number=202,
		title="t",
		body=_finding_body("F-plain"),
		labels=(),
	)

	result = plan_finding_closures(
		current_finding_ids=set(),
		open_issues=[overridden_issue, plain_issue],
		config=config,
	)

	# Non-overridden issue: closure plan emitted.
	assert len(result.closure_plans) == 1
	assert result.closure_plans[0].issue_number == 202
	assert result.closure_plans[0].reason == "no-longer-reproduces"
	# Overridden issue: DriftReport surfaced with the cycle-7 contract.
	assert len(result.drift_reports) == 1
	drift = result.drift_reports[0]
	assert drift.drift_kind == "skipped-closure"
	assert drift.override_label_present is True
	assert drift.finding_id == "F-overridden"
	assert drift.issue_number == 201
	# Foundation-concept anchor: the gating label came from the live
	# profile config, not the keyword default.
	assert expected["override_label"] == config.profile.override_label


# ---------------------------------------------------------------------------
# Test 5 — advisory-raises (Grouping R standing assertion)
# ---------------------------------------------------------------------------


def test_multi_profile_reconcile_advisory_profile_raises() -> None:
	"""Standing assertion: an advisory profile (``transitions=None``)
	raises ``WorkflowConfigError`` when the projection path reaches
	``_derive_human_triage``.

	Call chain: ``reconcile_finding_projection`` -> issue has a
	``state:`` label and lacks the override label, so the
	state-projection branch runs and calls
	``_derive_human_triage(github_state, ..., config=advisory)``,
	which invokes ``_resolve_transitions(advisory)``, which raises.
	"""
	# The advisory profile uses the default override_label
	# ``"human-override"``; the fixture issue must not carry it so the
	# override-skip branch does not short-circuit the call.
	finding = _finding("F-400", status="triage")
	# Use a state label that is *not* "human-override"; any non-empty
	# state value reaches _derive_human_triage. Choose ``"triage"`` as
	# a profile-neutral state present in both remediating profiles'
	# state_labels — the value choice does not matter because
	# _resolve_transitions raises before any state membership check.
	issue = _issue_with_state("triage", issue_number=401)

	with pytest.raises(WorkflowConfigError):
		reconcile_finding_projection(finding, issue, config=advisory_config())


# ---------------------------------------------------------------------------
# Test 6 — no-config-single-warning (Grouping R standing assertion)
# ---------------------------------------------------------------------------


def test_multi_profile_reconcile_no_config_emits_single_deprecation_warning() -> None:
	"""Standing assertion: ``config=None`` on the projection path that
	reaches ``_derive_human_triage`` emits exactly one
	``DeprecationWarning`` (the cycle-11 Commit 1 three-state fallback).
	"""
	finding = _finding("F-500", status="triage")
	# Use an owner_gate accepted state so behavior post-warning is the
	# documented fallback behavior, not an OOV no-op.
	issue = _issue_with_state("owner-approved", issue_number=501)

	with warnings.catch_warnings(record=True) as captured:
		warnings.simplefilter("always")
		reconcile_finding_projection(finding, issue, config=None)

	deprecations = [w for w in captured if issubclass(w.category, DeprecationWarning)]
	assert len(deprecations) == 1
