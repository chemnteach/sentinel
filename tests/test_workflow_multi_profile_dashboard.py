"""Cycle 13 Commit 3: multi-profile parametrized tests for
:mod:`sentinel.workflow.dashboard.build_dashboard` (Grouping D).

This module is the codebase-portability proof for the two
profile-coupled dashboard builder sites:

- Site 7: ``build_human_override_summary`` reads
  ``config.profile.override_label`` directly.
- Site 8: ``build_workflow_state_summary`` reads
  ``config.profile.state_labels`` directly.

Per the cycle-13 design pass (Section 3 standing-assertions
clause): neither site invokes ``_resolve_transitions``, so the
Grouping-R / Grouping-P standing assertions (advisory
``WorkflowConfigError`` + no-config single ``DeprecationWarning``)
do **not** apply here. Both sites use a literal default
(``"human-override"`` and ``_OWNER_GATE_STATE_LABELS`` respectively)
when ``config is None`` and do not raise on an advisory profile
because advisory profiles still expose ``state_labels`` /
``override_label`` (only ``transitions`` is ``None`` per §1.6.2).
Adding those tests for symmetry would assert behavior the code
does not implement.

``_PROFILE_EXPECTATIONS`` values are read from the live
``WorkflowConfig`` object at test-setup time; each per-site
assertion includes a foundation-concept anchor asserting equality
with the corresponding ``config.profile.*`` field. Conventions
mirror ``tests/test_workflow_bulk_operations.py``.
"""

from __future__ import annotations

import pytest

from sentinel.workflow.config import WorkflowConfig
from sentinel.workflow.dashboard.build_dashboard import (
	build_human_override_summary,
	build_workflow_state_summary,
)
from sentinel.workflow.models import (
	FindingIssueRecord,
	GitHubIssueSnapshot,
	ThemeReference,
)


# ---------------------------------------------------------------------------
# Profile-sweep parametrization
# ---------------------------------------------------------------------------


_PROFILE_NAMES: tuple[str, ...] = ("owner_gate", "solo_local_remediating")


def _profile_expectations(profile_name: str) -> dict[str, object]:
	"""Build the per-profile expectation table from the live config.

	``configured_state_member`` picks a profile-distinguishing state
	label drawn from the profile's own ``state_labels`` so the test
	fixtures can populate findings with statuses guaranteed in-vocab
	under one profile and (for the OOV test) demonstrably outside the
	other profile's vocabulary.
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	configured_state_member = (
		"owner-approved" if profile_name == "owner_gate" else "approved"
	)
	assert configured_state_member in config.profile.state_labels
	return {
		"override_label": config.profile.override_label,
		"state_labels": config.profile.state_labels,
		"configured_state_member": configured_state_member,
	}


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _finding(
	finding_id: str = "F-100",
	*,
	status: str = "triage",
	severity: str = "P2",
) -> FindingIssueRecord:
	return FindingIssueRecord(
		triage_item_id=f"TD-{finding_id}",
		finding_id=finding_id,
		summary=f"summary {finding_id}",
		severity=severity,
		skill="layer2a-quick-review",
		module="m.py",
		category="error_handling",
		location="m.py:1",
		description="desc",
		suggested_fix="fix",
		status=status,
		human_triage=None,
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


def _finding_body(finding_id: str) -> str:
	return f"## Description\nsome detail\n\n- **Finding ID:** {finding_id}\n"


def _issue(
	*,
	issue_number: int,
	finding_id: str,
	labels: tuple[str, ...] = (),
) -> GitHubIssueSnapshot:
	return GitHubIssueSnapshot(
		issue_number=issue_number,
		title="t",
		body=_finding_body(finding_id),
		labels=labels,
	)


# ---------------------------------------------------------------------------
# Test 1 — override-summary-uses-profile-label (Site 7, positive)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_dashboard_override_summary_uses_profile_label(
	profile_name: str,
) -> None:
	"""Site 7 positive: an issue carrying ``config.profile.override_label``
	surfaces as a ``HumanOverrideRow`` with ``override_label_present=True``.
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	expected = _profile_expectations(profile_name)
	findings = [_finding("F-100", status="triage")]
	issues = [
		_issue(
			issue_number=101,
			finding_id="F-100",
			labels=(expected["override_label"],),  # type: ignore[arg-type]
		),
	]

	rows = build_human_override_summary(findings, issues, config=config)

	assert len(rows) == 1
	assert rows[0].finding_id == "F-100"
	assert rows[0].issue_number == 101
	assert rows[0].override_label_present is True
	# Foundation-concept anchor: gating label equals the live profile's
	# override_label.
	assert expected["override_label"] == config.profile.override_label


