"""Reconciliation between Sentinel workflow state and GitHub Issues."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Iterable

from sentinel.workflow.config import WorkflowConfig, _resolve_transitions
from sentinel.workflow.models import (
	DriftReport,
	FieldDrift,
	FindingClosurePlanningResult,
	FindingIssueRecord,
	FindingProjectionResult,
	GitHubIssueSnapshot,
	IssueClosurePlan,
)

# Default human-override label. Commit 5 (Gap C) will wire profile config
# to substitute this; keeping a named constant marks the swap point.
_HUMAN_OVERRIDE_LABEL = "human-override"
_THEME_ROLLUP_LABELS = frozenset({"is-struct-theme", "is-concept-theme"})
_FINDING_ID_BODY_PATTERN = re.compile(r"\*\*Finding ID:\*\*\s*(\S+)")
# Sentinel-managed fields the locked Gap B scope compares between scan
# state and GitHub state. Today these are the two fields Sentinel emits
# into GitHub labels (severity:p?, state:?) and therefore the only
# fields where round-trip drift is observable without parsing the body.
_SENTINEL_MANAGED_FIELDS: tuple[str, ...] = ("severity", "status")


def parse_state_label(labels: Iterable[str]) -> str | None:
	state_labels = sorted({label.split(":", 1)[1] for label in labels if label.startswith("state:")})
	if len(state_labels) > 1:
		raise ValueError("Multiple workflow state labels found on GitHub issue")
	return state_labels[0] if state_labels else None


def _parse_severity_label(labels: Iterable[str]) -> str | None:
	severities = sorted({label.split(":", 1)[1] for label in labels if label.startswith("severity:")})
	if len(severities) > 1:
		raise ValueError("Multiple severity labels found on GitHub issue")
	return severities[0] if severities else None


def reconcile_finding_projection(
	finding: FindingIssueRecord,
	github_issue: GitHubIssueSnapshot,
	*,
	override_label: str = _HUMAN_OVERRIDE_LABEL,
	config: WorkflowConfig | None = None,
) -> FindingProjectionResult:
	"""Project authoritative GitHub issue state back onto the local finding record.

	When ``override_label`` (default ``human-override``) is present on
	the GitHub issue, the locked Gap B record-level skip applies:
	Sentinel returns the finding unchanged and surfaces a
	``skipped-projection`` ``DriftReport`` so the operator's decision is
	preserved per spec section 7.3. Otherwise the GitHub ``state:`` label
	is projected onto the record as before. If ``config`` is supplied,
	``config.profile.override_label`` takes precedence over the
	``override_label`` keyword.
	"""
	effective_label = config.profile.override_label if config is not None else override_label
	if effective_label in github_issue.labels:
		return FindingProjectionResult(
			finding=finding,
			drift_report=DriftReport(
				finding_id=finding.finding_id,
				issue_number=github_issue.issue_number,
				drift_kind="skipped-projection",
				override_label_present=True,
			),
		)
	github_state = parse_state_label(github_issue.labels)
	if github_state is None:
		return FindingProjectionResult(finding=finding)
	projected = replace(
		finding,
		status=github_state,
		human_triage=_derive_human_triage(
			github_state, finding.human_triage, config=config
		),
	)
	return FindingProjectionResult(finding=projected)


def _derive_human_triage(
	status: str,
	current_value: str | None,
	*,
	config: WorkflowConfig | None = None,
) -> str | None:
	"""Derive the human_triage label from a workflow state per the active
	profile's transition vocabulary (item 16 part A, cycle 11 Commit 3).

	Reads ``accepted_states`` and ``rejected_states`` from
	:func:`_resolve_transitions`, which itself honors the three-state
	no-config-fallback discipline locked in Commit 1: ``config=None``
	emits a :class:`DeprecationWarning` and falls back to
	``_OWNER_GATE_TRANSITIONS``; an advisory profile
	(``profile.transitions is None``) raises
	:class:`WorkflowConfigError`; an explicit configured profile
	returns its transitions as supplied.

	Behavior parity (format-compatibility pin via
	:func:`test_owner_gate_transitions_default` from Commit 1): when
	the active profile is owner_gate, this function is behaviorally
	identical to the prior hardcoded implementation. Other profiles
	(e.g. solo_local_remediating) substitute their own state
	vocabulary while preserving the same control flow.

	The literal default ``"accepted"`` is profile-portable: both
	owner_gate (via ``DriftReport`` semantics) and
	solo_local_remediating (via §1.6.3 explicit ``accepted`` state
	label) accept it as the foundation human_triage label.
	"""
	transitions = _resolve_transitions(config)
	if status in transitions.accepted_states:
		return current_value or "accepted"
	if status in transitions.rejected_states:
		return status
	return current_value


def extract_finding_id(body: str) -> str | None:
	"""Pull the finding identifier from a finding issue body.

	Mirrors the ``- **Finding ID:** <id>`` line emitted by
	``sentinel.workflow.issue_renderer.render_finding_issue_body``.
	Returns ``None`` when the body does not declare a finding id.
	"""
	match = _FINDING_ID_BODY_PATTERN.search(body or "")
	return match.group(1) if match else None


def _is_theme_rollup(issue: GitHubIssueSnapshot) -> bool:
	return any(label in _THEME_ROLLUP_LABELS for label in issue.labels)


def plan_finding_closures(
	current_finding_ids: set[str],
	open_issues: Iterable[GitHubIssueSnapshot],
	*,
	override_label: str = _HUMAN_OVERRIDE_LABEL,
	config: WorkflowConfig | None = None,
) -> FindingClosurePlanningResult:
	"""Plan reconcile-driven closures for finding issues that no longer reproduce.

	For each open issue whose embedded ``finding_id`` is absent from
	``current_finding_ids``, emit an ``IssueClosurePlan`` with reason
	``no-longer-reproduces``. Theme rollup issues (``is-struct-theme``
	/ ``is-concept-theme``) are never closed by this path. Issues
	carrying ``override_label`` (default ``human-override``) are skipped
	and surface a ``skipped-closure`` ``DriftReport`` per spec section
	7.3 -- this replaces the Commit 1 INFO log so the operator's
	decision flows back to the caller as structured data. When
	``config`` is supplied, ``config.profile.override_label`` takes
	precedence over the ``override_label`` keyword.
	"""
	effective_label = config.profile.override_label if config is not None else override_label
	plans: list[IssueClosurePlan] = []
	drifts: list[DriftReport] = []
	for issue in open_issues:
		if _is_theme_rollup(issue):
			continue
		finding_id = extract_finding_id(issue.body)
		if finding_id is None:
			continue
		if finding_id in current_finding_ids:
			continue
		if effective_label in issue.labels:
			drifts.append(
				DriftReport(
					finding_id=finding_id,
					issue_number=issue.issue_number,
					drift_kind="skipped-closure",
					override_label_present=True,
				)
			)
			continue
		plans.append(
			IssueClosurePlan(
				issue_number=issue.issue_number,
				reason="no-longer-reproduces",
			)
		)
	return FindingClosurePlanningResult(
		closure_plans=tuple(plans),
		drift_reports=tuple(drifts),
	)


def detect_drift(
	record: FindingIssueRecord,
	github_issue: GitHubIssueSnapshot,
	*,
	sentinel_managed_fields: tuple[str, ...] = _SENTINEL_MANAGED_FIELDS,
) -> DriftReport | None:
	"""Detect per-field disagreement between Sentinel state and GitHub state.

	Cycle 7 lands this primitive for use by the Phase 3 dashboard's
	human-override summary section. It is intentionally unwired in
	cycle 7: no production code path invokes it. The locked Gap B scope
	uses record-level skips in the three reconcile sites; this function
	provides the field-level audit view spec section 7.3 calls for.

	The default ``sentinel_managed_fields`` set is ``("severity",
	"status")`` -- the two fields Sentinel writes into GitHub labels.
	Future cycles can broaden the set as profiles formalize which
	fields are Sentinel-managed vs human-managed.
	"""
	details: list[FieldDrift] = []
	for field_name in sentinel_managed_fields:
		github_value = _read_github_field(field_name, github_issue)
		if github_value is None:
			continue
		sentinel_value = str(getattr(record, field_name, ""))
		if _normalize_field(field_name, sentinel_value) != _normalize_field(field_name, github_value):
			details.append(
				FieldDrift(
					field_name=field_name,
					sentinel_value=sentinel_value,
					github_value=github_value,
				)
			)
	if not details:
		return None
	return DriftReport(
		finding_id=record.finding_id,
		issue_number=github_issue.issue_number,
		drift_kind="field-disagreement",
		drift_details=tuple(details),
	)


def _read_github_field(field_name: str, github_issue: GitHubIssueSnapshot) -> str | None:
	if field_name == "status":
		return parse_state_label(github_issue.labels)
	if field_name == "severity":
		return _parse_severity_label(github_issue.labels)
	return None


def _normalize_field(field_name: str, value: str) -> str:
	if field_name == "severity":
		return value.lower()
	return value


__all__ = [
	"detect_drift",
	"extract_finding_id",
	"parse_state_label",
	"plan_finding_closures",
	"reconcile_finding_projection",
]