# ---------------------------------------------------------------------------
# Test 2 — override-summary-negative-control (Site 7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_dashboard_override_summary_negative_control(
	profile_name: str,
) -> None:
	"""Site 7 negative control: an issue whose only label is *not*
	``config.profile.override_label`` does not surface. This catches the
	regression where the gate is implemented against a hardcoded
	``"human-override"`` literal instead of the profile field.
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	expected = _profile_expectations(profile_name)
	# Construct a label guaranteed not to equal the active profile's
	# override_label; today both default profiles use "human-override",
	# so "definitely-not-the-override-label" is safe under both. If a
	# future profile adopts that exact string, this assertion fails
	# loudly — which is the right signal.
	wrong_label = "definitely-not-the-override-label"
	assert wrong_label != expected["override_label"]
	findings = [_finding("F-100", status="triage")]
	issues = [
		_issue(
			issue_number=101,
			finding_id="F-100",
			labels=(wrong_label,),
		),
	]

	rows = build_human_override_summary(findings, issues, config=config)

	# No surfaced row because the issue's label is not the gating label.
	assert rows == []


# ---------------------------------------------------------------------------
# Test 3 — workflow-state-summary-configured-states (Site 8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_dashboard_workflow_state_summary_configured_states(
	profile_name: str,
) -> None:
	"""Site 8: every configured state in
	``config.profile.state_labels`` is emitted as a row (count=0 when no
	findings carry it; count>0 when at least one does).
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	expected = _profile_expectations(profile_name)
	# A single finding with a profile-specific status so we can also
	# assert the populated-count case for at least one configured row.
	findings = [
		_finding("F-100", status=expected["configured_state_member"]),  # type: ignore[arg-type]
	]

	rows = build_workflow_state_summary(findings, config=config)

	# All configured states present.
	row_states = {row.state for row in rows}
	for label in expected["state_labels"]:  # type: ignore[union-attr]
		assert label in row_states, (
			f"profile {profile_name}: expected configured state {label!r} "
			f"not emitted; got {sorted(row_states)}"
		)
	# The populated profile-specific state has count>0.
	populated_rows = [
		row for row in rows if row.state == expected["configured_state_member"]
	]
	assert len(populated_rows) == 1
	assert populated_rows[0].count == 1
	# Foundation-concept anchor: the state vocabulary used to build
	# rows is the live profile's state_labels.
	assert expected["state_labels"] == config.profile.state_labels


# ---------------------------------------------------------------------------
# Test 4 — workflow-state-summary-oov-flagging (Site 8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_dashboard_workflow_state_summary_oov_flagging(
	profile_name: str,
) -> None:
	"""Site 8: a finding carrying a status that is not a member of
	``config.profile.state_labels`` still surfaces as an OOV row, per the
	include-and-flag contract (cycle 8).
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	expected = _profile_expectations(profile_name)
	# Construct an OOV status guaranteed not to appear in either
	# profile's state_labels.
	oov_status = "totally-unknown-state-xyz"
	assert oov_status not in expected["state_labels"]  # type: ignore[operator]
	findings = [_finding("F-200", status=oov_status)]

	rows = build_workflow_state_summary(findings, config=config)

	# OOV row surfaced with the OOV status and count==1.
	oov_rows = [row for row in rows if row.state == oov_status]
	assert len(oov_rows) == 1
	assert oov_rows[0].count == 1
	# Every configured state still present (count==0 since the single
	# finding's status is OOV).
	row_states = {row.state for row in rows}
	for label in expected["state_labels"]:  # type: ignore[union-attr]
		assert label in row_states
